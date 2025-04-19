# Estructura de la Codebase

```
app/
├── __init__.py
├── api
│   ├── __init__.py
│   └── v1
│       ├── __init__.py
│       ├── endpoints
│       │   ├── __init__.py
│       │   └── ingest.py
│       └── schemas.py
├── core
│   ├── __init__.py
│   ├── config.py
│   └── logging_config.py
├── db
│   ├── __init__.py
│   ├── base.py
│   └── postgres_client.py
├── main.py
├── models
│   ├── __init__.py
│   └── domain.py
├── services
│   ├── __init__.py
│   ├── base_client.py
│   └── minio_client.py
├── tasks
│   ├── __init__.py
│   ├── celery_app.py
│   └── process_document.py
└── utils
    ├── __init__.py
    └── helpers.py
```

# Codebase: `app`

## File: `app\__init__.py`
```py

```

## File: `app\api\__init__.py`
```py

```

## File: `app\api\v1\__init__.py`
```py

```

## File: `app\api\v1\endpoints\__init__.py`
```py

```

## File: `app\api\v1\endpoints\ingest.py`
```py
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
```

## File: `app\api\v1\schemas.py`
```py
# ingest-service/app/api/v1/schemas.py
import uuid
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Dict, Any, List
from app.models.domain import DocumentStatus
from datetime import datetime
import json

class IngestResponse(BaseModel):
    document_id: uuid.UUID
    task_id: str
    status: str
    message: str = "Document upload received and queued for processing."

    class Config:
        json_schema_extra = { # Corregido de schema_extra
            "example": {
                "document_id": "123e4567-e89b-12d3-a456-426614174000",
                "task_id": "abcd1234efgh",
                "status": "uploaded", # Estado inicial devuelto por la API
                "message": "Document upload received and queued for processing."
            }
        }

class StatusResponse(BaseModel):
    document_id: uuid.UUID = Field(..., alias="id")
    company_id: uuid.UUID
    file_name: str
    file_type: str
    file_path: Optional[str] = None # Puede ser None inicialmente
    metadata: Optional[Dict[str, Any]] = None
    status: str
    chunk_count: Optional[int] = 0 # Default a 0, puede ser None si hay error
    error_message: Optional[str] = None
    uploaded_at: datetime
    updated_at: datetime

    # Fields added dynamically by status endpoints
    minio_exists: Optional[bool] = None # Puede ser None si no se pudo verificar
    milvus_chunk_count: Optional[int] = None # Puede ser None si no se pudo verificar o -1 si hubo error
    message: Optional[str] = None # Mensaje descriptivo del estado actual

    # Validador para convertir metadata de string a dict si es necesario
    @field_validator('metadata', mode='before')
    def metadata_to_dict(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return {"error": "invalid metadata JSON in DB"}
        return v

    class Config:
        validate_assignment = True # Permitir validar al asignar campos dinámicos
        populate_by_name = True # Permitir usar 'id' en lugar de 'document_id'
        json_schema_extra = { # Corregido de schema_extra
            "example": {
                "id": "123e4567-e89b-12d3-a456-426614174000",
                "company_id": "51a66c8f-f6b1-43bd-8038-8768471a8b09",
                "file_name": "document.pdf",
                "file_type": "application/pdf",
                "file_path": "51a66c8f-f6b1-43bd-8038-8768471a8b09/123e4567-e89b-12d3-a456-426614174000/document.pdf",
                "metadata": {"source": "web upload"},
                "status": "processed",
                "chunk_count": 10, # DB value
                "error_message": None,
                "uploaded_at": "2025-04-18T20:00:00Z",
                "updated_at": "2025-04-18T20:30:00Z",
                "minio_exists": True, # Realtime check
                "milvus_chunk_count": 10, # Realtime check (-1 if error)
                "message": "Documento procesado correctamente." # Descriptive message
            }
        }
```

## File: `app\core\__init__.py`
```py

```

