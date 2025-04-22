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

# --- Haystack Dependencies ---
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
from haystack.utils import ComponentDevice # Import for device selection

# --- Milvus Dependencies ---
from milvus_haystack import MilvusDocumentStore

# --- FastEmbed Dependencies ---
from haystack_integrations.components.embedders.fastembed import (
    FastembedDocumentEmbedder,
)

# --- Custom Application Imports ---
from app.core.config import settings # Settings now includes USE_GPU, FASTEMBED_MODEL
from app.db import postgres_client as db_client
from app.models.domain import DocumentStatus
from app.services.minio_client import MinioClient, MinioError
from app.tasks.celery_app import celery_app

# Initialize logger
log = structlog.get_logger(__name__)

# Timeout for the entire processing flow within the task
TIMEOUT_SECONDS = 600 # 10 minutes, adjust as needed

# --- Milvus Initialization ---
def _initialize_milvus_store() -> MilvusDocumentStore:
    log.debug("Initializing MilvusDocumentStore...")
    try:
        store = MilvusDocumentStore(
            connection_args={"uri": settings.MILVUS_URI},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            embedding_dim=settings.EMBEDDING_DIMENSION,
            consistency_level="Strong", # Ensure strong consistency for reliable reads after writes
        )
        log.info("MilvusDocumentStore initialized successfully.",
                 uri=settings.MILVUS_URI, collection=settings.MILVUS_COLLECTION_NAME,
                 embedding_dim=settings.EMBEDDING_DIMENSION)
        return store
    except TypeError as te:
        log.exception("MilvusDocumentStore init TypeError (check embedding_dim)", error=str(te), exc_info=True)
        raise RuntimeError(f"Milvus TypeError (check embedding_dim={settings.EMBEDDING_DIMENSION}): {te}") from te
    except Exception as e:
        log.exception("Failed to initialize MilvusDocumentStore", error=str(e), exc_info=True)
        raise RuntimeError(f"Milvus Store Initialization Error: {e}") from e

# --- Haystack Component Initialization ---
def _initialize_haystack_components(
    document_store: MilvusDocumentStore
) -> Tuple[DocumentSplitter, FastembedDocumentEmbedder, DocumentWriter]:
    log.debug("Initializing Haystack components (Splitter, Embedder, Writer)...")
    try:
        splitter = DocumentSplitter(
            split_by=settings.SPLITTER_SPLIT_BY,
            split_length=settings.SPLITTER_CHUNK_SIZE,
            split_overlap=settings.SPLITTER_CHUNK_OVERLAP
        )
        log.info("DocumentSplitter initialized", split_by=settings.SPLITTER_SPLIT_BY, length=settings.SPLITTER_CHUNK_SIZE, overlap=settings.SPLITTER_CHUNK_OVERLAP)

        # Determine device for FastEmbed
        if settings.USE_GPU:
            try:
                # Try to select the first GPU
                device = ComponentDevice.from_str("cuda:0")
                log.info("GPU configured AND selected for FastEmbed.", device_str="cuda:0")
            except Exception as gpu_err:
                log.warning("GPU configured but FAILED to select, falling back to CPU.", error=str(gpu_err), setting_use_gpu=settings.USE_GPU)
                device = ComponentDevice.from_str("cpu")
        else:
            device = ComponentDevice.from_str("cpu")
            log.info("CPU selected for FastEmbed (USE_GPU is false).", setting_use_gpu=settings.USE_GPU)

        log.info("Initializing FastembedDocumentEmbedder...", model=settings.FASTEMBED_MODEL, device=str(device))
        embedder = FastembedDocumentEmbedder(
            model=settings.FASTEMBED_MODEL,
            device=device, # Pass the selected device
            batch_size=256, # Adjust batch size as needed
            # Set parallel based on device type for potential optimization
            parallel=0 if device.type == "cpu" else None
        )

        log.info("Warming up FastEmbed model...")
        embedder.warm_up()
        log.info("FastEmbed model warmed up successfully.")

        writer = DocumentWriter(
            document_store=document_store,
            policy=DuplicatePolicy.OVERWRITE, # Overwrite if chunks with same ID exist
            batch_size=512 # Adjust batch size based on memory/performance
        )
        log.info("DocumentWriter initialized", policy="OVERWRITE", batch_size=512)

        log.info("Haystack components initialized successfully.")
        return splitter, embedder, writer
    except Exception as e:
        log.exception("Failed to initialize Haystack components", error=str(e), exc_info=True)
        raise RuntimeError(f"Haystack Component Initialization Error: {e}") from e

