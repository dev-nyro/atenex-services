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
    log.debug("Initializing MilvusDocumentStore...")
    try:
        store = MilvusDocumentStore(
            connection_args={"uri": settings.MILVUS_URI},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            embedding_dim=settings.EMBEDDING_DIMENSION,
            consistency_level="Strong",
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

# --- Haystack Component Initialization (sin cambios) ---
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

# --- File Type to Converter Mapping (sin cambios) ---
def get_converter(content_type: str) -> Type[Any]:
    log.debug("Selecting converter", content_type=content_type)
    if content_type == "application/pdf": return PyPDFToDocument
    elif content_type in ["application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/msword"]: return DOCXToDocument
    elif content_type == "text/plain": return TextFileToDocument
    elif content_type == "text/markdown": return MarkdownToDocument
    elif content_type == "text/html": return HTMLToDocument
    else: raise ValueError(f"Unsupported content type: {content_type}")

# --- Celery Task Setup & Logging (sin cambios) ---
structlog.configure(processors=[ structlog.stdlib.filter_by_level, structlog.contextvars.merge_contextvars, structlog.stdlib.add_logger_name, structlog.stdlib.add_log_level, structlog.processors.TimeStamper(fmt="iso", utc=True), structlog.processors.StackInfoRenderer(), structlog.processors.format_exc_info, structlog.stdlib.ProcessorFormatter.wrap_for_formatter, ], logger_factory=structlog.stdlib.LoggerFactory(), wrapper_class=structlog.stdlib.BoundLogger, cache_logger_on_first_use=True,)
formatter = structlog.stdlib.ProcessorFormatter( processor=structlog.processors.JSONRenderer(), )
handler = logging.StreamHandler()
handler.setFormatter(formatter)
root_logger = logging.getLogger()
if not root_logger.handlers:
    root_logger.addHandler(handler)
    try: root_logger.setLevel(settings.LOG_LEVEL.upper())
    except ValueError: root_logger.setLevel("INFO"); log.warning("Invalid LOG_LEVEL, defaulting to INFO.")

db_retry_strategy = retry( stop=stop_after_attempt(3), wait=wait_fixed(2), retry=retry_if_exception_type((asyncpg.exceptions.PostgresConnectionError, TimeoutError, OSError)), before_sleep=before_sleep_log(log, logging.WARNING))

@asynccontextmanager
async def db_session_manager():
    pool = None
    try: pool = await db_client.get_db_pool(); yield pool
    except Exception as e: log.error("Failed get DB pool", error=str(e)); raise
    finally: log.debug("DB session context exited.")

# --- Main Asynchronous Processing Flow (sin cambios) ---
async def async_process_flow( *, document_id: str, company_id: str, filename: str, content_type: str, task_id: str, attempt: int ):
    flow_log = log.bind( document_id=document_id, company_id=company_id, task_id=task_id, attempt=attempt, filename=filename, content_type=content_type )
    flow_log.info("Starting asynchronous processing flow")
    minio_client = MinioClient()
    object_name = f"{company_id}/{document_id}/{filename}"
    temp_file_path = None
    total_chunks_written = 0

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
                pipeline_log = structlog.get_logger("sync_pipeline").bind( document_id=document_id, task_id=task_id, filename=filename )
                pipeline_log.debug("Executing conversion...")
                conversion_result = converter.run(sources=[local_temp_file_path])
                docs = conversion_result["documents"]
                pipeline_log.debug("Conversion complete", num_docs_converted=len(docs))
                if not docs: pipeline_log.warning("Converter produced 0 docs."); return 0

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
                return written_count

            chunks_written = await loop.run_in_executor(None, run_haystack_pipeline_sync, temp_file_path)
            total_chunks_written = chunks_written
            flow_log.info("Haystack pipeline execution finished.", chunks_written=total_chunks_written)
            return total_chunks_written

    except MinioError as me:
        flow_log.error("MinIO Error", object_name=object_name, error=str(me))
        raise RuntimeError(f"MinIO failed: {me}") from me
    except ValueError as ve:
         flow_log.error("Value Error (likely unsupported type)", error=str(ve))
         raise ve
    except RuntimeError as rt_err:
         flow_log.error("Runtime Error during processing", error=str(rt_err))
         raise rt_err
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

    async def _update_status_with_retry( self, pool: asyncpg.Pool, doc_id: str, status: DocumentStatus, chunk_count: Optional[int] = None, error_msg: Optional[str] = None ):
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
            # Log the error but raise a specific exception type Celery can handle for rejection
            raise ConnectionError(f"Persistent DB error updating status for {doc_id}") from e

    # <<< CORRECCIÓN: Hacer run async def y eliminar asyncio.run() >>>
    async def run(self, *args, **kwargs):
        """Asynchronous Celery task entry point."""
        doc_id = kwargs.get('document_id') # Use .get for safety
        # Ensure self.request is available before accessing its attributes
        task_id = self.request.id if self.request else 'N/A'
        attempt = (self.request.retries + 1) if self.request else 1
        task_log = self.task_log.bind(document_id=doc_id, task_id=task_id, attempt=attempt)

        task_log.info("Task received", args=args, kwargs=list(kwargs.keys()))

        # Check if document_id was provided
        if not doc_id:
             task_log.error("Task received without document_id in kwargs.")
             # Reject the task immediately as it cannot proceed
             raise Reject("Missing document_id in task arguments", requeue=False)

        final_status = DocumentStatus.ERROR
        final_chunk_count = 0
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
                    # Directly await the async function
                    final_chunk_count = await asyncio.wait_for(
                        async_process_flow(task_id=task_id, attempt=attempt, **kwargs),
                        timeout=TIMEOUT_SECONDS
                    )
                    final_status = DocumentStatus.PROCESSED
                    error_to_report = None
                    task_log.info("Async process flow completed successfully.", chunks_processed=final_chunk_count)

                 except asyncio.TimeoutError as e:
                     task_log.error("Processing timed out", timeout=TIMEOUT_SECONDS)
                     error_to_report = f"Processing timed out after {TIMEOUT_SECONDS}s."
                     final_status = DocumentStatus.ERROR # Ensure status is ERROR
                     processing_exception = e
                 except ValueError as e:
                      task_log.error("Processing failed: Value error", error=str(e))
                      error_to_report = f"Input/Config Error: {e}"
                      final_status = DocumentStatus.ERROR # Ensure status is ERROR
                      processing_exception = e
                 except RuntimeError as e:
                      task_log.error("Processing failed: Runtime error", error=str(e), exc_info=False)
                      error_to_report = f"Runtime Error: {e}"
                      final_status = DocumentStatus.ERROR # Ensure status is ERROR
                      processing_exception = e
                 except ConnectionError as e:
                      task_log.critical("Persistent DB connection error during status update", error=str(e))
                      # Raise Reject directly if DB update fails critically within retries
                      raise Reject(f"Persistent DB error for {doc_id}: {e}", requeue=False) from e
                 except Exception as e:
                     task_log.exception("Unexpected exception during processing flow.", error=str(e))
                     error_to_report = f"Unexpected error: {type(e).__name__}"
                     final_status = DocumentStatus.ERROR # Ensure status is ERROR
                     processing_exception = e

                 # Final status update attempt
                 task_log.info("Attempting final DB status update", status=final_status.value, chunks=final_chunk_count, error=error_to_report)
                 await self._update_status_with_retry(
                     pool, doc_id, final_status,
                     chunk_count=final_chunk_count if final_status == DocumentStatus.PROCESSED else 0,
                     error_msg=error_to_report
                 )

        except Reject as r:
             task_log.error(f"Task rejected: {r.reason}")
             # Re-raise Reject to ensure Celery handles it correctly
             raise r
        except ConnectionError as db_update_exc:
            task_log.critical("CRITICAL: Failed final DB update outside main try!", error=str(db_update_exc))
            # Raise Reject if the final status update fails critically
            raise Reject(f"Final DB update failed for {doc_id}: {db_update_exc}", requeue=False) from db_update_exc
        except Exception as outer_exc:
             task_log.exception("Outer exception caught in async run", error=str(outer_exc))
             processing_exception = outer_exc
             final_status = DocumentStatus.ERROR
             error_to_report = f"Outer task error: {type(outer_exc).__name__}"
             # Attempt to update status to error one last time if an outer exception occurred
             # This might fail if the pool is the issue, but worth a try.
             try:
                  async with db_session_manager() as final_pool:
                      if final_pool:
                          await self._update_status_with_retry(final_pool, doc_id, final_status, chunk_count=0, error_msg=error_to_report)
             except Exception as final_db_err:
                  task_log.critical("Failed to update status to ERROR after outer exception", final_db_error=str(final_db_err))
             # Regardless of final update success, raise Reject for the outer error
             raise Reject(f"Outer exception for {doc_id}: {error_to_report}", requeue=False) from outer_exc


        # --- Retry / Reject Logic (After successful execution or caught exception) ---
        if final_status == DocumentStatus.ERROR and processing_exception:
             is_retryable = not isinstance(processing_exception, (ValueError, asyncio.TimeoutError, Reject, ConnectionError))

             # Check if self.request exists before attempting retry
             if is_retryable and self.request and self.request.retries < self.max_retries:
                 task_log.warning("Processing failed, attempting retry.", error=str(processing_exception))
                 try:
                     # self.retry will raise the Retry exception
                     raise self.retry(exc=processing_exception, countdown=self.default_retry_delay * attempt)
                 except MaxRetriesExceededError:
                     task_log.error("Max retries exceeded.", error=str(processing_exception))
                     raise Reject(f"Max retries exceeded for {doc_id}", requeue=False) from processing_exception
                 except Reject as r: # Catch if self.retry itself raises Reject (e.g., disabled)
                     task_log.error("Retry attempt resulted in Reject.", reason=str(r))
                     raise r
                 except Exception as retry_exc: # Catch unexpected errors during retry mechanism
                     task_log.exception("Exception during retry mechanism", error=str(retry_exc))
                     raise Reject(f"Retry mechanism failed for {doc_id}", requeue=False) from retry_exc
             else: # Non-retryable error or max retries exceeded
                 task_log.error("Processing failed permanently.", error=str(processing_exception), type=type(processing_exception).__name__)
                 raise Reject(f"Non-retryable error/max retries for {doc_id}: {error_to_report}", requeue=False) from processing_exception

        elif final_status == DocumentStatus.PROCESSED:
              task_log.info("Processing completed successfully.")
              return {"status": "processed", "document_id": doc_id, "chunks_processed": final_chunk_count}
        else: # Should ideally not be reached if status is always PROCESSED or ERROR with exception
             task_log.error("Task ended in unexpected final state", final_status=final_status)
             raise Reject(f"Unexpected final state {final_status} for {doc_id}", requeue=False)

    # Keep on_failure and on_success as they are (they are called by Celery based on task outcome)
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        failure_log = log.bind(task_id=task_id, task_name=self.name, status="FAILED")
        reason = getattr(exc, 'reason', str(exc))
        failure_log.error(
            "Celery task final failure", args=args, kwargs=kwargs,
            error_type=type(exc).__name__, error=reason,
            traceback=str(einfo.traceback) if einfo else "No traceback"
        )

    def on_success(self, retval, task_id, args, kwargs):
        success_log = log.bind(task_id=task_id, task_name=self.name, status="SUCCESS")
        success_log.info("Celery task completed successfully", args=args, kwargs=kwargs, retval=retval)
# <<< FIN CORRECCIÓN >>>

# Register the custom task class with Celery
# This now registers the class where 'run' is async def
process_document_haystack_task = celery_app.register_task(ProcessDocumentTask())