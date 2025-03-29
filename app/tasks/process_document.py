# ./app/tasks/process_document.py
import uuid
import asyncio
from typing import Dict, Any, Optional, List, Type
import tempfile
import os
from pathlib import Path
import structlog
import base64
import io

# --- Haystack Imports ---
from haystack import Pipeline, Document
from haystack.utils import Secret
from haystack.components.converters import (
    PyPDFToDocument,
    TextFileToDocument,
    MarkdownToDocument,
    HTMLToDocument,
    DOCXToDocument,
)
from haystack.components.preprocessors import DocumentSplitter
from haystack.components.embedders import OpenAIDocumentEmbedder # Use DocumentEmbedder for indexing
# *** CORREGIDO: Importación correcta para Haystack 2.x Milvus Integration ***
from milvus_haystack import MilvusDocumentStore
from haystack.components.writers import DocumentWriter
from haystack.dataclasses import ByteStream

# --- Local Imports ---
from app.tasks.celery_app import celery_app
from app.core.config import settings
from app.db import postgres_client
from app.models.domain import DocumentStatus
from app.services.minio_client import MinioStorageClient
# from app.services.ocr_client import OcrServiceClient # Descomentar si se usa OCR

log = structlog.get_logger(__name__)


# --- Haystack Component Initialization ---
def get_haystack_document_store() -> MilvusDocumentStore:
    """Initializes the MilvusDocumentStore."""
    # *** CORREGIDO: Usar host y port de settings para la URI de conexión K8s ***
    milvus_uri = f"http://{settings.MILVUS_HOST}:{settings.MILVUS_PORT}"
    log.debug("Initializing MilvusDocumentStore",
             uri=milvus_uri, # Usar la URI construida
             collection=settings.MILVUS_COLLECTION_NAME,
             dim=settings.EMBEDDING_DIMENSION,
             metadata_fields=settings.MILVUS_METADATA_FIELDS) # Asegúrate que esto se pasa

    return MilvusDocumentStore(
        # connection_args={"uri": milvus_uri}, # Alternativa según doc, pero uri directo funciona
        uri=milvus_uri, # URI directa para conexión a servidor Milvus (K8s)
        collection_name=settings.MILVUS_COLLECTION_NAME,
        dim=settings.EMBEDDING_DIMENSION,
        embedding_field=settings.MILVUS_EMBEDDING_FIELD,
        content_field=settings.MILVUS_CONTENT_FIELD,
        metadata_fields=settings.MILVUS_METADATA_FIELDS, # Pasar la lista de campos permitidos
        index_params=settings.MILVUS_INDEX_PARAMS,
        search_params=settings.MILVUS_SEARCH_PARAMS,
        consistency_level="Strong", # Ajustar si es necesario (e.g., "Bounded" para rendimiento)
        # drop_old=False, # Importante: NO poner True en producción sin estar seguro
    )

def get_haystack_embedder() -> OpenAIDocumentEmbedder:
    """Initializes the OpenAI Embedder for documents."""
    api_key_env_var = "OPENAI_API_KEY"
    if not os.getenv(api_key_env_var):
        log.error(f"Environment variable {api_key_env_var} not set for OpenAI Embedder.")
        # Considerar lanzar error si la clave es indispensable
        # raise ValueError(f"{api_key_env_var} not set.")
    return OpenAIDocumentEmbedder(
        api_key=Secret.from_env_var(api_key_env_var, strict=False), # strict=False para no fallar si no está, aunque dará error después
        model=settings.OPENAI_EMBEDDING_MODEL,
        # batch_size=32, # Ajustar si es necesario
        meta_fields_to_embed=[] # No embeber metadatos por defecto
    )

def get_haystack_splitter() -> DocumentSplitter:
    """Initializes the DocumentSplitter."""
    return DocumentSplitter(
        split_by=settings.SPLITTER_SPLIT_BY,
        split_length=settings.SPLITTER_CHUNK_SIZE,
        split_overlap=settings.SPLITTER_CHUNK_OVERLAP
    )

