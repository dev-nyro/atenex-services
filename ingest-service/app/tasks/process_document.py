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

# --- Custom Application Imports ---
from app.core.config import settings
from app.db.postgres_client import get_sync_engine, set_status_sync # Keep DB client
from app.models.domain import DocumentStatus # Keep domain models
from app.services.minio_client import MinioClient, MinioError # Keep MinIO client
# --- Import the NEW pipeline function ---
from app.services.ingest_pipeline import ingest_document_pipeline, EXTRACTORS # Import new pipeline and extractors map for validation
from app.tasks.celery_app import celery_app # Keep Celery app

# --- Initialize Structlog Logger ---
# Get logger instance BEFORE defining task function if used inside
task_struct_log = structlog.get_logger(__name__)

# --- Synchronous Database Client Engine ---
try:
    # Get the engine once when the module loads
    sync_engine = get_sync_engine()
    task_struct_log.info("Synchronous DB engine initialized successfully for worker.")
except Exception as db_init_err:
    task_struct_log.critical(
        "Failed to initialize synchronous DB engine! Worker tasks depending on DB will fail.",
        error=str(db_init_err),
        exc_info=True
    )
    sync_engine = None


# --------------------------------------------------------------------------
# Global Resource Initialization (Simplified)
# --------------------------------------------------------------------------
# Resources needed directly by the task logic itself (excluding pipeline components)
minio_client = None

try:
    task_struct_log.info("Initializing global resources (MinIO Client) for Celery worker process...")
    minio_client = MinioClient()
    task_struct_log.info("Global MinIO client initialized.")
    # Other resources like Milvus client, embedding model are now initialized
    # within the ingest_pipeline module itself when first used.

except Exception as e:
    task_struct_log.critical("CRITICAL: Failed to initialize global MinIO client in worker process!", error=str(e), exc_info=True)
    minio_client = None # Ensure it's None so the check below fails

