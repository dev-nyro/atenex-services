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
└── tasks
    ├── __init__.py
    ├── celery_app.py
    └── process_document.py
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
# LLM_FLAG: ADD_IMPORT - Needed for logging.WARNING used in retry strategy
import logging # <--- IMPORTACIÓN AÑADIDA

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
# LLM_FLAG: SENSITIVE_DEPENDENCY - Database client module
from app.db import postgres_client as db_client
from app.models.domain import DocumentStatus
# LLM_FLAG: SENSITIVE_DEPENDENCY - API Schema module
from app.api.v1.schemas import IngestResponse, StatusResponse, PaginatedStatusResponse, ErrorDetail
# LLM_FLAG: SENSITIVE_DEPENDENCY - Minio client module (Corrected name)
from app.services.minio_client import MinioClient, MinioError
# LLM_FLAG: SENSITIVE_DEPENDENCY - Celery app instance
from app.tasks.celery_app import celery_app
# LLM_FLAG: SENSITIVE_DEPENDENCY - Celery task signature
from app.tasks.process_document import process_document_haystack_task

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Helper Functions ---

def get_minio_client():
    """Dependency to get Minio client instance."""
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Minio Client Dependency
    try:
        # Instantiates MinioClient using settings from config
        # The MinioClient class itself handles the connection logic
        client = MinioClient()
        return client
    except Exception as e:
        # Log exception if MinioClient init fails (should be rare if config is ok)
        log.exception("Failed to initialize MinioClient dependency", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage service configuration error."
        )
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Minio Client Dependency

# Define retry strategy for database operations within API requests
# LLM_FLAG: SENSITIVE_RETRY_LOGIC - API DB Retry Strategy
api_db_retry_strategy = retry(
    stop=stop_after_attempt(2), # Fewer retries for API context
    wait=wait_fixed(1),
    retry=retry_if_exception_type((asyncpg.exceptions.PostgresConnectionError, TimeoutError, OSError)),
    # Use the imported logging module here
    before_sleep=before_sleep_log(log, logging.WARNING) # Correct usage
)

@asynccontextmanager
async def get_db_conn():
    """Provides a single connection from the pool for API request context."""
    # LLM_FLAG: SENSITIVE_DB_CONNECTION - DB Connection Pool Management
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
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Milvus Sync Init Helper
    api_log = log.bind(component="MilvusHelperSync")
    api_log.debug("Initializing MilvusDocumentStore for API helper...")
    try:
        store = MilvusDocumentStore(
            connection_args={"uri": settings.MILVUS_URI},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            # No embedding_dim needed here
        )
        api_log.debug("MilvusDocumentStore initialized successfully for API helper.")
        return store
    except TypeError as te:
        api_log.error("MilvusDocumentStore init TypeError in API helper", error=str(te), exc_info=True)
        raise RuntimeError(f"Milvus TypeError (check arguments): {te}") from te
    except Exception as e:
        api_log.exception("Failed to initialize MilvusDocumentStore for API helper", error=str(e))
        raise RuntimeError(f"Milvus Store Initialization Error for API helper: {e}") from e
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Milvus Sync Init Helper

def _get_milvus_chunk_count_sync(document_id: str, company_id: str) -> int:
    """Synchronously counts chunks in Milvus for a specific document."""
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Milvus Sync Count Helper
    count_log = log.bind(document_id=document_id, company_id=company_id, component="MilvusHelperSync")
    try:
        store = _initialize_milvus_store_sync()
        # Construct filters based on metadata Haystack uses
        # LLM_FLAG: CRITICAL_FILTERING - Ensure company_id filter is correct
        filters = {
            "operator": "AND", # Top-level operator
            "conditions": [
                 # Assuming meta fields are directly searchable
                {"field": "meta.document_id", "operator": "==", "value": document_id},
                {"field": "meta.company_id", "operator": "==", "value": company_id},
            ]
        }
        # Alternative filter structure if meta fields are nested differently
        # filters = {"document_id": document_id, "company_id": company_id} # Check MilvusDocumentStore expected format

        count = store.count_documents(filters=filters)
        count_log.info("Milvus chunk count successful", count=count)
        return count
    except RuntimeError as re: # Catch init errors
        count_log.error("Failed to get Milvus count due to store init error", error=str(re))
        return -1 # Indicate error with -1
    except Exception as e:
        count_log.exception("Error counting documents in Milvus", error=str(e))
        return -1 # Indicate error with -1
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Milvus Sync Count Helper

