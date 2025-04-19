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

import asyncpg # Importar para tipos de excepción

# --- Haystack Imports ---
from haystack import Pipeline, Document
from haystack.utils import Secret
from haystack.components.converters import (
    PyPDFToDocument, TextFileToDocument, MarkdownToDocument,
    HTMLToDocument, DOCXToDocument,
)
from haystack.components.preprocessors import DocumentSplitter
from haystack.components.embedders import OpenAIDocumentEmbedder
from milvus_haystack import MilvusDocumentStore # Importación correcta
from pymilvus.exceptions import MilvusException # Importar excepciones de Milvus
from minio.error import S3Error # Importar excepciones de Minio
from haystack.components.writers import DocumentWriter
from haystack.dataclasses import ByteStream

# --- Local Imports ---
from app.tasks.celery_app import celery_app
from app.core.config import settings
from app.db import postgres_client # Cliente DB async
from app.models.domain import DocumentStatus
from app.services.minio_client import MinioStorageClient # Cliente MinIO async

log = structlog.get_logger(__name__)

# --- Funciones Helper Síncronas para Haystack ---
def _initialize_milvus_store() -> MilvusDocumentStore:
    """Función interna SÍNCRONA para inicializar MilvusDocumentStore."""
    init_log = log.bind(component="MilvusDocumentStore")
    init_log.info("Attempting to initialize MilvusDocumentStore...")
    try:
        # *** CORRECCIÓN DEFINITIVA: Usar embedding_dim consistentemente ***
        store = MilvusDocumentStore(
            connection_args={"uri": str(settings.MILVUS_URI)},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            embedding_dim=settings.EMBEDDING_DIMENSION, # CORREGIDO
            embedding_field=settings.MILVUS_EMBEDDING_FIELD,
            content_field=settings.MILVUS_CONTENT_FIELD,
            metadata_fields=settings.MILVUS_METADATA_FIELDS,
            index_params=settings.MILVUS_INDEX_PARAMS,
            search_params=settings.MILVUS_SEARCH_PARAMS,
            consistency_level="Strong",
        )
        init_log.info("MilvusDocumentStore initialization successful.")
        return store
    except MilvusException as me:
        init_log.error("Milvus connection/initialization failed", code=getattr(me, 'code', None), message=str(me), exc_info=True)
        raise ConnectionError(f"Milvus connection failed: {me}") from me
    except TypeError as te:
        # Capturar el error específico de argumento inesperado
        init_log.error(f"MilvusDocumentStore init TypeError: {te}. Check arguments (e.g., 'embedding_dim').", exc_info=True)
        raise RuntimeError(f"Milvus TypeError (check arguments like embedding_dim): {te}") from te
    except Exception as e:
        init_log.exception("Unexpected error during MilvusDocumentStore initialization")
        raise RuntimeError(f"Unexpected Milvus init error: {e}") from e

# --- (El resto de helpers _initialize_* no cambian) ---
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
    writer = DocumentWriter(document_store=store, policy="OVERWRITE")
    init_log.info("Initialization successful.")
    return writer

def get_converter_for_content_type(content_type: str) -> Optional[Type]:
    """Devuelve la clase del conversor Haystack apropiada para el tipo de archivo."""
    converters = {
        "application/pdf": PyPDFToDocument,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DOCXToDocument,
        "application/msword": DOCXToDocument,
        "text/plain": TextFileToDocument,
        "text/markdown": MarkdownToDocument,
        "text/html": HTMLToDocument,
        "application/vnd.ms-excel": None,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": None,
        "image/png": None,
        "image/jpeg": None,
        "image/jpg": None,
    }
    normalized_content_type = content_type.lower().split(';')[0].strip()
    converter = converters.get(normalized_content_type)
    if converter is None:
        log.warning("Unsupported content type received for conversion", provided_type=content_type, normalized_type=normalized_content_type)
        raise ValueError(f"Tipo de archivo '{normalized_content_type}' no soportado actualmente.")
    return converter

