# ingest-service/app/tasks/process_document.py
def normalize_filename(filename: str) -> str:
    """Normaliza el nombre de archivo eliminando espacios al inicio/final y espacios duplicados."""
    return " ".join(filename.strip().split())

import os
import tempfile
import uuid
import sys
import pathlib
import time
import json
from typing import Optional, Dict, Any, List

import structlog
import httpx
from celery import Task, states
from celery.exceptions import Ignore, Reject, MaxRetriesExceededError, Retry
from celery.signals import worker_process_init
from sqlalchemy import Engine

from app.services.clients.embedding_service_client import EmbeddingServiceClient, EmbeddingServiceClientError
from app.services.clients.docproc_service_client import DocProcServiceClient, DocProcServiceClientError

from app.core.config import settings
from app.db.postgres_client import get_sync_engine, set_status_sync, bulk_insert_chunks_sync
from app.models.domain import DocumentStatus
from app.services.gcs_client import GCSClient, GCSClientError
from app.services.ingest_pipeline import (
    index_chunks_in_milvus_and_prepare_for_pg,
    delete_milvus_chunks
)
from app.tasks.celery_app import celery_app
import asyncio

task_struct_log = structlog.get_logger(__name__)
IS_WORKER = "worker" in sys.argv

# --- Global Resources for Worker Process ---
sync_engine: Optional[Engine] = None
gcs_client_global: Optional[GCSClient] = None # Renamed to avoid conflict
embedding_service_client_global: Optional[EmbeddingServiceClient] = None
docproc_service_client_global: Optional[DocProcServiceClient] = None


@worker_process_init.connect(weak=False)
def init_worker_resources(**kwargs):
    global sync_engine, gcs_client_global, embedding_service_client_global, docproc_service_client_global
    log = task_struct_log.bind(signal="worker_process_init")
    log.info("Worker process initializing resources...")
    try:
        if sync_engine is None:
            sync_engine = get_sync_engine()
            log.info("Synchronous DB engine initialized for worker.")
        else:
             log.info("Synchronous DB engine already initialized for worker.")

        if gcs_client_global is None:
            gcs_client_global = GCSClient()
            log.info("GCS client initialized for worker.")
        else:
            log.info("GCS client already initialized for worker.")

        if embedding_service_client_global is None:
            log.info("Initializing Embedding Service client for worker...")
            embedding_service_client_global = EmbeddingServiceClient()
            log.info("Embedding Service client initialized for worker.")
        else:
            log.info("Embedding Service client already initialized for worker.")
        
        if docproc_service_client_global is None:
            log.info("Initializing Document Processing Service client for worker...")
            docproc_service_client_global = DocProcServiceClient()
            log.info("Document Processing Service client initialized for worker.")
        else:
            log.info("Document Processing Service client already initialized for worker.")

    except Exception as e:
        log.critical("CRITICAL FAILURE during worker resource initialization!", error=str(e), exc_info=True)
        sync_engine = None
        gcs_client_global = None
        embedding_service_client_global = None
        docproc_service_client_global = None


def run_async_from_sync(awaitable):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running(): # Check if a loop is already running in this thread
             # If so, create a new loop for this task execution to avoid interference
             # This is a common pattern for sync Celery workers calling async code
             temp_loop = asyncio.new_event_loop()
             asyncio.set_event_loop(temp_loop)
             result = temp_loop.run_until_complete(awaitable)
             temp_loop.close() # Close the temporary loop
             asyncio.set_event_loop(loop) # Restore original loop if it was running
             return result
    except RuntimeError: # No current event loop in thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(awaitable)
        finally:
            loop.close() # Ensure the new loop is closed
            # No need to restore if there wasn't one before


