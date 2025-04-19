# ingest-service/app/api/v1/endpoints/ingest.py
import uuid
import mimetypes
import json
from typing import List, Optional, Dict, Any
import asyncio
from contextlib import asynccontextmanager

from fastapi import (
    APIRouter, Depends, HTTPException, status,
    UploadFile, File, Form, Header, Query, Path, BackgroundTasks, Request
)
import structlog
import asyncpg
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type, before_sleep_log

# Haystack imports (only for type hinting or specific checks if needed here)
# Note: DocumentStore interactions for count/delete are often better abstracted
from milvus_haystack import MilvusDocumentStore # For type hints or direct use

# Custom imports
from app.core.config import settings
from app.db import postgres_client as db_client
from app.models.domain import DocumentStatus
from app.api.v1.schemas import IngestResponse, StatusResponse, PaginatedStatusResponse, ErrorDetail
from app.services.minio_client import MinioClient, MinioError
from app.tasks.celery_app import celery_app # Import celery_app instance
from app.tasks.process_document import process_document_haystack_task # Import task signature

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Helper Functions ---

def get_minio_client():
    """Dependency to get Minio client instance."""
    try:
        return MinioClient(
            endpoint=settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            bucket_name=settings.MINIO_BUCKET_NAME,
            secure=settings.MINIO_USE_SSL
        )
    except Exception as e:
        log.exception("Failed to initialize MinioClient dependency", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage service configuration error."
        )

# Define retry strategy for database operations within API requests
api_db_retry_strategy = retry(
    stop=stop_after_attempt(2), # Fewer retries for API context
    wait=wait_fixed(1),
    retry=retry_if_exception_type((asyncpg.exceptions.PostgresConnectionError, TimeoutError, OSError)),
    before_sleep=before_sleep_log(log, logging.WARNING)
)

@asynccontextmanager
async def get_db_conn():
    """Provides a single connection from the pool for API request context."""
    pool = await db_client.get_db_pool()
    conn = None
    try:
        conn = await pool.acquire()
        yield conn
    except Exception as e:
        log.error("Failed to acquire DB connection for request", error=str(e))
        raise HTTPException(status_code=503, detail="Database connection unavailable.")
    finally:
        if conn:
            await pool.release(conn)


# Helper for Milvus interactions (Count and Delete)
# These run synchronously within asyncio's executor
def _initialize_milvus_store_sync() -> MilvusDocumentStore:
    """Synchronously initializes MilvusDocumentStore for API helpers."""
    api_log = log.bind(component="MilvusHelperSync")
    api_log.debug("Initializing MilvusDocumentStore for API helper...")
    try:
        store = MilvusDocumentStore(
            connection_args={"uri": settings.MILVUS_URI},
            collection_name=settings.MILVUS_COLLECTION_NAME,
             # embedding_dim=settings.EMBEDDING_DIMENSION, # <-- REMOVED
            # Other relevant args? Consistency Level might not be needed for count/delete
        )
        api_log.debug("MilvusDocumentStore initialized successfully for API helper.")
        return store
    except TypeError as te: # Should be fixed now, but keep check
        api_log.error("MilvusDocumentStore init TypeError in API helper", error=str(te), exc_info=True)
        raise RuntimeError(f"Milvus TypeError (check arguments like embedding_dim): {te}") from te
    except Exception as e:
        api_log.exception("Failed to initialize MilvusDocumentStore for API helper", error=str(e))
        raise RuntimeError(f"Milvus Store Initialization Error for API helper: {e}") from e

def _get_milvus_chunk_count_sync(document_id: str, company_id: str) -> int:
    """Synchronously counts chunks in Milvus for a specific document."""
    count_log = log.bind(document_id=document_id, company_id=company_id, component="MilvusHelperSync")
    try:
        store = _initialize_milvus_store_sync()
        # Construct filters based on metadata Haystack uses
        filters = {
            "field": "meta", # Assuming meta field contains our IDs
            "operator": "AND",
            "conditions": [
                {"field": "document_id", "operator": "==", "value": document_id},
                {"field": "company_id", "operator": "==", "value": company_id},
            ]
        }
        count = store.count_documents(filters=filters)
        count_log.info("Milvus chunk count successful", count=count)
        return count
    except RuntimeError as re: # Catch init errors
        count_log.error("Failed to get Milvus count due to store init error", error=str(re))
        return -1 # Indicate error with -1
    except Exception as e:
        count_log.exception("Error counting documents in Milvus", error=str(e))
        return -1 # Indicate error with -1

