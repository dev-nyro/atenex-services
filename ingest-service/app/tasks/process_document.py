# ingest-service/app/tasks/process_document.py
import asyncio
import os
import tempfile
import uuid
from typing import Optional, Dict, Any, List, Tuple, Type
from contextlib import asynccontextmanager
import structlog
# LLM_FLAG: ADD_IMPORT - Needed for StreamHandler used below
import logging # <--- IMPORTACIÓN AÑADIDA
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
# >>>>>>>>>>> CORRECTION: Remove OpenAI Embedder import <<<<<<<<<<<<<<
# from haystack.components.embedders import OpenAIDocumentEmbedder
# >>>>>>>>>>> END CORRECTION <<<<<<<<<<<<<<
from haystack.components.writers import DocumentWriter

# Milvus specific integration
from milvus_haystack import MilvusDocumentStore

# >>>>>>>>>>> CORRECTION: Add FastEmbed imports <<<<<<<<<<<<<<
# FastEmbed Imports
from haystack_integrations.components.embedders.fastembed import (
    FastembedDocumentEmbedder,
)
# Import for GPU device selection
from haystack.utils import ComponentDevice
# >>>>>>>>>>> END CORRECTION <<<<<<<<<<<<<<

# Custom imports
from app.core.config import settings
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
    """
    Synchronously initializes and returns a MilvusDocumentStore instance.
    Handles potential configuration errors during initialization.
    """
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Milvus Initialization
    log.debug("Initializing MilvusDocumentStore...")
    try:
        # >>>>>>>>>>> CORRECTION: Add embedding_dim if needed by constructor <<<<<<<<<<<<<<
        # Check MilvusDocumentStore constructor arguments for your version.
        # If it requires embedding_dim, uncomment and use settings.EMBEDDING_DIMENSION
        store = MilvusDocumentStore(
            connection_args={"uri": settings.MILVUS_URI},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            # embedding_dim=settings.EMBEDDING_DIMENSION, # Uncomment if required
            consistency_level="Strong",
        )
        # >>>>>>>>>>> END CORRECTION <<<<<<<<<<<<<<
        log.info("MilvusDocumentStore initialized successfully.",
                 uri=settings.MILVUS_URI, collection=settings.MILVUS_COLLECTION_NAME)
        return store
    except TypeError as te:
        log.exception("MilvusDocumentStore init TypeError", error=str(te), exc_info=True)
        raise RuntimeError(f"Milvus TypeError (check arguments like embedding_dim): {te}") from te
    except Exception as e:
        log.exception("Failed to initialize MilvusDocumentStore", error=str(e), exc_info=True)
        raise RuntimeError(f"Milvus Store Initialization Error: {e}") from e
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Milvus Initialization

