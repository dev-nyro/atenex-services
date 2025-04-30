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
│   ├── ingest_pipeline.py
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

# --- pymilvus Imports (Replaces Haystack Milvus imports) ---
from pymilvus import Collection, connections, utility, MilvusException

# --- Custom Application Imports ---
from app.core.config import settings
from app.db import postgres_client as db_client
from app.models.domain import DocumentStatus
from app.api.v1.schemas import IngestResponse, StatusResponse, PaginatedStatusResponse, ErrorDetail
from app.services.minio_client import MinioClient, MinioError
from app.tasks.celery_app import celery_app
# Import the specific task instance registered in process_document.py
# Note: The task name might change if you renamed the function in process_document.py
from app.tasks.process_document import process_document_standalone as process_document_task # Use the new name
# Import Milvus field name constants for consistency
from app.services.ingest_pipeline import (
    MILVUS_COLLECTION_NAME,
    MILVUS_COMPANY_ID_FIELD,
    MILVUS_DOCUMENT_ID_FIELD,
)

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
        raise HTTPException(status_code=503, detail="Database connection unavailable.")
    finally:
        if conn and pool:
            await pool.release(conn)


# --- Milvus Synchronous Helper Functions (Refactored to use pymilvus) ---

def _get_milvus_collection_sync() -> Collection:
    """Synchronously connects to Milvus and returns the Collection object."""
    alias = "api_sync_helper" # Use a different alias for API helpers if desired
    sync_milvus_log = log.bind(component="MilvusHelperSync", alias=alias)

    # Check if connection exists for this alias
    if alias not in connections.list_connections():
        sync_milvus_log.info("Connecting to Milvus for sync helper...")
        try:
            connections.connect(
                alias=alias,
                uri=settings.MILVUS_URI,
                timeout=settings.MILVUS_GRPC_TIMEOUT
            )
            sync_milvus_log.info("Milvus connection established for sync helper.")
        except MilvusException as e:
            sync_milvus_log.error("Failed to connect to Milvus for sync helper", error=str(e))
            raise RuntimeError(f"Milvus connection failed for API helper: {e}") from e
    else:
        sync_milvus_log.debug("Reusing existing Milvus connection for sync helper.")

    # Get the collection object
    try:
        collection = Collection(name=MILVUS_COLLECTION_NAME, using=alias)
        # Optional: Check if collection exists if creation isn't guaranteed by worker
        # if not utility.has_collection(MILVUS_COLLECTION_NAME, using=alias):
        #     log.error("Milvus collection not found by API helper", collection_name=MILVUS_COLLECTION_NAME)
        #     raise RuntimeError(f"Collection '{MILVUS_COLLECTION_NAME}' not found.")

        # Always load collection into memory (necessary for sync queries)
        sync_milvus_log.info("Loading Milvus collection for sync helper...")
        collection.load()
        sync_milvus_log.info("Milvus collection loaded for sync helper.")

        return collection
    except MilvusException as e:
        sync_milvus_log.error("Failed to get or load Milvus collection for sync helper", error=str(e))
        raise RuntimeError(f"Milvus collection access error for API helper: {e}") from e


def _get_milvus_chunk_count_sync(document_id: str, company_id: str) -> int:
    """Synchronously counts chunks in Milvus for a specific document using pymilvus query."""
    count_log = log.bind(document_id=document_id, company_id=company_id, component="MilvusHelperSync")
    try:
        collection = _get_milvus_collection_sync()
        # Construct the filter expression using imported constants
        expr = f'{MILVUS_COMPANY_ID_FIELD} == "{company_id}" and {MILVUS_DOCUMENT_ID_FIELD} == "{document_id}"'
        count_log.debug("Attempting to query Milvus chunk count", filter_expr=expr)

        # Query for PK field and count results (workaround for unsupported count(*))
        query_res = collection.query(
            expr=expr,
            output_fields=["pk_id"]
        )
        count = len(query_res) if query_res else 0
        count_log.info("Milvus chunk count successful (pymilvus)", count=count)
        return count
    except RuntimeError as re: # Catch collection/connection error
        count_log.error("Failed to get Milvus count due to connection/collection error", error=str(re))
        return -1
    except MilvusException as e:
        count_log.error("Milvus query error during count", error=str(e), exc_info=True)
        return -1 # Indicate error
    except Exception as e:
        count_log.exception("Unexpected error during Milvus count", error=str(e))
        return -1


def _delete_milvus_sync(document_id: str, company_id: str) -> bool:
    """Synchronously deletes chunks from Milvus using pymilvus delete."""
    delete_log = log.bind(document_id=document_id, company_id=company_id, component="MilvusHelperSync")
    try:
        collection = _get_milvus_collection_sync()
        # Construct the filter expression
        expr = f'{MILVUS_COMPANY_ID_FIELD} == "{company_id}" and {MILVUS_DOCUMENT_ID_FIELD} == "{document_id}"'
        delete_log.info("Attempting to delete chunks from Milvus (pymilvus)", filter_expr=expr)

        delete_result = collection.delete(expr=expr)
        delete_log.info("Milvus delete operation executed (pymilvus)", deleted_count=delete_result.delete_count)
        # Consider success even if delete_count is 0 (nothing to delete)
        return True
    except RuntimeError as re: # Catch collection/connection error
        delete_log.error("Failed to delete Milvus chunks due to connection/collection error", error=str(re))
        return False
    except MilvusException as e:
        delete_log.error("Milvus delete error", error=str(e), exc_info=True)
        return False
    except Exception as e:
        delete_log.exception("Unexpected error during Milvus delete", error=str(e))
        return False


