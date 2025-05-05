# ingest-service/app/tasks/process_document.py
def normalize_filename(filename: str) -> str:
    """Normaliza el nombre de archivo eliminando espacios al inicio/final y espacios duplicados."""
    return " ".join(filename.strip().split())
# ingest-service/app/tasks/process_document.py
import os
import tempfile
import uuid
import sys
import pathlib
import time
import json # For serializing metadata to JSONB
from typing import Optional, Dict, Any, List # Added List

import structlog
from celery import Task
from celery.exceptions import Ignore, Reject, MaxRetriesExceededError
from celery.signals import worker_process_init
from sqlalchemy import Engine

# Embedding components
from sentence_transformers import SentenceTransformer
from app.services.embedder import get_embedding_model

# Custom Application Imports
from app.core.config import settings
from app.db.postgres_client import get_sync_engine, set_status_sync, bulk_insert_chunks_sync # Import new function
from app.models.domain import DocumentStatus, DocumentChunkData, ChunkVectorStatus # Import needed models
from app.services.gcs_client import GCSClient, GCSClientError
# Import REFACTORIZADO pipeline and supporting elements
from app.services.ingest_pipeline import (
    ingest_document_pipeline,
    EXTRACTORS,
    EXTRACTION_ERRORS,
    delete_milvus_chunks # Import delete function for potential cleanup
)
from app.tasks.celery_app import celery_app

task_struct_log = structlog.get_logger(__name__)
IS_WORKER = "worker" in sys.argv

# --- Global Resources for Worker Process ---
sync_engine: Optional[Engine] = None
gcs_client: Optional[GCSClient] = None
worker_embedding_model: Optional[SentenceTransformer] = None

@worker_process_init.connect(weak=False)
def init_worker_resources(**kwargs):
    """Initializes resources required by the worker process."""
    global sync_engine, gcs_client, worker_embedding_model
    log = task_struct_log.bind(signal="worker_process_init")
    log.info("Worker process initializing resources...")
    try:
        if sync_engine is None:
            sync_engine = get_sync_engine()
            log.info("Synchronous DB engine initialized.")
        else:
             log.info("Synchronous DB engine already initialized.")

        if gcs_client is None:
            gcs_client = GCSClient()
            log.info("GCS client initialized.")
        else:
            log.info("GCS client already initialized.")

        if worker_embedding_model is None:
            log.info("Preloading embedding model...")
            worker_embedding_model = get_embedding_model() # This handles loading and caching
            log.info("Embedding model preloaded.")
        else:
            log.info("Embedding model already preloaded.")

    except Exception as e:
        log.critical("CRITICAL FAILURE during worker resource initialization!", error=str(e), exc_info=True)
        sync_engine = None
        gcs_client = None
        worker_embedding_model = None