# --- File Type to Converter Mapping ---
def get_converter(content_type: str) -> Type[Any]:
    log.debug("Selecting converter", content_type=content_type)
    if content_type == "application/pdf": return PyPDFToDocument
    elif content_type in ["application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/msword"]: return DOCXToDocument
    elif content_type == "text/plain": return TextFileToDocument
    elif content_type == "text/markdown": return MarkdownToDocument
    elif content_type == "text/html": return HTMLToDocument
    else: raise ValueError(f"Unsupported content type: {content_type}")

# --- Celery Task Setup & Logging Configuration ---
# Ensure logging is configured early
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
formatter = structlog.stdlib.ProcessorFormatter(
    processor=structlog.processors.JSONRenderer(),
)
handler = logging.StreamHandler()
handler.setFormatter(formatter)
root_logger = logging.getLogger()
# Prevent adding handler multiple times in worker restarts
if not root_logger.handlers:
    root_logger.addHandler(handler)
    try:
        root_logger.setLevel(settings.LOG_LEVEL.upper())
    except ValueError:
        root_logger.setLevel("INFO")
        log.warning("Invalid LOG_LEVEL, defaulting to INFO.")

# --- Database Retry Strategy ---
db_retry_strategy = retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(2),
    retry=retry_if_exception_type((asyncpg.exceptions.PostgresConnectionError, TimeoutError, OSError)),
    before_sleep=before_sleep_log(log, logging.WARNING) # Use standard logging level
)

# --- Database Session Manager ---
@asynccontextmanager
async def db_session_manager():
    """Provides a pooled connection for the duration of the 'with' block."""
    pool = None
    conn = None
    try:
        pool = await db_client.get_db_pool()
        # Acquire connection within the context manager
        conn = await pool.acquire()
        log.debug("DB connection acquired from pool.")
        yield conn # Provide the connection to the 'with' block
    except Exception as e:
        log.error("Failed to get DB pool or acquire connection", error=str(e))
        raise # Re-raise the exception
    finally:
        if conn and pool:
            # Release connection back to the pool
            await pool.release(conn)
            log.debug("DB connection released back to pool.")
        log.debug("DB session context exited.")