# --- API Endpoints (Logic remains largely the same, but task name might change) ---

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
@router.post(
    "/ingest/upload",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    include_in_schema=False
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
    """
    company_id = request.headers.get("X-Company-ID")
    user_id = request.headers.get("X-User-ID")
    req_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    endpoint_log = log.bind(request_id=req_id)

    if not company_id:
        endpoint_log.warning("Missing X-Company-ID header")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")
    try: company_uuid = uuid.UUID(company_id)
    except ValueError: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")

    if not user_id:
        endpoint_log.warning("Missing X-User-ID header")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-User-ID")

    endpoint_log = endpoint_log.bind(company_id=company_id, user_id=user_id,
                                     filename=file.filename, content_type=file.content_type)
    endpoint_log.info("Processing document ingestion request from gateway")

    if file.content_type not in settings.SUPPORTED_CONTENT_TYPES:
        endpoint_log.warning("Unsupported content type received")
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type: {file.content_type}. Supported types: {', '.join(settings.SUPPORTED_CONTENT_TYPES)}"
        )

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
            elif existing_doc:
                 endpoint_log.info("Found existing document in error state, proceeding with upload.", document_id=existing_doc['id'])
    except HTTPException as http_exc: raise http_exc
    except Exception as e:
        endpoint_log.exception("Error checking for duplicate document", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error checking for duplicates.")

    document_id = uuid.uuid4()
    file_path_in_storage = f"{company_id}/{document_id}/{file.filename}"

    try:
        async with get_db_conn() as conn:
            await api_db_retry_strategy(db_client.create_document_record)(
                conn=conn, doc_id=document_id, company_id=company_uuid,
                filename=file.filename, file_type=file.content_type,
                file_path=file_path_in_storage, status=DocumentStatus.PENDING,
                metadata=metadata
            )
        endpoint_log.info("Document record created in PostgreSQL", document_id=str(document_id))
    except Exception as e:
        endpoint_log.exception("Failed to create document record in PostgreSQL", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error creating record.")

    try:
        file_content = await file.read()
        endpoint_log.debug(f"MinIO upload object_name repr: {repr(file_path_in_storage)}")
        await minio_client.upload_file_async(
            object_name=file_path_in_storage, data=file_content, content_type=file.content_type
        )
        endpoint_log.info("File uploaded successfully to MinIO", object_name=file_path_in_storage)
        async with get_db_conn() as conn:
            await api_db_retry_strategy(db_client.update_document_status)(
                document_id=document_id, status=DocumentStatus.UPLOADED, conn=conn # Use keyword arg for conn
            )
        endpoint_log.info("Document status updated to 'uploaded'", document_id=str(document_id))
    except MinioError as me:
        endpoint_log.error("Failed to upload file to MinIO", object_name=file_path_in_storage, error=str(me))
        try:
            async with get_db_conn() as conn:
                await api_db_retry_strategy(db_client.update_document_status)(
                    document_id=document_id, status=DocumentStatus.ERROR, error_message=f"MinIO upload failed: {me}", conn=conn
                )
        except Exception as db_err:
            endpoint_log.exception("Failed to update status to ERROR after MinIO failure", error=str(db_err))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Storage service error: {me}")
    except Exception as e:
         endpoint_log.exception("Unexpected error during file upload or DB update", error=str(e))
         try:
             async with get_db_conn() as conn:
                 await api_db_retry_strategy(db_client.update_document_status)(
                     document_id=document_id, status=DocumentStatus.ERROR, error_message=f"Unexpected upload error: {type(e).__name__}", conn=conn
                 )
         except Exception as db_err:
             endpoint_log.exception("Failed to update status to ERROR after unexpected upload failure", error=str(db_err))
         raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Internal server error during upload.")
    finally:
        await file.close()

    try:
        task_payload = {
            "document_id": str(document_id), "company_id": company_id,
            "filename": file.filename, "content_type": file.content_type
        }
        # Use the imported task instance (now points to process_document_standalone)
        task = process_document_task.delay(**task_payload)
        endpoint_log.info("Document ingestion task queued successfully", task_id=task.id, task_name=process_document_task.name)
    except Exception as e:
        endpoint_log.exception("Failed to queue Celery task", error=str(e))
        try:
            async with get_db_conn() as conn:
                await api_db_retry_strategy(db_client.update_document_status)(
                    document_id=document_id, status=DocumentStatus.ERROR, error_message=f"Failed to queue processing task: {e}", conn=conn
                )
        except Exception as db_err:
            endpoint_log.exception("Failed to update status to ERROR after Celery failure", error=str(db_err))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to queue processing task.")

    return IngestResponse(
        document_id=str(document_id), task_id=task.id,
        status=DocumentStatus.UPLOADED.value,
        message="Document upload accepted, processing started."
    )


@router.get(
    "/status/{document_id}",
    response_model=StatusResponse,
    summary="Get the status of a specific document with live checks",
    responses={
        404: {"model": ErrorDetail, "description": "Document not found"},
        422: {"model": ErrorDetail, "description": "Validation Error (Missing Headers or Invalid ID)"},
        500: {"model": ErrorDetail, "description": "Internal Server Error"},
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB, MinIO, Milvus)"},
    }
)
@router.get(
    "/ingest/status/{document_id}",
    response_model=StatusResponse,
    include_in_schema=False
)
async def get_document_status(
    request: Request,
    document_id: uuid.UUID = Path(..., description="The UUID of the document"),
    minio_client: MinioClient = Depends(get_minio_client),
):
    """
    Retrieves document status from PostgreSQL and performs live MinIO/Milvus checks.
    Uses refactored pymilvus helpers for counting.
    """
    company_id = request.headers.get("X-Company-ID")
    req_id = getattr(request.state, 'request_id', 'N/A')
    if not company_id:
        log.bind(request_id=req_id).warning("Missing X-Company-ID header in get_document_status")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")
    try: company_uuid = uuid.UUID(company_id)
    except ValueError: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")

    status_log = log.bind(request_id=req_id, document_id=str(document_id), company_id=company_id)
    status_log.info("Request received for document status")

    doc_data: Optional[Dict[str, Any]] = None
    try:
        async with get_db_conn() as conn:
             doc_data = await api_db_retry_strategy(db_client.get_document_by_id)(
                 conn, doc_id=document_id, company_id=company_uuid
             )
        if not doc_data:
            status_log.warning("Document not found in DB for this company")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
        status_log.info("Retrieved base document data from DB", status=doc_data['status'])
    except HTTPException as http_exc: raise http_exc
    except Exception as e:
        status_log.exception("Error fetching document status from DB", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error fetching status.")

    needs_update = False
    current_status_enum = DocumentStatus(doc_data['status'])
    current_chunk_count = doc_data.get('chunk_count')
    current_error_message = doc_data.get('error_message')
    updated_status_enum = current_status_enum
    updated_chunk_count = current_chunk_count
    updated_error_message = current_error_message

    minio_path = doc_data.get('file_path')
    minio_exists = False
    if not minio_path:
         status_log.warning("MinIO file path missing in DB record", db_id=doc_data['id'])
         if updated_status_enum not in [DocumentStatus.ERROR, DocumentStatus.PENDING]:
             needs_update = True
             updated_status_enum = DocumentStatus.ERROR
             updated_error_message = (updated_error_message or "") + " File path missing."
    else:
        status_log.debug("Checking MinIO for file existence", object_name=minio_path)
        try:
            minio_exists = await minio_client.check_file_exists_async(minio_path)
            status_log.info("MinIO existence check complete", exists=minio_exists)
            if not minio_exists and updated_status_enum not in [DocumentStatus.ERROR, DocumentStatus.PENDING]:
                 status_log.warning("File missing in MinIO but DB status suggests otherwise.", current_db_status=updated_status_enum.value)
                 if updated_status_enum != DocumentStatus.ERROR:
                     needs_update = True
                     updated_status_enum = DocumentStatus.ERROR
                     updated_error_message = "File missing from storage."
                     updated_chunk_count = 0
        except Exception as minio_e:
            status_log.error("MinIO check failed", error=str(minio_e))
            minio_exists = False
            if updated_status_enum != DocumentStatus.ERROR:
                 needs_update = True
                 updated_status_enum = DocumentStatus.ERROR
                 updated_error_message = (updated_error_message or "") + f" MinIO check error ({type(minio_e).__name__})."
                 updated_chunk_count = 0

    status_log.debug("Checking Milvus for chunk count using pymilvus helper...")
    loop = asyncio.get_running_loop()
    milvus_chunk_count = -1
    try:
        # Use the refactored pymilvus helper ran in executor
        milvus_chunk_count = await loop.run_in_executor(
            None, _get_milvus_chunk_count_sync, str(document_id), company_id
        )
        status_log.info("Milvus chunk count check complete (pymilvus)", count=milvus_chunk_count)

        if milvus_chunk_count == -1:
            status_log.error("Milvus count check failed (returned -1).")
            if updated_status_enum != DocumentStatus.ERROR:
                needs_update = True
                updated_status_enum = DocumentStatus.ERROR
                updated_error_message = (updated_error_message or "") + " Failed Milvus count check."
        elif milvus_chunk_count > 0:
            if updated_status_enum == DocumentStatus.UPLOADED:
                status_log.warning("Inconsistency: Chunks found but DB status is 'uploaded'. Correcting.")
                needs_update = True
                updated_status_enum = DocumentStatus.PROCESSED
                updated_chunk_count = milvus_chunk_count
                updated_error_message = None
            elif updated_status_enum == DocumentStatus.PROCESSED:
                if updated_chunk_count != milvus_chunk_count:
                     status_log.warning("Inconsistency: DB chunk count differs from live Milvus count.", db_count=updated_chunk_count, live_count=milvus_chunk_count)
                     needs_update = True
                     updated_chunk_count = milvus_chunk_count
        elif milvus_chunk_count == 0:
            if updated_status_enum == DocumentStatus.PROCESSED:
                 status_log.warning("Inconsistency: DB status 'processed' but no chunks found. Correcting.")
                 needs_update = True
                 updated_status_enum = DocumentStatus.ERROR
                 updated_chunk_count = 0
                 updated_error_message = (updated_error_message or "") + " Processed data missing."
            elif updated_status_enum == DocumentStatus.ERROR and updated_chunk_count != 0:
                needs_update = True
                updated_chunk_count = 0

    except Exception as e:
        status_log.exception("Unexpected error during Milvus count check execution", error=str(e))
        milvus_chunk_count = -1
        if updated_status_enum != DocumentStatus.ERROR:
            needs_update = True
            updated_status_enum = DocumentStatus.ERROR
            updated_error_message = (updated_error_message or "") + f" Error checking Milvus ({type(e).__name__})."

    if needs_update:
        status_log.warning("Inconsistency detected, updating document status in DB",
                           new_status=updated_status_enum.value, new_count=updated_chunk_count)
        try:
             async with get_db_conn() as conn:
                 await api_db_retry_strategy(db_client.update_document_status)(
                     document_id=document_id, status=updated_status_enum,
                     chunk_count=updated_chunk_count, error_message=updated_error_message,
                     conn=conn # Pass connection
                 )
             status_log.info("Document status updated successfully in DB.")
             doc_data['status'] = updated_status_enum.value
             doc_data['chunk_count'] = updated_chunk_count
             doc_data['error_message'] = updated_error_message
        except Exception as e:
            status_log.exception("Failed to update document status in DB after inconsistency check", error=str(e))

    final_status_val = doc_data['status']
    final_chunk_count_val = doc_data.get('chunk_count', 0)
    final_error_message_val = doc_data.get('error_message')

    return StatusResponse(
        document_id=str(doc_data['id']), company_id=str(doc_data['company_id']),
        status=final_status_val, file_name=doc_data.get('file_name'),
        file_type=doc_data.get('file_type'), file_path=doc_data.get('file_path'),
        chunk_count=final_chunk_count_val, minio_exists=minio_exists,
        milvus_chunk_count=milvus_chunk_count, last_updated=doc_data.get('updated_at'),
        uploaded_at=doc_data.get('uploaded_at'), error_message=final_error_message_val,
        metadata=doc_data.get('metadata')
    )


@router.get(
    "/status",
    response_model=List[StatusResponse],
    summary="List document statuses with pagination and live checks",
    responses={
        422: {"model": ErrorDetail, "description": "Validation Error (Missing Headers)"},
        500: {"model": ErrorDetail, "description": "Internal Server Error"},
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB, MinIO, Milvus)"},
    }
)
@router.get(
    "/ingest/status",
    response_model=List[StatusResponse],
    include_in_schema=False
)
async def list_document_statuses(
    request: Request,
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    minio_client: MinioClient = Depends(get_minio_client),
):
    """
    Lists documents for the company with pagination. Performs live MinIO/Milvus checks.
    Uses refactored pymilvus helpers.
    """
    company_id = request.headers.get("X-Company-ID")
    req_id = getattr(request.state, 'request_id', 'N/A')
    if not company_id:
        log.bind(request_id=req_id).warning("Missing X-Company-ID header in list_document_statuses")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")
    try: company_uuid = uuid.UUID(company_id)
    except ValueError: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")

    list_log = log.bind(request_id=req_id, company_id=company_id, limit=limit, offset=offset)
    list_log.info("Listing document statuses with real-time checks")

    documents_db: List[Dict[str, Any]] = []
    total_db_count: int = 0
    try:
        async with get_db_conn() as conn:
            documents_db, total_db_count = await api_db_retry_strategy(db_client.list_documents_paginated)(
                conn, company_id=company_uuid, limit=limit, offset=offset
            )
        list_log.info("Retrieved documents from DB", count=len(documents_db), total_db_count=total_db_count)
    except Exception as e:
        list_log.exception("Error listing documents from DB", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error listing documents.")

    if not documents_db: return []

    async def check_single_document(doc_db_data: Dict[str, Any]) -> Dict[str, Any]:
        check_log = log.bind(request_id=req_id, document_id=str(doc_db_data['id']), company_id=company_id)
        doc_id_str = str(doc_db_data['id'])
        doc_needs_update = False
        doc_current_status_enum = DocumentStatus(doc_db_data['status'])
        doc_current_chunk_count = doc_db_data.get('chunk_count')
        doc_current_error_msg = doc_db_data.get('error_message')
        doc_updated_status_enum = doc_current_status_enum
        doc_updated_chunk_count = doc_current_chunk_count
        doc_updated_error_msg = doc_current_error_msg

        minio_path_db = doc_db_data.get('file_path')
        live_minio_exists = False
        if minio_path_db:
            try:
                live_minio_exists = await minio_client.check_file_exists_async(minio_path_db)
                if not live_minio_exists and doc_updated_status_enum not in [DocumentStatus.ERROR, DocumentStatus.PENDING]:
                     if doc_updated_status_enum != DocumentStatus.ERROR:
                         doc_needs_update=True; doc_updated_status_enum=DocumentStatus.ERROR; doc_updated_error_msg="File missing from storage."; doc_updated_chunk_count=0
            except Exception as e:
                check_log.error("MinIO check failed for list item", error=str(e)); live_minio_exists = False
                if doc_updated_status_enum != DocumentStatus.ERROR:
                    doc_needs_update=True; doc_updated_status_enum=DocumentStatus.ERROR; doc_updated_error_msg=(doc_updated_error_msg or "")+f" MinIO check error."; doc_updated_chunk_count=0
        else: live_minio_exists = False

        live_milvus_chunk_count = -1
        loop = asyncio.get_running_loop()
        try:
            live_milvus_chunk_count = await loop.run_in_executor(None, _get_milvus_chunk_count_sync, doc_id_str, company_id)
            if live_milvus_chunk_count == -1:
                 if doc_updated_status_enum != DocumentStatus.ERROR:
                     doc_needs_update=True; doc_updated_status_enum=DocumentStatus.ERROR; doc_updated_error_msg=(doc_updated_error_msg or "")+" Failed Milvus count."
            elif live_milvus_chunk_count > 0:
                 if doc_updated_status_enum == DocumentStatus.UPLOADED:
                     doc_needs_update=True; doc_updated_status_enum=DocumentStatus.PROCESSED; doc_updated_chunk_count=live_milvus_chunk_count; doc_updated_error_msg=None
                 elif doc_updated_status_enum == DocumentStatus.PROCESSED and doc_updated_chunk_count != live_milvus_chunk_count:
                     doc_needs_update=True; doc_updated_chunk_count=live_milvus_chunk_count
            elif live_milvus_chunk_count == 0:
                 if doc_updated_status_enum == DocumentStatus.PROCESSED:
                     doc_needs_update=True; doc_updated_status_enum=DocumentStatus.ERROR; doc_updated_chunk_count=0; doc_updated_error_msg=(doc_updated_error_msg or "")+" Processed data missing."
                 elif doc_updated_status_enum == DocumentStatus.ERROR and doc_updated_chunk_count != 0:
                     doc_needs_update=True; doc_updated_chunk_count=0
        except Exception as e:
            check_log.exception("Unexpected error during Milvus count check for list item", error=str(e)); live_milvus_chunk_count = -1
            if doc_updated_status_enum != DocumentStatus.ERROR:
                doc_needs_update=True; doc_updated_status_enum=DocumentStatus.ERROR; doc_updated_error_msg=(doc_updated_error_msg or "")+f" Error checking Milvus."

        return {
            "db_data": doc_db_data, "needs_update": doc_needs_update,
            "updated_status_enum": doc_updated_status_enum, "updated_chunk_count": doc_updated_chunk_count,
            "final_error_message": doc_updated_error_msg, "live_minio_exists": live_minio_exists,
            "live_milvus_chunk_count": live_milvus_chunk_count
        }

    list_log.info(f"Performing live checks for {len(documents_db)} documents concurrently...")
    check_tasks = [check_single_document(doc) for doc in documents_db]
    check_results = await asyncio.gather(*check_tasks)
    list_log.info("Live checks completed.")

    updated_doc_data_map = {}
    docs_to_update_in_db = []
    for result in check_results:
        doc_id_str = str(result["db_data"]["id"])
        updated_doc_data_map[doc_id_str] = {
            **result["db_data"], "status": result["updated_status_enum"].value,
            "chunk_count": result["updated_chunk_count"], "error_message": result["final_error_message"]
        }
        if result["needs_update"]:
            docs_to_update_in_db.append({
                "id": result["db_data"]["id"], "status_enum": result["updated_status_enum"],
                "chunk_count": result["updated_chunk_count"], "error_message": result["final_error_message"]
            })

    if docs_to_update_in_db:
        list_log.warning("Updating statuses sequentially in DB for inconsistent documents", count=len(docs_to_update_in_db))
        updated_count = 0; failed_update_count = 0
        try:
             async with get_db_conn() as conn:
                 for update_info in docs_to_update_in_db:
                     try:
                         await api_db_retry_strategy(db_client.update_document_status)(
                             conn=conn, document_id=update_info["id"], status=update_info["status_enum"],
                             chunk_count=update_info["chunk_count"], error_message=update_info["error_message"]
                         )
                         updated_count += 1
                     except Exception as single_update_err:
                         failed_update_count += 1
                         list_log.error("Failed DB update for single doc during list check", document_id=str(update_info["id"]), error=str(single_update_err))
             list_log.info("Finished sequential DB updates.", updated=updated_count, failed=failed_update_count)
        except Exception as bulk_db_conn_err:
            list_log.exception("Error acquiring DB connection for sequential updates", error=str(bulk_db_conn_err))

    final_response_items = []
    for result in check_results:
         doc_id_str = str(result["db_data"]["id"])
         current_data = updated_doc_data_map.get(doc_id_str, result["db_data"])
         final_response_items.append(StatusResponse(
            document_id=doc_id_str, company_id=str(current_data['company_id']),
            status=current_data['status'], file_name=current_data.get('file_name'),
            file_type=current_data.get('file_type'), file_path=current_data.get('file_path'),
            chunk_count=current_data.get('chunk_count', 0), minio_exists=result["live_minio_exists"],
            milvus_chunk_count=result["live_milvus_chunk_count"], last_updated=current_data.get('updated_at'),
            uploaded_at=current_data.get('uploaded_at'), error_message=current_data.get('error_message'),
            metadata=current_data.get('metadata')
         ))

    list_log.info("Returning enriched statuses", count=len(final_response_items))
    return final_response_items


@router.post(
    "/retry/{document_id}",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Retry ingestion for a document currently in 'error' state",
    responses={
        404: {"model": ErrorDetail, "description": "Document not found"},
        409: {"model": ErrorDetail, "description": "Document is not in 'error' state"},
        422: {"model": ErrorDetail, "description": "Validation Error (Missing Headers or Invalid ID)"},
        500: {"model": ErrorDetail, "description": "Internal Server Error"},
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB or Celery)"},
    }
)
@router.post(
    "/ingest/retry/{document_id}",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    include_in_schema=False
)
async def retry_ingestion(
    request: Request,
    document_id: uuid.UUID = Path(..., description="The UUID of the document to retry"),
):
    """
    Allows retrying the ingestion process for a document that previously failed.
    Uses the refactored task name.
    """
    company_id = request.headers.get("X-Company-ID")
    user_id = request.headers.get("X-User-ID")
    req_id = getattr(request.state, 'request_id', 'N/A')
    if not company_id: raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")
    try: company_uuid = uuid.UUID(company_id)
    except ValueError: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")
    if not user_id: raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-User-ID")

    retry_log = log.bind(request_id=req_id, document_id=str(document_id), company_id=company_id, user_id=user_id)
    retry_log.info("Received request to retry document ingestion")

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
             retry_log.warning("Document not in error state, cannot retry", current_status=doc_data['status'])
             raise HTTPException(
                 status_code=status.HTTP_409_CONFLICT,
                 detail=f"Document is not in 'error' state (current state: {doc_data['status']}). Cannot retry."
             )
         retry_log.info("Document found and confirmed to be in 'error' state.")
    except HTTPException as http_exc: raise http_exc
    except Exception as e:
        retry_log.exception("Error fetching document for retry", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error checking document for retry.")

    try:
        async with get_db_conn() as conn:
            await api_db_retry_strategy(db_client.update_document_status)(
                conn=conn, document_id=document_id, status=DocumentStatus.PROCESSING,
                chunk_count=None, error_message=None
            )
        retry_log.info("Document status updated to 'processing' for retry.")
    except Exception as e:
        retry_log.exception("Failed to update document status for retry", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error updating status for retry.")

    try:
        file_name_from_db = doc_data.get('file_name')
        content_type_from_db = doc_data.get('file_type')
        if not file_name_from_db or not content_type_from_db:
             retry_log.error("File name or type missing in DB data for retry task", document_id=str(document_id))
             raise HTTPException(status_code=500, detail="Internal error: Missing file name or type for retry.")

        task_payload = {
            "document_id": str(document_id), "company_id": company_id,
            "filename": file_name_from_db, "content_type": content_type_from_db
        }
        # Use the correct task instance variable
        task = process_document_task.delay(**task_payload)
        retry_log.info("Document reprocessing task queued successfully", task_id=task.id, task_name=process_document_task.name)
    except Exception as e:
        retry_log.exception("Failed to re-queue Celery task for retry", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to queue reprocessing task.")

    return IngestResponse(
        document_id=str(document_id), task_id=task.id,
        status=DocumentStatus.PROCESSING.value,
        message="Document retry accepted, processing started."
    )


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document and its associated data",
    responses={
        404: {"model": ErrorDetail, "description": "Document not found"},
        422: {"model": ErrorDetail, "description": "Validation Error (Missing Headers or Invalid ID)"},
        500: {"model": ErrorDetail, "description": "Internal Server Error"},
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB, MinIO, Milvus)"},
    }
)
@router.delete(
    "/ingest/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    include_in_schema=False
)
async def delete_document_endpoint(
    request: Request,
    document_id: uuid.UUID = Path(..., description="The UUID of the document to delete"),
    minio_client: MinioClient = Depends(get_minio_client),
):
    """
    Deletes a document: removes from Milvus (using pymilvus helper), MinIO, and PostgreSQL.
    """
    company_id = request.headers.get("X-Company-ID")
    req_id = getattr(request.state, 'request_id', 'N/A')
    if not company_id:
        log.bind(request_id=req_id).warning("Missing X-Company-ID header in delete_document_endpoint")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")
    try: company_uuid = uuid.UUID(company_id)
    except ValueError: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")

    delete_log = log.bind(request_id=req_id, document_id=str(document_id), company_id=company_id)
    delete_log.info("Received request to delete document")

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
    except HTTPException as http_exc: raise http_exc
    except Exception as e:
        delete_log.exception("Error verifying document before deletion", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error during delete verification.")

    errors = []

    # --- 1. Delete from Milvus (using refactored pymilvus helper) ---
    delete_log.info("Attempting to delete chunks from Milvus (pymilvus)...")
    loop = asyncio.get_running_loop()
    try:
        # Run the refactored synchronous pymilvus delete helper in executor
        milvus_deleted = await loop.run_in_executor(None, _delete_milvus_sync, str(document_id), company_id)
        if milvus_deleted: delete_log.info("Milvus delete command executed successfully (pymilvus).")
        else:
            errors.append("Failed Milvus delete (pymilvus helper returned False)")
            delete_log.warning("Milvus delete operation reported failure (check helper logs).")
    except Exception as e:
        delete_log.exception("Unexpected error during Milvus delete execution", error=str(e))
        errors.append(f"Milvus delete exception: {type(e).__name__}")

    # --- 2. Delete from MinIO ---
    minio_path = doc_data.get('file_path')
    if minio_path:
        delete_log.info("Attempting to delete file from MinIO...", object_name=minio_path)
        try:
            await minio_client.delete_file_async(minio_path)
            delete_log.info("Successfully deleted file from MinIO.")
        except MinioError as me:
            delete_log.error("Failed to delete file from MinIO", object_name=minio_path, error=str(me))
            errors.append(f"MinIO delete failed: {me}")
        except Exception as e:
            delete_log.exception("Unexpected error during MinIO delete", error=str(e))
            errors.append(f"MinIO delete exception: {type(e).__name__}")
    else:
        delete_log.warning("Skipping MinIO delete: file path not found in DB record.")
        errors.append("MinIO path unknown in DB.")

    # --- 3. Delete from PostgreSQL ---
    delete_log.info("Attempting to delete record from PostgreSQL...")
    try:
         async with get_db_conn() as conn:
            deleted_in_db = await api_db_retry_strategy(db_client.delete_document)(
                conn=conn, doc_id=document_id, company_id=company_uuid
            )
            if deleted_in_db: delete_log.info("Document record deleted successfully from PostgreSQL")
            else: delete_log.warning("PostgreSQL delete command executed but no record was deleted.")
    except Exception as e:
        delete_log.exception("CRITICAL: Failed to delete document record from PostgreSQL", error=str(e))
        error_detail = f"Deleted/Attempted delete from storage/vectors (errors: {', '.join(errors)}) but FAILED to delete DB record: {e}"
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=error_detail)

    if errors: delete_log.warning("Document deletion process completed with non-critical errors", errors=errors)

    delete_log.info("Document deletion process finished.")
    return None # Return 204 No Content
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
# LLM_COMMENT: Update Milvus service name based on latest deployment
POSTGRES_K8S_SVC = "postgresql-service.nyro-develop.svc.cluster.local"
MINIO_K8S_SVC = "minio-service.nyro-develop.svc.cluster.local"
MILVUS_K8S_SVC = "milvus-standalone.nyro-develop.svc.cluster.local" # Updated Milvus service name
REDIS_K8S_SVC = "redis-service-master.nyro-develop.svc.cluster.local"

# --- Defaults ---
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex"
POSTGRES_K8S_USER_DEFAULT = "postgres"
MINIO_K8S_PORT_DEFAULT = 9000
MINIO_BUCKET_DEFAULT = "ingested-documents"
MILVUS_K8S_PORT_DEFAULT = 19530
MILVUS_DEFAULT_COLLECTION = "document_chunks_haystack"
# --- CORRECTION: Changed metric_type from COSINE to IP ---
MILVUS_DEFAULT_INDEX_PARAMS = '{"metric_type": "IP", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 256}}'
MILVUS_DEFAULT_SEARCH_PARAMS = '{"metric_type": "IP", "params": {"ef": 128}}' # Also update search metric
# ----------------------------------------------------------
DEFAULT_FASTEMBED_MODEL = "BAAI/bge-large-en-v1.5" # Default for bge-large
DEFAULT_FASTEMBED_DIM = 1024 # Default dimension for bge-large

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
    MILVUS_URI: str = Field(default=f"http://{MILVUS_K8S_SVC}:{MILVUS_K8S_PORT_DEFAULT}") # Updated default to use correct service name
    MILVUS_COLLECTION_NAME: str = MILVUS_DEFAULT_COLLECTION
    MILVUS_GRPC_TIMEOUT: int = 10 # Default timeout in seconds
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

    # --- Embeddings ---
    FASTEMBED_MODEL: str = DEFAULT_FASTEMBED_MODEL
    USE_GPU: bool = False
    EMBEDDING_DIMENSION: int = DEFAULT_FASTEMBED_DIM # Default matches FASTEMBED_MODEL
    OPENAI_API_KEY: Optional[SecretStr] = None

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
    SPLITTER_CHUNK_SIZE: int = 1000
    SPLITTER_CHUNK_OVERLAP: int = 250
    SPLITTER_SPLIT_BY: str = "word"

    # --- Validators ---
    @field_validator("LOG_LEVEL")
    @classmethod
    def check_log_level(cls, v: str) -> str:
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        normalized_v = v.upper()
        if normalized_v not in valid_levels: raise ValueError(f"Invalid LOG_LEVEL '{v}'. Must be one of {valid_levels}")
        return normalized_v

    @field_validator('EMBEDDING_DIMENSION')
    @classmethod
    def check_embedding_dimension(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0: raise ValueError("EMBEDDING_DIMENSION must be a positive integer.")
        logging.debug(f"Using EMBEDDING_DIMENSION: {v}")
        return v

    @field_validator('POSTGRES_PASSWORD', 'MINIO_ACCESS_KEY', 'MINIO_SECRET_KEY', mode='before')
    @classmethod
    def check_required_secret_value_present(cls, v: Any, info: ValidationInfo) -> Any:
        if v is None or v == "":
             field_name = info.field_name if info.field_name else "Unknown Secret Field"
             raise ValueError(f"Required secret field '{field_name}' cannot be empty.")
        return v

    @field_validator('MILVUS_URI', mode='before')
    @classmethod
    def validate_milvus_uri(cls, v: str) -> str:
        if not v.startswith("http://") and not v.startswith("https://"):
             # Allow relative paths for k8s service names implicitly
             if "." not in v: # Basic check if it might be a k8s service name
                 return f"http://{v}" # Assume http if no schema
             raise ValueError(f"Invalid MILVUS_URI format: '{v}'. Must start with 'http://' or 'https://' or be a valid service name.")
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
    temp_log.info(f"  MILVUS_GRPC_TIMEOUT:      {settings.MILVUS_GRPC_TIMEOUT}s")
    temp_log.info(f"  MINIO_ENDPOINT:           {settings.MINIO_ENDPOINT}")
    temp_log.info(f"  MINIO_BUCKET_NAME:        {settings.MINIO_BUCKET_NAME}")
    temp_log.info(f"  MINIO_ACCESS_KEY:         {'*** SET ***' if settings.MINIO_ACCESS_KEY else '!!! NOT SET !!!'}")
    temp_log.info(f"  MINIO_SECRET_KEY:         {'*** SET ***' if settings.MINIO_SECRET_KEY else '!!! NOT SET !!!'}")
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

## File: `app\services\ingest_pipeline.py`
```py
# ingest-service/app/services/ingest_pipeline.py
from __future__ import annotations

import os
import re
import pathlib
import uuid
import markdown # type: ignore
import html2text # type: ignore
import structlog
from typing import List, Dict, Any, Callable, Optional

# Direct library imports
from bs4 import BeautifulSoup
from pypdf import PdfReader
from docx import Document as DocxDocument
# LLM_FLAG: REMOVED - Global import and initialization of TextEmbedding removed from here.
from pymilvus import (
    Collection, CollectionSchema, FieldSchema, DataType, connections,
    utility, MilvusException
)
# LLM_FLAG: ADDED - Import only for type hinting within the function signature.
from fastembed.embedding import TextEmbedding

# Local application imports
from app.core.config import settings

log = structlog.get_logger(__name__)

# --------------- 1. TEXT EXTRACTION FUNCTIONS -----------------
# LLM_FLAG: NO_CHANGE - Text extraction functions remain the same.
def _extract_from_pdf(file_path: pathlib.Path) -> str:
    log.debug("Extracting text from PDF", path=str(file_path))
    try:
        reader = PdfReader(str(file_path))
        text_content = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        log.info("PDF extraction successful", path=str(file_path), num_pages=len(reader.pages))
        return text_content
    except Exception as e:
        log.error("Failed to extract text from PDF", path=str(file_path), error=str(e), exc_info=True)
        raise ValueError(f"Error processing PDF {file_path.name}: {e}") from e

def _extract_from_docx(file_path: pathlib.Path) -> str:
    log.debug("Extracting text from DOCX", path=str(file_path))
    try:
        doc = DocxDocument(str(file_path))
        text_content = "\n\n".join(p.text for p in doc.paragraphs if p.text)
        log.info("DOCX extraction successful", path=str(file_path), num_paragraphs=len(doc.paragraphs))
        return text_content
    except Exception as e:
        log.error("Failed to extract text from DOCX", path=str(file_path), error=str(e), exc_info=True)
        raise ValueError(f"Error processing DOCX {file_path.name}: {e}") from e

def _extract_from_html(file_path: pathlib.Path) -> str:
    log.debug("Extracting text from HTML", path=str(file_path))
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            soup = BeautifulSoup(f, "html.parser")
        for script_or_style in soup(["script", "style"]):
            script_or_style.decompose()
        text_content = soup.get_text(separator="\n", strip=True)
        log.info("HTML extraction successful", path=str(file_path))
        return text_content
    except Exception as e:
        log.error("Failed to extract text from HTML", path=str(file_path), error=str(e), exc_info=True)
        raise ValueError(f"Error processing HTML {file_path.name}: {e}") from e

def _extract_from_md(file_path: pathlib.Path) -> str:
    log.debug("Extracting text from Markdown", path=str(file_path))
    try:
        md_content = file_path.read_text(encoding="utf-8")
        html_content = markdown.markdown(md_content)
        h = html2text.HTML2Text()
        h.ignore_links = True
        h.ignore_images = True
        text_content = h.handle(html_content)
        log.info("Markdown extraction successful", path=str(file_path))
        return text_content
    except Exception as e:
        log.error("Failed to extract text from Markdown", path=str(file_path), error=str(e), exc_info=True)
        raise ValueError(f"Error processing Markdown {file_path.name}: {e}") from e

def _extract_from_txt(file_path: pathlib.Path) -> str:
    log.debug("Extracting text from TXT", path=str(file_path))
    try:
        text_content = file_path.read_text(encoding="utf-8")
        log.info("TXT extraction successful", path=str(file_path))
        return text_content
    except Exception as e:
        log.error("Failed to extract text from TXT", path=str(file_path), error=str(e), exc_info=True)
        raise ValueError(f"Error processing TXT {file_path.name}: {e}") from e

# LLM_FLAG: NO_CHANGE - Keep extractor map
EXTRACTORS: Dict[str, Callable[[pathlib.Path], str]] = {
    "application/pdf": _extract_from_pdf,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": _extract_from_docx,
    "application/msword": _extract_from_docx,
    "text/plain": _extract_from_txt,
    "text/markdown": _extract_from_md,
    "text/html": _extract_from_html,
}

# --------------- 2. CHUNKER FUNCTION -----------------
# LLM_FLAG: NO_CHANGE - Keep chunker function as is.
def chunk_text(text: str, chunk_size: int, chunk_overlap: int) -> List[str]:
    if not text or text.isspace():
        return []
    words = re.split(r'\s+', text.strip())
    words = [word for word in words if word]

    if not words:
        return []

    chunks: List[str] = []
    current_pos = 0
    while current_pos < len(words):
        end_pos = min(current_pos + chunk_size, len(words))
        chunk = " ".join(words[current_pos:end_pos])
        chunks.append(chunk)

        current_pos += chunk_size - chunk_overlap
        if current_pos <= (end_pos - chunk_size):
             current_pos = end_pos - chunk_overlap
        if current_pos >= len(words):
            break
        current_pos = max(0, current_pos)

    log.debug(f"Chunked text into {len(chunks)} chunks.", requested_size=chunk_size, overlap=chunk_overlap)
    return chunks

# --------------- 3. MILVUS HELPERS (using pymilvus) -----------------
# LLM_FLAG: NO_CHANGE - Keep Milvus constants and helpers.
MILVUS_COLLECTION_NAME = settings.MILVUS_COLLECTION_NAME
MILVUS_EMBEDDING_DIM = settings.EMBEDDING_DIMENSION
MILVUS_PK_FIELD = "pk_id"
MILVUS_VECTOR_FIELD = "embedding"
MILVUS_CONTENT_FIELD = "content"
MILVUS_COMPANY_ID_FIELD = "company_id"
MILVUS_DOCUMENT_ID_FIELD = "document_id"
MILVUS_FILENAME_FIELD = "file_name"

_milvus_collection: Optional[Collection] = None
_milvus_connected = False

def _ensure_milvus_connection_and_collection() -> Collection:
    global _milvus_collection, _milvus_connected
    alias = "default"

    if not _milvus_connected:
        uri = settings.MILVUS_URI
        log.info("Connecting to Milvus...", uri=uri, alias=alias, timeout=settings.MILVUS_GRPC_TIMEOUT)
        try:
            connections.connect(alias=alias, uri=uri)
            _milvus_connected = True
            log.info("Connected to Milvus.")
        except MilvusException as e:
            log.error("Failed to connect to Milvus.", error=str(e))
            raise RuntimeError(f"Milvus connection failed: {e}") from e

    # Obtener o crear colección y garantizar esquema e índices
    if not utility.has_collection(MILVUS_COLLECTION_NAME, using=alias):
        collection = _create_milvus_collection(alias)
    else:
        collection = Collection(name=MILVUS_COLLECTION_NAME, using=alias)
    # Asegurar índice vectorial existente
    existing_indexes = collection.indexes
    if not any(idx.field_name == MILVUS_VECTOR_FIELD for idx in existing_indexes):
        log.info("Creando índice vectorial en colección Milvus", field=MILVUS_VECTOR_FIELD)
        collection.create_index(field_name=MILVUS_VECTOR_FIELD, index_params=settings.MILVUS_INDEX_PARAMS)
    # Asignar colección global
    _milvus_collection = collection

    # Cargar colección en memoria
    try:
        # Cargar la colección en memoria de forma indiscriminada (idempotente)
        log.info("Loading collection into memory...", collection=MILVUS_COLLECTION_NAME)
        _milvus_collection.load()
        log.info("Collection loaded.")
    except MilvusException as e:
        log.error("Error loading Milvus collection", error=str(e))
        raise RuntimeError(f"Milvus collection error: {e}") from e

    return _milvus_collection

def _create_milvus_collection(alias: str) -> Collection:
    log.info(f"Defining schema for collection '{MILVUS_COLLECTION_NAME}'")
    fields = [
        FieldSchema(name=MILVUS_PK_FIELD, dtype=DataType.INT64, is_primary=True, auto_id=True),
        FieldSchema(name=MILVUS_VECTOR_FIELD, dtype=DataType.FLOAT_VECTOR, dim=MILVUS_EMBEDDING_DIM),
        FieldSchema(name=MILVUS_CONTENT_FIELD, dtype=DataType.VARCHAR, max_length=settings.SPLITTER_CHUNK_SIZE * 4),
        FieldSchema(name=MILVUS_COMPANY_ID_FIELD, dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name=MILVUS_DOCUMENT_ID_FIELD, dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name=MILVUS_FILENAME_FIELD, dtype=DataType.VARCHAR, max_length=512),
    ]
    schema = CollectionSchema(fields, description="Document Chunks for Atenex RAG")

    log.info(f"Creating collection '{MILVUS_COLLECTION_NAME}'...")
    try:
        collection = Collection(name=MILVUS_COLLECTION_NAME, schema=schema, using=alias, consistency_level="Strong")
        log.info(f"Collection '{MILVUS_COLLECTION_NAME}' created. Creating index...")

        index_params = settings.MILVUS_INDEX_PARAMS
        log.info("Creating index for vector field", field_name=MILVUS_VECTOR_FIELD, index_params=index_params)
        collection.create_index(field_name=MILVUS_VECTOR_FIELD, index_params=index_params)
        log.info("Index created successfully.")

        log.info("Creating scalar index for company_id field...")
        collection.create_index(field_name=MILVUS_COMPANY_ID_FIELD, index_name="company_id_idx")
        log.info("Creating scalar index for document_id field...")
        collection.create_index(field_name=MILVUS_DOCUMENT_ID_FIELD, index_name="document_id_idx")
        log.info("Scalar indexes created.")

    except MilvusException as e:
        log.error("Failed to create Milvus collection or index", collection_name=MILVUS_COLLECTION_NAME, error=str(e), exc_info=True)
        raise RuntimeError(f"Milvus collection/index creation failed: {e}") from e
    return collection

def delete_milvus_chunks(company_id: str, document_id: str) -> int:
    del_log = log.bind(company_id=company_id, document_id=document_id)
    try:
        # Ensure collection is ready
        collection = _ensure_milvus_connection_and_collection()
        # Construct filter for company/document
        expr = f'{MILVUS_COMPANY_ID_FIELD} == "{company_id}" and {MILVUS_DOCUMENT_ID_FIELD} == "{document_id}"'
        del_log.info("Querying chunks to delete by primary key", filter_expr=expr)
        # Get primary keys of matching entities
        pk_results = collection.query(expr=expr, output_fields=[MILVUS_PK_FIELD])
        pk_list = [str(item.get(MILVUS_PK_FIELD)) for item in pk_results]
        if not pk_list:
            del_log.info("No existing chunks to delete.")
            return 0
        delete_expr = f"{MILVUS_PK_FIELD} in [{', '.join(pk_list)}]"
        del_log.info("Deleting chunks from Milvus", delete_expr=delete_expr)
        delete_result = collection.delete(expr=delete_expr)
        del_log.info("Milvus delete operation executed", deleted_count=delete_result.delete_count)
        return delete_result.delete_count
    except MilvusException as e:
        del_log.error("Milvus delete error", error=str(e), exc_info=True)
        return 0
    except Exception as e:
        del_log.exception("Unexpected error during Milvus delete", error=str(e))
        return 0
    except Exception as e:
        del_log.exception("Unexpected error during Milvus chunk deletion", error=str(e))
        raise RuntimeError(f"Unexpected Milvus deletion error: {e}") from e


# --------------- 4. EMBEDDING MODEL INITIALIZATION REMOVED FROM GLOBAL SCOPE ---------------
# LLM_FLAG: REMOVED - Global embedding_model = TextEmbedding(...) block was here.

# --------------- 5. MAIN INGEST FUNCTION (MODIFIED) -----------------

# LLM_FLAG: MODIFIED - Added embedding_model argument to function signature
def ingest_document_pipeline(
    file_path: pathlib.Path,
    company_id: str,
    document_id: str,
    content_type: str,
    embedding_model: TextEmbedding, # Argument added
    delete_existing: bool = True
) -> int:
    """
    Processes a single document: extracts text, chunks, embeds, and inserts into Milvus.
    Args:
        file_path: Path to the downloaded document file.
        company_id: The company ID for multi-tenancy.
        document_id: The unique ID for the document.
        content_type: The MIME type of the file.
        embedding_model: The initialized FastEmbed TextEmbedding instance.
        delete_existing: If True, delete existing chunks for this company/document before inserting.
    Returns:
        The number of chunks successfully inserted into Milvus.
    Raises:
        ValueError: If the content type is unsupported, extraction fails, or model not provided.
        ConnectionError: If connection to Milvus fails.
        RuntimeError: For other Milvus or Embedding errors.
    """
    ingest_log = log.bind(
        company_id=company_id,
        document_id=document_id,
        filename=file_path.name,
        content_type=content_type
    )
    ingest_log.info("Starting ingestion pipeline for document")

    # --- 0. Check if embedding model was provided ---
    # LLM_FLAG: MODIFIED - Check the passed embedding_model argument
    if not embedding_model:
        ingest_log.error("Embedding model was not provided to the pipeline function.")
        raise ValueError("Embedding model instance is required for the pipeline.")

    # --- 1. Select Extractor ---
    extractor = EXTRACTORS.get(content_type)
    if not extractor:
        ingest_log.error("Unsupported content type for extraction.")
        raise ValueError(f"Unsupported content type: {content_type}")

    # --- 2. Extract Text ---
    ingest_log.debug("Extracting text content...")
    try:
        text_content = extractor(file_path)
        if not text_content or text_content.isspace():
            ingest_log.warning("No text content extracted from the document. Skipping.")
            return 0
        ingest_log.info(f"Text extracted successfully, length: {len(text_content)} chars.")
    except ValueError as ve:
        ingest_log.error("Text extraction failed.", error=str(ve))
        raise

    # --- 3. Chunk Text ---
    ingest_log.debug("Chunking extracted text...")
    chunks = chunk_text(
        text_content,
        chunk_size=settings.SPLITTER_CHUNK_SIZE,
        chunk_overlap=settings.SPLITTER_CHUNK_OVERLAP
    )
    if not chunks:
        ingest_log.warning("Text content resulted in zero chunks. Skipping.")
        return 0
    ingest_log.info(f"Text chunked into {len(chunks)} chunks.")

    # --- 4. Embed Chunks ---
    ingest_log.debug(f"Generating embeddings for {len(chunks)} chunks...")
    try:
        embeddings = list(embedding_model.embed(chunks))
        ingest_log.info(f"Embeddings generated successfully for {len(embeddings)} chunks.")
        if len(embeddings) != len(chunks):
            ingest_log.warning("Mismatch between number of chunks and generated embeddings.", num_chunks=len(chunks), num_embeddings=len(embeddings))
            min_len = min(len(chunks), len(embeddings))
            chunks = chunks[:min_len]
            embeddings = embeddings[:min_len]

    except Exception as e:
        ingest_log.error("Failed to generate embeddings", error=str(e), exc_info=True)
        raise RuntimeError(f"Embedding generation failed: {e}") from e

    # --- 5. Prepare Data for Milvus ---
    max_content_len = 4000
    def truncate_utf8_bytes(s, max_bytes):
        b = s.encode('utf-8')
        if len(b) <= max_bytes:
            return s
        # Truncate by bytes, decode ignoring incomplete chars
        return b[:max_bytes].decode('utf-8', errors='ignore')
    truncated_chunks = [truncate_utf8_bytes(c, max_content_len) for c in chunks]
    # Ensure embeddings and truncated_chunks are aligned
    if len(truncated_chunks) != len(embeddings):
        min_len = min(len(truncated_chunks), len(embeddings))
        truncated_chunks = truncated_chunks[:min_len]
        embeddings = embeddings[:min_len]
    # Debug: log chunk lengths before insert
    for idx, chunk in enumerate(truncated_chunks):
        ingest_log.debug(f"Chunk {idx} length before insert: {len(chunk)} bytes: {len(chunk.encode('utf-8'))}")
    data_to_insert = [
        embeddings,
        truncated_chunks,
        [company_id] * len(truncated_chunks),
        [document_id] * len(truncated_chunks),
        [file_path.name] * len(truncated_chunks),
    ]

    # --- 6. Delete Existing Chunks (Optional) ---
    if delete_existing:
        try:
            deleted_count = delete_milvus_chunks(company_id, document_id)
            ingest_log.info(f"Deleted {deleted_count} existing chunks before insertion.")
        except Exception as del_err:
            ingest_log.error("Failed to delete existing chunks, proceeding with insert anyway.", error=str(del_err))

    # --- 7. Insert into Milvus ---
    ingest_log.debug(f"Inserting {len(truncated_chunks)} chunks into Milvus collection '{MILVUS_COLLECTION_NAME}'...")
    try:
        collection = _ensure_milvus_connection_and_collection()
        mutation_result = collection.insert(data_to_insert)
        inserted_count = mutation_result.insert_count

        if inserted_count == len(truncated_chunks):
            ingest_log.info(f"Successfully inserted {inserted_count} chunks into Milvus.")
            log.debug("Flushing Milvus collection...")
            collection.flush()
            log.info("Milvus collection flushed.")
        else:
            ingest_log.warning(f"Milvus insert result count ({inserted_count}) differs from chunks sent ({len(truncated_chunks)}).")

        return inserted_count

    except MilvusException as e:
        ingest_log.error("Failed to insert data into Milvus", error=str(e), exc_info=True)
        raise RuntimeError(f"Milvus insertion failed: {e}") from e
    except Exception as e:
        ingest_log.exception("Unexpected error during Milvus insertion", error=str(e))
        raise RuntimeError(f"Unexpected Milvus insertion error: {e}") from e
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
        """Synchronously checks if a file exists in MinIO. Strips bucket prefix if present."""
        # Defensive: Remove bucket name prefix if present
        if object_name.startswith(self.bucket_name + "/"):
            self.log.warning("Object name included bucket prefix, stripping it for MinIO API.", original_object_name=object_name)
            object_name = object_name[len(self.bucket_name) + 1:]
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
import pathlib # Use pathlib for paths
from typing import Optional, Dict, Any

import structlog
from celery import Task
from celery.exceptions import Ignore, Reject, MaxRetriesExceededError
# LLM_FLAG: ADDED - Import TextEmbedding for type hinting and conditional init
from fastembed.embedding import TextEmbedding
from sqlalchemy import Engine # Import Engine for type hinting

# --- Custom Application Imports ---
from app.core.config import settings
from app.db.postgres_client import get_sync_engine, set_status_sync # Keep DB client
from app.models.domain import DocumentStatus # Keep domain models
from app.services.minio_client import MinioClient, MinioError # Keep MinIO client
# --- Import the NEW pipeline function ---
from app.services.ingest_pipeline import ingest_document_pipeline, EXTRACTORS # Import pipeline and extractors map
from app.tasks.celery_app import celery_app # Keep Celery app


# --- Initialize Structlog Logger ---
task_struct_log = structlog.get_logger(__name__)

# --- Detect if running as Celery Worker ---
# A common way to detect if running under Celery worker context
IS_WORKER = "worker" in sys.argv

# --- Initialize Global Resources Conditionally ---
sync_engine: Optional[Engine] = None
minio_client: Optional[MinioClient] = None
embedding_model: Optional[TextEmbedding] = None

# LLM_FLAG: MODIFIED - Initialize resources conditionally based on IS_WORKER
try:
    task_struct_log.info("Initializing global resources for process...", is_worker=IS_WORKER)

    # MinIO Client is needed by both (though API should ideally use async)
    # Initialize globally for now, but mark for potential async refactor in API
    minio_client = MinioClient()
    task_struct_log.info("Global MinIO client initialized.")

    if IS_WORKER:
        # Resources needed ONLY by the worker
        task_struct_log.info("Initializing worker-specific resources (DB Engine, Embedding Model)...")
        try:
            sync_engine = get_sync_engine()
            task_struct_log.info("Synchronous DB engine initialized successfully for worker.")
        except Exception as db_init_err:
            task_struct_log.critical(
                "Failed to initialize synchronous DB engine! Worker tasks depending on DB will fail.",
                error=str(db_init_err),
                exc_info=True
            )
            # sync_engine remains None

        try:
            task_struct_log.info("Initializing FastEmbed model instance for worker...", model=settings.FASTEMBED_MODEL, device="cpu")
            embedding_model = TextEmbedding(
                model_name=settings.FASTEMBED_MODEL,
                cache_dir=os.environ.get("FASTEMBED_CACHE_DIR"),
                device="cpu",
                threads=1,
                parallel=0
            )
            task_struct_log.info("Global FastEmbed model instance initialized successfully for worker.")
        except Exception as emb_init_err:
             task_struct_log.critical(
                 "Failed to initialize FastEmbed model! Worker tasks depending on embeddings will fail.",
                 error=str(emb_init_err),
                 exc_info=True
             )
             # embedding_model remains None
    else:
         task_struct_log.info("Running in API context. Skipping worker-specific resource initialization (DB Engine, Embedding Model).")

except Exception as global_init_err:
    task_struct_log.critical("CRITICAL: Failed during global resource initialization!", error=str(global_init_err), exc_info=True)
    # Ensure potentially failed initializations result in None
    if not isinstance(minio_client, MinioClient): minio_client = None
    if IS_WORKER: # Only nullify worker resources if we expected them
        if not isinstance(sync_engine, Engine): sync_engine = None
        if not isinstance(embedding_model, TextEmbedding): embedding_model = None

# --------------------------------------------------------------------------
# Refactored Synchronous Celery Task Definition (MODIFIED Pre-checks)
# --------------------------------------------------------------------------
@celery_app.task(
    bind=True,
    name="ingest.process_document",
    autoretry_for=(Exception,),
    # Exclude errors that indicate non-transient problems or config issues
    exclude=(Reject, Ignore, ValueError, ConnectionError, RuntimeError, TypeError),
    retry_backoff=True,
    retry_backoff_max=600, # Max ~10 minutes backoff
    retry_jitter=True,
    max_retries=3 # Retry up to 3 times for transient errors
)
def process_document_standalone(self: Task, *args, **kwargs) -> Dict[str, Any]:
    """
    Synchronous Celery task to process a document using the standalone pipeline.
    Downloads from MinIO, then calls ingest_document_pipeline for conversion,
    chunking, embedding (CPU), and writing to Milvus via pymilvus.
    Updates status in PostgreSQL synchronously. Uses structlog for logging.
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
    log.info("Starting standalone document processing task")

    # --- Pre-checks ---
    if not IS_WORKER:
         # This should ideally not happen if imports are managed well, but safety first.
         log.critical("Task function called outside of a worker context! Rejecting.")
         raise Reject("Task running outside worker context.", requeue=False)

    if not all([document_id_str, company_id_str, filename, content_type]):
        log.error("Missing required arguments in task payload.", payload_kwargs=kwargs)
        raise Reject("Missing required arguments (doc_id, company_id, filename, content_type)", requeue=False)

    # --- Check if Worker Resources Initialized Correctly ---
    # These checks run *inside* the task execution for robustness
    if not sync_engine:
         log.critical("Worker Sync DB Engine is not initialized. Task cannot proceed.")
         # Attempting DB update here is difficult without the engine. Reject permanently.
         raise Reject("Worker sync DB engine initialization failed.", requeue=False)

    if not embedding_model:
         log.critical("Worker Embedding Model is not initialized. Task cannot proceed.")
         # Attempt to update DB status via the (hopefully existing) sync_engine
         if document_id_str:
             try:
                 doc_uuid_for_error = uuid.UUID(document_id_str)
                 set_status_sync(engine=sync_engine, document_id=doc_uuid_for_error, status=DocumentStatus.ERROR, error_message="Worker embedding model init failed.")
             except Exception as db_err:
                 log.critical("Failed to update status to ERROR after worker embedding model init failure!", error=str(db_err))
         raise Reject("Worker embedding model initialization failed.", requeue=False)

    if not minio_client:
         log.critical("Worker MinIO Client is not initialized. Task cannot proceed.")
         # Attempt to update DB status via sync_engine
         if document_id_str:
              try:
                  doc_uuid_for_error = uuid.UUID(document_id_str)
                  set_status_sync(engine=sync_engine, document_id=doc_uuid_for_error, status=DocumentStatus.ERROR, error_message="Worker MinIO client init failed.")
              except Exception as db_err:
                  log.critical("Failed to update status to ERROR after worker MinIO client init failure!", error=str(db_err))
         raise Reject("Worker MinIO client initialization failed.", requeue=False)
    # --- End Pre-checks ---


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


    object_name = f"{company_id_str}/{document_id_str}/{filename}"
    log.debug(f"MinIO download object_name repr: {repr(object_name)}")
    temp_file_path_obj: Optional[pathlib.Path] = None
    inserted_chunk_count = 0

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

        # 2. Download file from MinIO to temporary directory (with retry on NoSuchKey)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = pathlib.Path(temp_dir)
            temp_file_path_obj = temp_dir_path / filename
            log.info(f"Downloading MinIO object: {object_name} -> {str(temp_file_path_obj)}")
            max_minio_retries = 5
            minio_delay = 2  # seconds
            for attempt_num in range(1, max_minio_retries + 1):
                try:
                    minio_client.download_file_sync(object_name, str(temp_file_path_obj))
                    log.info("File downloaded successfully from MinIO.")
                    break
                except MinioError as me:
                    # Only retry on NoSuchKey
                    if "NoSuchKey" in str(me) or "Object not found" in str(me):
                        log.warning(f"MinIO NoSuchKey on attempt {attempt_num}/{max_minio_retries}. Retrying in {minio_delay} seconds...", error=str(me))
                        if attempt_num == max_minio_retries:
                            log.error(f"File still not found in MinIO after {max_minio_retries} attempts. Aborting.")
                            raise
                        import time
                        time.sleep(minio_delay)
                        minio_delay *= 2  # Exponential backoff
                        continue
                    else:
                        raise  # Other Minio errors are not retried

            # 3. Execute Standalone Ingestion Pipeline
            log.info("Executing standalone ingest pipeline (extract, chunk, embed, insert)...")
            # Lazy-init embedding model for this task to avoid multiprocessing hangs
            from fastembed.embedding import TextEmbedding
            task_struct_log.info("Initializing embedding model for this task...", model=settings.FASTEMBED_MODEL)
            local_embedding_model = TextEmbedding(
                model_name=settings.FASTEMBED_MODEL,
                cache_dir=os.environ.get("FASTEMBED_CACHE_DIR"),
                device="cpu",
                threads=1,
                parallel=0
            )
            inserted_chunk_count = ingest_document_pipeline(
                file_path=temp_file_path_obj,
                company_id=company_id_str,
                document_id=document_id_str,
                content_type=content_type,
                embedding_model=local_embedding_model,
                delete_existing=True
            )
            log.info(f"Ingestion pipeline finished. Inserted chunks: {inserted_chunk_count}")

        # 4. Update status to PROCESSED
        log.debug("Setting status to PROCESSED in DB.")
        final_status_updated = set_status_sync(
            engine=sync_engine,
            document_id=doc_uuid,
            status=DocumentStatus.PROCESSED,
            chunk_count=inserted_chunk_count,
            error_message=None
        )
        if not final_status_updated:
             log.warning("Failed to update status to PROCESSED after successful processing (document possibly deleted?).")

        log.info(f"Document processing finished successfully. Final chunk count: {inserted_chunk_count}")
        return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": inserted_chunk_count, "document_id": document_id_str}

    # --- Error Handling ---
    except MinioError as me:
        log.error(f"MinIO Error during processing: {me}", exc_info=True)
        error_msg = f"MinIO Error: {str(me)[:400]}"
        try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
        except Exception as db_err: log.critical("Failed to update status to ERROR after MinIO failure!", error=str(db_err))
        if "Object not found" in str(me):
            # If the object is not found, it's not a transient error. Reject permanently.
            raise Reject(f"MinIO Error: Object not found: {object_name}", requeue=False) from me
        else:
            # For other S3 errors, raise to allow Celery's autoretry mechanism
            raise me # Will be caught by autoretry_for=(Exception,)

    except (ValueError, ConnectionError, RuntimeError, TypeError) as pipeline_err:
         # These are likely non-recoverable errors from the pipeline logic or Milvus/DB connection issues
         # that might persist. Reject permanently after marking as error.
         log.error(f"Pipeline/Connection Error: {pipeline_err}", exc_info=True)
         error_msg = f"Pipeline Error: {type(pipeline_err).__name__} - {str(pipeline_err)[:400]}"
         try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
         except Exception as db_err: log.critical("Failed to update status to ERROR after pipeline failure!", error=str(db_err))
         # Reject instead of raising to prevent retries for these specific error types
         raise Reject(f"Pipeline failed: {error_msg}", requeue=False) from pipeline_err

    except Reject as r:
         # If Reject is raised explicitly (e.g., from pre-checks)
         log.error(f"Task rejected permanently: {r.reason}")
         # Attempt to set status one last time if possible and ID is valid
         if sync_engine and doc_uuid:
             try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=f"Rejected: {r.reason}"[:500])
             except Exception as db_err: log.critical("Failed to update status to ERROR after task rejection!", error=str(db_err))
         raise r # Re-raise Reject

    except Ignore:
         # If Ignore is raised explicitly
         log.info("Task ignored.")
         raise Ignore() # Re-raise Ignore

    except MaxRetriesExceededError as mree:
        # This happens after autoretry_for fails max_retries times
        log.error("Max retries exceeded for task.", exc_info=True)
        final_error = mree.cause if mree.cause else mree
        error_msg = f"Max retries exceeded ({max_attempts}). Last error: {type(final_error).__name__} - {str(final_error)[:300]}"
        try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
        except Exception as db_err: log.critical("Failed to update status to ERROR after max retries!", error=str(db_err))
        # Do not raise here, Celery handles logging MaxRetriesExceededError

    except Exception as exc:
        # Catch any other unexpected exception to allow Celery's autoretry
        log.exception(f"An unexpected error occurred during document processing, will retry if attempts remain.")
        error_msg = f"Attempt {attempt} failed: {type(exc).__name__} - {str(exc)[:400]}"
        try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
        except Exception as db_err: log.critical("CRITICAL: Failed to update status to ERROR after unexpected failure!", error=str(db_err))
        # Raise the original exception to trigger Celery's retry mechanism defined in autoretry_for
        raise exc

