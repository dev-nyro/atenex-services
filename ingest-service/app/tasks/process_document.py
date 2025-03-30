# ./app/tasks/process_document.py (CORREGIDO - Llamada a MinIO en executor)
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
from milvus_haystack import MilvusDocumentStore # Asegúrate que esté importado
from haystack.components.writers import DocumentWriter
from haystack.dataclasses import ByteStream

# --- Local Imports ---
from app.tasks.celery_app import celery_app
from app.core.config import settings
from app.db import postgres_client
from app.models.domain import DocumentStatus
from app.services.minio_client import MinioStorageClient # Importar cliente corregido

log = structlog.get_logger(__name__)

# --- Funciones de inicialización de Haystack (sin cambios necesarios) ---
def get_haystack_document_store() -> MilvusDocumentStore:
    """Initializes the MilvusDocumentStore."""
    log.debug("Initializing MilvusDocumentStore",
             uri=str(settings.MILVUS_URI),
             collection=settings.MILVUS_COLLECTION_NAME,
             dim=settings.EMBEDDING_DIMENSION,
             metadata_fields=settings.MILVUS_METADATA_FIELDS)
    # Asegúrate que los parámetros coinciden con tu versión de Milvus y Haystack
    return MilvusDocumentStore(
        uri=str(settings.MILVUS_URI),
        collection_name=settings.MILVUS_COLLECTION_NAME,
        dim=settings.EMBEDDING_DIMENSION,
        embedding_field=settings.MILVUS_EMBEDDING_FIELD,
        content_field=settings.MILVUS_CONTENT_FIELD,
        metadata_fields=settings.MILVUS_METADATA_FIELDS,
        index_params=settings.MILVUS_INDEX_PARAMS,
        search_params=settings.MILVUS_SEARCH_PARAMS,
        consistency_level="Strong", # O el nivel que necesites
    )

