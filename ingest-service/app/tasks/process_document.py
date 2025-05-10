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
from celery import Task, states
from celery.exceptions import Ignore, Reject, MaxRetriesExceededError, Retry
from celery.signals import worker_process_init
from sqlalchemy import Engine

# Embedding components - CLIENT
from app.services.clients.embedding_service_client import EmbeddingServiceClient, EmbeddingServiceClientError

# Custom Application Imports
from app.core.config import settings
from app.db.postgres_client import get_sync_engine, set_status_sync, bulk_insert_chunks_sync
from app.models.domain import DocumentStatus, DocumentChunkData, ChunkVectorStatus
from app.services.gcs_client import GCSClient, GCSClientError
from app.services.ingest_pipeline import (
    ingest_document_pipeline,
    EXTRACTORS,
    EXTRACTION_ERRORS,
    delete_milvus_chunks
)
from app.tasks.celery_app import celery_app
import asyncio # Para ejecutar funciones async desde sync

task_struct_log = structlog.get_logger(__name__)
IS_WORKER = "worker" in sys.argv

# --- Global Resources for Worker Process ---
sync_engine: Optional[Engine] = None
gcs_client: Optional[GCSClient] = None
# worker_embedding_model: Optional[SentenceTransformer] = None # Eliminado
embedding_service_client_global: Optional[EmbeddingServiceClient] = None # Nuevo


@worker_process_init.connect(weak=False)
def init_worker_resources(**kwargs):
    global sync_engine, gcs_client, embedding_service_client_global # Modificado
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

        # if worker_embedding_model is None: # Eliminado
        #     log.info("Preloading embedding model...")
        #     worker_embedding_model = get_embedding_model()
        #     log.info("Embedding model preloaded.")
        # else:
        #     log.info("Embedding model already preloaded.")

        if embedding_service_client_global is None: # Nuevo
            log.info("Initializing Embedding Service client...")
            # La URL se toma de settings directamente en el cliente
            embedding_service_client_global = EmbeddingServiceClient()
            # Realizar un health check inicial al servicio de embedding
            # loop = asyncio.get_event_loop() # No se puede usar get_event_loop en un hilo no principal de asyncio
            # if not loop.run_until_complete(embedding_service_client_global.health_check()):
            #    log.critical("Embedding Service health check FAILED during worker init. Client may not function.")
            # else:
            #    log.info("Embedding Service client initialized and health check successful.")
            # Simplificado: No hacer health check aquí para evitar problemas con event loop en worker init.
            # Se hará un health check implícito al primer uso o se confía en la resiliencia.
            log.info("Embedding Service client initialized (health check deferred to first use or task).")

        else:
            log.info("Embedding Service client already initialized.")


    except Exception as e:
        log.critical("CRITICAL FAILURE during worker resource initialization!", error=str(e), exc_info=True)
        sync_engine = None
        gcs_client = None
        # worker_embedding_model = None # Eliminado
        embedding_service_client_global = None # Nuevo