def _delete_milvus_sync(document_id: str, company_id: str) -> bool:
    """Synchronously deletes chunks from Milvus for a specific document."""
     # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Milvus Sync Delete Helper
    delete_log = log.bind(document_id=document_id, company_id=company_id, component="MilvusHelperSync")
    try:
        store = _initialize_milvus_store_sync()
        # Construct filters based on metadata
        # LLM_FLAG: CRITICAL_FILTERING - Ensure company_id filter is correct for delete
        filters = {
            "operator": "AND", # Top-level operator
            "conditions": [
                {"field": "meta.document_id", "operator": "==", "value": document_id},
                {"field": "meta.company_id", "operator": "==", "value": company_id},
            ]
        }
        store.delete_documents(filters=filters)
        delete_log.info("Milvus delete operation executed.")
        return True
    except RuntimeError as re: # Catch init errors
        delete_log.error("Failed to delete Milvus chunks due to store init error", error=str(re))
        return False
    except Exception as e:
        delete_log.exception("Error deleting documents from Milvus", error=str(e))
        return False
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Milvus Sync Delete Helper

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
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Upload Endpoint Logic
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
    metadata = {} # Default to empty dict if not provided
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
            # LLM_FLAG: CRITICAL_DB_CALL - Check for existing document
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
                 pass # Allow creation of new record

    except ValueError:
        endpoint_log.error("Invalid Company ID format provided", company_id_received=company_id)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        endpoint_log.exception("Error checking for duplicate document", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error checking for duplicates.")

    # 4. Create Initial Document Record (PostgreSQL)
    document_id = uuid.uuid4()
    object_name = f"{company_id}/{document_id}/{file.filename}"

    try:
        user_uuid = uuid.UUID(user_id) # Validate user_id format early
        async with get_db_conn() as conn:
            # LLM_FLAG: CRITICAL_DB_WRITE - Create document record
            await api_db_retry_strategy(db_client.create_document_record)(
                conn=conn,
                doc_id=document_id,
                company_id=company_uuid,
                user_id=user_uuid,
                filename=file.filename,
                file_type=file.content_type,
                minio_object_name=object_name,
                status=DocumentStatus.PENDING, # Start as PENDING before upload
                metadata=metadata # Pass parsed metadata
            )
        endpoint_log.info("Document record created in PostgreSQL", document_id=str(document_id))
    except ValueError:
        endpoint_log.error("Invalid User ID format provided", user_id_received=user_id)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid User ID format.")
    except Exception as e:
        endpoint_log.exception("Failed to create document record in PostgreSQL", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error creating record.")

    # 5. Upload to MinIO
    try:
        file_content = await file.read()
        await file.seek(0)
        # LLM_FLAG: CRITICAL_STORAGE_WRITE - Upload to MinIO
        await minio_client.upload_file_async(
            object_name=object_name,
            data=file_content,
            content_type=file.content_type
        )
        endpoint_log.info("File uploaded successfully to MinIO", object_name=object_name)

        # 6. Update DB status to 'uploaded'
        async with get_db_conn() as conn:
             # LLM_FLAG: CRITICAL_DB_WRITE - Update status after upload
             await api_db_retry_strategy(db_client.update_document_status)(
                 document_id=document_id,
                 status=DocumentStatus.UPLOADED
                 # Pool is handled by context manager
             )
        endpoint_log.info("Document status updated to 'uploaded'", document_id=str(document_id))

    except MinioError as me:
        endpoint_log.error("Failed to upload file to MinIO", object_name=object_name, error=str(me))
        # Attempt to mark document as error in DB
        try:
            async with get_db_conn() as conn:
                await db_client.update_document_status(
                     document_id, DocumentStatus.ERROR, error_message=f"MinIO upload failed: {me}"
                )
        except Exception as db_err:
            endpoint_log.exception("Failed to update status to ERROR after MinIO failure", error=str(db_err))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Storage service error: {me}")
    except Exception as e:
         endpoint_log.exception("Unexpected error during file upload or DB update", error=str(e))
         # Attempt to mark document as error in DB
         try:
             async with get_db_conn() as conn:
                 await db_client.update_document_status(
                     document_id, DocumentStatus.ERROR, error_message=f"Unexpected upload error: {e}"
                 )
         except Exception as db_err:
             endpoint_log.exception("Failed to update status to ERROR after unexpected upload failure", error=str(db_err))
         raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Internal server error during upload: {e}")
    finally:
         await file.close()

    # 7. Queue Celery Task
    try:
        task_payload = {
            "document_id": str(document_id),
            "company_id": company_id,
            "filename": file.filename,
            "content_type": file.content_type,
            "user_id": user_id,
        }
        # LLM_FLAG: CRITICAL_TASK_QUEUEING - Send task to Celery
        task = process_document_haystack_task.delay(**task_payload)
        endpoint_log.info("Document ingestion task queued successfully", task_id=task.id)
    except Exception as e:
        endpoint_log.exception("Failed to queue Celery task", error=str(e))
        # Attempt to mark document as error
        try:
            async with get_db_conn() as conn:
                await db_client.update_document_status(
                    document_id, DocumentStatus.ERROR, error_message=f"Failed to queue processing task: {e}"
                )
        except Exception as db_err:
             endpoint_log.exception("Failed to update status to ERROR after Celery failure", error=str(db_err))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to queue processing task: {e}")

    return IngestResponse(
        document_id=str(document_id),
        task_id=task.id,
        status=DocumentStatus.UPLOADED.value,
        message="Document upload accepted, processing started."
    )
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Upload Endpoint Logic


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
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Get Status Endpoint Logic
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
             # LLM_FLAG: CRITICAL_DB_READ - Get document by ID
             doc_data = await api_db_retry_strategy(db_client.get_document_by_id)(
                 conn, doc_id=document_id, company_id=company_uuid
             )
        if not doc_data:
            status_log.warning("Document not found in DB")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
        status_log.info("Retrieved base document data from DB", status=doc_data['status'])
        # Store initial values for comparison
        updated_status = DocumentStatus(doc_data['status'])
        updated_chunk_count = doc_data.get('chunk_count') # Can be None
        final_error_message = doc_data.get('error_message')

    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        status_log.exception("Error fetching document status from DB", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error fetching status.")

    minio_path = doc_data.get('minio_object_name')
    if not minio_path:
         status_log.warning("MinIO object name missing in DB record", db_id=doc_data['id'])
         minio_exists = False
    else:
        # 2. Check MinIO Existence (Async)
        status_log.debug("Checking MinIO for file existence", object_name=minio_path)
        minio_exists = await minio_client.check_file_exists_async(minio_path)
        status_log.info("MinIO existence check complete", exists=minio_exists)
        if not minio_exists and updated_status not in [DocumentStatus.ERROR, DocumentStatus.PENDING]:
             status_log.warning("File missing in MinIO but DB status is not ERROR/PENDING", current_db_status=updated_status.value)
             if updated_status != DocumentStatus.ERROR:
                 needs_update = True
                 updated_status = DocumentStatus.ERROR
                 final_error_message = "File missing from storage."

    # 3. Check Milvus Chunk Count (Sync in Executor)
    status_log.debug("Checking Milvus for chunk count...")
    loop = asyncio.get_running_loop()
    milvus_chunk_count = -1 # Default to error state
    try:
        # LLM_FLAG: EXTERNAL_CALL - Milvus count check
        milvus_chunk_count = await loop.run_in_executor(
            None, _get_milvus_chunk_count_sync, str(document_id), company_id
        )
        status_log.info("Milvus chunk count check complete", count=milvus_chunk_count)

        # --- Logic for handling inconsistencies ---
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
            # If status is already processed, update DB chunk count if it differs from live count
            # or if DB count was null/zero initially.
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
                 # LLM_FLAG: CRITICAL_DB_WRITE - Update status based on checks
                 await api_db_retry_strategy(db_client.update_document_status)(
                     document_id=document_id,
                     status=updated_status,
                     chunk_count=updated_chunk_count,
                     error_message=final_error_message
                 )
             status_log.info("Document status updated successfully in DB due to inconsistency check.")
             # Update local data for response
             doc_data['status'] = updated_status.value
             if updated_chunk_count is not None: doc_data['chunk_count'] = updated_chunk_count
             if final_error_message is not None: doc_data['error_message'] = final_error_message
             else: doc_data['error_message'] = None # Clear if resolved
        except Exception as e:
             status_log.exception("Failed to update document status in DB after inconsistency check", error=str(e))

    # 5. Construct and Return Response
    status_log.info("Returning final document status")
    return StatusResponse(
        document_id=str(doc_data['id']),
        company_id=doc_data.get('company_id'), # Include company_id
        status=doc_data['status'],
        file_name=doc_data['file_name'],
        file_type=doc_data['file_type'],
        file_path=doc_data.get('file_path'),
        chunk_count=doc_data.get('chunk_count', 0), # Use DB value (potentially updated)
        minio_exists=minio_exists, # Live check result
        milvus_chunk_count=milvus_chunk_count, # Live check result (or -1 for error)
        last_updated=doc_data['updated_at'],
        uploaded_at=doc_data.get('uploaded_at'),
        error_message=doc_data.get('error_message'), # Use DB value (potentially updated)
        metadata=doc_data.get('metadata')
    )
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Get Status Endpoint Logic


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
    minio_client: MinioClient = Depends(get_minio_client),
):
    """
    Lists documents for the company with pagination.
    Performs live checks for MinIO/Milvus in parallel for listed documents.
    Updates the DB status/chunk_count if inconsistencies are found.
    Returns the potentially updated status information.
    """
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - List Statuses Endpoint Logic
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
             # LLM_FLAG: CRITICAL_DB_READ - List documents paginated
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
        # LLM_FLAG: SENSITIVE_SUB_LOGIC - Parallel check for single document
        check_log = log.bind(request_id=req_id, document_id=str(doc_db_data['id']), company_id=company_id)
        check_log.debug("Starting live checks for document")

        minio_exists_live = False
        milvus_count_live = -1
        doc_needs_update = False
        # Start with current DB values
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
                 minio_exists_live = False
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
            # LLM_FLAG: EXTERNAL_CALL - Milvus count check in executor
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
                 doc_final_error_msg = None
            elif milvus_count_live == 0 and doc_updated_status_val == DocumentStatus.PROCESSED.value:
                 check_log.warning("Inconsistency: DB status is 'processed' but no chunks found in Milvus. Correcting to 'error'.")
                 doc_needs_update = True
                 doc_updated_status_val = DocumentStatus.ERROR.value
                 doc_updated_chunk_count = 0
                 doc_final_error_msg = (doc_final_error_msg or "") + " Processed data missing (Milvus count is 0)."
            elif doc_updated_status_val == DocumentStatus.PROCESSED.value:
                 if doc_updated_chunk_count is None or doc_updated_chunk_count != milvus_count_live:
                      doc_updated_chunk_count = milvus_count_live
                      if doc_db_data.get('chunk_count') != doc_updated_chunk_count:
                           doc_needs_update = True
        except Exception as e:
            check_log.exception("Unexpected error during Milvus count check", error=str(e))
            milvus_count_live = -1
            if doc_updated_status_val != DocumentStatus.ERROR.value:
                doc_needs_update = True
                doc_updated_status_val = DocumentStatus.ERROR.value
                doc_final_error_msg = (doc_final_error_msg or "") + f" Error checking processed data: {e}."

        # Return results including whether an update is needed
        return {
            "db_data": doc_db_data,
            "needs_update": doc_needs_update,
            "updated_status": doc_updated_status_val,
            "updated_chunk_count": doc_updated_chunk_count,
            "final_error_message": doc_final_error_msg,
            "live_minio_exists": minio_exists_live,
            "live_milvus_chunk_count": milvus_count_live,
        }
        # LLM_FLAG: SENSITIVE_SUB_LOGIC_END - Parallel check

    # Run checks concurrently
    check_tasks = [check_single_document(doc) for doc in documents_db]
    check_results = await asyncio.gather(*check_tasks)

    # Update DB for inconsistent documents
    updated_doc_data_map = {}
    docs_to_update_in_db = []
    for result in check_results:
        doc_id_str = str(result["db_data"]["id"])
        if result["needs_update"]:
            docs_to_update_in_db.append({
                "id": result["db_data"]["id"],
                "status": DocumentStatus(result["updated_status"]),
                "chunk_count": result["updated_chunk_count"],
                "error_message": result["final_error_message"],
            })
        # Store potentially updated data for response construction
        updated_doc_data_map[doc_id_str] = {
             **result["db_data"],
             "status": result["updated_status"],
             "chunk_count": result["updated_chunk_count"],
             "error_message": result["final_error_message"],
        }

    if docs_to_update_in_db:
        list_log.warning("Updating statuses in DB for inconsistent documents", count=len(docs_to_update_in_db))
        try:
             async with get_db_conn() as conn:
                 for update_info in docs_to_update_in_db:
                     try:
                         # LLM_FLAG: CRITICAL_DB_WRITE - Update status based on checks (List)
                         await api_db_retry_strategy(db_client.update_document_status)(
                             document_id=update_info["id"],
                             status=update_info["status"],
                             chunk_count=update_info["chunk_count"],
                             error_message=update_info["error_message"]
                         )
                         list_log.info("Successfully updated DB status", document_id=str(update_info["id"]), new_status=update_info["status"].value)
                     except Exception as single_update_err:
                         list_log.error("Failed DB update for single document during list check",
                                        document_id=str(update_info["id"]), error=str(single_update_err))
        except Exception as bulk_update_err:
            list_log.exception("Error during bulk DB status update process", error=str(bulk_update_err))

    # Construct final response using potentially updated data
    final_items = []
    for result in check_results:
         doc_id_str = str(result["db_data"]["id"])
         current_data = updated_doc_data_map.get(doc_id_str, result["db_data"])
         final_items.append(StatusResponse(
            document_id=doc_id_str,
            company_id=current_data.get('company_id'),
            status=current_data['status'],
            file_name=current_data['file_name'],
            file_type=current_data['file_type'],
            file_path=current_data.get('file_path'),
            chunk_count=current_data.get('chunk_count', 0),
            minio_exists=result["live_minio_exists"],
            milvus_chunk_count=result["live_milvus_chunk_count"],
            last_updated=current_data['updated_at'],
            uploaded_at=current_data.get('uploaded_at'),
            error_message=current_data.get('error_message'),
            metadata=current_data.get('metadata')
         ))

    list_log.info("Returning enriched statuses", count=len(final_items))
    return PaginatedStatusResponse(items=final_items, total=total_count, limit=limit, offset=offset)
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - List Statuses Endpoint Logic


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
    """
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Retry Endpoint Logic
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
            # LLM_FLAG: CRITICAL_DB_READ - Get document for retry
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
            # LLM_FLAG: CRITICAL_DB_WRITE - Update status for retry
            await api_db_retry_strategy(db_client.update_document_status)(
                 document_id, DocumentStatus.PROCESSING, chunk_count=None, error_message=None
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
        # LLM_FLAG: CRITICAL_TASK_QUEUEING - Re-queue task for retry
        task = process_document_haystack_task.delay(**task_payload)
        retry_log.info("Document reprocessing task queued successfully", task_id=task.id)
    except Exception as e:
        retry_log.exception("Failed to re-queue Celery task for retry", error=str(e))
        # Attempt to revert status back to 'error'? Could lead to inconsistent state if task was already picked up.
        # Best to leave in 'processing' and rely on monitoring/manual intervention if queueing fails persistently.
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to queue reprocessing task: {e}")

    return IngestResponse(
        document_id=str(document_id),
        task_id=task.id,
        status=DocumentStatus.PROCESSING.value,
        message="Document retry accepted, processing started."
    )
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Retry Endpoint Logic


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
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Delete Endpoint Logic
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
             # LLM_FLAG: CRITICAL_DB_READ - Verify document before delete
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

    errors = [] # Collect non-critical errors

    # 2. Delete from Milvus (Sync in Executor)
    delete_log.info("Attempting to delete chunks from Milvus...")
    loop = asyncio.get_running_loop()
    try:
        # LLM_FLAG: EXTERNAL_CALL - Milvus delete
        milvus_deleted = await loop.run_in_executor(
            None, _delete_milvus_sync, str(document_id), company_id
        )
        if milvus_deleted:
            delete_log.info("Milvus delete command executed successfully.")
        else:
            errors.append("Failed to execute delete operation in Milvus.")
            delete_log.warning("Milvus delete operation failed.")
    except Exception as e:
        delete_log.exception("Unexpected error during Milvus delete", error=str(e))
        errors.append(f"Unexpected error during Milvus delete: {e}")


    # 3. Delete from MinIO (Async)
    minio_path = doc_data.get('minio_object_name')
    if minio_path:
        delete_log.info("Attempting to delete file from MinIO...", object_name=minio_path)
        try:
            # LLM_FLAG: CRITICAL_STORAGE_DELETE - Delete from MinIO
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
            # LLM_FLAG: CRITICAL_DB_DELETE - Delete document record
            deleted_id = await api_db_retry_strategy(db_client.delete_document)(
                conn, doc_id=document_id, company_id=company_uuid
            )
            if deleted_id:
                 delete_log.info("Document record deleted successfully from PostgreSQL")
            else:
                 delete_log.warning("PostgreSQL delete command executed but no record was deleted (already gone?).")
    except Exception as e:
        delete_log.exception("CRITICAL: Failed to delete document record from PostgreSQL", error=str(e))
        error_detail = f"Deleted from storage/vectors (errors: {', '.join(errors)}) but FAILED to delete DB record: {e}"
        # LLM_FLAG: CRITICAL_FAILURE - Raise 500 if DB delete fails
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=error_detail)

    # 5. Log warnings if non-critical errors occurred
    if errors:
        delete_log.warning("Document deletion completed with non-critical errors (Milvus/MinIO)", errors=errors)

    delete_log.info("Document deletion process finished.")
    # Return 204 No Content implicitly
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Delete Endpoint Logic
```

## File: `app\api\v1\schemas.py`
```py
# ingest-service/app/api/v1/schemas.py
import uuid
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Dict, Any, List
from app.models.domain import DocumentStatus # Importar DocumentStatus si se usa directamente
from datetime import datetime
import json
import logging # Importar logging

# LLM_COMMENT: Asegurarse de que Pydantic maneje correctamente los tipos UUID y datetime.
# LLM_COMMENT: Definir modelos claros para las respuestas de la API.

log = logging.getLogger(__name__) # Usar logging estándar si structlog no está configurado aquí

class ErrorDetail(BaseModel):
    """Schema for error details in responses."""
    detail: str

class IngestResponse(BaseModel):
    document_id: uuid.UUID
    task_id: str
    status: str # Idealmente usar DocumentStatus, pero str es más simple para respuesta directa
    message: str = "Document upload received and queued for processing."

    class Config:
        json_schema_extra = {
            "example": {
                "document_id": "123e4567-e89b-12d3-a456-426614174000",
                "task_id": "c79ba436-fe88-4b82-9afc-44b1091564e4", # Example task ID
                "status": DocumentStatus.UPLOADED.value,
                "message": "Document upload accepted, processing started."
            }
        }

class StatusResponse(BaseModel):
    # LLM_COMMENT: Usar alias 'id' para document_id si es conveniente para el frontend.
    document_id: uuid.UUID = Field(..., alias="id")
    # LLM_COMMENT: Incluir todos los campos relevantes del documento para la UI.
    company_id: Optional[uuid.UUID] = None # Hacer opcional si no siempre está presente
    file_name: str
    file_type: str
    file_path: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    status: str # Podría ser DocumentStatus, pero str es más flexible
    chunk_count: Optional[int] = 0
    error_message: Optional[str] = None
    uploaded_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # LLM_COMMENT: Campos dinámicos añadidos por los endpoints de status.
    minio_exists: Optional[bool] = None
    milvus_chunk_count: Optional[int] = None # Usar Optional[int] en lugar de valor centinela -1
    message: Optional[str] = None

    @field_validator('metadata', mode='before')
    @classmethod
    def metadata_to_dict(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                # LLM_COMMENT: Loggear error si metadata en DB no es JSON válido.
                log.warning("Invalid metadata JSON found in DB record", raw_metadata=v)
                return {"error": "invalid metadata JSON in DB"}
        # LLM_COMMENT: Permitir que metadata sea None o ya un diccionario.
        return v if v is None or isinstance(v, dict) else {}

    class Config:
        validate_assignment = True
        populate_by_name = True # Permite usar 'id' o 'document_id'
        json_schema_extra = {
             "example": {
                "id": "52ad2ba8-cab9-4108-a504-b9822fe99bdc",
                "company_id": "51a66c8f-f6b1-43bd-8038-8768471a8b09",
                "file_name": "Anexo-00-Modificaciones-de-la-Guia-5.1.0.pdf",
                "file_type": "application/pdf",
                "file_path": "51a66c8f-f6b1-43bd-8038-8768471a8b09/52ad2ba8-cab9-4108-a504-b9822fe99bdc/Anexo-00-Modificaciones-de-la-Guia-5.1.0.pdf",
                "metadata": {"source": "manual upload", "version": "1.1"},
                "status": DocumentStatus.ERROR.value, # Ejemplo de estado de error
                "chunk_count": 0, # DB value (puede ser 0 si hubo error antes de procesar)
                "error_message": "Processing timed out after 600 seconds.", # Ejemplo de mensaje de error
                "uploaded_at": "2025-04-19T19:42:38.671016Z",
                "updated_at": "2025-04-19T19:42:42.337854Z",
                "minio_exists": True, # Resultado de la verificación en vivo
                "milvus_chunk_count": 0, # Resultado de la verificación en vivo (0 si no hay chunks)
                "message": "El procesamiento falló: Timeout." # Mensaje descriptivo añadido por el endpoint
            }
        }

# --- Definición Faltante ---
class PaginatedStatusResponse(BaseModel):
    """Schema for paginated list of document statuses."""
    items: List[StatusResponse] = Field(..., description="List of document status objects on the current page.")
    total: int = Field(..., description="Total number of documents matching the query.")
    limit: int = Field(..., description="Number of items requested per page.")
    offset: int = Field(..., description="Number of items skipped for pagination.")

    class Config:
        json_schema_extra = {
            "example": {
                "items": [
                    # Aquí iría un ejemplo de StatusResponse (como el de arriba)
                    {
                        "id": "52ad2ba8-cab9-4108-a504-b9822fe99bdc",
                        "company_id": "51a66c8f-f6b1-43bd-8038-8768471a8b09",
                        "file_name": "Anexo-00-Modificaciones-de-la-Guia-5.1.0.pdf",
                        "file_type": "application/pdf",
                        "file_path": "51a66c8f-f6b1-43bd-8038-8768471a8b09/52ad2ba8-cab9-4108-a504-b9822fe99bdc/Anexo-00-Modificaciones-de-la-Guia-5.1.0.pdf",
                        "metadata": {"source": "manual upload", "version": "1.1"},
                        "status": DocumentStatus.ERROR.value,
                        "chunk_count": 0,
                        "error_message": "Processing timed out after 600 seconds.",
                        "uploaded_at": "2025-04-19T19:42:38.671016Z",
                        "updated_at": "2025-04-19T19:42:42.337854Z",
                        "minio_exists": True,
                        "milvus_chunk_count": 0,
                        "message": "El procesamiento falló: Timeout."
                    }
                    # ... (podrían ir más items)
                ],
                "total": 1,
                "limit": 30,
                "offset": 0
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
from typing import IO, BinaryIO, Optional # Añadir Optional
from minio import Minio
from minio.error import S3Error # Importar directamente S3Error
import structlog
import asyncio

# LLM_COMMENT: Importar settings directamente para configuración.
from app.core.config import settings

log = structlog.get_logger(__name__)

# Definir excepción personalizada para errores de MinIO
class MinioError(Exception):
    """Custom exception for MinIO related errors."""
    def __init__(self, message: str, original_exception: Optional[Exception] = None):
        self.message = message
        self.original_exception = original_exception
        super().__init__(message)

    def __str__(self):
        if self.original_exception:
            return f"{self.message}: {type(self.original_exception).__name__} - {str(self.original_exception)}"
        return self.message


# --- RENOMBRAR CLASE ---
class MinioClient:
    """Client to interact with MinIO using configured settings."""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        bucket_name: Optional[str] = None,
        secure: Optional[bool] = None
    ):
        # LLM_COMMENT: Usar valores de settings como default, permitir override si se pasan argumentos.
        self._endpoint = endpoint or settings.MINIO_ENDPOINT
        self._access_key = access_key or settings.MINIO_ACCESS_KEY.get_secret_value()
        self._secret_key = secret_key or settings.MINIO_SECRET_KEY.get_secret_value()
        self.bucket_name = bucket_name or settings.MINIO_BUCKET_NAME
        self._secure = secure if secure is not None else settings.MINIO_USE_SECURE

        self._client: Optional[Minio] = None
        self.log = log.bind(minio_endpoint=self._endpoint, bucket_name=self.bucket_name)

        # LLM_COMMENT: Inicializar el cliente interno de MinIO.
        self._initialize_client()

    def _initialize_client(self):
        """Initializes the internal MinIO client and ensures bucket exists."""
        self.log.debug("Initializing MinIO client...")
        try:
            self._client = Minio(
                self._endpoint,
                access_key=self._access_key,
                secret_key=self._secret_key,
                secure=self._secure
            )
            self._ensure_bucket_exists_sync() # Llamada síncrona en init
            self.log.info("MinIO client initialized successfully.")
        except (S3Error, TypeError, ValueError) as e:
            self.log.critical("CRITICAL: Failed to initialize MinIO client", error=str(e), exc_info=True)
            # LLM_COMMENT: Lanzar RuntimeError si falla la inicialización crítica.
            raise RuntimeError(f"MinIO client initialization failed: {e}") from e

    def _get_client(self) -> Minio:
        """Returns the initialized MinIO client, raising error if not available."""
        if self._client is None:
            # LLM_COMMENT: Error si se intenta usar un cliente no inicializado.
            self.log.error("Minio client accessed before initialization.")
            raise RuntimeError("Minio client is not initialized.")
        return self._client

    def _ensure_bucket_exists_sync(self):
        """Synchronously creates the bucket if it doesn't exist."""
        client = self._get_client()
        try:
            found = client.bucket_exists(self.bucket_name)
            if not found:
                client.make_bucket(self.bucket_name)
                self.log.info(f"MinIO bucket '{self.bucket_name}' created.")
            else:
                self.log.debug(f"MinIO bucket '{self.bucket_name}' already exists.")
        except S3Error as e:
            self.log.error(f"S3Error checking/creating MinIO bucket", bucket=self.bucket_name, error_code=getattr(e, 'code', 'Unknown'), error_details=str(e))
            raise MinioError(f"Error checking/creating bucket '{self.bucket_name}'", e) from e
        except Exception as e:
            self.log.error(f"Unexpected error checking/creating MinIO bucket", bucket=self.bucket_name, error=str(e), exc_info=True)
            raise MinioError(f"Unexpected error with bucket '{self.bucket_name}'", e) from e

    async def upload_file_async(
        self,
        object_name: str,
        data: bytes, # Cambiado a bytes para simplificar
        content_type: str
    ) -> str:
        """Uploads file content to MinIO asynchronously using run_in_executor."""
        # LLM_COMMENT: Usar run_in_executor para operaciones síncronas de MinIO.
        upload_log = self.log.bind(bucket=self.bucket_name, object_name=object_name, content_type=content_type, length=len(data))
        upload_log.info("Queueing file upload to MinIO executor")

        loop = asyncio.get_running_loop()
        client = self._get_client() # Obtener cliente inicializado

        def _upload_sync():
            # LLM_COMMENT: La operación síncrona real dentro del executor.
            data_stream = io.BytesIO(data)
            try:
                 result = client.put_object(
                    bucket_name=self.bucket_name,
                    object_name=object_name,
                    data=data_stream,
                    length=len(data),
                    content_type=content_type
                )
                 # LLM_COMMENT: Loggear etag si la subida es exitosa.
                 upload_log.debug("MinIO put_object successful", etag=getattr(result, 'etag', 'N/A'), version_id=getattr(result, 'version_id', 'N/A'))
                 return object_name
            except S3Error as e:
                 upload_log.error("S3Error during MinIO upload (sync part)", error_code=getattr(e, 'code', 'Unknown'), error_details=str(e))
                 raise MinioError(f"S3 error uploading {object_name}", e) from e
            except Exception as e:
                 upload_log.exception("Unexpected error during MinIO upload (sync part)", error=str(e))
                 raise MinioError(f"Unexpected error uploading {object_name}", e) from e

        try:
            # LLM_COMMENT: Ejecutar la función síncrona en el threadpool por defecto de asyncio.
            uploaded_object_name = await loop.run_in_executor(None, _upload_sync)
            upload_log.info("File uploaded successfully to MinIO via executor")
            return uploaded_object_name
        except MinioError as me: # Capturar nuestra excepción personalizada
            upload_log.error("Upload failed via executor", error=str(me))
            raise me # Relanzar para que el llamador la maneje
        except Exception as e: # Capturar otros errores inesperados del executor
             upload_log.exception("Unexpected executor error during upload", error=str(e))
             raise MinioError("Unexpected executor error during upload", e) from e

    def download_file_sync(self, object_name: str, file_path: str):
        """Synchronously downloads a file from MinIO to a local path."""
        # LLM_COMMENT: Operación síncrona para descargar a un archivo local.
        download_log = self.log.bind(bucket=self.bucket_name, object_name=object_name, target_path=file_path)
        download_log.info("Downloading file from MinIO (sync operation)...")
        client = self._get_client()
        try:
            client.fget_object(self.bucket_name, object_name, file_path)
            download_log.info(f"File downloaded successfully from MinIO to {file_path} (sync)")
        except S3Error as e:
            download_log.error("S3Error downloading file (sync)", error_code=getattr(e, 'code', 'Unknown'), error_details=str(e))
            if getattr(e, 'code', None) == 'NoSuchKey':
                raise MinioError(f"Object not found in MinIO: {object_name}", e) from e
            else:
                raise MinioError(f"S3 error downloading {object_name}: {e.code}", e) from e
        except Exception as e:
            download_log.exception("Unexpected error during sync file download", error=str(e))
            raise MinioError(f"Unexpected error downloading {object_name}", e) from e

    async def download_file(self, object_name: str, file_path: str):
        """Downloads a file from MinIO to a local path asynchronously."""
        # LLM_COMMENT: Envolver la descarga síncrona en run_in_executor.
        download_log = self.log.bind(bucket=self.bucket_name, object_name=object_name, target_path=file_path)
        download_log.info("Queueing file download from MinIO executor")
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self.download_file_sync, object_name, file_path)
            download_log.info("File download successful via executor")
        except MinioError as me:
            download_log.error("Download failed via executor", error=str(me))
            raise me
        except Exception as e:
            download_log.exception("Unexpected executor error during download", error=str(e))
            raise MinioError("Unexpected executor error during download", e) from e

    def check_file_exists_sync(self, object_name: str) -> bool:
        """Synchronously checks if a file exists in MinIO."""
        # LLM_COMMENT: Operación síncrona para verificar existencia (usa stat_object).
        check_log = self.log.bind(bucket=self.bucket_name, object_name=object_name)
        client = self._get_client()
        try:
            client.stat_object(self.bucket_name, object_name)
            check_log.debug("Object exists in MinIO (sync check).")
            return True
        except S3Error as e:
            # LLM_COMMENT: Manejar específicamente NoSuchKey como 'no existe'.
            if getattr(e, 'code', None) in ('NoSuchKey', 'NoSuchBucket'):
                check_log.debug("Object not found in MinIO (sync check)", code=e.code)
                return False
            # LLM_COMMENT: Loggear otros errores S3 pero considerarlos como fallo de verificación.
            check_log.error("S3Error checking object existence (sync)", error_code=getattr(e, 'code', 'Unknown'), error_details=str(e))
            raise MinioError(f"S3 error checking existence for {object_name}", e) from e
        except Exception as e:
             check_log.exception("Unexpected error checking object existence (sync)", error=str(e))
             raise MinioError(f"Unexpected error checking existence for {object_name}", e) from e

    async def check_file_exists_async(self, object_name: str) -> bool:
        """Checks if a file exists in MinIO asynchronously."""
        # LLM_COMMENT: Envolver la verificación síncrona en run_in_executor.
        check_log = self.log.bind(bucket=self.bucket_name, object_name=object_name)
        check_log.debug("Queueing file existence check in executor")
        loop = asyncio.get_running_loop()
        try:
            exists = await loop.run_in_executor(None, self.check_file_exists_sync, object_name)
            check_log.debug("File existence check completed via executor", exists=exists)
            return exists
        except MinioError as me:
            # LLM_COMMENT: Loggear error pero devolver False si fue NoSuchKey, relanzar otros.
            if "Object not found" in str(me): # Chequeo simple del mensaje de error interno
                 return False
            check_log.error("Existence check failed via executor", error=str(me))
            # Podrías querer relanzar aquí si un error diferente a Not Found es crítico
            # raise me
            return False # O tratar otros errores como 'no se pudo verificar -> no existe'
        except Exception as e:
            check_log.exception("Unexpected executor error during existence check", error=str(e))
            # Tratar error inesperado como 'no se pudo verificar -> no existe'
            return False

    def delete_file_sync(self, object_name: str):
        """Synchronously deletes a file from MinIO."""
        # LLM_COMMENT: Operación síncrona para eliminar objeto.
        delete_log = self.log.bind(bucket=self.bucket_name, object_name=object_name)
        delete_log.info("Deleting file from MinIO (sync operation)...")
        client = self._get_client()
        try:
            client.remove_object(self.bucket_name, object_name)
            delete_log.info("File deleted successfully from MinIO (sync)")
        except S3Error as e:
            # LLM_COMMENT: Loggear error pero no relanzar para permitir eliminación parcial.
            delete_log.error("S3Error deleting file (sync)", error_code=getattr(e, 'code', 'Unknown'), error_details=str(e))
            # No relanzar aquí para que el proceso de borrado principal continúe si es posible
            # raise MinioError(f"S3 error deleting {object_name}", e) from e
        except Exception as e:
            delete_log.exception("Unexpected error during sync file deletion", error=str(e))
            # No relanzar aquí tampoco
            # raise MinioError(f"Unexpected error deleting {object_name}", e) from e

    async def delete_file_async(self, object_name: str) -> None:
        """Deletes a file from MinIO asynchronously."""
        # LLM_COMMENT: Envolver la eliminación síncrona en run_in_executor.
        delete_log = self.log.bind(bucket=self.bucket_name, object_name=object_name)
        delete_log.info("Queueing file deletion from MinIO executor")
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self.delete_file_sync, object_name)
            delete_log.info("File deletion successful via executor")
        except Exception as e:
            # LLM_COMMENT: Loggear errores del executor pero no impedir que el flujo continúe.
            delete_log.exception("Unexpected executor error during deletion", error=str(e))
            # No relanzar para no bloquear el resto del proceso de borrado
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
# LLM_FLAG: IMPORT_FIX - Ensure correct class name is imported
from app.services.minio_client import MinioClient, MinioError # Corrected class name

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
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Milvus Initialization
    log.debug("Initializing MilvusDocumentStore...")
    try:
        store = MilvusDocumentStore(
            connection_args={"uri": settings.MILVUS_URI},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            consistency_level="Strong", # Example setting, adjust if needed
        )
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
def _initialize_haystack_components(
    document_store: MilvusDocumentStore
) -> Tuple[DocumentSplitter, OpenAIDocumentEmbedder, DocumentWriter]:
    """Synchronously initializes necessary Haystack processing components."""
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Haystack Component Init
    log.debug("Initializing Haystack components (Splitter, Embedder, Writer)...")
    try:
        # Document Splitter
        splitter = DocumentSplitter(
            split_by="word",
            split_length=settings.SPLITTER_CHUNK_SIZE,
            split_overlap=settings.SPLITTER_CHUNK_OVERLAP
        )

        # Document Embedder (OpenAI)
        embedder = OpenAIDocumentEmbedder(
            api_key=settings.OPENAI_API_KEY,
            model=settings.OPENAI_EMBEDDING_MODEL,
        )

        # Document Writer (using the initialized Milvus store)
        writer = DocumentWriter(
            document_store=document_store,
            policy=DuplicatePolicy.OVERWRITE
        )
        log.info("Haystack components initialized successfully.")
        return splitter, embedder, writer
    except Exception as e:
        log.exception("Failed to initialize Haystack components", error=str(e), exc_info=True)
        raise RuntimeError(f"Haystack Component Initialization Error: {e}") from e
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Haystack Component Init

