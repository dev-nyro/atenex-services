# ingest-service/app/tasks/process_document.py
import asyncio
import os
import tempfile
import uuid
from typing import Optional, Dict, Any, List, Tuple, Type
from contextlib import asynccontextmanager
import structlog
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type, before_sleep_log
from celery import Celery, Task
from celery.exceptions import Ignore, Reject, MaxRetriesExceededError
import httpx
import asyncpg

# Haystack Imports
from haystack.dataclasses import Document
from haystack.document_stores.types import DuplicatePolicy
from haystack.components.converters import (
    PyPDFToDocument,  # Assuming tika is not strictly needed based on current logs
    MarkdownToDocument,
    HTMLToDocument,
    TextFileToDocument,
    # Add others if needed and installed, e.g., from haystack.components.converters.tika import TikaDocumentConverter
)
# Requires 'pip install haystack-ai[ocr]' for image-to-text or OCR PDFs
# from haystack.components.converters.ocr import OCRDocumentConverter
# Requires 'pip install haystack-ai[docx]'
from haystack.components.converters.docx import DOCXToDocument
from haystack.components.preprocessors import DocumentSplitter
from haystack.components.embedders import OpenAIDocumentEmbedder
from haystack.components.writers import DocumentWriter

# Milvus specific integration
from milvus_haystack import MilvusDocumentStore # Correct import for Haystack 2.x

# Custom imports
from app.core.config import settings
from app.db import postgres_client as db_client
from app.models.domain import DocumentStatus
from app.services.minio_client import MinioClient, MinioError

# Initialize logger
log = structlog.get_logger(__name__)

# Timeout for the entire processing flow within the task
TIMEOUT_SECONDS = 600 # 10 minutes, adjust as needed

# --- Milvus Initialization ---
# Wrap synchronous Haystack init in a function for run_in_executor
def _initialize_milvus_store() -> MilvusDocumentStore:
    """
    Synchronously initializes and returns a MilvusDocumentStore instance.
    Handles potential configuration errors during initialization.
    """
    log.debug("Initializing MilvusDocumentStore...")
    try:
        store = MilvusDocumentStore(
            connection_args={"uri": settings.MILVUS_URI},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            # embedding_dim=settings.EMBEDDING_DIMENSION, # <--- REMOVED: Incorrect argument for milvus-haystack 2.x
            consistency_level="Strong", # Example setting, adjust if needed
            # Add other relevant Milvus connection args if required
            # drop_old=False, # Usually False for workers, maybe True for testing
        )
        # Optional: Add a quick check to ensure the connection works if the store provides one
        # e.g., if store.count_documents() doesn't fail immediately (though might be slow)
        log.info("MilvusDocumentStore initialized successfully.",
                 uri=settings.MILVUS_URI, collection=settings.MILVUS_COLLECTION_NAME)
        return store
    except TypeError as te:
        # Catching the specific error seen in logs, though it should be fixed now.
        log.exception("MilvusDocumentStore init TypeError", error=str(te), exc_info=True)
        # Raise a specific runtime error to be caught in the main task flow
        raise RuntimeError(f"Milvus TypeError (check arguments like embedding_dim): {te}") from te
    except Exception as e:
        log.exception("Failed to initialize MilvusDocumentStore", error=str(e), exc_info=True)
        # Raise a generic runtime error for other potential init issues
        raise RuntimeError(f"Milvus Store Initialization Error: {e}") from e

# --- Haystack Component Initialization ---
def _initialize_haystack_components(
    document_store: MilvusDocumentStore
) -> Tuple[DocumentSplitter, OpenAIDocumentEmbedder, DocumentWriter]:
    """Synchronously initializes necessary Haystack processing components."""
    log.debug("Initializing Haystack components (Splitter, Embedder, Writer)...")
    try:
        # Document Splitter
        splitter = DocumentSplitter(
            split_by="word", # or "sentence", "passage"
            split_length=settings.SPLITTER_CHUNK_SIZE,
            split_overlap=settings.SPLITTER_CHUNK_OVERLAP
        )

        # Document Embedder (OpenAI)
        embedder = OpenAIDocumentEmbedder(
            api_key=settings.OPENAI_API_KEY,
            model=settings.OPENAI_EMBEDDING_MODEL,
            # Consider adding dimensions_to_truncate if needed by model/Milvus
            # dimensions=settings.EMBEDDING_DIMENSION, # May not be needed if model provides it
            # meta_fields_to_embed=["company_id", "document_id"] # Example if needed
            # batch_size=... # Configure batch size
        )

        # Document Writer (using the initialized Milvus store)
        writer = DocumentWriter(
            document_store=document_store,
            policy=DuplicatePolicy.OVERWRITE # Or FAIL, SKIP
        )
        log.info("Haystack components initialized successfully.")
        return splitter, embedder, writer
    except Exception as e:
        log.exception("Failed to initialize Haystack components", error=str(e), exc_info=True)
        raise RuntimeError(f"Haystack Component Initialization Error: {e}") from e