## File: `app\core\config.py`
```py
# ingest-service/app/core/config.py
import logging
import os
from typing import Optional, List, Any, Dict, Union
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import (
    RedisDsn, AnyHttpUrl, SecretStr, Field, field_validator, ValidationError,
    ValidationInfo
)
import sys
import json

# --- Service Names en K8s ---
POSTGRES_K8S_SVC = "postgresql-service.nyro-develop.svc.cluster.local" # Corregido nombre servicio
MINIO_K8S_SVC = "minio-service.nyro-develop.svc.cluster.local"
MILVUS_K8S_SVC = "milvus-milvus.default.svc.cluster.local" # Servicio en namespace 'default'
REDIS_K8S_SVC = "redis-service-master.nyro-develop.svc.cluster.local"

# --- Defaults ---
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex"
POSTGRES_K8S_USER_DEFAULT = "postgres"
MINIO_K8S_PORT_DEFAULT = 9000
MINIO_BUCKET_DEFAULT = "ingested-documents"
MILVUS_K8S_PORT_DEFAULT = 19530
MILVUS_DEFAULT_COLLECTION = "document_chunks_haystack"
MILVUS_DEFAULT_INDEX_PARAMS = '{"metric_type": "COSINE", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 256}}'
MILVUS_DEFAULT_SEARCH_PARAMS = '{"metric_type": "COSINE", "params": {"ef": 128}}'
OPENAI_DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_DIM = 1536 # Dimension for text-embedding-3-small & ada-002

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env', env_prefix='INGEST_', env_file_encoding='utf-8',
        case_sensitive=False, extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = "Atenex Ingest Service"
    API_V1_STR: str = "/api/v1/ingest"
    LOG_LEVEL: str = "INFO"

    # --- Celery ---
    CELERY_BROKER_URL: RedisDsn = Field(default_factory=lambda: RedisDsn(f"redis://{REDIS_K8S_SVC}:{REDIS_K8S_PORT_DEFAULT}/0"))
    CELERY_RESULT_BACKEND: RedisDsn = Field(default_factory=lambda: RedisDsn(f"redis://{REDIS_K8S_SVC}:{REDIS_K8S_PORT_DEFAULT}/1"))


    # --- Database ---
    POSTGRES_USER: str = POSTGRES_K8S_USER_DEFAULT
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = POSTGRES_K8S_SVC
    POSTGRES_PORT: int = POSTGRES_K8S_PORT_DEFAULT
    POSTGRES_DB: str = POSTGRES_K8S_DB_DEFAULT

    # --- Milvus ---
    MILVUS_URI: str = Field(default=f"http://{MILVUS_K8S_SVC}:{MILVUS_K8S_PORT_DEFAULT}")
    MILVUS_COLLECTION_NAME: str = MILVUS_DEFAULT_COLLECTION
    MILVUS_METADATA_FIELDS: List[str] = Field(default=["company_id", "document_id", "file_name", "file_type"])
    MILVUS_CONTENT_FIELD: str = "content"
    MILVUS_EMBEDDING_FIELD: str = "embedding"
    MILVUS_INDEX_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_INDEX_PARAMS))
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_SEARCH_PARAMS))

    # --- MinIO ---
    MINIO_ENDPOINT: str = Field(default=f"{MINIO_K8S_SVC}:{MINIO_K8S_PORT_DEFAULT}")
    MINIO_ACCESS_KEY: SecretStr
    MINIO_SECRET_KEY: SecretStr
    MINIO_BUCKET_NAME: str = MINIO_BUCKET_DEFAULT
    MINIO_USE_SECURE: bool = False

    # --- Embeddings (OpenAI for Ingestion) ---
    OPENAI_API_KEY: SecretStr
    OPENAI_EMBEDDING_MODEL: str = OPENAI_DEFAULT_EMBEDDING_MODEL
    EMBEDDING_DIMENSION: int = DEFAULT_EMBEDDING_DIM

    # --- Clients ---
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 2
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    # --- Processing ---
    SUPPORTED_CONTENT_TYPES: List[str] = Field(default=[
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", # DOCX
        "application/msword", # DOC
        "text/plain",
        "text/markdown",
        "text/html"
    ])
    SPLITTER_CHUNK_SIZE: int = 500
    SPLITTER_CHUNK_OVERLAP: int = 50
    SPLITTER_SPLIT_BY: str = "word"

    # --- Validators ---
    @field_validator("LOG_LEVEL")
    @classmethod
    def check_log_level(cls, v: str) -> str:
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        normalized_v = v.upper()
        if normalized_v not in valid_levels: raise ValueError(f"Invalid LOG_LEVEL '{v}'. Must be one of {valid_levels}")
        return normalized_v

    @field_validator('EMBEDDING_DIMENSION', mode='before', check_fields=False)
    @classmethod
    def set_embedding_dimension(cls, v: Optional[int], info: ValidationInfo) -> int:
        config_values = info.data
        model = config_values.get('OPENAI_EMBEDDING_MODEL', OPENAI_DEFAULT_EMBEDDING_MODEL)
        calculated_dim = DEFAULT_EMBEDDING_DIM
        if model == "text-embedding-3-large": calculated_dim = 3072
        elif model in ["text-embedding-3-small", "text-embedding-ada-002"]: calculated_dim = 1536

        if v is not None and v != calculated_dim:
             logging.warning(f"Provided INGEST_EMBEDDING_DIMENSION {v} conflicts with INGEST_OPENAI_EMBEDDING_MODEL {model} ({calculated_dim} expected). Using calculated value: {calculated_dim}")
             return calculated_dim
        elif v is None:
             logging.debug(f"EMBEDDING_DIMENSION not set, defaulting to {calculated_dim} based on model {model}")
             return calculated_dim
        else:
             if v == calculated_dim:
                 logging.debug(f"Provided EMBEDDING_DIMENSION {v} matches model {model}")
             return v

    @field_validator('POSTGRES_PASSWORD', 'MINIO_ACCESS_KEY', 'MINIO_SECRET_KEY', 'OPENAI_API_KEY', mode='before')
    @classmethod
    def check_secret_value_present(cls, v: Any, info: ValidationInfo) -> Any:
        if v is None or v == "":
             field_name = info.field_name if info.field_name else "Unknown Secret Field"
             raise ValueError(f"Required secret field '{field_name}' cannot be empty.")
        return v

    @field_validator('MILVUS_URI', mode='before')
    @classmethod
    def validate_milvus_uri(cls, v: str) -> str:
        if not v.startswith("http://") and not v.startswith("https://"):
             raise ValueError(f"Invalid MILVUS_URI format: '{v}'. Must start with 'http://' or 'https://'")
        return v

# --- Instancia Global ---
temp_log = logging.getLogger("ingest_service.config.loader")
if not temp_log.handlers:
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(levelname)-8s [%(asctime)s] [%(name)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    handler.setFormatter(formatter)
    temp_log.addHandler(handler)
    temp_log.setLevel(logging.INFO)

try:
    temp_log.info("Loading Ingest Service settings...")
    settings = Settings()
    temp_log.info("--- Ingest Service Settings Loaded ---")
    temp_log.info(f"  PROJECT_NAME:             {settings.PROJECT_NAME}")
    temp_log.info(f"  LOG_LEVEL:                {settings.LOG_LEVEL}")
    temp_log.info(f"  API_V1_STR:               {settings.API_V1_STR}")
    temp_log.info(f"  CELERY_BROKER_URL:        {settings.CELERY_BROKER_URL}")
    temp_log.info(f"  CELERY_RESULT_BACKEND:    {settings.CELERY_RESULT_BACKEND}")
    temp_log.info(f"  POSTGRES_SERVER:          {settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}")
    temp_log.info(f"  POSTGRES_DB:              {settings.POSTGRES_DB}")
    temp_log.info(f"  POSTGRES_USER:            {settings.POSTGRES_USER}")
    temp_log.info(f"  POSTGRES_PASSWORD:        *** SET ***")
    temp_log.info(f"  MILVUS_URI:               {settings.MILVUS_URI}")
    temp_log.info(f"  MILVUS_COLLECTION_NAME:   {settings.MILVUS_COLLECTION_NAME}")
    temp_log.info(f"  MINIO_ENDPOINT:           {settings.MINIO_ENDPOINT}")
    temp_log.info(f"  MINIO_BUCKET_NAME:        {settings.MINIO_BUCKET_NAME}")
    temp_log.info(f"  MINIO_ACCESS_KEY:         *** SET ***")
    temp_log.info(f"  MINIO_SECRET_KEY:         *** SET ***")
    temp_log.info(f"  OPENAI_API_KEY:           *** SET ***")
    temp_log.info(f"  OPENAI_EMBEDDING_MODEL:   {settings.OPENAI_EMBEDDING_MODEL}")
    temp_log.info(f"  EMBEDDING_DIMENSION:      {settings.EMBEDDING_DIMENSION}")
    temp_log.info(f"  SUPPORTED_CONTENT_TYPES:  {settings.SUPPORTED_CONTENT_TYPES}")
    temp_log.info(f"  SPLITTER_CHUNK_SIZE:      {settings.SPLITTER_CHUNK_SIZE}")
    temp_log.info(f"  SPLITTER_CHUNK_OVERLAP:   {settings.SPLITTER_CHUNK_OVERLAP}")
    temp_log.info(f"------------------------------------")

except (ValidationError, ValueError) as e:
    error_details = ""
    if isinstance(e, ValidationError):
        try: error_details = f"\nValidation Errors:\n{e.json(indent=2)}"
        except Exception: error_details = f"\nRaw Errors: {e.errors()}"
    temp_log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    temp_log.critical(f"! FATAL: Ingest Service configuration validation failed:{error_details}")
    temp_log.critical(f"! Check environment variables (prefixed with INGEST_) or .env file.")
    temp_log.critical(f"! Original Error: {e}")
    temp_log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    sys.exit(1)
except Exception as e:
    temp_log.exception(f"FATAL: Unexpected error loading Ingest Service settings: {e}")
    sys.exit(1)
```

