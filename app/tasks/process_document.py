# ./app/tasks/process_document.py (CORREGIDO)
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
from haystack.components.embedders import OpenAIDocumentEmbedder
# *** CORREGIDO: Importación correcta para Haystack 2.x Milvus Integration ***
from milvus_haystack import MilvusDocumentStore # Asegúrate que 'milvus-haystack' está instalado
from haystack.components.writers import DocumentWriter
from haystack.dataclasses import ByteStream

# --- Local Imports ---
from app.tasks.celery_app import celery_app
from app.core.config import settings
from app.db import postgres_client # Importar el cliente de base de datos
from app.models.domain import DocumentStatus
from app.services.minio_client import MinioStorageClient
# from app.services.ocr_client import OcrServiceClient # Descomentar si se usa OCR

log = structlog.get_logger(__name__)


# --- Haystack Component Initialization ---
def get_haystack_document_store() -> MilvusDocumentStore:
    """Initializes the MilvusDocumentStore."""
    # *** CORREGIDO: Usar settings.MILVUS_URI ***
    log.debug("Initializing MilvusDocumentStore",
             uri=str(settings.MILVUS_URI), # Convertir AnyHttpUrl a string si es necesario
             collection=settings.MILVUS_COLLECTION_NAME,
             dim=settings.EMBEDDING_DIMENSION,
             metadata_fields=settings.MILVUS_METADATA_FIELDS)

    return MilvusDocumentStore(
        uri=str(settings.MILVUS_URI), # Usar la URI directamente desde settings
        collection_name=settings.MILVUS_COLLECTION_NAME,
        dim=settings.EMBEDDING_DIMENSION,
        embedding_field=settings.MILVUS_EMBEDDING_FIELD,
        content_field=settings.MILVUS_CONTENT_FIELD,
        metadata_fields=settings.MILVUS_METADATA_FIELDS, # Pasar la lista de campos permitidos
        index_params=settings.MILVUS_INDEX_PARAMS,
        search_params=settings.MILVUS_SEARCH_PARAMS,
        consistency_level="Strong", # O "Bounded", "Session", "Eventually" según necesidad
        # auto_id=True, # Generalmente recomendado si no gestionas IDs manualmente
        # drop_old=False, # MUY IMPORTANTE: No poner True en producción sin estar seguro
    )

