# ingest-service/app/tasks/process_document.py
import asyncio
import os
import tempfile
import uuid
from typing import Optional, Dict, Any, List, Tuple, Type
from contextlib import asynccontextmanager
import structlog
import logging
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type, before_sleep_log
from celery import Celery, Task
from celery.exceptions import Ignore, Reject, MaxRetriesExceededError
import httpx
import asyncpg

# Haystack Imports
from haystack.dataclasses import Document
from haystack.document_stores.types import DuplicatePolicy
from haystack.components.converters import (
    PyPDFToDocument,
    MarkdownToDocument,
    HTMLToDocument,
    TextFileToDocument,
)
from haystack.components.converters.docx import DOCXToDocument
from haystack.components.preprocessors import DocumentSplitter
from haystack.components.writers import DocumentWriter

# Milvus specific integration
from milvus_haystack import MilvusDocumentStore

# FastEmbed Imports
from haystack_integrations.components.embedders.fastembed import (
    FastembedDocumentEmbedder,
)
# Import for device selection
from haystack.utils import ComponentDevice

# Custom imports
from app.core.config import settings # Settings now includes USE_GPU, FASTEMBED_MODEL
from app.db import postgres_client as db_client
from app.models.domain import DocumentStatus
from app.services.minio_client import MinioClient, MinioError
from app.tasks.celery_app import celery_app

# Initialize logger
log = structlog.get_logger(__name__)

# Timeout for the entire processing flow within the task
TIMEOUT_SECONDS = 600 # 10 minutes, adjust as needed

# --- Milvus Initialization (sin cambios) ---
def _initialize_milvus_store() -> MilvusDocumentStore:
    """
    Synchronously initializes and returns a MilvusDocumentStore instance.
    Handles potential configuration errors during initialization.
    """
    log.debug("Initializing MilvusDocumentStore...")
    try:
        # Ensure EMBEDDING_DIMENSION is correctly set in config.py / configmap.yaml
        # for the FastEmbed model being used.
        store = MilvusDocumentStore(
            connection_args={"uri": settings.MILVUS_URI},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            embedding_dim=settings.EMBEDDING_DIMENSION, # Crucial: Use dimension from settings
            consistency_level="Strong",
        )
        log.info("MilvusDocumentStore initialized successfully.",
                 uri=settings.MILVUS_URI, collection=settings.MILVUS_COLLECTION_NAME,
                 embedding_dim=settings.EMBEDDING_DIMENSION)
        return store
    except TypeError as te:
        # Common error if embedding_dim is missing or wrong type
        log.exception("MilvusDocumentStore init TypeError (check embedding_dim)", error=str(te), exc_info=True)
        raise RuntimeError(f"Milvus TypeError (check embedding_dim={settings.EMBEDDING_DIMENSION}): {te}") from te
    except Exception as e:
        log.exception("Failed to initialize MilvusDocumentStore", error=str(e), exc_info=True)
        raise RuntimeError(f"Milvus Store Initialization Error: {e}") from e