def get_haystack_embedder() -> OpenAIDocumentEmbedder:
    """Initializes the OpenAI Embedder for documents."""
    api_key_env_var = "INGEST_OPENAI_API_KEY" # La variable de entorno real según tu config
    if not settings.OPENAI_API_KEY.get_secret_value():
         log.warning(f"OpenAI API Key not found in settings. Haystack embedding might fail.")
    return OpenAIDocumentEmbedder(
        api_key=Secret.from_env_var(api_key_env_var) if os.getenv(api_key_env_var) else Secret.from_token(settings.OPENAI_API_KEY.get_secret_value()),
        model=settings.OPENAI_EMBEDDING_MODEL,
        meta_fields_to_embed=[] # Ajusta si necesitas embeber metadatos
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
     if content_type == "application/pdf": return PyPDFToDocument
     elif content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document": return DOCXToDocument
     elif content_type == "text/plain": return TextFileToDocument
     elif content_type == "text/markdown": return MarkdownToDocument
     elif content_type == "text/html": return HTMLToDocument
     # Añadir más conversores si son necesarios
     else:
         log.warning("No specific Haystack converter found for content type", content_type=content_type)
         return None


# --- Celery Task ---
@celery_app.task(
    bind=True,
    autoretry_for=(Exception,), # Reintenta en cualquier excepción (ajusta si es necesario)
    retry_kwargs={'max_retries': 2, 'countdown': 60}, # 2 reintentos con 60s de espera
    acks_late=True, # Reconoce el mensaje solo después de completar o fallar definitivamente
    name="tasks.process_document_haystack"
)
def process_document_haystack_task(
    self, # Instancia de la tarea (proporcionada por bind=True)
    document_id_str: str,
    company_id_str: str,
    minio_object_name: str,
    file_name: str,
    content_type: str,
    original_metadata: Dict[str, Any],
):
    """
    Procesa un documento usando un pipeline Haystack (MinIO -> Haystack -> Milvus -> Supabase Status).
    """
    document_id = uuid.UUID(document_id_str)
    company_id = uuid.UUID(company_id_str)
    task_log = log.bind(document_id=str(document_id), company_id=str(company_id),
                      task_id=self.request.id, file_name=file_name, object_name=minio_object_name, content_type=content_type)
    task_log.info("Starting Haystack document processing task")

    # Usar una función async interna para poder usar await con postgres_client y run_in_executor
    async def async_process():
        haystack_pipeline = Pipeline()
        processed_docs_count = 0
        # Crear instancia del cliente MinIO aquí dentro
        minio_client = MinioStorageClient()
        downloaded_file_stream: Optional[io.BytesIO] = None
        document_store: Optional[MilvusDocumentStore] = None

        try:
            # 0. Marcar como procesando en Supabase
            await postgres_client.update_document_status(document_id, DocumentStatus.PROCESSING)
            task_log.info("Document status set to PROCESSING")

            # 1. Descargar archivo de MinIO (usando executor para la llamada síncrona)
            task_log.info("Downloading file from MinIO via executor...")
            loop = asyncio.get_running_loop()
            try:
                # *** CORREGIDO: Llamar a la función síncrona en el executor ***
                downloaded_file_stream = await loop.run_in_executor(
                    None, # Usa el default ThreadPoolExecutor
                    minio_client.download_file_stream_sync, # Llama a la función sync corregida
                    minio_object_name
                )
            except FileNotFoundError as fnf_err:
                 # Si el archivo no existe, no tiene sentido reintentar. Marcar como error y salir.
                 task_log.error("File not found in MinIO storage. Cannot process.", object_name=minio_object_name, error=str(fnf_err))
                 await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message="File not found in storage")
                 # No relanzamos la excepción aquí para que Celery no intente reintentar por FileNotFoundError
                 return # Salir de la función async_process
            except Exception as download_err:
                 # Otros errores de descarga sí podrían beneficiarse de un reintento
                 task_log.error("Failed to download file from MinIO.", error=str(download_err), exc_info=True)
                 raise download_err # Relanzar para que Celery reintente

            file_bytes = downloaded_file_stream.getvalue()
            if not file_bytes:
                # Si el archivo está vacío, marcar como error y salir.
                task_log.error("Downloaded file from MinIO is empty.")
                await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message="Downloaded file is empty")
                return # Salir de la función async_process
            task_log.info(f"File downloaded successfully via executor ({len(file_bytes)} bytes)")

            # 2. Preparar Input Haystack (ByteStream) con metadatos FILTRADOS
            # (Sin cambios en esta lógica, parece correcta)
            allowed_meta_keys = set(settings.MILVUS_METADATA_FIELDS)
            # Asegurar que los IDs son strings para Milvus/Haystack
            doc_meta = {
                "company_id": str(company_id),
                "document_id": str(document_id),
                "file_name": file_name or "unknown",
                "file_type": content_type or "unknown",
            }
            # Añadir metadatos originales si están permitidos y no son claves reservadas
            filtered_original_meta_count = 0
            for key, value in original_metadata.items():
                if key in allowed_meta_keys and key not in doc_meta:
                    # Convertir a string para asegurar compatibilidad
                    doc_meta[key] = str(value) if value is not None else None
                    filtered_original_meta_count += 1
                elif key not in doc_meta:
                    task_log.debug("Ignoring metadata field not in MILVUS_METADATA_FIELDS", field=key)

            task_log.debug("Filtered metadata for Haystack/Milvus", final_meta=doc_meta, original_allowed_added=filtered_original_meta_count)
            source_stream = ByteStream(data=file_bytes, meta=doc_meta)

            # 3. Seleccionar Conversor o Manejar OCR
            # (Sin cambios en esta lógica)
            ConverterClass = get_converter_for_content_type(content_type)
            if content_type in settings.EXTERNAL_OCR_REQUIRED_CONTENT_TYPES:
                # Aquí iría la lógica para llamar a un servicio OCR externo si estuviera implementado
                task_log.error("OCR processing required but not implemented.", content_type=content_type)
                raise NotImplementedError(f"OCR processing for {content_type} not implemented.")
            elif ConverterClass:
                 task_log.info(f"Using Haystack converter: {ConverterClass.__name__}")
                 document_store = get_haystack_document_store() # Inicializar store
                 haystack_pipeline.add_component("converter", ConverterClass())
                 haystack_pipeline.add_component("splitter", get_haystack_splitter())
                 haystack_pipeline.add_component("embedder", get_haystack_embedder())
                 haystack_pipeline.add_component("writer", DocumentWriter(document_store=document_store))
                 # Conectar los componentes en orden
                 haystack_pipeline.connect("converter.documents", "splitter.documents")
                 haystack_pipeline.connect("splitter.documents", "embedder.documents")
                 haystack_pipeline.connect("embedder.documents", "writer.documents")
                 pipeline_input = {"converter": {"sources": [source_stream]}}
            else:
                 # Si no hay conversor y no es OCR, es un tipo no soportado
                 task_log.error("Unsupported content type for Haystack processing", content_type=content_type)
                 raise ValueError(f"Unsupported content type for processing: {content_type}")

            # 4. Ejecutar el Pipeline Haystack (usando executor porque puede ser bloqueante)
            if not haystack_pipeline.inputs: # Verificar si la pipeline se construyó
                 raise RuntimeError("Haystack pipeline construction failed or is empty.")

            task_log.info("Running Haystack indexing pipeline via executor...", pipeline_input_keys=list(pipeline_input.keys()))
            loop = asyncio.get_running_loop()
            # Ejecutar el pipeline síncrono en el executor
            pipeline_result = await loop.run_in_executor(
                None, # Default executor
                lambda: haystack_pipeline.run(pipeline_input)
            )
            task_log.info("Haystack pipeline finished via executor.")

            # 5. Verificar resultado y obtener contador de chunks/documentos procesados
            # (Sin cambios en esta lógica)
            writer_output = pipeline_result.get("writer", {})
            # Haystack 2.x: el output del writer suele ser {"documents_written": count}
            if isinstance(writer_output, dict) and "documents_written" in writer_output:
                 processed_docs_count = writer_output["documents_written"]
                 task_log.info(f"Chunks/Documents written to Milvus (from writer output): {processed_docs_count}")
            else:
                 # Fallback: intentar contar desde el splitter si el writer no informa
                 splitter_output = pipeline_result.get("splitter", {})
                 if isinstance(splitter_output, dict) and "documents" in splitter_output:
                      processed_docs_count = len(splitter_output["documents"])
                      task_log.warning(f"Could not get count from writer, inferred processed chunk count from splitter: {processed_docs_count}", writer_output=writer_output)
                 else:
                      processed_docs_count = 0 # No se pudo determinar
                      task_log.warning("Processed chunk count could not be determined from pipeline output, setting to 0.", pipeline_output=pipeline_result)

            # 6. Actualizar Estado Final en Supabase como PROCESSED (o INDEXED si prefieres)
            final_status = DocumentStatus.PROCESSED # O DocumentStatus.INDEXED
            await postgres_client.update_document_status(
                document_id, final_status, chunk_count=processed_docs_count, error_message=None # Limpiar mensaje de error
            )
            task_log.info("Document status set to PROCESSED/INDEXED in Supabase", chunk_count=processed_docs_count)

        except Exception as e:
            # Captura cualquier excepción durante el proceso
            task_log.error("Error during Haystack document processing", error=str(e), exc_info=True)
            try:
                # Intenta marcar como error en la BD
                await postgres_client.update_document_status(
                    document_id, DocumentStatus.ERROR, error_message=f"Task Error: {type(e).__name__}: {str(e)[:500]}" # Limita longitud del error
                )
                task_log.info("Document status set to ERROR in Supabase due to processing failure.")
            except Exception as db_update_err:
                # Loguea si falla la actualización de estado a ERROR
                task_log.error("CRITICAL: Failed to update document status to ERROR after processing failure", nested_error=str(db_update_err), exc_info=True)
            # Re-lanza la excepción original para que Celery la vea y maneje reintentos/fallo
            raise e
        finally:
            # Asegurar limpieza de recursos
            if downloaded_file_stream:
                downloaded_file_stream.close()
            # Si se inicializó el document_store, podrías cerrarlo si es necesario (revisar documentación de MilvusDocumentStore)
            # if document_store: await document_store.close() # O método similar si existe y es async
            task_log.debug("Cleaned up resources for task.")

    # --- Ejecutar la lógica async dentro de la tarea síncrona de Celery ---
    try:
        # Ejecuta la función async_process hasta que complete
        asyncio.run(async_process())
        task_log.info("Haystack document processing task finished successfully.")
    except Exception as task_exception:
        # Si async_process lanzó una excepción (y no fue FileNotFoundError o archivo vacío manejados internamente)
        # Celery necesita ver la excepción para marcar la tarea como fallida y potencialmente reintentar.
        task_log.exception("Haystack processing task failed at top level after potential retries.")
        # La excepción ya fue relanzada desde async_process, Celery la capturará.
        # No es necesario relanzar explícitamente aquí si ya se hizo en async_process.
        # Si quieres asegurarte, puedes añadir: raise task_exception
        pass # La excepción ya se propagó y Celery la manejará