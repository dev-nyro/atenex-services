# ingest-service/app/tasks/process_document.py
import os
import tempfile
import uuid
import sys
from typing import Optional, Dict, Any, List, Tuple, Type

import structlog
from celery import Task
from celery.exceptions import Ignore, Reject, MaxRetriesExceededError
from celery.utils.log import get_task_logger # Use Celery's task logger

# --- Haystack Dependencies ---
from haystack.dataclasses import Document
from haystack.document_stores.types import DuplicatePolicy
from haystack.components.converters import (
    PyPDFToDocument, MarkdownToDocument, HTMLToDocument, TextFileToDocument, DOCXToDocument
)
from haystack.components.preprocessors import DocumentSplitter
from haystack.components.writers import DocumentWriter
from haystack.utils import ComponentDevice # Keep import for potential future type checks or other uses

# --- Milvus Dependencies ---
from milvus_haystack import MilvusDocumentStore

# --- FastEmbed Dependencies ---
from haystack_integrations.components.embedders.fastembed import FastembedDocumentEmbedder

# --- Custom Application Imports ---
from app.core.config import settings
# Import the specific sync functions needed
from app.db.postgres_client import get_sync_engine, set_status_sync
from app.models.domain import DocumentStatus
from app.services.minio_client import MinioClient, MinioError
from app.tasks.celery_app import celery_app

# --- Synchronous Database Client Engine ---
try:
    # Get the engine once when the module loads
    sync_engine = get_sync_engine()
except Exception as db_init_err:
    # Log critical error if engine fails to initialize
    structlog.get_logger(__name__).critical(
        "Failed to initialize synchronous DB engine! Worker tasks depending on DB will fail.",
        error=str(db_init_err),
        exc_info=True
    )
    sync_engine = None


# --------------------------------------------------------------------------
# Global Resource Initialization (Executed once per worker process)
# --------------------------------------------------------------------------
log = structlog.get_logger(__name__) # Module-level logger

# Initialize resources in a try-except block to handle potential init failures
minio_client = None
milvus_store = None
splitter = None
embedder = None
writer = None
CONVERTERS = {}

try:
    log.info("Initializing global resources for Celery worker process...")

    # 1. MinIO Client (uses sync methods internally)
    minio_client = MinioClient()
    log.info("Global MinIO client initialized.")

    # 2. Milvus Document Store
    milvus_connection_args = {"uri": settings.MILVUS_URI}
    if settings.MILVUS_URI.lower().startswith("https"):
        milvus_connection_args["secure"] = True

    # NOTE: No 'dim' or 'embedding_dim' needed here for Haystack 2.x constructor
    milvus_store = MilvusDocumentStore(
        connection_args=milvus_connection_args,
        collection_name=settings.MILVUS_COLLECTION_NAME,
        consistency_level="Strong", # Ensure consistency for reads after writes
    )
    log.info("Global MilvusDocumentStore initialized.",
             uri_scheme=settings.MILVUS_URI.split(':')[0],
             collection=settings.MILVUS_COLLECTION_NAME,
             consistency="Strong")

    # 3. Haystack Components
    log.info("Initializing global Haystack components...")
    splitter = DocumentSplitter(
        split_by=settings.SPLITTER_SPLIT_BY,
        split_length=settings.SPLITTER_CHUNK_SIZE,
        split_overlap=settings.SPLITTER_CHUNK_OVERLAP
    )
    log.info("Global DocumentSplitter initialized.",
             split_by=settings.SPLITTER_SPLIT_BY,
             length=settings.SPLITTER_CHUNK_SIZE,
             overlap=settings.SPLITTER_CHUNK_OVERLAP)

    # --- Determine device for FastEmbed (CORRECTED) ---
    # Pass device as a simple string ("cpu" or "cuda:0") based on settings
    # This resolves the TypeError: __init__() got an unexpected keyword argument 'device'
    # because the argument name might be correct, but the type being passed before was wrong.
    # Now we pass the expected string type.
    device_str = "cuda:0" if settings.USE_GPU else "cpu"
    log.info("Selected device for FastEmbed based on settings", device=device_str, use_gpu_setting=settings.USE_GPU)

    embedder = FastembedDocumentEmbedder(
        model=settings.FASTEMBED_MODEL,
        # Pass the determined device string directly. This is the fix for the TypeError.
        device=device_str,
        batch_size=256, # Adjust batch size as needed
        # parallel=0 makes sense for CPU, disables multiprocess parallelization within FastEmbed
        parallel=0 if device_str.startswith("cpu") else None # Disable internal parallel if CPU
    )
    log.info("Global FastembedDocumentEmbedder initialized.", model=settings.FASTEMBED_MODEL, device=device_str, parallel=embedder.parallel)
    # --- End Correction ---

    log.info("Warming up FastEmbed model...")
    embedder.warm_up()
    log.info("Global FastEmbed model warmed up successfully.")

    writer = DocumentWriter(
        document_store=milvus_store,
        policy=DuplicatePolicy.OVERWRITE, # Overwrite existing chunks for the same document ID
        batch_size=512 # Adjust batch size as needed
    )
    log.info("Global DocumentWriter initialized.", policy="OVERWRITE", batch_size=512)

    # 4. Converters Mapping
    CONVERTERS = {
        "application/pdf": PyPDFToDocument,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DOCXToDocument,
        "application/msword": DOCXToDocument, # Also handle .doc via DOCX converter (may need additional libs like antiword if problematic)
        "text/plain": TextFileToDocument,
        "text/markdown": MarkdownToDocument,
        "text/html": HTMLToDocument,
    }
    log.info("Global Converters mapping created.")

    log.info("Global resources for Celery worker process initialized successfully.")