## File: `app\core\logging_config.py`
```py
import logging
import sys
import structlog
from app.core.config import settings
import os

def setup_logging():
    """Configura el logging estructurado con structlog."""

    # Disable existing handlers if running in certain environments (like Uvicorn default)
    # to avoid duplicate logs. This might need adjustment based on deployment.
    # logging.getLogger().handlers.clear()

    # Determine if running inside Celery worker
    is_celery_worker = "celery" in sys.argv[0]

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.LOG_LEVEL == logging.DEBUG:
         # Add caller info only in debug mode for performance
         shared_processors.append(structlog.processors.CallsiteParameterAdder(
             {
                 structlog.processors.CallsiteParameter.FILENAME,
                 structlog.processors.CallsiteParameter.LINENO,
             }
         ))

    # Configure structlog
    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Configure the formatter for stdlib logging
    formatter = structlog.stdlib.ProcessorFormatter(
        # These run ONCE per log structuralization
        foreign_pre_chain=shared_processors,
         # These run on EVERY record
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(), # Render as JSON
        ],
    )

    # Configure root logger handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    # Avoid adding handler twice if already configured (e.g., by Uvicorn/Gunicorn)
    if not any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers):
         root_logger.addHandler(handler)

    root_logger.setLevel(settings.LOG_LEVEL)

    # Silence verbose libraries
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("asyncpg").setLevel(logging.WARNING)
    logging.getLogger("haystack").setLevel(logging.INFO) # Or DEBUG for more Haystack details
    logging.getLogger("milvus_haystack").setLevel(logging.INFO) # Adjust as needed

    log = structlog.get_logger("ingest_service")
    log.info("Logging configured", log_level=settings.LOG_LEVEL, is_celery_worker=is_celery_worker)
```

## File: `app\db\__init__.py`
```py

```

## File: `app\db\base.py`
```py

```