# --------------------------------------------------------------------------
# Refactored Synchronous Celery Task Definition
# --------------------------------------------------------------------------
@celery_app.task(
    bind=True,
    name="ingest.process_document", # Keep explicit name
    autoretry_for=(Exception,), # Retry on general exceptions (network, temp Milvus/Embedding issues)
    exclude=(Reject, Ignore, ValueError, ConnectionError, RuntimeError), # Don't retry permanent errors from pipeline
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3
)
def process_document_standalone(self: Task, *args, **kwargs) -> Dict[str, Any]:
    """
    Synchronous Celery task to process a document using the standalone pipeline.
    Downloads from MinIO, then calls ingest_document_pipeline for conversion,
    chunking, embedding (CPU), and writing to Milvus via pymilvus.
    Updates status in PostgreSQL synchronously. Uses structlog for logging.
    """
    # --- Extract arguments ---
    document_id_str = kwargs.get('document_id')
    company_id_str = kwargs.get('company_id')
    filename = kwargs.get('filename')
    content_type = kwargs.get('content_type')

    # --- Setup Logging Context ---
    task_id = self.request.id or "unknown_task_id"
    attempt = self.request.retries + 1
    max_attempts = (self.max_retries or 0) + 1
    # Use structlog for task-specific logging with context
    log = task_struct_log.bind(
        task_id=task_id,
        attempt=f"{attempt}/{max_attempts}",
        doc_id=document_id_str,
        company_id=company_id_str,
        filename=filename,
        content_type=content_type
    )
    log.info("Starting standalone document processing task")

    # --- Pre-checks ---
    if not all([document_id_str, company_id_str, filename, content_type]):
        log.error("Missing required arguments in task payload.", payload_kwargs=kwargs)
        raise Reject("Missing required arguments (doc_id, company_id, filename, content_type)", requeue=False)

    # Check if essential global resources (DB engine, MinIO client) initialized
    if not sync_engine or not minio_client:
         log.critical("Core global resources (DB Engine or MinIO Client) are not initialized. Task cannot proceed.")
         raise Reject("Worker process core resource initialization failed.", requeue=False)

    # --- Prepare variables ---
    try:
        doc_uuid = uuid.UUID(document_id_str)
    except ValueError:
         log.error("Invalid document_id format received.")
         raise Reject("Invalid document_id format.", requeue=False)

    # Validate content type against new EXTRACTORS map
    if content_type not in EXTRACTORS:
        log.error(f"Unsupported content type provided: {content_type}")
        raise Reject(f"Unsupported content type: {content_type}", requeue=False)


    object_name = f"{company_id_str}/{document_id_str}/{filename}"
    temp_file_path_obj: Optional[pathlib.Path] = None # Store Path object
    inserted_chunk_count = 0 # Track number of chunks inserted

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
            temp_file_path_obj = temp_dir_path / filename # Use pathlib Path object
            log.info(f"Downloading MinIO object: {object_name} -> {str(temp_file_path_obj)}")
            minio_client.download_file_sync(object_name, str(temp_file_path_obj))
            log.info("File downloaded successfully from MinIO.")

            # 3. Execute Standalone Ingestion Pipeline
            log.info("Executing standalone ingest pipeline (extract, chunk, embed, insert)...")
            # Call the refactored pipeline function
            # This function now handles extraction, chunking, embedding, and Milvus insertion
            inserted_chunk_count = ingest_document_pipeline(
                file_path=temp_file_path_obj,
                company_id=company_id_str,
                document_id=document_id_str,
                content_type=content_type,
                delete_existing=True # Ensure idempotency by default
            )
            log.info(f"Ingestion pipeline finished. Inserted chunks: {inserted_chunk_count}")

        # 4. Update status to PROCESSED
        log.debug("Setting status to PROCESSED in DB.")
        final_status_updated = set_status_sync(
            engine=sync_engine,
            document_id=doc_uuid,
            status=DocumentStatus.PROCESSED,
            chunk_count=inserted_chunk_count, # Store the actual count inserted
            error_message=None
        )
        if not final_status_updated:
             log.warning("Failed to update status to PROCESSED after successful processing (document possibly deleted?).")

        log.info(f"Document processing finished successfully. Final chunk count: {inserted_chunk_count}")
        return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": inserted_chunk_count, "document_id": document_id_str}

    # --- Specific Error Handling ---
    except MinioError as me:
        log.error(f"MinIO Error during processing: {me}", exc_info=True)
        error_msg = f"MinIO Error: {str(me)[:400]}"
        try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
        except Exception as db_err: log.critical("Failed to update status to ERROR after MinIO failure!", error=str(db_err))
        if "Object not found" in str(me):
            raise Reject(f"MinIO Error: Object not found: {object_name}", requeue=False) from me
        else: raise me # Let Celery retry other MinIO errors

    except (ValueError, ConnectionError, RuntimeError) as pipeline_err:
         # Catch specific, potentially non-retryable errors from the pipeline (e.g., unsupported type, Milvus connection)
         log.error(f"Pipeline Error: {pipeline_err}", exc_info=True)
         error_msg = f"Pipeline Error: {type(pipeline_err).__name__} - {str(pipeline_err)[:400]}"
         try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
         except Exception as db_err: log.critical("Failed to update status to ERROR after pipeline failure!", error=str(db_err))
         # Raise as Reject because these are likely config/setup/permanent issues
         raise Reject(f"Pipeline failed: {error_msg}", requeue=False) from pipeline_err

    except Reject as r:
         log.error(f"Task rejected permanently: {r.reason}")
         try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=f"Rejected: {r.reason}"[:500])
         except Exception as db_err: log.critical("Failed to update status to ERROR after task rejection!", error=str(db_err))
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
        # Do not re-raise; let Celery mark as failed

    except Exception as exc:
        # Catch-all for other unexpected errors (should trigger autoretry)
        log.exception(f"An unexpected error occurred during document processing")
        error_msg = f"Attempt {attempt} failed: {type(exc).__name__} - {str(exc)[:400]}"
        try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
        except Exception as db_err: log.critical("CRITICAL: Failed to update status to ERROR after unexpected failure!", error=str(db_err))
        raise exc # Re-raise to let Celery handle retry based on autoretry_for

# Assign the refactored task function to the name expected by the API endpoint
process_document_haystack_task = process_document_standalone