def run_async_from_sync(awaitable):
    """Ejecuta una corutina desde código síncrono (Celery task)."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError: # No current event loop in thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(awaitable)


@celery_app.task(
    bind=True,
    name="ingest.process_document",
    autoretry_for=(EmbeddingServiceClientError, httpx.RequestError, httpx.HTTPStatusError, Exception), # Añadir errores de cliente
    exclude=(Reject, Ignore, ValueError, ConnectionError, RuntimeError, TypeError, *EXTRACTION_ERRORS),
    retry_backoff=True,
    retry_backoff_max=600, # 10 minutos
    retry_jitter=True,
    max_retries=5, # Aumentado para el servicio externo
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
    log = task_struct_log.bind(
        task_id=task_id, attempt=f"{attempt}/{max_attempts}", doc_id=document_id_str,
        company_id=company_id_str, filename=filename, content_type=content_type
    )
    log.info("Starting document processing task (uses Embedding Service)")

    if not IS_WORKER:
         log.critical("Task function called outside of a worker context! Rejecting.")
         raise Reject("Task running outside worker context.", requeue=False)

    if not all([document_id_str, company_id_str, filename, content_type]):
        log.error("Missing required arguments in task payload.", payload_kwargs=kwargs)
        raise Reject("Missing required arguments (doc_id, company_id, filename, content_type)", requeue=False)

    # --- Validar recursos globales del worker ---
    if not sync_engine:
         log.critical("Worker Sync DB Engine is not initialized. Task cannot proceed.")
         raise Reject("Worker sync DB engine initialization failed.", requeue=False) # No reintentar
    # if not worker_embedding_model: # Eliminado
    #      log.critical("Worker Embedding Model is not available/loaded. Task cannot proceed.")
    #      # ...
    #      raise Reject(error_msg, requeue=False)
    if not embedding_service_client_global: # Nuevo
        log.critical("Worker Embedding Service Client is not initialized. Task cannot proceed.")
        error_msg = "Worker Embedding Service Client init failed."
        doc_uuid_err_esc = None
        try: doc_uuid_err_esc = uuid.UUID(document_id_str)
        except ValueError: pass
        if doc_uuid_err_esc and sync_engine:
            try: set_status_sync(engine=sync_engine, document_id=doc_uuid_err_esc, status=DocumentStatus.ERROR, error_message=error_msg)
            except Exception as db_err: log.critical("Failed to update status after ESC client check failure!", error=str(db_err))
        raise Reject(error_msg, requeue=False) # No reintentar este error de infraestructura

    if not gcs_client:
         log.critical("Worker GCS Client is not initialized. Task cannot proceed.")
         error_msg = "Worker GCS client init failed."
         doc_uuid_err_gcs = None
         try: doc_uuid_err_gcs = uuid.UUID(document_id_str)
         except ValueError: pass
         if doc_uuid_err_gcs and sync_engine:
              try: set_status_sync(engine=sync_engine, document_id=doc_uuid_err_gcs, status=DocumentStatus.ERROR, error_message=error_msg)
              except Exception as db_err: log.critical("Failed to update status after GCS client check failure!", error=str(db_err))
         raise Reject(error_msg, requeue=False) # No reintentar

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
    processed_chunks_data: List[DocumentChunkData] = []

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
            max_gcs_retries = 2; gcs_delay = 2; download_success = False
            for attempt_num in range(1, max_gcs_retries + 1):
                try:
                    gcs_client.download_file_sync(object_name, str(temp_file_path_obj))
                    log.info("File downloaded successfully from GCS.")
                    file_bytes = temp_file_path_obj.read_bytes()
                    log.info(f"File content read into memory ({len(file_bytes)} bytes).")
                    download_success = True; break
                except GCSClientError as gce_dl:
                    if "Object not found" in str(gce_dl) or (hasattr(gce_dl, 'original_exception') and isinstance(gce_dl.original_exception, FileNotFoundError)):
                        log.warning(f"GCS object not found on download attempt {attempt_num}/{max_gcs_retries}. Retrying in {gcs_delay}s...", error=str(gce_dl))
                        if attempt_num == max_gcs_retries: log.error(f"File still not found in GCS after {max_gcs_retries} attempts. Aborting."); raise
                        time.sleep(gcs_delay); gcs_delay *= 2
                    else: log.error("Non-retriable GCS error during download", error=str(gce_dl)); raise
                except Exception as read_err:
                    log.error("Failed to read downloaded file into bytes", error=str(read_err), exc_info=True)
                    raise RuntimeError(f"Failed to read temp file: {read_err}") from read_err
            if not download_success or file_bytes is None:
                 raise RuntimeError("File download or read failed, bytes not available.")

        log.info("Executing ingest pipeline (extract, chunk, metadata, embed via service, Milvus insert)...")
        # Ejecutar la función async del pipeline desde el contexto sync de Celery
        inserted_milvus_count, milvus_pks, processed_chunks_data = run_async_from_sync(
            ingest_document_pipeline(
                file_bytes=file_bytes,
                filename=normalized_filename,
                company_id=company_id_str,
                document_id=document_id_str,
                content_type=content_type,
                embedding_service_client=embedding_service_client_global, # Pasamos el cliente
                delete_existing=True
            )
        )
        log.info(f"Ingestion pipeline finished. Milvus insert count: {inserted_milvus_count}, PKs returned: {len(milvus_pks)}")

        if inserted_milvus_count == 0 or not processed_chunks_data:
             log.warning("Ingestion pipeline reported zero inserted chunks or no data. Assuming processing complete with 0 chunks.")
             final_status_updated_zero = set_status_sync(
                 engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSED,
                 chunk_count=0, error_message=None
             )
             return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": 0, "document_id": document_id_str}

        log.info(f"Preparing {len(processed_chunks_data)} chunks for PostgreSQL bulk insert...")
        chunks_for_pg = []
        for chunk_obj in processed_chunks_data:
            if chunk_obj.embedding_id is None:
                log.warning("Chunk missing embedding_id after Milvus insert, skipping PG insert for this chunk.", chunk_index=chunk_obj.chunk_index)
                continue
            chunk_dict = {
                'document_id': chunk_obj.document_id, 'company_id': chunk_obj.company_id,
                'chunk_index': chunk_obj.chunk_index, 'content': chunk_obj.content,
                'metadata': chunk_obj.metadata.model_dump(mode='json'),
                'embedding_id': chunk_obj.embedding_id, 'vector_status': chunk_obj.vector_status.value
            }
            chunks_for_pg.append(chunk_dict)

        if not chunks_for_pg:
             log.error("No valid chunks prepared for PostgreSQL insertion after Milvus step. Marking document as error.")
             error_msg_pg_prep = "Milvus insertion succeeded but no valid data for PG."
             set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_pg_prep)
             raise Reject(error_msg_pg_prep, requeue=False)

        log.debug(f"Attempting bulk insert of {len(chunks_for_pg)} chunks into PostgreSQL.")
        try:
            inserted_pg_count = bulk_insert_chunks_sync(engine=sync_engine, chunks_data=chunks_for_pg)
            log.info(f"Successfully bulk inserted {inserted_pg_count} chunks into PostgreSQL.")
            if inserted_pg_count != len(chunks_for_pg):
                 log.warning("Mismatch between prepared PG chunks and inserted PG count.", prepared=len(chunks_for_pg), inserted=inserted_pg_count)
        except Exception as pg_insert_err:
             log.critical("CRITICAL: Failed to bulk insert chunks into PostgreSQL after successful Milvus insert!", error=str(pg_insert_err), exc_info=True)
             log.warning("Attempting to clean up Milvus chunks due to PG insert failure.")
             try: delete_milvus_chunks(company_id=company_id_str, document_id=document_id_str)
             except Exception as cleanup_err: log.error("Failed to cleanup Milvus chunks after PG failure.", cleanup_error=str(cleanup_err))
             error_msg_pg_fail = f"PG bulk insert failed: {type(pg_insert_err).__name__}"
             set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_pg_fail[:500])
             raise Reject(f"PostgreSQL bulk insert failed: {pg_insert_err}", requeue=False) from pg_insert_err

        log.debug("Setting final status to PROCESSED in DB.")
        final_status_updated_ok = set_status_sync(
            engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSED,
            chunk_count=inserted_pg_count, error_message=None
        )
        if not final_status_updated_ok:
             log.warning("Failed to update final status to PROCESSED (document possibly deleted?).")

        log.info(f"Document processing finished successfully. Final chunk count (PG): {inserted_pg_count}")
        return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": inserted_pg_count, "document_id": document_id_str}

    except GCSClientError as gce:
        log.error(f"GCS Error during processing", error=str(gce), exc_info=True)
        error_msg_gcs = f"GCS Error: {str(gce)[:400]}"
        set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_gcs)
        if "Object not found" in str(gce): # No reintentar si el objeto no existe
            raise Reject(f"GCS Error: Object not found: {object_name}", requeue=False) from gce
        # Dejar que Celery reintente otros errores de GCS según la configuración de la tarea
        raise self.retry(exc=gce, countdown=int(self.default_retry_delay * (self.request.retries + 1)))

    except EmbeddingServiceClientError as esce: # Capturar errores del cliente de embedding
        log.error("Embedding Service Client Error", error=str(esce), status_code=esce.status_code, details=esce.detail, exc_info=True)
        error_msg_esc = f"Embedding Service Error: {str(esce)[:350]}"
        set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_esc)
        # Reintentar si es un error de servidor (5xx) o de red, no si es un error de cliente (4xx)
        if esce.status_code and 500 <= esce.status_code < 600:
            raise self.retry(exc=esce, countdown=int(self.default_retry_delay * (self.request.retries + 1)))
        else: # Errores 4xx o sin status_code (errores de conexión) no suelen ser recuperables con reintentos simples
            raise Reject(f"Embedding Service critical error: {error_msg_esc}", requeue=False) from esce

    except (*EXTRACTION_ERRORS, ValueError, RuntimeError, TypeError) as pipeline_err:
         log.error(f"Non-retriable Pipeline/Data Error: {pipeline_err}", exc_info=True)
         error_msg_pipe = f"Pipeline Error: {type(pipeline_err).__name__} - {str(pipeline_err)[:400]}"
         set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_pipe)
         raise Reject(f"Pipeline failed: {error_msg_pipe}", requeue=False) from pipeline_err

    except Reject as r:
         log.error(f"Task rejected permanently: {r.reason}")
         if sync_engine and doc_uuid and r.reason: # Asegurarse que reason no sea None
             set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=f"Rejected: {str(r.reason)[:500]}")
         raise # Propagate Reject

    except Ignore:
         log.info("Task ignored.")
         raise # Propagate Ignore

    except Retry as retry_exc: # Captura explícita de la excepción Retry
        log.warning(f"Task is being retried. Reason: {retry_exc}", exc_info=True)
        # No actualizar el estado a ERROR aquí, ya que Celery lo está reintentando.
        # El estado 'processing' es apropiado durante los reintentos.
        raise # Re-elevar para que Celery maneje el reintento

    except MaxRetriesExceededError as mree:
        log.error("Max retries exceeded for task.", exc_info=True)
        final_error = mree.cause if mree.cause else mree
        error_msg_mree = f"Max retries exceeded ({max_attempts}). Last error: {type(final_error).__name__} - {str(final_error)[:300]}"
        set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_mree)
        # Celery ya maneja esto y no lo reenvía, así que no necesitamos hacer Reject.
        # Simplemente actualizamos el estado final y dejamos que Celery termine.
        self.update_state(state=states.FAILURE, meta={'exc_type': type(final_error).__name__, 'exc_message': str(final_error)})
        # No se debe re-elevar mree si queremos que Celery la maneje como una falla final
        # y no la intente procesar de nuevo. Simplemente retornamos o dejamos que termine.

    except Exception as exc: # Captura errores generales que pueden ser retriables
        log.exception(f"An unexpected error occurred, attempting retry if possible.")
        error_msg_exc = f"Attempt {attempt} failed: {type(exc).__name__} - {str(exc)[:400]}"
        # Actualizar a error temporalmente. Si el reintento tiene éxito, se cambiará a PROCESSED.
        # Si todos los reintentos fallan, MaxRetriesExceededError se encargará del estado final.
        set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSING, error_message=error_msg_exc)
        raise self.retry(exc=exc, countdown=int(self.default_retry_delay * (self.request.retries + 1)))


process_document_haystack_task = process_document_standalone