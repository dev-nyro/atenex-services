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
# LLM_FLAG: ADD_HAYSTACK_IMPORT - Needed for Document dataclass typing
from haystack.dataclasses import Document

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
# Import the task instance exported from process_document.py
from app.tasks.process_document import process_document_haystack_task

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Helper Functions ---

# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH get_minio_client dependency setup
def get_minio_client():
    """Dependency to get Minio client instance."""
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Minio Client Dependency
    try:
        client = MinioClient()
        return client
    except Exception as e:
        log.exception("Failed to initialize MinioClient dependency", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Storage service configuration error."
        )
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Minio Client Dependency

# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH api_db_retry_strategy logic
# LLM_FLAG: SENSITIVE_RETRY_LOGIC - API DB Retry Strategy
api_db_retry_strategy = retry(
    stop=stop_after_attempt(2),
    wait=wait_fixed(1),
    retry=retry_if_exception_type((asyncpg.exceptions.PostgresConnectionError, TimeoutError, OSError)),
    before_sleep=before_sleep_log(log, logging.WARNING)
)

# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH get_db_conn context manager
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


# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH Milvus Sync Helpers unless explicitly told to
def _initialize_milvus_store_sync() -> MilvusDocumentStore:
    """Synchronously initializes MilvusDocumentStore for API helpers."""
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Milvus Sync Init Helper
    api_log = log.bind(component="MilvusHelperSync")
    api_log.debug("Initializing MilvusDocumentStore for API helper...")
    try:
        store = MilvusDocumentStore(
            connection_args={"uri": settings.MILVUS_URI},
            collection_name=settings.MILVUS_COLLECTION_NAME,
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
    """Synchronously counts chunks in Milvus for a specific document using filter_documents."""
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Milvus Sync Count Helper (Corrected with filter_documents)
    count_log = log.bind(document_id=document_id, company_id=company_id, component="MilvusHelperSync")
    try:
        store = _initialize_milvus_store_sync()
        # LLM_FLAG: CRITICAL_FILTERING - Ensure company_id filter is correct
        filters = {
            "operator": "AND",
            "conditions": [
                {"field": "meta.document_id", "operator": "==", "value": document_id},
                {"field": "meta.company_id", "operator": "==", "value": company_id},
            ]
        }
        # CORRECCIÓN: aplicamos filtros con filter_documents() y contamos los resultados
        # filter_documents should return a List[Document]
        docs: List[Document] = store.filter_documents(filters=filters)
        count = len(docs)
        count_log.info("Milvus chunk count successful", count=count)
        return count
    except RuntimeError as re:
        count_log.error("Failed to get Milvus count due to store init error", error=str(re))
        return -1 # Return -1 to indicate error during count
    # Removed the specific AttributeError handler for count_documents(filters=...)
    except Exception as e:
        count_log.exception("Error filtering/counting documents in Milvus", error=str(e))
        count_log.debug("Filter used for Milvus count", filter_details=json.dumps(filters))
        return -1 # Return -1 to indicate error during count
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Milvus Sync Count Helper (Corrected with filter_documents)

def _delete_milvus_sync(document_id: str, company_id: str) -> bool:
    """Synchronously deletes chunks from Milvus for a specific document."""
     # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Milvus Sync Delete Helper
    delete_log = log.bind(document_id=document_id, company_id=company_id, component="MilvusHelperSync")
    try:
        store = _initialize_milvus_store_sync()
        # LLM_FLAG: CRITICAL_FILTERING - Ensure company_id filter is correct for delete
        filters = {
            "operator": "AND",
            "conditions": [
                {"field": "meta.document_id", "operator": "==", "value": document_id},
                {"field": "meta.company_id", "operator": "==", "value": company_id},
            ]
        }
        # Assuming delete_documents exists and works as expected
        store.delete_documents(filters=filters)
        delete_log.info("Milvus delete operation executed.")
        return True
    except RuntimeError as re:
        delete_log.error("Failed to delete Milvus chunks due to store init error", error=str(re))
        return False
    except Exception as e:
        delete_log.exception("Error deleting documents from Milvus", error=str(e))
        delete_log.debug("Filter used for Milvus delete", filter_details=json.dumps(filters))
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
        422: {"model": ErrorDetail, "description": "Validation Error (Missing Headers)"},
        500: {"model": ErrorDetail, "description": "Internal Server Error"},
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB or MinIO)"},
    }
)
async def upload_document(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    metadata_json: Optional[str] = Form(None),
    minio_client: MinioClient = Depends(get_minio_client),
):
    """
    Receives a document file and optional metadata, saves it to MinIO,
    creates a record in PostgreSQL, and queues a Celery task for processing.
    Prevents upload if a non-error document with the same name exists for the company.
    Headers X-Company-ID and X-User-ID are now read directly from the request.
    """
    company_id = request.headers.get("X-Company-ID")
    user_id = request.headers.get("X-User-ID") # user_id is read but no longer sent to DB create or Celery
    req_id = getattr(request.state, 'request_id', str(uuid.uuid4()))

    endpoint_log = log.bind(request_id=req_id)

    if not company_id:
        endpoint_log.warning("Missing X-Company-ID header")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")
    if not user_id:
        endpoint_log.warning("Missing X-User-ID header")
        # Although user_id is not directly used in core logic now, it might be needed for auth/logging later
        # Decide if it should be strictly required. For now, keeping the check as in original code.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-User-ID")

    endpoint_log = endpoint_log.bind(company_id=company_id, user_id=user_id,
                                     filename=file.filename, content_type=file.content_type)
    endpoint_log.info("Processing document ingestion request from gateway")

    # --- CODE LOGIC ---
    if file.content_type not in settings.SUPPORTED_CONTENT_TYPES:
        endpoint_log.warning("Unsupported content type received")
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=f"Unsupported file type: {file.content_type}. Supported types: {', '.join(settings.SUPPORTED_CONTENT_TYPES)}")

    metadata = {}
    if metadata_json:
        try:
            metadata = json.loads(metadata_json)
            if not isinstance(metadata, dict): raise ValueError("Metadata must be a JSON object.")
        except json.JSONDecodeError:
            endpoint_log.warning("Invalid metadata JSON format received")
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid metadata format: Must be valid JSON.")
        except ValueError as e:
             endpoint_log.warning(f"Invalid metadata content: {e}")
             raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid metadata content: {e}")

    try:
        company_uuid = uuid.UUID(company_id)
        async with get_db_conn() as conn:
            existing_doc = await api_db_retry_strategy(db_client.find_document_by_name_and_company)(
                conn=conn, filename=file.filename, company_id=company_uuid
            )
            if existing_doc and existing_doc['status'] != DocumentStatus.ERROR.value:
                 endpoint_log.warning("Duplicate document detected", document_id=existing_doc['id'], status=existing_doc['status'])
                 raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Document '{file.filename}' already exists with status '{existing_doc['status']}'. Delete it first or wait for processing.")
            elif existing_doc and existing_doc['status'] == DocumentStatus.ERROR.value:
                 endpoint_log.info("Found existing document in error state, proceeding with overwrite logic implicitly (new upload).", document_id=existing_doc['id'])
                 pass # Keep existing logic
    except ValueError:
        endpoint_log.error("Invalid Company ID format provided", company_id_received=company_id)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")
    except HTTPException as http_exc: raise http_exc
    except Exception as e:
        endpoint_log.exception("Error checking for duplicate document", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error checking for duplicates.")

    document_id = uuid.uuid4()
    # Use file_path consistently
    file_path_in_storage = f"{company_id}/{document_id}/{file.filename}"

    try:
        # Validate user_id format here, even if not stored directly in `documents` table
        # user_uuid = uuid.UUID(user_id) # Original code has this, keep if needed for audit/other logs
        async with get_db_conn() as conn:
            # Passing filename which matches the parameter name in create_document_record
            # The function itself maps it to the file_name column in the query.
            await api_db_retry_strategy(db_client.create_document_record)(
                conn=conn,
                doc_id=document_id,
                company_id=company_uuid,
                filename=file.filename, # Parameter name is filename
                file_type=file.content_type,
                file_path=file_path_in_storage,
                status=DocumentStatus.PENDING, # Pass Enum
                metadata=metadata
            )
        endpoint_log.info("Document record created in PostgreSQL", document_id=str(document_id))
    # except ValueError: # Uncomment if user_uuid validation is kept
    #     endpoint_log.error("Invalid User ID format provided", user_id_received=user_id)
    #     raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid User ID format.")
    except asyncpg.exceptions.UndefinedColumnError as col_err: # Catch specific DB error if schema isn't right
        endpoint_log.critical("Database schema error during document creation", error=str(col_err), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error: Database schema mismatch.")
    except Exception as e:
        endpoint_log.exception("Failed to create document record in PostgreSQL", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error creating record.")

    try:
        file_content = await file.read()
        # Use file_path_in_storage consistently
        await minio_client.upload_file_async(
            object_name=file_path_in_storage, data=file_content, content_type=file.content_type
        )
        endpoint_log.info("File uploaded successfully to MinIO", object_name=file_path_in_storage)

        # >>>>>>>>>>> CORRECTION APPLIED (Pass Enum Member) <<<<<<<<<<<<<<
        # Update status to UPLOADED after successful MinIO upload
        async with get_db_conn() as conn:
            await api_db_retry_strategy(db_client.update_document_status)(
                conn=conn,
                document_id=document_id,
                status=DocumentStatus.UPLOADED # Pass Enum
            )
        endpoint_log.info("Document status updated to 'uploaded'", document_id=str(document_id))
        # >>>>>>>>>>> END CORRECTION <<<<<<<<<<<<<<

    # >>>>>>>>>>> CORRECTION APPLIED (Pass Enum Member) <<<<<<<<<<<<<<
    except MinioError as me:
        endpoint_log.error("Failed to upload file to MinIO", object_name=file_path_in_storage, error=str(me))
        try:
            # Update DB status to ERROR if MinIO fails, passing conn and using retry
            async with get_db_conn() as conn:
                await api_db_retry_strategy(db_client.update_document_status)(
                    conn=conn,
                    document_id=document_id,
                    status=DocumentStatus.ERROR,   # Pass Enum
                    error_message=f"MinIO upload failed: {me}"
                )
        except Exception as db_err:
            endpoint_log.exception("Failed to update status to ERROR after MinIO failure", error=str(db_err))
        # Raise original exception details
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Storage service error: {me}"
        )
    # >>>>>>>>>>> END CORRECTION <<<<<<<<<<<<<<

    # >>>>>>>>>>> CORRECTION APPLIED (Pass Enum Member) <<<<<<<<<<<<<<
    except Exception as e: # Handle other unexpected errors during upload/DB update
         endpoint_log.exception("Unexpected error during file upload or DB update", error=str(e))
         try:
             # Update DB status to ERROR, passing conn and using retry
             async with get_db_conn() as conn:
                 await api_db_retry_strategy(db_client.update_document_status)(
                     conn=conn,
                     document_id=document_id,
                     status=DocumentStatus.ERROR, # Pass Enum
                     error_message=f"Unexpected upload error: {e}"
                 )
         except Exception as db_err:
             endpoint_log.exception("Failed to update status to ERROR after unexpected upload failure", error=str(db_err))
         raise HTTPException(
             status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
             detail=f"Internal server error during upload: {e}"
         )
    # >>>>>>>>>>> END CORRECTION <<<<<<<<<<<<<<

    finally: await file.close()

    try:
        # --- REMOVED user_id from task payload ---
        task_payload = {
            "document_id": str(document_id),
            "company_id": company_id,
            # "user_id": user_id,           # REMOVED this field
            "filename": file.filename,
            "content_type": file.content_type
        }
        # Use the imported task instance
        task = process_document_haystack_task.delay(**task_payload)
        endpoint_log.info("Document ingestion task queued successfully", task_id=task.id, task_name=process_document_haystack_task.name)

    # >>>>>>>>>>> CORRECTION APPLIED (Pass Enum Member) <<<<<<<<<<<<<<
    except Exception as e:
        endpoint_log.exception("Failed to queue Celery task", error=str(e))
        try:
            # Update DB status to ERROR if Celery queueing fails, passing conn and using retry
            async with get_db_conn() as conn:
                await api_db_retry_strategy(db_client.update_document_status)(
                    conn=conn,
                    document_id=document_id,
                    status=DocumentStatus.ERROR,   # Pass Enum
                    error_message=f"Failed to queue processing task: {e}"
                )
        except Exception as db_err:
            endpoint_log.exception("Failed to update status to ERROR after Celery failure", error=str(db_err))
        # Raise original exception details
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to queue processing task: {e}"
        )
    # >>>>>>>>>>> END CORRECTION <<<<<<<<<<<<<<

    return IngestResponse(document_id=str(document_id), task_id=task.id, status=DocumentStatus.UPLOADED.value, message="Document upload accepted, processing started.")
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Upload Endpoint Logic


@router.get(
    "/status/{document_id}",
    response_model=StatusResponse,
    summary="Get the status of a specific document",
    responses={
        404: {"model": ErrorDetail, "description": "Document not found"},
        422: {"model": ErrorDetail, "description": "Validation Error (Missing Headers)"},
        500: {"model": ErrorDetail, "description": "Internal Server Error"},
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB, MinIO, Milvus)"},
    }
)
async def get_document_status(
    request: Request,
    document_id: uuid.UUID = Path(..., description="The UUID of the document"),
    minio_client: MinioClient = Depends(get_minio_client),
):
    """
    Retrieves the status of a document from PostgreSQL.
    Performs live checks:
    - Verifies file existence in MinIO using the 'file_path' column.
    - Counts chunks in Milvus (via executor).
    - Updates the DB status if inconsistencies are found.
    Header X-Company-ID is read directly from the request.
    """
    company_id = request.headers.get("X-Company-ID")
    req_id = getattr(request.state, 'request_id', 'N/A')
    if not company_id:
        log.bind(request_id=req_id).warning("Missing X-Company-ID header in get_document_status")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")

    # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Get Status Endpoint Logic
    status_log = log.bind(request_id=req_id, document_id=str(document_id), company_id=company_id)
    status_log.info("Request received for document status")

    try:
        company_uuid = uuid.UUID(company_id)
    except ValueError:
        status_log.warning("Invalid Company ID format")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")

    doc_data: Optional[Dict[str, Any]] = None
    needs_update = False
    updated_status_enum = None # Use enum internally for logic
    updated_chunk_count = None
    final_error_message = None

    try:
        async with get_db_conn() as conn:
             doc_data = await api_db_retry_strategy(db_client.get_document_by_id)(
                 conn, doc_id=document_id, company_id=company_uuid
             )
        if not doc_data:
            status_log.warning("Document not found in DB")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
        status_log.info("Retrieved base document data from DB", status=doc_data['status'])
        updated_status_enum = DocumentStatus(doc_data['status'])
        updated_chunk_count = doc_data.get('chunk_count')
        final_error_message = doc_data.get('error_message')
    except HTTPException as http_exc: raise http_exc
    except Exception as e:
        status_log.exception("Error fetching document status from DB", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error fetching status.")

    # --- USE CORRECT COLUMN NAME: file_path ---
    minio_path = doc_data.get('file_path')
    # ------------------------------------------
    if not minio_path:
         status_log.warning("MinIO file path missing in DB record", db_id=doc_data['id'])
         minio_exists = False
    else:
        status_log.debug("Checking MinIO for file existence", object_name=minio_path)
        minio_exists = await minio_client.check_file_exists_async(minio_path)
        status_log.info("MinIO existence check complete", exists=minio_exists)
        if not minio_exists and updated_status_enum not in [DocumentStatus.ERROR, DocumentStatus.PENDING]:
             status_log.warning("File missing in MinIO but DB status is not ERROR/PENDING", current_db_status=updated_status_enum.value)
             if updated_status_enum != DocumentStatus.ERROR:
                 needs_update = True
                 updated_status_enum = DocumentStatus.ERROR
                 final_error_message = "File missing from storage."

    status_log.debug("Checking Milvus for chunk count...")
    loop = asyncio.get_running_loop()
    milvus_chunk_count = -1
    try:
        # Use the corrected helper function _get_milvus_chunk_count_sync
        milvus_chunk_count = await loop.run_in_executor(
            None, _get_milvus_chunk_count_sync, str(document_id), company_id
        )
        status_log.info("Milvus chunk count check complete", count=milvus_chunk_count)
        if milvus_chunk_count == -1:
            status_log.error("Milvus count check failed (returned -1). Treating as error.")
            if updated_status_enum != DocumentStatus.ERROR: needs_update = True; updated_status_enum = DocumentStatus.ERROR; final_error_message = (final_error_message or "") + " Failed to verify processed data (Milvus count error)."
        elif milvus_chunk_count > 0 and updated_status_enum == DocumentStatus.UPLOADED:
            status_log.warning("Inconsistency: Chunks found in Milvus but DB status is 'uploaded'. Correcting to 'processed'.")
            needs_update = True; updated_status_enum = DocumentStatus.PROCESSED; updated_chunk_count = milvus_chunk_count; final_error_message = None
        elif milvus_chunk_count == 0 and updated_status_enum == DocumentStatus.PROCESSED:
             status_log.warning("Inconsistency: DB status is 'processed' but no chunks found in Milvus. Correcting to 'error'.")
             needs_update = True; updated_status_enum = DocumentStatus.ERROR; updated_chunk_count = 0; final_error_message = (final_error_message or "") + " Processed data missing (Milvus count is 0)."
        elif updated_status_enum == DocumentStatus.PROCESSED:
            # If status is already PROCESSED, just update chunk_count if it differs from Milvus live count
            if updated_chunk_count is None or updated_chunk_count != milvus_chunk_count:
                 updated_chunk_count = milvus_chunk_count
                 if doc_data.get('chunk_count') != updated_chunk_count: needs_update = True
    except Exception as e:
        status_log.exception("Unexpected error during Milvus count check", error=str(e))
        milvus_chunk_count = -1
        if updated_status_enum != DocumentStatus.ERROR: needs_update = True; updated_status_enum = DocumentStatus.ERROR; final_error_message = (final_error_message or "") + f" Error checking processed data: {e}."

    # If any inconsistency was detected, update the DB record
    if needs_update:
        status_log.warning("Inconsistency detected, updating document status in DB", new_status=updated_status_enum.value, new_count=updated_chunk_count, new_error=final_error_message)
        try:
             async with get_db_conn() as conn:
                 # >>>>>>>>>>> CORRECTION APPLIED (Pass Enum Member) <<<<<<<<<<<<<<
                 await api_db_retry_strategy(db_client.update_document_status)(
                     conn=conn, # Pass conn
                     document_id=document_id,
                     status=updated_status_enum, # Pass Enum
                     chunk_count=updated_chunk_count,
                     error_message=final_error_message
                 )
                 # >>>>>>>>>>> END CORRECTION <<<<<<<<<<<<<<
             status_log.info("Document status updated successfully in DB due to inconsistency check.")
             # Update local doc_data to reflect the change for the response
             doc_data['status'] = updated_status_enum.value
             if updated_chunk_count is not None: doc_data['chunk_count'] = updated_chunk_count
             if final_error_message is not None: doc_data['error_message'] = final_error_message
             else: doc_data['error_message'] = None # Ensure error message is cleared if status is not ERROR
        except Exception as e: status_log.exception("Failed to update document status in DB after inconsistency check", error=str(e))
        # Proceed even if update fails, return the intended corrected status

    status_log.info("Returning final document status")
    # Access file_name, assuming DB client returns dict with this key
    file_name_from_db = doc_data.get('file_name')

    return StatusResponse(
        document_id=str(doc_data['id']),
        company_id=doc_data.get('company_id'),
        status=doc_data['status'], # Use potentially updated status string
        file_name=file_name_from_db, # Use the fetched file_name
        file_type=doc_data.get('file_type'),
        file_path=doc_data.get('file_path'),
        chunk_count=doc_data.get('chunk_count', 0),
        minio_exists=minio_exists,
        milvus_chunk_count=milvus_chunk_count,
        last_updated=doc_data.get('updated_at'),
        uploaded_at=doc_data.get('uploaded_at'),
        error_message=doc_data.get('error_message'),
        metadata=doc_data.get('metadata')
    )
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Get Status Endpoint Logic


# >>>>>>>>>>> CORRECTION APPLIED (Change response model and return type) <<<<<<<<<<<<<<
@router.get(
    "/status",
    # response_model=PaginatedStatusResponse, <-- Remove Paginated Response
    response_model=List[StatusResponse],      # <-- Use List of StatusResponse
    summary="List document statuses with pagination and live checks",
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
# >>>>>>>>>>> END CORRECTION <<<<<<<<<<<<<<
    """
    Lists documents for the company with pagination.
    Performs live checks for MinIO/Milvus in parallel for listed documents using 'file_path'.
    Updates the DB status/chunk_count if inconsistencies are found **sequentially**.
    Returns the potentially updated status information **as a direct list**.
    Header X-Company-ID is read directly from the request.
    """
    company_id = request.headers.get("X-Company-ID")
    req_id = getattr(request.state, 'request_id', 'N/A')
    if not company_id:
        log.bind(request_id=req_id).warning("Missing X-Company-ID header in list_document_statuses")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")

    # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - List Statuses Endpoint Logic
    list_log = log.bind(request_id=req_id, company_id=company_id, limit=limit, offset=offset)
    list_log.info("Listing document statuses with real-time checks")

    try:
        company_uuid = uuid.UUID(company_id)
    except ValueError:
        list_log.warning("Invalid Company ID format")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")

    documents_db: List[Dict[str, Any]] = []
    total_count: int = 0 # total_count is fetched but no longer returned in the response

    try:
        async with get_db_conn() as conn:
             # LLM_FLAG: CRITICAL_DB_READ - List documents paginated
            documents_db, total_count = await api_db_retry_strategy(db_client.list_documents_paginated)(
                conn, company_id=company_uuid, limit=limit, offset=offset
            )
        list_log.info("Retrieved documents from DB", count=len(documents_db), total_db_count=total_count)
    except AttributeError as ae: # Specifically catch the AttributeError if list_documents_paginated isn't defined
        list_log.exception("AttributeError calling database function (likely missing function)", error=str(ae))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error: Incomplete database client.")
    except Exception as e:
        list_log.exception("Error listing documents from DB", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error listing documents.")

    if not documents_db:
        # >>>>>>>>>>> CORRECTION APPLIED (Return empty list) <<<<<<<<<<<<<<
        return [] # Return empty list directly
        # >>>>>>>>>>> END CORRECTION <<<<<<<<<<<<<<

    async def check_single_document(doc_db_data: Dict[str, Any]) -> Dict[str, Any]:
        """Async helper to check MinIO/Milvus for one document."""
        # (Keep internal logic of check_single_document as it was, using filter_documents)
        # LLM_FLAG: SENSITIVE_SUB_LOGIC - Parallel check for single document
        check_log = log.bind(request_id=req_id, document_id=str(doc_db_data['id']), company_id=company_id)
        check_log.debug("Starting live checks for document")
        minio_exists_live = False; milvus_count_live = -1; doc_needs_update = False
        # Initialize updated values with current DB values
        # Use Enum internally for easier comparison
        doc_updated_status_enum = DocumentStatus(doc_db_data['status'])
        doc_updated_chunk_count = doc_db_data.get('chunk_count')
        doc_final_error_msg = doc_db_data.get('error_message')

        # --- USE CORRECT COLUMN NAME: file_path ---
        minio_path_db = doc_db_data.get('file_path') # Ensuring 'file_path' is used
        # ------------------------------------------
        minio_check_error = None
        if minio_path_db:
            try:
                minio_exists_live = await minio_client.check_file_exists_async(minio_path_db)
                check_log.debug("MinIO check done", exists=minio_exists_live)
                if not minio_exists_live and doc_updated_status_enum not in [DocumentStatus.ERROR, DocumentStatus.PENDING]:
                     if doc_updated_status_enum != DocumentStatus.ERROR:
                         doc_needs_update = True
                         doc_updated_status_enum = DocumentStatus.ERROR
                         doc_final_error_msg = "File missing from storage."
            except Exception as e:
                minio_check_error = e
                check_log.error("MinIO check failed", error=str(e))
                minio_exists_live = False
                if doc_updated_status_enum != DocumentStatus.ERROR:
                    doc_needs_update = True
                    doc_updated_status_enum = DocumentStatus.ERROR
                    doc_final_error_msg = (doc_final_error_msg or "") + f" MinIO check error ({type(minio_check_error).__name__})."
        else: check_log.warning("MinIO file path missing in DB record."); minio_exists_live = False

        loop = asyncio.get_running_loop()
        milvus_check_error = None
        try:
            milvus_count_live = await loop.run_in_executor( None, _get_milvus_chunk_count_sync, str(doc_db_data['id']), company_id )
            check_log.debug("Milvus count check done", count=milvus_count_live)
            if milvus_count_live == -1:
                if doc_updated_status_enum != DocumentStatus.ERROR:
                    doc_needs_update = True; doc_updated_status_enum = DocumentStatus.ERROR; doc_final_error_msg = (doc_final_error_msg or "") + " Failed Milvus count check."
            elif milvus_count_live > 0 and doc_updated_status_enum == DocumentStatus.UPLOADED:
                 doc_needs_update = True; doc_updated_status_enum = DocumentStatus.PROCESSED; doc_updated_chunk_count = milvus_count_live; doc_final_error_msg = None
            elif milvus_count_live == 0 and doc_updated_status_enum == DocumentStatus.PROCESSED:
                 doc_needs_update = True; doc_updated_status_enum = DocumentStatus.ERROR; doc_updated_chunk_count = 0; doc_final_error_msg = (doc_final_error_msg or "") + " Processed data missing."
            elif doc_updated_status_enum == DocumentStatus.PROCESSED:
                 if doc_updated_chunk_count is None or doc_updated_chunk_count != milvus_count_live:
                      doc_updated_chunk_count = milvus_count_live
                      if doc_db_data.get('chunk_count') != doc_updated_chunk_count: doc_needs_update = True
        except Exception as e:
            milvus_check_error = e
            check_log.exception("Unexpected error during Milvus count check", error=str(e)); milvus_count_live = -1
            if doc_updated_status_enum != DocumentStatus.ERROR:
                doc_needs_update = True
                doc_updated_status_enum = DocumentStatus.ERROR
                doc_final_error_msg = (doc_final_error_msg or "") + f" Error checking Milvus ({type(milvus_check_error).__name__})."

        # Return status as enum for easier processing later
        return {
            "db_data": doc_db_data,
            "needs_update": doc_needs_update,
            "updated_status_enum": doc_updated_status_enum, # Return enum
            "updated_chunk_count": doc_updated_chunk_count,
            "final_error_message": doc_final_error_msg,
            "live_minio_exists": minio_exists_live,
            "live_milvus_chunk_count": milvus_count_live
        }
        # LLM_FLAG: SENSITIVE_SUB_LOGIC_END - Parallel check

    # Run checks concurrently
    check_tasks = [check_single_document(doc) for doc in documents_db]
    check_results = await asyncio.gather(*check_tasks)

    # Aggregate results and identify documents needing DB updates
    updated_doc_data_map = {}
    docs_to_update_in_db = []
    for result in check_results:
        doc_id_str = str(result["db_data"]["id"])
        # Store the potentially updated state for the final response, using .value for status
        updated_doc_data_map[doc_id_str] = {
            **result["db_data"],
            "status": result["updated_status_enum"].value, # Store string value
            "chunk_count": result["updated_chunk_count"],
            "error_message": result["final_error_message"]
        }
        # If an update is needed, add to the list for DB operation
        if result["needs_update"]:
            docs_to_update_in_db.append({
                "id": result["db_data"]["id"],
                "status_enum": result["updated_status_enum"], # Keep enum for DB call
                "chunk_count": result["updated_chunk_count"],
                "error_message": result["final_error_message"]
            })

    # >>>>>>>>>>> CORRECTION APPLIED (Pass Enum Member) <<<<<<<<<<<<<<
    # Perform DB updates if needed, sequentially within one connection
    if docs_to_update_in_db:
        list_log.warning("Updating statuses sequentially in DB for inconsistent documents", count=len(docs_to_update_in_db))
        try:
             async with get_db_conn() as conn: # Get one connection for all sequential updates
                 for update_info in docs_to_update_in_db:
                     doc_id_to_update = update_info["id"]
                     try:
                         update_result = await api_db_retry_strategy(db_client.update_document_status)(
                             conn=conn, # Use the single connection
                             document_id=doc_id_to_update,
                             status=update_info["status_enum"], # Pass Enum
                             chunk_count=update_info["chunk_count"],
                             error_message=update_info["error_message"]
                         )
                         if update_result is False:
                             list_log.warning("DB update command executed but document not found during update", document_id=str(doc_id_to_update))
                         else:
                             list_log.info("Successfully updated DB status", document_id=str(doc_id_to_update), new_status=update_info['status_enum'].value)
                     except Exception as single_update_err:
                         # Log the error for this specific update but continue with others
                         list_log.error("Failed DB update for single document during list check", document_id=str(doc_id_to_update), error=str(single_update_err))

        except Exception as bulk_db_conn_err: # Error getting the connection itself
            list_log.exception("Error acquiring DB connection for sequential updates", error=str(bulk_db_conn_err))
            # If connection fails, updates won't happen. The map still holds intended state.
    # >>>>>>>>>>> END CORRECTION <<<<<<<<<<<<<<

    # Construct final response items using the potentially updated data map
    final_items = []
    for result in check_results: # Iterate through original check results to maintain order
         doc_id_str = str(result["db_data"]["id"])
         # Use the data from the updated map, which reflects intended corrections (status is string value)
         current_data = updated_doc_data_map.get(doc_id_str, result["db_data"]) # Fallback just in case
         # Access file_name, assuming DB client returns dict with this key
         file_name_from_db = current_data.get('file_name')

         final_items.append(StatusResponse(
            document_id=doc_id_str,
            company_id=current_data.get('company_id'),
            status=current_data['status'], # Use potentially updated status string from map
            file_name=file_name_from_db,
            file_type=current_data.get('file_type'),
            file_path=current_data.get('file_path'),
            chunk_count=current_data.get('chunk_count', 0),
            minio_exists=result["live_minio_exists"], # Live check result
            milvus_chunk_count=result["live_milvus_chunk_count"], # Live check result
            last_updated=current_data.get('updated_at'),
            uploaded_at=current_data.get('uploaded_at'),
            error_message=current_data.get('error_message'),
            metadata=current_data.get('metadata')
         ))

    list_log.info("Returning enriched statuses", count=len(final_items))
    # >>>>>>>>>>> CORRECTION APPLIED (Return list directly) <<<<<<<<<<<<<<
    return final_items # Return the list directly
    # return PaginatedStatusResponse(items=final_items, total=total_count, limit=limit, offset=offset) # Old return
    # >>>>>>>>>>> END CORRECTION <<<<<<<<<<<<<<
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - List Statuses Endpoint Logic


@router.post(
    "/retry/{document_id}",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Retry ingestion for a document currently in 'error' state",
    responses={
        404: {"model": ErrorDetail, "description": "Document not found"},
        409: {"model": ErrorDetail, "description": "Document is not in 'error' state"},
        422: {"model": ErrorDetail, "description": "Validation Error (Missing Headers)"},
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
    Headers X-Company-ID and X-User-ID are read directly from the request.
    User ID is not sent to Celery task.
    """
    company_id = request.headers.get("X-Company-ID")
    user_id = request.headers.get("X-User-ID") # Read user_id, but don't pass to task
    req_id = getattr(request.state, 'request_id', 'N/A')
    if not company_id: raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")
    if not user_id: raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-User-ID")

    # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Retry Endpoint Logic
    retry_log = log.bind(request_id=req_id, document_id=str(document_id), company_id=company_id, user_id=user_id)
    retry_log.info("Received request to retry document ingestion")

    try:
        company_uuid = uuid.UUID(company_id)
    except ValueError:
        retry_log.warning("Invalid Company ID format")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")

    doc_data: Optional[Dict[str, Any]] = None
    try:
         async with get_db_conn() as conn:
            # Fetch the document data to check status and get filename/type
            doc_data = await api_db_retry_strategy(db_client.get_document_by_id)(
                conn, doc_id=document_id, company_id=company_uuid
            )
         if not doc_data: raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
         if doc_data['status'] != DocumentStatus.ERROR.value: raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Document is not in 'error' state (current state: {doc_data['status']}). Cannot retry.")
         retry_log.info("Document found and confirmed to be in 'error' state.")
    except HTTPException as http_exc: raise http_exc
    except Exception as e:
        retry_log.exception("Error fetching document for retry", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error checking document for retry.")

    # Update status to PROCESSING before queueing the task
    try:
        # >>>>>>>>>>> CORRECTION APPLIED (Pass Enum Member) <<<<<<<<<<<<<<
        async with get_db_conn() as conn:
            await api_db_retry_strategy(db_client.update_document_status)(
                conn=conn,
                document_id=document_id,
                status=DocumentStatus.PROCESSING, # Pass Enum
                chunk_count=None,
                error_message=None
            )
        retry_log.info("Document status updated to 'processing' for retry.")
        # >>>>>>>>>>> END CORRECTION <<<<<<<<<<<<<<
    except Exception as e:
        retry_log.exception("Failed to update document status to 'processing' for retry", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error updating status for retry.")

    try:
        # Access file_name, assuming DB client returns dict with this key
        file_name_from_db = doc_data.get('file_name')
        if not file_name_from_db:
             # Fallback or raise error if file_name is crucial and missing
             retry_log.error("File name missing in fetched document data for retry task payload", document_id=str(document_id))
             raise HTTPException(status_code=500, detail="Internal error: Missing file name for retry.")

        task_payload = {
            "document_id": str(document_id),
            "company_id": company_id,
            "filename": file_name_from_db, # Use fetched file_name
            "content_type": doc_data.get('file_type')
        }
        # Use the imported task instance
        task = process_document_haystack_task.delay(**task_payload)
        retry_log.info("Document reprocessing task queued successfully", task_id=task.id, task_name=process_document_haystack_task.name)
    except Exception as e:
        retry_log.exception("Failed to re-queue Celery task for retry", error=str(e))
        # Attempt to revert status back to ERROR? Or leave as PROCESSING? Leaving as PROCESSING for now.
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to queue reprocessing task: {e}")

    # Return response indicating retry has started
    return IngestResponse(document_id=str(document_id), task_id=task.id, status=DocumentStatus.PROCESSING.value, message="Document retry accepted, processing started.")
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Retry Endpoint Logic


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document and its associated data",
    responses={
        404: {"model": ErrorDetail, "description": "Document not found"},
        422: {"model": ErrorDetail, "description": "Validation Error (Missing Headers)"},
        500: {"model": ErrorDetail, "description": "Internal Server Error"},
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB, MinIO, Milvus)"},
    }
)
async def delete_document_endpoint(
    request: Request,
    document_id: uuid.UUID = Path(..., description="The UUID of the document to delete"),
    minio_client: MinioClient = Depends(get_minio_client),
):
    """
    Deletes a document completely:
    - Removes chunks from Milvus (via executor).
    - Removes the file from MinIO (async) using 'file_path'.
    - Removes the record from PostgreSQL.
    Verifies ownership before deletion using X-Company-ID.
    Header X-Company-ID is read directly from the request.
    """
    company_id = request.headers.get("X-Company-ID")
    req_id = getattr(request.state, 'request_id', 'N/A')
    if not company_id:
        log.bind(request_id=req_id).warning("Missing X-Company-ID header in delete_document_endpoint")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")

    # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Delete Endpoint Logic
    delete_log = log.bind(request_id=req_id, document_id=str(document_id), company_id=company_id)
    delete_log.info("Received request to delete document")

    try:
        company_uuid = uuid.UUID(company_id)
    except ValueError:
        delete_log.warning("Invalid Company ID format")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")

    # Fetch document data first to get file_path and verify ownership
    doc_data: Optional[Dict[str, Any]] = None
    try:
        async with get_db_conn() as conn:
             doc_data = await api_db_retry_strategy(db_client.get_document_by_id)(
                 conn, doc_id=document_id, company_id=company_uuid
             )
        if not doc_data: raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
        delete_log.info("Document verified for deletion", filename=doc_data.get('file_name'))
    except HTTPException as http_exc: raise http_exc # Re-raise 404
    except Exception as e:
        delete_log.exception("Error verifying document before deletion", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error during delete verification.")

    errors = [] # Collect non-critical errors during deletion

    # 1. Delete from Milvus (Vector Store)
    delete_log.info("Attempting to delete chunks from Milvus...")
    loop = asyncio.get_running_loop()
    try:
        # Run synchronous delete helper in executor
        milvus_deleted = await loop.run_in_executor(None, _delete_milvus_sync, str(document_id), company_id)
        if milvus_deleted: delete_log.info("Milvus delete command executed successfully.")
        else: errors.append("Failed Milvus delete (helper returned False)"); delete_log.warning("Milvus delete operation reported failure.")
    except Exception as e:
        delete_log.exception("Unexpected error during Milvus delete", error=str(e)); errors.append(f"Milvus error: {e}")

    # 2. Delete from MinIO (Object Storage)
    # --- USE CORRECT COLUMN NAME: file_path ---
    minio_path = doc_data.get('file_path') # Ensuring 'file_path' is used
    # ------------------------------------------
    if minio_path:
        delete_log.info("Attempting to delete file from MinIO...", object_name=minio_path)
        try:
            await minio_client.delete_file_async(minio_path)
            delete_log.info("Successfully deleted file from MinIO.")
        except MinioError as me:
            delete_log.error("Failed to delete file from MinIO", object_name=minio_path, error=str(me)); errors.append(f"MinIO error: {me}")
        except Exception as e:
            delete_log.exception("Unexpected error during MinIO delete", error=str(e)); errors.append(f"MinIO unexpected error: {e}")
    else:
        delete_log.warning("Skipping MinIO delete: file path not found in DB."); errors.append("MinIO path unknown.")

    # 3. Delete from PostgreSQL (Database Record) - Do this last
    delete_log.info("Attempting to delete record from PostgreSQL...")
    try:
         async with get_db_conn() as conn:
            # Pass connection and company_uuid for verification again
            deleted_in_db = await api_db_retry_strategy(db_client.delete_document)(
                conn=conn, # Pass the connection
                doc_id=document_id,
                company_id=company_uuid
            )
            if deleted_in_db: delete_log.info("Document record deleted successfully from PostgreSQL")
            else:
                # This case should ideally not happen if get_document_by_id succeeded,
                # but could occur in a race condition or if delete_document logic is flawed.
                delete_log.warning("PostgreSQL delete command executed but no record was deleted (already gone or company mismatch on delete?).")
                # Should this be an error? If get found it, delete should too.
                errors.append("PostgreSQL record not found during delete, despite initial verification.")
    except Exception as e:
        # This is critical - data might be orphaned in MinIO/Milvus
        delete_log.exception("CRITICAL: Failed to delete document record from PostgreSQL", error=str(e))
        error_detail = f"Deleted from storage/vectors (errors: {', '.join(errors)}) but FAILED to delete DB record: {e}"
        # Return 500 because the overall state is inconsistent
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=error_detail)

    # Log any non-critical errors encountered
    if errors: delete_log.warning("Document deletion process completed with non-critical errors (Milvus/MinIO/DB state)", errors=errors)

    delete_log.info("Document deletion process finished.")
    # Return 204 No Content on success, even if minor errors occurred in storage/vector deletion
    # The critical part (DB deletion) succeeded or raised HTTP 500.
    return None # FastAPI handles 204 automatically when function returns None
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Delete Endpoint Logic
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

## File: `app\db\postgres_client.py`
```py
# ingest-service/app/db/postgres_client.py
import uuid
from typing import Any, Optional, Dict, List, Tuple
import asyncpg
import structlog
import json
from datetime import datetime, timezone

# LLM_FLAG: FUNCTIONAL_CODE - Imports (DO NOT TOUCH)
from app.core.config import settings
from app.models.domain import DocumentStatus

log = structlog.get_logger(__name__)

_pool: Optional[asyncpg.Pool] = None

# --- Pool Management (Functional - DO NOT TOUCH) ---
# LLM_FLAG: SENSITIVE_CODE_BLOCK_START - DB Pool Management
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
# LLM_FLAG: SENSITIVE_CODE_BLOCK_END - DB Pool Management

# --- Document Operations ---

# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH create_document_record DB logic lightly
# >>>>>>>>>>> CORRECTION APPLIED (Diff 1) <<<<<<<<<<<<<<
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
    # --- QUERY CORREGIDA: Formatted, no functional change from diff ---
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
    # -----------------------------------
    metadata_db = json.dumps(metadata) if metadata else None
    # --- PARÁMETROS CORREGIDOS: Sin user_id ---
    params = [
        doc_id,
        company_id,
        filename,
        file_type,
        file_path,
        metadata_db,
        status.value,
        0  # chunk_count inicializado a 0
    ]
    # ------------------------------------------
    insert_log = log.bind(company_id=str(company_id), filename=filename, doc_id=str(doc_id))
    try:
        await conn.execute(query, *params)
        insert_log.info("Document record created in PostgreSQL")
    except asyncpg.exceptions.UndefinedColumnError as col_err:
         # Log específico si el error es por columna inexistente
         insert_log.critical(f"FATAL DB SCHEMA ERROR: Column missing in 'documents' table.", error=str(col_err), table_schema_expected="id, company_id, file_name, file_type, file_path, metadata, status, chunk_count, error_message, uploaded_at, updated_at")
         raise RuntimeError(f"Database schema error: {col_err}") from col_err
    except Exception as e:
        insert_log.error("Failed to create document record", error=str(e), exc_info=True)
        raise
# >>>>>>>>>>> END CORRECTION (Diff 1) <<<<<<<<<<<<<<

# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH find_document_by_name_and_company DB logic lightly
async def find_document_by_name_and_company(conn: asyncpg.Connection, filename: str, company_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """Busca un documento por nombre y compañía."""
    query = """
    SELECT id, status FROM documents
    WHERE file_name = $1 AND company_id = $2;
    """
    find_log = log.bind(company_id=str(company_id), filename=filename)
    try:
        record = await conn.fetchrow(query, filename, company_id)
        if record:
            find_log.debug("Found existing document record")
            return dict(record)
        else:
            find_log.debug("No existing document record found")
            return None
    except Exception as e:
        find_log.error("Failed to find document by name and company", error=str(e), exc_info=True)
        raise

# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH update_document_status DB logic lightly
async def update_document_status(
    document_id: uuid.UUID,
    status: DocumentStatus,
    pool: Optional[asyncpg.Pool] = None,
    conn: Optional[asyncpg.Connection] = None,
    chunk_count: Optional[int] = None,
    error_message: Optional[str] = None
) -> bool:
    """Actualiza el estado, chunk_count y/o error_message de un documento."""
    if not pool and not conn:
        pool = await get_db_pool()

    params: List[Any] = [document_id]
    fields: List[str] = ["status = $2", "updated_at = NOW() AT TIME ZONE 'UTC'"]
    params.append(status.value) # Ensure enum value is used
    param_index = 3

    if chunk_count is not None:
        fields.append(f"chunk_count = ${param_index}"); params.append(chunk_count); param_index += 1

    if status == DocumentStatus.ERROR:
        if error_message is not None:
            fields.append(f"error_message = ${param_index}"); params.append(error_message); param_index += 1
    else:
        # Ensure error_message is explicitly set to NULL when status is not ERROR
        fields.append("error_message = NULL")

    set_clause = ", ".join(fields)
    query = f"UPDATE documents SET {set_clause} WHERE id = $1;"
    update_log = log.bind(document_id=str(document_id), new_status=status.value)

    try:
        if conn:
            result = await conn.execute(query, *params)
        else:
             async with pool.acquire() as connection: # Use acquire() with pool if conn not provided
                result = await connection.execute(query, *params)

        if result == 'UPDATE 0':
             update_log.warning("Attempted to update status for non-existent document_id")
             return False
        update_log.info("Document status updated in PostgreSQL")
        return True
    except Exception as e:
        update_log.error("Failed to update document status", error=str(e), exc_info=True)
        raise

# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH get_document_by_id DB logic lightly
async def get_document_by_id(conn: asyncpg.Connection, doc_id: uuid.UUID, company_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """Obtiene un documento por ID y verifica la compañía."""
    # >>>>>>>>>>> CORRECTION APPLIED (Diff 4a) <<<<<<<<<<<<<<
    # --- UPDATED QUERY: Uses file_path, includes error_message ---
    query = """
    SELECT
        id,
        company_id,
        file_name,
        file_type,
        file_path,
        metadata,
        status,
        chunk_count,
        error_message,
        uploaded_at,
        updated_at
    FROM documents
    WHERE id = $1 AND company_id = $2;
    """
    # --------------------------------------
    # >>>>>>>>>>> END CORRECTION (Diff 4a) <<<<<<<<<<<<<<
    get_log = log.bind(document_id=str(doc_id), company_id=str(company_id))
    try:
        record = await conn.fetchrow(query, doc_id, company_id)
        if not record:
            get_log.warning("Document not found or company mismatch")
            return None
        # Convert Record to dict for consistent return type
        return dict(record)
    except Exception as e:
        get_log.error("Failed to get document by ID", error=str(e), exc_info=True)
        raise

# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH list_documents_paginated DB logic lightly
async def list_documents_paginated(
    conn: asyncpg.Connection,
    company_id: uuid.UUID,
    limit: int,
    offset: int
) -> Tuple[List[Dict[str, Any]], int]:
    """Lista documentos paginados para una compañía y devuelve el conteo total."""
    # >>>>>>>>>>> CORRECTION APPLIED (Diff 4b) <<<<<<<<<<<<<<
    # --- UPDATED QUERY: Uses file_path ---
    query = """
    SELECT
        id,
        company_id,
        file_name,
        file_type,
        file_path,
        metadata,
        status,
        chunk_count,
        error_message,
        uploaded_at,
        updated_at,
        COUNT(*) OVER() AS total_count
    FROM documents
    WHERE company_id = $1
    ORDER BY updated_at DESC
    LIMIT $2 OFFSET $3;
    """
    # --------------------------------------
    # >>>>>>>>>>> END CORRECTION (Diff 4b) <<<<<<<<<<<<<<
    list_log = log.bind(company_id=str(company_id), limit=limit, offset=offset)
    try:
        rows = await conn.fetch(query, company_id, limit, offset)
        total = 0
        results = []
        if rows:
            total = rows[0]['total_count'] # Get total count from the first row
            # Convert all rows to dictionaries and remove the total_count field
            results = [dict(r) for r in rows]
            for r in results:
                r.pop('total_count', None)

        list_log.debug("Fetched paginated documents", count=len(results), total=total)
        return results, total
    except Exception as e:
        list_log.error("Failed to list paginated documents", error=str(e), exc_info=True)
        raise

# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH delete_document DB logic lightly
async def delete_document(conn: asyncpg.Connection, doc_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    """Elimina un documento verificando la compañía."""
    query = "DELETE FROM documents WHERE id = $1 AND company_id = $2 RETURNING id;"
    delete_log = log.bind(document_id=str(doc_id), company_id=str(company_id))
    try:
        deleted_id = await conn.fetchval(query, doc_id, company_id)
        if deleted_id:
            delete_log.info("Document deleted from PostgreSQL", deleted_id=str(deleted_id))
            return True
        else:
            # This case handles both "not found" and "company mismatch"
            delete_log.warning("Document not found or company mismatch during delete attempt.")
            return False
    except Exception as e:
        delete_log.error("Error deleting document record", error=str(e), exc_info=True)
        raise

# --- Chat Functions (Placeholder - Copied from Query Service for context, likely unused here) ---
# LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Chat Functions (Likely Unused)
# NOTE: These functions seem out of place for an ingest service, but kept as per original file structure
async def create_chat(user_id: uuid.UUID, company_id: uuid.UUID, title: Optional[str] = None) -> uuid.UUID:
    pool = await get_db_pool()
    chat_id = uuid.uuid4()
    query = """INSERT INTO chats (id, user_id, company_id, title, created_at, updated_at) VALUES ($1, $2, $3, $4, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC') RETURNING id;"""
    try:
        async with pool.acquire() as conn:
            result = await conn.fetchval(query, chat_id, user_id, company_id, title or f"Chat {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}")
            return result
    except Exception as e:
        log.error("Failed create_chat (ingest context - likely unused)", error=str(e))
        raise

async def get_user_chats(user_id: uuid.UUID, company_id: uuid.UUID, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    pool = await get_db_pool()
    query = """SELECT id, title, updated_at FROM chats WHERE user_id = $1 AND company_id = $2 ORDER BY updated_at DESC LIMIT $3 OFFSET $4;"""
    try:
        async with pool.acquire() as conn: rows = await conn.fetch(query, user_id, company_id, limit, offset); return [dict(row) for row in rows]
    except Exception as e: log.error("Failed get_user_chats (ingest context - likely unused)", error=str(e)); raise

async def check_chat_ownership(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    pool = await get_db_pool()
    query = "SELECT EXISTS (SELECT 1 FROM chats WHERE id = $1 AND user_id = $2 AND company_id = $3);"
    try:
        async with pool.acquire() as conn: exists = await conn.fetchval(query, chat_id, user_id, company_id); return exists is True
    except Exception as e: log.error("Failed check_chat_ownership (ingest context - likely unused)", error=str(e)); return False

async def get_chat_messages(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    pool = await get_db_pool(); owner = await check_chat_ownership(chat_id, user_id, company_id)
    if not owner: return []
    messages_query = """SELECT id, role, content, sources, created_at FROM messages WHERE chat_id = $1 ORDER BY created_at ASC LIMIT $2 OFFSET $3;"""
    try:
        async with pool.acquire() as conn: message_rows = await conn.fetch(messages_query, chat_id, limit, offset); return [dict(row) for row in message_rows]
    except Exception as e: log.error("Failed get_chat_messages (ingest context - likely unused)", error=str(e)); raise

async def save_message(chat_id: uuid.UUID, role: str, content: str, sources: Optional[List[Dict[str, Any]]] = None) -> uuid.UUID:
    pool = await get_db_pool(); message_id = uuid.uuid4()
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                update_chat_query = "UPDATE chats SET updated_at = NOW() AT TIME ZONE 'UTC' WHERE id = $1 RETURNING id;"; chat_updated = await conn.fetchval(update_chat_query, chat_id)
                if not chat_updated: raise ValueError(f"Chat {chat_id} not found (ingest context - likely unused).")
                insert_message_query = """INSERT INTO messages (id, chat_id, role, content, sources, created_at) VALUES ($1, $2, $3, $4, $5, NOW() AT TIME ZONE 'UTC') RETURNING id;"""
                result = await conn.fetchval(insert_message_query, message_id, chat_id, role, content, json.dumps(sources or [])); return result
            except Exception as e: log.error("Failed save_message (ingest context - likely unused)", error=str(e)); raise

async def delete_chat(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    pool = await get_db_pool()
    query = "DELETE FROM chats WHERE id = $1 AND user_id = $2 AND company_id = $3 RETURNING id;"; delete_log = log.bind(chat_id=str(chat_id), user_id=str(user_id))
    try:
        async with pool.acquire() as conn: deleted_id = await conn.fetchval(query, chat_id, user_id, company_id); return deleted_id is not None
    except Exception as e: delete_log.error("Failed to delete chat (ingest context - likely unused)", error=str(e)); raise
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
    PyPDFToDocument,
    MarkdownToDocument,
    HTMLToDocument,
    TextFileToDocument,
)
from haystack.components.converters.docx import DOCXToDocument
from haystack.components.preprocessors import DocumentSplitter
# >>>>>>>>>>> CORRECTION: Remove OpenAI Embedder import <<<<<<<<<<<<<<
# from haystack.components.embedders import OpenAIDocumentEmbedder
# >>>>>>>>>>> END CORRECTION <<<<<<<<<<<<<<
from haystack.components.writers import DocumentWriter

# Milvus specific integration
from milvus_haystack import MilvusDocumentStore

# >>>>>>>>>>> CORRECTION: Add FastEmbed imports <<<<<<<<<<<<<<
# FastEmbed Imports
from haystack_integrations.components.embedders.fastembed import (
    FastembedDocumentEmbedder,
)
# Import for GPU device selection
from haystack.utils import ComponentDevice
# >>>>>>>>>>> END CORRECTION <<<<<<<<<<<<<<

# Custom imports
from app.core.config import settings
from app.db import postgres_client as db_client
from app.models.domain import DocumentStatus
from app.services.minio_client import MinioClient, MinioError
from app.tasks.celery_app import celery_app

# Initialize logger
log = structlog.get_logger(__name__)

# Timeout for the entire processing flow within the task
TIMEOUT_SECONDS = 600 # 10 minutes, adjust as needed

# --- Milvus Initialization ---
def _initialize_milvus_store() -> MilvusDocumentStore:
    """
    Synchronously initializes and returns a MilvusDocumentStore instance.
    Handles potential configuration errors during initialization.
    """
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Milvus Initialization
    log.debug("Initializing MilvusDocumentStore...")
    try:
        # >>>>>>>>>>> CORRECTION: Add embedding_dim if needed by constructor <<<<<<<<<<<<<<
        # Check MilvusDocumentStore constructor arguments for your version.
        # If it requires embedding_dim, uncomment and use settings.EMBEDDING_DIMENSION
        store = MilvusDocumentStore(
            connection_args={"uri": settings.MILVUS_URI},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            # embedding_dim=settings.EMBEDDING_DIMENSION, # Uncomment if required
            consistency_level="Strong",
        )
        # >>>>>>>>>>> END CORRECTION <<<<<<<<<<<<<<
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
# >>>>>>>>>>> CORRECTION: Use FastEmbed Embedder and GPU setting <<<<<<<<<<<<<<
def _initialize_haystack_components(
    document_store: MilvusDocumentStore
) -> Tuple[DocumentSplitter, FastembedDocumentEmbedder, DocumentWriter]: # Adjusted return type hint
    """
    Inicializa Splitter + FastEmbed + Writer.
    Usa GPU si settings.USE_GPU == "true"
    """
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Haystack Component Init
    log.debug("Initializing Haystack components (Splitter, Embedder, Writer)...")
    try:
        # Document Splitter (Remains the same)
        splitter = DocumentSplitter(
            split_by=settings.SPLITTER_SPLIT_BY, # Use setting
            split_length=settings.SPLITTER_CHUNK_SIZE,
            split_overlap=settings.SPLITTER_CHUNK_OVERLAP
        )
        log.info("DocumentSplitter initialized", split_by=settings.SPLITTER_SPLIT_BY, length=settings.SPLITTER_CHUNK_SIZE, overlap=settings.SPLITTER_CHUNK_OVERLAP)

        # Determine Device for FastEmbed
        use_gpu = settings.USE_GPU.lower() == "true"
        if use_gpu:
            try:
                device = ComponentDevice.from_str("cuda:0") # Try to get GPU
                log.info("GPU selected for FastEmbed.", device_str="cuda:0")
            except Exception as gpu_err:
                log.warning("Failed to select GPU, falling back to CPU.", error=str(gpu_err), setting_use_gpu=settings.USE_GPU)
                device = ComponentDevice.from_str("cpu") # Fallback to CPU
        else:
            device = ComponentDevice.from_str("cpu") # Explicitly CPU
            log.info("CPU selected for FastEmbed.", setting_use_gpu=settings.USE_GPU)


        # Document Embedder (FastEmbed)
        log.info("Initializing FastembedDocumentEmbedder...", model=settings.FASTEMBED_MODEL, device=str(device))
        embedder = FastembedDocumentEmbedder(
            model=settings.FASTEMBED_MODEL,
            device=device,
            batch_size=256,      # Increased throughput
            parallel=0           # Use all available cores if device is CPU
            # Add other relevant FastEmbed parameters if needed, e.g., cache_dir
        )

        # Preload model weights to avoid delays in the first task run
        log.info("Warming up FastEmbed model...")
        embedder.warm_up()
        log.info("FastEmbed model warmed up successfully.")


        # Document Writer (using the initialized Milvus store)
        writer = DocumentWriter(
            document_store=document_store,
            policy=DuplicatePolicy.OVERWRITE,
            batch_size=512 # Added for potential performance improvement with many chunks
        )
        log.info("DocumentWriter initialized", policy="OVERWRITE", batch_size=512)

        log.info("Haystack components initialized successfully.")
        return splitter, embedder, writer
    except Exception as e:
        log.exception("Failed to initialize Haystack components", error=str(e), exc_info=True)
        raise RuntimeError(f"Haystack Component Initialization Error: {e}") from e
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Haystack Component Init
# >>>>>>>>>>> END CORRECTION <<<<<<<<<<<<<<

# --- File Type to Converter Mapping ---
def get_converter(content_type: str) -> Type[Any]: # Use Type[Any] for broader compatibility
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
# Celery app instance is imported from app.tasks.celery_app

# --- Logging Configuration ---
# (Keep existing logging setup)
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

handler = logging.StreamHandler()
handler.setFormatter(formatter)
root_logger = logging.getLogger()
if not root_logger.handlers:
    root_logger.addHandler(handler)
    try:
        root_logger.setLevel(settings.LOG_LEVEL.upper())
    except ValueError:
        root_logger.setLevel("INFO")
        log.warning("Invalid LOG_LEVEL in settings, defaulting to INFO.")

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
    minio_client = MinioClient()

    # 2. Download file from Minio
    object_name = f"{company_id}/{document_id}/{filename}"
    temp_file_path = None
    try:
        flow_log.info("Downloading file from MinIO", object_name=object_name)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_file_path = os.path.join(temp_dir, filename)
            await minio_client.download_file(object_name, temp_file_path)
            flow_log.info("File downloaded successfully", temp_path=temp_file_path)

            # --- Processing happens within the 'with temp_dir' block ---

            # 3. Initialize Milvus Store
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

            # 4. Initialize other Haystack Components (Splitter, FastEmbed, Writer)
            try:
                flow_log.info("Initializing Haystack processing components...")
                # This now returns FastembedDocumentEmbedder
                splitter, embedder, writer = await loop.run_in_executor(None, _initialize_haystack_components, store)
                flow_log.info("Haystack processing components initialized.")
            except RuntimeError as e:
                flow_log.error("Failed to initialize Haystack components during flow", error=str(e))
                raise e
            except Exception as e:
                flow_log.exception("Unexpected error initializing Haystack components", error=str(e))
                raise RuntimeError(f"Unexpected Haystack init error: {e}") from e

            # 5. Initialize Converter
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

                def run_haystack_pipeline_sync(local_temp_file_path): # Pass the path
                    nonlocal total_chunks_written
                    pipeline_log = log.bind(
                        document_id=document_id, company_id=company_id, task_id=task_id,
                        filename=filename, in_sync_executor=True
                    )
                    pipeline_log.debug("Executing conversion...")
                    conversion_result = converter.run(sources=[local_temp_file_path])
                    docs = conversion_result["documents"]
                    pipeline_log.debug("Conversion complete", num_docs_converted=len(docs))
                    if not docs:
                        pipeline_log.warning("Converter produced no documents.")
                        return 0

                    for doc in docs:
                        if doc.meta is None: doc.meta = {}
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
                    # Embedder is now FastEmbed
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

                chunks_written = await loop.run_in_executor(None, run_haystack_pipeline_sync, temp_file_path)
                flow_log.info("Haystack pipeline execution finished.", chunks_written=chunks_written)
                return total_chunks_written

            except Exception as e:
                flow_log.exception("Error during Haystack pipeline execution", error=str(e))
                raise RuntimeError(f"Haystack Pipeline Error: {e}") from e
            # Temp dir cleaned automatically

    except MinioError as me:
        flow_log.error("Failed to download file from MinIO", object_name=object_name, error=str(me))
        raise RuntimeError(f"MinIO download failed: {me}") from me
    except Exception as e:
        flow_log.exception("Unexpected error during file download or processing initiation", error=str(e))
        if temp_file_path and os.path.exists(temp_file_path) and isinstance(e, MinioError):
             try: os.remove(temp_file_path)
             except OSError: pass
        raise RuntimeError(f"Unexpected download/init error: {e}") from e

    return 0 # Should not be reached if logic is sound
    # LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Main Async Flow


# --- Celery Task Definition ---
class ProcessDocumentTask(Task):
    """Custom Celery Task class for document processing."""
    name = "app.tasks.process_document.ProcessDocumentTask"
    max_retries = 3
    default_retry_delay = 60

    def __init__(self):
        super().__init__()
        self.task_log = log.bind(task_name=self.name)
        self.task_log.info("ProcessDocumentTask initialized.")

    # (Keep the previously corrected _update_status_with_retry)
    async def _update_status_with_retry(
        self, pool: asyncpg.Pool, doc_id: str, status: DocumentStatus,
        chunk_count: Optional[int] = None, error_msg: Optional[str] = None
    ):
        """Helper to update document status with retry."""
        update_log = self.task_log.bind(document_id=doc_id, target_status=status.value)
        try:
            async with pool.acquire() as conn:
                await db_retry_strategy(db_client.update_document_status)(
                    conn=conn,
                    document_id=uuid.UUID(doc_id),
                    status=status, # Pass Enum member
                    chunk_count=chunk_count,
                    error_message=error_msg
                )
            update_log.info("Document status updated successfully in DB.")
        except Exception as e:
            update_log.critical("CRITICAL: Failed final document status update in DB!",
                                error=str(e), chunk_count=chunk_count, error_msg=error_msg,
                                exc_info=True)
            raise ConnectionError(f"Persistent DB error updating status for {doc_id} to {status.value}") from e

    async def run_async_processing(self, *args, **kwargs):
        """Runs the main async processing flow and handles final status updates."""
        # (Keep existing logic, including calls to the corrected _update_status_with_retry)
        doc_id = kwargs['document_id']
        task_id = self.request.id
        attempt = self.request.retries + 1
        task_log = self.task_log.bind(document_id=doc_id, task_id=task_id, attempt=attempt)
        final_status = DocumentStatus.ERROR
        final_chunk_count = None
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
                    final_chunk_count = await asyncio.wait_for(
                        async_process_flow(task_id=task_id, attempt=attempt, **kwargs),
                        timeout=TIMEOUT_SECONDS
                    )
                    final_status = DocumentStatus.PROCESSED
                    error_to_report = None
                    task_log.info("Async process flow completed successfully.", chunks_processed=final_chunk_count)

                 except asyncio.TimeoutError as e:
                     task_log.error("Processing timed out", timeout=TIMEOUT_SECONDS)
                     error_to_report = f"Processing timed out after {TIMEOUT_SECONDS} seconds."
                     final_status = DocumentStatus.ERROR
                     processing_exception = e
                 except ValueError as e:
                      task_log.error("Processing failed due to value error", error=str(e))
                      error_to_report = f"Configuration or Input Error: {e}"
                      final_status = DocumentStatus.ERROR
                      processing_exception = e
                 except RuntimeError as e:
                      task_log.error(f"Processing failed with runtime error: {e}", exc_info=False)
                      error_to_report = f"Processing Runtime Error: {e}"
                      final_status = DocumentStatus.ERROR
                      processing_exception = e
                 except ConnectionError as e:
                      task_log.critical("Persistent DB connection error during status update", error=str(e))
                      raise Reject(f"Persistent DB error for {doc_id}: {e}", requeue=False) from e
                 except Exception as e:
                     task_log.exception("Unexpected exception during processing flow.", error=str(e))
                     error_to_report = f"Unexpected error during processing: {type(e).__name__}"
                     final_status = DocumentStatus.ERROR
                     processing_exception = e

                 task_log.info("Attempting to update final document status in DB", status=final_status.value, chunks=final_chunk_count, error=error_to_report)
                 try:
                     await self._update_status_with_retry(
                         pool, doc_id, final_status,
                         chunk_count=final_chunk_count if final_status == DocumentStatus.PROCESSED else None,
                         error_msg=error_to_report
                     )
                 except ConnectionError as db_update_exc:
                      task_log.critical("CRITICAL: Failed to update final document status in DB!", target_status=final_status.value, error=str(db_update_exc))
                      raise Reject(f"Final DB update failed for {doc_id}: {db_update_exc}", requeue=False) from db_update_exc
                 except Exception as db_unhandled_exc:
                      task_log.critical("CRITICAL: Unhandled exception during final DB update", error=str(db_unhandled_exc), exc_info=True)
                      raise Reject(f"Unhandled final DB update error for {doc_id}", requeue=False) from db_unhandled_exc


        except Reject as r:
             task_log.error(f"Task rejected: {r.reason}")
             raise r
        except Exception as outer_exc:
             task_log.exception("Outer exception caught in run_async_processing", error=str(outer_exc))
             processing_exception = outer_exc
             final_status = DocumentStatus.ERROR


        if final_status == DocumentStatus.ERROR and processing_exception:
             is_retryable = not isinstance(processing_exception, (ValueError, RuntimeError, asyncio.TimeoutError, Reject, ConnectionError))
             if is_retryable:
                 task_log.warning("Processing failed with a potentially retryable error, attempting task retry.", error=str(processing_exception))
                 try:
                     if self.request:
                         raise self.retry(exc=processing_exception, countdown=self.default_retry_delay * attempt)
                     else:
                         task_log.error("Cannot retry task: self.request is not available.")
                         raise Reject(f"Cannot retry {doc_id}, request context unavailable", requeue=False) from processing_exception
                 except MaxRetriesExceededError:
                     task_log.error("Max retries exceeded for task.", error=str(processing_exception))
                     raise Reject(f"Max retries exceeded for {doc_id}", requeue=False) from processing_exception
                 except Reject as r:
                      task_log.error("Task rejected during retry attempt.", reason=str(r))
                      raise r
                 except Exception as retry_exc:
                     task_log.exception("Exception occurred during task retry mechanism", error=str(retry_exc))
                     raise Reject(f"Retry mechanism failed for {doc_id}", requeue=False) from retry_exc
             else:
                 task_log.error("Processing failed with non-retryable error.", error=str(processing_exception), type=type(processing_exception).__name__)
                 raise Reject(f"Non-retryable error for {doc_id}: {processing_exception}", requeue=False) from processing_exception

        elif final_status == DocumentStatus.PROCESSED:
              task_log.info("Processing completed successfully for document.")
              return {"status": "processed", "document_id": doc_id, "chunks_processed": final_chunk_count}
        else:
             task_log.error("Task ended in unexpected state", final_status=final_status, error=error_to_report)
             raise Reject(f"Task for {doc_id} ended in unexpected state {final_status}", requeue=False)

    def run(self, *args, **kwargs):
        """Synchronous wrapper to run the async processing logic."""
        task_log = log.bind(task_id=self.request.id, task_name=self.name)
        task_log.info("Task received", args=args, kwargs=list(kwargs.keys()))
        try:
            return asyncio.run(self.run_async_processing(*args, **kwargs))
        except Reject as r:
             task_log.error(f"Task rejected: {r.reason}", exc_info=False)
             raise r
        except Ignore:
             task_log.warning("Task is being ignored.")
             raise Ignore()
        except Exception as e:
             task_log.exception("Task failed with unhandled exception in run wrapper", error=str(e))
             raise e

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Log task failure."""
        failure_log = log.bind(task_id=task_id, task_name=self.name, status="FAILED")
        reason = getattr(exc, 'reason', str(exc))
        failure_log.error(
            "Celery task final failure",
            args=args, kwargs=kwargs, error_type=type(exc).__name__, error=reason,
            traceback=str(einfo.traceback) if einfo else "No traceback info",
            exc_info=False
        )

    def on_success(self, retval, task_id, args, kwargs):
        """Log task success."""
        success_log = log.bind(task_id=task_id, task_name=self.name, status="SUCCESS")
        success_log.info(
            "Celery task completed successfully",
            args=args, kwargs=kwargs, retval=retval
        )

# Register the custom task class with Celery *ONCE*
process_document_haystack_task = celery_app.register_task(ProcessDocumentTask())
```

## File: `pyproject.toml`
```toml
# ingest-service/pyproject.toml
[tool.poetry]
name = "ingest-service"
version = "0.1.3" # Increment version for GPU changes
description = "Ingest service for Atenex B2B SaaS (Haystack/Postgres/Minio/Milvus/FastEmbed)" # Update description
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
gevent = "^23.9.1"
asyncpg = "^0.29.0"
tenacity = "^8.2.3"
python-multipart = "^0.0.9"
structlog = "^24.1.0"
minio = "^7.1.17"

# --- Haystack Dependencies ---
haystack-ai = "^2.0.1" # Or the stable version you use for Haystack 2.x
# openai = "^1.14.3" # Keep if used elsewhere, remove if only for embedding
pymilvus = "^2.4.1"
milvus-haystack = "^0.0.6"

# --- Haystack Converter Dependencies ---
pypdf = "^4.0.1"
python-docx = "^1.1.0"
markdown = "^3.5.1"
beautifulsoup4 = "^4.12.3"

# >>>>>>>>>>> CORRECTION: Add FastEmbed and ONNX GPU Dependencies <<<<<<<<<<<<<<
# --- FastEmbed & GPU Dependencies ---
fastembed-haystack = "^1.3.0"    # Haystack integration for FastEmbed
onnxruntime = "^1.21.1" # <-- LÍNEA ELIMINADA/COMENTADA
onnxruntime-gpu = "^1.17.0"      # ONNX Runtime with GPU support (ensure version compatibility)
# >>>>>>>>>>> END CORRECTION <<<<<<<<<<<<<<

# --- HTTP Client ---
httpx = {extras = ["http2"], version = "^0.27.0"}
h2 = "^4.1.0"

[tool.poetry.group.dev.dependencies]
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"
# httpx is already in main dependencies

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