## File: `app\db\postgres_client.py`
```py
# ingest-service/app/db/postgres_client.py
import uuid
from typing import Any, Optional, Dict, List
import asyncpg
import structlog
import json
from datetime import datetime, timezone

from app.core.config import settings
from app.models.domain import DocumentStatus

log = structlog.get_logger(__name__)

_pool: Optional[asyncpg.Pool] = None

# --- Pool Management (Sin cambios) ---
async def get_db_pool() -> asyncpg.Pool:
    global _pool
    if (_pool is None or _pool._closed):
        log.info("Creating PostgreSQL connection pool...", host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT, user=settings.POSTGRES_USER, db=settings.POSTGRES_DB)
        try:
            def _json_encoder(value): return json.dumps(value)
            def _json_decoder(value): return json.loads(value)
            async def init_connection(conn):
                await conn.set_type_codec('jsonb', encoder=_json_encoder, decoder=_json_decoder, schema='pg_catalog', format='text')
                await conn.set_type_codec('json', encoder=_json_encoder, decoder=_json_decoder, schema='pg_catalog', format='text')

            _pool = await asyncpg.create_pool(
                user=settings.POSTGRES_USER, password=settings.POSTGRES_PASSWORD.get_secret_value(),
                database=settings.POSTGRES_DB, host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT,
                min_size=2, max_size=10, timeout=30.0, command_timeout=60.0,
                init=init_connection, statement_cache_size=0
            )
            log.info("PostgreSQL connection pool created successfully.")
        except (asyncpg.exceptions.InvalidPasswordError, OSError, ConnectionRefusedError) as conn_err:
            log.critical("CRITICAL: Failed to connect to PostgreSQL", error=str(conn_err), exc_info=True)
            _pool = None; raise ConnectionError(f"Failed to connect to PostgreSQL: {conn_err}") from conn_err
        except Exception as e:
            log.critical("CRITICAL: Failed to create PostgreSQL connection pool", error=str(e), exc_info=True)
            _pool = None; raise RuntimeError(f"Failed to create PostgreSQL pool: {e}") from e
    return _pool

async def close_db_pool():
    global _pool
    if (_pool and not _pool._closed): log.info("Closing PostgreSQL connection pool..."); await _pool.close(); _pool = None; log.info("PostgreSQL connection pool closed.")
    elif _pool and _pool._closed: log.warning("Attempted to close an already closed PostgreSQL pool."); _pool = None
    else: log.info("No active PostgreSQL connection pool to close.")

async def check_db_connection() -> bool:
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            async with conn.transaction(): result = await conn.fetchval("SELECT 1")
        return result == 1
    except Exception as e: log.error("Database connection check failed", error=str(e)); return False

# --- Document Operations ---
async def create_document(document_id: uuid.UUID, company_id: uuid.UUID, file_name: str, file_type: str, metadata: Dict[str, Any]) -> None:
    """Crea un registro inicial para un documento en la base de datos."""
    pool = await get_db_pool()
    query = """
    INSERT INTO documents (id, company_id, file_name, file_type, file_path, metadata, status, chunk_count, error_message, uploaded_at, updated_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NULL, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC');
    """
    # Usar "" como placeholder inicial para file_path y DocumentStatus.UPLOADED
    params = [document_id, company_id, file_name, file_type, "", json.dumps(metadata), DocumentStatus.UPLOADED.value, 0]
    insert_log = log.bind(company_id=str(company_id), filename=file_name, doc_id=str(document_id))
    try:
        async with pool.acquire() as conn:
            await conn.execute(query, *params)
        insert_log.info("Document record created in PostgreSQL")
    except Exception as e:
        insert_log.error("Failed to create document record", error=str(e), exc_info=True)
        raise

async def update_document_status(
    document_id: uuid.UUID,
    status: DocumentStatus,
    file_path: Optional[str] = None,
    chunk_count: Optional[int] = None,
    error_message: Optional[str] = None
) -> bool:
    """Actualiza el estado, file_path, chunk_count y/o error_message de un documento."""
    pool = await get_db_pool()
    params: List[Any] = [document_id]
    fields: List[str] = ["status = $2", "updated_at = NOW() AT TIME ZONE 'UTC'"]
    params.append(status.value)
    param_index = 3
    if file_path is not None:
        fields.append(f"file_path = ${param_index}"); params.append(file_path); param_index += 1
    if chunk_count is not None:
        fields.append(f"chunk_count = ${param_index}"); params.append(chunk_count); param_index += 1

    # Manejo de error_message: Limpiar si no es estado ERROR, setear si es ERROR y se provee
    if status == DocumentStatus.ERROR:
        # Solo añadir/actualizar error_message si se proporciona uno
        if error_message is not None:
            fields.append(f"error_message = ${param_index}"); params.append(error_message); param_index += 1
        # Si status es ERROR pero no viene mensaje, se mantiene el existente (no añadir "error_message = NULL")
    else:
        # Si el status NO es ERROR, limpiar el mensaje de error explícitamente
        fields.append("error_message = NULL")

    set_clause = ", ".join(fields)
    query = f"UPDATE documents SET {set_clause} WHERE id = $1;"
    update_log = log.bind(document_id=str(document_id), new_status=status.value)
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(query, *params)
            # Check if update affected any row
            if result == 'UPDATE 0':
                 update_log.warning("Attempted to update status for non-existent document_id")
                 return False
        update_log.info("Document status updated in PostgreSQL")
        return True
    except Exception as e:
        update_log.error("Failed to update document status", error=str(e), exc_info=True)
        raise

async def get_document_status(document_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    pool = await get_db_pool()
    query = """
    SELECT id, company_id, file_name, file_type, file_path, metadata, status, chunk_count, error_message, uploaded_at, updated_at
    FROM documents WHERE id = $1;
    """
    get_log = log.bind(document_id=str(document_id))
    try:
        async with pool.acquire() as conn:
            record = await conn.fetchrow(query, document_id)
        if not record:
            get_log.warning("Queried non-existent document_id")
            return None
        return dict(record)
    except Exception as e:
        get_log.error("Failed to get document status", error=str(e), exc_info=True)
        raise

async def list_documents_by_company(company_id: uuid.UUID, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    pool = await get_db_pool()
    query = """
    SELECT id, company_id, file_name, file_type, file_path, metadata, status, chunk_count, error_message, uploaded_at, updated_at
    FROM documents WHERE company_id = $1 ORDER BY updated_at DESC LIMIT $2 OFFSET $3;
    """
    list_log = log.bind(company_id=str(company_id), limit=limit, offset=offset)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, company_id, limit, offset)
        return [dict(r) for r in rows]
    except Exception as e:
        list_log.error("Failed to list documents by company", error=str(e), exc_info=True)
        raise

async def delete_document(document_id: uuid.UUID) -> bool:
    pool = await get_db_pool()
    query = "DELETE FROM documents WHERE id = $1 RETURNING id;"
    delete_log = log.bind(document_id=str(document_id))
    try:
        async with pool.acquire() as conn:
            deleted_id = await conn.fetchval(query, document_id)
        delete_log.info("Document deleted from PostgreSQL", deleted_id=str(deleted_id))
        return deleted_id is not None
    except Exception as e:
        delete_log.error("Error deleting document record", error=str(e), exc_info=True)
        raise

# --- Funciones de Chat (Se mantienen, sin cambios) ---
async def create_chat(user_id: uuid.UUID, company_id: uuid.UUID, title: Optional[str] = None) -> uuid.UUID:
    pool = await get_db_pool()
    chat_id = uuid.uuid4()
    query = """INSERT INTO chats (id, user_id, company_id, title, created_at, updated_at) VALUES ($1, $2, $3, $4, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC') RETURNING id;"""
    try:
        async with pool.acquire() as conn:
            result = await conn.fetchval(query, chat_id, user_id, company_id, title or f"Chat {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}")
            return result
    except Exception as e:
        log.error("Failed create_chat (ingest context)", error=str(e))
        raise

async def get_user_chats(user_id: uuid.UUID, company_id: uuid.UUID, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    pool = await get_db_pool()
    query = """SELECT id, title, updated_at FROM chats WHERE user_id = $1 AND company_id = $2 ORDER BY updated_at DESC LIMIT $3 OFFSET $4;"""
    try:
        async with pool.acquire() as conn: rows = await conn.fetch(query, user_id, company_id, limit, offset); return [dict(row) for row in rows]
    except Exception as e: log.error("Failed get_user_chats (ingest context)", error=str(e)); raise

async def check_chat_ownership(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    pool = await get_db_pool()
    query = "SELECT EXISTS (SELECT 1 FROM chats WHERE id = $1 AND user_id = $2 AND company_id = $3);"
    try:
        async with pool.acquire() as conn: exists = await conn.fetchval(query, chat_id, user_id, company_id); return exists is True
    except Exception as e: log.error("Failed check_chat_ownership (ingest context)", error=str(e)); return False

async def get_chat_messages(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    pool = await get_db_pool(); owner = await check_chat_ownership(chat_id, user_id, company_id)
    if not owner: return []
    messages_query = """SELECT id, role, content, sources, created_at FROM messages WHERE chat_id = $1 ORDER BY created_at ASC LIMIT $2 OFFSET $3;"""
    try:
        async with pool.acquire() as conn: message_rows = await conn.fetch(messages_query, chat_id, limit, offset); return [dict(row) for row in message_rows]
    except Exception as e: log.error("Failed get_chat_messages (ingest context)", error=str(e)); raise

async def save_message(chat_id: uuid.UUID, role: str, content: str, sources: Optional[List[Dict[str, Any]]] = None) -> uuid.UUID:
    pool = await get_db_pool(); message_id = uuid.uuid4()
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                update_chat_query = "UPDATE chats SET updated_at = NOW() AT TIME ZONE 'UTC' WHERE id = $1 RETURNING id;"; chat_updated = await conn.fetchval(update_chat_query, chat_id)
                if not chat_updated: raise ValueError(f"Chat {chat_id} not found (ingest context).")
                insert_message_query = """INSERT INTO messages (id, chat_id, role, content, sources, created_at) VALUES ($1, $2, $3, $4, $5, NOW() AT TIME ZONE 'UTC') RETURNING id;"""
                result = await conn.fetchval(insert_message_query, message_id, chat_id, role, content, json.dumps(sources or [])); return result
            except Exception as e: log.error("Failed save_message (ingest context)", error=str(e)); raise

async def delete_chat(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    pool = await get_db_pool()
    query = "DELETE FROM chats WHERE id = $1 AND user_id = $2 AND company_id = $3 RETURNING id;"; delete_log = log.bind(chat_id=str(chat_id), user_id=str(user_id))
    try:
        async with pool.acquire() as conn: deleted_id = await conn.fetchval(query, chat_id, user_id, company_id); return deleted_id is not None
    except Exception as e: delete_log.error("Failed to delete chat (ingest context)", error=str(e)); raise
```

