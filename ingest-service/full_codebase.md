# Estructura de la Codebase Ingest Service

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
import logging # Required for before_sleep_log

from fastapi import (
    APIRouter, Depends, HTTPException, status,
    UploadFile, File, Form, Header, Query, Path, BackgroundTasks, Request
)
import structlog
import asyncpg
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type, before_sleep_log

# --- Haystack Imports ---
# LLM_FLAG: REMOVE_HAYSTACK_IMPORT - DocumentStore interaction is now abstracted in helpers
# from milvus_haystack import MilvusDocumentStore # No longer needed directly here
from haystack.dataclasses import Document # Needed for type hint in helpers

# --- Custom Application Imports ---
from app.core.config import settings
from app.db import postgres_client as db_client
from app.models.domain import DocumentStatus
from app.api.v1.schemas import IngestResponse, StatusResponse, PaginatedStatusResponse, ErrorDetail
from app.services.minio_client import MinioClient, MinioError
from app.tasks.celery_app import celery_app
# Import the specific task instance registered in process_document.py
from app.tasks.process_document import process_document_haystack_task
# LLM_FLAG: ADD_MISSING_IMPORT - MilvusDocumentStore needed for helper function type hints
from milvus_haystack import MilvusDocumentStore # Re-add for helper type hint

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Helper Functions ---

# LLM_FLAG: NO_CHANGE - Minio Client Dependency
def get_minio_client():
    """Dependency to get Minio client instance."""
    try:
        client = MinioClient()
        return client
    except Exception as e:
        log.exception("Failed to initialize MinioClient dependency", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage service configuration error."
        )

# LLM_FLAG: NO_CHANGE - API DB Retry Strategy
api_db_retry_strategy = retry(
    stop=stop_after_attempt(2),
    wait=wait_fixed(1),
    retry=retry_if_exception_type((asyncpg.exceptions.PostgresConnectionError, TimeoutError, OSError)),
    # Use standard logging level for retry logs
    before_sleep=before_sleep_log(log, logging.WARNING)
)

# LLM_FLAG: NO_CHANGE - DB Connection Context Manager
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
        # Use 503 Service Unavailable for connection issues
        raise HTTPException(status_code=503, detail="Database connection unavailable.")
    finally:
        if conn and pool: # Ensure pool exists before releasing
            await pool.release(conn)


# --- Milvus Synchronous Helper Functions (Corrected) ---

# LLM_FLAG: SENSITIVE_HELPER - Milvus Store Initialization (Sync)
def _initialize_milvus_store_sync() -> MilvusDocumentStore:
    """Synchronously initializes MilvusDocumentStore for API helpers."""
    api_log = log.bind(component="MilvusHelperSync")
    api_log.debug("Initializing MilvusDocumentStore for API helper...")
    try:
        # Ensure embedding_dim is passed if required by the constructor
        # Check the specific version's requirements. Assuming it's not needed for basic ops.
        store = MilvusDocumentStore(
            connection_args={"uri": settings.MILVUS_URI},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            # embedding_dim=settings.EMBEDDING_DIMENSION # May not be needed for count/delete
        )
        api_log.debug("MilvusDocumentStore initialized successfully for API helper.")
        return store
    except TypeError as te:
        # If TypeError occurs, it might be due to missing embedding_dim or other args
        api_log.error("MilvusDocumentStore init TypeError in API helper", error=str(te), exc_info=True, milvus_uri=settings.MILVUS_URI, collection=settings.MILVUS_COLLECTION_NAME)
        raise RuntimeError(f"Milvus TypeError (check arguments/version): {te}") from te
    except Exception as e:
        api_log.exception("Failed to initialize MilvusDocumentStore for API helper", error=str(e))
        raise RuntimeError(f"Milvus Store Initialization Error for API helper: {e}") from e

# LLM_FLAG: SENSITIVE_HELPER - Milvus Chunk Count (Sync - Corrected)
# <<< CORRECTION: Use filter_documents and len() for counting >>>
def _get_milvus_chunk_count_sync(document_id: str, company_id: str) -> int:
    """Synchronously counts chunks in Milvus for a specific document using filter_documents."""
    count_log = log.bind(document_id=document_id, company_id=company_id, component="MilvusHelperSync")
    try:
        store = _initialize_milvus_store_sync()
        # Define the filter based on metadata fields
        filters = {
            "operator": "AND",
            "conditions": [
                # Adjust field names based on how they are stored in Milvus metadata
                {"field": "meta.document_id", "operator": "==", "value": document_id},
                {"field": "meta.company_id", "operator": "==", "value": company_id},
            ]
        }
        count_log.debug("Attempting to filter documents in Milvus", milvus_filters=filters)
        # Use filter_documents which returns a list of Document objects
        docs: List[Document] = store.filter_documents(filters=filters)
        count = len(docs)
        count_log.info("Milvus chunk count successful", count=count)
        return count
    except RuntimeError as re: # Catch store init error
        count_log.error("Failed to get Milvus count due to store init error", error=str(re))
        return -1 # Indicate error
    except Exception as e:
        # Catch potential errors during filtering (e.g., connection, invalid filter format)
        count_log.exception("Error filtering documents in Milvus for count", error=str(e))
        # Log the filter used for debugging
        count_log.debug("Filter used for Milvus count", filter_details=json.dumps(filters))
        return -1 # Indicate error
# <<< END CORRECTION >>>

# LLM_FLAG: SENSITIVE_HELPER - Milvus Delete (Sync - Corrected)
# <<< CORRECTION: Use filter_documents then delete_documents by ID >>>
def _delete_milvus_sync(document_id: str, company_id: str) -> bool:
    """Synchronously deletes chunks from Milvus for a specific document by filtering then deleting IDs."""
    delete_log = log.bind(document_id=document_id, company_id=company_id, component="MilvusHelperSync")
    try:
        store = _initialize_milvus_store_sync()
        # 1. Filter documents to get their IDs
        filters = {
            "operator": "AND",
            "conditions": [
                {"field": "meta.document_id", "operator": "==", "value": document_id},
                {"field": "meta.company_id", "operator": "==", "value": company_id},
            ]
        }
        delete_log.debug("Filtering documents to get IDs for deletion", milvus_filters=filters)
        docs_to_delete: List[Document] = store.filter_documents(filters=filters)

        if not docs_to_delete:
            delete_log.warning("No documents found in Milvus matching criteria for deletion.")
            return True # Consider success if nothing needed deleting

        doc_ids_to_delete = [doc.id for doc in docs_to_delete if doc.id]
        if not doc_ids_to_delete:
             delete_log.warning("Documents found but they lack IDs, cannot delete by ID.", count_found=len(docs_to_delete))
             return False # Cannot proceed with deletion by ID

        delete_log.info(f"Found {len(doc_ids_to_delete)} document IDs to delete from Milvus.")

        # 2. Delete documents by their IDs
        store.delete_documents(ids=doc_ids_to_delete)
        delete_log.info("Milvus delete_documents by ID operation executed successfully.")
        return True
    except RuntimeError as re: # Catch store init error
        delete_log.error("Failed to delete Milvus chunks due to store init error", error=str(re))
        return False
    except Exception as e:
        # Catch errors during filtering or deletion
        delete_log.exception("Error filtering or deleting documents from Milvus", error=str(e))
        # Log filter if error happened during filtering phase (less likely now)
        # delete_log.debug("Filter used for Milvus delete attempt", filter_details=json.dumps(filters))
        return False
# <<< END CORRECTION >>>


# --- API Endpoints ---