# --- File Type to Converter Mapping ---
def get_converter(content_type: str) -> Type:
    """Returns the appropriate Haystack Converter based on content type."""
    log.debug("Selecting converter", content_type=content_type)
    if content_type == "application/pdf":
        # PyPDFToDocument is generally preferred unless OCR is needed
        # return OCRDocumentConverter() # Use if OCR is needed for image-based PDFs
        return PyPDFToDocument
    elif content_type in ["application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/msword"]:
        # Requires 'pip install python-docx'
        return DOCXToDocument
    elif content_type == "text/plain":
        return TextFileToDocument
    elif content_type == "text/markdown":
        return MarkdownToDocument
    elif content_type == "text/html":
        return HTMLToDocument
    # Add more converters as needed
    # elif content_type == "application/vnd.oasis.opendocument.text": # ODT
    #    from haystack.components.converters.odt import ODTToDocument
    #    return ODTToDocument
    else:
        log.warning("Unsupported content type for conversion", content_type=content_type)
        raise ValueError(f"Unsupported content type: {content_type}")

# --- Celery Task Setup ---
celery_app = Celery(
    'ingest_tasks',
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND
)

celery_app.conf.update(
    task_serializer='json',
    result_serializer='json',
    accept_content=['json'],
    task_track_started=True,
    # task_acks_late=True, # Consider enabling for more robust error handling
    # worker_prefetch_multiplier=1, # Process one task at a time if memory/CPU intensive
    task_time_limit=TIMEOUT_SECONDS + 60, # Task hard timeout slightly > flow timeout
    task_soft_time_limit=TIMEOUT_SECONDS, # Task soft timeout = flow timeout
)

# Setup logging for Celery task context
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        # Add process/thread info if needed
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
if not root_logger.handlers: # Avoid adding handlers multiple times if reloaded
    root_logger.addHandler(handler)
    root_logger.setLevel(settings.LOG_LEVEL.upper())


# Define retry strategy for database operations
db_retry_strategy = retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(2),
    retry=retry_if_exception_type((asyncpg.exceptions.PostgresConnectionError, TimeoutError, OSError)),
    before_sleep=before_sleep_log(log, logging.WARNING)
)

@asynccontextmanager
async def db_session_manager():
    """Provides a managed database connection pool session."""
    pool = None
    try:
        pool = await db_client.get_db_pool() # Reuse existing pool logic
        yield pool # The pool itself can be used to acquire connections
    except Exception as e:
        log.error("Failed to get DB pool for session", error=str(e), exc_info=True)
        raise
    finally:
        # The pool itself is managed globally, don't close it here.
        # Connections acquired from the pool should be released.
        log.debug("DB session context exited.")
        pass