except Exception as e:
    # Log the critical failure during initialization
    log.critical("CRITICAL: Failed to initialize one or more global resources in worker process!", error=str(e), exc_info=True)
    # Setting resources to None ensures the check within the task fails cleanly
    minio_client = milvus_store = splitter = embedder = writer = None
    # No need to re-raise here, the check inside the task will handle it


# --------------------------------------------------------------------------
# Synchronous Celery Task Definition
# --------------------------------------------------------------------------
@celery_app.task(
    bind=True,
    name="ingest.process_document", # Explicit name for clarity
    autoretry_for=(Exception,), # Retry on general exceptions (network, temp issues)
    # Exclude Reject and Ignore from automatic retries
    exclude=(Reject, Ignore),
    retry_backoff=True,      # Enable exponential backoff
    retry_backoff_max=600,   # Max delay 600 seconds (10 minutes)
    retry_jitter=True,       # Add jitter to delays
    max_retries=3            # Max 3 retries (plus initial attempt)
)
def process_document_sync(self: Task, *args, **kwargs) -> Dict[str, Any]:
    """
    Synchronous Celery task to process a single document.
    Downloads from MinIO, converts, chunks, embeds (CPU), and writes to Milvus.
    Uses globally initialized resources and updates status in PostgreSQL synchronously.
    """
    document_id_str = kwargs.get('document_id')
    company_id_str = kwargs.get('company_id')
    filename = kwargs.get('filename')
    content_type = kwargs.get('content_type')

    # Use Celery's logger for task-specific context including attempt number
    task_log = get_task_logger(f"{__name__}.{document_id_str}")
    task_id = self.request.id or "unknown_task_id"
    attempt = self.request.retries + 1
    max_attempts = (self.max_retries or 0) + 1
    log_context = {"doc": filename, "doc_id": document_id_str, "company_id": company_id_str, "task_id": task_id, "attempt": f"{attempt}/{max_attempts}"}
    task_log.info(f"Starting sync processing", **log_context)

    # --- Pre-checks for required arguments and global resources ---
    if not all([document_id_str, company_id_str, filename, content_type]):
        task_log.error("Missing required arguments in task payload.", payload_kwargs=kwargs, **log_context)
        # Use Reject for permanent failure due to bad input
        raise Reject("Missing required arguments (doc_id, company_id, filename, content_type)", requeue=False)

    # Check if global resources failed initialization (logged above)
    if not sync_engine or not minio_client or not milvus_store or not splitter or not embedder or not writer:
         task_log.critical("One or more global resources (DB, MinIO, Milvus, Haystack) are not initialized. Task cannot proceed.", **log_context)
         # This is a worker setup issue, not task-specific. Reject permanently.
         raise Reject("Worker process global resource initialization failed.", requeue=False)

    # --- Prepare variables ---
    try:
        doc_uuid = uuid.UUID(document_id_str)
    except ValueError:
         task_log.error("Invalid document_id format received.", **log_context)
         raise Reject("Invalid document_id format.", requeue=False)

    # Construct the object name used for MinIO storage
    object_name = f"{company_id_str}/{document_id_str}/{filename}"
    temp_file_path = None
    written_count = 0 # Track number of chunks written

    try:
        # 1. Update status to PROCESSING (Synchronous DB call)
        task_log.debug("Setting status to PROCESSING in DB.", **log_context)
        status_updated = set_status_sync(
            engine=sync_engine,
            document_id=doc_uuid,
            status=DocumentStatus.PROCESSING,
            error_message=None # Clear any previous errors
        )
        if not status_updated:
            task_log.warning("Failed to update status to PROCESSING (document possibly deleted?). Ignoring task.", **log_context)
            raise Ignore() # Ignore if DB update fails (e.g., row gone)
        task_log.info("Status set to PROCESSING.", **log_context)

        # 2. Download file from MinIO (Synchronous) into a temporary directory
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_file_path = os.path.join(temp_dir, filename)
            task_log.info(f"Downloading MinIO object: {object_name} -> {temp_file_path}", **log_context)
            minio_client.download_file_sync(object_name, temp_file_path) # Uses sync method
            task_log.info("File downloaded successfully from MinIO.", **log_context)

            # 3. Select Appropriate Haystack Converter based on content_type
            ConverterClass = CONVERTERS.get(content_type)
            if not ConverterClass:
                task_log.error(f"Unsupported content type provided: {content_type}", **log_context)
                # Permanent failure if file type is unsupported
                raise Reject(f"Unsupported content type: {content_type}", requeue=False)

            converter = ConverterClass()
            task_log.debug(f"Using converter: {ConverterClass.__name__}", **log_context)

            # 4. Execute Haystack Pipeline (Synchronous Steps)
            task_log.info("Starting Haystack pipeline execution (Convert -> Meta -> Split -> Embed -> Write)...", **log_context)

            # --- Step 4a: Conversion ---
            task_log.debug("Running file conversion...", **log_context)
            # Converters expect a list of sources
            conversion_result = converter.run(sources=[temp_file_path])
            # Extract the list of Haystack Document objects
            docs: List[Document] = conversion_result.get("documents", [])
            task_log.info(f"Conversion complete. Found {len(docs)} initial document(s).", **log_context)

            # If conversion yields no documents, stop processing.
            if not docs:
                task_log.warning("Converter returned no documents. Nothing to process.", **log_context)
                written_count = 0 # Ensure count is 0
            else:
                # --- Step 4b: Add Essential Metadata ---
                task_log.debug("Adding standard metadata to documents...", **log_context)
                for doc in docs:
                    if doc.meta is None: doc.meta = {}
                    # Add multi-tenant info and file context for filtering/retrieval
                    doc.meta.update({
                        "company_id": str(company_id_str), # Store as string for Milvus/filtering
                        "document_id": str(document_id_str), # Store as string
                        "file_name": str(filename),
                        "file_type": str(content_type),
                        # Add other relevant meta from original doc if needed
                        # "source_page": doc.meta.get("page_number") # Example if converter provides it
                    })
                    # Clean potential problematic meta values (e.g., None)
                    doc.meta = {k: v for k, v in doc.meta.items() if v is not None}


                # --- Step 4c: Splitting ---
                task_log.debug("Running document splitting...", **log_context)
                split_result = splitter.run(documents=docs)
                split_docs: List[Document] = split_result.get("documents", [])
                task_log.info(f"Splitting complete. Generated {len(split_docs)} chunks.", **log_context)

                if not split_docs:
                    task_log.warning("Splitter returned no chunks after processing documents.", **log_context)
                    written_count = 0
                else:
                    # --- Step 4d: Embedding ---
                    task_log.debug(f"Running embedding for {len(split_docs)} chunks using {settings.FASTEMBED_MODEL} on {device_str}...", **log_context)
                    embed_result = embedder.run(documents=split_docs)
                    embedded_docs: List[Document] = embed_result.get("documents", [])
                    task_log.info(f"Embedding complete for {len(embedded_docs)} chunks.", **log_context)

                    if not embedded_docs:
                       task_log.warning("Embedder returned no documents after processing chunks.", **log_context)
                       written_count = 0
                    else:
                        # --- Step 4e: Writing to Milvus ---
                        task_log.debug(f"Running writing of {len(embedded_docs)} embedded chunks to Milvus...", **log_context)
                        write_result = writer.run(documents=embedded_docs)
                        # Check the key returned by writer.run in Haystack 2.x (usually 'documents_written')
                        written_count = write_result.get("documents_written", 0)

                        task_log.info(f"Writing complete. Attempted to write {len(embedded_docs)} chunks, result reports {written_count} written.", **log_context)
                        if written_count != len(embedded_docs):
                             task_log.warning("Mismatch between chunks sent to writer and reported written count.", sent=len(embedded_docs), reported=written_count, **log_context)
                             # Potentially retry or investigate Milvus logs if this happens often


        # 5. Update status to PROCESSED (Synchronous DB call)
        task_log.debug("Setting status to PROCESSED in DB.", **log_context)
        final_status_updated = set_status_sync(
            engine=sync_engine,
            document_id=doc_uuid,
            status=DocumentStatus.PROCESSED,
            chunk_count=written_count, # Store the actual count reported by the writer
            error_message=None # Clear any previous error
        )
        if not final_status_updated:
             # This is unlikely if the first update worked, but handle defensively
             task_log.warning("Failed to update status to PROCESSED after successful processing (document possibly deleted?).", **log_context)
             # Task succeeded functionally, but DB state might be off. Log and finish.

        task_log.info(f"Document processing finished successfully. Final chunk count: {written_count}", **log_context)
        # Return a result dictionary for Celery backend storage
        return {"status": DocumentStatus.PROCESSED.value, "chunks_written": written_count, "document_id": document_id_str}

    except MinioError as me:
        # Handle specific MinIO errors (e.g., file not found)
        task_log.error(f"MinIO Error during processing: {me}", **log_context, exc_info=True)
        error_msg = f"MinIO Error: {str(me)[:400]}"
        try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
        except Exception as db_err: task_log.critical("Failed to update status to ERROR after MinIO failure!", error=str(db_err), **log_context)
        # If file not found, reject permanently. Otherwise, let Celery retry.
        if "Object not found" in str(me):
            raise Reject(f"MinIO Error: Object not found: {object_name}", requeue=False) from me
        else:
            # Re-raise for Celery's autoretry
            raise me

    except Reject as r:
         # Handle non-retryable rejections explicitly set in the code
         task_log.error(f"Task rejected permanently: {r.reason}", **log_context)
         try:
              set_status_sync(
                    engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR,
                    error_message=f"Rejected: {r.reason}"[:500] # Truncate error message
              )
         except Exception as db_final_err:
              task_log.critical("Failed to update status to ERROR after task rejection!", error=str(db_final_err), **log_context)
         raise r # Re-raise Reject to notify Celery

    except Ignore:
         # Handle ignored tasks (e.g., DB update failed indicating doc was deleted)
         task_log.info("Task ignored.", **log_context)
         raise Ignore() # Re-raise Ignore

    except MaxRetriesExceededError as mree:
        # Handle exceeding max retries
        task_log.error("Max retries exceeded for task.", **log_context, exc_info=True)
        error_msg = f"Max retries exceeded ({max_attempts}). Last error: {str(mree.cause)[:300]}"
        try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
        except Exception as db_final_err: task_log.critical("Failed to update status to ERROR after max retries!", error=str(db_final_err), **log_context)
        # Don't re-raise, let Celery handle the MaxRetriesExceededError state

    except Exception as exc:
        # Catch all other exceptions for retry or final failure
        task_log.exception(f"An unexpected error occurred during document processing", **log_context, exc_info=True)
        error_msg = f"Attempt {attempt} failed: {type(exc).__name__} - {str(exc)[:400]}" # Truncate
        try:
            set_status_sync( # Update DB to ERROR before retrying
                engine=sync_engine,
                document_id=doc_uuid,
                status=DocumentStatus.ERROR,
                error_message=error_msg
            )
            task_log.info("Set status to ERROR in DB before retrying/failing.", **log_context)
        except Exception as db_final_err:
             # Log critical failure, but still raise original exception for retry mechanism
             task_log.critical("CRITICAL: Failed to update status to ERROR after processing failure!", error=str(db_final_err), **log_context)

        # Re-raise the original exception to trigger Celery's autoretry logic
        raise exc

# Make the task instance easily accessible if needed elsewhere
process_document_haystack_task = process_document_sync