## File: `app\main.py`
```py
# ingest-service/app/main.py
from fastapi import FastAPI, HTTPException, status as fastapi_status, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
import structlog
import uvicorn
import logging
import sys
import asyncio
import time
import uuid
from contextlib import asynccontextmanager # Importar asynccontextmanager

# Configurar logging ANTES de importar otros módulos
from app.core.logging_config import setup_logging
setup_logging()

# Importaciones post-logging
from app.core.config import settings
log = structlog.get_logger("ingest_service.main")
from app.api.v1.endpoints import ingest
from app.db import postgres_client

# Flag global para indicar si el servicio está listo
SERVICE_READY = False
DB_CONNECTION_OK = False # Flag específico para DB

# --- Lifespan Manager (Startup/Shutdown) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global SERVICE_READY, DB_CONNECTION_OK
    log.info("Executing Ingest Service startup sequence...")
    db_pool_ok_startup = False
    try:
        # Intenta obtener y verificar el pool de DB
        await postgres_client.get_db_pool()
        db_pool_ok_startup = await postgres_client.check_db_connection()
        if db_pool_ok_startup:
            log.info("PostgreSQL connection pool initialized and verified successfully.")
            DB_CONNECTION_OK = True
            SERVICE_READY = True # Marcar listo si DB está ok
        else:
            log.critical("PostgreSQL connection check FAILED after pool initialization attempt.")
            DB_CONNECTION_OK = False
            SERVICE_READY = False
    except Exception as e:
        log.critical("CRITICAL FAILURE during PostgreSQL startup verification", error=str(e), exc_info=True)
        DB_CONNECTION_OK = False
        SERVICE_READY = False

    if SERVICE_READY:
        log.info("Ingest Service startup successful. SERVICE IS READY.")
    else:
        log.error("Ingest Service startup completed BUT SERVICE IS NOT READY (DB connection issue).")

    yield # La aplicación se ejecuta aquí

    # --- Shutdown ---
    log.info("Executing Ingest Service shutdown sequence...")
    await postgres_client.close_db_pool()
    log.info("Shutdown sequence complete.")


# --- Creación de la App FastAPI ---
app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.1.2", # Incrementar versión
    description="Microservicio Atenex para ingesta de documentos usando Haystack.",
    lifespan=lifespan
)

# --- Middlewares ---
@app.middleware("http")
async def add_request_context_timing_logging(request: Request, call_next):
    start_time = time.perf_counter()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    # LLM_COMMENT: Bind request context early
    structlog.contextvars.bind_contextvars(request_id=request_id)
    req_log = log.bind(method=request.method, path=request.url.path)
    req_log.info("Request received")
    request.state.request_id = request_id # Store for access in endpoints if needed

    response = None
    try:
        response = await call_next(request)
        process_time_ms = (time.perf_counter() - start_time) * 1000
        # LLM_COMMENT: Bind response context for final log
        resp_log = req_log.bind(status_code=response.status_code, duration_ms=round(process_time_ms, 2))
        log_level = "warning" if 400 <= response.status_code < 500 else "error" if response.status_code >= 500 else "info"
        getattr(resp_log, log_level)("Request finished")
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
    except Exception as e:
        process_time_ms = (time.perf_counter() - start_time) * 1000
        # LLM_COMMENT: Log unhandled exceptions at middleware level
        exc_log = req_log.bind(status_code=500, duration_ms=round(process_time_ms, 2))
        exc_log.exception("Unhandled exception during request processing")
        response = JSONResponse(
            status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal Server Error"}
        )
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
    finally:
         # LLM_COMMENT: Clear contextvars after request is done
         structlog.contextvars.clear_contextvars()
    return response

# --- Exception Handlers ---
@app.exception_handler(ResponseValidationError)
async def response_validation_exception_handler(request: Request, exc: ResponseValidationError):
    log.error("Response Validation Error", errors=exc.errors(), exc_info=True)
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Error de validación en la respuesta", "errors": exc.errors()},
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    log_level = log.warning if exc.status_code < 500 else log.error
    log_level("HTTP Exception", status_code=exc.status_code, detail=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail if isinstance(exc.detail, str) else "Error HTTP"},
        headers=getattr(exc, "headers", None)
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    log.warning("Request Validation Error", errors=exc.errors())
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Error de validación en la petición", "errors": exc.errors()},
    )

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.exception("Excepción no controlada") # Log con traceback
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Error interno del servidor"}
    )

# --- Router Inclusion ---
# ¡¡¡¡NUNCA MODIFICAR ESTA LÍNEA NI EL PREFIJO DE RUTA!!!
# El prefijo DEBE ser settings.API_V1_STR == '/api/v1/ingest' para que el API Gateway funcione correctamente.
# Si cambias esto, romperás la integración y el proxy de rutas. Si tienes dudas, consulta con el equipo de plataforma.
app.include_router(ingest.router, prefix=settings.API_V1_STR, tags=["Ingestion"])
log.info(f"Included ingestion router with prefix: {settings.API_V1_STR}")

# --- Root Endpoint / Health Check ---
@app.get("/", tags=["Health Check"], status_code=fastapi_status.HTTP_200_OK, response_class=PlainTextResponse)
async def health_check():
    """
    Simple health check endpoint. Returns 200 OK if the app is running.
    """
    return PlainTextResponse("OK", status_code=fastapi_status.HTTP_200_OK)

# --- Local execution ---
if __name__ == "__main__":
    port = 8001 # Default port for ingest-service
    log_level_str = settings.LOG_LEVEL.lower()
    print(f"----- Starting {settings.PROJECT_NAME} locally on port {port} -----")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True, log_level=log_level_str)
```