# --- Main Asynchronous Processing Flow ---
async def async_process_flow(
    *, # Enforce keyword arguments
    document_id: str,
    company_id: str,
    filename: str,
    content_type: str,
    task_id: str,
    attempt: int
) -> int:
    """
    Downloads file, runs Haystack pipeline (convert, split, embed, write),
    and returns the number of chunks written.
    Raises specific exceptions on failure.
    """
    flow_log = log.bind(
        document_id=document_id,
        company_id=company_id,
        task_id=task_id,
        attempt=attempt,
        filename=filename,
        content_type=content_type
    )
    flow_log.info("Starting asynchronous processing flow")
    minio_client = MinioClient()
    object_name = f"{company_id}/{document_id}/{filename}"
    temp_file_path = None
    total_chunks_written = 0

    try:
        flow_log.info("Downloading file from MinIO", object_name=object_name)
        # Use a temporary directory for downloaded file
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_file_path = os.path.join(temp_dir, filename)
            await minio_client.download_file(object_name, temp_file_path)
            flow_log.info("File downloaded successfully", temp_path=temp_file_path)

            # Initialize components - run blocking calls in executor
            loop = asyncio.get_running_loop()
            store = await loop.run_in_executor(None, _initialize_milvus_store)
            splitter, embedder, writer = await loop.run_in_executor(None, _initialize_haystack_components, store)

            # Get the appropriate converter class
            ConverterClass = get_converter(content_type)
            converter = ConverterClass() # Instantiate the converter

            flow_log.info("Starting Haystack pipeline execution (converter, splitter, embedder, writer)...")

            # Define the synchronous pipeline logic to run in executor
            def run_haystack_pipeline_sync(local_temp_file_path):
                # Bind log context within the sync function if needed, or use flow_log
                pipeline_log = flow_log # Can reuse the outer log context
                pipeline_log.debug("Executing conversion...")
                # --- Conversion ---
                # Correctly call run with list of sources
                conversion_result = converter.run(sources=[local_temp_file_path])
                docs: List[Document] = conversion_result["documents"]
                pipeline_log.debug("Conversion complete", num_docs_converted=len(docs))
                if not docs:
                    pipeline_log.warning("Converter produced 0 documents. Skipping rest of pipeline.")
                    return 0 # Return 0 chunks written

                # --- Add Metadata ---
                pipeline_log.debug("Adding standard metadata to documents...")
                for doc in docs:
                    if doc.meta is None: doc.meta = {} # Ensure meta exists
                    # Add essential metadata for filtering and identification
                    doc.meta["company_id"] = company_id
                    doc.meta["document_id"] = document_id
                    doc.meta["file_name"] = filename
                    doc.meta["file_type"] = content_type
                    # LLM_FLAG: CUSTOM_METADATA - Consider adding custom metadata from upload here if needed

                # --- Splitting ---
                pipeline_log.debug("Executing splitting...")
                split_result = splitter.run(documents=docs)
                split_docs: List[Document] = split_result["documents"]
                pipeline_log.debug("Splitting complete", num_chunks=len(split_docs))
                if not split_docs:
                    pipeline_log.warning("Splitter produced 0 chunks. Skipping embedding/writing.")
                    return 0

                # --- Embedding ---
                pipeline_log.debug("Executing embedding...")
                embed_result = embedder.run(documents=split_docs)
                embedded_docs: List[Document] = embed_result["documents"]
                pipeline_log.debug("Embedding complete.")
                if not embedded_docs:
                    # This would be unusual if splitting worked but worth checking
                    pipeline_log.warning("Embedder produced 0 embedded documents.")
                    return 0

                # --- Writing ---
                pipeline_log.debug("Executing writing to Milvus...")
                write_result = writer.run(documents=embedded_docs)
                written_count = write_result["documents_written"]
                pipeline_log.info("Writing complete.", documents_written=written_count)
                return written_count

            # Run the synchronous Haystack pipeline in an executor thread
            chunks_written = await loop.run_in_executor(None, run_haystack_pipeline_sync, temp_file_path)
            total_chunks_written = chunks_written
            flow_log.info("Haystack pipeline execution finished.", chunks_written=total_chunks_written)

            return total_chunks_written

    except MinioError as me:
        flow_log.error("MinIO Error during download", object_name=object_name, error=str(me))
        # Raise a more specific error or wrap it
        raise RuntimeError(f"MinIO download failed for {object_name}: {me}") from me
    except ValueError as ve: # Catch unsupported content type or other value errors
         flow_log.error("Value Error (likely unsupported type or bad data)", error=str(ve))
         # Propagate ValueError to indicate bad input, likely non-retryable
         raise ve
    except RuntimeError as rt_err: # Catch Milvus/Haystack init errors
         flow_log.error("Runtime Error during processing (component init or pipeline)", error=str(rt_err))
         # Propagate runtime errors, potentially retryable depending on cause
         raise rt_err
    except Exception as e:
        # Catch any other unexpected error
        flow_log.exception("Unexpected error during processing flow", error=str(e))
        raise RuntimeError(f"Unexpected flow error: {e}") from e
    # No finally block needed for temp_dir, 'with' handles cleanup