# --- Celery Task Definition ---
# Añadir RuntimeError a los no reintentables (por si falla init de Milvus por TypeError)
NON_RETRYABLE_ERRORS = (FileNotFoundError, ValueError, TypeError, NotImplementedError, KeyError, AttributeError, asyncpg.exceptions.DataError, asyncpg.exceptions.IntegrityConstraintViolationError, RuntimeError)
# Añadir ConnectionError y ConnectionDoesNotExistError
RETRYABLE_ERRORS = (IOError, ConnectionError, TimeoutError, S3Error, MilvusException, asyncpg.exceptions.PostgresConnectionError, asyncpg.exceptions.InterfaceError, asyncpg.exceptions.ConnectionDoesNotExistError, asyncio.TimeoutError)

# --- Helper para actualizar estado de forma robusta ---
async def _robust_update_status(doc_id: uuid.UUID, status: DocumentStatus, message: Optional[str] = None, chunk_count: Optional[int] = None):
    """Intenta actualizar el estado, reintentando una vez en caso de error de conexión."""
    update_log = log.bind(document_id=str(doc_id), target_status=status.value)
    for attempt in range(2): # Intentar 2 veces
        try:
            # Asegurarse de tener una conexión fresca si la anterior falló
            await postgres_client.get_db_pool() # Re-asegura que el pool esté vivo (no lanza error si ya existe)
            success = await postgres_client.update_document_status(doc_id, status, error_message=message, chunk_count=chunk_count)
            if success:
                 update_log.info("Successfully updated final document status.", attempt=attempt+1)
                 return True
            else:
                 # Si update_document_status devuelve False, el documento no existía
                 update_log.error("Document not found during final status update attempt.", attempt=attempt+1)
                 return False # No tiene sentido reintentar si no existe
        except (asyncpg.exceptions.PostgresConnectionError, asyncpg.exceptions.InterfaceError, asyncpg.exceptions.ConnectionDoesNotExistError) as db_conn_err:
            update_log.warning("Connection error during final status update", attempt=attempt+1, error=str(db_conn_err))
            if attempt == 0:
                update_log.info("Waiting before retrying final status update...")
                await asyncio.sleep(3) # Esperar 3 segundos antes de reintentar
            else:
                update_log.critical("Failed to update final document status after retry due to connection error.", error=str(db_conn_err))
                return False # Falló después de reintentar
        except Exception as e:
            update_log.critical("Unexpected error during final status update", attempt=attempt+1, error=str(e), exc_info=True)
            return False # Falló por otra razón
    return False # Si por alguna razón sale del bucle