# --- Haystack Component Initialization (Corregido para CPU) ---
def _initialize_haystack_components(
    document_store: MilvusDocumentStore
) -> Tuple[DocumentSplitter, FastembedDocumentEmbedder, DocumentWriter]:
    """
    Initializes Splitter + FastEmbed (CPU) + Writer.
    Respects settings.USE_GPU but forces CPU if false.
    """
    log.debug("Initializing Haystack components (Splitter, Embedder, Writer)...")
    try:
        # Document Splitter (Remains the same)
        splitter = DocumentSplitter(
            split_by=settings.SPLITTER_SPLIT_BY,
            split_length=settings.SPLITTER_CHUNK_SIZE,
            split_overlap=settings.SPLITTER_CHUNK_OVERLAP
        )
        log.info("DocumentSplitter initialized", split_by=settings.SPLITTER_SPLIT_BY, length=settings.SPLITTER_CHUNK_SIZE, overlap=settings.SPLITTER_CHUNK_OVERLAP)

        # <<< CORRECCIÓN: Determine Device based on settings.USE_GPU >>>
        if settings.USE_GPU:
            try:
                # Try GPU first if configured
                device = ComponentDevice.from_str("cuda:0")
                log.info("GPU configured AND selected for FastEmbed.", device_str="cuda:0")
            except Exception as gpu_err:
                # Fallback to CPU if GPU selection fails even if configured
                log.warning("GPU configured but FAILED to select, falling back to CPU.", error=str(gpu_err), setting_use_gpu=settings.USE_GPU)
                device = ComponentDevice.from_str("cpu")
        else:
            # Force CPU if USE_GPU is false
            device = ComponentDevice.from_str("cpu")
            log.info("CPU selected for FastEmbed (USE_GPU is false).", setting_use_gpu=settings.USE_GPU)
        # <<< FIN CORRECCIÓN >>>

        # Document Embedder (FastEmbed)
        log.info("Initializing FastembedDocumentEmbedder...", model=settings.FASTEMBED_MODEL, device=str(device))
        embedder = FastembedDocumentEmbedder(
            model=settings.FASTEMBED_MODEL,
            device=device, # Usa el dispositivo determinado
            batch_size=256,
            parallel=0 if device.type == "cpu" else None # Use max cores only on CPU
        )

        log.info("Warming up FastEmbed model...")
        embedder.warm_up()
        log.info("FastEmbed model warmed up successfully.")


        # Document Writer (using the initialized Milvus store)
        writer = DocumentWriter(
            document_store=document_store,
            policy=DuplicatePolicy.OVERWRITE,
            batch_size=512
        )
        log.info("DocumentWriter initialized", policy="OVERWRITE", batch_size=512)

        log.info("Haystack components initialized successfully.")
        return splitter, embedder, writer
    except Exception as e:
        log.exception("Failed to initialize Haystack components", error=str(e), exc_info=True)
        raise RuntimeError(f"Haystack Component Initialization Error: {e}") from e

# --- File Type to Converter Mapping (sin cambios) ---
def get_converter(content_type: str) -> Type[Any]:
    """Returns the appropriate Haystack Converter based on content type."""
    log.debug("Selecting converter", content_type=content_type)
    if content_type == "application/pdf":
        return PyPDFToDocument
    elif content_type in ["application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/msword"]:
        return DOCXToDocument
    elif content_type == "text/plain":
        return TextFileToDocument
    elif content_type == "text/markdown":
        return MarkdownToDocument
    elif content_type == "text/html":
        return HTMLToDocument
    else:
        log.warning("Unsupported content type for conversion", content_type=content_type)
        raise ValueError(f"Unsupported content type: {content_type}")

# --- Celery Task Setup & Logging (sin cambios) ---
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)
formatter = structlog.stdlib.ProcessorFormatter( processor=structlog.processors.JSONRenderer(), )
handler = logging.StreamHandler()
handler.setFormatter(formatter)
root_logger = logging.getLogger()
if not root_logger.handlers:
    root_logger.addHandler(handler)
    try: root_logger.setLevel(settings.LOG_LEVEL.upper())
    except ValueError: root_logger.setLevel("INFO"); log.warning("Invalid LOG_LEVEL, defaulting to INFO.")

db_retry_strategy = retry(
    stop=stop_after_attempt(3), wait=wait_fixed(2),
    retry=retry_if_exception_type((asyncpg.exceptions.PostgresConnectionError, TimeoutError, OSError)),
    before_sleep=before_sleep_log(log, logging.WARNING)
)

@asynccontextmanager
async def db_session_manager():
    """Provides a managed database connection pool session."""
    pool = None
    try: pool = await db_client.get_db_pool(); yield pool
    except Exception as e: log.error("Failed get DB pool", error=str(e)); raise
    finally: log.debug("DB session context exited.") # No pool closing here