## File: `app\models\__init__.py`
```py

```

## File: `app\models\domain.py`
```py
from enum import Enum

class DocumentStatus(str, Enum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    PROCESSED = "processed"
    INDEXED = "indexed" # Podríamos unir processed e indexed
    ERROR = "error"
```

## File: `app\services\__init__.py`
```py

```

## File: `app\services\base_client.py`
```py
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import structlog
from typing import Any, Dict, Optional

from app.core.config import settings

log = structlog.get_logger(__name__)

class BaseServiceClient:
    """Cliente HTTP base asíncrono con reintentos."""

    def __init__(self, base_url: str, service_name: str):
        self.base_url = base_url
        self.service_name = service_name
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=settings.HTTP_CLIENT_TIMEOUT
        )

    async def close(self):
        """Cierra el cliente HTTP."""
        await self.client.aclose()
        log.info(f"{self.service_name} client closed.")

    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR),
        retry=retry_if_exception_type(httpx.RequestError)
    )
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        """Realiza una petición HTTP con reintentos."""
        log.debug(f"Requesting {self.service_name}", method=method, endpoint=endpoint, params=params)
        try:
            response = await self.client.request(
                method=method,
                url=endpoint,
                params=params,
                json=json,
                data=data,
                files=files,
                headers=headers,
            )
            response.raise_for_status()
            log.info(f"Received response from {self.service_name}", status_code=response.status_code)
            return response
        except httpx.HTTPStatusError as e:
            log.error(f"HTTP error from {self.service_name}", status_code=e.response.status_code, detail=e.response.text)
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            log.error(f"Network error when calling {self.service_name}", error=str(e))
            raise
        except Exception as e:
            log.error(f"Unexpected error when calling {self.service_name}", error=str(e), exc_info=True)
            raise
```