@celery_app.task(
    bind=True,
    autoretry_for=RETRYABLE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    retry_kwargs={'max_retries': 3},
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
    processed_chunk_count = 0
    # Establecer valores por defecto para el bloque finally
    final_status = DocumentStatus.ERROR
    final_error_message = "Error inesperado al inicio de la tarea."
    task_exception = None # Para guardar la excepción original si ocurre

    async def async_process_flow():
        # Usar nonlocal para modificar las variables del scope exterior
        nonlocal processed_chunk_count, final_status, final_error_message
        minio_client = None
        downloaded_file_stream: Optional[io.BytesIO] = None
        pipeline: Optional[Pipeline] = None

        try:
            # 0. Marcar como PROCESSING en DB
            task_log.info("Updating document status to PROCESSING")
            update_success = await postgres_client.update_document_status(document_id, DocumentStatus.PROCESSING, error_message=None)
            if not update_success:
                task_log.warning("Document record not found in DB before processing started. Aborting task.")
                final_status = DocumentStatus.ERROR
                final_error_message = "Registro del documento no encontrado al iniciar procesamiento."
                # Levantar error no reintentable para detener aquí
                raise FileNotFoundError(final_error_message)

            # 1. Descargar archivo de MinIO
            task_log.info("Downloading file from MinIO")
            minio_client = MinioStorageClient()
            downloaded_file_stream = await minio_client.download_file_stream(minio_object_name)
            file_bytes = downloaded_file_stream.getvalue()
            if not file_bytes: raise ValueError("Downloaded file is empty.")
            task_log.info(f"File downloaded successfully ({len(file_bytes)} bytes)")

            # 2. Inicializar componentes Haystack y construir pipeline
            task_log.info("Initializing Haystack components and building pipeline via executor...")
            loop = asyncio.get_running_loop()
            store = await loop.run_in_executor(None, _initialize_milvus_store) # Puede lanzar error
            embedder = await loop.run_in_executor(None, _initialize_openai_embedder)
            splitter = await loop.run_in_executor(None, _initialize_splitter)
            writer = await loop.run_in_executor(None, _initialize_document_writer, store)
            ConverterClass = get_converter_for_content_type(content_type)
            converter_instance = ConverterClass()

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
            doc_meta = {
                "company_id": str(company_id),
                "document_id": str(document_id),
                "file_name": file_name or "unknown",
                "file_type": content_type or "unknown",
            }
            added_original_meta = 0
            for key, value in original_metadata.items():
                if key in allowed_meta_keys and key not in doc_meta:
                    doc_meta[key] = str(value) if value is not None else None
                    added_original_meta += 1
            task_log.debug("Prepared metadata for Haystack Document", final_meta=doc_meta, added_original_count=added_original_meta)

            source_stream = ByteStream(data=file_bytes, meta=doc_meta)
            pipeline_input = {"converter": {"sources": [source_stream]}}

            # 4. Ejecutar Pipeline Haystack
            task_log.info("Running Haystack pipeline via executor...")
            start_time = time.monotonic()
            pipeline_result = await loop.run_in_executor(None, pipeline.run, pipeline_input)
            duration = time.monotonic() - start_time
            task_log.info(f"Haystack pipeline execution finished", duration_sec=round(duration, 2))

            # 5. Procesar Resultado y Contar Chunks
            writer_output = pipeline_result.get("writer", {})
            if isinstance(writer_output, dict) and "documents_written" in writer_output:
                processed_chunk_count = writer_output["documents_written"]
                task_log.info(f"Chunks written to Milvus determined by writer: {processed_chunk_count}")
            else:
                 task_log.warning("Writer output missing 'documents_written', attempting fallback count from splitter", writer_output=writer_output)
                 splitter_output = pipeline_result.get("splitter", {})
                 if isinstance(splitter_output, dict) and "documents" in splitter_output and isinstance(splitter_output["documents"], list):
                      processed_chunk_count = len(splitter_output["documents"])
                      task_log.warning(f"Inferred chunk count from splitter output: {processed_chunk_count}")
                 else:
                      task_log.error("Pipeline finished but failed to determine processed chunk count.", pipeline_output=pipeline_result)
                      raise RuntimeError("Pipeline execution yielded unclear results regarding written documents.")

            if processed_chunk_count == 0:
                 task_log.warning("Pipeline ran successfully but resulted in 0 chunks being written to Milvus.")

            # 6. Éxito: Marcar estado final como PROCESSED
            final_status = DocumentStatus.PROCESSED
            final_error_message = None # Limpiar mensaje de error en éxito
            task_log.info("Haystack pipeline completed successfully.")

        # *** Manejo de errores dentro del flujo async ***
        except NON_RETRYABLE_ERRORS as e_non_retry:
            err_type = type(e_non_retry).__name__
            err_msg_detail = str(e_non_retry)[:500]
            user_error_msg = f"Error irrecuperable ({err_type}). Verifique el archivo o contacte a soporte."
            if isinstance(e_non_retry, ValueError) and ("Unsupported content type" in err_msg_detail or "API Key" in err_msg_detail):
                 user_error_msg = err_msg_detail
            elif isinstance(e_non_retry, RuntimeError) and "Milvus TypeError" in err_msg_detail:
                 user_error_msg = f"Error config./código Milvus ({err_type}). Contacte soporte."
            elif isinstance(e_non_retry, FileNotFoundError):
                 user_error_msg = final_error_message # Usar el mensaje ya definido

            formatted_traceback = traceback.format_exc()
            task_log.error(f"Processing failed permanently: {err_type}: {err_msg_detail}", traceback=formatted_traceback)
            final_status = DocumentStatus.ERROR
            final_error_message = user_error_msg
            # Re-levantar para que el bloque exterior lo capture
            raise e_non_retry

        except RETRYABLE_ERRORS as e_retry:
            err_type = type(e_retry).__name__
            err_msg_detail = str(e_retry)[:500]
            max_retries = self.max_retries if hasattr(self, 'max_retries') else 3
            current_attempt = self.request.retries + 1
            user_error_msg = f"Error temporal ({err_type} - Intento {current_attempt}/{max_retries+1}). Reintentando..."
            if isinstance(e_retry, ConnectionError) and "Milvus connection failed" in err_msg_detail:
                 user_error_msg = f"Error temporal conexión base vectorial (Intento {current_attempt}/{max_retries+1}). Reintentando..."

            task_log.warning(f"Processing failed, will retry: {err_type}: {err_msg_detail}", traceback=traceback.format_exc())
            final_status = DocumentStatus.ERROR
            final_error_message = user_error_msg
            # Re-levantar la excepción ORIGINAL para que Celery la capture y reintente
            raise e_retry

        finally:
            if downloaded_file_stream:
                downloaded_file_stream.close()
            task_log.debug("Cleaned up async flow resources.")

    # --- Ejecutar el flujo async y manejar resultado final / actualización DB ---
    try:
        TIMEOUT_SECONDS = 600 # 10 minutos
        asyncio.run(asyncio.wait_for(async_process_flow(), timeout=TIMEOUT_SECONDS))
    except asyncio.TimeoutError as toe:
        task_log.error(f"Processing timed out after {TIMEOUT_SECONDS} seconds.")
        final_status = DocumentStatus.ERROR
        final_error_message = "El procesamiento del documento tardó demasiado."
        task_exception = toe
    except Exception as outer_exc:
        task_log.exception("Exception caught after running async_process_flow.", exc_info=outer_exc)
        # El estado y mensaje ya deberían estar seteados dentro del flujo
        if final_status != DocumentStatus.ERROR:
             final_error_message = final_error_message or f"Error inesperado: {outer_exc}"
             final_status = DocumentStatus.ERROR
        task_exception = outer_exc

    # --- Actualización Final de Estado (Robusta) ---
    task_log.info("Attempting final status update in DB.", status=final_status.value, chunks=processed_chunk_count, error=final_error_message)
    # Usar la función robusta para la actualización final
    update_success = asyncio.run(_robust_update_status(document_id, final_status, final_error_message, processed_chunk_count))

    if not update_success:
         task_log.critical("CRITICAL: Failed to update final document status in DB after processing attempt!",
                           target_status=final_status.value, error_msg=final_error_message)

    # --- Devolver resultado a Celery ---
    if final_status == DocumentStatus.PROCESSED:
        task_log.info("Haystack document processing task completed successfully.")
        return {"status": "success", "document_id": str(document_id), "chunk_count": processed_chunk_count}
    else:
        # Si hubo una excepción guardada Y no es una de las que Celery reintenta,
        # la levantamos para que Celery la marque como FAILED.
        if task_exception and not isinstance(task_exception, tuple(celery_app.conf.task_autoretry_for.keys())):
             task_log.error("Raising final non-retryable exception to Celery.", exception_type=type(task_exception).__name__)
             raise task_exception
        # Si no hubo excepción explícita O era reintentable (y este es el último intento),
        # simplemente devolvemos fallo sin levantar excepción.
        task_log.error("Task finished with error status.", final_db_status=final_status.value, error_msg=final_error_message)
        return {"status": "failure", "document_id": str(document_id), "error": final_error_message or "Error desconocido"}