@celery_app.task(
    bind=True,
    name="ingest.process_document",
    autoretry_for=(
        EmbeddingServiceClientError, 
        DocProcServiceClientError,
        httpx.RequestError, 
        httpx.HTTPStatusError,
        GCSClientError,
        ConnectionRefusedError, # For network issues to dependent services
        asyncio.TimeoutError, # For timeouts in async calls
        Exception
    ),
    exclude=(
        Reject, Ignore, ValueError, ConnectionError, RuntimeError, TypeError,
        # Errors from client-side validation (4xx from services) should not be retried by Celery default
        # but EmbeddingServiceClientError/DocProcServiceClientError might wrap them.
        # Custom logic handles retry for 5xx within the task.
    ),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5, 
    acks_late=True
)
def process_document_standalone(self: Task, *args, **kwargs) -> Dict[str, Any]:
    document_id_str = kwargs.get('document_id')
    company_id_str = kwargs.get('company_id')
    filename = kwargs.get('filename')
    content_type = kwargs.get('content_type')

    task_id = self.request.id or "unknown_task_id"
    attempt = self.request.retries + 1
    max_attempts = (self.max_retries or 0) + 1
    log_context = {
        "task_id": task_id, "attempt": f"{attempt}/{max_attempts}", "doc_id": document_id_str,
        "company_id": company_id_str, "filename": filename, "content_type": content_type
    }
    log = task_struct_log.bind(**log_context)
    log.info("Starting document processing task")

    if not IS_WORKER:
         log.critical("Task function called outside of a worker context! Rejecting.")
         raise Reject("Task running outside worker context.", requeue=False)

    if not all([document_id_str, company_id_str, filename, content_type]):
        log.error("Missing required arguments in task payload.", payload_kwargs=kwargs)
        raise Reject("Missing required arguments (doc_id, company_id, filename, content_type)", requeue=False)

    global_resources = {
        "Sync DB Engine": sync_engine, "GCS Client": gcs_client_global,
        "Embedding Service Client": embedding_service_client_global,
        "DocProc Service Client": docproc_service_client_global
    }
    for name, resource in global_resources.items():
        if not resource:
            log.critical(f"Worker resource '{name}' is not initialized. Task cannot proceed.")
            error_msg = f"Worker resource '{name}' initialization failed."
            # Attempt to update DB status only if engine is available
            if name != "Sync DB Engine" and sync_engine:
                try: 
                    doc_uuid_for_error_res = uuid.UUID(document_id_str)
                    set_status_sync(engine=sync_engine, document_id=doc_uuid_for_error_res, status=DocumentStatus.ERROR, error_message=error_msg)
                except Exception as db_err_res: log.critical(f"Failed to update status after {name} check failure!", error=str(db_err_res))
            raise Reject(error_msg, requeue=False)


    try:
        doc_uuid = uuid.UUID(document_id_str)
    except ValueError:
         log.error("Invalid document_id format received.")
         raise Reject("Invalid document_id format.", requeue=False)

    if content_type not in settings.SUPPORTED_CONTENT_TYPES:
        log.error(f"Unsupported content type: {content_type}")
        error_msg = f"Unsupported content type by ingest-service: {content_type}"
        set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
        raise Reject(error_msg, requeue=False)


    normalized_filename = normalize_filename(filename) if filename else filename
    object_name = f"{company_id_str}/{document_id_str}/{normalized_filename}"
    file_bytes: Optional[bytes] = None
    
    processed_chunks_from_docproc: List[Dict[str, Any]] = []

    try:
        log.debug("Setting status to PROCESSING in DB.")
        status_updated = set_status_sync(
            engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSING, error_message=None
        )
        if not status_updated:
            log.warning("Failed to update status to PROCESSING (document possibly deleted?). Ignoring task.")
            raise Ignore()
        log.info("Status set to PROCESSING.")

        # 1. Descargar archivo de GCS
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = pathlib.Path(temp_dir)
            temp_file_path_obj = temp_dir_path / normalized_filename
            log.info(f"Downloading GCS object: {object_name} -> {str(temp_file_path_obj)}")
            
            max_gcs_retries = 3; gcs_delay = 2; download_success = False
            for attempt_num in range(1, max_gcs_retries + 1):
                try:
                    gcs_client_global.download_file_sync(object_name, str(temp_file_path_obj))
                    log.info("File downloaded successfully from GCS.")
                    file_bytes = temp_file_path_obj.read_bytes()
                    log.info(f"File content read into memory ({len(file_bytes)} bytes).")
                    download_success = True; break
                except GCSClientError as gce_dl:
                    is_not_found = "Object not found" in str(gce_dl) or \
                                   (hasattr(gce_dl, 'original_exception') and \
                                    isinstance(gce_dl.original_exception, FileNotFoundError))
                    if is_not_found:
                        log.warning(f"GCS object not found on download attempt {attempt_num}/{max_gcs_retries}. Retrying in {gcs_delay}s...", error_short=str(gce_dl).splitlines()[0])
                        if attempt_num == max_gcs_retries: log.error(f"File still not found in GCS after {max_gcs_retries} attempts. Aborting."); raise
                        time.sleep(gcs_delay); gcs_delay *= 2
                    else: log.error("Non-retriable GCS error during download", error=str(gce_dl)); raise
                except Exception as read_err:
                    log.error("Failed to read downloaded file into bytes", error=str(read_err), exc_info=True)
                    raise RuntimeError(f"Failed to read temp file: {read_err}") from read_err
            if not download_success or file_bytes is None:
                 raise RuntimeError("File download or read failed, bytes not available.")

        # 2. Llamar al DocProc Service
        log.info("Calling Document Processing Service...")
        try:
            docproc_response = run_async_from_sync(
                docproc_service_client_global.process_document(
                    file_bytes=file_bytes,
                    original_filename=normalized_filename,
                    content_type=content_type,
                    document_id=document_id_str,
                    company_id=company_id_str
                )
            )
            processed_chunks_from_docproc = docproc_response.get("data", {}).get("chunks", [])
            log.info(f"Received {len(processed_chunks_from_docproc)} chunks from DocProc Service.")
        except DocProcServiceClientError as dpce:
            log.error("DocProc Service Client Error", error_msg=str(dpce), status_code=dpce.status_code, details_preview=str(dpce.detail)[:200], exc_info=True)
            error_msg_dpce = f"DocProc Error: {str(dpce.detail or dpce.message)[:350]}"
            set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_dpce)
            if dpce.status_code and 500 <= dpce.status_code < 600: # Server-side errors on docproc
                raise self.retry(exc=dpce, countdown=int(self.default_retry_delay * (self.request.retries + 1)))
            else: # Client-side errors (4xx) or connection issues
                raise Reject(f"DocProc Service critical error: {error_msg_dpce}", requeue=False) from dpce
        
        if not processed_chunks_from_docproc:
            log.warning("DocProc Service returned no chunks. Document processing considered complete with 0 chunks.")
            set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSED, chunk_count=0, error_message=None)
            return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": 0, "document_id": document_id_str}

        # 3. Preparar textos para Embedding Service
        chunk_texts_for_embedding = [chunk['text'] for chunk in processed_chunks_from_docproc if chunk.get('text','').strip()]
        if not chunk_texts_for_embedding:
            log.warning("No non-empty text found in chunks received from DocProc. Finishing with 0 chunks.")
            set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSED, chunk_count=0, error_message=None)
            return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": 0, "document_id": document_id_str}

        # 4. Llamar al Embedding Service
        log.info(f"Calling Embedding Service for {len(chunk_texts_for_embedding)} texts...")
        embeddings: List[List[float]] = []
        try:
            embeddings, model_info = run_async_from_sync(
                embedding_service_client_global.get_embeddings(chunk_texts_for_embedding)
            )
            log.info(f"Embeddings received from service for {len(embeddings)} chunks. Model: {model_info}")
            if len(embeddings) != len(chunk_texts_for_embedding):
                log.error("Embedding count mismatch from Embedding Service.", expected=len(chunk_texts_for_embedding), received=len(embeddings))
                raise RuntimeError(f"Embedding count mismatch. Expected {len(chunk_texts_for_embedding)}, got {len(embeddings)}.")
            if embeddings and len(embeddings[0]) != settings.EMBEDDING_DIMENSION:
                 log.error(f"Received embedding dimension ({len(embeddings[0])}) from service does not match configured Milvus dimension ({settings.EMBEDDING_DIMENSION}).")
                 raise RuntimeError(f"Embedding dimension mismatch. Expected {settings.EMBEDDING_DIMENSION}, got {len(embeddings[0])}")
        except EmbeddingServiceClientError as esce:
            log.error("Embedding Service Client Error", error_msg=str(esce), status_code=esce.status_code, details_preview=str(esce.detail)[:200], exc_info=True)
            error_msg_esc = f"Embedding Service Error: {str(esce.detail or esce.message)[:350]}"
            set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_esc)
            if esce.status_code and 500 <= esce.status_code < 600: # Server-side errors on embedding service
                raise self.retry(exc=esce, countdown=int(self.default_retry_delay * (self.request.retries + 1)))
            else: # Client-side errors (4xx) or connection issues
                raise Reject(f"Embedding Service critical error: {error_msg_esc}", requeue=False) from esce

        # 5. Indexar en Milvus y preparar para PostgreSQL
        log.info("Preparing chunks for Milvus and PostgreSQL indexing...")
        inserted_milvus_count, milvus_pks, chunks_for_pg_insert = index_chunks_in_milvus_and_prepare_for_pg(
            processed_chunks_from_docproc=[chunk for chunk in processed_chunks_from_docproc if chunk.get('text','').strip()], # Pass only non-empty chunks
            embeddings=embeddings, # Embeddings match non-empty chunks
            filename=normalized_filename,
            company_id_str=company_id_str,
            document_id_str=document_id_str,
            delete_existing_milvus_chunks=True
        )
        log.info(f"Milvus indexing complete. Inserted: {inserted_milvus_count}, PKs: {len(milvus_pks)}")

        if inserted_milvus_count == 0 or not chunks_for_pg_insert:
            log.warning("Milvus indexing resulted in zero inserted chunks or no data for PG. Assuming processing complete with 0 chunks.")
            set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSED, chunk_count=0, error_message=None)
            return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": 0, "document_id": document_id_str}

        # 6. Indexar en PostgreSQL
        log.debug(f"Attempting bulk insert of {len(chunks_for_pg_insert)} chunks into PostgreSQL.")
        inserted_pg_count = 0
        try:
            inserted_pg_count = bulk_insert_chunks_sync(engine=sync_engine, chunks_data=chunks_for_pg_insert)
            log.info(f"Successfully bulk inserted {inserted_pg_count} chunks into PostgreSQL.")
            if inserted_pg_count != len(chunks_for_pg_insert):
                 log.warning("Mismatch between prepared PG chunks and inserted PG count.", prepared=len(chunks_for_pg_insert), inserted=inserted_pg_count)
        except Exception as pg_insert_err:
             log.critical("CRITICAL: Failed to bulk insert chunks into PostgreSQL after successful Milvus insert!", error=str(pg_insert_err), exc_info=True)
             log.warning("Attempting to clean up Milvus chunks due to PG insert failure.")
             try: delete_milvus_chunks(company_id=company_id_str, document_id=document_id_str)
             except Exception as cleanup_err: log.error("Failed to cleanup Milvus chunks after PG failure.", cleanup_error=str(cleanup_err))
             error_msg_pg_fail = f"PG bulk insert failed: {type(pg_insert_err).__name__}"
             set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_pg_fail[:500])
             raise Reject(f"PostgreSQL bulk insert failed: {pg_insert_err}", requeue=False) from pg_insert_err

        # 7. Actualización de Estado Final
        log.debug("Setting final status to PROCESSED in DB.")
        set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSED, chunk_count=inserted_pg_count, error_message=None)
        log.info(f"Document processing finished successfully. Final chunk count (PG): {inserted_pg_count}")
        return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": inserted_pg_count, "document_id": document_id_str}

    except GCSClientError as gce:
        log.error(f"GCS Error during processing", error_msg=str(gce), exc_info=True)
        error_msg_gcs = f"GCS Error: {str(gce)[:400]}"
        set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_gcs)
        if "Object not found" in str(gce): # Non-retriable if object not found after initial upload
            raise Reject(f"GCS Error: Object not found: {object_name}", requeue=False) from gce
        # For other GCS errors, let Celery's autoretry_for handle it.
        raise # Re-raise for Celery to catch for retry

    except (ValueError, RuntimeError, TypeError) as non_retriable_err:
         log.error(f"Non-retriable Error: {non_retriable_err}", exc_info=True)
         error_msg_pipe = f"Pipeline/Data Error: {type(non_retriable_err).__name__} - {str(non_retriable_err)[:400]}"
         set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_pipe)
         raise Reject(f"Pipeline failed: {error_msg_pipe}", requeue=False) from non_retriable_err

    except Reject as r:
         log.error(f"Task rejected permanently: {r.reason}")
         if sync_engine and doc_uuid and r.reason: # Ensure sync_engine and doc_uuid are valid
             set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=f"Rejected: {str(r.reason)[:500]}")
         raise 
    except Ignore:
         log.info("Task ignored.")
         raise 
    except Retry as retry_exc: # Specific catch for Celery's Retry exception
        log.warning(f"Task is being retried by Celery. Reason: {retry_exc}", exc_info=False)
        # Status is already PROCESSING, no need to update for retry.
        raise # Re-raise for Celery to handle the retry
    except MaxRetriesExceededError as mree:
        log.error("Max retries exceeded for task.", exc_info=True, cause_type=type(mree.cause).__name__, cause_message=str(mree.cause))
        final_error = mree.cause if mree.cause else mree
        error_msg_mree = f"Max retries exceeded ({max_attempts}). Last error: {type(final_error).__name__} - {str(final_error)[:300]}"
        set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_mree)
        self.update_state(state=states.FAILURE, meta={'exc_type': type(final_error).__name__, 'exc_message': str(final_error)})
        # Do not re-raise MaxRetriesExceededError, Celery handles it.
    except Exception as exc: 
        log.exception(f"An unexpected error occurred, attempting Celery retry if possible.")
        error_msg_exc = f"Attempt {attempt} failed: {type(exc).__name__} - {str(exc)[:400]}"
        # Set status to PROCESSING with error for visibility during retries.
        set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSING, error_message=error_msg_exc)
        # Let Celery's autoretry_for handle it by re-raising the original exception.
        raise