# --- Main Asynchronous Processing Flow ---
async def async_process_flow(
    *, # Enforce keyword arguments
    document_id: str,
    company_id: str,
    filename: str,
    content_type: str,
    task_id: str,
    attempt: int
):
    """The core asynchronous processing logic for a single document."""
    flow_log = log.bind(
        document_id=document_id, company_id=company_id, task_id=task_id,
        attempt=attempt, filename=filename, content_type=content_type
    )
    flow_log.info("Starting asynchronous processing flow")

    # 1. Initialize Minio Client
    minio_client = MinioClient(
        endpoint=settings.MINIO_ENDPOINT,
        access_key=settings.MINIO_ACCESS_KEY,
        secret_key=settings.MINIO_SECRET_KEY,
        bucket_name=settings.MINIO_BUCKET_NAME,
        secure=settings.MINIO_USE_SSL
    )

    # 2. Download file from Minio
    object_name = f"{company_id}/{document_id}/{filename}"
    temp_file_path = None
    try:
        flow_log.info("Downloading file from MinIO", object_name=object_name)
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(filename)[1]) as temp_file:
            temp_file_path = temp_file.name
            await minio_client.download_file(object_name, temp_file_path)
        flow_log.info("File downloaded successfully", temp_path=temp_file_path)
    except MinioError as me:
        flow_log.error("Failed to download file from MinIO", object_name=object_name, error=str(me))
        raise RuntimeError(f"MinIO download failed: {me}") from me # Non-retryable for now
    except Exception as e:
        flow_log.exception("Unexpected error during file download", error=str(e))
        raise RuntimeError(f"Unexpected download error: {e}") from e

    # 3. Initialize Milvus Store (potentially blocking)
    loop = asyncio.get_running_loop()
    try:
        flow_log.info("Initializing Milvus document store...")
        # Run synchronous initialization in a separate thread
        store = await loop.run_in_executor(None, _initialize_milvus_store) # Can raise RuntimeError
        flow_log.info("Milvus document store initialized.")
    except RuntimeError as e:
        flow_log.error("Failed to initialize Milvus store during flow", error=str(e))
        # This is a configuration/infrastructure error, likely not retryable from worker perspective
        raise e # Re-raise the specific RuntimeError
    except Exception as e:
        flow_log.exception("Unexpected error initializing Milvus store", error=str(e))
        raise RuntimeError(f"Unexpected Milvus init error: {e}") from e

    # 4. Initialize other Haystack Components (potentially blocking)
    try:
        flow_log.info("Initializing Haystack processing components...")
        splitter, embedder, writer = await loop.run_in_executor(None, _initialize_haystack_components, store)
        flow_log.info("Haystack processing components initialized.")
    except RuntimeError as e:
        flow_log.error("Failed to initialize Haystack components during flow", error=str(e))
        raise e # Configuration/setup error
    except Exception as e:
        flow_log.exception("Unexpected error initializing Haystack components", error=str(e))
        raise RuntimeError(f"Unexpected Haystack init error: {e}") from e

    # 5. Initialize Converter (potentially blocking, select based on type)
    try:
        flow_log.info("Initializing document converter...")
        ConverterClass = get_converter(content_type) # Can raise ValueError
        # Initialize the converter (assuming simple init or handled by Haystack)
        converter = ConverterClass()
        flow_log.info("Document converter initialized", converter=ConverterClass.__name__)
    except ValueError as ve: # Specific error for unsupported type
        flow_log.error("Unsupported content type", error=str(ve))
        raise ve # Propagate as non-retryable user error
    except Exception as e:
        flow_log.exception("Failed to initialize converter", error=str(e))
        raise RuntimeError(f"Converter Initialization Error: {e}") from e

    # --- Haystack Pipeline Execution (Run in Executor) ---
    total_chunks_written = 0
    try:
        flow_log.info("Starting Haystack pipeline execution (converter, splitter, embedder, writer)...")

        def run_haystack_pipeline_sync():
            nonlocal total_chunks_written
            # This function runs synchronously in the executor thread
            pipeline_log = log.bind(
                document_id=document_id, company_id=company_id, task_id=task_id,
                filename=filename, in_sync_executor=True
            )
            pipeline_log.debug("Executing conversion...")
            conversion_result = converter.run(sources=[temp_file_path])
            docs = conversion_result["documents"]
            pipeline_log.debug("Conversion complete", num_docs_converted=len(docs))
            if not docs:
                pipeline_log.warning("Converter produced no documents.")
                return 0 # No chunks to process

            # Add essential metadata for filtering/identification
            for doc in docs:
                doc.meta["company_id"] = company_id
                doc.meta["document_id"] = document_id
                doc.meta["file_name"] = filename
                doc.meta["file_type"] = content_type
                # Add other meta from upload if available and needed

            pipeline_log.debug("Executing splitting...")
            split_docs = splitter.run(documents=docs)["documents"]
            pipeline_log.debug("Splitting complete", num_chunks=len(split_docs))
            if not split_docs:
                 pipeline_log.warning("Splitter produced no documents (chunks).")
                 return 0

            pipeline_log.debug("Executing embedding...")
            embedded_docs = embedder.run(documents=split_docs)["documents"]
            pipeline_log.debug("Embedding complete.")
            if not embedded_docs:
                 pipeline_log.warning("Embedder produced no documents.")
                 return 0

            pipeline_log.debug("Executing writing to Milvus...")
            # Writer handles potential batching internally
            write_result = writer.run(documents=embedded_docs)
            written_count = write_result["documents_written"]
            pipeline_log.info("Writing complete.", documents_written=written_count)
            total_chunks_written = written_count
            return written_count # Return the count

        # Run the synchronous pipeline function in the executor
        chunks_written = await loop.run_in_executor(None, run_haystack_pipeline_sync)
        flow_log.info("Haystack pipeline execution finished.", chunks_written=chunks_written)

    except Exception as e:
        flow_log.exception("Error during Haystack pipeline execution", error=str(e))
        # Determine if error is potentially retryable (e.g., temporary OpenAI issue)
        # For now, treat most pipeline errors as potentially document-specific failures
        raise RuntimeError(f"Haystack Pipeline Error: {e}") from e
    finally:
        # 6. Clean up temporary file
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
                flow_log.debug("Temporary file deleted", path=temp_file_path)
            except OSError as e:
                flow_log.warning("Failed to delete temporary file", path=temp_file_path, error=str(e))

    return total_chunks_written # Return the final count