# --- Celery Task Definition ---
class ProcessDocumentTask(Task):
    name = "app.tasks.process_document.ProcessDocumentTask"
    # Configure retries for potentially transient issues (e.g., network, temporary Milvus unavailability)
    max_retries = 3
    default_retry_delay = 60 # seconds

    # LLM_FLAG: IGNORE_CONSTRUCTOR - Standard Celery Task setup
    def __init__(self):
        super().__init__()
        # Bind logger context early if needed, or within run/methods
        self.task_log = log.bind(task_name=self.name)
        self.task_log.info("ProcessDocumentTask initialized.")

    # LLM_FLAG: SENSITIVE_DB_UPDATE - Method for updating status with retries
    async def _update_status_with_retry(
        self,
        conn: asyncpg.Connection, # Expect an acquired connection
        doc_id: str,
        status: DocumentStatus,
        chunk_count: Optional[int] = None,
        error_msg: Optional[str] = None
    ):
        """Updates document status in DB using the provided connection and retry strategy."""
        update_log = self.task_log.bind(document_id=doc_id, target_status=status.value)
        try:
            # Apply retry strategy directly to the DB client function
            await db_retry_strategy(db_client.update_document_status)(
                conn=conn, # Pass the acquired connection
                document_id=uuid.UUID(doc_id),
                status=status, # Pass Enum
                chunk_count=chunk_count,
                error_message=error_msg # Pass error message
            )
            update_log.info("Document status updated successfully in DB.")
        except Exception as e:
            # If retries fail, log critically and raise a specific error
            update_log.critical("CRITICAL: Failed final DB status update after retries!", error=str(e), exc_info=True)
            # Raising ConnectionError signifies a persistent issue the task might not recover from
            raise ConnectionError(f"Persistent DB error updating status for {doc_id}") from e

    # LLM_FLAG: CELERY_TASK_RUNNER - Main async task execution logic
    # <<< CORRECTION: Removed nested asyncio.run(), directly await async_process_flow >>>
    async def run(self, *args, **kwargs):
        """Asynchronous Celery task entry point."""
        # Extract arguments safely
        document_id = kwargs.get('document_id')
        company_id = kwargs.get('company_id')
        filename = kwargs.get('filename')
        content_type = kwargs.get('content_type')

        # Get task metadata safely
        task_id = self.request.id if self.request else 'N/A'
        attempt = (self.request.retries + 1) if self.request else 1

        task_log = self.task_log.bind(
            document_id=document_id, task_id=task_id, attempt=attempt,
            company_id=company_id, filename=filename
        )

        task_log.info("Task received", args_repr=repr(args), kwargs_keys=list(kwargs.keys()))

        # --- Input Validation ---
        if not all([document_id, company_id, filename, content_type]):
             missing = [k for k, v in kwargs.items() if k in ['document_id', 'company_id', 'filename', 'content_type'] and not v]
             task_log.error("Task received with missing essential arguments.", missing_args=missing)
             # Reject immediately, non-retryable if core info is missing
             raise Reject(f"Missing essential arguments: {missing}", requeue=False)

        # --- Processing Logic ---
        final_status = DocumentStatus.ERROR # Default to error
        final_chunk_count = 0
        error_to_report = "Unknown processing error"
        processing_exception: Optional[Exception] = None

        try:
            # Use async context manager for DB connection
            async with db_session_manager() as conn:
                 if not conn:
                     task_log.critical("Failed to get DB connection for task execution.")
                     # Cannot proceed without DB, reject permanently
                     raise Reject("DB connection unavailable for task", requeue=False)

                 try:
                    # 1. Set status to PROCESSING
                    task_log.info("Setting document status to 'processing'")
                    await self._update_status_with_retry(
                        conn, document_id, DocumentStatus.PROCESSING, error_msg=None # Clear previous error
                    )

                    # 2. Execute the main processing flow
                    task_log.info("Executing main async_process_flow with timeout", timeout=TIMEOUT_SECONDS)
                    # --- Directly await the async function ---
                    final_chunk_count = await asyncio.wait_for(
                        async_process_flow(
                            document_id=document_id,
                            company_id=company_id,
                            filename=filename,
                            content_type=content_type,
                            task_id=task_id,
                            attempt=attempt
                        ),
                        timeout=TIMEOUT_SECONDS
                    )
                    # --- End Direct Await ---
                    final_status = DocumentStatus.PROCESSED
                    error_to_report = None # Clear error on success
                    task_log.info("Async process flow completed successfully.", chunks_processed=final_chunk_count)

                 # --- Specific Exception Handling for Processing ---
                 except asyncio.TimeoutError as e:
                     task_log.error("Processing timed out", timeout=TIMEOUT_SECONDS)
                     error_to_report = f"Processing timed out after {TIMEOUT_SECONDS}s."
                     final_status = DocumentStatus.ERROR # Ensure status is ERROR
                     processing_exception = e
                 except ValueError as e: # Non-retryable input/config errors
                      task_log.error("Processing failed: Value error (check input/config)", error=str(e))
                      error_to_report = f"Input/Config Error: {e}"
                      final_status = DocumentStatus.ERROR
                      processing_exception = e
                 except RuntimeError as e: # Potentially retryable runtime errors
                      task_log.error("Processing failed: Runtime error", error=str(e), exc_info=False) # Log less verbose for runtime
                      error_to_report = f"Runtime Error: {e}"
                      final_status = DocumentStatus.ERROR
                      processing_exception = e
                 except ConnectionError as e: # Catch critical DB update failures from _update_status_with_retry
                      task_log.critical("Persistent DB connection error during status update", error=str(e))
                      # This indicates a potentially persistent DB issue, reject permanently
                      raise Reject(f"Persistent DB error for {document_id}: {e}", requeue=False) from e
                 except Exception as e: # Catch-all for unexpected errors during processing
                     task_log.exception("Unexpected exception during processing flow.", error=str(e))
                     error_to_report = f"Unexpected error: {type(e).__name__}"
                     final_status = DocumentStatus.ERROR
                     processing_exception = e

                 # 3. Final status update attempt (always happens unless DB connection failed)
                 task_log.info("Attempting final DB status update", status=final_status.value, chunks=final_chunk_count, error=error_to_report)
                 await self._update_status_with_retry(
                     conn, document_id, final_status,
                     # Only set chunk_count if processed successfully
                     chunk_count=final_chunk_count if final_status == DocumentStatus.PROCESSED else 0,
                     error_msg=error_to_report
                 )

        except Reject as r:
             task_log.error(f"Task rejected: {r.reason}")
             # Re-raise Reject to ensure Celery handles it correctly (no further processing)
             raise r
        except ConnectionError as db_update_exc: # Catch DB errors from final update outside the inner try
            task_log.critical("CRITICAL: Failed final DB update outside main try (likely connection issue)!", error=str(db_update_exc))
            # Raise Reject if the *final* status update fails critically after processing attempt
            raise Reject(f"Final DB update failed for {document_id}: {db_update_exc}", requeue=False) from db_update_exc
        except Exception as outer_exc: # Catch errors getting DB connection etc.
             task_log.exception("Outer exception caught in async run", error=str(outer_exc))
             processing_exception = outer_exc # Record the exception
             final_status = DocumentStatus.ERROR # Ensure error status
             error_to_report = f"Outer task error: {type(outer_exc).__name__}"
             # Attempt to update status to error one last time if an outer exception occurred
             # This might fail if the pool is the issue, but worth a try.
             try:
                  async with db_session_manager() as final_conn:
                      if final_conn:
                          await self._update_status_with_retry(final_conn, document_id, final_status, chunk_count=0, error_msg=error_to_report)
                      else:
                           task_log.error("Could not get DB connection for final error update after outer exception.")
             except Exception as final_db_err:
                  task_log.critical("Failed to update status to ERROR after outer exception", final_db_error=str(final_db_err))
             # Regardless of final update success, reject the task for the outer error
             raise Reject(f"Outer exception for {document_id}: {error_to_report}", requeue=False) from outer_exc

        # --- Retry / Final Outcome Logic ---
        if final_status == DocumentStatus.ERROR and processing_exception:
             # Determine if the error is potentially temporary/retryable
             # ValueError is usually configuration/input -> not retryable
             # TimeoutError might be temporary load -> maybe retryable
             # RuntimeError could be temporary (network) or permanent (bad data) -> retry
             # ConnectionError during update is handled above by Reject
             is_retryable = isinstance(processing_exception, (RuntimeError, asyncio.TimeoutError, httpx.RequestError, MinioError))

             # Check if retries are possible
             if is_retryable and self.request and self.request.retries < self.max_retries:
                 task_log.warning("Processing failed, attempting retry.", error=str(processing_exception))
                 try:
                     # Raise the special Retry exception to trigger Celery's retry mechanism
                     # Note: self.retry raises the exception, it doesn't return
                     raise self.retry(exc=processing_exception, countdown=int(self.default_retry_delay * (attempt**1.5))) # Exponential backoff
                 except MaxRetriesExceededError:
                     task_log.error("Max retries exceeded after failure.", error=str(processing_exception))
                     # Raise Reject after max retries
                     raise Reject(f"Max retries exceeded for {document_id}", requeue=False) from processing_exception
                 # Catching Reject is good practice in case retry is disabled or fails internally
                 except Reject as r:
                     task_log.error("Retry attempt resulted in Reject.", reason=str(r))
                     raise r
                 except Exception as retry_exc: # Catch unexpected errors during the retry call itself
                     task_log.exception("Exception during Celery retry mechanism", error=str(retry_exc))
                     raise Reject(f"Retry mechanism failed for {document_id}", requeue=False) from retry_exc
             else: # Non-retryable error or max retries already reached
                 error_reason = "Non-retryable error" if not is_retryable else "Max retries exceeded"
                 task_log.error(f"Processing failed permanently ({error_reason}).", error=str(processing_exception), type=type(processing_exception).__name__)
                 raise Reject(f"{error_reason} for {document_id}: {error_to_report}", requeue=False) from processing_exception

        elif final_status == DocumentStatus.PROCESSED:
              task_log.info("Processing completed successfully.")
              # Return a JSON-serializable dictionary for the result backend
              # This is what was causing the original error - ensure it's simple data
              return {"status": "processed", "document_id": document_id, "chunks_processed": final_chunk_count}
        else:
             # This state should ideally not be reached if status is always updated
             task_log.error("Task ended in unexpected final state without explicit error/success", final_status=final_status.value)
             raise Reject(f"Unexpected final state {final_status.value} for {document_id}", requeue=False)


    # LLM_FLAG: STANDARD_CELERY_CALLBACK - Keep standard failure handler
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Called by Celery when the task fails definitively (after retries or Reject)."""
        failure_log = log.bind(task_id=task_id, task_name=self.name, status="FAILED")
        reason = getattr(exc, 'reason', str(exc)) # Get reason from Reject if available
        failure_log.error(
            "Celery task final failure",
            args_repr=repr(args),
            kwargs_keys=list(kwargs.keys()),
            error_type=type(exc).__name__,
            error=reason,
            traceback=str(einfo.traceback) if einfo else "No traceback available"
        )
        # NOTE: It's generally complex and potentially risky to update DB status here,
        # as the failure might be *because* of DB issues. The main run logic attempts final updates.

    # LLM_FLAG: STANDARD_CELERY_CALLBACK - Keep standard success handler
    def on_success(self, retval, task_id, args, kwargs):
        """Called by Celery when the task succeeds."""
        success_log = log.bind(task_id=task_id, task_name=self.name, status="SUCCESS")
        success_log.info(
            "Celery task completed successfully",
            args_repr=repr(args),
            kwargs_keys=list(kwargs.keys()),
            retval=retval # Log the returned value
        )
# <<< END CORRECTION >>>

# Register the custom task class with Celery
# This now registers the class where 'run' is async def
process_document_haystack_task = celery_app.register_task(ProcessDocumentTask())