# ingest-service/app/tasks/process_document.py
import os
import tempfile
import uuid
import sys
import pathlib
import time
import json
from typing import Optional, Dict, Any, List

import structlog
import httpx 
from celery import Task, states
from celery.exceptions import Ignore, Reject, MaxRetriesExceededError, Retry
from celery.signals import worker_process_init
from sqlalchemy import Engine
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, before_sleep_log
import logging 

logging.basicConfig(
    stream=sys.stdout, 
    level=logging.DEBUG, 
    format='%(asctime)s - %(name)s - %(levelname)s - [StdLib] - %(message)s',
    force=True 
)
stdlib_task_logger = logging.getLogger("app.tasks.process_document.stdlib")


def normalize_filename(filename: str) -> str:
    return " ".join(filename.strip().split())


from app.core.config import settings
from app.db.postgres_client import get_sync_engine, set_status_sync, bulk_insert_chunks_sync
from app.models.domain import DocumentStatus
from app.services.gcs_client import GCSClient, GCSClientError
from app.services.ingest_pipeline import (
    index_chunks_in_milvus_and_prepare_for_pg,
    delete_milvus_chunks
)
from app.tasks.celery_app import celery_app

task_struct_log = structlog.get_logger(__name__) 
IS_WORKER = "worker" in sys.argv

sync_engine: Optional[Engine] = None
gcs_client_global: Optional[GCSClient] = None

sync_http_retry_strategy = retry(
    stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES +1),
    wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=1, max=10),
    retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
    before_sleep=before_sleep_log(task_struct_log, logging.WARNING), 
    reraise=True
)


@worker_process_init.connect(weak=False)
def init_worker_resources(**kwargs):
    global sync_engine, gcs_client_global
    
    init_log_struct = structlog.get_logger("app.tasks.worker_init.struct")
    init_log_std = logging.getLogger("app.tasks.worker_init.std")
    
    init_log_struct.info("Worker process initializing resources (structlog)...", signal="worker_process_init")
    init_log_std.info("Worker process initializing resources (std)...")
    try:
        if sync_engine is None:
            sync_engine = get_sync_engine()
            init_log_struct.info("Synchronous DB engine initialized for worker (structlog).")
            init_log_std.info("Synchronous DB engine initialized for worker (std).")
        else:
             init_log_struct.info("Synchronous DB engine already initialized for worker (structlog).")
             init_log_std.info("Synchronous DB engine already initialized for worker (std).")

        if gcs_client_global is None:
            gcs_client_global = GCSClient() 
            init_log_struct.info("GCS client initialized for worker (structlog).")
            init_log_std.info("GCS client initialized for worker (std).")
        else:
            init_log_struct.info("GCS client already initialized for worker (structlog).")
            init_log_std.info("GCS client already initialized for worker (std).")
        
        init_log_std.info("Worker resources initialization complete (std).")

    except Exception as e:
        init_log_struct.critical("CRITICAL FAILURE during worker resource initialization (structlog)!", error=str(e), exc_info=True)
        init_log_std.critical(f"CRITICAL FAILURE during worker resource initialization (std)! Error: {str(e)}", exc_info=True)
        print(f"CRITICAL WORKER INIT FAILURE (print): {e}", file=sys.stderr, flush=True)
        sync_engine = None
        gcs_client_global = None


