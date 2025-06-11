from celery import Task # Import Task for self type hint
from app.infrastructure.tasks.celery_app_config import celery_app
from app.infrastructure.dependency_injection import create_process_document_use_case_for_worker
from app.infrastructure.persistence.postgres_connector import get_sync_engine, dispose_sync_engine # For init/shutdown
from app.services.gcs_client import GCSClient # Assuming GCSClient is globally managed for worker for now
from app.core.config import settings # To initialize GCSClient with correct bucket
from celery.signals import worker_process_init, worker_process_shutdown
import uuid
import structlog

# Global resources for worker process, initialized by signals
sync_db_engine_global = None
gcs_client_global = None # GCSClient can be shared if bucket is fixed per worker process


@worker_process_init.connect(weak=False)
def init_worker_resources(**kwargs):
    global sync_db_engine_global, gcs_client_global
    log = structlog.get_logger("celery_worker.init")
    log.info("Worker process initializing resources...")
    try:
        if sync_db_engine_global is None:
            sync_db_engine_global = get_sync_engine() # From postgres_connector
            log.info("Synchronous DB engine initialized for Celery worker.")
        
        if gcs_client_global is None:
            # GCSClient can be instantiated once per worker process
            # It's generally thread-safe for read operations.
            gcs_client_global = GCSClient(bucket_name=settings.GCS_BUCKET_NAME)
            log.info("GCS client initialized for Celery worker.", bucket=settings.GCS_BUCKET_NAME)
            
        # Potentially initialize other shared resources like Milvus connection for VectorStorePort
        # if MilvusAdapter is made to use a global connection.
        # For now, MilvusAdapter handles its own connection.

        log.info("Celery worker resources initialization complete.")
    except Exception as e:
        log.critical("CRITICAL FAILURE during Celery worker resource initialization!", error=str(e), exc_info=True)
        # Decide if worker should exit or try to continue without resources
        raise # Re-raising will likely stop the worker process if critical

@worker_process_shutdown.connect(weak=False)
def shutdown_worker_resources(**kwargs):
    log = structlog.get_logger("celery_worker.shutdown")
    log.info("Worker process shutting down resources...")
    if sync_db_engine_global:
        dispose_sync_engine() # From postgres_connector
        log.info("Synchronous DB engine disposed for Celery worker.")
    # GCSClient doesn't have an explicit close/dispose method in the provided code.
    # HTTP clients used by service adapters (docproc, embedding) will be closed by their BaseServiceClient.
    log.info("Celery worker resources shutdown complete.")


@celery_app.task(name='ingest_service.process_document_task', bind=True) # Ensure task name is unique
def process_document_task(self: Task, document_id_str: str, company_id_str: str, filename: str, content_type: str):
    task_log = structlog.get_logger("celery.task.process_document").bind(
        task_id=str(self.request.id), 
        document_id=document_id_str,
        company_id=company_id_str
    )
    task_log.info("Celery task process_document_task started.", filename=filename, content_type=content_type)

    # Ensure resources are available (they should be from worker_process_init)
    if sync_db_engine_global is None or gcs_client_global is None:
        task_log.error("Critical worker resources not initialized. Task cannot proceed.")
        # This situation indicates a problem with worker_process_init
        # Update status to error manually or re-raise to let Celery handle retry/failure
        # For now, let Celery's retry mechanism handle it if configured, or it will fail.
        raise Exception("Worker resources (DB engine or GCS client) not available.")

    try:
        # Use the factory from dependency_injection to get the use case
        # This ensures all dependencies are correctly wired for the worker context
        use_case = create_process_document_use_case_for_worker()
        
        # Execute the use case
        # The execute method of ProcessDocumentUseCase is synchronous
        result = use_case.execute(
            document_id_str=document_id_str, # Pass as string
            company_id_str=company_id_str,   # Pass as string
            filename=filename,               # Use original filename
            content_type=content_type
        )
        task_log.info("Celery task process_document_task completed successfully.", result=result)
        return result
    except Exception as e:
        task_log.error("Exception in Celery task process_document_task.", error=str(e), exc_info=True)
        # Celery will handle retries/failure based on task configuration and exception type
        raise # Re-raise the exception for Celery to handle