## File: `app\services\minio_client.py`
```py
# ingest-service/app/services/minio_client.py
import io
import uuid
from typing import IO, BinaryIO
from minio import Minio
from minio.error import S3Error
import structlog
import asyncio

from app.core.config import settings

log = structlog.get_logger(__name__)

class MinioStorageClient:
    """Cliente para interactuar con MinIO usando el bucket configurado."""

    def __init__(self):
        self.bucket_name = settings.MINIO_BUCKET_NAME
        try:
            self.client = Minio(
                settings.MINIO_ENDPOINT,
                access_key=settings.MINIO_ACCESS_KEY.get_secret_value(),
                secret_key=settings.MINIO_SECRET_KEY.get_secret_value(),
                secure=settings.MINIO_USE_SECURE
            )
            self._ensure_bucket_exists()
            log.info("MinIO client initialized", endpoint=settings.MINIO_ENDPOINT, bucket=self.bucket_name)
        except Exception as e:
            log.critical("CRITICAL: Failed to initialize MinIO client", bucket=self.bucket_name, error=str(e), exc_info=True)
            raise RuntimeError(f"MinIO client initialization failed: {e}") from e

    def _ensure_bucket_exists(self):
        """Crea el bucket especificado si no existe (síncrono)."""
        try:
            found = self.client.bucket_exists(self.bucket_name)
            if not found:
                self.client.make_bucket(self.bucket_name)
                log.info(f"MinIO bucket '{self.bucket_name}' created.")
            else:
                log.debug(f"MinIO bucket '{self.bucket_name}' already exists.")
        except S3Error as e:
            log.error(f"Error checking/creating MinIO bucket '{self.bucket_name}'", error=str(e), exc_info=True)
            raise

    async def upload_file(
        self,
        company_id: uuid.UUID,
        document_id: uuid.UUID,
        file_name: str,
        file_content_stream: IO[bytes],
        content_type: str,
        content_length: int
    ) -> str:
        object_name = f"{str(company_id)}/{str(document_id)}/{file_name}"
        upload_log = log.bind(bucket=self.bucket_name, object_name=object_name, content_type=content_type, length=content_length)
        upload_log.info("Queueing file upload to MinIO executor")

        loop = asyncio.get_running_loop()
        def _upload():
            file_content_stream.seek(0)
            return self.client.put_object(
                bucket_name=self.bucket_name,
                object_name=object_name,
                data=file_content_stream,
                length=content_length,
                content_type=content_type
            )
        try:
            await loop.run_in_executor(None, _upload)
            upload_log.info("File uploaded successfully to MinIO via executor")
            return object_name
        except S3Error as e:
            upload_log.error("Failed to upload file to MinIO", error=str(e), code=e.code)
            raise IOError(f"Failed to upload to storage: {e.code}") from e
        except Exception as e:
            upload_log.error("Unexpected error during file upload", error=str(e), exc_info=True)
            raise IOError("Unexpected storage upload error") from e

    def download_file_stream_sync(self, object_name: str) -> io.BytesIO:
        """Operación SÍNCRONA para descargar un archivo a BytesIO."""
        download_log = log.bind(bucket=self.bucket_name, object_name=object_name)
        download_log.info("Downloading file from MinIO (sync operation starting)...")
        response = None
        try:
            response = self.client.get_object(self.bucket_name, object_name)
            file_data = response.read()
            file_stream = io.BytesIO(file_data)
            download_log.info(f"File downloaded successfully from MinIO (sync, {len(file_data)} bytes)")
            file_stream.seek(0)
            return file_stream
        except S3Error as e:
            download_log.error("Failed to download file from MinIO (sync)", error=str(e), code=e.code, exc_info=False)
            if e.code == 'NoSuchKey':
                 raise FileNotFoundError(f"Object not found in MinIO bucket '{self.bucket_name}': {object_name}") from e
            else:
                 raise IOError(f"S3 error downloading file {object_name}: {e.code}") from e
        except Exception as e:
             download_log.error("Unexpected error during sync file download", error=str(e), exc_info=True)
             raise IOError(f"Unexpected error downloading file {object_name}") from e
        finally:
            if response:
                response.close()
                response.release_conn()

    async def download_file_stream(self, object_name: str) -> io.BytesIO:
        """Descarga un archivo de MinIO como BytesIO de forma asíncrona."""
        download_log = log.bind(bucket=self.bucket_name, object_name=object_name)
        download_log.info("Queueing file download from MinIO executor")
        loop = asyncio.get_running_loop()
        try:
            file_stream = await loop.run_in_executor(None, self.download_file_stream_sync, object_name)
            download_log.info("File download successful via executor")
            return file_stream
        except FileNotFoundError:
            download_log.error("File not found in MinIO via executor", object_name=object_name)
            raise
        except Exception as e:
            download_log.error("Error downloading file via executor", error=str(e), error_type=type(e).__name__, exc_info=True)
            raise IOError(f"Failed to download file via executor: {e}") from e

    async def file_exists(self, object_name: str) -> bool:
        check_log = log.bind(bucket=self.bucket_name, object_name=object_name)
        loop = asyncio.get_running_loop()
        def _stat():
            self.client.stat_object(self.bucket_name, object_name)
            return True
        try:
            return await loop.run_in_executor(None, _stat)
        except S3Error as e:
            if getattr(e, 'code', None) in ('NoSuchKey', 'NoSuchBucket'):
                check_log.warning("Object not found in MinIO", code=e.code)
                return False
            check_log.error("Error checking MinIO object existence", error=str(e), code=e.code)
            raise IOError(f"Error checking storage existence: {e.code}") from e
        except Exception as e:
            check_log.error("Unexpected error checking MinIO object existence", error=str(e), exc_info=True)
            raise IOError("Unexpected error checking storage existence") from e

    async def delete_file(self, object_name: str) -> None:
        delete_log = log.bind(bucket=self.bucket_name, object_name=object_name)
        delete_log.info("Queueing file deletion from MinIO executor")
        loop = asyncio.get_running_loop()
        def _remove():
            self.client.remove_object(self.bucket_name, object_name)
        try:
            await loop.run_in_executor(None, _remove)
            delete_log.info("File deleted successfully from MinIO")
        except S3Error as e:
            delete_log.error("Failed to delete file from MinIO", error=str(e), code=e.code)
            # No raise para que eliminación parcial no bloquee flujo
        except Exception as e:
            delete_log.error("Unexpected error during file deletion", error=str(e), exc_info=True)
            raise IOError("Unexpected storage deletion error") from e
```

