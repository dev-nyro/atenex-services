# ingest-service/app/tasks/process_document.py
def normalize_filename(filename: str) -> str:
    """Normaliza el nombre de archivo eliminando espacios al inicio/final y espacios duplicados."""
    return " ".join(filename.strip().split())

import os
import tempfile
import uuid
import sys
import pathlib
import time
import json
from typing import Optional, Dict, Any, List

import structlog
import httpx # Importar httpx síncrono
from celery import Task, states
from celery.exceptions import Ignore, Reject, MaxRetriesExceededError, Retry
from celery.signals import worker_process_init
from sqlalchemy import Engine
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, before_sleep_log
import logging


# Ya no se usarán los clientes asíncronos globales para las llamadas HTTP desde esta tarea.
# from app.services.clients.embedding_service_client import EmbeddingServiceClient, EmbeddingServiceClientError
# from app.services.clients.docproc_service_client import DocProcServiceClient, DocProcServiceClientError

from app.core.config import settings
from app.db.postgres_client import get_sync_engine, set_status_sync, bulk_insert_chunks_sync
from app.models.domain import DocumentStatus
from app.services.gcs_client import GCSClient, GCSClientError
from app.services.ingest_pipeline import (
    index_chunks_in_milvus_and_prepare_for_pg,
    delete_milvus_chunks
)
from app.tasks.celery_app import celery_app
# asyncio ya no es necesario para las llamadas HTTP en esta tarea
# import asyncio

task_struct_log = structlog.get_logger(__name__)
IS_WORKER = "worker" in sys.argv

# --- Global Resources for Worker Process (Solo los que siguen siendo necesarios globalmente) ---
sync_engine: Optional[Engine] = None
gcs_client_global: Optional[GCSClient] = None
# embedding_service_client_global: Optional[EmbeddingServiceClient] = None # No se usará aquí
# docproc_service_client_global: Optional[DocProcServiceClient] = None # No se usará aquí

# --- Retry Decorator for HTTP Client Calls (Synchronous) ---
sync_http_retry_strategy = retry(
    stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES +1), # settings.HTTP_CLIENT_MAX_RETRIES es el numero de reintentos
    wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=1, max=10),
    retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
    before_sleep=before_sleep_log(task_struct_log, logging.WARNING),
    reraise=True
)


@worker_process_init.connect(weak=False)
def init_worker_resources(**kwargs):
    global sync_engine, gcs_client_global
    log = task_struct_log.bind(signal="worker_process_init")
    log.info("Worker process initializing resources...")
    try:
        if sync_engine is None:
            sync_engine = get_sync_engine()
            log.info("Synchronous DB engine initialized for worker.")
        else:
             log.info("Synchronous DB engine already initialized for worker.")

        if gcs_client_global is None:
            gcs_client_global = GCSClient()
            log.info("GCS client initialized for worker.")
        else:
            log.info("GCS client already initialized for worker.")

        # No inicializar clientes de servicio asíncronos aquí si no se usan globalmente
        # if embedding_service_client_global is None: ...
        # if docproc_service_client_global is None: ...

    except Exception as e:
        log.critical("CRITICAL FAILURE during worker resource initialization!", error=str(e), exc_info=True)
        sync_engine = None
        gcs_client_global = None


# run_async_from_sync ya no es necesaria para las llamadas HTTP en esta tarea.
# def run_async_from_sync(awaitable): ...