@celery_app.task(
    bind=True,
    name="ingest.process_document",
    autoretry_for=(
        httpx.RequestError, 
        httpx.HTTPStatusError, 
        GCSClientError, 
        ConnectionRefusedError,
        Exception 
    ),
    exclude=(
        Reject, Ignore, ValueError, ConnectionError, RuntimeError, TypeError,
    ),
    retry_backoff=True,
    retry_backoff_max=600, 
    retry_jitter=True,
    max_retries=5, 
    acks_late=True
)
def process_document_standalone(self: Task, *args, **kwargs) -> Dict[str, Any]:
    sys.stdout.flush()
    sys.stderr.flush()

    early_task_id = str(self.request.id or uuid.uuid4()) 
    stdlib_task_logger.info(f"--- TASK ENTRY ID: {early_task_id} --- RAW KWARGS: {kwargs}")
    print(f"--- PRINT TASK ENTRY ID: {early_task_id} ---", flush=True)


    document_id_str = kwargs.get('document_id')
    company_id_str = kwargs.get('company_id')
    filename = kwargs.get('filename')
    content_type = kwargs.get('content_type')
    
    attempt = self.request.retries + 1
    max_attempts = (self.max_retries or 0) + 1
    
    log_context = {
        "task_id": early_task_id, "attempt": f"{attempt}/{max_attempts}", "doc_id": document_id_str,
        "company_id": company_id_str, "filename": filename, "content_type": content_type
    }
    log = structlog.get_logger("app.tasks.process_document.task_exec").bind(**log_context)
    
    log.info("Structlog: Payload parsed, task instance bound.")
    stdlib_task_logger.info(f"StdLib: Starting document processing task for doc_id: {document_id_str}, task_id: {early_task_id}, attempt: {attempt}/{max_attempts}")


    if not IS_WORKER:
         err_msg_not_worker = "Task function called outside of a worker context! Rejecting."
         log.critical(err_msg_not_worker)
         stdlib_task_logger.critical(err_msg_not_worker)
         raise Reject(err_msg_not_worker, requeue=False)

    if not all([document_id_str, company_id_str, filename, content_type]):
        err_msg_args = "Missing required arguments (doc_id, company_id, filename, content_type)"
        log.error(err_msg_args, payload_kwargs=kwargs)
        stdlib_task_logger.error(f"{err_msg_args} - Payload: {kwargs}")
        raise Reject(err_msg_args, requeue=False)

    stdlib_task_logger.debug("Checking global resources...")
    global_resources_check = {"Sync DB Engine": sync_engine, "GCS Client": gcs_client_global}
    for name, resource in global_resources_check.items():
        if not resource:
            error_msg_resource = f"Worker resource '{name}' is not initialized. Task {early_task_id} cannot proceed."
            log.critical(error_msg_resource)
            stdlib_task_logger.critical(error_msg_resource)
            if name != "Sync DB Engine" and sync_engine and document_id_str:
                try: 
                    doc_uuid_for_error_res = uuid.UUID(document_id_str)
                    set_status_sync(engine=sync_engine, document_id=doc_uuid_for_error_res, status=DocumentStatus.ERROR, error_message=error_msg_resource)
                except Exception as db_err_res: 
                    crit_msg_db_fail = f"Failed to update status after {name} check failure!"
                    log.critical(crit_msg_db_fail, error=str(db_err_res))
                    stdlib_task_logger.critical(f"{crit_msg_db_fail} Error: {db_err_res}")
            raise Reject(error_msg_resource, requeue=False)
    stdlib_task_logger.debug("Global resources check passed.")

    doc_uuid: uuid.UUID
    try:
        doc_uuid = uuid.UUID(document_id_str)
    except ValueError:
         err_msg_uuid = "Invalid document_id format."
         log.error(err_msg_uuid, received_doc_id=document_id_str)
         stdlib_task_logger.error(f"{err_msg_uuid} - Received: {document_id_str}")
         raise Reject(err_msg_uuid, requeue=False)

    if content_type not in settings.SUPPORTED_CONTENT_TYPES:
        error_msg_content = f"Unsupported content type by ingest-service: {content_type}"
        log.error(error_msg_content)
        stdlib_task_logger.error(error_msg_content)
        if sync_engine: 
             set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_content)
        raise Reject(error_msg_content, requeue=False)

    normalized_filename = normalize_filename(filename) 
    object_name = f"{company_id_str}/{document_id_str}/{normalized_filename}"
    file_bytes: Optional[bytes] = None
    processed_chunks_from_docproc: List[Dict[str, Any]] = []
    
    stdlib_task_logger.info(f"Task {early_task_id}: Pre-processing checks passed. Normalized filename: {normalized_filename}, GCS object: {object_name}")

    try:
        log.info("Attempting to set status to PROCESSING in DB.") 
        stdlib_task_logger.info(f"StdLib: Attempting to set status to PROCESSING for {doc_uuid}")
        status_updated = set_status_sync(
            engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSING, error_message=None
        )
        if not status_updated:
            warn_msg_status = "Failed to update status to PROCESSING (document possibly deleted?). Ignoring task."
            log.warning(warn_msg_status)
            stdlib_task_logger.warning(warn_msg_status)
            raise Ignore()
        log.info("Successfully set status to PROCESSING.")
        stdlib_task_logger.info(f"StdLib: Successfully set status to PROCESSING for {doc_uuid}")


        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = pathlib.Path(temp_dir)
            temp_file_path_obj = temp_dir_path / normalized_filename
            
            log.info(f"Attempting GCS download: {object_name} -> {str(temp_file_path_obj)}")
            stdlib_task_logger.info(f"StdLib: Attempting GCS download: {object_name} -> {str(temp_file_path_obj)}")
            
            gcs_client_global.download_file_sync(object_name, str(temp_file_path_obj))
            
            log.info("File downloaded successfully from GCS.")
            stdlib_task_logger.info(f"StdLib: File downloaded successfully from GCS. Object: {object_name}")
            
            file_bytes = temp_file_path_obj.read_bytes()
            
            log.info(f"File content read into memory ({len(file_bytes)} bytes).")
            stdlib_task_logger.info(f"StdLib: File content read into memory ({len(file_bytes)} bytes). Object: {object_name}")

        if file_bytes is None:
            fb_none_err = "File_bytes is None after GCS download and read, indicating an issue."
            log.error(fb_none_err, object_name=object_name)
            stdlib_task_logger.error(f"StdLib: {fb_none_err}. Object: {object_name}")
            raise RuntimeError(fb_none_err) 


        log.info("Calling Document Processing Service (synchronous)...")
        stdlib_task_logger.info("StdLib: Calling Document Processing Service (synchronous)...")
        docproc_url = str(settings.DOCPROC_SERVICE_URL)
        files_payload = {'file': (normalized_filename, file_bytes, content_type)}
        data_payload = {
            'original_filename': normalized_filename, 
            'content_type': content_type,
            'document_id': document_id_str,
            'company_id': company_id_str
        }
        try:
            with httpx.Client(timeout=settings.HTTP_CLIENT_TIMEOUT) as client:
                @sync_http_retry_strategy
                def call_docproc():
                    log.debug(f"Attempting POST to DocProc: {docproc_url}")
                    stdlib_task_logger.debug(f"StdLib: Attempting POST to DocProc: {docproc_url}")
                    return client.post(docproc_url, files=files_payload, data=data_payload)
                
                response = call_docproc()
                response.raise_for_status() 
                docproc_response_data = response.json()

            if "data" not in docproc_response_data or "chunks" not in docproc_response_data.get("data", {}):
                val_err_msg = "Invalid response format from DocProc Service."
                log.error(val_err_msg, response_data_preview=str(docproc_response_data)[:200])
                stdlib_task_logger.error(f"StdLib: {val_err_msg} - Preview: {str(docproc_response_data)[:200]}")
                raise ValueError(val_err_msg)
            
            processed_chunks_from_docproc = docproc_response_data.get("data", {}).get("chunks", [])
            log.info(f"Received {len(processed_chunks_from_docproc)} chunks from DocProc Service.")
            stdlib_task_logger.info(f"StdLib: Received {len(processed_chunks_from_docproc)} chunks from DocProc.")


        except httpx.HTTPStatusError as hse:
            error_msg_dpce = f"DocProc Error ({hse.response.status_code}): {str(hse.response.text)[:300]}"
            log.error("DocProc Service HTTP Error", status_code=hse.response.status_code, response_text=hse.response.text, exc_info=True)
            stdlib_task_logger.error(f"StdLib: DocProc Service HTTP Error {hse.response.status_code} - {hse.response.text[:100]}", exc_info=True)
            if sync_engine: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_dpce)
            if 500 <= hse.response.status_code < 600: 
                raise 
            else: 
                raise Reject(f"DocProc Service critical error: {error_msg_dpce}", requeue=False) from hse
        except httpx.RequestError as re: 
            req_err_msg = f"DocProc Network Error: {type(re).__name__}"
            log.error("DocProc Service Request Error", error_msg=str(re), exc_info=True)
            stdlib_task_logger.error(f"StdLib: DocProc Service Request Error {type(re).__name__} - {str(re)}", exc_info=True)
            if sync_engine: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=req_err_msg)
            raise


        if not processed_chunks_from_docproc:
            no_chunks_msg = "DocProc Service returned no chunks. Document processing considered complete with 0 chunks."
            log.warning(no_chunks_msg)
            stdlib_task_logger.warning(f"StdLib: {no_chunks_msg}")
            if sync_engine: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSED, chunk_count=0, error_message=None)
            return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": 0, "document_id": document_id_str}

        chunk_texts_for_embedding = [chunk['text'] for chunk in processed_chunks_from_docproc if chunk.get('text','').strip()]
        if not chunk_texts_for_embedding:
            no_text_chunks_msg = "No non-empty text found in chunks received from DocProc. Finishing with 0 chunks."
            log.warning(no_text_chunks_msg)
            stdlib_task_logger.warning(f"StdLib: {no_text_chunks_msg}")
            if sync_engine: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSED, chunk_count=0, error_message=None)
            return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": 0, "document_id": document_id_str}


        log.info(f"Calling Embedding Service for {len(chunk_texts_for_embedding)} texts (synchronous)...")
        stdlib_task_logger.info(f"StdLib: Calling Embedding Service for {len(chunk_texts_for_embedding)} texts...")
        embedding_service_url = str(settings.EMBEDDING_SERVICE_URL)
        
        embedding_request_payload = {
            "texts": chunk_texts_for_embedding,
            "text_type": "passage"
        }
        embeddings: List[List[float]] = []
        try:
            with httpx.Client(timeout=settings.HTTP_CLIENT_TIMEOUT) as client:
                @sync_http_retry_strategy
                def call_embedding_svc():
                    log.debug(f"Attempting POST to EmbeddingSvc: {embedding_service_url}")
                    stdlib_task_logger.debug(f"StdLib: Attempting POST to EmbeddingSvc: {embedding_service_url} with payload: {json.dumps(embedding_request_payload)[:200]}...")
                    return client.post(embedding_service_url, json=embedding_request_payload)

                response_embed = call_embedding_svc()
                response_embed.raise_for_status()
                embedding_response_data = response_embed.json()

            if "embeddings" not in embedding_response_data or "model_info" not in embedding_response_data:
                emb_val_err_msg = "Invalid response format from Embedding Service."
                log.error(emb_val_err_msg, response_data_preview=str(embedding_response_data)[:200])
                stdlib_task_logger.error(f"StdLib: {emb_val_err_msg} - Preview: {str(embedding_response_data)[:200]}")
                raise ValueError(emb_val_err_msg)

            embeddings = embedding_response_data["embeddings"]
            model_info = embedding_response_data["model_info"]
            log.info(f"Embeddings received from service for {len(embeddings)} chunks. Model: {model_info}")
            stdlib_task_logger.info(f"StdLib: Embeddings received for {len(embeddings)} chunks. Model: {model_info}")


            if len(embeddings) != len(chunk_texts_for_embedding):
                emb_count_err = f"Embedding count mismatch. Expected {len(chunk_texts_for_embedding)}, got {len(embeddings)}."
                log.error("Embedding count mismatch from Embedding Service.", expected=len(chunk_texts_for_embedding), received=len(embeddings))
                stdlib_task_logger.error(f"StdLib: {emb_count_err}")
                raise RuntimeError(emb_count_err)
            
            if embeddings and model_info.get("dimension") != settings.EMBEDDING_DIMENSION:
                 emb_dim_err = (f"Embedding dimension from service ({model_info.get('dimension')}) "
                                f"does not match ingest-service's configured EMBEDDING_DIMENSION ({settings.EMBEDDING_DIMENSION}). "
                                "Ensure configurations are aligned across services (embedding-service and ingest-service).")
                 log.error(emb_dim_err, 
                           service_reported_dim=model_info.get('dimension'), 
                           ingest_configured_dim=settings.EMBEDDING_DIMENSION)
                 stdlib_task_logger.error(f"StdLib: {emb_dim_err}")
                 if sync_engine: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=emb_dim_err[:500])
                 raise Reject(emb_dim_err, requeue=False)

        except httpx.HTTPStatusError as hse_embed:
            error_msg_esc = f"Embedding Service Error ({hse_embed.response.status_code}): {str(hse_embed.response.text)[:300]}"
            log.error("Embedding Service HTTP Error", status_code=hse_embed.response.status_code, response_text=hse_embed.response.text, exc_info=True)
            stdlib_task_logger.error(f"StdLib: Embedding Service HTTP Error {hse_embed.response.status_code} - {hse_embed.response.text[:100]}", exc_info=True)
            if sync_engine: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_esc)
            if 500 <= hse_embed.response.status_code < 600:
                raise
            else:
                raise Reject(f"Embedding Service critical error: {error_msg_esc}", requeue=False) from hse_embed
        except httpx.RequestError as re_embed:
            emb_req_err_msg = f"EmbeddingSvc Network Error: {type(re_embed).__name__}"
            log.error("Embedding Service Request Error", error_msg=str(re_embed), exc_info=True)
            stdlib_task_logger.error(f"StdLib: Embedding Service Request Error {type(re_embed).__name__} - {str(re_embed)}", exc_info=True)
            if sync_engine: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=emb_req_err_msg)
            raise

        log.info("Preparing chunks for Milvus and PostgreSQL indexing...")
        stdlib_task_logger.info("StdLib: Preparing chunks for Milvus and PostgreSQL indexing...")
        inserted_milvus_count, milvus_pks, chunks_for_pg_insert = index_chunks_in_milvus_and_prepare_for_pg(
            processed_chunks_from_docproc=[chunk for chunk in processed_chunks_from_docproc if chunk.get('text','').strip()],
            embeddings=embeddings,
            filename=normalized_filename,
            company_id_str=company_id_str,
            document_id_str=document_id_str,
            delete_existing_milvus_chunks=True 
        )
        log.info(f"Milvus indexing complete. Inserted: {inserted_milvus_count}, PKs: {len(milvus_pks)}")
        stdlib_task_logger.info(f"StdLib: Milvus indexing complete. Inserted: {inserted_milvus_count}, PKs: {len(milvus_pks)}")


        if inserted_milvus_count == 0 or not chunks_for_pg_insert:
            zero_milvus_msg = "Milvus indexing resulted in zero inserted chunks or no data for PG. Assuming processing complete with 0 chunks."
            log.warning(zero_milvus_msg)
            stdlib_task_logger.warning(f"StdLib: {zero_milvus_msg}")
            if sync_engine: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSED, chunk_count=0, error_message=None)
            return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": 0, "document_id": document_id_str}

        log.info(f"Attempting bulk insert of {len(chunks_for_pg_insert)} chunks into PostgreSQL.") 
        stdlib_task_logger.info(f"StdLib: Attempting bulk insert of {len(chunks_for_pg_insert)} chunks into PostgreSQL.")
        inserted_pg_count = 0
        try:
            inserted_pg_count = bulk_insert_chunks_sync(engine=sync_engine, chunks_data=chunks_for_pg_insert)
            log.info(f"Successfully bulk inserted {inserted_pg_count} chunks into PostgreSQL.")
            stdlib_task_logger.info(f"StdLib: Successfully bulk inserted {inserted_pg_count} chunks into PostgreSQL.")
            if inserted_pg_count != len(chunks_for_pg_insert):
                 pg_mismatch_warn = "Mismatch between prepared PG chunks and inserted PG count."
                 log.warning(pg_mismatch_warn, prepared=len(chunks_for_pg_insert), inserted=inserted_pg_count)
                 stdlib_task_logger.warning(f"StdLib: {pg_mismatch_warn} - Prepared: {len(chunks_for_pg_insert)}, Inserted: {inserted_pg_count}")
        except Exception as pg_insert_err:
             pg_crit_err_msg = f"PG bulk insert failed: {type(pg_insert_err).__name__}"
             log.critical("CRITICAL: Failed to bulk insert chunks into PostgreSQL after successful Milvus insert!", error=str(pg_insert_err), exc_info=True)
             stdlib_task_logger.critical(f"StdLib: CRITICAL: Failed to bulk insert chunks into PostgreSQL! Error: {pg_insert_err}", exc_info=True)
             log.warning("Attempting to clean up Milvus chunks due to PG insert failure.")
             stdlib_task_logger.warning("StdLib: Attempting to clean up Milvus chunks due to PG insert failure.")
             try: delete_milvus_chunks(company_id=company_id_str, document_id=document_id_str)
             except Exception as cleanup_err: 
                 log.error("Failed to cleanup Milvus chunks after PG failure.", cleanup_error=str(cleanup_err))
                 stdlib_task_logger.error(f"StdLib: Failed to cleanup Milvus chunks after PG failure. Error: {cleanup_err}")
             if sync_engine: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=pg_crit_err_msg[:500])
             raise Reject(f"PostgreSQL bulk insert failed: {pg_insert_err}", requeue=False) from pg_insert_err

        log.info("Setting final status to PROCESSED in DB.") 
        stdlib_task_logger.info("StdLib: Setting final status to PROCESSED in DB.")
        if sync_engine: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSED, chunk_count=inserted_pg_count, error_message=None)
        
        final_success_msg = f"Document processing finished successfully. Final chunk count (PG): {inserted_pg_count}"
        log.info(final_success_msg)
        stdlib_task_logger.info(f"StdLib: {final_success_msg}")
        return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": inserted_pg_count, "document_id": document_id_str}

    except GCSClientError as gce:
        gcs_err_msg_task = f"GCS Error: {str(gce)[:400]}"
        log.error(f"GCS Error during processing task {early_task_id}", error_msg=str(gce), exc_info=True)
        stdlib_task_logger.error(f"StdLib: GCS Error during processing task {early_task_id} - {str(gce)}", exc_info=True)
        if sync_engine and 'doc_uuid' in locals(): 
             set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=gcs_err_msg_task)
        if "Object not found" in str(gce): 
            raise Reject(f"GCS Error: Object not found: {object_name}", requeue=False) from gce
        raise 

    except (ValueError, RuntimeError, TypeError) as non_retriable_err: 
         error_msg_pipe = f"Pipeline/Data Error: {type(non_retriable_err).__name__} - {str(non_retriable_err)[:400]}"
         log.error(f"Non-retriable Error in task {early_task_id}: {non_retriable_err}", exc_info=True)
         stdlib_task_logger.error(f"StdLib: Non-retriable Error in task {early_task_id}: {type(non_retriable_err).__name__} - {str(non_retriable_err)}", exc_info=True)
         if sync_engine and 'doc_uuid' in locals():
             set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_pipe)
         raise Reject(f"Pipeline failed for task {early_task_id}: {error_msg_pipe}", requeue=False) from non_retriable_err
    
    except Reject as r: 
         reject_reason = getattr(r, 'reason', 'Unknown reason')
         log.error(f"Task {early_task_id} rejected permanently: {reject_reason}")
         stdlib_task_logger.error(f"StdLib: Task {early_task_id} rejected permanently: {reject_reason}")
         if sync_engine and 'doc_uuid' in locals() and reject_reason: 
             set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=f"Rejected: {str(reject_reason)[:500]}")
         raise 
    except Ignore: 
         log.info(f"Task {early_task_id} ignored.")
         stdlib_task_logger.info(f"StdLib: Task {early_task_id} ignored.")
         raise 
    except Retry as retry_exc: 
        log.warning(f"Task {early_task_id} is being retried by Celery. Reason: {retry_exc}", exc_info=False) 
        stdlib_task_logger.warning(f"StdLib: Task {early_task_id} is being retried by Celery. Reason: {retry_exc}")
        raise 
    except MaxRetriesExceededError as mree:
        final_error = mree.cause if mree.cause else mree
        error_msg_mree = f"Max retries exceeded for task {early_task_id} ({max_attempts}). Last error: {type(final_error).__name__} - {str(final_error)[:300]}"
        log.error(error_msg_mree, exc_info=True, cause_type=type(final_error).__name__, cause_message=str(final_error))
        stdlib_task_logger.error(f"StdLib: {error_msg_mree}", exc_info=True)
        if sync_engine and 'doc_uuid' in locals(): 
            set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_mree)
        self.update_state(state=states.FAILURE, meta={'exc_type': type(final_error).__name__, 'exc_message': str(final_error)})
    except Exception as exc: 
        error_msg_exc_for_db = f"Attempt {attempt} failed: {type(exc).__name__} - {str(exc)[:200]}"
        log.exception(f"An unexpected error occurred in task {early_task_id}, attempting Celery retry if possible.")
        stdlib_task_logger.exception(f"StdLib: An unexpected error occurred in task {early_task_id}.")
        if sync_engine and 'doc_uuid' in locals(): 
            set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSING, error_message=error_msg_exc_for_db) 
        raise 
    finally:
        stdlib_task_logger.info(f"--- TASK EXIT: {early_task_id} --- Attempt: {attempt}/{max_attempts}")
        print(f"--- PRINT TASK EXIT: {early_task_id} --- Attempt: {attempt}/{max_attempts}", flush=True)
        sys.stdout.flush()
        sys.stderr.flush()