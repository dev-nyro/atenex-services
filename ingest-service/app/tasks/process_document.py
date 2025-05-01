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
# LLM_FLAG: ADDED - Import Celery signal
from celery.signals import worker_process_init
from sqlalchemy import Engine # Import Engine for type hinting
# LLM_FLAG: ADDED - Import TextEmbedding for type hinting and lazy loading
from fastembed.embedding import TextEmbedding

# --- Custom Application Imports ---
from app.core.config import settings
from app.db.postgres_client import get_sync_engine, set_status_sync # Keep DB client
from app.models.domain import DocumentStatus # Keep domain models
from app.services.minio_client import MinioClient, MinioError # Keep MinIO client
# --- Import the NEW pipeline function ---
from app.services.ingest_pipeline import ingest_document_pipeline, EXTRACTORS, EXTRACTION_ERRORS # Import pipeline and extractors map
from app.tasks.celery_app import celery_app # Keep Celery app


# --- Initialize Structlog Logger ---
task_struct_log = structlog.get_logger(__name__)

# --- Detect if running as Celery Worker ---
# A common way to detect if running under Celery worker context
IS_WORKER = "worker" in sys.argv

# --- Global Resources for Worker Process (Initialize to None) ---
sync_engine: Optional[Engine] = None
minio_client: Optional[MinioClient] = None
# LLM_FLAG: Variable to hold the loaded embedding model per worker process
worker_embedding_model: Optional[TextEmbedding] = None

# LLM_FLAG: Initialize resources at worker startup using Celery signal
@worker_process_init.connect(weak=False)
def init_worker_resources(**kwargs):
    """Initializes resources needed by each Celery worker process."""
    global sync_engine, minio_client, worker_embedding_model
    log = task_struct_log.bind(signal="worker_process_init")
    log.info("Worker process initializing resources...")

    # Initialize DB Engine if not already done
    if sync_engine is None:
        try:
            sync_engine = get_sync_engine()
            log.info("Synchronous DB engine initialized for worker process.")
        except Exception as db_init_err:
            log.critical("Failed to initialize synchronous DB engine for worker!", error=str(db_init_err), exc_info=True)
            sync_engine = None # Ensure it's None on failure
    else:
        log.debug("Synchronous DB engine already initialized for worker process.")

    # Initialize MinIO Client if not already done
    if minio_client is None:
        try:
            minio_client = MinioClient()
            log.info("MinIO client initialized for worker process.")
        except Exception as minio_init_err:
            log.critical("Failed to initialize MinIO client for worker!", error=str(minio_init_err), exc_info=True)
            minio_client = None # Ensure it's None on failure
    else:
        log.debug("MinIO client already initialized for worker process.")

    # Initialize Embedding Model if not already done
    if worker_embedding_model is None:
        log.info("Initializing FastEmbed model instance for worker process...", model=settings.FASTEMBED_MODEL, device="cpu")
        try:
            # LLM_FLAG: This is where the model download will happen *at runtime* within the worker process
            worker_embedding_model = TextEmbedding(
                model_name=settings.FASTEMBED_MODEL,
                cache_dir=os.environ.get("FASTEMBED_CACHE_DIR"), # Allows overriding cache dir via env var
                device="cpu", # Explicitly CPU
                threads=1,    # Limit threads for ONNX CPU inference if needed
                parallel=0    # Set parallel processing count if applicable
            )
            # Optional: Perform a dummy embedding to ensure download/load is complete
            # list(worker_embedding_model.embed(["test"]))
            log.info("FastEmbed model instance initialized successfully for worker process.")
        except Exception as emb_init_err:
             log.critical("Failed to initialize FastEmbed model for worker!", error=str(emb_init_err), exc_info=True)
             worker_embedding_model = None # Ensure it's None on failure
    else:
        log.debug("FastEmbed model already initialized for worker process.")

    # Final check after initialization attempts
    if not all([sync_engine, minio_client, worker_embedding_model]):
         log.critical("One or more critical resources failed to initialize for this worker process!")
         # Depending on the app's resilience strategy, you might want the worker process to exit
         # or continue but have tasks fail the pre-checks.