# --------------------------------------------------------------------------
# Refactored Celery Task Definition - v0.3.0
# --------------------------------------------------------------------------
@celery_app.task(
    bind=True,
    name="ingest.process_document", # Keep name for potential compatibility
    autoretry_for=(Exception,),
    exclude=(Reject, Ignore, ValueError, ConnectionError, RuntimeError, TypeError, *EXTRACTION_ERRORS),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3,
    acks_late=True # Ensure task is acknowledged only after completion/failure
)
def process_document_standalone(self: Task, *args, **kwargs) -> Dict[str, Any]:
    """
    Refactored Celery task (v0.3.0):
    1. Downloads file from GCS.
    2. Executes the ingestion pipeline (extract, chunk, metadata, embed, insert Milvus).
    3. Stores detailed chunk info (content, metadata, Milvus PK) in PostgreSQL.
    4. Updates document status synchronously.
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
    log.info("Starting document processing task v0.3.0")

    # --- Pre-checks ---
    if not IS_WORKER:
         log.critical("Task function called outside of a worker context! Rejecting.")
         raise Reject("Task running outside worker context.", requeue=False)

    if not all([document_id_str, company_id_str, filename, content_type]):
        log.error("Missing required arguments in task payload.", payload_kwargs=kwargs)
        raise Reject("Missing required arguments (doc_id, company_id, filename, content_type)", requeue=False)

    if not sync_engine:
         log.critical("Worker Sync DB Engine is not initialized. Task cannot proceed.")
         raise Reject("Worker sync DB engine initialization failed.", requeue=False)
    if not worker_embedding_model:
         log.critical("Worker Embedding Model is not available/loaded. Task cannot proceed.")
         error_msg = "Worker embedding model init/preload failed."
         doc_uuid_err = None
         try: doc_uuid_err = uuid.UUID(document_id_str)
         except ValueError: pass
         if doc_uuid_err:
             try: set_status_sync(engine=sync_engine, document_id=doc_uuid_err, status=DocumentStatus.ERROR, error_message=error_msg)
             except Exception as db_err: log.critical("Failed to update status after embedding model check failure!", error=str(db_err))
         raise Reject(error_msg, requeue=False)
    if not gcs_client:
         log.critical("Worker GCS Client is not initialized. Task cannot proceed.")
         error_msg = "Worker GCS client init failed."
         doc_uuid_err = None
         try: doc_uuid_err = uuid.UUID(document_id_str)
         except ValueError: pass
         if doc_uuid_err:
              try: set_status_sync(engine=sync_engine, document_id=doc_uuid_err, status=DocumentStatus.ERROR, error_message=error_msg)
              except Exception as db_err: log.critical("Failed to update status after GCS client check failure!", error=str(db_err))
         raise Reject(error_msg, requeue=False)

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

    normalized_filename = normalize_filename(filename) if filename else filename
    object_name = f"{company_id_str}/{document_id_str}/{normalized_filename}"
    file_bytes: Optional[bytes] = None
    inserted_milvus_count = 0
    inserted_pg_count = 0
    milvus_pks: List[str] = []
    processed_chunks_data: List[DocumentChunkData] = [] # Holds data before PG insert

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

        # 2. Download file and read bytes
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = pathlib.Path(temp_dir)
            temp_file_path_obj = temp_dir_path / normalized_filename
            log.info(f"Downloading GCS object: {object_name} -> {str(temp_file_path_obj)}")

            max_gcs_retries = 3
            gcs_delay = 2
            download_success = False
            for attempt_num in range(1, max_gcs_retries + 1):
                try:
                    gcs_client.download_file_sync(object_name, str(temp_file_path_obj))
                    log.info("File downloaded successfully from GCS.")
                    file_bytes = temp_file_path_obj.read_bytes()
                    log.info(f"File content read into memory ({len(file_bytes)} bytes).")
                    download_success = True
                    break
                except GCSClientError as gce:
                    if "Object not found" in str(gce):
                        log.warning(f"GCS object not found on download attempt {attempt_num}/{max_gcs_retries}. Retrying in {gcs_delay}s...", error=str(gce))
                        if attempt_num == max_gcs_retries:
                            log.error(f"File still not found in GCS after {max_gcs_retries} attempts. Aborting.")
                            raise # Re-raise the final GCSClientError
                        time.sleep(gcs_delay)
                        gcs_delay *= 2
                    else:
                        log.error("Non-retriable GCS error during download", error=str(gce))
                        raise
                except Exception as read_err:
                    log.error("Failed to read downloaded file into bytes", error=str(read_err), exc_info=True)
                    raise RuntimeError(f"Failed to read temp file: {read_err}") from read_err
            if not download_success or file_bytes is None:
                 raise RuntimeError("File download or read failed, bytes not available.")

        # --- File bytes are now available ---

        # 3. Execute Ingestion Pipeline (Milvus Insert Included)
        log.info("Executing ingest pipeline (extract, chunk, metadata, embed, Milvus insert)...")
        inserted_milvus_count, milvus_pks, processed_chunks_data = ingest_document_pipeline(
            file_bytes=file_bytes,
            filename=normalized_filename,
            company_id=company_id_str,
            document_id=document_id_str,
            content_type=content_type,
            embedding_model=worker_embedding_model,
            delete_existing=True
        )
        log.info(f"Ingestion pipeline finished. Milvus insert count: {inserted_milvus_count}, PKs returned: {len(milvus_pks)}")

        if inserted_milvus_count == 0 or not processed_chunks_data:
             log.warning("Ingestion pipeline reported zero inserted chunks or no data. Assuming processing complete with 0 chunks.")
             # Update status to PROCESSED with 0 chunks
             final_status_updated = set_status_sync(
                 engine=sync_engine,
                 document_id=doc_uuid,
                 status=DocumentStatus.PROCESSED,
                 chunk_count=0,
                 error_message=None
             )
             return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": 0, "document_id": document_id_str}


        # 4. Prepare data and Insert into PostgreSQL
        log.info(f"Preparing {len(processed_chunks_data)} chunks for PostgreSQL bulk insert...")
        chunks_for_pg = []
        for chunk_obj in processed_chunks_data:
            if chunk_obj.embedding_id is None:
                log.warning("Chunk missing embedding_id after Milvus insert, skipping PG insert for this chunk.", chunk_index=chunk_obj.chunk_index)
                continue # Skip chunks that didn't get a Milvus PK assigned
            chunk_dict = {
                # 'id' is auto-generated by PG
                'document_id': chunk_obj.document_id,
                'company_id': chunk_obj.company_id,
                'chunk_index': chunk_obj.chunk_index,
                'content': chunk_obj.content,
                'metadata': chunk_obj.metadata.model_dump(mode='json'), # Use Pydantic's method for JSONB
                'embedding_id': chunk_obj.embedding_id,
                'vector_status': chunk_obj.vector_status.value
                # 'created_at' is auto-generated by PG
            }
            chunks_for_pg.append(chunk_dict)

        if not chunks_for_pg:
             log.error("No valid chunks prepared for PostgreSQL insertion after Milvus step. Marking document as error.")
             error_msg = "Milvus insertion succeeded but no valid data for PG."
             set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
             # Consider cleanup of Milvus chunks here?
             raise Reject(error_msg, requeue=False)

        log.debug(f"Attempting bulk insert of {len(chunks_for_pg)} chunks into PostgreSQL.")
        try:
            inserted_pg_count = bulk_insert_chunks_sync(engine=sync_engine, chunks_data=chunks_for_pg)
            log.info(f"Successfully bulk inserted {inserted_pg_count} chunks into PostgreSQL.")
            if inserted_pg_count != len(chunks_for_pg):
                 log.warning("Mismatch between prepared PG chunks and inserted PG count.", prepared=len(chunks_for_pg), inserted=inserted_pg_count)
                 # Potentially mark document as error or investigate? For now, use inserted_pg_count.
        except Exception as pg_insert_err:
             log.critical("CRITICAL: Failed to bulk insert chunks into PostgreSQL after successful Milvus insert!", error=str(pg_insert_err), exc_info=True)
             # --- Attempt Milvus Cleanup ---
             log.warning("Attempting to clean up Milvus chunks due to PG insert failure.")
             try:
                 delete_milvus_chunks(company_id=company_id_str, document_id=document_id_str)
                 log.info("Milvus cleanup attempt completed after PG failure.")
             except Exception as cleanup_err:
                 log.error("Failed to cleanup Milvus chunks after PG failure.", cleanup_error=str(cleanup_err))
             # --- Set Document to Error ---
             error_msg = f"PG bulk insert failed: {type(pg_insert_err).__name__}"
             set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg[:500])
             raise Reject(f"PostgreSQL bulk insert failed: {pg_insert_err}", requeue=False) from pg_insert_err


        # 5. Update status to PROCESSED with final count from PG
        log.debug("Setting final status to PROCESSED in DB.")
        final_status_updated = set_status_sync(
            engine=sync_engine,
            document_id=doc_uuid,
            status=DocumentStatus.PROCESSED,
            chunk_count=inserted_pg_count, # Use count from successful PG insert
            error_message=None
        )
        if not final_status_updated:
             log.warning("Failed to update final status to PROCESSED (document possibly deleted?).")

        log.info(f"Document processing finished successfully. Final chunk count (PG): {inserted_pg_count}")
        return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": inserted_pg_count, "document_id": document_id_str}

    # --- Error Handling ---
    except GCSClientError as gce:
        log.error(f"GCS Error during processing", error=str(gce), exc_info=True)
        error_msg = f"GCS Error: {str(gce)[:400]}"
        try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
        except Exception as db_err: log.critical("Failed to update status after GCS failure!", error=str(db_err))
        if "Object not found" in str(gce):
            raise Reject(f"GCS Error: Object not found: {object_name}", requeue=False) from gce
        else:
            # Let Celery retry other GCS errors
             raise self.retry(exc=gce, countdown=gcs_delay) # Manually retry for specific non-Reject GCS errors


    except (*EXTRACTION_ERRORS, ValueError, RuntimeError, TypeError) as pipeline_err:
         # Catches non-retriable errors from extraction, pipeline logic, Milvus insert, PG insert prep etc.
         log.error(f"Non-retriable Pipeline/Data Error: {pipeline_err}", exc_info=True)
         error_msg = f"Pipeline Error: {type(pipeline_err).__name__} - {str(pipeline_err)[:400]}"
         try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
         except Exception as db_err: log.critical("Failed to update status after pipeline failure!", error=str(db_err))
         raise Reject(f"Pipeline failed: {error_msg}", requeue=False) from pipeline_err

    except Reject as r:
         log.error(f"Task rejected permanently: {r.reason}")
         if sync_engine and doc_uuid:
             try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=f"Rejected: {r.reason}"[:500])
             except Exception as db_err: log.critical("Failed to update status after task rejection!", error=str(db_err))
         raise r # Propagate Reject

    except Ignore:
         log.info("Task ignored.")
         raise Ignore() # Propagate Ignore

    except MaxRetriesExceededError as mree:
        log.error("Max retries exceeded for task.", exc_info=True)
        final_error = mree.cause if mree.cause else mree
        error_msg = f"Max retries exceeded ({max_attempts}). Last error: {type(final_error).__name__} - {str(final_error)[:300]}"
        try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
        except Exception as db_err: log.critical("Failed to update status to ERROR after max retries!", error=str(db_err))
        # Let Celery handle logging MaxRetriesExceededError - don't raise Reject here

    except Exception as exc:
        # Catch-all for potentially retriable unexpected errors
        log.exception(f"An unexpected error occurred, attempting retry if possible.")
        error_msg = f"Attempt {attempt} failed: {type(exc).__name__} - {str(exc)[:400]}"
        try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg) # Log error on current attempt
        except Exception as db_err: log.critical("CRITICAL: Failed to update status during unexpected failure handling!", error=str(db_err))
        # Raise the original exception to let Celery's autoretry handle it
        raise exc

# LLM_FLAG: Keeping alias for potential external references
process_document_haystack_task = process_document_standalone