# --- Haystack Component Initialization ---
# >>>>>>>>>>> CORRECTION: Use FastEmbed Embedder and GPU setting <<<<<<<<<<<<<<
def _initialize_haystack_components(
    document_store: MilvusDocumentStore
) -> Tuple[DocumentSplitter, FastembedDocumentEmbedder, DocumentWriter]: # Adjusted return type hint
    """
    Inicializa Splitter + FastEmbed + Writer.
    Usa GPU si settings.USE_GPU == "true"
    """
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Haystack Component Init
    log.debug("Initializing Haystack components (Splitter, Embedder, Writer)...")
    try:
        # Document Splitter (Remains the same)
        splitter = DocumentSplitter(
            split_by=settings.SPLITTER_SPLIT_BY, # Use setting
            split_length=settings.SPLITTER_CHUNK_SIZE,
            split_overlap=settings.SPLITTER_CHUNK_OVERLAP
        )
        log.info("DocumentSplitter initialized", split_by=settings.SPLITTER_SPLIT_BY, length=settings.SPLITTER_CHUNK_SIZE, overlap=settings.SPLITTER_CHUNK_OVERLAP)

        # Determine Device for FastEmbed
        use_gpu = settings.USE_GPU.lower() == "true"
        if use_gpu:
            try:
                device = ComponentDevice.from_str("cuda:0") # Try to get GPU
                log.info("GPU selected for FastEmbed.", device_str="cuda:0")
            except Exception as gpu_err:
                log.warning("Failed to select GPU, falling back to CPU.", error=str(gpu_err), setting_use_gpu=settings.USE_GPU)
                device = ComponentDevice.from_str("cpu") # Fallback to CPU
        else:
            device = ComponentDevice.from_str("cpu") # Explicitly CPU
            log.info("CPU selected for FastEmbed.", setting_use_gpu=settings.USE_GPU)


        # Document Embedder (FastEmbed)
        log.info("Initializing FastembedDocumentEmbedder...", model=settings.FASTEMBED_MODEL, device=str(device))
        embedder = FastembedDocumentEmbedder(
            model=settings.FASTEMBED_MODEL,
            device=device,
            batch_size=256,      # Increased throughput
            parallel=0           # Use all available cores if device is CPU
            # Add other relevant FastEmbed parameters if needed, e.g., cache_dir
        )

        # Preload model weights to avoid delays in the first task run
        log.info("Warming up FastEmbed model...")
        embedder.warm_up()
        log.info("FastEmbed model warmed up successfully.")


        # Document Writer (using the initialized Milvus store)
        writer = DocumentWriter(
            document_store=document_store,
            policy=DuplicatePolicy.OVERWRITE,
            batch_size=512 # Added for potential performance improvement with many chunks
        )
        log.info("DocumentWriter initialized", policy="OVERWRITE", batch_size=512)

        log.info("Haystack components initialized successfully.")
        return splitter, embedder, writer
    except Exception as e:
        log.exception("Failed to initialize Haystack components", error=str(e), exc_info=True)
        raise RuntimeError(f"Haystack Component Initialization Error: {e}") from e
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Haystack Component Init
# >>>>>>>>>>> END CORRECTION <<<<<<<<<<<<<<

# --- File Type to Converter Mapping ---
def get_converter(content_type: str) -> Type[Any]: # Use Type[Any] for broader compatibility
    """Returns the appropriate Haystack Converter based on content type."""
    log.debug("Selecting converter", content_type=content_type)
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Converter Mapping
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
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Converter Mapping

# --- Celery Task Setup ---
# Celery app instance is imported from app.tasks.celery_app

