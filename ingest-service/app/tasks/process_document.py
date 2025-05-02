def normalize_filename(filename: str) -> str:
    """Normaliza el nombre de archivo eliminando espacios al inicio/final y espacios duplicados."""
    return " ".join(filename.strip().split())
# ingest-service/app/tasks/process_document.py
import os
import tempfile
import uuid
import sys
import pathlib
import time
from typing import Optional, Dict, Any

import structlog
from celery import Task
from celery.exceptions import Ignore, Reject, MaxRetriesExceededError
from celery.signals import worker_process_init
from sqlalchemy import Engine
# LLM_FLAG: Import correct embedding components
from sentence_transformers import SentenceTransformer
from app.services.embedder import get_embedding_model # Function to load/cache model

# --- Custom Application Imports ---
from app.core.config import settings
from app.db.postgres_client import get_sync_engine, set_status_sync
from app.models.domain import DocumentStatus
from app.services.gcs_client import GCSClient, GCSError
# LLM_FLAG: Import REFACTORIZADO pipeline and EXTRACTORS map
from app.services.ingest_pipeline import ingest_document_pipeline, EXTRACTORS, EXTRACTION_ERRORS
from app.tasks.celery_app import celery_app

task_struct_log = structlog.get_logger(__name__)
IS_WORKER = "worker" in sys.argv

# --- Global Resources for Worker Process ---
sync_engine: Optional[Engine] = None
gcs_client: Optional[GCSClient] = None
worker_embedding_model: Optional[SentenceTransformer] = None

# LLM_FLAG: Initialize resources at worker startup
@worker_process_init.connect(weak=False)
def init_worker_resources(**kwargs):
    global sync_engine, minio_client, worker_embedding_model
    log = task_struct_log.bind(signal="worker_process_init")
    log.info("Worker process initializing resources...")
    try:
        if sync_engine is None:
            sync_engine = get_sync_engine()
            log.info("Synchronous DB engine initialized.")
        else:
             log.info("Synchronous DB engine already initialized.")

        if gcs_client is None:
            gcs_client = GCSClient()
            log.info("GCS client initialized.")
        else:
            log.info("GCS client already initialized.")

        if worker_embedding_model is None:
            log.info("Preloading embedding model...")
            worker_embedding_model = get_embedding_model() # This handles loading and caching
            log.info("Embedding model preloaded.")
        else:
            log.info("Embedding model already preloaded.")

    except Exception as e:
        log.critical("CRITICAL FAILURE during worker resource initialization!", error=str(e), exc_info=True)
        # Set to None so pre-checks in task will fail cleanly
        sync_engine = None
        gcs_client = None
        worker_embedding_model = None