# LLM_FLAG: NO_CHANGE - Keep task alias assignment
process_document_haystack_task = process_document_standalone
```

## File: `pyproject.toml`
```toml
[tool.poetry]
name = "ingest-service"
# Incrementamos versión por refactorización mayor
version = "0.2.0"
description = "Ingest service for Atenex B2B SaaS (Postgres/Minio/Milvus/FastEmbed - CPU - Prefork - Haystack Removed)"
authors = ["Atenex Team <dev@atenex.com>"]
readme = "README.md"

[tool.poetry.dependencies]
python = ">=3.10,<3.13"
fastapi = "^0.110.0"
uvicorn = {extras = ["standard"], version = "^0.28.0"}
gunicorn = "^21.2.0"
pydantic = {extras = ["email"], version = "^2.6.4"}
pydantic-settings = "^2.2.1"
celery = {extras = ["redis"], version = "^5.3.6"}
# gevent ya no es necesario con prefork
asyncpg = "^0.29.0" # Keep for API
tenacity = "^8.2.3"
python-multipart = "^0.0.9"
structlog = "^24.1.0"
minio = "^7.1.17"

# --- Core Processing Dependencies (Haystack REMOVED) ---
fastembed = ">=0.2.1,<0.3.0" # Standalone FastEmbed (includes ONNX runtime)
pymilvus = ">=2.4.1,<2.5.0"  # Official Milvus client

# --- Converter Dependencies (Standalone) ---
pypdf = ">=4.0.1,<5.0.0"
python-docx = ">=1.1.0,<2.0.0"
markdown = ">=3.5.1,<4.0.0"
beautifulsoup4 = ">=4.12.3,<5.0.0"
html2text = ">=2024.1.0,<2025.0.0" # For Markdown -> HTML -> Text conversion

# --- HTTP Client (API) ---
httpx = {extras = ["http2"], version = "^0.27.0"}
h2 = "^4.1.0"

# --- Synchronous DB Dependencies (Worker) ---
sqlalchemy = "^2.0.28"
psycopg2-binary = "^2.9.9"

[tool.poetry.group.dev.dependencies]
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
