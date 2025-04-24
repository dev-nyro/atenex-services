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
from haystack.utils import ComponentDevice

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

    # NOTE: Assuming grpc_timeout is handled via pymilvus config or env vars if needed,
    # as it's not a direct constructor arg in recent milvus-haystack versions.
    # --- CORRECTION: Reverted 'dim' back to 'embedding_dim' based on the latest error log ---
    milvus_store = MilvusDocumentStore(
        connection_args=milvus_connection_args,
        collection_name=settings.MILVUS_COLLECTION_NAME,
        embedding_dim=settings.EMBEDDING_DIMENSION, # <-- Reverted to embedding_dim
        consistency_level="Strong",
    )
    log.info("Global MilvusDocumentStore initialized.",
             uri_scheme=settings.MILVUS_URI.split(':')[0], # Log scheme only
             collection=settings.MILVUS_COLLECTION_NAME,
             embedding_dim=settings.EMBEDDING_DIMENSION,
             consistency="Strong")
    # --- END CORRECTION ---

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

    # Determine device for FastEmbed
    if settings.USE_GPU:
        try:
            embedder_device = ComponentDevice.from_str("cuda:0")
            log.info("GPU selected for FastEmbed based on settings.")
        except Exception as gpu_err:
            log.warning("GPU configured but FAILED to select, falling back to CPU.", error=str(gpu_err), setting_use_gpu=settings.USE_GPU)
            embedder_device = ComponentDevice.from_str("cpu")
    else:
        embedder_device = ComponentDevice.from_str("cpu")
        log.info("CPU selected for FastEmbed (USE_GPU is false).")

    embedder = FastembedDocumentEmbedder(
        model=settings.FASTEMBED_MODEL,
        device=embedder_device,
        batch_size=256,
        parallel=0 if embedder_device.type == "cpu" else None
    )
    log.info("Global FastembedDocumentEmbedder initialized.", model=settings.FASTEMBED_MODEL, device=str(embedder_device))

    log.info("Warming up FastEmbed model...")
    embedder.warm_up()
    log.info("Global FastEmbed model warmed up successfully.")

    writer = DocumentWriter(
        document_store=milvus_store,
        policy=DuplicatePolicy.OVERWRITE,
        batch_size=512
    )
    log.info("Global DocumentWriter initialized.", policy="OVERWRITE", batch_size=512)

    # 4. Converters Mapping
    CONVERTERS = {
        "application/pdf": PyPDFToDocument,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DOCXToDocument,
        "application/msword": DOCXToDocument,
        "text/plain": TextFileToDocument,
        "text/markdown": MarkdownToDocument,
        "text/html": HTMLToDocument,
    }
    log.info("Global Converters mapping created.")

    log.info("Global resources for Celery worker process initialized successfully.")

except Exception as e:
    log.critical("CRITICAL: Failed to initialize global resources in worker process!", error=str(e), exc_info=True)
    # Set flags or raise an error to prevent tasks from running if core components fail
    # For simplicity, we let tasks fail if resources are None, logged below.