def get_haystack_embedder() -> OpenAIDocumentEmbedder:
    """Initializes the OpenAI Embedder for documents."""
    # Asegurar que la clave API esté disponible como variable de entorno
    # (El Secret handler de Haystack busca la variable INGEST_OPENAI_API_KEY por defecto)
    api_key_env_var = "INGEST_OPENAI_API_KEY" # Corresponde al prefijo de BaseSettings
    if not os.getenv(api_key_env_var):
         log.warning(f"Environment variable {api_key_env_var} not found for OpenAI Embedder. Haystack might fail.")
         # Dependiendo de tu Secret handling, podría fallar aquí o más tarde
         # Consider throwing an error immediately if the key is essential
         # raise EnvironmentError(f"{api_key_env_var} is not set in the environment.")

    return OpenAIDocumentEmbedder(
        # api_key=Secret.from_env_var(api_key_env_var, strict=False), # No necesario si usas el default de Haystack
        model=settings.OPENAI_EMBEDDING_MODEL,
        # batch_size=32, # Ajustar si es necesario
        # organization= Optional[str] # Si es necesario
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
     # (Sin cambios necesarios aquí, se mantiene como estaba)
     if content_type == "application/pdf":
         # Asegúrate de tener pypdf instalado (`pip install pypdf`)
         return PyPDFToDocument
     elif content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
         # Asegúrate de tener python-docx instalado (`pip install python-docx`)
         return DOCXToDocument
     elif content_type == "text/plain":
         return TextFileToDocument
     elif content_type == "text/markdown":
         # Asegúrate de tener 'markdown' instalado (`pip install markdown`)
         return MarkdownToDocument
     elif content_type == "text/html":
          # Asegúrate de tener 'beautifulsoup4' y 'lxml' instalados (`pip install beautifulsoup4 lxml`)
         return HTMLToDocument
     # Añadir más conversores si son necesarios
     else:
         log.warning("No specific Haystack converter found for content type", content_type=content_type)
         return None


# --- Celery Task ---
@celery_app.task(
    bind=True,
    autoretry_for=(Exception,), # Reintentar en excepciones genéricas (cuidado con errores de lógica)
    retry_kwargs={'max_retries': 2, 'countdown': 60}, # Estrategia de reintento (ajustar)
    acks_late=True, # Confirmar sólo tras éxito/fracaso final
    name="tasks.process_document_haystack" # Nombre explícito es buena práctica
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
                      task_id=self.request.id, file_name=file_name, object_name=minio_object_name, content_type=content_type)
    task_log.info("Starting Haystack document processing task")

    # Usar una función async interna para poder usar await con postgres_client y minio_client
    async def async_process():
        haystack_pipeline = Pipeline()
        processed_docs_count = 0
        minio_client = MinioStorageClient() # Asume inicialización síncrona OK en __init__
        downloaded_file_stream: Optional[io.BytesIO] = None
        document_store: Optional[MilvusDocumentStore] = None # Para manejo de conexión

        try:
            # 0. Marcar como procesando en Supabase
            # Usar el cliente postgres importado
            await postgres_client.update_document_status(document_id, DocumentStatus.PROCESSING)
            task_log.info("Document status set to PROCESSING")

            # 1. Descargar archivo de MinIO
            task_log.info("Downloading file from MinIO...")
            # El cliente MinIO actual es síncrono, pero lo llamamos desde una corutina.
            # Para E/S de red intensiva, idealmente se usaría un cliente async como aiobotocore/aioboto3 para S3/MinIO.
            # Por ahora, asumimos que la descarga es aceptablemente rápida o se usa un threadpool.
            # O ejecutar la descarga síncrona en un executor para no bloquear el event loop:
            loop = asyncio.get_running_loop()
            downloaded_file_stream = await loop.run_in_executor(
                None, # Usa el default ThreadPoolExecutor
                lambda: asyncio.run(minio_client.download_file_stream(minio_object_name)) # Correr la corutina sync
            )
            # Alternativa simple (si el cliente MinIO fuera async):
            # downloaded_file_stream = await minio_client.download_file_stream(minio_object_name)

            file_bytes = downloaded_file_stream.getvalue()
            if not file_bytes:
                raise ValueError("Downloaded file from MinIO is empty.")
            task_log.info(f"File downloaded successfully ({len(file_bytes)} bytes)")

            # 2. Preparar Input Haystack (ByteStream) con metadatos FILTRADOS
            allowed_meta_keys = set(settings.MILVUS_METADATA_FIELDS)
            doc_meta = {
                "company_id": str(company_id), # Convertir a string por si acaso Milvus lo requiere
                "document_id": str(document_id),
                "file_name": file_name or "unknown",
                "file_type": content_type or "unknown",
            }
            filtered_original_meta_count = 0
            for key, value in original_metadata.items():
                if key in allowed_meta_keys and key not in doc_meta:
                    # Asegurar que el valor es compatible con JSON/Milvus (e.g., string)
                    # Podrías añadir validación más robusta aquí si es necesario
                    doc_meta[key] = str(value) if value is not None else None
                    filtered_original_meta_count += 1
                elif key not in doc_meta:
                    task_log.debug("Ignoring metadata field not in MILVUS_METADATA_FIELDS", field=key)

            task_log.debug("Filtered metadata for Haystack/Milvus", final_meta=doc_meta, original_allowed_added=filtered_original_meta_count)
            source_stream = ByteStream(data=file_bytes, meta=doc_meta) # Pasar metadatos filtrados

            # 3. Seleccionar Conversor o Manejar OCR
            ConverterClass = get_converter_for_content_type(content_type)

            # --- Manejo OCR (Ejemplo simplificado) ---
            if content_type in settings.EXTERNAL_OCR_REQUIRED_CONTENT_TYPES:
                task_log.warning("OCR path triggered - requires implementation")
                # Aquí iría la lógica real para llamar al servicio OCR
                # Ejemplo:
                # if settings.OCR_SERVICE_URL:
                #    ocr_client = OcrServiceClient() # Necesitarías este cliente async
                #    try:
                #        ocr_text = await ocr_client.process_image_bytes(file_bytes)
                #        task_log.info("OCR processing successful")
                #        # Crear Document directamente y empezar pipeline desde splitter
                #        ocr_document = Document(content=ocr_text, meta=doc_meta)
                #        pipeline_input = {"splitter": {"documents": [ocr_document]}}
                #        # Construir pipeline SIN conversor...
                #    except Exception as ocr_err: ...
                # else: ...
                raise NotImplementedError(f"OCR processing for {content_type} not implemented.")

            # --- Manejo con Conversores Haystack ---
            elif ConverterClass:
                 task_log.info(f"Using Haystack converter: {ConverterClass.__name__}")
                 document_store = get_haystack_document_store() # Inicializar aquí

                 haystack_pipeline.add_component("converter", ConverterClass())
                 haystack_pipeline.add_component("splitter", get_haystack_splitter())
                 haystack_pipeline.add_component("embedder", get_haystack_embedder())
                 # Pasar document_store inicializado al writer
                 haystack_pipeline.add_component("writer", DocumentWriter(document_store=document_store))

                 haystack_pipeline.connect("converter.documents", "splitter.documents")
                 haystack_pipeline.connect("splitter.documents", "embedder.documents")
                 haystack_pipeline.connect("embedder.documents", "writer.documents")

                 pipeline_input = {"converter": {"sources": [source_stream]}}
            else:
                 task_log.error("Unsupported content type for Haystack processing", content_type=content_type)
                 raise ValueError(f"Unsupported content type for processing: {content_type}")

            # 4. Ejecutar el Pipeline Haystack
            if not haystack_pipeline.inputs():
                 task_log.error("Haystack pipeline was not constructed correctly.")
                 raise RuntimeError("Pipeline construction failed.")

            task_log.info("Running Haystack indexing pipeline...", pipeline_input_keys=list(pipeline_input.keys()))

            # Ejecutar pipeline síncrono en executor para no bloquear el event loop de Celery/asyncio
            # pipeline.run() puede ser bloqueante (especialmente embedding/writing)
            loop = asyncio.get_running_loop()
            pipeline_result = await loop.run_in_executor(
                None, # Usa el default ThreadPoolExecutor
                lambda: haystack_pipeline.run(pipeline_input)
            )
            task_log.info("Haystack pipeline finished.")

            # 5. Verificar resultado y obtener contador
            # (La lógica para extraer 'documents_written' se mantiene igual)
            writer_output = pipeline_result.get("writer", {})
            if isinstance(writer_output, dict) and "documents_written" in writer_output:
                 processed_docs_count = writer_output["documents_written"]
                 task_log.info(f"Documents written to Milvus (from writer): {processed_docs_count}")
            else:
                 # Plan B: Intentar contar desde la salida del splitter
                 splitter_output = pipeline_result.get("splitter", {})
                 if isinstance(splitter_output, dict) and "documents" in splitter_output:
                      processed_docs_count = len(splitter_output["documents"])
                      task_log.warning(f"Inferred processed chunk count from splitter: {processed_docs_count}", writer_output=writer_output)
                 else:
                      processed_docs_count = 0
                      task_log.warning("Processed chunk count could not be determined, setting to 0.", pipeline_output=pipeline_result)


            # 6. Actualizar Estado Final en Supabase
            final_status = DocumentStatus.PROCESSED # O INDEXED
            await postgres_client.update_document_status(
                document_id,
                final_status,
                chunk_count=processed_docs_count,
                error_message=None # Asegurar que se limpia el error
            )
            task_log.info("Document status set to PROCESSED/INDEXED in Supabase", chunk_count=processed_docs_count)

        except Exception as e:
            task_log.error("Error during Haystack document processing", error=str(e), exc_info=True)
            try:
                # Intentar marcar como error en Supabase
                await postgres_client.update_document_status(
                    document_id,
                    DocumentStatus.ERROR,
                    error_message=f"Task Error: {type(e).__name__}: {str(e)[:500]}" # Truncar error
                )
                task_log.info("Document status set to ERROR in Supabase due to processing failure.")
            except Exception as db_update_err:
                task_log.error("Failed to update document status to ERROR after processing failure",
                               nested_error=str(db_update_err), exc_info=True)
            # Re-lanzar la excepción para que Celery la maneje (reintentos, marcar como FAILURE)
            raise e
        finally:
            # Limpiar recursos
            if downloaded_file_stream:
                downloaded_file_stream.close()
            # No hay un close explícito necesario para MilvusDocumentStore en Haystack v2
            task_log.debug("Cleaned up resources for task.")

    # --- Ejecutar la lógica async dentro de la tarea síncrona de Celery ---
    try:
        # Crear un nuevo event loop para esta tarea si no existe uno
        # o obtener el existente si Celery se ejecuta con un loop (e.g., con -P gevent/eventlet)
        # asyncio.run() es más simple si no hay loop existente.
        asyncio.run(async_process())
        task_log.info("Haystack document processing task finished successfully.")
        # Retornar algo útil si es necesario (e.g., número de chunks)
        # return {"status": "success", "chunks_processed": processed_docs_count} # OJO: processed_docs_count no está en este scope
    except Exception as task_exception:
        # El error ya se ha logueado y el estado (probablemente) actualizado dentro de async_process.
        # Celery necesita ver la excepción para marcar la tarea como fallida y manejar reintentos.
        task_log.exception("Haystack processing task failed at top level.") # Loguear con traceback
        # No es necesario hacer raise explícitamente si asyncio.run propaga la excepción.
        # Si usaste try/except dentro de async_process y no relanzaste, necesitas relanzar aquí:
        raise task_exception