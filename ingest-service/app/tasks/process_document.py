# ingest-service/app/tasks/process_document.py
import os
import tempfile
import uuid
import sys
import pathlib # Use pathlib for paths
from typing import Optional, Dict, Any

import structlog
from celery import Task
from celery.exceptions import Ignore, Reject, MaxRetriesExceededError
# LLM_FLAG: ADDED - Import TextEmbedding for type hinting and conditional init
from fastembed.embedding import TextEmbedding
from sqlalchemy import Engine # Import Engine for type hinting

# --- Custom Application Imports ---
from app.core.config import settings
from app.db.postgres_client import get_sync_engine, set_status_sync # Keep DB client
from app.models.domain import DocumentStatus # Keep domain models
from app.services.minio_client import MinioClient, MinioError # Keep MinIO client
# --- Import the NEW pipeline function ---
from app.services.ingest_pipeline import ingest_document_pipeline, EXTRACTORS # Import pipeline and extractors map
from app.tasks.celery_app import celery_app # Keep Celery app


# --- Initialize Structlog Logger ---
task_struct_log = structlog.get_logger(__name__)

# --- Detect if running as Celery Worker ---
# A common way to detect if running under Celery worker context
IS_WORKER = "worker" in sys.argv

# --- Initialize Global Resources Conditionally ---
sync_engine: Optional[Engine] = None
minio_client: Optional[MinioClient] = None
embedding_model: Optional[TextEmbedding] = None

# LLM_FLAG: MODIFIED - Initialize resources conditionally based on IS_WORKER
try:
    task_struct_log.info("Initializing global resources for process...", is_worker=IS_WORKER)

    # MinIO Client is needed by both (though API should ideally use async)
    # Initialize globally for now, but mark for potential async refactor in API
    minio_client = MinioClient()
    task_struct_log.info("Global MinIO client initialized.")

    if IS_WORKER:
        # Resources needed ONLY by the worker
        task_struct_log.info("Initializing worker-specific resources (DB Engine, Embedding Model)...")
        try:
            sync_engine = get_sync_engine()
            task_struct_log.info("Synchronous DB engine initialized successfully for worker.")
        except Exception as db_init_err:
            task_struct_log.critical(
                "Failed to initialize synchronous DB engine! Worker tasks depending on DB will fail.",
                error=str(db_init_err),
                exc_info=True
            )
            # sync_engine remains None

        try:
            task_struct_log.info("Initializing FastEmbed model instance for worker...", model=settings.FASTEMBED_MODEL, device="cpu")
            embedding_model = TextEmbedding(
                model_name=settings.FASTEMBED_MODEL,
                cache_dir=os.environ.get("FASTEMBED_CACHE_DIR"), # Optional: configure cache
                threads=None, # Let FastEmbed decide based on environment / CPU cores
                # Set parallel to 0 or 1 for CPU to avoid potential conflicts with Celery prefork
                # 0 might let FastEmbed manage internal parallelism, 1 disables it explicitly.
                # Let's use 1 to be safer with prefork.
                parallel=1
            )
            task_struct_log.info("Global FastEmbed model instance initialized successfully for worker.")
        except Exception as emb_init_err:
             task_struct_log.critical(
                 "Failed to initialize FastEmbed model! Worker tasks depending on embeddings will fail.",
                 error=str(emb_init_err),
                 exc_info=True
             )
             # embedding_model remains None
    else:
         task_struct_log.info("Running in API context. Skipping worker-specific resource initialization (DB Engine, Embedding Model).")

except Exception as global_init_err:
    task_struct_log.critical("CRITICAL: Failed during global resource initialization!", error=str(global_init_err), exc_info=True)
    # Ensure potentially failed initializations result in None
    if not isinstance(minio_client, MinioClient): minio_client = None
    if IS_WORKER: # Only nullify worker resources if we expected them
        if not isinstance(sync_engine, Engine): sync_engine = None
        if not isinstance(embedding_model, TextEmbedding): embedding_model = None

# --------------------------------------------------------------------------
# Refactored Synchronous Celery Task Definition (MODIFIED Pre-checks)
# --------------------------------------------------------------------------
@celery_app.task(
    bind=True,
    name="ingest.process_document",
    autoretry_for=(Exception,),
    # Exclude errors that indicate non-transient problems or config issues
    exclude=(Reject, Ignore, ValueError, ConnectionError, RuntimeError, TypeError),
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

    # --- Pre-checks ---
    if not IS_WORKER:
         # This should ideally not happen if imports are managed well, but safety first.
         log.critical("Task function called outside of a worker context! Rejecting.")
         raise Reject("Task running outside worker context.", requeue=False)

    if not all([document_id_str, company_id_str, filename, content_type]):
        log.error("Missing required arguments in task payload.", payload_kwargs=kwargs)
        raise Reject("Missing required arguments (doc_id, company_id, filename, content_type)", requeue=False)

    # --- Check if Worker Resources Initialized Correctly ---
    # These checks run *inside* the task execution for robustness
    if not sync_engine:
         log.critical("Worker Sync DB Engine is not initialized. Task cannot proceed.")
         # Attempting DB update here is difficult without the engine. Reject permanently.
         raise Reject("Worker sync DB engine initialization failed.", requeue=False)

    if not embedding_model:
         log.critical("Worker Embedding Model is not initialized. Task cannot proceed.")
         # Attempt to update DB status via the (hopefully existing) sync_engine
         if document_id_str:
             try:
                 doc_uuid_for_error = uuid.UUID(document_id_str)
                 set_status_sync(engine=sync_engine, document_id=doc_uuid_for_error, status=DocumentStatus.ERROR, error_message="Worker embedding model init failed.")
             except Exception as db_err:
                 log.critical("Failed to update status to ERROR after worker embedding model init failure!", error=str(db_err))
         raise Reject("Worker embedding model initialization failed.", requeue=False)

    if not minio_client:
         log.critical("Worker MinIO Client is not initialized. Task cannot proceed.")
         # Attempt to update DB status via sync_engine
         if document_id_str:
              try:
                  doc_uuid_for_error = uuid.UUID(document_id_str)
                  set_status_sync(engine=sync_engine, document_id=doc_uuid_for_error, status=DocumentStatus.ERROR, error_message="Worker MinIO client init failed.")
              except Exception as db_err:
                  log.critical("Failed to update status to ERROR after worker MinIO client init failure!", error=str(db_err))
         raise Reject("Worker MinIO client initialization failed.", requeue=False)
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

        # 2. Download file from MinIO to temporary directory
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = pathlib.Path(temp_dir)
            temp_file_path_obj = temp_dir_path / filename
            log.info(f"Downloading MinIO object: {object_name} -> {str(temp_file_path_obj)}")
            # Use the initialized global minio_client
            minio_client.download_file_sync(object_name, str(temp_file_path_obj))
            log.info("File downloaded successfully from MinIO.")

            # 3. Execute Standalone Ingestion Pipeline
            log.info("Executing standalone ingest pipeline (extract, chunk, embed, insert)...")
            # Pass the initialized global embedding_model
            inserted_chunk_count = ingest_document_pipeline(
                file_path=temp_file_path_obj,
                company_id=company_id_str,
                document_id=document_id_str,
                content_type=content_type,
                embedding_model=embedding_model, # Pass the initialized model here
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

    # --- Error Handling ---
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

    except (ValueError, ConnectionError, RuntimeError, TypeError) as pipeline_err:
         # These are likely non-recoverable errors from the pipeline logic or Milvus/DB connection issues
         # that might persist. Reject permanently after marking as error.
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