## File: `app\tasks\__init__.py`
```py

```

## File: `app\tasks\celery_app.py`
```py
from celery import Celery
from app.core.config import settings
import structlog

log = structlog.get_logger(__name__)

celery_app = Celery(
    "ingest_tasks",
    broker=str(settings.CELERY_BROKER_URL),
    backend=str(settings.CELERY_RESULT_BACKEND),
    include=["app.tasks.process_document"] # Importante para que Celery descubra la tarea
)

# Configuración opcional de Celery
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Ajustar concurrencia y otros parámetros según sea necesario
    # worker_concurrency=4,
    task_track_started=True,
    # Configuración de reintentos por defecto (puede sobreescribirse por tarea)
    task_reject_on_worker_lost=True,
    task_acks_late=True,
)

log.info("Celery app configured", broker=settings.CELERY_BROKER_URL)
```

## File: `app\tasks\process_document.py`
```py
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
```

## File: `app\utils\__init__.py`
```py

```

## File: `app\utils\helpers.py`
```py

```

## File: `pyproject.toml`
```toml
[tool.poetry]
name = "ingest-service"
version = "0.1.2" # Incremento de versión a 0.1.2
description = "Ingest service for Atenex B2B SaaS (Haystack/Postgres/Minio/Milvus)" # Descripción actualizada
authors = ["Atenex Team <dev@atenex.com>"] # Autor actualizado
readme = "README.md"

[tool.poetry.dependencies]
python = "^3.10"
fastapi = "^0.110.0"
uvicorn = {extras = ["standard"], version = "^0.28.0"}
gunicorn = "^21.2.0"
pydantic = {extras = ["email"], version = "^2.6.4"}
pydantic-settings = "^2.2.1"
celery = {extras = ["redis"], version = "^5.3.6"}
gevent = "^23.9.1" # Necesario para el pool de workers de Celery
asyncpg = "^0.29.0" # Para PostgreSQL directo
tenacity = "^8.2.3"
python-multipart = "^0.0.9" # Para subir archivos
structlog = "^24.1.0"
minio = "^7.1.17"

# --- Haystack Dependencies ---
haystack-ai = "^2.0.1" # O la versión estable que uses de Haystack 2.x
openai = "^1.14.3" # Para embeddings
# --- Asegurar pymilvus explícitamente ---
pymilvus = "^2.4.1" # Verifica compatibilidad con tu versión de Milvus
milvus-haystack = "^0.0.6" # Integración Milvus con Haystack 2.x

# --- Haystack Converter Dependencies ---
pypdf = "^4.0.1" # Para PDFs
python-docx = "^1.1.0" # Para DOCX
markdown = "^3.5.1" # Para Markdown (asegura última versión)
beautifulsoup4 = "^4.12.3" # Para HTML

# --- CORRECCIÓN: httpx definido UNA SOLA VEZ con extras ---
# Cliente HTTP asíncrono
httpx = {extras = ["http2"], version = "^0.27.0"}
# Dependencia necesaria para httpx[http2]
h2 = "^4.1.0"

[tool.poetry.group.dev.dependencies] # Grupo dev corregido
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"
# httpx ya está en dependencias principales

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api" 
```