@celery_app.task(
    bind=True,
    name="ingest.process_document",
    autoretry_for=(
        # Quitar EmbeddingServiceClientError, DocProcServiceClientError si no se usan más
        httpx.RequestError, 
        httpx.HTTPStatusError, # Reintentar errores 5xx de servicios externos
        GCSClientError,
        ConnectionRefusedError,
        # asyncio.TimeoutError, # Ya no se usa asyncio directamente para HTTP
        Exception # Mantener Exception para errores inesperados y reintentos generales
    ),
    exclude=(
        Reject, Ignore, ValueError, ConnectionError, RuntimeError, TypeError,
        # Las excepciones de cliente (4xx) de los servicios externos se manejarán
        # para no reintentar indefinidamente.
    ),
    retry_backoff=True,
    retry_backoff_max=600, # 10 minutos
    retry_jitter=True,
    max_retries=5, 
    acks_late=True
)
def process_document_standalone(self: Task, *args, **kwargs) -> Dict[str, Any]:
    document_id_str = kwargs.get('document_id')
    company_id_str = kwargs.get('company_id')
    filename = kwargs.get('filename')
    content_type = kwargs.get('content_type')

    task_id = self.request.id or "unknown_task_id"
    attempt = self.request.retries + 1
    max_attempts = (self.max_retries or 0) + 1
    log_context = {
        "task_id": task_id, "attempt": f"{attempt}/{max_attempts}", "doc_id": document_id_str,
        "company_id": company_id_str, "filename": filename, "content_type": content_type
    }
    log = task_struct_log.bind(**log_context)
    log.info("Starting document processing task")

    if not IS_WORKER:
         log.critical("Task function called outside of a worker context! Rejecting.")
         raise Reject("Task running outside worker context.", requeue=False)

    if not all([document_id_str, company_id_str, filename, content_type]):
        log.error("Missing required arguments in task payload.", payload_kwargs=kwargs)
        raise Reject("Missing required arguments (doc_id, company_id, filename, content_type)", requeue=False)

    # Verificar solo los recursos globales que se siguen usando
    global_resources_check = {"Sync DB Engine": sync_engine, "GCS Client": gcs_client_global}
    for name, resource in global_resources_check.items():
        if not resource:
            log.critical(f"Worker resource '{name}' is not initialized. Task cannot proceed.")
            error_msg = f"Worker resource '{name}' initialization failed."
            if name != "Sync DB Engine" and sync_engine:
                try: 
                    doc_uuid_for_error_res = uuid.UUID(document_id_str)
                    set_status_sync(engine=sync_engine, document_id=doc_uuid_for_error_res, status=DocumentStatus.ERROR, error_message=error_msg)
                except Exception as db_err_res: log.critical(f"Failed to update status after {name} check failure!", error=str(db_err_res))
            raise Reject(error_msg, requeue=False)

    try:
        doc_uuid = uuid.UUID(document_id_str)
    except ValueError:
         log.error("Invalid document_id format received.")
         raise Reject("Invalid document_id format.", requeue=False)

    if content_type not in settings.SUPPORTED_CONTENT_TYPES:
        log.error(f"Unsupported content type: {content_type}")
        error_msg = f"Unsupported content type by ingest-service: {content_type}"
        set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
        raise Reject(error_msg, requeue=False)

    normalized_filename = normalize_filename(filename) if filename else filename
    object_name = f"{company_id_str}/{document_id_str}/{normalized_filename}"
    file_bytes: Optional[bytes] = None
    processed_chunks_from_docproc: List[Dict[str, Any]] = []

    try:
        log.debug("Setting status to PROCESSING in DB.")
        status_updated = set_status_sync(
            engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSING, error_message=None
        )
        if not status_updated:
            log.warning("Failed to update status to PROCESSING (document possibly deleted?). Ignoring task.")
            raise Ignore()
        log.info("Status set to PROCESSING.")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = pathlib.Path(temp_dir)
            temp_file_path_obj = temp_dir_path / normalized_filename
            log.info(f"Downloading GCS object: {object_name} -> {str(temp_file_path_obj)}")
            gcs_client_global.download_file_sync(object_name, str(temp_file_path_obj)) # GCSClientError será manejada por el except externo
            log.info("File downloaded successfully from GCS.")
            file_bytes = temp_file_path_obj.read_bytes()
            log.info(f"File content read into memory ({len(file_bytes)} bytes).")

        # 2. Llamar al DocProc Service (Síncrono)
        log.info("Calling Document Processing Service (synchronous)...")
        docproc_url = str(settings.INGEST_DOCPROC_SERVICE_URL)
        files_payload = {'file': (normalized_filename, file_bytes, content_type)}
        data_payload = {
            'original_filename': normalized_filename,
            'content_type': content_type,
            'document_id': document_id_str,
            'company_id': company_id_str
        }
        try:
            with httpx.Client(timeout=settings.HTTP_CLIENT_TIMEOUT) as client:
                # Aplicar reintentos manualmente para la llamada síncrona
                @sync_http_retry_strategy
                def call_docproc():
                    log.debug(f"Attempting POST to DocProc: {docproc_url}")
                    return client.post(docproc_url, files=files_payload, data=data_payload)
                
                response = call_docproc()
                response.raise_for_status() # Lanza HTTPStatusError para 4xx/5xx
                docproc_response_data = response.json()

            if "data" not in docproc_response_data or "chunks" not in docproc_response_data.get("data", {}):
                log.error("Invalid response format from DocProc Service", response_data_preview=str(docproc_response_data)[:200])
                raise ValueError("Invalid response format from DocProc Service.") # Será capturado por el except ValueError abajo
            
            processed_chunks_from_docproc = docproc_response_data.get("data", {}).get("chunks", [])
            log.info(f"Received {len(processed_chunks_from_docproc)} chunks from DocProc Service.")

        except httpx.HTTPStatusError as hse:
            log.error("DocProc Service HTTP Error", status_code=hse.response.status_code, response_text=hse.response.text, exc_info=True)
            error_msg_dpce = f"DocProc Error ({hse.response.status_code}): {str(hse.response.text)[:300]}"
            set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_dpce)
            if 500 <= hse.response.status_code < 600: # Server-side errors en DocProc
                raise # Dejar que Celery maneje el reintento basado en HTTPStatusError
            else: # Client-side errors (4xx) o errores no recuperables de DocProc
                raise Reject(f"DocProc Service critical error: {error_msg_dpce}", requeue=False) from hse
        except httpx.RequestError as re: # Errores de red, timeouts, etc.
            log.error("DocProc Service Request Error", error_msg=str(re), exc_info=True)
            set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=f"DocProc Network Error: {type(re).__name__}")
            raise # Dejar que Celery maneje el reintento


        if not processed_chunks_from_docproc:
            log.warning("DocProc Service returned no chunks. Document processing considered complete with 0 chunks.")
            set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSED, chunk_count=0, error_message=None)
            return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": 0, "document_id": document_id_str}

        chunk_texts_for_embedding = [chunk['text'] for chunk in processed_chunks_from_docproc if chunk.get('text','').strip()]
        if not chunk_texts_for_embedding:
            log.warning("No non-empty text found in chunks received from DocProc. Finishing with 0 chunks.")
            set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSED, chunk_count=0, error_message=None)
            return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": 0, "document_id": document_id_str}

        # 4. Llamar al Embedding Service (Síncrono)
        log.info(f"Calling Embedding Service for {len(chunk_texts_for_embedding)} texts (synchronous)...")
        embedding_service_url = str(settings.INGEST_EMBEDDING_SERVICE_URL)
        embedding_request_payload = {"texts": chunk_texts_for_embedding}
        embeddings: List[List[float]] = []
        try:
            with httpx.Client(timeout=settings.HTTP_CLIENT_TIMEOUT) as client:
                @sync_http_retry_strategy
                def call_embedding_svc():
                    log.debug(f"Attempting POST to EmbeddingSvc: {embedding_service_url}")
                    return client.post(embedding_service_url, json=embedding_request_payload)

                response_embed = call_embedding_svc()
                response_embed.raise_for_status()
                embedding_response_data = response_embed.json()

            if "embeddings" not in embedding_response_data or "model_info" not in embedding_response_data:
                log.error("Invalid response format from Embedding Service", response_data_preview=str(embedding_response_data)[:200])
                raise ValueError("Invalid response format from Embedding Service.")

            embeddings = embedding_response_data["embeddings"]
            model_info = embedding_response_data["model_info"]
            log.info(f"Embeddings received from service for {len(embeddings)} chunks. Model: {model_info}")

            if len(embeddings) != len(chunk_texts_for_embedding):
                log.error("Embedding count mismatch from Embedding Service.", expected=len(chunk_texts_for_embedding), received=len(embeddings))
                raise RuntimeError(f"Embedding count mismatch. Expected {len(chunk_texts_for_embedding)}, got {len(embeddings)}.")
            if embeddings and settings.EMBEDDING_DIMENSION > 0 and len(embeddings[0]) != settings.EMBEDDING_DIMENSION:
                 log.error(f"Received embedding dimension ({len(embeddings[0])}) from service does not match configured Milvus dimension ({settings.EMBEDDING_DIMENSION}).")
                 raise RuntimeError(f"Embedding dimension mismatch. Expected {settings.EMBEDDING_DIMENSION}, got {len(embeddings[0])}")
        except httpx.HTTPStatusError as hse_embed:
            log.error("Embedding Service HTTP Error", status_code=hse_embed.response.status_code, response_text=hse_embed.response.text, exc_info=True)
            error_msg_esc = f"Embedding Service Error ({hse_embed.response.status_code}): {str(hse_embed.response.text)[:300]}"
            set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_esc)
            if 500 <= hse_embed.response.status_code < 600:
                raise
            else:
                raise Reject(f"Embedding Service critical error: {error_msg_esc}", requeue=False) from hse_embed
        except httpx.RequestError as re_embed:
            log.error("Embedding Service Request Error", error_msg=str(re_embed), exc_info=True)
            set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=f"EmbeddingSvc Network Error: {type(re_embed).__name__}")
            raise

        # 5. Indexar en Milvus y preparar para PostgreSQL (Sin cambios en esta función interna)
        log.info("Preparing chunks for Milvus and PostgreSQL indexing...")
        inserted_milvus_count, milvus_pks, chunks_for_pg_insert = index_chunks_in_milvus_and_prepare_for_pg(
            processed_chunks_from_docproc=[chunk for chunk in processed_chunks_from_docproc if chunk.get('text','').strip()],
            embeddings=embeddings,
            filename=normalized_filename,
            company_id_str=company_id_str,
            document_id_str=document_id_str,
            delete_existing_milvus_chunks=True
        )
        log.info(f"Milvus indexing complete. Inserted: {inserted_milvus_count}, PKs: {len(milvus_pks)}")

        if inserted_milvus_count == 0 or not chunks_for_pg_insert:
            log.warning("Milvus indexing resulted in zero inserted chunks or no data for PG. Assuming processing complete with 0 chunks.")
            set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSED, chunk_count=0, error_message=None)
            return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": 0, "document_id": document_id_str}

        # 6. Indexar en PostgreSQL (Sin cambios)
        log.debug(f"Attempting bulk insert of {len(chunks_for_pg_insert)} chunks into PostgreSQL.")
        inserted_pg_count = 0
        try:
            inserted_pg_count = bulk_insert_chunks_sync(engine=sync_engine, chunks_data=chunks_for_pg_insert)
            log.info(f"Successfully bulk inserted {inserted_pg_count} chunks into PostgreSQL.")
            if inserted_pg_count != len(chunks_for_pg_insert):
                 log.warning("Mismatch between prepared PG chunks and inserted PG count.", prepared=len(chunks_for_pg_insert), inserted=inserted_pg_count)
        except Exception as pg_insert_err:
             log.critical("CRITICAL: Failed to bulk insert chunks into PostgreSQL after successful Milvus insert!", error=str(pg_insert_err), exc_info=True)
             log.warning("Attempting to clean up Milvus chunks due to PG insert failure.")
             try: delete_milvus_chunks(company_id=company_id_str, document_id=document_id_str)
             except Exception as cleanup_err: log.error("Failed to cleanup Milvus chunks after PG failure.", cleanup_error=str(cleanup_err))
             error_msg_pg_fail = f"PG bulk insert failed: {type(pg_insert_err).__name__}"
             set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_pg_fail[:500])
             raise Reject(f"PostgreSQL bulk insert failed: {pg_insert_err}", requeue=False) from pg_insert_err

        # 7. Actualización de Estado Final (Sin cambios)
        log.debug("Setting final status to PROCESSED in DB.")
        set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSED, chunk_count=inserted_pg_count, error_message=None)
        log.info(f"Document processing finished successfully. Final chunk count (PG): {inserted_pg_count}")
        return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": inserted_pg_count, "document_id": document_id_str}

    except GCSClientError as gce:
        log.error(f"GCS Error during processing", error_msg=str(gce), exc_info=True)
        error_msg_gcs = f"GCS Error: {str(gce)[:400]}"
        set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_gcs)
        if "Object not found" in str(gce):
            raise Reject(f"GCS Error: Object not found: {object_name}", requeue=False) from gce
        raise 

    except (ValueError, RuntimeError, TypeError) as non_retriable_err:
         log.error(f"Non-retriable Error: {non_retriable_err}", exc_info=True)
         error_msg_pipe = f"Pipeline/Data Error: {type(non_retriable_err).__name__} - {str(non_retriable_err)[:400]}"
         set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_pipe)
         raise Reject(f"Pipeline failed: {error_msg_pipe}", requeue=False) from non_retriable_err

    except Reject as r:
         log.error(f"Task rejected permanently: {r.reason}")
         if sync_engine and doc_uuid and r.reason: 
             set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=f"Rejected: {str(r.reason)[:500]}")
         raise 
    except Ignore:
         log.info("Task ignored.")
         raise 
    except Retry as retry_exc:
        log.warning(f"Task is being retried by Celery. Reason: {retry_exc}", exc_info=False)
        raise 
    except MaxRetriesExceededError as mree:
        log.error("Max retries exceeded for task.", exc_info=True, cause_type=type(mree.cause).__name__, cause_message=str(mree.cause))
        final_error = mree.cause if mree.cause else mree
        error_msg_mree = f"Max retries exceeded ({max_attempts}). Last error: {type(final_error).__name__} - {str(final_error)[:300]}"
        set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_mree)
        self.update_state(state=states.FAILURE, meta={'exc_type': type(final_error).__name__, 'exc_message': str(final_error)})
    except Exception as exc: 
        log.exception(f"An unexpected error occurred, attempting Celery retry if possible.")
        # Para errores genéricos, actualizamos el estado en DB para reflejar el problema durante el reintento
        error_msg_exc_for_db = f"Attempt {attempt} failed: {type(exc).__name__}"
        set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSING, error_message=error_msg_exc_for_db[:500]) # PROCESSING para reintento
        raise # Dejar que Celery maneje el reintento basado en autoretry_for