# --- Main Asynchronous Processing Flow (sin cambios en la lógica interna del pipeline) ---
async def async_process_flow(
    *, document_id: str, company_id: str, filename: str,
    content_type: str, task_id: str, attempt: int
):
    """The core asynchronous processing logic for a single document."""
    flow_log = log.bind(
        document_id=document_id, company_id=company_id, task_id=task_id,
        attempt=attempt, filename=filename, content_type=content_type
    )
    flow_log.info("Starting asynchronous processing flow")
    minio_client = MinioClient()
    object_name = f"{company_id}/{document_id}/{filename}"
    temp_file_path = None
    total_chunks_written = 0 # Initialize here

    try:
        flow_log.info("Downloading file from MinIO", object_name=object_name)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_file_path = os.path.join(temp_dir, filename)
            await minio_client.download_file(object_name, temp_file_path)
            flow_log.info("File downloaded successfully", temp_path=temp_file_path)

            loop = asyncio.get_running_loop()
            store = await loop.run_in_executor(None, _initialize_milvus_store)
            splitter, embedder, writer = await loop.run_in_executor(None, _initialize_haystack_components, store)
            ConverterClass = get_converter(content_type)
            converter = ConverterClass()

            flow_log.info("Starting Haystack pipeline execution (converter, splitter, embedder, writer)...")

            def run_haystack_pipeline_sync(local_temp_file_path):
                # Need to re-bind log inside executor if you want task context
                pipeline_log = structlog.get_logger("sync_pipeline").bind(
                     document_id=document_id, task_id=task_id, filename=filename
                )
                pipeline_log.debug("Executing conversion...")
                # converter, splitter, embedder, writer are available via closure
                conversion_result = converter.run(sources=[local_temp_file_path])
                docs = conversion_result["documents"]
                pipeline_log.debug("Conversion complete", num_docs_converted=len(docs))
                if not docs: pipeline_log.warning("Converter produced 0 docs."); return 0

                # Add metadata
                for doc in docs:
                    if doc.meta is None: doc.meta = {}
                    doc.meta["company_id"] = company_id
                    doc.meta["document_id"] = document_id
                    doc.meta["file_name"] = filename
                    doc.meta["file_type"] = content_type

                pipeline_log.debug("Executing splitting...")
                split_docs = splitter.run(documents=docs)["documents"]
                pipeline_log.debug("Splitting complete", num_chunks=len(split_docs))
                if not split_docs: pipeline_log.warning("Splitter produced 0 chunks."); return 0

                pipeline_log.debug("Executing embedding...")
                embedded_docs = embedder.run(documents=split_docs)["documents"]
                pipeline_log.debug("Embedding complete.")
                if not embedded_docs: pipeline_log.warning("Embedder produced 0 embedded docs."); return 0

                pipeline_log.debug("Executing writing to Milvus...")
                write_result = writer.run(documents=embedded_docs)
                written_count = write_result["documents_written"]
                pipeline_log.info("Writing complete.", documents_written=written_count)
                return written_count # Return count from sync function

            # Execute sync pipeline in executor and store the result
            chunks_written = await loop.run_in_executor(None, run_haystack_pipeline_sync, temp_file_path)
            total_chunks_written = chunks_written # Assign the result
            flow_log.info("Haystack pipeline execution finished.", chunks_written=total_chunks_written)
            # Return the count from async function
            return total_chunks_written

    except MinioError as me:
        flow_log.error("MinIO Error", object_name=object_name, error=str(me))
        raise RuntimeError(f"MinIO failed: {me}") from me # Re-raise as runtime for celery retry logic
    except ValueError as ve: # Catch unsupported content type
         flow_log.error("Value Error (likely unsupported type)", error=str(ve))
         raise ve # Re-raise to be caught by Celery task as non-retryable
    except RuntimeError as rt_err: # Catch Milvus/Haystack init or pipeline errors
         flow_log.error("Runtime Error during processing", error=str(rt_err))
         raise rt_err # Re-raise to be caught by Celery task
    except Exception as e:
        flow_log.exception("Unexpected error during processing flow", error=str(e))
        raise RuntimeError(f"Unexpected flow error: {e}") from e # Wrap as Runtime