# --------------------------------------------------------------------------
# Refactored Synchronous Celery Task Definition (Checks use worker globals)
# --------------------------------------------------------------------------
@celery_app.task(
    bind=True,
    name="ingest.process_document",
    autoretry_for=(Exception,),
    # Exclude errors that indicate non-transient problems or config issues
    exclude=(Reject, Ignore, ValueError, ConnectionError, RuntimeError, TypeError, *EXTRACTION_ERRORS),
    retry_backoff=True,
    retry_backoff_max=600, # Max ~10 minutes backoff
    retry_jitter=True,
    max_retries=3 # Retry up to 3 times for transient errors
)
def process_document_standalone(self: Task, *args, **kwargs) -> Dict[str, Any]:
    """
    Synchronous Celery task to process a document using the standalone pipeline.
    Downloads from MinIO, then calls ingest_document_pipeline for conversion,
    chunking, embedding (CPU), and writing to Milvus via pymilvus.
    Updates status in PostgreSQL synchronously. Uses structlog for logging.
    Relies on resources initialized by worker_process_init.
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
    log.info("Starting standalone document processing task")

    # --- Pre-checks (Rely on globals set by worker_process_init) ---
    if not IS_WORKER:
         log.critical("Task function called outside of a worker context! Rejecting.")
         raise Reject("Task running outside worker context.", requeue=False)

    if not all([document_id_str, company_id_str, filename, content_type]):
        log.error("Missing required arguments in task payload.", payload_kwargs=kwargs)
        raise Reject("Missing required arguments (doc_id, company_id, filename, content_type)", requeue=False)

    # --- Check if Worker Resources Initialized Correctly ---
    # Check the global variables populated by the init signal
    if sync_engine is None:
         log.critical("Worker Sync DB Engine is not available (init failed). Task cannot proceed.")
         raise Reject("Worker sync DB engine initialization failed.", requeue=False)

    if worker_embedding_model is None: # Check the global variable holding the model
         log.critical("Worker Embedding Model is not available (init failed). Task cannot proceed.")
         error_msg = "Worker embedding model init/preload failed."
         doc_uuid_err = None
         try: doc_uuid_err = uuid.UUID(document_id_str)
         except ValueError: pass
         if doc_uuid_err:
             try: set_status_sync(engine=sync_engine, document_id=doc_uuid_err, status=DocumentStatus.ERROR, error_message=error_msg)
             except Exception as db_err: log.critical("Failed to update status after embedding model check failure!", error=str(db_err))
         raise Reject(error_msg, requeue=False)

    if minio_client is None:
         log.critical("Worker MinIO Client is not available (init failed). Task cannot proceed.")
         error_msg = "Worker MinIO client init failed."
         doc_uuid_err = None
         try: doc_uuid_err = uuid.UUID(document_id_str)
         except ValueError: pass
         if doc_uuid_err:
              try: set_status_sync(engine=sync_engine, document_id=doc_uuid_err, status=DocumentStatus.ERROR, error_message=error_msg)
              except Exception as db_err: log.critical("Failed to update status after MinIO client check failure!", error=str(db_err))
         raise Reject(error_msg, requeue=False)
    # --- End Pre-checks ---


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
        raise Reject(error_msg, requeue=False)


    object_name = f"{company_id_str}/{document_id_str}/{filename}"
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

        # 2. Download file from MinIO and read bytes
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = pathlib.Path(temp_dir)
            temp_file_path_obj = temp_dir_path / filename # Use original filename for temp file
            log.info(f"Downloading MinIO object: {object_name} -> {str(temp_file_path_obj)}")
            # Use the initialized global minio_client
            minio_client.download_file_sync(object_name, str(temp_file_path_obj))
            log.info("File downloaded successfully from MinIO.")
            # Read file content into bytes
            file_bytes = temp_file_path_obj.read_bytes()
            log.info(f"Read {len(file_bytes)} bytes from downloaded file.")

        # Ensure bytes were read
        if file_bytes is None:
             raise RuntimeError("Failed to read downloaded file bytes.")

        # 3. Execute Standalone Ingestion Pipeline
        log.info("Executing standalone ingest pipeline (extract, chunk, embed, insert)...")
        # Pass the model instance loaded by the worker init signal
        inserted_chunk_count = ingest_document_pipeline(
            file_bytes=file_bytes,           # Pass bytes instead of path
            filename=filename,               # Pass original filename
            company_id=company_id_str,
            document_id=document_id_str,
            content_type=content_type,
            embedding_model=worker_embedding_model, # Use the preloaded model
            delete_existing=True
        )
        log.info(f"Ingestion pipeline finished. Inserted chunks: {inserted_chunk_count}")

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

    # --- Error Handling (Mostly unchanged, relies on exclude tuple now for non-retriable errors) ---
    except MinioError as me:
        log.error(f"MinIO Error during processing: {me}", exc_info=True)
        error_msg = f"MinIO Error: {str(me)[:400]}"
        try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
        except Exception as db_err: log.critical("Failed to update status to ERROR after MinIO failure!", error=str(db_err))
        if "Object not found" in str(me):
            # If the object is not found, it's not a transient error. Reject permanently.
            raise Reject(f"MinIO Error: Object not found: {object_name}", requeue=False) from me
        else:
            # For other S3 errors, raise to allow Celery's autoretry mechanism
            raise me # Will be caught by autoretry_for=(Exception,)

    except (*EXTRACTION_ERRORS, ValueError, ConnectionError, RuntimeError, TypeError) as pipeline_err:
         # These are likely non-recoverable errors from the pipeline or config issues.
         # They are listed in 'exclude', so Celery won't retry them automatically.
         # We log, set status to ERROR, and raise Reject manually.
         log.error(f"Pipeline/Connection Error: {pipeline_err}", exc_info=True)
         error_msg = f"Pipeline Error: {type(pipeline_err).__name__} - {str(pipeline_err)[:400]}"
         try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
         except Exception as db_err: log.critical("Failed to update status to ERROR after pipeline failure!", error=str(db_err))
         # Reject instead of raising to prevent retries for these specific error types
         raise Reject(f"Pipeline failed: {error_msg}", requeue=False) from pipeline_err

    except Reject as r:
         # If Reject is raised explicitly (e.g., from pre-checks)
         log.error(f"Task rejected permanently: {r.reason}")
         # Attempt to set status one last time if possible and ID is valid
         if sync_engine and doc_uuid:
             try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=f"Rejected: {r.reason}"[:500])
             except Exception as db_err: log.critical("Failed to update status to ERROR after task rejection!", error=str(db_err))
         raise r # Re-raise Reject

    except Ignore:
         # If Ignore is raised explicitly
         log.info("Task ignored.")
         raise Ignore() # Re-raise Ignore

    except MaxRetriesExceededError as mree:
        # This happens after autoretry_for fails max_retries times
        log.error("Max retries exceeded for task.", exc_info=True)
        final_error = mree.cause if mree.cause else mree
        error_msg = f"Max retries exceeded ({max_attempts}). Last error: {type(final_error).__name__} - {str(final_error)[:300]}"
        try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
        except Exception as db_err: log.critical("Failed to update status to ERROR after max retries!", error=str(db_err))
        # Do not raise here, Celery handles logging MaxRetriesExceededError

    except Exception as exc:
        # Catch any other unexpected exception to allow Celery's autoretry
        log.exception(f"An unexpected error occurred during document processing, will retry if attempts remain.")
        error_msg = f"Attempt {attempt} failed: {type(exc).__name__} - {str(exc)[:400]}"
        try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
        except Exception as db_err: log.critical("CRITICAL: Failed to update status to ERROR after unexpected failure!", error=str(db_err))
        # Raise the original exception to trigger Celery's retry mechanism defined in autoretry_for
        raise exc

# LLM_FLAG: NO_CHANGE - Keep task alias assignment
process_document_haystack_task = process_document_standalone