# --------------------------------------------------------------------------
# Refactored Celery Task Definition
# --------------------------------------------------------------------------
@celery_app.task(
    bind=True,
    name="ingest.process_document",
    autoretry_for=(Exception,),
    # LLM_FLAG: Exclude specific non-retriable errors
    exclude=(Reject, Ignore, ValueError, ConnectionError, RuntimeError, TypeError, *EXTRACTION_ERRORS),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3
)
def process_document_standalone(self: Task, *args, **kwargs) -> Dict[str, Any]:
    """
    Refactored Celery task: Downloads from MinIO, passes bytes to the new pipeline
    (using SentenceTransformer, PyMuPDF, etc.), updates status sync.
    """
    document_id_str = kwargs.get('document_id')
    company_id_str = kwargs.get('company_id')
    filename = kwargs.get('filename')
    content_type = kwargs.get('content_type')

    task_id = self.request.id or "unknown_task_id"
    attempt = self.request.retries + 1
    max_attempts = (self.max_retries or 0) + 1
    log = task_struct_log.bind(
        task_id=task_id, attempt=f"{attempt}/{max_attempts}", doc_id=document_id_str,
        company_id=company_id_str, filename=filename, content_type=content_type
    )
    log.info("Starting REFACTORED standalone document processing task (MiniLM/PyMuPDF)")

    # --- Pre-checks ---
    if not IS_WORKER:
         log.critical("Task function called outside of a worker context! Rejecting.")
         raise Reject("Task running outside worker context.", requeue=False)

    if not all([document_id_str, company_id_str, filename, content_type]):
        log.error("Missing required arguments in task payload.", payload_kwargs=kwargs)
        raise Reject("Missing required arguments (doc_id, company_id, filename, content_type)", requeue=False)

    # LLM_FLAG: Check worker resources were initialized correctly
    if not sync_engine:
         log.critical("Worker Sync DB Engine is not initialized. Task cannot proceed.")
         raise Reject("Worker sync DB engine initialization failed.", requeue=False)
    if not worker_embedding_model: # Check the global variable holding the model
         log.critical("Worker Embedding Model (SentenceTransformer) is not available/loaded. Task cannot proceed.")
         error_msg = "Worker embedding model init/preload failed."
         doc_uuid_err = None
         try: doc_uuid_err = uuid.UUID(document_id_str)
         except ValueError: pass
         if doc_uuid_err:
             try: set_status_sync(engine=sync_engine, document_id=doc_uuid_err, status=DocumentStatus.ERROR, error_message=error_msg)
             except Exception as db_err: log.critical("Failed to update status after embedding model check failure!", error=str(db_err))
         raise Reject(error_msg, requeue=False)
    if not gcs_client:
        log.critical("Worker GCS Client is not initialized. Task cannot proceed.")
        error_msg = "Worker GCS client init failed."
        doc_uuid_err = None
        try:
            doc_uuid_err = uuid.UUID(document_id_str)
        except ValueError:
            pass
        if doc_uuid_err:
            try:
                set_status_sync(engine=sync_engine, document_id=doc_uuid_err, status=DocumentStatus.ERROR, error_message=error_msg)
            except Exception as db_err:
                log.critical("Failed to update status after GCS client check failure!", error=str(db_err))
        raise Reject(error_msg, requeue=False)

    try:
        doc_uuid = uuid.UUID(document_id_str)
    except ValueError:
         log.error("Invalid document_id format received.")
         raise Reject("Invalid document_id format.", requeue=False)

    if content_type not in EXTRACTORS:
        log.error(f"Unsupported content type provided: {content_type}")
        error_msg = f"Unsupported content type: {content_type}"
        try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
        except Exception as db_err: log.critical("Failed to update status to ERROR for unsupported type!", error=str(db_err))
        raise Reject(error_msg, requeue=False) # Non-retriable

    normalized_filename = normalize_filename(filename) if filename else filename
    object_name = f"{company_id_str}/{document_id_str}/{normalized_filename}"
    temp_file_path_obj: Optional[pathlib.Path] = None
    inserted_chunk_count = 0
    file_bytes: Optional[bytes] = None

    try:
        # 1. Update status to PROCESSING
        log.debug("Setting status to PROCESSING in DB.")
        status_updated = set_status_sync(
            engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSING, error_message=None
        )
        if not status_updated:
            log.warning("Failed to update status to PROCESSING (document possibly deleted?). Ignoring task.")
            raise Ignore()
        log.info("Status set to PROCESSING.")

        # 2. Download file and read bytes
        # Download file bytes directly from GCS
        max_gcs_retries = 3
        gcs_delay = 2
        for attempt_num in range(1, max_gcs_retries + 1):
            try:
                file_bytes = gcs_client.read_file_sync(object_name)
                log.info(f"File content read from GCS into memory ({len(file_bytes)} bytes).")
                break
            except GCSError as ge:
                if "not found" in str(ge).lower():
                    log.warning(f"GCS file not found on download attempt {attempt_num}/{max_gcs_retries}. Retrying in {gcs_delay}s...", error=str(ge))
                    if attempt_num == max_gcs_retries:
                        log.error(f"File still not found in GCS after {max_gcs_retries} attempts. Aborting.")
                        raise
                    time.sleep(gcs_delay)
                    gcs_delay *= 2
                else:
                    log.error("Non-retriable GCS error during download", error=str(ge))
                    raise
            except Exception as read_err:
                log.error("Failed to read file from GCS into bytes", error=str(read_err), exc_info=True)
                raise RuntimeError(f"Failed to read file from GCS: {read_err}") from read_err
        # Check if file_bytes was successfully read
            if file_bytes is None:
                 raise RuntimeError("File download or read failed, bytes not available.")

            # 3. Execute Refactored Ingestion Pipeline
            log.info("Executing refactored ingest pipeline...")
        inserted_chunk_count = ingest_document_pipeline(
            file_bytes=file_bytes,
            filename=normalized_filename,
            company_id=company_id_str,
            document_id=document_id_str,
            content_type=content_type,
            embedding_model=worker_embedding_model, # Pass the loaded model
            delete_existing=True
        )
        log.info(f"Ingestion pipeline finished. Inserted chunks reported: {inserted_chunk_count}")

        # 4. Update status to PROCESSED
        log.debug("Setting status to PROCESSED in DB.")
        final_status_updated = set_status_sync(
            engine=sync_engine,
            document_id=doc_uuid,
            status=DocumentStatus.PROCESSED,
            chunk_count=inserted_chunk_count,
            error_message=None
        )
        if not final_status_updated:
             log.warning("Failed to update status to PROCESSED after successful processing (document possibly deleted?).")

        log.info(f"Document processing finished successfully. Final chunk count: {inserted_chunk_count}")
        return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": inserted_chunk_count, "document_id": document_id_str}

    # --- Error Handling ---
    except GCSError as ge:
        log.error(f"GCS Error during processing", error=str(ge), exc_info=True)
        error_msg = f"GCS Error: {str(ge)[:400]}"
        try:
            set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
        except Exception as db_err:
            log.critical("Failed to update status after GCS failure!", error=str(db_err))
        if "not found" in str(ge).lower():
            raise Reject(f"GCS Error: Object not found: {object_name}", requeue=False) from ge
        else:
            raise ge  # Allow retry for other potential GCS errors

    except (*EXTRACTION_ERRORS, ValueError, RuntimeError, TypeError) as pipeline_err:
         # Catches extraction errors, embedding errors, chunking errors, Milvus errors, etc.
         # Considered non-retriable by default based on 'exclude' tuple
         log.error(f"Pipeline Error: {pipeline_err}", exc_info=True)
         error_msg = f"Pipeline Error: {type(pipeline_err).__name__} - {str(pipeline_err)[:400]}"
         try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
         except Exception as db_err: log.critical("Failed to update status after pipeline failure!", error=str(db_err))
         raise Reject(f"Pipeline failed: {error_msg}", requeue=False) from pipeline_err

    except Reject as r:
         log.error(f"Task rejected permanently: {r.reason}")
         if sync_engine and doc_uuid:
             try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=f"Rejected: {r.reason}"[:500])
             except Exception as db_err: log.critical("Failed to update status after task rejection!", error=str(db_err))
         raise r

    except Ignore:
         log.info("Task ignored.")
         raise Ignore()

    except MaxRetriesExceededError as mree:
        log.error("Max retries exceeded for task.", exc_info=True)
        final_error = mree.cause if mree.cause else mree
        error_msg = f"Max retries exceeded ({max_attempts}). Last error: {type(final_error).__name__} - {str(final_error)[:300]}"
        try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
        except Exception as db_err: log.critical("Failed to update status to ERROR after max retries!", error=str(db_err))
        # Let Celery handle logging MaxRetriesExceededError

    except Exception as exc:
        # Catch-all for unexpected errors, triggers Celery's autoretry
        log.exception(f"An unexpected error occurred, will retry if attempts remain.")
        error_msg = f"Attempt {attempt} failed: {type(exc).__name__} - {str(exc)[:400]}"
        try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
        except Exception as db_err: log.critical("CRITICAL: Failed to update status after unexpected failure!", error=str(db_err))
        raise exc

# LLM_FLAG: Keeping alias for potential external references
process_document_haystack_task = process_document_standalone