# --- Logging Configuration ---
# (Keep existing logging setup)
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
if not root_logger.handlers:
    root_logger.addHandler(handler)
    try:
        root_logger.setLevel(settings.LOG_LEVEL.upper())
    except ValueError:
        root_logger.setLevel("INFO")
        log.warning("Invalid LOG_LEVEL in settings, defaulting to INFO.")

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
        pool = await db_client.get_db_pool()
        yield pool
    except Exception as e:
        log.error("Failed to get DB pool for session", error=str(e), exc_info=True)
        raise
    finally:
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
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Main Async Flow
    flow_log = log.bind(
        document_id=document_id, company_id=company_id, task_id=task_id,
        attempt=attempt, filename=filename, content_type=content_type
    )
    flow_log.info("Starting asynchronous processing flow")

    # 1. Initialize Minio Client
    minio_client = MinioClient()

    # 2. Download file from Minio
    object_name = f"{company_id}/{document_id}/{filename}"
    temp_file_path = None
    try:
        flow_log.info("Downloading file from MinIO", object_name=object_name)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_file_path = os.path.join(temp_dir, filename)
            await minio_client.download_file(object_name, temp_file_path)
            flow_log.info("File downloaded successfully", temp_path=temp_file_path)

            # --- Processing happens within the 'with temp_dir' block ---

            # 3. Initialize Milvus Store
            loop = asyncio.get_running_loop()
            try:
                flow_log.info("Initializing Milvus document store...")
                store = await loop.run_in_executor(None, _initialize_milvus_store)
                flow_log.info("Milvus document store initialized.")
            except RuntimeError as e:
                flow_log.error("Failed to initialize Milvus store during flow", error=str(e))
                raise e
            except Exception as e:
                flow_log.exception("Unexpected error initializing Milvus store", error=str(e))
                raise RuntimeError(f"Unexpected Milvus init error: {e}") from e

            # 4. Initialize other Haystack Components (Splitter, FastEmbed, Writer)
            try:
                flow_log.info("Initializing Haystack processing components...")
                # This now returns FastembedDocumentEmbedder
                splitter, embedder, writer = await loop.run_in_executor(None, _initialize_haystack_components, store)
                flow_log.info("Haystack processing components initialized.")
            except RuntimeError as e:
                flow_log.error("Failed to initialize Haystack components during flow", error=str(e))
                raise e
            except Exception as e:
                flow_log.exception("Unexpected error initializing Haystack components", error=str(e))
                raise RuntimeError(f"Unexpected Haystack init error: {e}") from e

            # 5. Initialize Converter
            try:
                flow_log.info("Initializing document converter...")
                ConverterClass = get_converter(content_type)
                converter = ConverterClass()
                flow_log.info("Document converter initialized", converter=ConverterClass.__name__)
            except ValueError as ve:
                flow_log.error("Unsupported content type", error=str(ve))
                raise ve
            except Exception as e:
                flow_log.exception("Failed to initialize converter", error=str(e))
                raise RuntimeError(f"Converter Initialization Error: {e}") from e

            # --- Haystack Pipeline Execution (Run in Executor) ---
            total_chunks_written = 0
            try:
                flow_log.info("Starting Haystack pipeline execution (converter, splitter, embedder, writer)...")

                def run_haystack_pipeline_sync(local_temp_file_path): # Pass the path
                    nonlocal total_chunks_written
                    pipeline_log = log.bind(
                        document_id=document_id, company_id=company_id, task_id=task_id,
                        filename=filename, in_sync_executor=True
                    )
                    pipeline_log.debug("Executing conversion...")
                    conversion_result = converter.run(sources=[local_temp_file_path])
                    docs = conversion_result["documents"]
                    pipeline_log.debug("Conversion complete", num_docs_converted=len(docs))
                    if not docs:
                        pipeline_log.warning("Converter produced no documents.")
                        return 0

                    for doc in docs:
                        if doc.meta is None: doc.meta = {}
                        doc.meta["company_id"] = company_id
                        doc.meta["document_id"] = document_id
                        doc.meta["file_name"] = filename
                        doc.meta["file_type"] = content_type

                    pipeline_log.debug("Executing splitting...")
                    split_docs = splitter.run(documents=docs)["documents"]
                    pipeline_log.debug("Splitting complete", num_chunks=len(split_docs))
                    if not split_docs:
                         pipeline_log.warning("Splitter produced no documents (chunks).")
                         return 0

                    pipeline_log.debug("Executing embedding...")
                    # Embedder is now FastEmbed
                    embedded_docs = embedder.run(documents=split_docs)["documents"]
                    pipeline_log.debug("Embedding complete.")
                    if not embedded_docs:
                         pipeline_log.warning("Embedder produced no documents.")
                         return 0

                    pipeline_log.debug("Executing writing to Milvus...")
                    write_result = writer.run(documents=embedded_docs)
                    written_count = write_result["documents_written"]
                    pipeline_log.info("Writing complete.", documents_written=written_count)
                    total_chunks_written = written_count
                    return written_count

                chunks_written = await loop.run_in_executor(None, run_haystack_pipeline_sync, temp_file_path)
                flow_log.info("Haystack pipeline execution finished.", chunks_written=chunks_written)
                return total_chunks_written

            except Exception as e:
                flow_log.exception("Error during Haystack pipeline execution", error=str(e))
                raise RuntimeError(f"Haystack Pipeline Error: {e}") from e
            # Temp dir cleaned automatically

    except MinioError as me:
        flow_log.error("Failed to download file from MinIO", object_name=object_name, error=str(me))
        raise RuntimeError(f"MinIO download failed: {me}") from me
    except Exception as e:
        flow_log.exception("Unexpected error during file download or processing initiation", error=str(e))
        if temp_file_path and os.path.exists(temp_file_path) and isinstance(e, MinioError):
             try: os.remove(temp_file_path)
             except OSError: pass
        raise RuntimeError(f"Unexpected download/init error: {e}") from e

    return 0 # Should not be reached if logic is sound
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Main Async Flow