# --- File Type to Converter Mapping ---
def get_converter(content_type: str) -> Type:
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
# LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Celery App Config
celery_app = Celery(
    'ingest_tasks',
    broker=str(settings.CELERY_BROKER_URL),
    backend=str(settings.CELERY_RESULT_BACKEND)
)

celery_app.conf.update(
    task_serializer='json',
    result_serializer='json',
    accept_content=['json'],
    task_track_started=True,
    task_time_limit=TIMEOUT_SECONDS + 60,
    task_soft_time_limit=TIMEOUT_SECONDS,
    # Consider task_acks_late=True for more robustness if needed
)
# LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Celery App Config

# --- Logging Configuration within Worker Context ---
# LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Logging Setup
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

# Corrected NameError location: Ensure logging is imported before use
handler = logging.StreamHandler() # Now 'logging' is defined due to import at top
handler.setFormatter(formatter)
root_logger = logging.getLogger()
if not root_logger.handlers:
    root_logger.addHandler(handler)
    try:
        root_logger.setLevel(settings.LOG_LEVEL.upper())
    except ValueError:
        root_logger.setLevel("INFO")
        log.warning("Invalid LOG_LEVEL in settings, defaulting to INFO.")

# LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Logging Setup


# Define retry strategy for database operations
db_retry_strategy = retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(2),
    retry=retry_if_exception_type((asyncpg.exceptions.PostgresConnectionError, TimeoutError, OSError)),
    before_sleep=before_sleep_log(log, logging.WARNING) # logging.WARNING is correct here
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
    # LLM_FLAG: SENSITIVE_DEPENDENCY - MinioClient instance
    minio_client = MinioClient(
        # Credentials and config are taken from settings inside MinioClient
    )

    # 2. Download file from Minio
    object_name = f"{company_id}/{document_id}/{filename}"
    temp_file_path = None
    try:
        flow_log.info("Downloading file from MinIO", object_name=object_name)
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(filename)[1]) as temp_file:
            temp_file_path = temp_file.name
            # Use the correct method name from MinioClient
            await minio_client.download_file(object_name, temp_file_path)
        flow_log.info("File downloaded successfully", temp_path=temp_file_path)
    except MinioError as me:
        flow_log.error("Failed to download file from MinIO", object_name=object_name, error=str(me))
        raise RuntimeError(f"MinIO download failed: {me}") from me
    except Exception as e:
        flow_log.exception("Unexpected error during file download", error=str(e))
        raise RuntimeError(f"Unexpected download error: {e}") from e

    # 3. Initialize Milvus Store (potentially blocking)
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

    # 4. Initialize other Haystack Components (potentially blocking)
    try:
        flow_log.info("Initializing Haystack processing components...")
        splitter, embedder, writer = await loop.run_in_executor(None, _initialize_haystack_components, store)
        flow_log.info("Haystack processing components initialized.")
    except RuntimeError as e:
        flow_log.error("Failed to initialize Haystack components during flow", error=str(e))
        raise e
    except Exception as e:
        flow_log.exception("Unexpected error initializing Haystack components", error=str(e))
        raise RuntimeError(f"Unexpected Haystack init error: {e}") from e

    # 5. Initialize Converter (potentially blocking, select based on type)
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

        def run_haystack_pipeline_sync():
            nonlocal total_chunks_written
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
                return 0

            # Add essential metadata
            for doc in docs:
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

        chunks_written = await loop.run_in_executor(None, run_haystack_pipeline_sync)
        flow_log.info("Haystack pipeline execution finished.", chunks_written=chunks_written)

    except Exception as e:
        flow_log.exception("Error during Haystack pipeline execution", error=str(e))
        raise RuntimeError(f"Haystack Pipeline Error: {e}") from e
    finally:
        # 6. Clean up temporary file
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
                flow_log.debug("Temporary file deleted", path=temp_file_path)
            except OSError as e:
                flow_log.warning("Failed to delete temporary file", path=temp_file_path, error=str(e))

    return total_chunks_written
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Main Async Flow


