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
from celery.exceptions import Ignore, Reject, MaxRetriesExceededError, Retry # Import Retry
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
            # embedding_dim=settings.EMBEDDING_DIMENSION, # REMOVED - Not a valid argument in newer versions
            consistency_level="Strong",
        )
        log.info("MilvusDocumentStore initialized successfully.",
                 uri=settings.MILVUS_URI, collection=settings.MILVUS_COLLECTION_NAME)
        return store
    except TypeError as te:
        log.exception("MilvusDocumentStore init TypeError (check constructor arguments for your version)", error=str(te), exc_info=True)
        raise RuntimeError(f"Milvus Constructor TypeError: {te}") from te
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
            device=device,
            batch_size=256,
            parallel=0 if device.type == "cpu" else None
        )

        log.info("Warming up FastEmbed model...")
        embedder.warm_up()
        log.info("FastEmbed model warmed up successfully.")

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
        log.warning("Invalid LOG_LEVEL, defaulting to INFO.")

# --- Database Retry Strategy ---
db_retry_strategy = retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(2),
    retry=retry_if_exception_type((asyncpg.exceptions.PostgresConnectionError, TimeoutError, OSError)),
    before_sleep=before_sleep_log(log, logging.WARNING)
)

# --- Database Session Manager ---
@asynccontextmanager
async def db_session_manager():
    """Provides a pooled connection for the duration of the 'with' block."""
    pool = None
    conn = None
    try:
        pool = await db_client.get_db_pool()
        conn = await pool.acquire()
        log.debug("DB connection acquired from pool.")
        yield conn
    except Exception as e:
        log.error("Failed to get DB pool or acquire connection", error=str(e))
        raise
    finally:
        if conn and pool:
            try:
                await pool.release(conn)
                log.debug("DB connection released back to pool.")
            except Exception as release_err:
                # Log error during release but don't prevent context exit
                log.error("Error releasing DB connection back to pool", error=str(release_err))
        log.debug("DB session context exited.")


# --- Main Asynchronous Processing Flow ---
async def async_process_flow(
    *,
    document_id: str,
    company_id: str,
    filename: str,
    content_type: str,
    task_id: str,
    attempt: int
) -> int:
    """
    Downloads file, runs Haystack pipeline, returns chunk count.
    (DB updates moved to the wrapper)
    """
    flow_log = log.bind(
        document_id=document_id, company_id=company_id, task_id=task_id, attempt=attempt,
        filename=filename, content_type=content_type
    )
    flow_log.info("Starting asynchronous processing flow")
    minio_client = MinioClient()
    object_name = f"{company_id}/{document_id}/{filename}"
    temp_file_path = None

    try:
        flow_log.info("Downloading file from MinIO", object_name=object_name)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_file_path = os.path.join(temp_dir, filename)
            await minio_client.download_file(object_name, temp_file_path)
            flow_log.info("File downloaded successfully", temp_path=temp_file_path)

            loop = asyncio.get_running_loop() # Get loop within async context
            store = await loop.run_in_executor(None, _initialize_milvus_store)
            splitter, embedder, writer = await loop.run_in_executor(None, _initialize_haystack_components, store)

            ConverterClass = get_converter(content_type)
            converter = ConverterClass()

            flow_log.info("Starting Haystack pipeline execution...")

            def run_haystack_pipeline_sync(local_temp_file_path):
                pipeline_log = flow_log
                pipeline_log.debug("Executing conversion...")
                conversion_result = converter.run(sources=[local_temp_file_path])
                docs: List[Document] = conversion_result["documents"]
                pipeline_log.debug("Conversion complete", num_docs_converted=len(docs))
                if not docs: return 0

                pipeline_log.debug("Adding standard metadata...")
                for doc in docs:
                    if doc.meta is None: doc.meta = {}
                    doc.meta["company_id"] = company_id
                    doc.meta["document_id"] = document_id
                    doc.meta["file_name"] = filename
                    doc.meta["file_type"] = content_type

                pipeline_log.debug("Executing splitting...")
                split_result = splitter.run(documents=docs)
                split_docs: List[Document] = split_result["documents"]
                pipeline_log.debug("Splitting complete", num_chunks=len(split_docs))
                if not split_docs: return 0

                pipeline_log.debug("Executing embedding...")
                embed_result = embedder.run(documents=split_docs)
                embedded_docs: List[Document] = embed_result["documents"]
                pipeline_log.debug("Embedding complete.")
                if not embedded_docs: return 0

                pipeline_log.debug("Executing writing to Milvus...")
                write_result = writer.run(documents=embedded_docs)
                written_count = write_result["documents_written"]
                pipeline_log.info("Writing complete.", documents_written=written_count)
                return written_count

            chunks_written = await loop.run_in_executor(None, run_haystack_pipeline_sync, temp_file_path)
            flow_log.info("Haystack pipeline execution finished.", chunks_written=chunks_written)
            return chunks_written

    except MinioError as me:
        flow_log.error("MinIO Error during download", object_name=object_name, error=str(me))
        raise RuntimeError(f"MinIO download failed for {object_name}: {me}") from me
    except ValueError as ve:
         flow_log.error("Value Error (likely unsupported type)", error=str(ve))
         raise ve # Propagate as non-retryable
    except RuntimeError as rt_err:
         flow_log.error("Runtime Error during processing", error=str(rt_err))
         raise rt_err # Propagate for potential retry
    except Exception as e:
        flow_log.exception("Unexpected error during processing flow", error=str(e))
        raise RuntimeError(f"Unexpected flow error: {e}") from e


