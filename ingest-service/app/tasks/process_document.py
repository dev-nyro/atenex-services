# ingest-service/app/tasks/process_document.py
import uuid
import asyncio
from typing import Dict, Any, Optional, List, Type
import tempfile
import os
from pathlib import Path
import structlog
import io
import time
import traceback # Para formatear excepciones

# LLM_COMMENT: Keep asyncpg import
import asyncpg

# --- Haystack Imports ---
from haystack import Pipeline, Document
from haystack.utils import Secret
from haystack.components.converters import (
    PyPDFToDocument, TextFileToDocument, MarkdownToDocument,
    HTMLToDocument, DOCXToDocument,
)
from haystack.components.preprocessors import DocumentSplitter
# LLM_COMMENT: Keep OpenAI embedder for document ingestion
from haystack.components.embedders import OpenAIDocumentEmbedder
from milvus_haystack import MilvusDocumentStore # Importación correcta
# Importar excepciones de Milvus para manejo específico si es necesario
from pymilvus.exceptions import MilvusException
# Importar excepciones de Minio para manejo específico
from minio.error import S3Error
from haystack.components.writers import DocumentWriter
from haystack.dataclasses import ByteStream

# --- Local Imports ---
from app.tasks.celery_app import celery_app
from app.core.config import settings
from app.db import postgres_client # Cliente DB async
from app.models.domain import DocumentStatus
from app.services.minio_client import MinioStorageClient # Cliente MinIO async

log = structlog.get_logger(__name__)

# --- Funciones Helper Síncronas para Haystack (Sin cambios) ---
# LLM_COMMENT: Initialization helpers remain the same logic

def _initialize_milvus_store() -> MilvusDocumentStore:
    """Función interna SÍNCRONA para inicializar MilvusDocumentStore."""
    init_log = log.bind(component="MilvusDocumentStore")
    init_log.info("Attempting to initialize...")
    try:
        store = MilvusDocumentStore(
            connection_args={"uri": str(settings.MILVUS_URI)},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            dim=settings.EMBEDDING_DIMENSION, # LLM_COMMENT: Crucial dimension setting
            embedding_field=settings.MILVUS_EMBEDDING_FIELD,
            content_field=settings.MILVUS_CONTENT_FIELD,
            metadata_fields=settings.MILVUS_METADATA_FIELDS,
            index_params=settings.MILVUS_INDEX_PARAMS,
            search_params=settings.MILVUS_SEARCH_PARAMS,
            consistency_level="Strong",
        )
        init_log.info("Initialization successful.")
        return store
    except MilvusException as me:
        init_log.error("Milvus connection/initialization failed", code=getattr(me, 'code', None), message=str(me), exc_info=True)
        raise ConnectionError(f"Milvus connection failed: {me}") from me
    except Exception as e:
        init_log.exception("Unexpected error during MilvusDocumentStore initialization")
        raise RuntimeError(f"Unexpected Milvus init error: {e}") from e

def _initialize_openai_embedder() -> OpenAIDocumentEmbedder:
    """Función interna SÍNCRONA para inicializar OpenAIDocumentEmbedder."""
    init_log = log.bind(component="OpenAIDocumentEmbedder")
    init_log.info("Initializing...")
    api_key_value = settings.OPENAI_API_KEY.get_secret_value()
    if not api_key_value:
        init_log.error("OpenAI API Key is missing!")
        raise ValueError("OpenAI API Key is required.")
    embedder = OpenAIDocumentEmbedder(
        api_key=Secret.from_token(api_key_value),
        model=settings.OPENAI_EMBEDDING_MODEL,
        # LLM_COMMENT: meta_fields_to_embed might be useful if metadata should influence embeddings
        meta_fields_to_embed=[]
    )
    init_log.info("Initialization successful.", model=settings.OPENAI_EMBEDDING_MODEL)
    return embedder

def _initialize_splitter() -> DocumentSplitter:
    """Función interna SÍNCRONA para inicializar DocumentSplitter."""
    init_log = log.bind(component="DocumentSplitter")
    init_log.info("Initializing...")
    splitter = DocumentSplitter(
        split_by=settings.SPLITTER_SPLIT_BY,
        split_length=settings.SPLITTER_CHUNK_SIZE,
        split_overlap=settings.SPLITTER_CHUNK_OVERLAP
    )
    init_log.info("Initialization successful.", split_by=settings.SPLITTER_SPLIT_BY, length=settings.SPLITTER_CHUNK_SIZE)
    return splitter