# --------------------------------------------------------------------------
# Synchronous Celery Task Definition
# --------------------------------------------------------------------------
@celery_app.task(
    bind=True,
    name="ingest.process_document", # Use the new explicit name
    autoretry_for=(Exception,), # Retry on most exceptions
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3
)
def process_document_sync(self: Task, *args, **kwargs) -> Dict[str, Any]:
    """
    Synchronous Celery task to process a single document.
    Uses globally initialized resources (MinIO, Milvus, Haystack components).
    Updates document status in PostgreSQL synchronously.
    """
    document_id_str = kwargs.get('document_id')
    company_id_str = kwargs.get('company_id')
    filename = kwargs.get('filename')
    content_type = kwargs.get('content_type')

    # Use Celery's logger for task-specific context
    task_log = get_task_logger(f"{__name__}.{document_id_str}")
    task_id = self.request.id or "unknown_task_id"
    attempt = self.request.retries + 1
    task_log.info(f"Starting sync processing: doc='{filename}' task_id={task_id} [Attempt {attempt}/{self.max_retries + 1}]")

    # --- Pre-checks for required arguments and global resources ---
    if not all([document_id_str, company_id_str, filename, content_type]):
        task_log.error("Missing required arguments in task payload.", payload_kwargs=kwargs)
        raise Reject("Missing required arguments (doc_id, company_id, filename, content_type)", requeue=False)

    if not sync_engine or not minio_client or not milvus_store or not splitter or not embedder or not writer:
         task_log.critical("One or more global resources (DB, MinIO, Milvus, Haystack) are not initialized. Task cannot proceed.")
         # This indicates a setup failure in the worker process, likely non-retryable per task.
         raise Reject("Worker process global resource initialization failed.", requeue=False)

    doc_uuid = uuid.UUID(document_id_str)
    # company_uuid = uuid.UUID(company_id_str) # No longer needed if only passing strings
    object_name = f"{company_id_str}/{document_id_str}/{filename}"
    temp_file_path = None
    written_count = 0

    try:
        # 1. Update status to PROCESSING (Synchronous)
        task_log.debug("Setting status to PROCESSING in DB.")
        set_status_sync( # Use the imported sync function
            engine=sync_engine,
            document_id=doc_uuid,
            status=DocumentStatus.PROCESSING,
            error_message=None # Clear previous errors
        )
        task_log.info("Status set to PROCESSING.")

        # 2. Download file from MinIO (Synchronous)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_file_path = os.path.join(temp_dir, filename)
            task_log.info(f"Downloading MinIO file: {object_name} -> {temp_file_path}")
            minio_client.download_file_sync(object_name, temp_file_path) # Use sync method
            task_log.info("File downloaded successfully.")

            # 3. Select Haystack Converter
            ConverterClass = CONVERTERS.get(content_type)
            if not ConverterClass:
                task_log.error(f"Unsupported content type: {content_type}")
                raise Reject(f"Unsupported content type: {content_type}", requeue=False)
            converter = ConverterClass()
            task_log.debug(f"Using converter: {ConverterClass.__name__}")

            # 4. Run Haystack Pipeline (Synchronous)
            task_log.info("Starting Haystack pipeline execution...")

            # --- Conversion ---
            task_log.debug("Running conversion...")
            conversion_result = converter.run(sources=[temp_file_path])
            docs: List[Document] = conversion_result["documents"]
            task_log.info(f"Conversion complete. Found {len(docs)} initial document(s).")

            if not docs:
                task_log.warning("Converter returned no documents. Finishing task.")
                written_count = 0
            else:
                # --- Add Metadata ---
                task_log.debug("Adding standard metadata...")
                for doc in docs:
                    if doc.meta is None: doc.meta = {}
                    # Ensure metadata keys are strings and values are compatible
                    doc.meta.update({
                        "company_id": str(company_id_str), # Store as string
                        "document_id": str(document_id_str), # Store as string
                        "file_name": str(filename),
                        "file_type": str(content_type),
                    })

                # --- Splitting ---
                task_log.debug("Running splitting...")
                split_result = splitter.run(documents=docs)
                split_docs: List[Document] = split_result["documents"]
                task_log.info(f"Splitting complete. Generated {len(split_docs)} chunks.")

                if not split_docs:
                    task_log.warning("Splitter returned no chunks. Finishing task.")
                    written_count = 0
                else:
                    # --- Embedding ---
                    task_log.debug("Running embedding...")
                    embed_result = embedder.run(documents=split_docs)
                    embedded_docs: List[Document] = embed_result["documents"]
                    task_log.info(f"Embedding complete for {len(embedded_docs)} chunks.")

                    if not embedded_docs:
                       task_log.warning("Embedder returned no documents. Finishing task.")
                       written_count = 0
                    else:
                        # --- Writing ---
                        task_log.debug("Running writing to Milvus...")
                        write_result = writer.run(documents=embedded_docs)
                        # Check the actual key returned by writer.run based on Haystack version
                        # It might be "documents_written" or similar
                        if "documents_written" in write_result:
                            written_count = write_result["documents_written"]
                        elif "count" in write_result: # Fallback check
                             written_count = write_result["count"]
                        else:
                            task_log.warning("Could not determine written count from writer result", result_keys=write_result.keys())
                            written_count = len(embedded_docs) # Best guess

                        task_log.info(f"Writing complete. Wrote/Attempted {written_count} documents/chunks to Milvus.")

        # 5. Update status to PROCESSED (Synchronous)
        task_log.debug("Setting status to PROCESSED in DB.")
        set_status_sync( # Use the imported sync function
            engine=sync_engine,
            document_id=doc_uuid,
            status=DocumentStatus.PROCESSED,
            chunk_count=written_count,
            error_message=None # Clear any previous error
        )
        task_log.info(f"Document processing finished successfully. Chunks processed: {written_count}")
        # Return a dictionary suitable for Celery result backend
        return {"status": DocumentStatus.PROCESSED.value, "chunks_written": written_count}

    except Reject as r:
         # Handle non-retryable rejections explicitly
         task_log.error(f"Task rejected permanently: {r.reason}")
         try:
              set_status_sync(
                    engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR,
                    error_message=f"Rejected: {r.reason}"[:500] # Truncate
              )
         except Exception as db_final_err:
              task_log.critical("Failed to update status to ERROR after task rejection!", error=str(db_final_err))
         raise r # Re-raise Reject to notify Celery

    except Exception as exc:
        task_log.exception(f"An error occurred during document processing [Attempt {attempt}]", exc_info=True)
        error_msg = f"Attempt {attempt} failed: {type(exc).__name__} - {str(exc)[:400]}" # Truncate
        try:
            set_status_sync( # Use the imported sync function
                engine=sync_engine,
                document_id=doc_uuid,
                status=DocumentStatus.ERROR,
                error_message=error_msg
            )
            task_log.info("Set status to ERROR in DB before retrying/failing.")
        except Exception as db_final_err:
             task_log.critical("CRITICAL: Failed to update status to ERROR after processing failure!", error=str(db_final_err))
             # Log but still raise original exception for Celery retry

        # Let Celery's autoretry handle the exception
        raise exc

# Assign explicit name for easier calling if needed elsewhere (optional but good practice)
process_document_haystack_task = process_document_sync