# --- Celery Task Definition ---
class ProcessDocumentTask(Task):
    """Custom Celery Task class for document processing."""
    name = "tasks.process_document_haystack"
    # Set maximum retries for the task itself (e.g., for transient broker issues)
    # Max retries for application logic errors are handled internally if needed.
    max_retries = 3 # Example: Retry task publish/infrastructure issues up to 3 times
    default_retry_delay = 60 # Example: Wait 60 seconds between task infrastructure retries

    # Global clients (initialized once per worker process)
    # Note: These might not be ideal if worker concurrency > 1 and clients are not thread-safe
    # Consider initializing them within the task or using context managers if needed.
    # For now, assume they are safe or worker concurrency is 1.
    # _minio_client: Optional[MinioClient] = None
    # _db_pool: Optional[asyncpg.Pool] = None

    def __init__(self):
        super().__init__()
        # Initialize clients if needed here, or better, within the task run/async flow
        # self._minio_client = MinioClient(...)
        self.task_log = log.bind(task_name=self.name)
        self.task_log.info("ProcessDocumentTask initialized.")

    # async def _get_db(self):
    #     # Example of lazy-loading DB pool per task instance (if needed)
    #     if self._db_pool is None or self._db_pool._closed:
    #         self._db_pool = await db_client.get_db_pool()
    #     return self._db_pool

    async def _update_status_with_retry(
        self, pool: asyncpg.Pool, doc_id: str, status: DocumentStatus,
        chunk_count: Optional[int] = None, error_msg: Optional[str] = None
    ):
        """Helper to update document status with retry."""
        update_log = self.task_log.bind(document_id=doc_id, target_status=status.value)
        try:
            await db_retry_strategy(db_client.update_document_status)(
                pool=pool,
                document_id=uuid.UUID(doc_id),
                new_status=status,
                chunk_count=chunk_count,
                error_message=error_msg
            )
            update_log.info("Document status updated successfully in DB.")
        except Exception as e:
            # Log critical failure after retries
            update_log.critical("CRITICAL: Failed final document status update in DB!",
                                error=str(e), chunk_count=chunk_count, error_msg=error_msg,
                                exc_info=True)
            # What to do now? The processing might be done, but status is wrong.
            # Option 1: Raise Reject to potentially dead-letter the task
            # Option 2: Log and move on, hoping a reconciliation job fixes it
            # Option 3: Try one last desperate log?
            # For now, let's raise Reject to signal a persistent DB issue requires attention
            raise Reject(f"Persistent DB error updating status for {doc_id} to {status.value}", requeue=False) from e

    async def run_async_processing(self, *args, **kwargs):
        """Runs the main async processing flow and handles final status updates."""
        doc_id = kwargs['document_id']
        task_id = self.request.id # Get task ID from request context
        attempt = self.request.retries + 1
        task_log = self.task_log.bind(document_id=doc_id, task_id=task_id, attempt=attempt)
        final_status = DocumentStatus.ERROR # Default to error
        final_chunk_count = None
        error_to_report = "Unknown processing error"
        is_retryable_error = False # Assume errors aren't retryable unless specified
        e_non_retry: Optional[Exception] = None # Store the final exception

        async with db_session_manager() as pool: # Ensure DB pool is ready
            try:
                # 1. Set status to 'processing' initially
                task_log.info("Setting document status to 'processing'")
                await self._update_status_with_retry(pool, doc_id, DocumentStatus.PROCESSING, error_msg=None) # Clear previous errors

                # 2. Execute the main processing flow with timeout
                task_log.info("Executing main async_process_flow with timeout", timeout=TIMEOUT_SECONDS)
                final_chunk_count = await asyncio.wait_for(
                    async_process_flow(task_id=task_id, attempt=attempt, **kwargs),
                    timeout=TIMEOUT_SECONDS
                )
                final_status = DocumentStatus.PROCESSED
                error_to_report = None # Success!
                task_log.info("Async process flow completed successfully.", chunks_processed=final_chunk_count)

            except asyncio.TimeoutError:
                task_log.error("Processing timed out", timeout=TIMEOUT_SECONDS)
                error_to_report = f"Processing timed out after {TIMEOUT_SECONDS} seconds."
                final_status = DocumentStatus.ERROR
                # Timeout might be due to temporary load, consider if retryable
                # is_retryable_error = True # Example if you want to retry timeouts
                e_non_retry = TimeoutError(error_to_report)

            except ValueError as ve: # Specific user errors (e.g., unsupported type)
                 task_log.error("Processing failed due to value error (e.g., unsupported file type)", error=str(ve))
                 error_to_report = f"Unsupported file type or invalid input: {ve}"
                 final_status = DocumentStatus.ERROR
                 is_retryable_error = False # User error, don't retry
                 e_non_retry = ve

            except RuntimeError as rte: # Errors raised explicitly from init/pipeline steps
                 task_log.error(f"Processing failed permanently: {rte}", exc_info=False) # Log traceback where raised
                 error_to_report = f"Error config./código Milvus o Haystack ({type(rte).__name__}). Contacte soporte." # User-friendly-ish
                 # Check if it's the Milvus init error specifically
                 if "Milvus TypeError" in str(rte) or "Milvus Store Initialization Error" in str(rte):
                    error_to_report = "Error config./código Milvus (RuntimeError). Contacte soporte."
                 elif "Haystack Component Initialization Error" in str(rte) or "Haystack Pipeline Error" in str(rte):
                    error_to_report = "Error interno Haystack (RuntimeError). Contacte soporte."
                 elif "Converter Initialization Error" in str(rte):
                     error_to_report = "Error interno Conversor (RuntimeError). Contacte soporte."
                 elif "MinIO download failed" in str(rte):
                     error_to_report = "Error descargando archivo (MinIO). Verifique conexión/permisos."
                 final_status = DocumentStatus.ERROR
                 is_retryable_error = False # Config/infra/code errors usually aren't task-retryable
                 e_non_retry = rte

            except Exception as e: # Catch-all for unexpected errors
                task_log.exception("Unexpected exception during processing flow.", error=str(e))
                error_to_report = f"Error inesperado ({type(e).__name__}). Contacte soporte."
                final_status = DocumentStatus.ERROR
                # Decide if truly unexpected errors warrant a retry
                # is_retryable_error = True # Maybe retry once for unexpected?
                e_non_retry = e

            # 3. Update final status in DB
            task_log.info("Attempting to update final document status in DB", status=final_status.value, chunks=final_chunk_count, error=error_to_report)
            try:
                await self._update_status_with_retry(
                    pool, doc_id, final_status,
                    chunk_count=final_chunk_count if final_status == DocumentStatus.PROCESSED else None, # Only set count on success
                    error_msg=error_to_report
                )
            except Reject as r: # Catch Reject from _update_status_with_retry
                # DB update failed persistently. The task state is now inconsistent.
                # Log critically and re-raise Reject to potentially DLQ.
                task_log.critical("CRITICAL: Failed to update final document status in DB after processing attempt!", target_status=final_status.value, error_msg=error_to_report)
                raise r # Propagate Reject
            except Exception as db_update_exc:
                 # Should ideally be caught by Reject, but handle just in case
                 task_log.critical("CRITICAL: Unhandled exception during final DB status update!", error=str(db_update_exc), target_status=final_status.value, exc_info=True)
                 # Raise Reject here too, as the state is inconsistent
                 raise Reject(f"Unhandled DB error updating status for {doc_id}", requeue=False) from db_update_exc

            # 4. Handle retries or final failure for the task itself
            if final_status == DocumentStatus.ERROR:
                if is_retryable_error:
                    task_log.warning("Processing failed with a retryable error, attempting task retry.", error=error_to_report)
                    try:
                        # Celery's retry mechanism
                        self.retry(exc=e_non_retry if e_non_retry else RuntimeError(error_to_report), countdown=60 * attempt) # Exponential backoff basic
                    except MaxRetriesExceededError:
                        task_log.error("Max retries exceeded for task.", error=error_to_report)
                        # Give up, the status is already 'error' in DB.
                        raise Ignore() # Prevent Celery from logging it as failure again
                    except Reject as r: # If retry itself causes Reject (e.g. broker issue)
                         task_log.error("Task rejected during retry attempt.", reason=str(r))
                         raise r # Propagate the rejection
                else:
                    # Non-retryable error, status is 'error', log and raise Ignore or the original exception
                    task_log.error("Processing failed with non-retryable error.", error=error_to_report, exception_type=type(e_non_retry).__name__)
                    # Raise the original exception to mark the task as failed in Celery,
                    # allowing result backend to store the error type.
                    if e_non_retry:
                        raise e_non_retry
                    else:
                        # Should not happen if error_to_report is set, but safety fallback
                        raise RuntimeError(error_to_report or "Unknown non-retryable processing error")

            elif final_status == DocumentStatus.PROCESSED:
                 task_log.info("Processing completed successfully for document.")
                 # Task succeeded
                 return {"status": "processed", "document_id": doc_id, "chunks_processed": final_chunk_count}

    def run(self, *args, **kwargs):
        """Synchronous wrapper to run the async processing logic."""
        self.task_log.info("Task received", args=args, kwargs=list(kwargs.keys()))
        try:
            # Run the async function using asyncio.run()
            # This handles the event loop management for the task.
            return asyncio.run(self.run_async_processing(*args, **kwargs))
        except Reject as r:
             # Log and re-raise Reject to ensure Celery handles it (e.g., DLQ)
             self.task_log.error(f"Task rejected due to persistent DB error: {r.reason}", exc_info=False)
             raise r
        except Ignore:
             # Log that we are ignoring after max retries or non-retryable error
             self.task_log.warning("Task is being ignored (e.g., max retries exceeded or non-retryable error).")
             raise Ignore() # Ensure Celery ignores it
        except Exception as e:
             # Catch exceptions raised by run_async_processing if they weren't Ignore/Reject
             self.task_log.exception("Task failed with unhandled exception in run wrapper", error=str(e))
             # Re-raise the exception so Celery marks the task as FAILED
             raise e

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Log task failure."""
        # This catches exceptions raised from run() that are not Ignore or Reject
        log.error(
            "Celery task failed",
            task_id=task_id,
            task_name=self.name,
            args=args,
            kwargs=kwargs,
            error_type=type(exc).__name__,
            error=str(exc),
            traceback=str(einfo.traceback),
            exc_info=False # Avoid duplicate traceback logging if already logged
        )
        # Optionally, trigger alerts or cleanup here.
        # The final DB status *should* have been set to 'error' already by run_async_processing,
        # unless the failure was in the final DB update itself (handled by Reject).

    def on_success(self, retval, task_id, args, kwargs):
        """Log task success."""
        log.info(
            "Celery task completed successfully",
            task_id=task_id,
            task_name=self.name,
            args=args,
            kwargs=kwargs,
            retval=retval
        )

# Register the custom task class with Celery
process_document_haystack_task = celery_app.register_task(ProcessDocumentTask())