def _initialize_document_writer(store: MilvusDocumentStore) -> DocumentWriter:
    """Función interna SÍNCRONA para inicializar DocumentWriter."""
    init_log = log.bind(component="DocumentWriter")
    init_log.info("Initializing...")
    # LLM_COMMENT: Ensure policy matches desired behavior (e.g., WRITE, SKIP, OVERWRITE)
    # Default is WRITE (adds new documents, fails on ID conflict)
    writer = DocumentWriter(document_store=store, policy="WRITE")
    init_log.info("Initialization successful.")
    return writer

# LLM_COMMENT: Converter selection logic remains the same
def get_converter_for_content_type(content_type: str) -> Optional[Type]:
    """Devuelve la clase del conversor Haystack apropiada para el tipo de archivo."""
    converters = {
        "application/pdf": PyPDFToDocument,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DOCXToDocument,
        "application/msword": DOCXToDocument,
        "text/plain": TextFileToDocument,
        "text/markdown": MarkdownToDocument,
        "text/html": HTMLToDocument,
        # LLM_COMMENT: Explicitly map unsupported types to None
        "application/vnd.ms-excel": None,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": None,
        "image/png": None,
        "image/jpeg": None,
        "image/jpg": None,
    }
    # LLM_COMMENT: Normalize content type lookup
    normalized_content_type = content_type.lower().split(';')[0].strip()
    converter = converters.get(normalized_content_type)
    if converter is None:
        # LLM_COMMENT: Raise specific error for unsupported types
        log.warning("Unsupported content type received for conversion", provided_type=content_type, normalized_type=normalized_content_type)
        raise ValueError(f"Tipo de archivo '{normalized_content_type}' no soportado actualmente.") # User-facing error
    return converter

# --- Celery Task Definition ---
# LLM_COMMENT: Keep retry logic, potentially refine errors based on experience
NON_RETRYABLE_ERRORS = (FileNotFoundError, ValueError, TypeError, NotImplementedError, KeyError, AttributeError, asyncpg.exceptions.DataError, asyncpg.exceptions.IntegrityConstraintViolationError)
RETRYABLE_ERRORS = (IOError, ConnectionError, TimeoutError, S3Error, MilvusException, asyncpg.exceptions.PostgresConnectionError, asyncpg.exceptions.InterfaceError, Exception) # Added InterfaceError