def _delete_milvus_sync(document_id: str, company_id: str) -> bool:
    """Synchronously deletes chunks from Milvus for a specific document."""
    delete_log = log.bind(document_id=document_id, company_id=company_id, component="MilvusHelperSync")
    try:
        store = _initialize_milvus_store_sync()
        # Construct filters based on metadata
        filters = {
            "field": "meta", # Assuming meta field contains our IDs
            "operator": "AND",
            "conditions": [
                {"field": "document_id", "operator": "==", "value": document_id},
                {"field": "company_id", "operator": "==", "value": company_id},
            ]
        }
        store.delete_documents(filters=filters) # Assuming filter-based delete is efficient
        # Note: delete_documents might not return success/fail count easily. Assume success if no exception.
        delete_log.info("Milvus delete operation executed (async nature means eventual consistency).")
        return True
    except RuntimeError as re: # Catch init errors
        delete_log.error("Failed to delete Milvus chunks due to store init error", error=str(re))
        return False
    except Exception as e:
        delete_log.exception("Error deleting documents from Milvus", error=str(e))
        return False

# --- Endpoints ---

@router.post(
    "/upload",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a document for asynchronous ingestion",
    responses={
        400: {"model": ErrorDetail, "description": "Bad Request (e.g., invalid metadata, type, duplicate)"},
        415: {"model": ErrorDetail, "description": "Unsupported Media Type"},
        409: {"model": ErrorDetail, "description": "Conflict (Duplicate file)"},
        500: {"model": ErrorDetail, "description": "Internal Server Error"},
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB or MinIO)"},
    }
)
async def upload_document(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    metadata_json: Optional[str] = Form(None),
    company_id: str = Header(..., description="Company ID associated with the document"),
    user_id: str = Header(..., description="User ID initiating the upload"),
    minio_client: MinioClient = Depends(get_minio_client),
):
    """
    Receives a document file and optional metadata, saves it to MinIO,
    creates a record in PostgreSQL, and queues a Celery task for processing.
    Prevents upload if a non-error document with the same name exists for the company.
    """
    req_id = getattr(request.state, 'request_id', 'N/A')
    endpoint_log = log.bind(request_id=req_id, company_id=company_id, user_id=user_id,
                            filename=file.filename, content_type=file.content_type)
    endpoint_log.info("Processing document ingestion request from gateway")

    # 1. Validate Content-Type
    if file.content_type not in settings.SUPPORTED_CONTENT_TYPES:
        endpoint_log.warning("Unsupported content type received")
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type: {file.content_type}. Supported types: {', '.join(settings.SUPPORTED_CONTENT_TYPES)}"
        )

    # 2. Validate Metadata JSON (if provided)
    metadata = None
    if metadata_json:
        try:
            metadata = json.loads(metadata_json)
            if not isinstance(metadata, dict):
                raise ValueError("Metadata must be a JSON object.")
        except json.JSONDecodeError:
            endpoint_log.warning("Invalid metadata JSON format received")
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid metadata format: Must be valid JSON.")
        except ValueError as e:
             endpoint_log.warning(f"Invalid metadata content: {e}")
             raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid metadata content: {e}")

    # 3. Check for Duplicates (in PostgreSQL)
    try:
        company_uuid = uuid.UUID(company_id)
        async with get_db_conn() as conn:
            existing_doc = await api_db_retry_strategy(db_client.find_document_by_name_and_company)(
                conn=conn, filename=file.filename, company_id=company_uuid
            )
            if existing_doc and existing_doc['status'] != DocumentStatus.ERROR.value:
                 endpoint_log.warning("Duplicate document detected", document_id=existing_doc['id'], status=existing_doc['status'])
                 raise HTTPException(
                     status_code=status.HTTP_409_CONFLICT,
                     detail=f"Document '{file.filename}' already exists with status '{existing_doc['status']}'. Delete it first or wait for processing."
                 )
            elif existing_doc and existing_doc['status'] == DocumentStatus.ERROR.value:
                 endpoint_log.info("Found existing document in error state, proceeding with overwrite logic implicitly (new upload).", document_id=existing_doc['id'])
                 # We will create a new record, the old 'error' record remains until manually deleted.
                 # Or, you could implement logic here to delete the old 'error' record first.
                 pass # Allow upload to proceed, creating a new document ID

    except ValueError:
        endpoint_log.error("Invalid Company ID format provided", company_id_received=company_id)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")
    except HTTPException as http_exc:
        raise http_exc # Re-raise conflict or other HTTP exceptions
    except Exception as e:
        endpoint_log.exception("Error checking for duplicate document", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error checking for duplicates.")

    # 4. Create Initial Document Record (PostgreSQL)
    document_id = uuid.uuid4()
    object_name = f"{company_id}/{document_id}/{file.filename}" # Define object name *before* DB write

    try:
        async with get_db_conn() as conn:
            await api_db_retry_strategy(db_client.create_document_record)(
                conn=conn,
                doc_id=document_id,
                company_id=company_uuid,
                user_id=uuid.UUID(user_id), # Assuming user_id is also UUID
                filename=file.filename,
                file_type=file.content_type,
                minio_object_name=object_name, # Store object name early
                status=DocumentStatus.PENDING, # Start as PENDING before upload
                metadata=metadata
            )
        endpoint_log.info("Document record created in PostgreSQL", document_id=str(document_id))
    except ValueError:
        endpoint_log.error("Invalid User ID format provided", user_id_received=user_id)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid User ID format.")
    except Exception as e:
        endpoint_log.exception("Failed to create document record in PostgreSQL", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error creating record.")

    # 5. Upload to MinIO (using background task for large files if preferred, but await is safer)
    try:
        file_content = await file.read() # Read file content
        await file.seek(0) # Reset file pointer if needed elsewhere
        await minio_client.upload_file_async(
            object_name=object_name,
            data=file_content,
            content_type=file.content_type
        )
        endpoint_log.info("File uploaded successfully to MinIO", object_name=object_name)

        # 6. Update DB status to 'uploaded'
        async with get_db_conn() as conn:
             await api_db_retry_strategy(db_client.update_document_status)(
                 pool=conn, # Use acquired connection directly if pool returns connection
                 document_id=document_id,
                 new_status=DocumentStatus.UPLOADED
             )
        endpoint_log.info("Document status updated to 'uploaded'", document_id=str(document_id))

    except MinioError as me:
        endpoint_log.error("Failed to upload file to MinIO", object_name=object_name, error=str(me))
        # Attempt to mark document as error in DB
        try:
            async with get_db_conn() as conn:
                await api_db_retry_strategy(db_client.update_document_status)(
                    conn, document_id, DocumentStatus.ERROR, error_message=f"MinIO upload failed: {me}"
                )
        except Exception as db_err:
            endpoint_log.exception("Failed to update status to ERROR after MinIO failure", error=str(db_err))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Storage service error: {me}")
    except Exception as e:
         endpoint_log.exception("Unexpected error during file upload or DB update", error=str(e))
          # Attempt to mark document as error in DB
         try:
             async with get_db_conn() as conn:
                 await api_db_retry_strategy(db_client.update_document_status)(
                     conn, document_id, DocumentStatus.ERROR, error_message=f"Unexpected upload error: {e}"
                 )
         except Exception as db_err:
             endpoint_log.exception("Failed to update status to ERROR after unexpected upload failure", error=str(db_err))
         raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Internal server error during upload: {e}")
    finally:
         await file.close() # Ensure file handle is closed

    # 7. Queue Celery Task
    try:
        task_payload = {
            "document_id": str(document_id),
            "company_id": company_id,
            "filename": file.filename,
            "content_type": file.content_type,
            "user_id": user_id, # Pass user if needed by task
        }
        task = process_document_haystack_task.delay(**task_payload)
        endpoint_log.info("Document ingestion task queued successfully", task_id=task.id)
    except Exception as e:
        endpoint_log.exception("Failed to queue Celery task", error=str(e))
        # Attempt to mark document as error
        try:
            async with get_db_conn() as conn:
                await api_db_retry_strategy(db_client.update_document_status)(
                    conn, document_id, DocumentStatus.ERROR, error_message=f"Failed to queue processing task: {e}"
                )
        except Exception as db_err:
             endpoint_log.exception("Failed to update status to ERROR after Celery failure", error=str(db_err))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to queue processing task: {e}")

    return IngestResponse(
        document_id=str(document_id),
        task_id=task.id,
        status=DocumentStatus.UPLOADED.value, # Return the status *after* successful upload
        message="Document upload accepted, processing started."
    )

@router.get(
    "/status/{document_id}",
    response_model=StatusResponse,
    summary="Get the status of a specific document",
    responses={
        404: {"model": ErrorDetail, "description": "Document not found"},
        500: {"model": ErrorDetail, "description": "Internal Server Error"},
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB, MinIO, Milvus)"},
    }
)
async def get_document_status(
    request: Request,
    document_id: uuid.UUID = Path(..., description="The UUID of the document"),
    company_id: str = Header(..., description="Company ID"),
    minio_client: MinioClient = Depends(get_minio_client),
):
    """
    Retrieves the status of a document from PostgreSQL.
    Performs live checks:
    - Verifies file existence in MinIO.
    - Counts chunks in Milvus (via executor).
    - Updates the DB status if inconsistencies are found (e.g., chunks exist but status is 'uploaded').
    """
    req_id = getattr(request.state, 'request_id', 'N/A')
    status_log = log.bind(request_id=req_id, document_id=str(document_id), company_id=company_id)
    status_log.info("Request received for document status")

    try:
        company_uuid = uuid.UUID(company_id)
    except ValueError:
        status_log.warning("Invalid Company ID format")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")

    doc_data: Optional[Dict[str, Any]] = None
    needs_update = False
    updated_status = None
    updated_chunk_count = None
    final_error_message = None

    # 1. Get Base Status from DB
    try:
        async with get_db_conn() as conn:
             doc_data = await api_db_retry_strategy(db_client.get_document_by_id)(
                 conn, doc_id=document_id, company_id=company_uuid
             )
        if not doc_data:
            status_log.warning("Document not found in DB")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
        status_log.info("Retrieved base document data from DB", status=doc_data['status'])
        updated_status = DocumentStatus(doc_data['status']) # Keep track
        final_error_message = doc_data.get('error_message')

    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        status_log.exception("Error fetching document status from DB", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error fetching status.")

    minio_path = doc_data.get('minio_object_name') # Use the stored path
    if not minio_path:
         status_log.warning("MinIO object name missing in DB record", db_id=doc_data['id'])
         # Consider setting error state? Or just report inconsistency
         minio_exists = False
    else:
        # 2. Check MinIO Existence (Async)
        status_log.debug("Checking MinIO for file existence", object_name=minio_path)
        minio_exists = await minio_client.check_file_exists_async(minio_path)
        status_log.info("MinIO existence check complete", exists=minio_exists)
        if not minio_exists and updated_status not in [DocumentStatus.ERROR, DocumentStatus.PENDING]:
             status_log.warning("File missing in MinIO but DB status is not ERROR/PENDING", current_db_status=updated_status.value)
             # File is gone, mark as error if not already
             if updated_status != DocumentStatus.ERROR:
                 needs_update = True
                 updated_status = DocumentStatus.ERROR
                 final_error_message = "File missing from storage."

    # 3. Check Milvus Chunk Count (Sync in Executor)
    status_log.debug("Checking Milvus for chunk count...")
    loop = asyncio.get_running_loop()
    try:
        milvus_chunk_count = await loop.run_in_executor(
            None, _get_milvus_chunk_count_sync, str(document_id), company_id
        )
        status_log.info("Milvus chunk count check complete", count=milvus_chunk_count)
        if milvus_chunk_count == -1:
            status_log.error("Milvus count check failed (returned -1). Treating as error.")
            if updated_status != DocumentStatus.ERROR:
                needs_update = True
                updated_status = DocumentStatus.ERROR
                final_error_message = (final_error_message or "") + " Failed to verify processed data (Milvus count error)."
        elif milvus_chunk_count > 0 and updated_status == DocumentStatus.UPLOADED:
            status_log.warning("Inconsistency: Chunks found in Milvus but DB status is 'uploaded'. Correcting to 'processed'.")
            needs_update = True
            updated_status = DocumentStatus.PROCESSED
            updated_chunk_count = milvus_chunk_count # Store the count we found
            final_error_message = None # Clear error if we are now processed
        elif milvus_chunk_count == 0 and updated_status == DocumentStatus.PROCESSED:
             status_log.warning("Inconsistency: DB status is 'processed' but no chunks found in Milvus. Correcting to 'error'.")
             needs_update = True
             updated_status = DocumentStatus.ERROR
             updated_chunk_count = 0
             final_error_message = (final_error_message or "") + " Processed data missing (Milvus count is 0)."
        elif updated_status == DocumentStatus.PROCESSED:
            # If status is already processed and count matches DB or is > 0, update stored count
            if updated_chunk_count is None or updated_chunk_count != milvus_chunk_count:
                 updated_chunk_count = milvus_chunk_count
                 if doc_data.get('chunk_count') != updated_chunk_count:
                      needs_update = True # Need to update DB chunk count
    except Exception as e:
        status_log.exception("Unexpected error during Milvus count check", error=str(e))
        milvus_chunk_count = -1 # Indicate error
        if updated_status != DocumentStatus.ERROR:
            needs_update = True
            updated_status = DocumentStatus.ERROR
            final_error_message = (final_error_message or "") + f" Error checking processed data: {e}."

    # 4. Update DB if inconsistencies were found
    if needs_update:
        status_log.warning("Inconsistency detected, updating document status in DB",
                          new_status=updated_status.value, new_count=updated_chunk_count, new_error=final_error_message)
        try:
             async with get_db_conn() as conn:
                 await api_db_retry_strategy(db_client.update_document_status)(
                     conn, document_id, updated_status,
                     chunk_count=updated_chunk_count, # Update count if changed
                     error_message=final_error_message # Update error message
                 )
             status_log.info("Document status updated successfully in DB due to inconsistency check.")
             # Update local data for response
             doc_data['status'] = updated_status.value
             if updated_chunk_count is not None: doc_data['chunk_count'] = updated_chunk_count
             if final_error_message is not None: doc_data['error_message'] = final_error_message
        except Exception as e:
             status_log.exception("Failed to update document status in DB after inconsistency check", error=str(e))
             # Continue to return status, but log the failure to update
             # The returned status might be stale now.

    # 5. Construct and Return Response
    status_log.info("Returning final document status")
    return StatusResponse(
        document_id=str(doc_data['id']),
        status=doc_data['status'],
        file_name=doc_data['file_name'],
        file_type=doc_data['file_type'],
        chunk_count=doc_data.get('chunk_count', 0), # Use DB value (potentially updated)
        minio_exists=minio_exists, # Live check result
        milvus_chunk_count=milvus_chunk_count, # Live check result (or -1 for error)
        last_updated=doc_data['updated_at'],
        error_message=doc_data.get('error_message'), # Use DB value (potentially updated)
        metadata=doc_data.get('metadata') # Include metadata if available
    )

@router.get(
    "/status",
    response_model=PaginatedStatusResponse,
    summary="List document statuses with pagination and live checks",
    responses={
        500: {"model": ErrorDetail, "description": "Internal Server Error"},
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB, MinIO, Milvus)"},
    }
)
async def list_document_statuses(
    request: Request,
    company_id: str = Header(..., description="Company ID"),
    limit: int = Query(30, ge=1, le=100, description="Number of documents per page"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    # Add optional filters? e.g., status=... filename=...
    minio_client: MinioClient = Depends(get_minio_client),
):
    """
    Lists documents for the company with pagination.
    Performs live checks for MinIO/Milvus in parallel for listed documents.
    Updates the DB status/chunk_count if inconsistencies are found.
    Returns the potentially updated status information.
    """
    req_id = getattr(request.state, 'request_id', 'N/A')
    list_log = log.bind(request_id=req_id, company_id=company_id, limit=limit, offset=offset)
    list_log.info("Listing document statuses with real-time checks")

    try:
        company_uuid = uuid.UUID(company_id)
    except ValueError:
        list_log.warning("Invalid Company ID format")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")

    documents_db: List[Dict[str, Any]] = []
    total_count: int = 0

    # 1. Get paginated list from DB
    try:
        async with get_db_conn() as conn:
            documents_db, total_count = await api_db_retry_strategy(db_client.list_documents_paginated)(
                conn, company_id=company_uuid, limit=limit, offset=offset
            )
        list_log.info("Retrieved documents from DB", count=len(documents_db), total_db_count=total_count)
    except Exception as e:
        list_log.exception("Error listing documents from DB", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error listing documents.")

    if not documents_db:
        return PaginatedStatusResponse(items=[], total=0, limit=limit, offset=offset)

    # --- Perform Live Checks in Parallel ---
    async def check_single_document(doc_db_data: Dict[str, Any]) -> Dict[str, Any]:
        """Async helper to check MinIO/Milvus for one document."""
        check_log = log.bind(request_id=req_id, document_id=str(doc_db_data['id']), company_id=company_id)
        check_log.debug("Starting live checks for document")

        minio_exists_live = False
        milvus_count_live = -1 # Default to error state
        doc_needs_update = False
        doc_updated_status_val = doc_db_data['status']
        doc_updated_chunk_count = doc_db_data.get('chunk_count')
        doc_final_error_msg = doc_db_data.get('error_message')

        # Check MinIO
        minio_path_db = doc_db_data.get('minio_object_name')
        if minio_path_db:
            try:
                minio_exists_live = await minio_client.check_file_exists_async(minio_path_db)
                check_log.debug("MinIO check done", exists=minio_exists_live)
                if not minio_exists_live and doc_updated_status_val not in [DocumentStatus.ERROR.value, DocumentStatus.PENDING.value]:
                     check_log.warning("File missing in MinIO but DB status is not ERROR/PENDING", current_db_status=doc_updated_status_val)
                     if doc_updated_status_val != DocumentStatus.ERROR.value:
                         doc_needs_update = True
                         doc_updated_status_val = DocumentStatus.ERROR.value
                         doc_final_error_msg = "File missing from storage."
            except Exception as e:
                 check_log.error("MinIO check failed", error=str(e))
                 minio_exists_live = False # Assume false on error
                 if doc_updated_status_val != DocumentStatus.ERROR.value:
                      doc_needs_update = True
                      doc_updated_status_val = DocumentStatus.ERROR.value
                      doc_final_error_msg = (doc_final_error_msg or "") + f" MinIO check error: {e}."
        else:
             check_log.warning("MinIO object name missing in DB record.")
             minio_exists_live = False

        # Check Milvus (Sync in Executor)
        loop = asyncio.get_running_loop()
        try:
            milvus_count_live = await loop.run_in_executor(
                None, _get_milvus_chunk_count_sync, str(doc_db_data['id']), company_id
            )
            check_log.debug("Milvus count check done", count=milvus_count_live)
            if milvus_count_live == -1:
                check_log.error("Milvus count check failed (returned -1)")
                if doc_updated_status_val != DocumentStatus.ERROR.value:
                    doc_needs_update = True
                    doc_updated_status_val = DocumentStatus.ERROR.value
                    doc_final_error_msg = (doc_final_error_msg or "") + " Failed to verify processed data (Milvus count error)."
            elif milvus_count_live > 0 and doc_updated_status_val == DocumentStatus.UPLOADED.value:
                 check_log.warning("Inconsistency: Chunks found in Milvus but DB status is 'uploaded'. Correcting to 'processed'.")
                 doc_needs_update = True
                 doc_updated_status_val = DocumentStatus.PROCESSED.value
                 doc_updated_chunk_count = milvus_count_live
                 doc_final_error_msg = None # Clear error
            elif milvus_count_live == 0 and doc_updated_status_val == DocumentStatus.PROCESSED.value:
                 check_log.warning("Inconsistency: DB status is 'processed' but no chunks found in Milvus. Correcting to 'error'.")
                 doc_needs_update = True
                 doc_updated_status_val = DocumentStatus.ERROR.value
                 doc_updated_chunk_count = 0
                 doc_final_error_msg = (doc_final_error_msg or "") + " Processed data missing (Milvus count is 0)."
            elif doc_updated_status_val == DocumentStatus.PROCESSED.value:
                 # If status is already processed and count matches DB or is > 0, update stored count
                 if doc_updated_chunk_count is None or doc_updated_chunk_count != milvus_count_live:
                      doc_updated_chunk_count = milvus_count_live
                      if doc_db_data.get('chunk_count') != doc_updated_chunk_count:
                           doc_needs_update = True # Need to update DB chunk count

        except Exception as e:
            check_log.exception("Unexpected error during Milvus count check", error=str(e))
            milvus_count_live = -1 # Indicate error
            if doc_updated_status_val != DocumentStatus.ERROR.value:
                doc_needs_update = True
                doc_updated_status_val = DocumentStatus.ERROR.value
                doc_final_error_msg = (doc_final_error_msg or "") + f" Error checking processed data: {e}."

        # Store results for potential DB update and final response construction
        return {
            "db_data": doc_db_data,
            "needs_update": doc_needs_update,
            "updated_status": doc_updated_status_val,
            "updated_chunk_count": doc_updated_chunk_count,
            "final_error_message": doc_final_error_msg,
            "live_minio_exists": minio_exists_live,
            "live_milvus_chunk_count": milvus_count_live,
        }

    # Run checks concurrently
    check_tasks = [check_single_document(doc) for doc in documents_db]
    check_results = await asyncio.gather(*check_tasks)

    # Update DB for inconsistent documents (sequentially or batched)
    updated_doc_data_map = {} # Store updated data by doc_id
    docs_to_update_in_db = []
    for result in check_results:
        if result["needs_update"]:
            docs_to_update_in_db.append({
                "id": result["db_data"]["id"],
                "status": DocumentStatus(result["updated_status"]), # Enum for update func
                "chunk_count": result["updated_chunk_count"],
                "error_message": result["final_error_message"],
            })
        # Store the potentially updated data for the final response
        updated_doc_data_map[str(result["db_data"]["id"])] = {
             **result["db_data"], # Original data
             "status": result["updated_status"], # Potentially updated status
             "chunk_count": result["updated_chunk_count"], # Potentially updated count
             "error_message": result["final_error_message"], # Potentially updated error
        }


    if docs_to_update_in_db:
        list_log.warning("Updating statuses in DB for inconsistent documents", count=len(docs_to_update_in_db))
        # Consider batch update if db_client supports it, otherwise sequential
        try:
             async with get_db_conn() as conn:
                 for update_info in docs_to_update_in_db:
                     try:
                         await api_db_retry_strategy(db_client.update_document_status)(
                             conn, update_info["id"], update_info["status"],
                             chunk_count=update_info["chunk_count"],
                             error_message=update_info["error_message"]
                         )
                         list_log.info("Successfully updated DB status", document_id=str(update_info["id"]), new_status=update_info["status"].value)
                     except Exception as single_update_err:
                         # Log error for this specific update but continue with others
                         list_log.error("Failed DB update for single document during list check",
                                        document_id=str(update_info["id"]), error=str(single_update_err))
        except Exception as bulk_update_err:
            list_log.exception("Error during bulk DB status update process", error=str(bulk_update_err))
            # Proceed with potentially stale data, error logged

    # Construct final response using potentially updated data
    final_items = []
    for result in check_results:
         doc_id_str = str(result["db_data"]["id"])
         # Use data from map which includes potential updates, fallback to original if error somehow
         current_data = updated_doc_data_map.get(doc_id_str, result["db_data"])
         final_items.append(StatusResponse(
            document_id=doc_id_str,
            status=current_data['status'],
            file_name=current_data['file_name'],
            file_type=current_data['file_type'],
            chunk_count=current_data.get('chunk_count', 0),
            minio_exists=result["live_minio_exists"], # Use live check result
            milvus_chunk_count=result["live_milvus_chunk_count"], # Use live check result
            last_updated=current_data['updated_at'],
            error_message=current_data.get('error_message'),
            metadata=current_data.get('metadata')
         ))

    list_log.info("Returning enriched statuses", count=len(final_items))
    return PaginatedStatusResponse(items=final_items, total=total_count, limit=limit, offset=offset)


@router.post(
    "/retry/{document_id}",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Retry ingestion for a document currently in 'error' state",
    responses={
        404: {"model": ErrorDetail, "description": "Document not found"},
        409: {"model": ErrorDetail, "description": "Document is not in 'error' state"},
        500: {"model": ErrorDetail, "description": "Internal Server Error"},
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB or Celery)"},
    }
)
async def retry_ingestion(
    request: Request,
    document_id: uuid.UUID = Path(..., description="The UUID of the document to retry"),
    company_id: str = Header(..., description="Company ID"),
    user_id: str = Header(..., description="User ID initiating the retry"),
):
    """
    Allows retrying the ingestion process for a document that previously failed.
    - Checks if the document exists and belongs to the company.
    - Verifies the document status is 'error'.
    - Updates the status to 'processing' (clearing error message).
    - Re-queues the Celery processing task.
    """
    req_id = getattr(request.state, 'request_id', 'N/A')
    retry_log = log.bind(request_id=req_id, document_id=str(document_id), company_id=company_id, user_id=user_id)
    retry_log.info("Received request to retry document ingestion")

    try:
        company_uuid = uuid.UUID(company_id)
    except ValueError:
        retry_log.warning("Invalid Company ID format")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")

    # 1. Get document and verify state
    doc_data: Optional[Dict[str, Any]] = None
    try:
         async with get_db_conn() as conn:
            doc_data = await api_db_retry_strategy(db_client.get_document_by_id)(
                conn, doc_id=document_id, company_id=company_uuid
            )
         if not doc_data:
            retry_log.warning("Document not found for retry")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")

         if doc_data['status'] != DocumentStatus.ERROR.value:
             retry_log.warning("Document is not in error state, cannot retry", current_status=doc_data['status'])
             raise HTTPException(
                 status_code=status.HTTP_409_CONFLICT,
                 detail=f"Document is not in 'error' state (current state: {doc_data['status']}). Cannot retry."
             )
         retry_log.info("Document found and confirmed to be in 'error' state.")

    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        retry_log.exception("Error fetching document for retry", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error checking document for retry.")

    # 2. Update status to 'processing' (clear error message)
    try:
        async with get_db_conn() as conn:
            await api_db_retry_strategy(db_client.update_document_status)(
                conn, document_id, DocumentStatus.PROCESSING, chunk_count=None, error_message=None # Clear error
            )
        retry_log.info("Document status updated to 'processing' for retry.")
    except Exception as e:
        retry_log.exception("Failed to update document status to 'processing' for retry", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error updating status for retry.")

    # 3. Re-queue Celery task
    try:
        task_payload = {
            "document_id": str(document_id),
            "company_id": company_id,
            "filename": doc_data['file_name'],
            "content_type": doc_data['file_type'],
             "user_id": user_id,
        }
        task = process_document_haystack_task.delay(**task_payload)
        retry_log.info("Document reprocessing task queued successfully", task_id=task.id)
    except Exception as e:
        retry_log.exception("Failed to re-queue Celery task for retry", error=str(e))
        # Attempt to revert status back to 'error' ? This is tricky.
        # For now, raise 500, status might be stuck in 'processing'.
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to queue reprocessing task: {e}")

    return IngestResponse(
        document_id=str(document_id),
        task_id=task.id,
        status=DocumentStatus.PROCESSING.value,
        message="Document retry accepted, processing started."
    )


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document and its associated data",
    responses={
        404: {"model": ErrorDetail, "description": "Document not found"},
        500: {"model": ErrorDetail, "description": "Internal Server Error"},
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB, MinIO, Milvus)"},
    }
)
async def delete_document_endpoint(
    request: Request,
    document_id: uuid.UUID = Path(..., description="The UUID of the document to delete"),
    company_id: str = Header(..., description="Company ID"),
    minio_client: MinioClient = Depends(get_minio_client),
):
    """
    Deletes a document completely:
    - Removes chunks from Milvus (via executor).
    - Removes the file from MinIO (async).
    - Removes the record from PostgreSQL.
    Verifies ownership before deletion.
    """
    req_id = getattr(request.state, 'request_id', 'N/A')
    delete_log = log.bind(request_id=req_id, document_id=str(document_id), company_id=company_id)
    delete_log.info("Received request to delete document")

    try:
        company_uuid = uuid.UUID(company_id)
    except ValueError:
        delete_log.warning("Invalid Company ID format")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")

    # 1. Verify document exists and belongs to company
    doc_data: Optional[Dict[str, Any]] = None
    try:
        async with get_db_conn() as conn:
             doc_data = await api_db_retry_strategy(db_client.get_document_by_id)(
                 conn, doc_id=document_id, company_id=company_uuid
             )
        if not doc_data:
            delete_log.warning("Document not found for deletion")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
        delete_log.info("Document verified for deletion", filename=doc_data.get('file_name'))
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        delete_log.exception("Error verifying document before deletion", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error during delete verification.")

    errors = [] # Collect non-critical errors (Milvus/MinIO)

    # 2. Delete from Milvus (Sync in Executor)
    delete_log.info("Attempting to delete chunks from Milvus...")
    loop = asyncio.get_running_loop()
    try:
        milvus_deleted = await loop.run_in_executor(
            None, _delete_milvus_sync, str(document_id), company_id
        )
        if milvus_deleted:
            delete_log.info("Milvus delete command executed successfully.")
        else:
            # Error already logged in _delete_milvus_sync
            errors.append("Failed to execute delete operation in Milvus (check worker logs for details).")
            delete_log.warning("Milvus delete operation failed.")
    except Exception as e:
        delete_log.exception("Unexpected error during Milvus delete", error=str(e))
        errors.append(f"Unexpected error during Milvus delete: {e}")


    # 3. Delete from MinIO (Async)
    minio_path = doc_data.get('minio_object_name')
    if minio_path:
        delete_log.info("Attempting to delete file from MinIO...", object_name=minio_path)
        try:
            await minio_client.delete_file_async(minio_path)
            delete_log.info("Successfully deleted file from MinIO.")
        except MinioError as me:
            delete_log.error("Failed to delete file from MinIO", object_name=minio_path, error=str(me))
            errors.append(f"Failed to delete file from storage: {me}")
        except Exception as e:
            delete_log.exception("Unexpected error during MinIO delete", error=str(e))
            errors.append(f"Unexpected error during storage delete: {e}")
    else:
        delete_log.warning("Skipping MinIO delete: object name not found in DB record.")
        errors.append("Could not delete from storage: path unknown.")

    # 4. Delete from PostgreSQL (Critical step)
    delete_log.info("Attempting to delete record from PostgreSQL...")
    try:
         async with get_db_conn() as conn:
            deleted_id = await api_db_retry_strategy(db_client.delete_document)(
                conn, doc_id=document_id, company_id=company_uuid
            )
            if deleted_id:
                 delete_log.info("Document record deleted successfully from PostgreSQL")
            else:
                 # Should not happen if verification passed, but log just in case
                 delete_log.warning("PostgreSQL delete command executed but no record was deleted (already gone?).")
                 # Don't add to errors, maybe already deleted by another request
    except Exception as e:
        delete_log.exception("CRITICAL: Failed to delete document record from PostgreSQL", error=str(e))
        # This is a critical failure, the record persists but data might be gone.
        # Raise 500 Internal Server Error.
        error_detail = f"Successfully deleted from storage/vectors (with errors: {', '.join(errors)}) but FAILED to delete database record: {e}"
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=error_detail)

    # 5. Log warnings if non-critical errors occurred
    if errors:
        delete_log.warning("Document deletion completed with non-critical errors (Milvus/MinIO)", errors=errors)
        # Still return 204 as the DB record (primary source of truth) is gone.

    delete_log.info("Document deletion process finished.")
    # Return 204 No Content implicitly by FastAPI for delete operations without a return value