# --- Celery Task Definition (sin cambios en la estructura de la clase) ---
class ProcessDocumentTask(Task):
    name = "app.tasks.process_document.ProcessDocumentTask"
    max_retries = 3
    default_retry_delay = 60

    def __init__(self):
        super().__init__()
        self.task_log = log.bind(task_name=self.name)
        self.task_log.info("ProcessDocumentTask initialized.")

    async def _update_status_with_retry(
        self, pool: asyncpg.Pool, doc_id: str, status: DocumentStatus,
        chunk_count: Optional[int] = None, error_msg: Optional[str] = None
    ):
        update_log = self.task_log.bind(document_id=doc_id, target_status=status.value)
        try:
            async with pool.acquire() as conn:
                await db_retry_strategy(db_client.update_document_status)(
                    conn=conn, document_id=uuid.UUID(doc_id), status=status,
                    chunk_count=chunk_count, error_message=error_msg
                )
            update_log.info("Document status updated successfully in DB.")
        except Exception as e:
            update_log.critical("CRITICAL: Failed final DB status update!", error=str(e), exc_info=True)
            raise ConnectionError(f"Persistent DB error updating status for {doc_id}") from e

    async def run_async_processing(self, *args, **kwargs):
        doc_id = kwargs['document_id']
        task_id = self.request.id if self.request else 'N/A'
        attempt = (self.request.retries + 1) if self.request else 1
        task_log = self.task_log.bind(document_id=doc_id, task_id=task_id, attempt=attempt)
        final_status = DocumentStatus.ERROR
        final_chunk_count = 0 # Default to 0
        error_to_report = "Unknown processing error"
        processing_exception: Optional[Exception] = None

        try:
            async with db_session_manager() as pool:
                 if not pool: raise Reject("DB pool unavailable", requeue=False)

                 try:
                    task_log.info("Setting status to 'processing'")
                    await self._update_status_with_retry(pool, doc_id, DocumentStatus.PROCESSING, error_msg=None)

                    task_log.info("Executing main async_process_flow with timeout", timeout=TIMEOUT_SECONDS)
                    # Store result which is the chunk count
                    final_chunk_count = await asyncio.wait_for(
                        async_process_flow(task_id=task_id, attempt=attempt, **kwargs),
                        timeout=TIMEOUT_SECONDS
                    )
                    final_status = DocumentStatus.PROCESSED
                    error_to_report = None # Clear error on success
                    task_log.info("Async process flow completed successfully.", chunks_processed=final_chunk_count)

                 except asyncio.TimeoutError as e:
                     task_log.error("Processing timed out", timeout=TIMEOUT_SECONDS)
                     error_to_report = f"Processing timed out after {TIMEOUT_SECONDS}s."
                     processing_exception = e
                 except ValueError as e: # Non-retryable config/input error
                      task_log.error("Processing failed: Value error", error=str(e))
                      error_to_report = f"Input/Config Error: {e}"
                      processing_exception = e
                 except RuntimeError as e: # Catch specific runtime errors from flow
                      task_log.error("Processing failed: Runtime error", error=str(e), exc_info=False)
                      error_to_report = f"Runtime Error: {e}"
                      processing_exception = e
                 except ConnectionError as e: # DB update failure within retry helper
                      task_log.critical("Persistent DB connection error", error=str(e))
                      raise Reject(f"DB conn error for {doc_id}: {e}", requeue=False) from e
                 except Exception as e: # Catch other unexpected errors
                     task_log.exception("Unexpected exception during flow.", error=str(e))
                     error_to_report = f"Unexpected error: {type(e).__name__}"
                     processing_exception = e

                 # Final status update attempt (always happens outside inner try)
                 task_log.info("Attempting final DB status update", status=final_status.value, chunks=final_chunk_count, error=error_to_report)
                 await self._update_status_with_retry(
                     pool, doc_id, final_status,
                     chunk_count=final_chunk_count if final_status == DocumentStatus.PROCESSED else 0, # Set count only if processed
                     error_msg=error_to_report
                 )

        except Reject as r: task_log.error(f"Task rejected: {r.reason}"); raise r
        except ConnectionError as db_update_exc: # Catch final update failure outside pool manager
            task_log.critical("CRITICAL: Failed final DB update!", error=str(db_update_exc))
            raise Reject(f"Final DB update failed for {doc_id}", requeue=False) from db_update_exc
        except Exception as outer_exc: # Catch errors getting pool or other unexpected issues
             task_log.exception("Outer exception in run_async_processing", error=str(outer_exc))
             processing_exception = outer_exc # Ensure exception is captured for retry logic
             final_status = DocumentStatus.ERROR # Mark as error if outer exception occurred
             # We might not have updated the DB in this case, state could be PROCESSING
             error_to_report = f"Outer task error: {type(outer_exc).__name__}"

        # --- Retry / Reject Logic ---
        if final_status == DocumentStatus.ERROR and processing_exception:
             # Non-retryable errors: ValueError, TimeoutError, Reject, ConnectionError, specific RuntimeErrors if needed
             is_retryable = not isinstance(processing_exception, (ValueError, asyncio.TimeoutError, Reject, ConnectionError))
             # Potentially add specific RuntimeErrors here if they are known to be non-retryable

             if is_retryable and self.request and self.request.retries < self.max_retries:
                 task_log.warning("Processing failed, attempting retry.", error=str(processing_exception))
                 try:
                     # Use exponential backoff potentially: countdown = self.default_retry_delay * (2 ** attempt)
                     raise self.retry(exc=processing_exception, countdown=self.default_retry_delay * attempt)
                 except MaxRetriesExceededError:
                     task_log.error("Max retries exceeded.", error=str(processing_exception))
                     raise Reject(f"Max retries exceeded for {doc_id}", requeue=False) from processing_exception
                 except Exception as retry_exc:
                     task_log.exception("Exception during retry mechanism", error=str(retry_exc))
                     raise Reject(f"Retry mechanism failed for {doc_id}", requeue=False) from retry_exc
             else:
                 task_log.error("Processing failed with non-retryable error or max retries reached.", error=str(processing_exception), type=type(processing_exception).__name__)
                 raise Reject(f"Non-retryable error/max retries for {doc_id}: {error_to_report}", requeue=False) from processing_exception

        elif final_status == DocumentStatus.PROCESSED:
              task_log.info("Processing completed successfully.")
              return {"status": "processed", "document_id": doc_id, "chunks_processed": final_chunk_count}
        else: # Should not happen if logic is correct
             task_log.error("Task ended in unexpected state", final_status=final_status)
             raise Reject(f"Unexpected final state {final_status} for {doc_id}", requeue=False)

    def run(self, *args, **kwargs):
        """Synchronous wrapper to run the async processing logic."""
        task_log = log.bind(task_id=self.request.id if self.request else 'sync_run', task_name=self.name)
        task_log.info("Task received", args=args, kwargs=list(kwargs.keys()))
        try:
            # Use asyncio.run() to execute the async part
            return asyncio.run(self.run_async_processing(*args, **kwargs))
        except Reject as r:
             task_log.error(f"Task rejected: {r.reason}", exc_info=False)
             # Ensure Reject is propagated correctly for Celery state
             raise r
        except Ignore as i:
             task_log.warning("Task ignored.")
             raise i # Propagate Ignore
        except Exception as e:
             task_log.exception("Task failed: Unhandled exception in run wrapper", error=str(e))
             # This will cause the task to be marked as FAILED by Celery
             raise e # Re-raise the original exception

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        failure_log = log.bind(task_id=task_id, task_name=self.name, status="FAILED")
        reason = getattr(exc, 'reason', str(exc)) # Get reason from Reject if available
        failure_log.error(
            "Celery task final failure", args=args, kwargs=kwargs,
            error_type=type(exc).__name__, error=reason,
            traceback=str(einfo.traceback) if einfo else "No traceback"
        )
        # DO NOT try to update DB status here, it might have failed already or cause loops

    def on_success(self, retval, task_id, args, kwargs):
        success_log = log.bind(task_id=task_id, task_name=self.name, status="SUCCESS")
        success_log.info("Celery task completed successfully", args=args, kwargs=kwargs, retval=retval)

# Register the custom task class with Celery
process_document_haystack_task = celery_app.register_task(ProcessDocumentTask())