@celery_app.task(
    bind=True,
    autoretry_for=RETRYABLE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=300, # 5 minutos máximo backoff
    retry_jitter=True,
    retry_kwargs={'max_retries': 3}, # Reintentar 3 veces
    reject_on_worker_lost=True,
    acks_late=True,
    name="tasks.process_document_haystack"
)
def process_document_haystack_task(
    self, # Instancia de la tarea Celery
    document_id_str: str,
    company_id_str: str,
    minio_object_name: str,
    file_name: str,
    content_type: str,
    original_metadata: Dict[str, Any],
):
    """Tarea Celery para procesar un documento usando Haystack."""
    document_id = uuid.UUID(document_id_str)
    company_id = uuid.UUID(company_id_str)
    task_log = log.bind(
        document_id=str(document_id),
        company_id=str(company_id),
        task_id=self.request.id or "unknown",
        attempt=self.request.retries + 1,
        filename=file_name,
        content_type=content_type
    )
    task_log.info("Starting Haystack document processing task execution")

    # --- Función async interna para orquestar el flujo ---
    async def async_process_flow():
        minio_client = None
        downloaded_file_stream: Optional[io.BytesIO] = None
        pipeline: Optional[Pipeline] = None
        processed_chunk_count = 0 # Initialize count

        try:
            # 0. Marcar como PROCESSING en DB
            task_log.info("Updating document status to PROCESSING")
            await postgres_client.update_document_status(document_id, DocumentStatus.PROCESSING, error_message=None) # Clear previous error

            # 1. Descargar archivo de MinIO
            task_log.info("Downloading file from MinIO")
            minio_client = MinioStorageClient()
            downloaded_file_stream = await minio_client.download_file_stream(minio_object_name)
            file_bytes = downloaded_file_stream.getvalue()
            if not file_bytes:
                raise ValueError("Downloaded file is empty.")
            task_log.info(f"File downloaded successfully ({len(file_bytes)} bytes)")

            # 2. Inicializar componentes Haystack y construir pipeline
            task_log.info("Initializing Haystack components and building pipeline via executor...")
            loop = asyncio.get_running_loop()
            # LLM_COMMENT: Run sync Haystack initializations in executor
            store = await loop.run_in_executor(None, _initialize_milvus_store)
            embedder = await loop.run_in_executor(None, _initialize_openai_embedder)
            splitter = await loop.run_in_executor(None, _initialize_splitter)
            writer = await loop.run_in_executor(None, _initialize_document_writer, store)

            ConverterClass = get_converter_for_content_type(content_type)
            # LLM_COMMENT: No need to check if ConverterClass is None, error is raised inside the function
            converter_instance = ConverterClass()

            # LLM_COMMENT: Define pipeline structure (unchanged)
            pipeline = Pipeline()
            pipeline.add_component("converter", converter_instance)
            pipeline.add_component("splitter", splitter)
            pipeline.add_component("embedder", embedder)
            pipeline.add_component("writer", writer)
            pipeline.connect("converter.documents", "splitter.documents")
            pipeline.connect("splitter.documents", "embedder.documents")
            pipeline.connect("embedder.documents", "writer.documents")
            task_log.info("Haystack pipeline built successfully.")

            # 3. Preparar Metadatos y ByteStream
            allowed_meta_keys = set(settings.MILVUS_METADATA_FIELDS)
            # LLM_COMMENT: Ensure mandatory fields for filtering/identification are present
            doc_meta = {
                "company_id": str(company_id),
                "document_id": str(document_id),
                "file_name": file_name or "unknown",
                "file_type": content_type or "unknown",
                # LLM_COMMENT: Add original upload timestamp if available and needed
                # "uploaded_at": datetime.now(timezone.utc).isoformat() # Example
            }
            # LLM_COMMENT: Merge original metadata safely, ensuring no overwrite of crucial keys
            added_original_meta = 0
            for key, value in original_metadata.items():
                if key in allowed_meta_keys and key not in doc_meta:
                    # LLM_COMMENT: Convert values to string for broader compatibility with Milvus metadata
                    doc_meta[key] = str(value) if value is not None else None
                    added_original_meta += 1
            task_log.debug("Prepared metadata for Haystack Document", final_meta=doc_meta, added_original_count=added_original_meta)

            source_stream = ByteStream(data=file_bytes, meta=doc_meta)
            pipeline_input = {"converter": {"sources": [source_stream]}}

            # 4. Ejecutar Pipeline Haystack
            task_log.info("Running Haystack pipeline via executor...")
            start_time = time.monotonic()
            # LLM_COMMENT: Run the potentially long-running pipeline.run in executor
            pipeline_result = await loop.run_in_executor(None, pipeline.run, pipeline_input)
            duration = time.monotonic() - start_time
            task_log.info(f"Haystack pipeline execution finished", duration_sec=round(duration, 2))

            # 5. Procesar Resultado y Contar Chunks
            # LLM_COMMENT: Check writer output for document count
            writer_output = pipeline_result.get("writer", {})
            if isinstance(writer_output, dict) and "documents_written" in writer_output:
                processed_chunk_count = writer_output["documents_written"]
                task_log.info(f"Chunks written to Milvus determined by writer: {processed_chunk_count}")
            else:
                # LLM_COMMENT: Fallback: check splitter output if writer doesn't report count
                task_log.warning("Writer output missing 'documents_written', attempting fallback count from splitter", writer_output=writer_output)
                splitter_output = pipeline_result.get("splitter", {})
                if isinstance(splitter_output, dict) and "documents" in splitter_output and isinstance(splitter_output["documents"], list):
                     processed_chunk_count = len(splitter_output["documents"])
                     task_log.warning(f"Inferred chunk count from splitter output: {processed_chunk_count}")
                else:
                    # LLM_COMMENT: If count cannot be determined, log error and potentially mark as error
                    task_log.error("Pipeline finished but failed to determine processed chunk count.", pipeline_output=pipeline_result)
                    # LLM_COMMENT: Decide if 0 chunks is an error or valid (e.g., empty doc after conversion)
                    # Raising an error here will mark the task as failed.
                    raise RuntimeError("Pipeline execution yielded unclear results regarding written documents.")

            if processed_chunk_count == 0:
                 task_log.warning("Pipeline ran successfully but resulted in 0 chunks being written to Milvus.")
                 # LLM_COMMENT: Mark as processed with 0 chunks, not necessarily an error unless expected otherwise.

            # 6. Actualizar Estado Final en DB
            # LLM_COMMENT: Use PROCESSED status after successful pipeline run
            final_status = DocumentStatus.PROCESSED
            task_log.info(f"Updating document status to {final_status.value} with chunk count {processed_chunk_count}.")
            await postgres_client.update_document_status(
                document_id=document_id,
                status=final_status,
                chunk_count=processed_chunk_count,
                error_message=None # LLM_COMMENT: Clear error message on success
            )
            task_log.info("Document status updated successfully in PostgreSQL.")

        # LLM_COMMENT: Handle non-retryable errors (e.g., bad file type, config issues)
        except NON_RETRYABLE_ERRORS as e_non_retry:
            err_type = type(e_non_retry).__name__
            err_msg_detail = str(e_non_retry)[:500] # Truncate long messages
            # LLM_COMMENT: Provide a more user-friendly message for common non-retryable errors
            if isinstance(e_non_retry, FileNotFoundError):
                 user_error_msg = "Error Interno: No se encontró el archivo original en el almacenamiento."
            elif isinstance(e_non_retry, ValueError) and "Unsupported content type" in err_msg_detail:
                 user_error_msg = err_msg_detail # Use the specific message from get_converter
            elif isinstance(e_non_retry, ValueError) and "API Key" in err_msg_detail:
                 user_error_msg = "Error de Configuración: Falta la clave API necesaria."
            else:
                 user_error_msg = f"Error irrecuperable durante el procesamiento ({err_type}). Contacte a soporte si persiste."

            formatted_traceback = traceback.format_exc()
            task_log.error(f"Processing failed permanently: {err_type}: {err_msg_detail}", traceback=formatted_traceback)
            try:
                await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=user_error_msg)
            except Exception as db_err:
                task_log.critical("Failed update status to ERROR after non-retryable failure!", db_error=str(db_err))
            # Do not re-raise, let Celery mark task as failed

        # LLM_COMMENT: Handle retryable errors (e.g., network, temporary service issues)
        except RETRYABLE_ERRORS as e_retry:
            err_type = type(e_retry).__name__
            err_msg_detail = str(e_retry)[:500]
            max_retries = self.max_retries if hasattr(self, 'max_retries') else 3 # Get max retries safely
            current_attempt = self.request.retries + 1
            # LLM_COMMENT: User-friendly message indicating a temporary issue
            user_error_msg = f"Error temporal durante procesamiento ({err_type} - Intento {current_attempt}/{max_retries+1}). Reintentando automáticamente."

            task_log.warning(f"Processing failed, will retry: {err_type}: {err_msg_detail}", traceback=traceback.format_exc())
            try:
                # LLM_COMMENT: Update status to ERROR but include the user-friendly temporary error message
                await postgres_client.update_document_status(
                    document_id, DocumentStatus.ERROR, error_message=user_error_msg
                )
            except Exception as db_err:
                task_log.error("Failed update status to ERROR during retryable failure!", db_error=str(db_err))
            # Re-raise the exception for Celery to handle the retry
            raise e_retry

        finally:
            # LLM_COMMENT: Ensure resources like file streams are closed
            if downloaded_file_stream:
                downloaded_file_stream.close()
            task_log.debug("Cleaned up task resources.")

    # --- Ejecutar el flujo async ---
    try:
        # LLM_COMMENT: Set a reasonable timeout for the entire async flow within the task
        TIMEOUT_SECONDS = 600 # 10 minutes, adjust as needed
        try:
            asyncio.run(asyncio.wait_for(async_process_flow(), timeout=TIMEOUT_SECONDS))
            task_log.info("Haystack document processing task completed successfully.")
            return {"status": "success", "document_id": str(document_id), "chunk_count": processed_chunk_count} # LLM_COMMENT: Return chunk count on success
        except asyncio.TimeoutError:
            timeout_msg = f"Procesamiento excedió el tiempo límite de {TIMEOUT_SECONDS} segundos."
            task_log.error(timeout_msg)
            user_error_msg = "El procesamiento del documento tardó demasiado y fue cancelado."
            try:
                # LLM_COMMENT: Ensure status is updated to ERROR on timeout
                asyncio.run(postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=user_error_msg))
            except Exception as db_err:
                task_log.critical("Failed to update status to ERROR after timeout!", db_error=str(db_err))
            # LLM_COMMENT: Return failure status on timeout
            return {"status": "failure", "document_id": str(document_id), "error": user_error_msg}

    except Exception as top_level_exc:
        # LLM_COMMENT: Catch-all for final failures after retries or unexpected sync errors
        user_error_msg = "Ocurrió un error final inesperado durante el procesamiento. Contacte a soporte."
        task_log.exception("Haystack processing task failed at top level (after retries or sync error). Final failure.", exc_info=top_level_exc)
        try:
            # LLM_COMMENT: Ensure final status is ERROR
            asyncio.run(postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=user_error_msg))
        except Exception as db_err:
            task_log.critical("Failed to update status to ERROR after top-level failure!", db_error=str(db_err))
        # LLM_COMMENT: Return failure status
        return {"status": "failure", "document_id": str(document_id), "error": user_error_msg}