# --- Celery Task Definition ---
class ProcessDocumentTask(Task):
    """Custom Celery Task class for document processing."""
    name = "tasks.process_document_haystack"
    max_retries = 3
    default_retry_delay = 60

    def __init__(self):
        super().__init__()
        self.task_log = log.bind(task_name=self.name)
        self.task_log.info("ProcessDocumentTask initialized.")

    async def _update_status_with_retry(
        self, pool: asyncpg.Pool, doc_id: str, status: DocumentStatus,
        chunk_count: Optional[int] = None, error_msg: Optional[str] = None
    ):
        """Helper to update document status with retry."""
        # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - DB Update Helper
        update_log = self.task_log.bind(document_id=doc_id, target_status=status.value)
        try:
            # Use the correct DB client function signature
            await db_retry_strategy(db_client.update_document_status)(
                document_id=uuid.UUID(doc_id), # Pass UUID directly
                status=status, # Pass enum member
                # Pass optional args explicitly, pool is handled by context manager now
                chunk_count=chunk_count,
                error_message=error_msg
                # file_path is not updated here usually
            )
            update_log.info("Document status updated successfully in DB.")
        except Exception as e:
            update_log.critical("CRITICAL: Failed final document status update in DB!",
                                error=str(e), chunk_count=chunk_count, error_msg=error_msg,
                                exc_info=True)
            raise Reject(f"Persistent DB error updating status for {doc_id} to {status.value}", requeue=False) from e
        # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - DB Update Helper

    async def run_async_processing(self, *args, **kwargs):
        """Runs the main async processing flow and handles final status updates."""
        # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Task Async Runner
        doc_id = kwargs['document_id']
        task_id = self.request.id
        attempt = self.request.retries + 1
        task_log = self.task_log.bind(document_id=doc_id, task_id=task_id, attempt=attempt)
        final_status = DocumentStatus.ERROR
        final_chunk_count = None
        error_to_report = "Unknown processing error"
        is_retryable_error = False
        e_non_retry: Optional[Exception] = None

        async with db_session_manager() as pool: # Manage DB pool connection
             # Ensure pool is valid before proceeding (get_db_pool handles this)
             if not pool:
                 task_log.critical("Failed to get DB pool for task execution.")
                 raise Reject("DB pool unavailable for task", requeue=False)

             try:
                # 1. Set status to 'processing'
                task_log.info("Setting document status to 'processing'")
                # Pass pool correctly if needed by the update function structure
                await self._update_status_with_retry(pool, doc_id, DocumentStatus.PROCESSING, error_msg=None)

                # 2. Execute main flow
                task_log.info("Executing main async_process_flow with timeout", timeout=TIMEOUT_SECONDS)
                final_chunk_count = await asyncio.wait_for(
                    async_process_flow(task_id=task_id, attempt=attempt, **kwargs),
                    timeout=TIMEOUT_SECONDS
                )
                final_status = DocumentStatus.PROCESSED
                error_to_report = None
                task_log.info("Async process flow completed successfully.", chunks_processed=final_chunk_count)

             except asyncio.TimeoutError:
                 task_log.error("Processing timed out", timeout=TIMEOUT_SECONDS)
                 error_to_report = f"Processing timed out after {TIMEOUT_SECONDS} seconds."
                 final_status = DocumentStatus.ERROR
                 e_non_retry = TimeoutError(error_to_report)
             except ValueError as ve:
                  task_log.error("Processing failed due to value error", error=str(ve))
                  error_to_report = f"Unsupported file type or invalid input: {ve}"
                  final_status = DocumentStatus.ERROR
                  is_retryable_error = False
                  e_non_retry = ve
             except RuntimeError as rte:
                  task_log.error(f"Processing failed permanently: {rte}", exc_info=False)
                  # Provide user-friendly errors based on runtime error type
                  if "Milvus" in str(rte): error_to_report = "Error config./código Milvus. Contacte soporte."
                  elif "Haystack" in str(rte): error_to_report = "Error interno Haystack. Contacte soporte."
                  elif "Converter" in str(rte): error_to_report = "Error interno Conversor. Contacte soporte."
                  elif "MinIO download failed" in str(rte): error_to_report = "Error descargando archivo (MinIO)."
                  else: error_to_report = f"Error interno ({type(rte).__name__}). Contacte soporte."
                  final_status = DocumentStatus.ERROR
                  is_retryable_error = False
                  e_non_retry = rte
             except Exception as e:
                 task_log.exception("Unexpected exception during processing flow.", error=str(e))
                 error_to_report = f"Error inesperado ({type(e).__name__}). Contacte soporte."
                 final_status = DocumentStatus.ERROR
                 e_non_retry = e

             # 3. Update final status in DB
             task_log.info("Attempting to update final document status in DB", status=final_status.value, chunks=final_chunk_count, error=error_to_report)
             try:
                 await self._update_status_with_retry(
                     pool, doc_id, final_status,
                     chunk_count=final_chunk_count if final_status == DocumentStatus.PROCESSED else None,
                     error_msg=error_to_report
                 )
             except Reject as r:
                 task_log.critical("CRITICAL: Failed to update final document status in DB!", target_status=final_status.value, error_msg=error_to_report)
                 raise r # Propagate Reject
             except Exception as db_update_exc:
                  task_log.critical("CRITICAL: Unhandled exception during final DB status update!", error=str(db_update_exc), target_status=final_status.value, exc_info=True)
                  raise Reject(f"Unhandled DB error updating status for {doc_id}", requeue=False) from db_update_exc

             # 4. Handle retries or final failure
             if final_status == DocumentStatus.ERROR:
                 if is_retryable_error:
                     task_log.warning("Processing failed with a retryable error, attempting task retry.", error=error_to_report)
                     try:
                         self.retry(exc=e_non_retry or RuntimeError(error_to_report), countdown=60 * attempt)
                     except MaxRetriesExceededError:
                         task_log.error("Max retries exceeded for task.", error=error_to_report)
                         raise Ignore()
                     except Reject as r:
                          task_log.error("Task rejected during retry attempt.", reason=str(r))
                          raise r
                 else:
                     task_log.error("Processing failed with non-retryable error.", error=error_to_report, exception_type=type(e_non_retry).__name__)
                     if e_non_retry: raise e_non_retry
                     else: raise RuntimeError(error_to_report or "Unknown non-retryable processing error")
             elif final_status == DocumentStatus.PROCESSED:
                  task_log.info("Processing completed successfully for document.")
                  return {"status": "processed", "document_id": doc_id, "chunks_processed": final_chunk_count}
         # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Task Async Runner

    def run(self, *args, **kwargs):
        """Synchronous wrapper to run the async processing logic."""
        # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Celery Sync Runner
        self.task_log.info("Task received", args=args, kwargs=list(kwargs.keys()))
        try:
            return asyncio.run(self.run_async_processing(*args, **kwargs))
        except Reject as r:
             self.task_log.error(f"Task rejected due to persistent DB error: {r.reason}", exc_info=False)
             raise r
        except Ignore:
             self.task_log.warning("Task is being ignored (e.g., max retries exceeded or non-retryable error).")
             raise Ignore()
        except Exception as e:
             self.task_log.exception("Task failed with unhandled exception in run wrapper", error=str(e))
             raise e # Propagate exception to mark task as FAILED
        # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Celery Sync Runner

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Log task failure."""
        # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Celery Failure Handler
        log.error(
            "Celery task failed", task_id=task_id, task_name=self.name,
            args=args, kwargs=kwargs, error_type=type(exc).__name__, error=str(exc),
            traceback=str(einfo.traceback), exc_info=False
        )
        # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Celery Failure Handler

    def on_success(self, retval, task_id, args, kwargs):
        """Log task success."""
        # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Celery Success Handler
        log.info(
            "Celery task completed successfully", task_id=task_id, task_name=self.name,
            args=args, kwargs=kwargs, retval=retval
        )
        # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Celery Success Handler

# Register the custom task class with Celery
process_document_haystack_task = celery_app.register_task(ProcessDocumentTask())
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
# LLM_FLAG: SENSITIVE_BUILD_CONFIG - Build system configuration
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