def get_converter_for_content_type(content_type: str) -> Optional[Type]:
     """Returns the appropriate Haystack Converter class."""
     if content_type == "application/pdf":
         return PyPDFToDocument
     elif content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
         return DOCXToDocument
     elif content_type == "text/plain":
         return TextFileToDocument
     elif content_type == "text/markdown":
         # Asegúrate de tener 'markdown' instalado (`pip install markdown`)
         return MarkdownToDocument
     elif content_type == "text/html":
          # Asegúrate de tener 'beautifulsoup4' instalado (`pip install beautifulsoup4`)
         return HTMLToDocument
     # Añadir más conversores si son necesarios (e.g., para imágenes si se usa OCR interno)
     else:
         log.warning("No specific Haystack converter found for content type", content_type=content_type)
         return None


# --- Celery Task ---
@celery_app.task(
    bind=True,
    autoretry_for=(Exception,), # Reintentar en excepciones genéricas
    retry_kwargs={'max_retries': 2, 'countdown': 60}, # Estrategia de reintento
    acks_late=True, # Confirmar sólo tras éxito/fracaso final
    name="tasks.process_document_haystack"
)
def process_document_haystack_task(
    self, # Instancia de la tarea (para self.request.id, etc.)
    document_id_str: str,
    company_id_str: str,
    minio_object_name: str,
    file_name: str,
    content_type: str,
    original_metadata: Dict[str, Any], # Metadatos originales de la API
):
    """
    Procesa un documento usando un pipeline Haystack (MinIO -> Haystack -> Milvus -> Supabase Status).
    """
    document_id = uuid.UUID(document_id_str)
    company_id = uuid.UUID(company_id_str)
    task_log = log.bind(document_id=str(document_id), company_id=str(company_id),
                      task_id=self.request.id, file_name=file_name, object_name=minio_object_name)
    task_log.info("Starting Haystack document processing task")

    # Usar una función async interna para poder usar await con postgres_client
    async def async_process():
        haystack_pipeline = Pipeline()
        processed_docs_count = 0
        minio_client = MinioStorageClient()
        downloaded_file_stream: Optional[io.BytesIO] = None
        document_store: Optional[MilvusDocumentStore] = None # Para manejo de conexión

        try:
            # 0. Marcar como procesando en Supabase
            await postgres_client.update_document_status(document_id, DocumentStatus.PROCESSING)
            task_log.info("Document status set to PROCESSING")

            # 1. Descargar archivo de MinIO
            task_log.info("Downloading file from MinIO...")
            downloaded_file_stream = await minio_client.download_file_stream(minio_object_name)
            file_bytes = downloaded_file_stream.getvalue()
            if not file_bytes:
                raise ValueError("Downloaded file from MinIO is empty.")
            task_log.info(f"File downloaded successfully ({len(file_bytes)} bytes)")

            # 2. Preparar Input Haystack (ByteStream) con metadatos FILTRADOS
            # Filtrar metadatos para incluir SOLO los definidos en settings.MILVUS_METADATA_FIELDS
            # y los campos clave requeridos.
            allowed_meta_keys = set(settings.MILVUS_METADATA_FIELDS)
            # Asegurar que los campos base siempre estén y sean strings si Milvus lo requiere
            doc_meta = {
                "company_id": str(company_id),
                "document_id": str(document_id),
                "file_name": file_name or "unknown",
                "file_type": content_type or "unknown",
            }
            # Añadir campos permitidos de los metadatos originales
            filtered_original_meta_count = 0
            for key, value in original_metadata.items():
                if key in allowed_meta_keys and key not in doc_meta: # Evitar sobrescribir los base
                    # Validar/Convertir tipos si es necesario antes de añadir
                    # Ejemplo simple: convertir a string si no es None
                    doc_meta[key] = str(value) if value is not None else None
                    filtered_original_meta_count += 1
                elif key not in doc_meta:
                    task_log.debug("Ignoring metadata field not in MILVUS_METADATA_FIELDS", field=key)

            task_log.debug("Filtered metadata for Haystack/Milvus", final_meta=doc_meta, original_allowed_added=filtered_original_meta_count)

            # Crear el ByteStream con los metadatos filtrados
            source_stream = ByteStream(data=file_bytes, meta=doc_meta)

            # 3. Seleccionar Conversor o Manejar OCR
            ConverterClass = get_converter_for_content_type(content_type)

            # --- Manejo OCR (Ejemplo básico - necesita implementación real) ---
            if content_type in settings.EXTERNAL_OCR_REQUIRED_CONTENT_TYPES:
                if settings.OCR_SERVICE_URL:
                     task_log.warning("External OCR path triggered but not fully implemented.")
                     # Aquí iría la lógica para llamar al servicio OCR
                     # ocr_client = OcrServiceClient() # Necesitarías este cliente
                     # try:
                     #     ocr_text = await ocr_client.process_image_bytes(file_bytes)
                     #     task_log.info("OCR processing successful")
                     #     # Crear un Document Haystack directamente con el texto OCR
                     #     # Haystack no tiene un "BytesToDocument" directo para texto plano extraído
                     #     # Necesitamos construir el pipeline de forma diferente para OCR
                     #     ocr_document = Document(content=ocr_text, meta=doc_meta) # Usar meta filtrada
                     #     pipeline_input = {"splitter": {"documents": [ocr_document]}} # Empezar desde splitter
                     #
                     #     # Construir pipeline SIN conversor
                     #     haystack_pipeline.add_component("splitter", get_haystack_splitter())
                     #     haystack_pipeline.add_component("embedder", get_haystack_embedder())
                     #     document_store = get_haystack_document_store()
                     #     haystack_pipeline.add_component("writer", DocumentWriter(document_store=document_store))
                     #     haystack_pipeline.connect("splitter.documents", "embedder.documents")
                     #     haystack_pipeline.connect("embedder.documents", "writer.documents")
                     #
                     # except Exception as ocr_err:
                     #     task_log.error("OCR service call failed", error=str(ocr_err))
                     #     raise ValueError(f"OCR processing failed: {ocr_err}") from ocr_err
                     raise NotImplementedError("External OCR path requires full implementation.") # Temporal
                else:
                     task_log.error("OCR required but OCR_SERVICE_URL not configured.")
                     raise ValueError(f"Content type {content_type} requires OCR, but service is not configured.")

            # --- Manejo con Conversores Haystack ---
            elif ConverterClass:
                 task_log.info(f"Using Haystack converter: {ConverterClass.__name__}")
                 # Inicializar DocumentStore aquí para manejar conexión por tarea
                 document_store = get_haystack_document_store()

                 haystack_pipeline.add_component("converter", ConverterClass())
                 haystack_pipeline.add_component("splitter", get_haystack_splitter())
                 haystack_pipeline.add_component("embedder", get_haystack_embedder())
                 haystack_pipeline.add_component("writer", DocumentWriter(document_store=document_store))

                 # Conectar componentes
                 haystack_pipeline.connect("converter.documents", "splitter.documents")
                 haystack_pipeline.connect("splitter.documents", "embedder.documents")
                 haystack_pipeline.connect("embedder.documents", "writer.documents")

                 # Definir input para el pipeline estándar
                 pipeline_input = {"converter": {"sources": [source_stream]}}
            else:
                 # Si no es OCR y no hay conversor, es un tipo no soportado
                 task_log.error("Unsupported content type for Haystack processing", content_type=content_type)
                 raise ValueError(f"Unsupported content type for processing: {content_type}")

            # 4. Ejecutar el Pipeline Haystack
            if not haystack_pipeline.inputs(): # Verificar si el pipeline se construyó
                 task_log.error("Haystack pipeline was not constructed correctly.")
                 raise RuntimeError("Pipeline construction failed.")

            task_log.info("Running Haystack indexing pipeline...", pipeline_input_keys=list(pipeline_input.keys()))

            # Haystack pipeline.run() es síncrono. Celery maneja concurrencia a nivel de worker/proceso.
            # Si el embedding/escritura es muy lento y bloquea, se podría usar run_in_executor,
            # pero usualmente no es necesario con Celery.
            pipeline_result = haystack_pipeline.run(pipeline_input)
            task_log.info("Haystack pipeline finished.")

            # 5. Verificar resultado y obtener contador
            # La salida exacta puede variar ligeramente entre versiones de Haystack/componentes
            writer_output = pipeline_result.get("writer", {})
            if isinstance(writer_output, dict) and "documents_written" in writer_output:
                 processed_docs_count = writer_output["documents_written"]
                 task_log.info(f"Documents written to Milvus (from writer): {processed_docs_count}")
            else:
                 # Plan B: Intentar contar desde la salida del splitter si writer no informa
                 splitter_output = pipeline_result.get("splitter", {})
                 if isinstance(splitter_output, dict) and "documents" in splitter_output:
                      processed_docs_count = len(splitter_output["documents"])
                      task_log.warning(f"Inferred processed chunk count from splitter: {processed_docs_count}",
                                       writer_output=writer_output)
                 else:
                      processed_docs_count = 0 # Asumir 0 si no se puede determinar
                      task_log.warning("Processed chunk count could not be determined, setting to 0.",
                                       pipeline_output=pipeline_result)

            # 6. Actualizar Estado Final en Supabase
            final_status = DocumentStatus.PROCESSED # O INDEXED si prefieres ese estado
            await postgres_client.update_document_status(
                document_id,
                final_status,
                chunk_count=processed_docs_count,
                # No es necesario actualizar file_path aquí, ya se hizo en la API
            )
            task_log.info("Document status set to PROCESSED/INDEXED in Supabase", chunk_count=processed_docs_count)

        except Exception as e:
            task_log.error("Error during Haystack document processing", error=str(e), exc_info=True)
            try:
                # Intentar marcar como error en Supabase
                await postgres_client.update_document_status(
                    document_id,
                    DocumentStatus.ERROR,
                    error_message=f"Processing Error: {type(e).__name__}: {str(e)[:500]}" # Truncar error
                )
                task_log.info("Document status set to ERROR in Supabase due to processing failure.")
            except Exception as db_update_err:
                # Loguear si la actualización de estado también falla
                task_log.error("Failed to update document status to ERROR after processing failure",
                               nested_error=str(db_update_err), exc_info=True)
            # Re-lanzar la excepción para que Celery la maneje (reintentos, etc.)
            raise e
        finally:
            # Limpiar recursos
            if downloaded_file_stream:
                downloaded_file_stream.close()
            # Considerar cerrar explícitamente la conexión de Milvus si es necesario,
            # aunque Haystack debería manejarla. DocumentStore no tiene un método close explícito.
            task_log.debug("Cleaned up resources for task.")

    # --- Ejecutar la lógica async dentro de la tarea síncrona de Celery ---
    try:
        # asyncio.run() crea un nuevo event loop para ejecutar la corutina
        asyncio.run(async_process())
        task_log.info("Haystack document processing task finished successfully.")
    except Exception as task_exception:
        # El error ya se ha logueado y el estado (probablemente) actualizado dentro de async_process.
        # Celery necesita ver la excepción para marcar la tarea como fallida.
        task_log.warning("Haystack processing task failed.")
        # No es necesario hacer raise aquí explícitamente, porque asyncio.run()
        # propagará la excepción que ocurrió dentro de async_process.
        pass