# --- Celery Task Definition ---
class ProcessDocumentTask(Task):
    name = "app.tasks.process_document.ProcessDocumentTask"
    max_retries = 3
    default_retry_delay = 60

    def __init__(self):
        super().__init__()
        self.task_log = log.bind(task_name=self.name)
        self.task_log.info("ProcessDocumentTask initialized.")

    # Async helper for DB updates remains async
    async def _update_status_with_retry(
        self,
        conn: asyncpg.Connection, doc_id: str, status: DocumentStatus,
        chunk_count: Optional[int] = None, error_msg: Optional[str] = None
    ):
        update_log = self.task_log.bind(document_id=doc_id, target_status=status.value)
        try:
            await db_retry_strategy(db_client.update_document_status)(
                conn=conn, document_id=uuid.UUID(doc_id), status=status,
                chunk_count=chunk_count, error_message=error_msg
            )
            update_log.info("Document status updated successfully in DB.")
        except Exception as e:
            update_log.critical("CRITICAL: Failed final DB status update after retries!", error=str(e), exc_info=True)
            raise ConnectionError(f"Persistent DB error updating status for {doc_id}") from e

    # The async wrapper handles the full lifecycle within an async context
    async def _async_run_wrapper(self, *args, **kwargs):
        document_id = kwargs.get('document_id')
        company_id = kwargs.get('company_id')
        filename = kwargs.get('filename')
        content_type = kwargs.get('content_type')
        task_id = kwargs.get('task_id', 'N/A')
        attempt = kwargs.get('attempt', 1)

        wrapper_log = self.task_log.bind(
             document_id=document_id, task_id=task_id, attempt=attempt,
             company_id=company_id, filename=filename
        )

        final_status = DocumentStatus.ERROR
        final_chunk_count = 0
        error_to_report = "Unknown processing error"
        processing_exception: Optional[Exception] = None

        try:
            async with db_session_manager() as conn:
                if not conn:
                    raise ConnectionError("DB connection unavailable for task")

                try:
                    wrapper_log.info("Setting document status to 'processing'")
                    await self._update_status_with_retry(
                        conn, document_id, DocumentStatus.PROCESSING, error_msg=None
                    )

                    wrapper_log.info("Executing main async_process_flow...")
                    # Removed timeout here, let Celery handle task timeout if needed
                    final_chunk_count = await async_process_flow(
                            document_id=document_id, company_id=company_id, filename=filename,
                            content_type=content_type, task_id=task_id, attempt=attempt
                        )
                    final_status = DocumentStatus.PROCESSED
                    error_to_report = None
                    wrapper_log.info("Async process flow completed successfully.", chunks_processed=final_chunk_count)

                except ValueError as e: # Non-retryable
                      wrapper_log.error("Processing failed: Value error", error=str(e))
                      error_to_report = f"Input/Config Error: {e}"
                      final_status = DocumentStatus.ERROR
                      processing_exception = e
                except RuntimeError as e: # Potentially retryable
                      wrapper_log.error("Processing failed: Runtime error", error=str(e), exc_info=False)
                      error_to_report = f"Runtime Error: {e}"
                      final_status = DocumentStatus.ERROR
                      processing_exception = e
                except ConnectionError as e: # Critical DB error during update
                      wrapper_log.critical("Persistent DB connection error during status update", error=str(e))
                      raise e # Re-raise to be caught by outer try
                except Exception as e: # Unexpected processing errors
                     wrapper_log.exception("Unexpected exception during processing flow.", error=str(e))
                     error_to_report = f"Unexpected error: {type(e).__name__}"
                     final_status = DocumentStatus.ERROR
                     processing_exception = e

                wrapper_log.info("Attempting final DB status update", status=final_status.value, chunks=final_chunk_count, error=error_to_report)
                await self._update_status_with_retry(
                    conn, document_id, final_status,
                    chunk_count=final_chunk_count if final_status == DocumentStatus.PROCESSED else 0,
                    error_msg=error_to_report
                )

                if processing_exception:
                    raise processing_exception # Re-raise after final DB update

        except (ConnectionError, asyncpg.exceptions.PostgresConnectionError) as db_conn_exc:
             wrapper_log.critical("Failed to execute task due to DB connection error", error=str(db_conn_exc))
             raise db_conn_exc # Propagate DB connection error
        except Exception as e:
             wrapper_log.exception("Exception caught in async wrapper", error=str(e))
             raise e # Propagate other errors

        # Return result dict only on success
        return {"status": "processed", "document_id": document_id, "chunks_processed": final_chunk_count}


    # <<< CORRECTION: run is now synchronous, using loop.run_until_complete >>>
    # LLM_FLAG: CELERY_TASK_RUNNER - Main synchronous task entry point
    def run(self, *args, **kwargs):
        """
        Synchronous Celery task entry point. Executes the async logic using
        loop.run_until_complete(). Handles retries and returns a JSON-serializable result.
        """
        document_id = kwargs.get('document_id')
        task_id = self.request.id if self.request else 'N/A'
        attempt = (self.request.retries + 1) if self.request else 1

        task_log = self.task_log.bind(
            document_id=document_id, task_id=task_id, attempt=attempt,
            company_id=kwargs.get('company_id'), filename=kwargs.get('filename')
        )
        task_log.info("Sync task run invoked", args_repr=repr(args), kwargs_keys=list(kwargs.keys()))

        result = None
        run_exception = None
        loop = None

        try:
            # Get or create an event loop
            try:
                loop = asyncio.get_running_loop()
                task_log.debug("Reusing existing event loop.")
            except RuntimeError:
                task_log.debug("No running event loop, creating a new one.")
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            # Prepare arguments for the async wrapper
            wrapper_kwargs = {**kwargs, "task_id": task_id, "attempt": attempt}

            # Run the async wrapper until it completes
            result = loop.run_until_complete(self._async_run_wrapper(*args, **wrapper_kwargs))
            task_log.info("Async wrapper executed successfully via run_until_complete.")

        except (Retry, Reject) as celery_exc:
             task_log.warning(f"Celery control flow exception caught: {type(celery_exc).__name__}")
             raise celery_exc # Let Celery handle these
        except Exception as e:
            task_log.exception("Exception caught from run_until_complete(_async_run_wrapper)", error=str(e))
            run_exception = e # Store exception for retry logic
        # finally:
            # Avoid closing the loop if we didn't create it or if gevent manages it
            # if loop and not loop.is_running() and hasattr(loop, 'close'):
            #     task_log.debug("Closing event loop created by task.")
            #     loop.close()

        # --- Retry / Final Outcome Logic (sync context) ---
        if run_exception:
             is_retryable = isinstance(run_exception, (
                 RuntimeError, asyncio.TimeoutError, httpx.RequestError, MinioError,
                 ConnectionError, asyncpg.exceptions.PostgresConnectionError, OSError
             )) and not isinstance(run_exception, ValueError)

             if is_retryable and self.request and self.request.retries < self.max_retries:
                 task_log.warning("Processing failed within run_until_complete, attempting retry.", error=str(run_exception))
                 try:
                     raise self.retry(exc=run_exception, countdown=int(self.default_retry_delay * (attempt**1.5)))
                 except MaxRetriesExceededError:
                     task_log.error("Max retries exceeded after failure.", error=str(run_exception))
                     raise run_exception from None # Reraise original exception
                 except Retry:
                     raise # Re-raise Retry for Celery
                 except Exception as retry_exc:
                     task_log.exception("Exception during Celery retry mechanism", error=str(retry_exc))
                     raise Reject(f"Retry mechanism failed for {document_id}", requeue=False) from retry_exc
             else:
                 error_reason = "Non-retryable error" if not is_retryable else "Max retries exceeded"
                 task_log.error(f"Processing failed permanently ({error_reason}).", error=str(run_exception), type=type(run_exception).__name__)
                 raise run_exception from None # Reraise original exception

        elif result:
              task_log.info("Processing completed successfully (sync run).")
              return result # Return the serializable dict
        else:
             task_log.error("Sync run finished without result or exception.")
             raise Reject(f"Unexpected end state for {document_id}", requeue=False)

    # LLM_FLAG: STANDARD_CELERY_CALLBACK - Keep standard failure handler
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        failure_log = log.bind(task_id=task_id, task_name=self.name, status="FAILED")
        reason = getattr(exc, 'reason', str(exc))
        failure_log.error(
            "Celery task final failure",
            args_repr=repr(args), kwargs_keys=list(kwargs.keys()),
            error_type=type(exc).__name__, error=reason,
            traceback=str(einfo.traceback) if einfo else "No traceback available"
        )

    # LLM_FLAG: STANDARD_CELERY_CALLBACK - Keep standard success handler
    def on_success(self, retval, task_id, args, kwargs):
        success_log = log.bind(task_id=task_id, task_name=self.name, status="SUCCESS")
        success_log.info(
            "Celery task completed successfully",
            args_repr=repr(args), kwargs_keys=list(kwargs.keys()),
            retval=retval
        )
# <<< END CORRECTION >>>

# Register the custom task class with Celery
process_document_haystack_task = celery_app.register_task(ProcessDocumentTask())