# --- Celery Task Definition ---
class ProcessDocumentTask(Task):
    """Custom Celery Task class for document processing."""
    name = "app.tasks.process_document.ProcessDocumentTask"
    max_retries = 3
    default_retry_delay = 60

    def __init__(self):
        super().__init__()
        self.task_log = log.bind(task_name=self.name)
        self.task_log.info("ProcessDocumentTask initialized.")

    # (Keep the previously corrected _update_status_with_retry)
    async def _update_status_with_retry(
        self, pool: asyncpg.Pool, doc_id: str, status: DocumentStatus,
        chunk_count: Optional[int] = None, error_msg: Optional[str] = None
    ):
        """Helper to update document status with retry."""
        update_log = self.task_log.bind(document_id=doc_id, target_status=status.value)
        try:
            async with pool.acquire() as conn:
                await db_retry_strategy(db_client.update_document_status)(
                    conn=conn,
                    document_id=uuid.UUID(doc_id),
                    status=status, # Pass Enum member
                    chunk_count=chunk_count,
                    error_message=error_msg
                )
            update_log.info("Document status updated successfully in DB.")
        except Exception as e:
            update_log.critical("CRITICAL: Failed final document status update in DB!",
                                error=str(e), chunk_count=chunk_count, error_msg=error_msg,
                                exc_info=True)
            raise ConnectionError(f"Persistent DB error updating status for {doc_id} to {status.value}") from e

    async def run_async_processing(self, *args, **kwargs):
        """Runs the main async processing flow and handles final status updates."""
        # (Keep existing logic, including calls to the corrected _update_status_with_retry)
        doc_id = kwargs['document_id']
        task_id = self.request.id
        attempt = self.request.retries + 1
        task_log = self.task_log.bind(document_id=doc_id, task_id=task_id, attempt=attempt)
        final_status = DocumentStatus.ERROR
        final_chunk_count = None
        error_to_report = "Unknown processing error"
        processing_exception: Optional[Exception] = None

        try:
            async with db_session_manager() as pool:
                 if not pool:
                     task_log.critical("Failed to get DB pool for task execution.")
                     raise Reject("DB pool unavailable for task", requeue=False)

                 try:
                    task_log.info("Setting document status to 'processing'")
                    await self._update_status_with_retry(pool, doc_id, DocumentStatus.PROCESSING, error_msg=None)

                    task_log.info("Executing main async_process_flow with timeout", timeout=TIMEOUT_SECONDS)
                    final_chunk_count = await asyncio.wait_for(
                        async_process_flow(task_id=task_id, attempt=attempt, **kwargs),
                        timeout=TIMEOUT_SECONDS
                    )
                    final_status = DocumentStatus.PROCESSED
                    error_to_report = None
                    task_log.info("Async process flow completed successfully.", chunks_processed=final_chunk_count)

                 except asyncio.TimeoutError as e:
                     task_log.error("Processing timed out", timeout=TIMEOUT_SECONDS)
                     error_to_report = f"Processing timed out after {TIMEOUT_SECONDS} seconds."
                     final_status = DocumentStatus.ERROR
                     processing_exception = e
                 except ValueError as e:
                      task_log.error("Processing failed due to value error", error=str(e))
                      error_to_report = f"Configuration or Input Error: {e}"
                      final_status = DocumentStatus.ERROR
                      processing_exception = e
                 except RuntimeError as e:
                      task_log.error(f"Processing failed with runtime error: {e}", exc_info=False)
                      error_to_report = f"Processing Runtime Error: {e}"
                      final_status = DocumentStatus.ERROR
                      processing_exception = e
                 except ConnectionError as e:
                      task_log.critical("Persistent DB connection error during status update", error=str(e))
                      raise Reject(f"Persistent DB error for {doc_id}: {e}", requeue=False) from e
                 except Exception as e:
                     task_log.exception("Unexpected exception during processing flow.", error=str(e))
                     error_to_report = f"Unexpected error during processing: {type(e).__name__}"
                     final_status = DocumentStatus.ERROR
                     processing_exception = e

                 task_log.info("Attempting to update final document status in DB", status=final_status.value, chunks=final_chunk_count, error=error_to_report)
                 try:
                     await self._update_status_with_retry(
                         pool, doc_id, final_status,
                         chunk_count=final_chunk_count if final_status == DocumentStatus.PROCESSED else None,
                         error_msg=error_to_report
                     )
                 except ConnectionError as db_update_exc:
                      task_log.critical("CRITICAL: Failed to update final document status in DB!", target_status=final_status.value, error=str(db_update_exc))
                      raise Reject(f"Final DB update failed for {doc_id}: {db_update_exc}", requeue=False) from db_update_exc
                 except Exception as db_unhandled_exc:
                      task_log.critical("CRITICAL: Unhandled exception during final DB update", error=str(db_unhandled_exc), exc_info=True)
                      raise Reject(f"Unhandled final DB update error for {doc_id}", requeue=False) from db_unhandled_exc


        except Reject as r:
             task_log.error(f"Task rejected: {r.reason}")
             raise r
        except Exception as outer_exc:
             task_log.exception("Outer exception caught in run_async_processing", error=str(outer_exc))
             processing_exception = outer_exc
             final_status = DocumentStatus.ERROR


        if final_status == DocumentStatus.ERROR and processing_exception:
             is_retryable = not isinstance(processing_exception, (ValueError, RuntimeError, asyncio.TimeoutError, Reject, ConnectionError))
             if is_retryable:
                 task_log.warning("Processing failed with a potentially retryable error, attempting task retry.", error=str(processing_exception))
                 try:
                     if self.request:
                         raise self.retry(exc=processing_exception, countdown=self.default_retry_delay * attempt)
                     else:
                         task_log.error("Cannot retry task: self.request is not available.")
                         raise Reject(f"Cannot retry {doc_id}, request context unavailable", requeue=False) from processing_exception
                 except MaxRetriesExceededError:
                     task_log.error("Max retries exceeded for task.", error=str(processing_exception))
                     raise Reject(f"Max retries exceeded for {doc_id}", requeue=False) from processing_exception
                 except Reject as r:
                      task_log.error("Task rejected during retry attempt.", reason=str(r))
                      raise r
                 except Exception as retry_exc:
                     task_log.exception("Exception occurred during task retry mechanism", error=str(retry_exc))
                     raise Reject(f"Retry mechanism failed for {doc_id}", requeue=False) from retry_exc
             else:
                 task_log.error("Processing failed with non-retryable error.", error=str(processing_exception), type=type(processing_exception).__name__)
                 raise Reject(f"Non-retryable error for {doc_id}: {processing_exception}", requeue=False) from processing_exception

        elif final_status == DocumentStatus.PROCESSED:
              task_log.info("Processing completed successfully for document.")
              return {"status": "processed", "document_id": doc_id, "chunks_processed": final_chunk_count}
        else:
             task_log.error("Task ended in unexpected state", final_status=final_status, error=error_to_report)
             raise Reject(f"Task for {doc_id} ended in unexpected state {final_status}", requeue=False)

    def run(self, *args, **kwargs):
        """Synchronous wrapper to run the async processing logic."""
        task_log = log.bind(task_id=self.request.id, task_name=self.name)
        task_log.info("Task received", args=args, kwargs=list(kwargs.keys()))
        try:
            return asyncio.run(self.run_async_processing(*args, **kwargs))
        except Reject as r:
             task_log.error(f"Task rejected: {r.reason}", exc_info=False)
             raise r
        except Ignore:
             task_log.warning("Task is being ignored.")
             raise Ignore()
        except Exception as e:
             task_log.exception("Task failed with unhandled exception in run wrapper", error=str(e))
             raise e

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Log task failure."""
        failure_log = log.bind(task_id=task_id, task_name=self.name, status="FAILED")
        reason = getattr(exc, 'reason', str(exc))
        failure_log.error(
            "Celery task final failure",
            args=args, kwargs=kwargs, error_type=type(exc).__name__, error=reason,
            traceback=str(einfo.traceback) if einfo else "No traceback info",
            exc_info=False
        )

    def on_success(self, retval, task_id, args, kwargs):
        """Log task success."""
        success_log = log.bind(task_id=task_id, task_name=self.name, status="SUCCESS")
        success_log.info(
            "Celery task completed successfully",
            args=args, kwargs=kwargs, retval=retval
        )

# Register the custom task class with Celery *ONCE*
process_document_haystack_task = celery_app.register_task(ProcessDocumentTask())