@router.post(
    "/upload",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a document for asynchronous ingestion",
    # LLM_FLAG: SWAGGER_METADATA - Keep response descriptions
    responses={
        400: {"model": ErrorDetail, "description": "Bad Request (e.g., invalid metadata, type, duplicate)"},
        415: {"model": ErrorDetail, "description": "Unsupported Media Type"},
        409: {"model": ErrorDetail, "description": "Conflict (Duplicate file)"},
        422: {"model": ErrorDetail, "description": "Validation Error (Missing Headers)"},
        500: {"model": ErrorDetail, "description": "Internal Server Error"},
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB or MinIO)"},
    }
)
async def upload_document(
    request: Request, # Inject Request to access headers and state
    background_tasks: BackgroundTasks, # Keep if needed for future background tasks
    file: UploadFile = File(...),
    metadata_json: Optional[str] = Form(None),
    minio_client: MinioClient = Depends(get_minio_client), # Use dependency injection
):
    """
    Receives a document file and optional metadata, saves it to MinIO,
    creates a record in PostgreSQL, and queues a Celery task for processing.
    Prevents upload if a non-error document with the same name exists for the company.
    Headers X-Company-ID and X-User-ID are read directly from the request.
    """
    # LLM_FLAG: SENSITIVE_AUTH_HEADERS - Reading headers from request
    company_id = request.headers.get("X-Company-ID")
    user_id = request.headers.get("X-User-ID") # Read but not directly used in core processing task payload
    # Get request ID from middleware state if available
    req_id = getattr(request.state, 'request_id', str(uuid.uuid4()))

    endpoint_log = log.bind(request_id=req_id) # Bind request ID early

    # --- Header Validation ---
    if not company_id:
        endpoint_log.warning("Missing X-Company-ID header")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")
    try: company_uuid = uuid.UUID(company_id) # Validate format early
    except ValueError: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")

    if not user_id:
        endpoint_log.warning("Missing X-User-ID header")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-User-ID")
    # Optionally validate user_id format here if needed elsewhere
    # try: user_uuid = uuid.UUID(user_id)
    # except ValueError: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid User ID format.")


    # --- Log Request Details ---
    endpoint_log = endpoint_log.bind(company_id=company_id, user_id=user_id,
                                     filename=file.filename, content_type=file.content_type)
    endpoint_log.info("Processing document ingestion request from gateway")

    # --- File Type Validation ---
    if file.content_type not in settings.SUPPORTED_CONTENT_TYPES:
        endpoint_log.warning("Unsupported content type received")
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type: {file.content_type}. Supported types: {', '.join(settings.SUPPORTED_CONTENT_TYPES)}"
        )

    # --- Metadata Parsing ---
    metadata = {}
    if metadata_json:
        try:
            metadata = json.loads(metadata_json)
            if not isinstance(metadata, dict): raise ValueError("Metadata must be a JSON object.")
            # LLM_FLAG: CUSTOM_METADATA_VALIDATION - Add more specific validation if needed
        except json.JSONDecodeError:
            endpoint_log.warning("Invalid metadata JSON format received")
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid metadata format: Must be valid JSON.")
        except ValueError as e:
             endpoint_log.warning(f"Invalid metadata content: {e}")
             raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid metadata content: {e}")

    # --- Duplicate Check ---
    try:
        async with get_db_conn() as conn:
            # Check for existing document by name and company
            existing_doc = await api_db_retry_strategy(db_client.find_document_by_name_and_company)(
                conn=conn, filename=file.filename, company_id=company_uuid
            )
            # If exists and not in ERROR state, raise Conflict
            if existing_doc and existing_doc['status'] != DocumentStatus.ERROR.value:
                 endpoint_log.warning("Duplicate document detected", document_id=existing_doc['id'], status=existing_doc['status'])
                 raise HTTPException(
                     status_code=status.HTTP_409_CONFLICT,
                     detail=f"Document '{file.filename}' already exists with status '{existing_doc['status']}'. Delete it first or wait for processing."
                 )
            elif existing_doc: # Exists but in ERROR state
                 endpoint_log.info("Found existing document in error state, proceeding with upload (will overwrite).", document_id=existing_doc['id'])
                 # No action needed, will create new record or task logic handles overwrite
    except HTTPException as http_exc: raise http_exc # Re-raise 409
    except Exception as e:
        endpoint_log.exception("Error checking for duplicate document", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error checking for duplicates.")

    # --- Create DB Record ---
    document_id = uuid.uuid4() # Generate new ID for the document
    # Define storage path using company and document ID for isolation
    file_path_in_storage = f"{company_id}/{document_id}/{file.filename}"

    try:
        async with get_db_conn() as conn:
            # Create the initial record with PENDING status
            await api_db_retry_strategy(db_client.create_document_record)(
                conn=conn,
                doc_id=document_id,
                company_id=company_uuid,
                filename=file.filename, # Pass filename as parameter
                file_type=file.content_type,
                file_path=file_path_in_storage, # Store the planned MinIO path
                status=DocumentStatus.PENDING, # Start as PENDING before upload
                metadata=metadata # Store parsed metadata
            )
        endpoint_log.info("Document record created in PostgreSQL", document_id=str(document_id))
    except asyncpg.exceptions.UniqueViolationError: # Example specific DB error
        endpoint_log.exception("Database unique constraint violation during record creation.")
        # Should ideally not happen if duplicate check passed, but handle defensively
        raise HTTPException(status_code=500, detail="Internal server error: Database conflict.")
    except asyncpg.exceptions.UndefinedColumnError as col_err:
        endpoint_log.critical("Database schema error during document creation", error=str(col_err), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error: Database schema mismatch.")
    except Exception as e:
        endpoint_log.exception("Failed to create document record in PostgreSQL", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error creating record.")

    # --- Upload to MinIO ---
    try:
        file_content = await file.read() # Read file content into memory
        await minio_client.upload_file_async(
            object_name=file_path_in_storage,
            data=file_content,
            content_type=file.content_type
        )
        endpoint_log.info("File uploaded successfully to MinIO", object_name=file_path_in_storage)

        # --- Update DB Status to UPLOADED ---
        async with get_db_conn() as conn:
            await api_db_retry_strategy(db_client.update_document_status)(
                conn=conn,
                document_id=document_id,
                status=DocumentStatus.UPLOADED # Update status after successful upload
            )
        endpoint_log.info("Document status updated to 'uploaded'", document_id=str(document_id))

    except MinioError as me:
        endpoint_log.error("Failed to upload file to MinIO", object_name=file_path_in_storage, error=str(me))
        # Attempt to update DB status to ERROR
        try:
            async with get_db_conn() as conn:
                await api_db_retry_strategy(db_client.update_document_status)(
                    conn=conn, document_id=document_id,
                    status=DocumentStatus.ERROR,
                    error_message=f"MinIO upload failed: {me}" # Store error message
                )
        except Exception as db_err:
            endpoint_log.exception("Failed to update status to ERROR after MinIO failure", error=str(db_err))
        # Raise appropriate HTTP error for MinIO failure
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Storage service error: {me}")
    except Exception as e: # Catch other unexpected errors during upload/DB update
         endpoint_log.exception("Unexpected error during file upload or DB update", error=str(e))
         # Attempt to update DB status to ERROR
         try:
             async with get_db_conn() as conn:
                 await api_db_retry_strategy(db_client.update_document_status)(
                     conn=conn, document_id=document_id,
                     status=DocumentStatus.ERROR,
                     error_message=f"Unexpected upload error: {type(e).__name__}" # Store generic error type
                 )
         except Exception as db_err:
             endpoint_log.exception("Failed to update status to ERROR after unexpected upload failure", error=str(db_err))
         raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Internal server error during upload.")
    finally:
        await file.close() # Ensure file handle is closed

    # --- Queue Celery Task ---
    try:
        # Prepare payload for the Celery task
        task_payload = {
            "document_id": str(document_id),
            "company_id": company_id, # Pass company_id as string
            # "user_id": user_id, # User ID is not needed by the processing task itself
            "filename": file.filename,
            "content_type": file.content_type
        }
        # Use the imported task instance's delay method
        task = process_document_haystack_task.delay(**task_payload)
        endpoint_log.info("Document ingestion task queued successfully", task_id=task.id, task_name=process_document_haystack_task.name)

    except Exception as e: # Catch errors during task queueing (e.g., broker connection)
        endpoint_log.exception("Failed to queue Celery task", error=str(e))
        # Attempt to update DB status to ERROR
        try:
            async with get_db_conn() as conn:
                await api_db_retry_strategy(db_client.update_document_status)(
                    conn=conn, document_id=document_id,
                    status=DocumentStatus.ERROR,
                    error_message=f"Failed to queue processing task: {e}"
                )
        except Exception as db_err:
            endpoint_log.exception("Failed to update status to ERROR after Celery failure", error=str(db_err))
        # Raise appropriate error for queueing failure
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to queue processing task.")

    # Return 202 Accepted response
    return IngestResponse(
        document_id=str(document_id),
        task_id=task.id, # Include Celery task ID
        status=DocumentStatus.UPLOADED.value, # Return current status
        message="Document upload accepted, processing started."
    )


@router.get(
    "/status/{document_id}",
    response_model=StatusResponse,
    summary="Get the status of a specific document with live checks",
    # LLM_FLAG: SWAGGER_METADATA - Keep response descriptions
    responses={
        404: {"model": ErrorDetail, "description": "Document not found"},
        422: {"model": ErrorDetail, "description": "Validation Error (Missing Headers or Invalid ID)"},
        500: {"model": ErrorDetail, "description": "Internal Server Error"},
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB, MinIO, Milvus)"},
    }
)
async def get_document_status(
    request: Request,
    document_id: uuid.UUID = Path(..., description="The UUID of the document"),
    minio_client: MinioClient = Depends(get_minio_client), # Dependency injection
):
    """
    Retrieves the status of a document from PostgreSQL.
    Performs live checks:
    - Verifies file existence in MinIO using the 'file_path' from DB.
    - Counts chunks in Milvus using the synchronous helper.
    - Updates the DB status/chunk_count/error_message if inconsistencies are found.
    Requires X-Company-ID header for authorization.
    """
    # LLM_FLAG: SENSITIVE_AUTH_HEADERS - Reading headers
    company_id = request.headers.get("X-Company-ID")
    req_id = getattr(request.state, 'request_id', 'N/A') # Get request ID

    # Validate header and company ID format
    if not company_id:
        log.bind(request_id=req_id).warning("Missing X-Company-ID header in get_document_status")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")
    try: company_uuid = uuid.UUID(company_id)
    except ValueError: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")

    status_log = log.bind(request_id=req_id, document_id=str(document_id), company_id=company_id)
    status_log.info("Request received for document status")

    # --- Fetch Base Data from DB ---
    doc_data: Optional[Dict[str, Any]] = None
    try:
        async with get_db_conn() as conn:
             doc_data = await api_db_retry_strategy(db_client.get_document_by_id)(
                 conn, doc_id=document_id, company_id=company_uuid # Verify ownership
             )
        if not doc_data:
            status_log.warning("Document not found in DB for this company")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
        status_log.info("Retrieved base document data from DB", status=doc_data['status'])
    except HTTPException as http_exc: raise http_exc # Re-raise 404
    except Exception as e:
        status_log.exception("Error fetching document status from DB", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error fetching status.")

    # --- Initialize State for Live Checks and Potential Updates ---
    needs_update = False
    # Use Enum for internal logic, store original values from DB
    current_status_enum = DocumentStatus(doc_data['status'])
    current_chunk_count = doc_data.get('chunk_count')
    current_error_message = doc_data.get('error_message')
    # These will hold the potentially corrected values
    updated_status_enum = current_status_enum
    updated_chunk_count = current_chunk_count
    updated_error_message = current_error_message

    # --- Perform Live MinIO Check ---
    # Use the correct column name 'file_path'
    minio_path = doc_data.get('file_path')
    minio_exists = False # Default to false
    if not minio_path:
         status_log.warning("MinIO file path missing in DB record", db_id=doc_data['id'])
         # If file path is missing, but status isn't ERROR/PENDING, it's an error state
         if updated_status_enum not in [DocumentStatus.ERROR, DocumentStatus.PENDING]:
             status_log.warning("Correcting status to ERROR due to missing MinIO path.")
             needs_update = True
             updated_status_enum = DocumentStatus.ERROR
             updated_error_message = (updated_error_message or "") + " File path missing from record."
    else:
        status_log.debug("Checking MinIO for file existence", object_name=minio_path)
        try:
            minio_exists = await minio_client.check_file_exists_async(minio_path)
            status_log.info("MinIO existence check complete", exists=minio_exists)
            # If file doesn't exist but DB status implies it should, mark for update
            if not minio_exists and updated_status_enum not in [DocumentStatus.ERROR, DocumentStatus.PENDING]:
                 status_log.warning("File missing in MinIO but DB status is not ERROR/PENDING.", current_db_status=updated_status_enum.value)
                 if updated_status_enum != DocumentStatus.ERROR: # Avoid overwriting specific errors
                     needs_update = True
                     updated_status_enum = DocumentStatus.ERROR
                     updated_error_message = "File missing from storage." # Set specific error
                     updated_chunk_count = 0 # Reset chunk count if file is gone
        except Exception as minio_e:
            status_log.error("MinIO check failed", error=str(minio_e))
            minio_exists = False # Assume doesn't exist on error
            if updated_status_enum != DocumentStatus.ERROR:
                 needs_update = True
                 updated_status_enum = DocumentStatus.ERROR
                 updated_error_message = (updated_error_message or "") + f" MinIO check error ({type(minio_e).__name__})."
                 updated_chunk_count = 0 # Reset chunk count

    # --- Perform Live Milvus Check ---
    status_log.debug("Checking Milvus for chunk count...")
    loop = asyncio.get_running_loop()
    milvus_chunk_count = -1 # Default to -1 (error/unknown)
    try:
        # Use the corrected synchronous helper ran in executor
        milvus_chunk_count = await loop.run_in_executor(
            None, _get_milvus_chunk_count_sync, str(document_id), company_id
        )
        status_log.info("Milvus chunk count check complete", count=milvus_chunk_count)

        # --- Logic to Detect and Correct Inconsistencies based on Milvus Count ---
        if milvus_chunk_count == -1: # Error during count check
            status_log.error("Milvus count check failed (returned -1).")
            if updated_status_enum != DocumentStatus.ERROR:
                needs_update = True
                updated_status_enum = DocumentStatus.ERROR
                updated_error_message = (updated_error_message or "") + " Failed Milvus count check."
        elif milvus_chunk_count > 0:
            # Chunks exist. If DB status is UPLOADED, it should be PROCESSED.
            if updated_status_enum == DocumentStatus.UPLOADED:
                status_log.warning("Inconsistency: Chunks found in Milvus but DB status is 'uploaded'. Correcting to 'processed'.")
                needs_update = True
                updated_status_enum = DocumentStatus.PROCESSED
                updated_chunk_count = milvus_chunk_count # Use live count
                updated_error_message = None # Clear error message
            # If DB status is already PROCESSED, ensure chunk count matches.
            elif updated_status_enum == DocumentStatus.PROCESSED:
                if updated_chunk_count != milvus_chunk_count:
                     status_log.warning("Inconsistency: DB chunk count differs from live Milvus count. Updating DB.", db_count=updated_chunk_count, live_count=milvus_chunk_count)
                     needs_update = True
                     updated_chunk_count = milvus_chunk_count
        elif milvus_chunk_count == 0:
            # No chunks exist. If DB status is PROCESSED, it's an error state.
            if updated_status_enum == DocumentStatus.PROCESSED:
                 status_log.warning("Inconsistency: DB status is 'processed' but no chunks found in Milvus. Correcting to 'error'.")
                 needs_update = True
                 updated_status_enum = DocumentStatus.ERROR
                 updated_chunk_count = 0 # Set count to 0
                 updated_error_message = (updated_error_message or "") + " Processed data missing (Milvus count is 0)."
            # If status is ERROR, ensure chunk count is 0
            elif updated_status_enum == DocumentStatus.ERROR and updated_chunk_count != 0:
                needs_update = True
                updated_chunk_count = 0

    except Exception as e:
        status_log.exception("Unexpected error during Milvus count check", error=str(e))
        milvus_chunk_count = -1 # Indicate error
        if updated_status_enum != DocumentStatus.ERROR:
            needs_update = True
            updated_status_enum = DocumentStatus.ERROR
            updated_error_message = (updated_error_message or "") + f" Error checking Milvus ({type(e).__name__})."

    # --- Update DB Record if Inconsistency Detected ---
    if needs_update:
        status_log.warning("Inconsistency detected, updating document status in DB",
                           new_status=updated_status_enum.value,
                           new_count=updated_chunk_count,
                           new_error=updated_error_message)
        try:
             async with get_db_conn() as conn:
                 # Pass the potentially corrected values
                 await api_db_retry_strategy(db_client.update_document_status)(
                     conn=conn,
                     document_id=document_id,
                     status=updated_status_enum, # Pass Enum
                     chunk_count=updated_chunk_count,
                     error_message=updated_error_message
                 )
             status_log.info("Document status updated successfully in DB due to inconsistency check.")
             # Update local doc_data to reflect the change for the response
             doc_data['status'] = updated_status_enum.value
             doc_data['chunk_count'] = updated_chunk_count
             doc_data['error_message'] = updated_error_message
        except Exception as e:
            # Log failure but proceed to return potentially corrected state anyway
            status_log.exception("Failed to update document status in DB after inconsistency check", error=str(e))
            # Consider raising 503 if update failure is critical? For now, just log.

    # --- Construct and Return Final Response ---
    status_log.info("Returning final document status")
    # Use the potentially updated values from doc_data (which was updated if DB update succeeded)
    # or the 'updated_*' variables if DB update failed but correction was intended.
    final_status_val = doc_data['status']
    final_chunk_count_val = doc_data.get('chunk_count', 0) # Use updated value if available
    final_error_message_val = doc_data.get('error_message')

    return StatusResponse(
        document_id=str(doc_data['id']),
        company_id=str(doc_data['company_id']), # Ensure UUID is stringified
        status=final_status_val,
        file_name=doc_data.get('file_name'),
        file_type=doc_data.get('file_type'),
        file_path=doc_data.get('file_path'), # Use correct field name
        chunk_count=final_chunk_count_val,
        minio_exists=minio_exists, # Live check result
        milvus_chunk_count=milvus_chunk_count, # Live check result (-1 if error)
        last_updated=doc_data.get('updated_at'),
        uploaded_at=doc_data.get('uploaded_at'),
        error_message=final_error_message_val,
        metadata=doc_data.get('metadata')
    )


# <<< CORRECTION: Changed response_model to List[StatusResponse] >>>
@router.get(
    "/status",
    response_model=List[StatusResponse], # Return a list directly
    summary="List document statuses with pagination and live checks",
    # LLM_FLAG: SWAGGER_METADATA - Keep response descriptions
    responses={
        422: {"model": ErrorDetail, "description": "Validation Error (Missing Headers)"},
        500: {"model": ErrorDetail, "description": "Internal Server Error"},
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB, MinIO, Milvus)"},
    }
)
async def list_document_statuses(
    request: Request,
    limit: int = Query(30, ge=1, le=100, description="Number of documents per page"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    minio_client: MinioClient = Depends(get_minio_client),
):
# <<< END CORRECTION >>>
    """
    Lists documents for the company with pagination.
    Performs live checks for MinIO/Milvus in parallel for listed documents using 'file_path'.
    Updates the DB status/chunk_count if inconsistencies are found sequentially after checks.
    Returns the potentially updated status information as a direct list.
    Requires X-Company-ID header.
    """
    # LLM_FLAG: SENSITIVE_AUTH_HEADERS - Reading headers
    company_id = request.headers.get("X-Company-ID")
    req_id = getattr(request.state, 'request_id', 'N/A') # Get request ID

    # Validate header and company ID format
    if not company_id:
        log.bind(request_id=req_id).warning("Missing X-Company-ID header in list_document_statuses")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")
    try: company_uuid = uuid.UUID(company_id)
    except ValueError: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")

    list_log = log.bind(request_id=req_id, company_id=company_id, limit=limit, offset=offset)
    list_log.info("Listing document statuses with real-time checks")

    # --- Fetch Base Data from DB ---
    documents_db: List[Dict[str, Any]] = []
    total_db_count: int = 0 # Fetched but not used in response now

    try:
        async with get_db_conn() as conn:
             # Fetch paginated list and total count
            documents_db, total_db_count = await api_db_retry_strategy(db_client.list_documents_paginated)(
                conn, company_id=company_uuid, limit=limit, offset=offset
            )
        list_log.info("Retrieved documents from DB", count=len(documents_db), total_db_count=total_db_count)
    except AttributeError as ae: # Catch if list_documents_paginated is missing
        list_log.exception("AttributeError calling database function (likely missing function)", error=str(ae))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error: Incomplete database client.")
    except Exception as e:
        list_log.exception("Error listing documents from DB", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error listing documents.")

    # If no documents, return empty list immediately
    if not documents_db:
        # <<< CORRECTION: Return empty list directly >>>
        return []
        # <<< END CORRECTION >>>

    # --- Define Async Helper for Concurrent Checks ---
    async def check_single_document(doc_db_data: Dict[str, Any]) -> Dict[str, Any]:
        """Async helper to check MinIO/Milvus for one document and determine needed updates."""
        # LLM_FLAG: CONCURRENT_CHECK_LOGIC - Logic for checking one doc
        check_log = log.bind(request_id=req_id, document_id=str(doc_db_data['id']), company_id=company_id)
        check_log.debug("Starting live checks for document")

        # Initialize state based on DB data
        doc_id_str = str(doc_db_data['id'])
        doc_needs_update = False
        doc_current_status_enum = DocumentStatus(doc_db_data['status'])
        doc_current_chunk_count = doc_db_data.get('chunk_count')
        doc_current_error_msg = doc_db_data.get('error_message')

        doc_updated_status_enum = doc_current_status_enum
        doc_updated_chunk_count = doc_current_chunk_count
        doc_updated_error_msg = doc_current_error_msg

        # --- MinIO Check ---
        minio_path_db = doc_db_data.get('file_path') # Use correct column name
        live_minio_exists = False
        if minio_path_db:
            try:
                live_minio_exists = await minio_client.check_file_exists_async(minio_path_db)
                check_log.debug("MinIO check done", exists=live_minio_exists)
                if not live_minio_exists and doc_updated_status_enum not in [DocumentStatus.ERROR, DocumentStatus.PENDING]:
                    if doc_updated_status_enum != DocumentStatus.ERROR:
                         doc_needs_update = True; doc_updated_status_enum = DocumentStatus.ERROR; doc_updated_error_msg = "File missing from storage."; doc_updated_chunk_count = 0
            except Exception as e:
                check_log.error("MinIO check failed", error=str(e)); live_minio_exists = False
                if doc_updated_status_enum != DocumentStatus.ERROR:
                    doc_needs_update = True; doc_updated_status_enum = DocumentStatus.ERROR; doc_updated_error_msg = (doc_updated_error_msg or "") + f" MinIO check error ({type(e).__name__})."; doc_updated_chunk_count = 0
        else: check_log.warning("MinIO file path missing in DB record."); live_minio_exists = False

        # --- Milvus Check ---
        live_milvus_chunk_count = -1
        loop = asyncio.get_running_loop()
        try:
            live_milvus_chunk_count = await loop.run_in_executor( None, _get_milvus_chunk_count_sync, doc_id_str, company_id )
            check_log.debug("Milvus count check done", count=live_milvus_chunk_count)
            if live_milvus_chunk_count == -1:
                if doc_updated_status_enum != DocumentStatus.ERROR:
                    doc_needs_update = True; doc_updated_status_enum = DocumentStatus.ERROR; doc_updated_error_msg = (doc_updated_error_msg or "") + " Failed Milvus count check."
            elif live_milvus_chunk_count > 0:
                if doc_updated_status_enum == DocumentStatus.UPLOADED:
                     doc_needs_update = True; doc_updated_status_enum = DocumentStatus.PROCESSED; doc_updated_chunk_count = live_milvus_chunk_count; doc_updated_error_msg = None
                elif doc_updated_status_enum == DocumentStatus.PROCESSED and doc_updated_chunk_count != live_milvus_chunk_count:
                     doc_needs_update = True; doc_updated_chunk_count = live_milvus_chunk_count
            elif live_milvus_chunk_count == 0:
                if doc_updated_status_enum == DocumentStatus.PROCESSED:
                     doc_needs_update = True; doc_updated_status_enum = DocumentStatus.ERROR; doc_updated_chunk_count = 0; doc_updated_error_msg = (doc_updated_error_msg or "") + " Processed data missing."
                elif doc_updated_status_enum == DocumentStatus.ERROR and doc_updated_chunk_count != 0:
                     doc_needs_update = True; doc_updated_chunk_count = 0
        except Exception as e:
            check_log.exception("Unexpected error during Milvus count check", error=str(e)); live_milvus_chunk_count = -1
            if doc_updated_status_enum != DocumentStatus.ERROR:
                doc_needs_update = True; doc_updated_status_enum = DocumentStatus.ERROR; doc_updated_error_msg = (doc_updated_error_msg or "") + f" Error checking Milvus ({type(e).__name__})."

        # Return combined results including original data and update flags/values
        return {
            "db_data": doc_db_data, # Original DB data
            "needs_update": doc_needs_update,
            "updated_status_enum": doc_updated_status_enum, # Final status (Enum)
            "updated_chunk_count": doc_updated_chunk_count, # Final count
            "final_error_message": doc_updated_error_msg, # Final error message
            "live_minio_exists": live_minio_exists, # Live check result
            "live_milvus_chunk_count": live_milvus_chunk_count # Live check result
        }

    # --- Run Checks Concurrently ---
    # <<< CORRECTION: Use asyncio.gather for concurrent checks >>>
    list_log.info(f"Performing live checks for {len(documents_db)} documents concurrently...")
    check_tasks = [check_single_document(doc) for doc in documents_db]
    check_results = await asyncio.gather(*check_tasks)
    list_log.info("Live checks completed.")
    # <<< END CORRECTION >>>

    # --- Process Results and Prepare Updates ---
    updated_doc_data_map = {} # Stores the final state for response construction
    docs_to_update_in_db = [] # Stores info needed for DB update calls
    for result in check_results:
        doc_id_str = str(result["db_data"]["id"])
        # Store the potentially corrected state for the final response
        updated_doc_data_map[doc_id_str] = {
            **result["db_data"], # Start with original data
            "status": result["updated_status_enum"].value, # Overwrite status with final string value
            "chunk_count": result["updated_chunk_count"], # Overwrite count
            "error_message": result["final_error_message"] # Overwrite error message
        }
        # If an update is needed based on the check, add details to the update list
        if result["needs_update"]:
            docs_to_update_in_db.append({
                "id": result["db_data"]["id"], # UUID object
                "status_enum": result["updated_status_enum"], # Pass Enum to DB func
                "chunk_count": result["updated_chunk_count"],
                "error_message": result["final_error_message"]
            })

    # --- Perform DB Updates Sequentially (if needed) ---
    if docs_to_update_in_db:
        list_log.warning("Updating statuses sequentially in DB for inconsistent documents", count=len(docs_to_update_in_db))
        updated_count = 0
        failed_update_count = 0
        try:
             # Use a single connection for all sequential updates
             async with get_db_conn() as conn:
                 for update_info in docs_to_update_in_db:
                     doc_id_to_update = update_info["id"]
                     try:
                         # Call DB update function with the determined final state
                         await api_db_retry_strategy(db_client.update_document_status)(
                             conn=conn, # Use the single acquired connection
                             document_id=doc_id_to_update,
                             status=update_info["status_enum"], # Pass Enum
                             chunk_count=update_info["chunk_count"],
                             error_message=update_info["error_message"]
                         )
                         updated_count += 1
                         list_log.info("Successfully updated DB status", document_id=str(doc_id_to_update), new_status=update_info['status_enum'].value)
                     except Exception as single_update_err:
                         failed_update_count += 1
                         # Log the error for this specific update but continue with others
                         list_log.error("Failed DB update for single document during list check", document_id=str(doc_id_to_update), error=str(single_update_err))
             list_log.info("Finished sequential DB updates.", updated=updated_count, failed=failed_update_count)
        except Exception as bulk_db_conn_err: # Error getting the connection itself
            list_log.exception("Error acquiring DB connection for sequential updates, updates skipped.", error=str(bulk_db_conn_err))
            # Updates were skipped, response will reflect intended state but DB might lag

    # --- Construct Final Response List ---
    final_response_items = []
    for result in check_results: # Iterate in original order
         doc_id_str = str(result["db_data"]["id"])
         # Use the potentially corrected data stored in the map
         current_data = updated_doc_data_map.get(doc_id_str, result["db_data"]) # Fallback just in case

         final_response_items.append(StatusResponse(
            document_id=doc_id_str,
            company_id=str(current_data['company_id']), # Ensure UUID is stringified
            status=current_data['status'], # Use status string from map
            file_name=current_data.get('file_name'),
            file_type=current_data.get('file_type'),
            file_path=current_data.get('file_path'), # Use correct field name
            chunk_count=current_data.get('chunk_count', 0),
            minio_exists=result["live_minio_exists"], # Use live check result
            milvus_chunk_count=result["live_milvus_chunk_count"], # Use live check result
            last_updated=current_data.get('updated_at'),
            uploaded_at=current_data.get('uploaded_at'),
            error_message=current_data.get('error_message'),
            metadata=current_data.get('metadata')
         ))

    list_log.info("Returning enriched statuses", count=len(final_response_items))
    # <<< CORRECTION: Return list directly >>>
    return final_response_items
    # <<< END CORRECTION >>>


@router.post(
    "/retry/{document_id}",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Retry ingestion for a document currently in 'error' state",
    # LLM_FLAG: SWAGGER_METADATA - Keep response descriptions
    responses={
        404: {"model": ErrorDetail, "description": "Document not found"},
        409: {"model": ErrorDetail, "description": "Document is not in 'error' state"},
        422: {"model": ErrorDetail, "description": "Validation Error (Missing Headers or Invalid ID)"},
        500: {"model": ErrorDetail, "description": "Internal Server Error"},
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB or Celery)"},
    }
)
async def retry_ingestion(
    request: Request,
    document_id: uuid.UUID = Path(..., description="The UUID of the document to retry"),
):
    """
    Allows retrying the ingestion process for a document that previously failed.
    Requires document to be in 'error' state. Updates status to 'processing'
    and re-queues the processing task. Requires X-Company-ID and X-User-ID headers.
    """
    # LLM_FLAG: SENSITIVE_AUTH_HEADERS - Reading headers
    company_id = request.headers.get("X-Company-ID")
    user_id = request.headers.get("X-User-ID") # Read user_id, not passed to task
    req_id = getattr(request.state, 'request_id', 'N/A') # Get request ID

    # Validate headers and IDs
    if not company_id: raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")
    try: company_uuid = uuid.UUID(company_id)
    except ValueError: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")
    if not user_id: raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-User-ID")
    # try: user_uuid = uuid.UUID(user_id) # Optional validation
    # except ValueError: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid User ID format.")

    retry_log = log.bind(request_id=req_id, document_id=str(document_id), company_id=company_id, user_id=user_id)
    retry_log.info("Received request to retry document ingestion")

    # --- Fetch Document and Validate State ---
    doc_data: Optional[Dict[str, Any]] = None
    try:
         async with get_db_conn() as conn:
            # Fetch the document, verifying ownership
            doc_data = await api_db_retry_strategy(db_client.get_document_by_id)(
                conn, doc_id=document_id, company_id=company_uuid
            )
         if not doc_data:
             retry_log.warning("Document not found for retry")
             raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
         # Check if status is actually ERROR
         if doc_data['status'] != DocumentStatus.ERROR.value:
             retry_log.warning("Document not in error state, cannot retry", current_status=doc_data['status'])
             raise HTTPException(
                 status_code=status.HTTP_409_CONFLICT,
                 detail=f"Document is not in 'error' state (current state: {doc_data['status']}). Cannot retry."
             )
         retry_log.info("Document found and confirmed to be in 'error' state.")
    except HTTPException as http_exc: raise http_exc # Re-raise 404, 409
    except Exception as e:
        retry_log.exception("Error fetching document for retry", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error checking document for retry.")

    # --- Update Status to PROCESSING ---
    try:
        async with get_db_conn() as conn:
            await api_db_retry_strategy(db_client.update_document_status)(
                conn=conn,
                document_id=document_id,
                status=DocumentStatus.PROCESSING, # Set status to PROCESSING
                chunk_count=None, # Let worker determine chunk count
                error_message=None # Clear previous error message
            )
        retry_log.info("Document status updated to 'processing' for retry.")
    except Exception as e:
        retry_log.exception("Failed to update document status to 'processing' for retry", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error updating status for retry.")

    # --- Re-queue Celery Task ---
    try:
        # Get necessary info from fetched doc_data
        file_name_from_db = doc_data.get('file_name')
        content_type_from_db = doc_data.get('file_type')
        if not file_name_from_db or not content_type_from_db:
             retry_log.error("File name or type missing in fetched document data for retry task payload", document_id=str(document_id))
             # Attempt to revert status back to ERROR? Risky. Raise 500.
             raise HTTPException(status_code=500, detail="Internal error: Missing file name or type for retry.")

        task_payload = {
            "document_id": str(document_id),
            "company_id": company_id, # Pass as string
            "filename": file_name_from_db,
            "content_type": content_type_from_db
        }
        # Use the imported task instance
        task = process_document_haystack_task.delay(**task_payload)
        retry_log.info("Document reprocessing task queued successfully", task_id=task.id, task_name=process_document_haystack_task.name)
    except Exception as e:
        retry_log.exception("Failed to re-queue Celery task for retry", error=str(e))
        # If queueing fails, the status is left as 'processing', which is not ideal.
        # Manual intervention might be needed, or a cleanup job.
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to queue reprocessing task.")

    # Return response indicating retry has started
    return IngestResponse(
        document_id=str(document_id),
        task_id=task.id,
        status=DocumentStatus.PROCESSING.value, # Reflect the status set before queueing
        message="Document retry accepted, processing started."
    )


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document and its associated data",
    # LLM_FLAG: SWAGGER_METADATA - Keep response descriptions
    responses={
        404: {"model": ErrorDetail, "description": "Document not found"},
        422: {"model": ErrorDetail, "description": "Validation Error (Missing Headers or Invalid ID)"},
        500: {"model": ErrorDetail, "description": "Internal Server Error"},
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB, MinIO, Milvus)"},
    }
)
async def delete_document_endpoint(
    request: Request,
    document_id: uuid.UUID = Path(..., description="The UUID of the document to delete"),
    minio_client: MinioClient = Depends(get_minio_client), # Dependency injection
):
    """
    Deletes a document completely:
    - Removes chunks from Milvus (filtering by ID after getting IDs via metadata filter).
    - Removes the file from MinIO using the 'file_path' from DB.
    - Removes the record from PostgreSQL.
    Verifies ownership using X-Company-ID header before deletion.
    """
    # LLM_FLAG: SENSITIVE_AUTH_HEADERS - Reading headers
    company_id = request.headers.get("X-Company-ID")
    req_id = getattr(request.state, 'request_id', 'N/A') # Get request ID

    # Validate header and company ID format
    if not company_id:
        log.bind(request_id=req_id).warning("Missing X-Company-ID header in delete_document_endpoint")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")
    try: company_uuid = uuid.UUID(company_id)
    except ValueError: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")

    delete_log = log.bind(request_id=req_id, document_id=str(document_id), company_id=company_id)
    delete_log.info("Received request to delete document")

    # --- Fetch Document Data (for file_path and verification) ---
    doc_data: Optional[Dict[str, Any]] = None
    try:
        async with get_db_conn() as conn:
             # Verify ownership and get data
             doc_data = await api_db_retry_strategy(db_client.get_document_by_id)(
                 conn, doc_id=document_id, company_id=company_uuid
             )
        if not doc_data:
            delete_log.warning("Document not found for deletion")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
        delete_log.info("Document verified for deletion", filename=doc_data.get('file_name'))
    except HTTPException as http_exc: raise http_exc # Re-raise 404
    except Exception as e:
        delete_log.exception("Error verifying document before deletion", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error during delete verification.")

    errors = [] # Collect non-critical errors during deletion steps

    # --- 1. Delete from Milvus (Vector Store) ---
    # <<< CORRECTION: Use corrected _delete_milvus_sync helper >>>
    delete_log.info("Attempting to delete chunks from Milvus...")
    loop = asyncio.get_running_loop()
    try:
        # Run the corrected synchronous delete helper in executor
        milvus_deleted = await loop.run_in_executor(None, _delete_milvus_sync, str(document_id), company_id)
        if milvus_deleted: delete_log.info("Milvus delete command executed successfully.")
        else:
            # Helper returning False indicates an issue (e.g., failed to get IDs, delete failed)
            errors.append("Failed Milvus delete (helper returned False)")
            delete_log.warning("Milvus delete operation reported failure (check helper logs).")
    except Exception as e:
        # Catch unexpected errors from the helper/executor
        delete_log.exception("Unexpected error during Milvus delete execution", error=str(e))
        errors.append(f"Milvus delete exception: {type(e).__name__}")
    # <<< END CORRECTION >>>

    # --- 2. Delete from MinIO (Object Storage) ---
    minio_path = doc_data.get('file_path') # Use correct column name
    if minio_path:
        delete_log.info("Attempting to delete file from MinIO...", object_name=minio_path)
        try:
            await minio_client.delete_file_async(minio_path)
            delete_log.info("Successfully deleted file from MinIO.")
        except MinioError as me:
            # Log as error but allow process to continue to DB deletion attempt
            delete_log.error("Failed to delete file from MinIO", object_name=minio_path, error=str(me))
            errors.append(f"MinIO delete failed: {me}")
        except Exception as e:
            delete_log.exception("Unexpected error during MinIO delete", error=str(e))
            errors.append(f"MinIO delete exception: {type(e).__name__}")
    else:
        # Log if path is missing, potentially an issue but not blocking DB delete
        delete_log.warning("Skipping MinIO delete: file path not found in DB record.")
        errors.append("MinIO path unknown in DB.")

    # --- 3. Delete from PostgreSQL (Database Record) ---
    # This is the last and most critical step for logical deletion.
    delete_log.info("Attempting to delete record from PostgreSQL...")
    try:
         async with get_db_conn() as conn:
            # Pass connection and company_uuid for verification again within delete function
            deleted_in_db = await api_db_retry_strategy(db_client.delete_document)(
                conn=conn, # Pass the connection
                doc_id=document_id,
                company_id=company_uuid # Ensure delete also checks ownership
            )
            if deleted_in_db: delete_log.info("Document record deleted successfully from PostgreSQL")
            else:
                # This implies the document was already gone, despite the initial check.
                # Could be a race condition, but log it as a warning.
                delete_log.warning("PostgreSQL delete command executed but no record was deleted (already gone or final check failed?).")
                # Don't add to errors list if it was already gone, as the goal is achieved.
    except Exception as e:
        # This is critical - data might be orphaned in MinIO/Milvus if this fails
        delete_log.exception("CRITICAL: Failed to delete document record from PostgreSQL", error=str(e))
        error_detail = f"Deleted/Attempted delete from storage/vectors (errors: {', '.join(errors)}) but FAILED to delete DB record: {e}"
        # Return 500 because the overall state is inconsistent and main record persists
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=error_detail)

    # Log any non-critical errors encountered during Milvus/MinIO steps
    if errors: delete_log.warning("Document deletion process completed with non-critical errors (Milvus/MinIO state)", errors=errors)

    delete_log.info("Document deletion process finished.")
    # Return 204 No Content on success (DB record deleted), even if minor errors occurred elsewhere.
    return None # FastAPI handles 204 automatically for None return
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
import logging

log = logging.getLogger(__name__)

class ErrorDetail(BaseModel):
    """Schema for error details in responses."""
    detail: str

class IngestResponse(BaseModel):
    document_id: uuid.UUID
    task_id: str
    status: str
    message: str = "Document upload received and queued for processing."

    class Config:
        json_schema_extra = {
            "example": {
                "document_id": "123e4567-e89b-12d3-a456-426614174000",
                "task_id": "c79ba436-fe88-4b82-9afc-44b1091564e4",
                "status": DocumentStatus.UPLOADED.value,
                "message": "Document upload accepted, processing started."
            }
        }

class StatusResponse(BaseModel):
    document_id: uuid.UUID = Field(..., alias="id")
    company_id: Optional[uuid.UUID] = None
    file_name: str
    file_type: str
    # --- RENAMED FIELD: file_path ---
    file_path: Optional[str] = None
    # --------------------------------
    metadata: Optional[Dict[str, Any]] = None
    status: str
    chunk_count: Optional[int] = 0
    error_message: Optional[str] = None
    uploaded_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    minio_exists: Optional[bool] = None
    milvus_chunk_count: Optional[int] = None
    message: Optional[str] = None

    @field_validator('metadata', mode='before')
    @classmethod
    def metadata_to_dict(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                log.warning("Invalid metadata JSON found in DB record", raw_metadata=v)
                return {"error": "invalid metadata JSON in DB"}
        return v if v is None or isinstance(v, dict) else {}

    class Config:
        validate_assignment = True
        populate_by_name = True
        # --- UPDATED EXAMPLE: Use file_path ---
        json_schema_extra = {
             "example": {
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
        }
        # ------------------------------------

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
                     {
                        "id": "52ad2ba8-cab9-4108-a504-b9822fe99bdc",
                        "company_id": "51a66c8f-f6b1-43bd-8038-8768471a8b09",
                        "file_name": "Anexo-00-Modificaciones-de-la-Guia-5.1.0.pdf",
                        "file_type": "application/pdf",
                        # --- UPDATED EXAMPLE ---
                        "file_path": "51a66c8f-f6b1-43bd-8038-8768471a8b09/52ad2ba8-cab9-4108-a504-b9822fe99bdc/Anexo-00-Modificaciones-de-la-Guia-5.1.0.pdf",
                        # -----------------------
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

# --- Service Names en K8s (sin cambios) ---
POSTGRES_K8S_SVC = "postgresql-service.nyro-develop.svc.cluster.local"
MINIO_K8S_SVC = "minio-service.nyro-develop.svc.cluster.local"
MILVUS_K8S_SVC = "milvus-milvus.default.svc.cluster.local"
REDIS_K8S_SVC = "redis-service-master.nyro-develop.svc.cluster.local"

# --- Defaults (sin cambios) ---
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex"
POSTGRES_K8S_USER_DEFAULT = "postgres"
MINIO_K8S_PORT_DEFAULT = 9000
MINIO_BUCKET_DEFAULT = "ingested-documents"
MILVUS_K8S_PORT_DEFAULT = 19530
MILVUS_DEFAULT_COLLECTION = "document_chunks_haystack"
MILVUS_DEFAULT_INDEX_PARAMS = '{"metric_type": "COSINE", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 256}}'
MILVUS_DEFAULT_SEARCH_PARAMS = '{"metric_type": "COSINE", "params": {"ef": 128}}'
# OPENAI_DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small" # No relevante para ingesta ahora
DEFAULT_FASTEMBED_MODEL = "BAAI/bge-base-en-v1.5" # Un default por si no se especifica
DEFAULT_FASTEMBED_DIM = 768 # Dimensión para bge-base

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env', env_prefix='INGEST_', env_file_encoding='utf-8',
        case_sensitive=False, extra='ignore'
    )

    # --- General (sin cambios) ---
    PROJECT_NAME: str = "Atenex Ingest Service"
    API_V1_STR: str = "/api/v1/ingest"
    LOG_LEVEL: str = "INFO"

    # --- Celery (sin cambios) ---
    CELERY_BROKER_URL: RedisDsn = Field(default_factory=lambda: RedisDsn(f"redis://{REDIS_K8S_SVC}:{REDIS_K8S_PORT_DEFAULT}/0"))
    CELERY_RESULT_BACKEND: RedisDsn = Field(default_factory=lambda: RedisDsn(f"redis://{REDIS_K8S_SVC}:{REDIS_K8S_PORT_DEFAULT}/1"))


    # --- Database (sin cambios) ---
    POSTGRES_USER: str = POSTGRES_K8S_USER_DEFAULT
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = POSTGRES_K8S_SVC
    POSTGRES_PORT: int = POSTGRES_K8S_PORT_DEFAULT
    POSTGRES_DB: str = POSTGRES_K8S_DB_DEFAULT

    # --- Milvus (sin cambios) ---
    MILVUS_URI: str = Field(default=f"http://{MILVUS_K8S_SVC}:{MILVUS_K8S_PORT_DEFAULT}")
    MILVUS_COLLECTION_NAME: str = MILVUS_DEFAULT_COLLECTION
    MILVUS_METADATA_FIELDS: List[str] = Field(default=["company_id", "document_id", "file_name", "file_type"])
    MILVUS_CONTENT_FIELD: str = "content"
    MILVUS_EMBEDDING_FIELD: str = "embedding"
    MILVUS_INDEX_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_INDEX_PARAMS))
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_SEARCH_PARAMS))

    # --- MinIO (sin cambios) ---
    MINIO_ENDPOINT: str = Field(default=f"{MINIO_K8S_SVC}:{MINIO_K8S_PORT_DEFAULT}")
    MINIO_ACCESS_KEY: SecretStr
    MINIO_SECRET_KEY: SecretStr
    MINIO_BUCKET_NAME: str = MINIO_BUCKET_DEFAULT
    MINIO_USE_SECURE: bool = False

    # --- Embeddings (Configuración para FastEmbed) ---
    FASTEMBED_MODEL: str = DEFAULT_FASTEMBED_MODEL # Modelo FastEmbed a usar
    USE_GPU: bool = False # Usar GPU (ahora es un booleano)
    EMBEDDING_DIMENSION: int = DEFAULT_FASTEMBED_DIM # Dimensión del modelo (tomado de ConfigMap)
    OPENAI_API_KEY: Optional[SecretStr] = None # Mantener opcional si se usa en otro lado

    # --- Clients (sin cambios) ---
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 2
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    # --- Processing (sin cambios) ---
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
        # (sin cambios)
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        normalized_v = v.upper()
        if normalized_v not in valid_levels: raise ValueError(f"Invalid LOG_LEVEL '{v}'. Must be one of {valid_levels}")
        return normalized_v

    # <<< REMOVED/SIMPLIFIED EMBEDDING VALIDATOR >>>
    # Ya no validamos la dimensión contra OpenAI, asumimos que el valor
    # de INGEST_EMBEDDING_DIMENSION en el ConfigMap es correcto para el
    # INGEST_FASTEMBED_MODEL seleccionado.
    @field_validator('EMBEDDING_DIMENSION')
    @classmethod
    def check_embedding_dimension(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("EMBEDDING_DIMENSION must be a positive integer.")
        # Podríamos añadir una validación más específica si conocemos las dimensiones de los modelos FastEmbed soportados
        # Ejemplo: if settings.FASTEMBED_MODEL == "BAAI/bge-large-en-v1.5" and v != 1024: log warning...
        logging.debug(f"Using EMBEDDING_DIMENSION: {v}")
        return v

    # Validador de secretos (modificado para hacer opcional OPENAI_API_KEY)
    @field_validator('POSTGRES_PASSWORD', 'MINIO_ACCESS_KEY', 'MINIO_SECRET_KEY', mode='before')
    @classmethod
    def check_required_secret_value_present(cls, v: Any, info: ValidationInfo) -> Any:
        # (sin cambios, aplica a los secretos requeridos)
        if v is None or v == "":
             field_name = info.field_name if info.field_name else "Unknown Secret Field"
             raise ValueError(f"Required secret field '{field_name}' cannot be empty.")
        return v

    @field_validator('MILVUS_URI', mode='before')
    @classmethod
    def validate_milvus_uri(cls, v: str) -> str:
        # (sin cambios)
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
    temp_log.info(f"  POSTGRES_PASSWORD:        {'*** SET ***' if settings.POSTGRES_PASSWORD else '!!! NOT SET !!!'}")
    temp_log.info(f"  MILVUS_URI:               {settings.MILVUS_URI}")
    temp_log.info(f"  MILVUS_COLLECTION_NAME:   {settings.MILVUS_COLLECTION_NAME}")
    temp_log.info(f"  MINIO_ENDPOINT:           {settings.MINIO_ENDPOINT}")
    temp_log.info(f"  MINIO_BUCKET_NAME:        {settings.MINIO_BUCKET_NAME}")
    temp_log.info(f"  MINIO_ACCESS_KEY:         {'*** SET ***' if settings.MINIO_ACCESS_KEY else '!!! NOT SET !!!'}")
    temp_log.info(f"  MINIO_SECRET_KEY:         {'*** SET ***' if settings.MINIO_SECRET_KEY else '!!! NOT SET !!!'}")
    # temp_log.info(f"  OPENAI_API_KEY:           {'*** SET ***' if settings.OPENAI_API_KEY else '--- NOT SET ---'}") # Comentado, menos relevante
    temp_log.info(f"  FASTEMBED_MODEL:          {settings.FASTEMBED_MODEL}")
    temp_log.info(f"  USE_GPU:                  {settings.USE_GPU}")
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

## File: `app\db\postgres_client.py`
```py
# ingest-service/app/db/postgres_client.py
import uuid
from typing import Any, Optional, Dict, List, Tuple
import asyncpg
import structlog
import json
from datetime import datetime, timezone

# --- Synchronous Imports (Added for Step 4.3) ---
from sqlalchemy import create_engine, text, Engine, Connection
from sqlalchemy.exc import SQLAlchemyError
# --- End Synchronous Imports ---

from app.core.config import settings
from app.models.domain import DocumentStatus

log = structlog.get_logger(__name__)

# --- Async Pool (for API) ---
_pool: Optional[asyncpg.Pool] = None

# --- Sync Engine (for Worker) ---
_sync_engine: Optional[Engine] = None
_sync_engine_dsn: Optional[str] = None


# --- Async Pool Management (No changes) ---
# LLM_FLAG: SENSITIVE_CODE_BLOCK_START - DB Async Pool Management
async def get_db_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None or _pool._closed:
        log.info(
            "Creating PostgreSQL connection pool...",
            host=settings.POSTGRES_SERVER,
            port=settings.POSTGRES_PORT,
            user=settings.POSTGRES_USER,
            db=settings.POSTGRES_DB
        )
        try:
            def _json_encoder(value):
                return json.dumps(value)

            def _json_decoder(value):
                return json.loads(value)

            async def init_connection(conn):
                await conn.set_type_codec(
                    'jsonb',
                    encoder=_json_encoder,
                    decoder=_json_decoder,
                    schema='pg_catalog',
                    format='text'
                )
                await conn.set_type_codec(
                    'json',
                    encoder=_json_encoder,
                    decoder=_json_decoder,
                    schema='pg_catalog',
                    format='text'
                )

            _pool = await asyncpg.create_pool(
                user=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD.get_secret_value(),
                database=settings.POSTGRES_DB,
                host=settings.POSTGRES_SERVER,
                port=settings.POSTGRES_PORT,
                min_size=2,
                max_size=10,
                timeout=30.0,
                command_timeout=60.0,
                init=init_connection,
                statement_cache_size=0
            )
            log.info("PostgreSQL async connection pool created successfully.")
        except (asyncpg.exceptions.InvalidPasswordError, OSError, ConnectionRefusedError) as conn_err:
            log.critical(
                "CRITICAL: Failed to connect to PostgreSQL (async pool)",
                error=str(conn_err),
                exc_info=True
            )
            _pool = None
            raise ConnectionError(f"Failed to connect to PostgreSQL (async pool): {conn_err}") from conn_err
        except Exception as e:
            log.critical(
                "CRITICAL: Failed to create PostgreSQL async connection pool",
                error=str(e),
                exc_info=True
            )
            _pool = None
            raise RuntimeError(f"Failed to create PostgreSQL async pool: {e}") from e
    return _pool

async def close_db_pool():
    global _pool
    if _pool and not _pool._closed:
        log.info("Closing PostgreSQL async connection pool...")
        await _pool.close()
        _pool = None
        log.info("PostgreSQL async connection pool closed.")
    elif _pool and _pool._closed:
        log.warning("Attempted to close an already closed PostgreSQL async pool.")
        _pool = None
    else:
        log.info("No active PostgreSQL async connection pool to close.")

async def check_db_connection() -> bool:
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                result = await conn.fetchval("SELECT 1")
        return result == 1
    except Exception as e:
        log.error("Database async connection check failed", error=str(e))
        return False
# LLM_FLAG: SENSITIVE_CODE_BLOCK_END - DB Async Pool Management


# --- Synchronous Engine Management (Added for Step 4.3) ---
# LLM_FLAG: SENSITIVE_CODE_BLOCK_START - DB Sync Engine Management
def get_sync_engine() -> Engine:
    """
    Creates and returns a SQLAlchemy synchronous engine instance.
    Caches the engine globally per process.
    Requires `sqlalchemy` and `psycopg2-binary` to be installed.
    """
    global _sync_engine, _sync_engine_dsn
    sync_log = log.bind(component="SyncEngine")

    # Construct DSN only once or if settings changed (not really possible here, but good practice)
    if not _sync_engine_dsn:
         _sync_engine_dsn = f"postgresql+psycopg2://{settings.POSTGRES_USER}:{settings.POSTGRES_PASSWORD.get_secret_value()}@{settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}"

    if _sync_engine is None:
        sync_log.info("Creating SQLAlchemy synchronous engine...")
        try:
            # Example pool settings for sync engine (adjust based on worker concurrency)
            # `pool_size` should ideally match or be slightly larger than Celery worker concurrency (`-c`)
            # `max_overflow` allows temporary extra connections under load.
            _sync_engine = create_engine(
                _sync_engine_dsn,
                pool_size=5, # Example: Start with slightly more than worker -c 4
                max_overflow=5,
                pool_timeout=30, # Seconds to wait for a connection from the pool
                pool_recycle=1800 # Recycle connections older than 30 mins
                # Add connect_args for SSL if needed: connect_args={'sslmode': 'require'}
            )
            # Optional: Test connection on creation
            with _sync_engine.connect() as conn_test:
                conn_test.execute(text("SELECT 1"))
            sync_log.info("SQLAlchemy synchronous engine created and tested successfully.")
        except ImportError as ie:
            sync_log.critical("SQLAlchemy or psycopg2 not installed! Cannot create sync engine.", error=str(ie))
            raise RuntimeError("Missing SQLAlchemy/psycopg2 dependency") from ie
        except SQLAlchemyError as sa_err:
            sync_log.critical("Failed to create or connect SQLAlchemy synchronous engine", error=str(sa_err), exc_info=True)
            _sync_engine = None # Ensure it's None on failure
            raise ConnectionError(f"Failed to connect sync engine: {sa_err}") from sa_err
        except Exception as e:
             sync_log.critical("Unexpected error creating SQLAlchemy synchronous engine", error=str(e), exc_info=True)
             _sync_engine = None
             raise RuntimeError(f"Unexpected error creating sync engine: {e}") from e

    return _sync_engine

def dispose_sync_engine():
    """Disposes of the synchronous engine pool."""
    global _sync_engine
    sync_log = log.bind(component="SyncEngine")
    if _sync_engine:
        sync_log.info("Disposing SQLAlchemy synchronous engine pool...")
        _sync_engine.dispose()
        _sync_engine = None
        sync_log.info("SQLAlchemy synchronous engine pool disposed.")
# LLM_FLAG: SENSITIVE_CODE_BLOCK_END - DB Sync Engine Management


# --- Synchronous Document Operations (Added for Step 4.3) ---
# LLM_FLAG: FUNCTIONAL_CODE - Synchronous DB update function
def set_status_sync(
    engine: Engine,
    document_id: uuid.UUID,
    status: DocumentStatus,
    chunk_count: Optional[int] = None,
    error_message: Optional[str] = None
) -> bool:
    """
    Synchronously updates the status, chunk_count, and/or error_message of a document.
    Uses SQLAlchemy Core API with the provided synchronous engine.
    """
    update_log = log.bind(document_id=str(document_id), new_status=status.value, component="SyncDBUpdate")
    update_log.debug("Attempting synchronous DB status update.")

    params: Dict[str, Any] = {
        "doc_id": document_id,
        "status": status.value,
        "updated_at": datetime.now(timezone.utc)
    }
    set_clauses: List[str] = ["status = :status", "updated_at = :updated_at"]

    if chunk_count is not None:
        set_clauses.append("chunk_count = :chunk_count")
        params["chunk_count"] = chunk_count

    # Explicitly set error message based on status
    if status == DocumentStatus.ERROR:
        set_clauses.append("error_message = :error_message")
        params["error_message"] = error_message
    else:
        # Clear error message if status is not ERROR
        set_clauses.append("error_message = NULL")
        # params["error_message"] = None # Not needed if using NULL directly

    set_clause_str = ", ".join(set_clauses)
    query = text(f"UPDATE documents SET {set_clause_str} WHERE id = :doc_id")

    try:
        with engine.connect() as connection:
            with connection.begin(): # Start transaction
                result = connection.execute(query, params)

            if result.rowcount == 0:
                update_log.warning("Attempted to update status for non-existent document_id (sync).")
                return False
            elif result.rowcount == 1:
                update_log.info("Document status updated successfully in PostgreSQL (sync).", updated_fields=set_clauses)
                return True
            else:
                 # Should not happen with WHERE id = :doc_id
                 update_log.error("Unexpected number of rows updated (sync).", row_count=result.rowcount)
                 return False # Or raise an error?

    except SQLAlchemyError as e:
        update_log.error("SQLAlchemyError during synchronous status update", error=str(e), query=str(query), params=params, exc_info=True)
        # Depending on the error, this might indicate a connection issue or SQL error.
        # Re-raise as a standard Exception to be caught by Celery's retry logic if applicable.
        raise Exception(f"Sync DB update failed: {e}") from e
    except Exception as e:
        update_log.error("Unexpected error during synchronous status update", error=str(e), query=str(query), params=params, exc_info=True)
        raise Exception(f"Unexpected sync DB update error: {e}") from e


# --- Async Document Operations (No changes needed for these) ---

# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH create_document_record DB logic lightly
async def create_document_record(
    conn: asyncpg.Connection,
    doc_id: uuid.UUID,
    company_id: uuid.UUID,
    # user_id: uuid.UUID,  # REMOVED Parameter
    filename: str,
    file_type: str,
    file_path: str,
    status: DocumentStatus = DocumentStatus.PENDING,
    metadata: Optional[Dict[str, Any]] = None
) -> None:
    """Crea un registro inicial para un documento en la base de datos."""
    query = """
    INSERT INTO documents (
        id, company_id, file_name, file_type, file_path,
        metadata, status, chunk_count, error_message,
        uploaded_at, updated_at
    ) VALUES (
        $1, $2, $3, $4, $5,
        $6, $7, $8, NULL,
        NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC'
    );
    """
    metadata_db = json.dumps(metadata) if metadata else None
    params = [ doc_id, company_id, filename, file_type, file_path, metadata_db, status.value, 0 ]
    insert_log = log.bind(company_id=str(company_id), filename=filename, doc_id=str(doc_id))
    try:
        await conn.execute(query, *params)
        insert_log.info("Document record created in PostgreSQL (async)")
    except asyncpg.exceptions.UndefinedColumnError as col_err:
         insert_log.critical(f"FATAL DB SCHEMA ERROR: Column missing in 'documents' table.", error=str(col_err), table_schema_expected="id, company_id, file_name, file_type, file_path, metadata, status, chunk_count, error_message, uploaded_at, updated_at")
         raise RuntimeError(f"Database schema error: {col_err}") from col_err
    except Exception as e:
        insert_log.error("Failed to create document record (async)", error=str(e), exc_info=True)
        raise

# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH find_document_by_name_and_company DB logic lightly
async def find_document_by_name_and_company(conn: asyncpg.Connection, filename: str, company_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """Busca un documento por nombre y compañía."""
    query = "SELECT id, status FROM documents WHERE file_name = $1 AND company_id = $2;"
    find_log = log.bind(company_id=str(company_id), filename=filename)
    try:
        record = await conn.fetchrow(query, filename, company_id)
        if record: find_log.debug("Found existing document record (async)"); return dict(record)
        else: find_log.debug("No existing document record found (async)"); return None
    except Exception as e: find_log.error("Failed to find document by name and company (async)", error=str(e), exc_info=True); raise

# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH update_document_status DB logic lightly
async def update_document_status(
    document_id: uuid.UUID,
    status: DocumentStatus,
    pool: Optional[asyncpg.Pool] = None,
    conn: Optional[asyncpg.Connection] = None,
    chunk_count: Optional[int] = None,
    error_message: Optional[str] = None
) -> bool:
    """Actualiza el estado, chunk_count y/o error_message de un documento (Async)."""
    if not pool and not conn:
        pool = await get_db_pool()

    params: List[Any] = [document_id]
    fields: List[str] = ["status = $2", "updated_at = NOW() AT TIME ZONE 'UTC'"]
    params.append(status.value)
    param_index = 3

    if chunk_count is not None:
        fields.append(f"chunk_count = ${param_index}")
        params.append(chunk_count)
        param_index += 1

    if status == DocumentStatus.ERROR:
        if error_message is not None:
            fields.append(f"error_message = ${param_index}")
            params.append(error_message)
            param_index += 1
    else:
        fields.append("error_message = NULL")

    set_clause = ", ".join(fields)
    query = f"UPDATE documents SET {set_clause} WHERE id = $1;"

    update_log = log.bind(document_id=str(document_id), new_status=status.value)

    try:
        if conn:
            result = await conn.execute(query, *params)
        else:
            async with pool.acquire() as connection:
                result = await connection.execute(query, *params)

        if result == 'UPDATE 0':
            update_log.warning("Attempted to update status for non-existent document_id (async)")
            return False

        update_log.info("Document status updated in PostgreSQL (async)")
        return True

    except Exception as e:
        update_log.error("Failed to update document status (async)", error=str(e), exc_info=True)
        raise

# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH get_document_by_id DB logic lightly
async def get_document_by_id(conn: asyncpg.Connection, doc_id: uuid.UUID, company_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """Obtiene un documento por ID y verifica la compañía (Async)."""
    query = """SELECT id, company_id, file_name, file_type, file_path, metadata, status, chunk_count, error_message, uploaded_at, updated_at FROM documents WHERE id = $1 AND company_id = $2;"""
    get_log = log.bind(document_id=str(doc_id), company_id=str(company_id))
    try:
        record = await conn.fetchrow(query, doc_id, company_id)
        if not record: get_log.warning("Document not found or company mismatch (async)"); return None
        return dict(record)
    except Exception as e: get_log.error("Failed to get document by ID (async)", error=str(e), exc_info=True); raise

# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH list_documents_paginated DB logic lightly
async def list_documents_paginated(conn: asyncpg.Connection, company_id: uuid.UUID, limit: int, offset: int) -> Tuple[List[Dict[str, Any]], int]:
    """Lista documentos paginados para una compañía y devuelve el conteo total (Async)."""
    query = """SELECT id, company_id, file_name, file_type, file_path, metadata, status, chunk_count, error_message, uploaded_at, updated_at, COUNT(*) OVER() AS total_count FROM documents WHERE company_id = $1 ORDER BY updated_at DESC LIMIT $2 OFFSET $3;"""
    list_log = log.bind(company_id=str(company_id), limit=limit, offset=offset)
    try:
        rows = await conn.fetch(query, company_id, limit, offset); total = 0; results = []
        if rows: total = rows[0]['total_count']; results = [dict(r) for r in rows]; [r.pop('total_count', None) for r in results]
        list_log.debug("Fetched paginated documents (async)", count=len(results), total=total)
        return results, total
    except Exception as e: list_log.error("Failed to list paginated documents (async)", error=str(e), exc_info=True); raise

# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH delete_document DB logic lightly
async def delete_document(conn: asyncpg.Connection, doc_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    """Elimina un documento verificando la compañía (Async)."""
    query = "DELETE FROM documents WHERE id = $1 AND company_id = $2 RETURNING id;"
    delete_log = log.bind(document_id=str(doc_id), company_id=str(company_id))
    try:
        deleted_id = await conn.fetchval(query, doc_id, company_id)
        if deleted_id: delete_log.info("Document deleted from PostgreSQL (async)", deleted_id=str(deleted_id)); return True
        else: delete_log.warning("Document not found or company mismatch during delete attempt (async)."); return False
    except Exception as e: delete_log.error("Error deleting document record (async)", error=str(e), exc_info=True); raise

# --- Chat Functions (Placeholder - Likely Unused, No Changes Needed) ---
# LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Chat Functions (Likely Unused)
async def create_chat(user_id: uuid.UUID, company_id: uuid.UUID, title: Optional[str] = None) -> uuid.UUID:
    pool = await get_db_pool()
    chat_id = uuid.uuid4()
    query = """
    INSERT INTO chats (id, user_id, company_id, title, created_at, updated_at)
    VALUES ($1, $2, $3, $4, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC')
    RETURNING id;
    """
    try:
        async with pool.acquire() as conn:
            result = await conn.fetchval(query, chat_id, user_id, company_id, title or f"Chat {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}")
            return result
    except Exception as e:
        log.error("Failed create_chat (ingest context - likely unused)", error=str(e))
        raise

async def get_user_chats(user_id: uuid.UUID, company_id: uuid.UUID, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    pool = await get_db_pool()
    query = """
    SELECT id, title, updated_at
    FROM chats
    WHERE user_id = $1 AND company_id = $2
    ORDER BY updated_at DESC
    LIMIT $3 OFFSET $4;
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, user_id, company_id, limit, offset)
            return [dict(row) for row in rows]
    except Exception as e:
        log.error("Failed get_user_chats (ingest context - likely unused)", error=str(e))
        raise

async def check_chat_ownership(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    pool = await get_db_pool()
    query = "SELECT EXISTS (SELECT 1 FROM chats WHERE id = $1 AND user_id = $2 AND company_id = $3);"
    try:
        async with pool.acquire() as conn:
            exists = await conn.fetchval(query, chat_id, user_id, company_id)
            return exists is True
    except Exception as e:
        log.error("Failed check_chat_ownership (ingest context - likely unused)", error=str(e))
        return False

async def get_chat_messages(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    pool = await get_db_pool()
    owner = await check_chat_ownership(chat_id, user_id, company_id)
    if not owner:
        return []
    messages_query = """
    SELECT id, role, content, sources, created_at
    FROM messages
    WHERE chat_id = $1
    ORDER BY created_at ASC
    LIMIT $2 OFFSET $3;
    """
    try:
        async with pool.acquire() as conn:
            message_rows = await conn.fetch(messages_query, chat_id, limit, offset)
            return [dict(row) for row in message_rows]
    except Exception as e:
        log.error("Failed get_chat_messages (ingest context - likely unused)", error=str(e))
        raise

async def save_message(chat_id: uuid.UUID, role: str, content: str, sources: Optional[List[Dict[str, Any]]] = None) -> uuid.UUID:
    pool = await get_db_pool()
    message_id = uuid.uuid4()
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                update_chat_query = "UPDATE chats SET updated_at = NOW() AT TIME ZONE 'UTC' WHERE id = $1 RETURNING id;"
                chat_updated = await conn.fetchval(update_chat_query, chat_id)
                if not chat_updated:
                    raise ValueError(f"Chat {chat_id} not found (ingest context - likely unused).")
                insert_message_query = """
                INSERT INTO messages (id, chat_id, role, content, sources, created_at)
                VALUES ($1, $2, $3, $4, $5, NOW() AT TIME ZONE 'UTC')
                RETURNING id;
                """
                result = await conn.fetchval(insert_message_query, message_id, chat_id, role, content, json.dumps(sources or []))
                return result
            except Exception as e:
                log.error("Failed save_message (ingest context - likely unused)", error=str(e))
                raise

async def delete_chat(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    pool = await get_db_pool()
    query = "DELETE FROM chats WHERE id = $1 AND user_id = $2 AND company_id = $3 RETURNING id;"
    delete_log = log.bind(chat_id=str(chat_id), user_id=str(user_id))
    try:
        async with pool.acquire() as conn:
            deleted_id = await conn.fetchval(query, chat_id, user_id, company_id)
            return deleted_id is not None
    except Exception as e:
        delete_log.error("Failed to delete chat (ingest context - likely unused)", error=str(e))
        raise
# LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Chat Functions
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
    PENDING = "pending"
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
from typing import IO, BinaryIO, Optional
from minio import Minio
from minio.error import S3Error
import structlog
import asyncio

from app.core.config import settings

log = structlog.get_logger(__name__)

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
        self._endpoint = endpoint or settings.MINIO_ENDPOINT
        self._access_key = access_key or settings.MINIO_ACCESS_KEY.get_secret_value()
        self._secret_key = secret_key or settings.MINIO_SECRET_KEY.get_secret_value()
        self.bucket_name = bucket_name or settings.MINIO_BUCKET_NAME
        self._secure = secure if secure is not None else settings.MINIO_USE_SECURE

        self._client: Optional[Minio] = None
        self.log = log.bind(minio_endpoint=self._endpoint, bucket_name=self.bucket_name)
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
            self._ensure_bucket_exists_sync()
            self.log.info("MinIO client initialized successfully.")
        except (S3Error, TypeError, ValueError) as e:
            self.log.critical("CRITICAL: Failed to initialize MinIO client", error=str(e), exc_info=True)
            raise RuntimeError(f"MinIO client initialization failed: {e}") from e

    def _get_client(self) -> Minio:
        """Returns the initialized MinIO client, raising error if not available."""
        if self._client is None:
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

    # --- Asynchronous Methods (Used by API) ---

    async def upload_file_async(
        self,
        object_name: str,
        data: bytes,
        content_type: str
    ) -> str:
        """Uploads file content to MinIO asynchronously using run_in_executor."""
        upload_log = self.log.bind(bucket=self.bucket_name, object_name=object_name, content_type=content_type, length=len(data))
        upload_log.info("Queueing file upload to MinIO executor")
        loop = asyncio.get_running_loop()
        client = self._get_client()

        def _upload_sync():
            data_stream = io.BytesIO(data)
            try:
                 result = client.put_object(
                    bucket_name=self.bucket_name, object_name=object_name,
                    data=data_stream, length=len(data), content_type=content_type
                )
                 upload_log.debug("MinIO put_object successful (sync part)", etag=getattr(result, 'etag', 'N/A'), version_id=getattr(result, 'version_id', 'N/A'))
                 return object_name
            except S3Error as e:
                 upload_log.error("S3Error during MinIO upload (sync part)", error_code=getattr(e, 'code', 'Unknown'), error_details=str(e))
                 raise MinioError(f"S3 error uploading {object_name}", e) from e
            except Exception as e:
                 upload_log.exception("Unexpected error during MinIO upload (sync part)", error=str(e))
                 raise MinioError(f"Unexpected error uploading {object_name}", e) from e
        try:
            uploaded_object_name = await loop.run_in_executor(None, _upload_sync)
            upload_log.info("File uploaded successfully to MinIO via executor")
            return uploaded_object_name
        except MinioError as me:
            upload_log.error("Upload failed via executor", error=str(me))
            raise me
        except Exception as e:
             upload_log.exception("Unexpected executor error during upload", error=str(e))
             raise MinioError("Unexpected executor error during upload", e) from e

    async def download_file(self, object_name: str, file_path: str):
        """Downloads a file from MinIO to a local path asynchronously."""
        download_log = self.log.bind(bucket=self.bucket_name, object_name=object_name, target_path=file_path)
        download_log.info("Queueing file download from MinIO executor")
        loop = asyncio.get_running_loop()
        try:
            # Note: Uses the synchronous download method internally via executor
            await loop.run_in_executor(None, self.download_file_sync, object_name, file_path)
            download_log.info("File download successful via executor")
        except MinioError as me:
            download_log.error("Download failed via executor", error=str(me))
            raise me
        except Exception as e:
            download_log.exception("Unexpected executor error during download", error=str(e))
            raise MinioError("Unexpected executor error during download", e) from e

    async def check_file_exists_async(self, object_name: str) -> bool:
        """Checks if a file exists in MinIO asynchronously."""
        check_log = self.log.bind(bucket=self.bucket_name, object_name=object_name)
        check_log.debug("Queueing file existence check in executor")
        loop = asyncio.get_running_loop()
        try:
             # Note: Uses the synchronous check method internally via executor
            exists = await loop.run_in_executor(None, self.check_file_exists_sync, object_name)
            check_log.debug("File existence check completed via executor", exists=exists)
            return exists
        except MinioError as me:
            if "Object not found" in str(me): return False
            check_log.error("Existence check failed via executor", error=str(me))
            return False
        except Exception as e:
            check_log.exception("Unexpected executor error during existence check", error=str(e))
            return False

    async def delete_file_async(self, object_name: str) -> None:
        """Deletes a file from MinIO asynchronously."""
        delete_log = self.log.bind(bucket=self.bucket_name, object_name=object_name)
        delete_log.info("Queueing file deletion from MinIO executor")
        loop = asyncio.get_running_loop()
        try:
            # Note: Uses the synchronous delete method internally via executor
            await loop.run_in_executor(None, self.delete_file_sync, object_name)
            delete_log.info("File deletion successful via executor")
        except Exception as e:
            delete_log.exception("Unexpected executor error during deletion", error=str(e))
            # Log but don't raise to allow main delete flow to continue

    # --- Synchronous Methods (Used by Worker) ---

    def download_file_sync(self, object_name: str, file_path: str):
        """Synchronously downloads a file from MinIO to a local path."""
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

    def check_file_exists_sync(self, object_name: str) -> bool:
        """Synchronously checks if a file exists in MinIO."""
        check_log = self.log.bind(bucket=self.bucket_name, object_name=object_name)
        client = self._get_client()
        try:
            client.stat_object(self.bucket_name, object_name)
            check_log.debug("Object exists in MinIO (sync check).")
            return True
        except S3Error as e:
            if getattr(e, 'code', None) in ('NoSuchKey', 'NoSuchBucket'):
                check_log.debug("Object not found in MinIO (sync check)", code=e.code)
                return False
            check_log.error("S3Error checking object existence (sync)", error_code=getattr(e, 'code', 'Unknown'), error_details=str(e))
            raise MinioError(f"S3 error checking existence for {object_name}", e) from e
        except Exception as e:
             check_log.exception("Unexpected error checking object existence (sync)", error=str(e))
             raise MinioError(f"Unexpected error checking existence for {object_name}", e) from e

    def delete_file_sync(self, object_name: str):
        """Synchronously deletes a file from MinIO."""
        delete_log = self.log.bind(bucket=self.bucket_name, object_name=object_name)
        delete_log.info("Deleting file from MinIO (sync operation)...")
        client = self._get_client()
        try:
            client.remove_object(self.bucket_name, object_name)
            delete_log.info("File deleted successfully from MinIO (sync)")
        except S3Error as e:
            delete_log.error("S3Error deleting file (sync)", error_code=getattr(e, 'code', 'Unknown'), error_details=str(e))
            # Allow process to continue for partial deletion scenarios
        except Exception as e:
            delete_log.exception("Unexpected error during sync file deletion", error=str(e))
            # Allow process to continue
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
import os
import tempfile
import uuid
import sys
from typing import Optional, Dict, Any, List, Tuple, Type

import structlog
from celery import Task
from celery.exceptions import Ignore, Reject, MaxRetriesExceededError
from celery.utils.log import get_task_logger # Use Celery's task logger

# --- Haystack Dependencies ---
from haystack.dataclasses import Document
from haystack.document_stores.types import DuplicatePolicy
from haystack.components.converters import (
    PyPDFToDocument, MarkdownToDocument, HTMLToDocument, TextFileToDocument, DOCXToDocument
)
from haystack.components.preprocessors import DocumentSplitter
from haystack.components.writers import DocumentWriter
from haystack.utils import ComponentDevice

# --- Milvus Dependencies ---
from milvus_haystack import MilvusDocumentStore

# --- FastEmbed Dependencies ---
from haystack_integrations.components.embedders.fastembed import FastembedDocumentEmbedder

# --- Custom Application Imports ---
from app.core.config import settings
# Import the specific sync functions needed
from app.db.postgres_client import get_sync_engine, set_status_sync
from app.models.domain import DocumentStatus
from app.services.minio_client import MinioClient, MinioError
from app.tasks.celery_app import celery_app

# --- Synchronous Database Client Engine ---
try:
    # Get the engine once when the module loads
    sync_engine = get_sync_engine()
except Exception as db_init_err:
    # Log critical error if engine fails to initialize
    structlog.get_logger(__name__).critical(
        "Failed to initialize synchronous DB engine! Worker tasks depending on DB will fail.",
        error=str(db_init_err),
        exc_info=True
    )
    sync_engine = None


# --------------------------------------------------------------------------
# Global Resource Initialization (Executed once per worker process)
# --------------------------------------------------------------------------
log = structlog.get_logger(__name__) # Module-level logger

# Initialize resources in a try-except block to handle potential init failures
minio_client = None
milvus_store = None
splitter = None
embedder = None
writer = None
CONVERTERS = {}

try:
    log.info("Initializing global resources for Celery worker process...")

    # 1. MinIO Client (uses sync methods internally)
    minio_client = MinioClient()
    log.info("Global MinIO client initialized.")

    # 2. Milvus Document Store
    milvus_connection_args = {"uri": settings.MILVUS_URI}
    if settings.MILVUS_URI.lower().startswith("https"):
        milvus_connection_args["secure"] = True

    # NOTE: Assuming grpc_timeout is handled via pymilvus config or env vars if needed,
    # as it's not a direct constructor arg in recent milvus-haystack versions.
    milvus_store = MilvusDocumentStore(
        connection_args=milvus_connection_args,
        collection_name=settings.MILVUS_COLLECTION_NAME,
        embedding_dim=settings.EMBEDDING_DIMENSION,
        consistency_level="Strong",
    )
    log.info("Global MilvusDocumentStore initialized.",
             uri_scheme=settings.MILVUS_URI.split(':')[0], # Log scheme only
             collection=settings.MILVUS_COLLECTION_NAME,
             embedding_dim=settings.EMBEDDING_DIMENSION,
             consistency="Strong")

    # 3. Haystack Components
    log.info("Initializing global Haystack components...")
    splitter = DocumentSplitter(
        split_by=settings.SPLITTER_SPLIT_BY,
        split_length=settings.SPLITTER_CHUNK_SIZE,
        split_overlap=settings.SPLITTER_CHUNK_OVERLAP
    )
    log.info("Global DocumentSplitter initialized.",
             split_by=settings.SPLITTER_SPLIT_BY,
             length=settings.SPLITTER_CHUNK_SIZE,
             overlap=settings.SPLITTER_CHUNK_OVERLAP)

    # Determine device for FastEmbed
    if settings.USE_GPU:
        try:
            embedder_device = ComponentDevice.from_str("cuda:0")
            log.info("GPU selected for FastEmbed based on settings.")
        except Exception as gpu_err:
            log.warning("GPU configured but FAILED to select, falling back to CPU.", error=str(gpu_err), setting_use_gpu=settings.USE_GPU)
            embedder_device = ComponentDevice.from_str("cpu")
    else:
        embedder_device = ComponentDevice.from_str("cpu")
        log.info("CPU selected for FastEmbed (USE_GPU is false).")

    embedder = FastembedDocumentEmbedder(
        model=settings.FASTEMBED_MODEL,
        device=embedder_device,
        batch_size=256,
        parallel=0 if embedder_device.type == "cpu" else None
    )
    log.info("Global FastembedDocumentEmbedder initialized.", model=settings.FASTEMBED_MODEL, device=str(embedder_device))

    log.info("Warming up FastEmbed model...")
    embedder.warm_up()
    log.info("Global FastEmbed model warmed up successfully.")

    writer = DocumentWriter(
        document_store=milvus_store,
        policy=DuplicatePolicy.OVERWRITE,
        batch_size=512
    )
    log.info("Global DocumentWriter initialized.", policy="OVERWRITE", batch_size=512)

    # 4. Converters Mapping
    CONVERTERS = {
        "application/pdf": PyPDFToDocument,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DOCXToDocument,
        "application/msword": DOCXToDocument,
        "text/plain": TextFileToDocument,
        "text/markdown": MarkdownToDocument,
        "text/html": HTMLToDocument,
    }
    log.info("Global Converters mapping created.")

    log.info("Global resources for Celery worker process initialized successfully.")

except Exception as e:
    log.critical("CRITICAL: Failed to initialize global resources in worker process!", error=str(e), exc_info=True)
    # Set flags or raise an error to prevent tasks from running if core components fail
    # For simplicity, we let tasks fail if resources are None, logged below.


# --------------------------------------------------------------------------
# Synchronous Celery Task Definition
# --------------------------------------------------------------------------
@celery_app.task(
    bind=True,
    name="ingest.process_document", # Use the new explicit name
    autoretry_for=(Exception,), # Retry on most exceptions
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3
)
def process_document_sync(self: Task, *args, **kwargs) -> Dict[str, Any]:
    """
    Synchronous Celery task to process a single document.
    Uses globally initialized resources (MinIO, Milvus, Haystack components).
    Updates document status in PostgreSQL synchronously.
    """
    document_id_str = kwargs.get('document_id')
    company_id_str = kwargs.get('company_id')
    filename = kwargs.get('filename')
    content_type = kwargs.get('content_type')

    # Use Celery's logger for task-specific context
    task_log = get_task_logger(f"{__name__}.{document_id_str}")
    task_id = self.request.id or "unknown_task_id"
    attempt = self.request.retries + 1
    task_log.info(f"Starting sync processing: doc='{filename}' task_id={task_id} [Attempt {attempt}/{self.max_retries + 1}]")

    # --- Pre-checks for required arguments and global resources ---
    if not all([document_id_str, company_id_str, filename, content_type]):
        task_log.error("Missing required arguments in task payload.", payload_kwargs=kwargs)
        raise Reject("Missing required arguments (doc_id, company_id, filename, content_type)", requeue=False)

    if not sync_engine or not minio_client or not milvus_store or not splitter or not embedder or not writer:
         task_log.critical("One or more global resources (DB, MinIO, Milvus, Haystack) are not initialized. Task cannot proceed.")
         # This indicates a setup failure in the worker process, likely non-retryable per task.
         raise Reject("Worker process global resource initialization failed.", requeue=False)

    doc_uuid = uuid.UUID(document_id_str)
    # company_uuid = uuid.UUID(company_id_str) # No longer needed if only passing strings
    object_name = f"{company_id_str}/{document_id_str}/{filename}"
    temp_file_path = None
    written_count = 0

    try:
        # 1. Update status to PROCESSING (Synchronous)
        task_log.debug("Setting status to PROCESSING in DB.")
        set_status_sync( # Use the imported sync function
            engine=sync_engine,
            document_id=doc_uuid,
            status=DocumentStatus.PROCESSING,
            error_message=None # Clear previous errors
        )
        task_log.info("Status set to PROCESSING.")

        # 2. Download file from MinIO (Synchronous)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_file_path = os.path.join(temp_dir, filename)
            task_log.info(f"Downloading MinIO file: {object_name} -> {temp_file_path}")
            minio_client.download_file_sync(object_name, temp_file_path) # Use sync method
            task_log.info("File downloaded successfully.")

            # 3. Select Haystack Converter
            ConverterClass = CONVERTERS.get(content_type)
            if not ConverterClass:
                task_log.error(f"Unsupported content type: {content_type}")
                raise Reject(f"Unsupported content type: {content_type}", requeue=False)
            converter = ConverterClass()
            task_log.debug(f"Using converter: {ConverterClass.__name__}")

            # 4. Run Haystack Pipeline (Synchronous)
            task_log.info("Starting Haystack pipeline execution...")

            # --- Conversion ---
            task_log.debug("Running conversion...")
            conversion_result = converter.run(sources=[temp_file_path])
            docs: List[Document] = conversion_result["documents"]
            task_log.info(f"Conversion complete. Found {len(docs)} initial document(s).")

            if not docs:
                task_log.warning("Converter returned no documents. Finishing task.")
                written_count = 0
            else:
                # --- Add Metadata ---
                task_log.debug("Adding standard metadata...")
                for doc in docs:
                    if doc.meta is None: doc.meta = {}
                    # Ensure metadata keys are strings and values are compatible
                    doc.meta.update({
                        "company_id": str(company_id_str), # Store as string
                        "document_id": str(document_id_str), # Store as string
                        "file_name": str(filename),
                        "file_type": str(content_type),
                    })

                # --- Splitting ---
                task_log.debug("Running splitting...")
                split_result = splitter.run(documents=docs)
                split_docs: List[Document] = split_result["documents"]
                task_log.info(f"Splitting complete. Generated {len(split_docs)} chunks.")

                if not split_docs:
                    task_log.warning("Splitter returned no chunks. Finishing task.")
                    written_count = 0
                else:
                    # --- Embedding ---
                    task_log.debug("Running embedding...")
                    embed_result = embedder.run(documents=split_docs)
                    embedded_docs: List[Document] = embed_result["documents"]
                    task_log.info(f"Embedding complete for {len(embedded_docs)} chunks.")

                    if not embedded_docs:
                       task_log.warning("Embedder returned no documents. Finishing task.")
                       written_count = 0
                    else:
                        # --- Writing ---
                        task_log.debug("Running writing to Milvus...")
                        write_result = writer.run(documents=embedded_docs)
                        # Check the actual key returned by writer.run based on Haystack version
                        # It might be "documents_written" or similar
                        if "documents_written" in write_result:
                            written_count = write_result["documents_written"]
                        elif "count" in write_result: # Fallback check
                             written_count = write_result["count"]
                        else:
                            task_log.warning("Could not determine written count from writer result", result_keys=write_result.keys())
                            written_count = len(embedded_docs) # Best guess

                        task_log.info(f"Writing complete. Wrote/Attempted {written_count} documents/chunks to Milvus.")

        # 5. Update status to PROCESSED (Synchronous)
        task_log.debug("Setting status to PROCESSED in DB.")
        set_status_sync( # Use the imported sync function
            engine=sync_engine,
            document_id=doc_uuid,
            status=DocumentStatus.PROCESSED,
            chunk_count=written_count,
            error_message=None # Clear any previous error
        )
        task_log.info(f"Document processing finished successfully. Chunks processed: {written_count}")
        # Return a dictionary suitable for Celery result backend
        return {"status": DocumentStatus.PROCESSED.value, "chunks_written": written_count}

    except Reject as r:
         # Handle non-retryable rejections explicitly
         task_log.error(f"Task rejected permanently: {r.reason}")
         try:
              set_status_sync(
                    engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR,
                    error_message=f"Rejected: {r.reason}"[:500] # Truncate
              )
         except Exception as db_final_err:
              task_log.critical("Failed to update status to ERROR after task rejection!", error=str(db_final_err))
         raise r # Re-raise Reject to notify Celery

    except Exception as exc:
        task_log.exception(f"An error occurred during document processing [Attempt {attempt}]", exc_info=True)
        error_msg = f"Attempt {attempt} failed: {type(exc).__name__} - {str(exc)[:400]}" # Truncate
        try:
            set_status_sync( # Use the imported sync function
                engine=sync_engine,
                document_id=doc_uuid,
                status=DocumentStatus.ERROR,
                error_message=error_msg
            )
            task_log.info("Set status to ERROR in DB before retrying/failing.")
        except Exception as db_final_err:
             task_log.critical("CRITICAL: Failed to update status to ERROR after processing failure!", error=str(db_final_err))
             # Log but still raise original exception for Celery retry

        # Let Celery's autoretry handle the exception
        raise exc

# Assign explicit name for easier calling if needed elsewhere (optional but good practice)
process_document_haystack_task = process_document_sync
```

## File: `pyproject.toml`
```toml
[tool.poetry]
name = "ingest-service"
version = "0.1.7" # Increment version
description = "Ingest service for Atenex B2B SaaS (Haystack/Postgres/Minio/Milvus/FastEmbed - CPU - Prefork)"
authors = ["Atenex Team <dev@atenex.com>"]
readme = "README.md"

[tool.poetry.dependencies]
python = "^3.10"
fastapi = "^0.110.0"
uvicorn = {extras = ["standard"], version = "^0.28.0"}
gunicorn = "^21.2.0"
pydantic = {extras = ["email"], version = "^2.6.4"}
pydantic-settings = "^2.2.1"
celery = {extras = ["redis"], version = "^5.3.6"}
# gevent = "^23.9.1" # REMOVED as per plan
asyncpg = "^0.29.0" # Keep for API
tenacity = "^8.2.3"
python-multipart = "^0.0.9"
structlog = "^24.1.0"
minio = "^7.1.17"

# --- Haystack Dependencies ---
haystack-ai = "^2.0.1" # Or the stable version you use for Haystack 2.x
pymilvus = "^2.4.1"
milvus-haystack = ">=0.0.16"

# --- Haystack Converter Dependencies ---
pypdf = "^4.0.1"
python-docx = "^1.1.0"
markdown = "^3.5.1"
beautifulsoup4 = "^4.12.3"

# --- FastEmbed & ONNX CPU Dependencies ---
fastembed-haystack = "^1.3.0"
onnxruntime = "^1.17.0"

# --- HTTP Client ---
httpx = {extras = ["http2"], version = "^0.27.0"}
h2 = "^4.1.0"

# --- Synchronous DB Dependencies (Added for Step 4.3) ---
sqlalchemy = "^2.0.28"
psycopg2-binary = "^2.9.9"


[tool.poetry.group.dev.dependencies]
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
