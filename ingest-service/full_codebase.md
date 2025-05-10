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
│   ├── clients
│   │   ├── docproc_service_client.py
│   │   └── embedding_service_client.py
│   ├── gcs_client.py
│   └── ingest_pipeline.py
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
def normalize_filename(filename: str) -> str:
    """Normaliza el nombre de archivo eliminando espacios al inicio/final y espacios duplicados."""
    return " ".join(filename.strip().split())
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
from app.services.gcs_client import GCSClient, GCSClientError
from app.tasks.celery_app import celery_app
# Import the specific task instance registered in process_document.py
from app.tasks.process_document import process_document_standalone as process_document_task # Use the new name
# Import Milvus field name constants for consistency
from app.services.ingest_pipeline import (
    MILVUS_COLLECTION_NAME,
    MILVUS_COMPANY_ID_FIELD,
    MILVUS_DOCUMENT_ID_FIELD,
    MILVUS_PK_FIELD, # Added for query
)

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Helper Functions ---

# GCS Client Dependency
def get_gcs_client():
    """Dependency to get GCS client instance."""
    try:
        # No specific config needed here if GOOGLE_APPLICATION_CREDENTIALS is set
        client = GCSClient()
        return client
    except Exception as e:
        log.exception("Failed to initialize GCSClient dependency", error=str(e))
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
# LLM_FLAG: NO_CHANGE - Milvus Sync Helper Functions (Already Correct) - _get_milvus_collection_sync is correct
def _get_milvus_collection_sync() -> Collection:
    """Synchronously connects to Milvus and returns the Collection object."""
    alias = "api_sync_helper"
    sync_milvus_log = log.bind(component="MilvusHelperSync", alias=alias)
    if alias not in connections.list_connections():
        sync_milvus_log.info("Connecting to Milvus for sync helper...")
        try:
            connections.connect(alias=alias, uri=settings.MILVUS_URI, timeout=settings.MILVUS_GRPC_TIMEOUT)
            sync_milvus_log.info("Milvus connection established for sync helper.")
        except MilvusException as e:
            sync_milvus_log.error("Failed to connect to Milvus for sync helper", error=str(e))
            raise RuntimeError(f"Milvus connection failed for API helper: {e}") from e
    else:
        sync_milvus_log.debug("Reusing existing Milvus connection for sync helper.")
    try:
        collection = Collection(name=MILVUS_COLLECTION_NAME, using=alias)
        sync_milvus_log.info("Loading Milvus collection for sync helper...")
        collection.load()
        sync_milvus_log.info("Milvus collection loaded for sync helper.")
        return collection
    except MilvusException as e:
        sync_milvus_log.error("Failed to get or load Milvus collection for sync helper", error=str(e))
        raise RuntimeError(f"Milvus collection access error for API helper: {e}") from e

# LLM_FLAG: NO_CHANGE - Milvus Sync Helper Functions (Already Correct) - _get_milvus_chunk_count_sync is correct
def _get_milvus_chunk_count_sync(document_id: str, company_id: str) -> int:
    """Synchronously counts chunks in Milvus for a specific document using pymilvus query."""
    count_log = log.bind(document_id=document_id, company_id=company_id, component="MilvusHelperSync")
    try:
        collection = _get_milvus_collection_sync()
        expr = f'{MILVUS_COMPANY_ID_FIELD} == "{company_id}" and {MILVUS_DOCUMENT_ID_FIELD} == "{document_id}"'
        count_log.debug("Attempting to query Milvus chunk count", filter_expr=expr)
        query_res = collection.query(expr=expr, output_fields=[MILVUS_PK_FIELD]) # Use PK field for count
        count = len(query_res) if query_res else 0
        count_log.info("Milvus chunk count successful (pymilvus)", count=count)
        return count
    except RuntimeError as re:
        count_log.error("Failed to get Milvus count due to connection/collection error", error=str(re))
        return -1
    except MilvusException as e:
        count_log.error("Milvus query error during count", error=str(e), exc_info=True)
        return -1
    except Exception as e:
        count_log.exception("Unexpected error during Milvus count", error=str(e))
        return -1

# --- FIX: Milvus Delete Sync Helper ---
def _delete_milvus_sync(document_id: str, company_id: str) -> bool:
    """Synchronously deletes chunks from Milvus using pymilvus: Query PKs first, then delete by PK list."""
    delete_log = log.bind(document_id=document_id, company_id=company_id, component="MilvusHelperSync")
    expr = f'{MILVUS_COMPANY_ID_FIELD} == "{company_id}" and {MILVUS_DOCUMENT_ID_FIELD} == "{document_id}"'
    pks_to_delete: List[str] = []

    try:
        collection = _get_milvus_collection_sync()

        # 1. Query for Primary Keys
        delete_log.info("Querying Milvus for PKs to delete (sync)...", filter_expr=expr)
        query_res = collection.query(expr=expr, output_fields=[MILVUS_PK_FIELD])
        pks_to_delete = [item[MILVUS_PK_FIELD] for item in query_res if MILVUS_PK_FIELD in item]

        if not pks_to_delete:
            delete_log.info("No matching primary keys found in Milvus for deletion (sync).")
            return True # Return True as there's nothing to delete

        delete_log.info(f"Found {len(pks_to_delete)} primary keys to delete (sync).")

        # 2. Delete using the retrieved Primary Keys
        # Ensure the list formatting is correct for the 'in' operator
        delete_expr = f'{MILVUS_PK_FIELD} in {json.dumps(pks_to_delete)}' # Use json.dumps for correct list representation
        delete_log.info("Attempting to delete chunks from Milvus using PK list expression (sync).", filter_expr=delete_expr)
        delete_result = collection.delete(expr=delete_expr)
        delete_log.info("Milvus delete operation by PK list executed (sync).", deleted_count=delete_result.delete_count)

        if delete_result.delete_count != len(pks_to_delete):
             delete_log.warning("Milvus delete count mismatch (sync).", expected=len(pks_to_delete), reported=delete_result.delete_count)
             # Return False if count mismatches? Or True if delete was attempted? Let's return True if no exception.
        return True # Indicate successful execution (even if count mismatch)

    except RuntimeError as re:
        delete_log.error("Failed to delete Milvus chunks due to connection/collection error (sync)", error=str(re))
        return False
    except MilvusException as e:
        delete_log.error("Milvus query or delete error (sync)", error=str(e), exc_info=True)
        return False
    except Exception as e:
        delete_log.exception("Unexpected error during Milvus delete (sync)", error=str(e))
        return False

# --- API Endpoints ---

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
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB or GCS)"}, # Updated description
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
    gcs_client: GCSClient = Depends(get_gcs_client), # Uses GCS client
):
    """
    Receives a document file and optional metadata, saves it to GCS,
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

    normalized_filename = normalize_filename(file.filename)
    endpoint_log = endpoint_log.bind(company_id=company_id, user_id=user_id,
                                     filename=normalized_filename, content_type=file.content_type)
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
                conn=conn, filename=normalized_filename, company_id=company_uuid
            )
            if existing_doc and existing_doc['status'] != DocumentStatus.ERROR.value:
                 endpoint_log.warning("Duplicate document detected", document_id=existing_doc['id'], status=existing_doc['status'])
                 raise HTTPException(
                     status_code=status.HTTP_409_CONFLICT,
                     detail=f"Document '{normalized_filename}' already exists with status '{existing_doc['status']}'. Delete it first or wait for processing."
                 )
            elif existing_doc:
                 endpoint_log.info("Found existing document in error state, proceeding with upload.", document_id=existing_doc['id'])
    except HTTPException as http_exc: raise http_exc
    except Exception as e:
        endpoint_log.exception("Error checking for duplicate document", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error checking for duplicates.")

    document_id = uuid.uuid4()
    # GCS object name format
    file_path_in_storage = f"{company_id}/{document_id}/{normalized_filename}"

    try:
        async with get_db_conn() as conn:
            await api_db_retry_strategy(db_client.create_document_record)(
                conn=conn, doc_id=document_id, company_id=company_uuid,
                filename=normalized_filename, file_type=file.content_type,
                file_path=file_path_in_storage, status=DocumentStatus.PENDING,
                metadata=metadata
            )
        endpoint_log.info("Document record created in PostgreSQL", document_id=str(document_id))
    except Exception as e:
        endpoint_log.exception("Failed to create document record in PostgreSQL", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error creating record.")

    # Use GCS Client
    try:
        # Potential OOM Risk: Reading entire file here. Consider streaming upload if needed.
        file_content = await file.read()
        endpoint_log.info("Preparing upload to GCS", object_name=file_path_in_storage, filename=normalized_filename, size=len(file_content), content_type=file.content_type)
        try:
            await gcs_client.upload_file_async(
                object_name=file_path_in_storage, data=file_content, content_type=file.content_type
            )
            endpoint_log.info("File uploaded successfully to GCS", object_name=file_path_in_storage)
        except Exception as upload_exc:
            endpoint_log.error("Exception during GCS upload", object_name=file_path_in_storage, error=str(upload_exc))
            raise # Re-raise to be caught below

        # --- Validation: check file exists in GCS ---
        try:
            file_exists = await gcs_client.check_file_exists_async(file_path_in_storage)
            endpoint_log.info("GCS existence check after upload", object_name=file_path_in_storage, exists=file_exists)
        except Exception as check_exc:
            endpoint_log.error("Exception during GCS existence check", object_name=file_path_in_storage, error=str(check_exc))
            file_exists = False # Assume not exists on error

        if not file_exists:
            endpoint_log.error("File not found in GCS after upload attempt", object_name=file_path_in_storage, filename=normalized_filename)
            async with get_db_conn() as conn:
                await api_db_retry_strategy(db_client.update_document_status)(
                    document_id=document_id, status=DocumentStatus.ERROR, error_message="File not found in GCS after upload", conn=conn
                )
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="File verification in GCS failed after upload.")

        async with get_db_conn() as conn:
            await api_db_retry_strategy(db_client.update_document_status)(
                document_id=document_id, status=DocumentStatus.UPLOADED, conn=conn
            )
        endpoint_log.info("Document status updated to 'uploaded'", document_id=str(document_id))

    except GCSClientError as gce: # Catch GCS specific errors
        endpoint_log.error("Failed to upload file to GCS", object_name=file_path_in_storage, error=str(gce))
        try:
            async with get_db_conn() as conn:
                await api_db_retry_strategy(db_client.update_document_status)(
                    document_id=document_id, status=DocumentStatus.ERROR, error_message=f"GCS upload failed: {str(gce)[:200]}", conn=conn
                )
        except Exception as db_err:
            endpoint_log.exception("Failed to update status to ERROR after GCS failure", error=str(db_err))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Storage service error: {gce}")
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

    # Enqueue Celery task (No change needed here)
    try:
        task_payload = {
            "document_id": str(document_id), "company_id": company_id,
            "filename": normalized_filename, "content_type": file.content_type
        }
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
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB, GCS, Milvus)"}, # Updated description
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
    gcs_client: GCSClient = Depends(get_gcs_client), # Uses GCS Client
):
    """
    Retrieves document status from PostgreSQL and performs live GCS/Milvus checks.
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

    # Live checks and status correction logic
    from datetime import datetime, timedelta, timezone
    needs_update = False
    current_status_enum = DocumentStatus(doc_data['status'])
    current_chunk_count = doc_data.get('chunk_count')
    current_error_message = doc_data.get('error_message')
    updated_status_enum = current_status_enum
    updated_chunk_count = current_chunk_count
    updated_error_message = current_error_message

    updated_at = doc_data.get('updated_at')
    now_utc = datetime.now(timezone.utc)
    updated_at_dt = None
    if updated_at:
        if isinstance(updated_at, datetime): updated_at_dt = updated_at.astimezone(timezone.utc)
        else:
            try: updated_at_dt = datetime.fromisoformat(updated_at.replace('Z', '+00:00'))
            except Exception: updated_at_dt = None

    GRACE_PERIOD_SECONDS = 120 # Grace period to avoid flapping

    gcs_path = doc_data.get('file_path')
    # --- RENAMED VARIABLE: gcs_exists ---
    gcs_exists = False
    if not gcs_path:
        status_log.warning("GCS file path missing in DB record", db_id=doc_data['id'])
        if updated_status_enum not in [DocumentStatus.ERROR, DocumentStatus.PENDING]:
            if not (updated_status_enum == DocumentStatus.PROCESSED and updated_at_dt and (now_utc - updated_at_dt).total_seconds() < GRACE_PERIOD_SECONDS):
                needs_update = True
                updated_status_enum = DocumentStatus.ERROR
                updated_error_message = (updated_error_message or "") + " File path missing."
            else:
                status_log.warning("Grace period: no status change for missing file_path (recent processed)")
    else:
        status_log.debug("Checking GCS for file existence", object_name=gcs_path)
        try:
            # Use GCS client and renamed variable
            gcs_exists = await gcs_client.check_file_exists_async(gcs_path)
            status_log.info("GCS existence check complete", exists=gcs_exists)
            if not gcs_exists and updated_status_enum not in [DocumentStatus.ERROR, DocumentStatus.PENDING]:
                status_log.warning("File missing in GCS but DB status suggests otherwise.", current_db_status=updated_status_enum.value)
                if updated_status_enum != DocumentStatus.ERROR:
                    if not (updated_status_enum == DocumentStatus.PROCESSED and updated_at_dt and (now_utc - updated_at_dt).total_seconds() < GRACE_PERIOD_SECONDS):
                        needs_update = True
                        updated_status_enum = DocumentStatus.ERROR
                        updated_error_message = "File missing from storage."
                        updated_chunk_count = 0
                    else:
                        status_log.warning("Grace period: no status change for missing GCS file (recent processed)")
        except Exception as gcs_e:
            status_log.error("GCS check failed", error=str(gcs_e))
            gcs_exists = False # Assume not exists on error
            if updated_status_enum != DocumentStatus.ERROR:
                if not (updated_status_enum == DocumentStatus.PROCESSED and updated_at_dt and (now_utc - updated_at_dt).total_seconds() < GRACE_PERIOD_SECONDS):
                    needs_update = True
                    updated_status_enum = DocumentStatus.ERROR
                    updated_error_message = (updated_error_message or "") + f" GCS check error ({type(gcs_e).__name__})."
                    updated_chunk_count = 0
                else:
                     status_log.warning("Grace period: no status change for GCS exception (recent processed)")

    # Milvus Check (No changes needed here)
    status_log.debug("Checking Milvus for chunk count using pymilvus helper...")
    loop = asyncio.get_running_loop()
    milvus_chunk_count = -1
    try:
        milvus_chunk_count = await loop.run_in_executor(
            None, _get_milvus_chunk_count_sync, str(document_id), company_id
        )
        status_log.info("Milvus chunk count check complete (pymilvus)", count=milvus_chunk_count)

        if milvus_chunk_count == -1:
            status_log.error("Milvus count check failed (returned -1).")
            if updated_status_enum != DocumentStatus.ERROR:
                if not (updated_status_enum == DocumentStatus.PROCESSED and updated_at_dt and (now_utc - updated_at_dt).total_seconds() < GRACE_PERIOD_SECONDS):
                    needs_update = True
                    updated_status_enum = DocumentStatus.ERROR
                    updated_error_message = (updated_error_message or "") + " Failed Milvus count check."
                else:
                    status_log.warning("Grace period: no status change for Milvus count failure (recent processed)")
        elif milvus_chunk_count > 0:
             # --- Use gcs_exists ---
            if gcs_exists and updated_status_enum in [DocumentStatus.ERROR, DocumentStatus.UPLOADED]:
                status_log.warning("Inconsistency: Chunks found and file exists but DB status is not 'processed'. Correcting.")
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
                if not (updated_status_enum == DocumentStatus.PROCESSED and updated_at_dt and (now_utc - updated_at_dt).total_seconds() < GRACE_PERIOD_SECONDS):
                    needs_update = True
                    updated_status_enum = DocumentStatus.ERROR
                    updated_chunk_count = 0
                    updated_error_message = (updated_error_message or "") + " Processed data missing."
                else:
                     status_log.warning("Grace period: no status change for zero Milvus chunks (recent processed)")
            elif updated_status_enum == DocumentStatus.ERROR and updated_chunk_count != 0:
                needs_update = True
                updated_chunk_count = 0

    except Exception as e:
        status_log.exception("Unexpected error during Milvus count check execution", error=str(e))
        milvus_chunk_count = -1
        if updated_status_enum != DocumentStatus.ERROR:
            if not (updated_status_enum == DocumentStatus.PROCESSED and updated_at_dt and (now_utc - updated_at_dt).total_seconds() < GRACE_PERIOD_SECONDS):
                needs_update = True
                updated_status_enum = DocumentStatus.ERROR
                updated_error_message = (updated_error_message or "") + f" Error checking Milvus ({type(e).__name__})."
            else:
                status_log.warning("Grace period: no status change for Milvus exception (recent processed)")

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

    # Return StatusResponse using renamed field
    return StatusResponse(
        document_id=str(doc_data['id']), company_id=str(doc_data['company_id']),
        status=final_status_val, file_name=doc_data.get('file_name'),
        file_type=doc_data.get('file_type'), file_path=doc_data.get('file_path'),
        chunk_count=final_chunk_count_val,
        gcs_exists=gcs_exists,  # Use renamed field
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
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB, GCS, Milvus)"}, # Updated description
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
    gcs_client: GCSClient = Depends(get_gcs_client), # Uses GCS Client
):
    """
    Lists documents for the company with pagination. Performs live GCS/Milvus checks.
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

    # --- Inner function for concurrent checks ---
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

        # --- RENAMED VARIABLE: live_gcs_exists ---
        gcs_path_db = doc_db_data.get('file_path')
        live_gcs_exists = False
        if gcs_path_db:
            try:
                live_gcs_exists = await gcs_client.check_file_exists_async(gcs_path_db)
                if not live_gcs_exists and doc_updated_status_enum not in [DocumentStatus.ERROR, DocumentStatus.PENDING]:
                    if doc_updated_status_enum != DocumentStatus.ERROR:
                        doc_needs_update = True
                        doc_updated_status_enum = DocumentStatus.ERROR
                        doc_updated_error_msg = "File missing from storage."
                        doc_updated_chunk_count = 0
            except Exception as e:
                check_log.error("GCS check failed for list item", error=str(e))
                live_gcs_exists = False # Assume not exists on error
                if doc_updated_status_enum != DocumentStatus.ERROR:
                    doc_needs_update = True
                    doc_updated_status_enum = DocumentStatus.ERROR
                    doc_updated_error_msg = (doc_updated_error_msg or "") + f" GCS check error."
                    doc_updated_chunk_count = 0
        else:
            live_gcs_exists = False # If path is missing, it doesn't exist

        # Milvus Check (No changes needed)
        live_milvus_chunk_count = -1
        loop = asyncio.get_running_loop()
        try:
            live_milvus_chunk_count = await loop.run_in_executor(None, _get_milvus_chunk_count_sync, doc_id_str, company_id)
            if live_milvus_chunk_count == -1:
                if doc_updated_status_enum != DocumentStatus.ERROR:
                    doc_needs_update = True
                    doc_updated_status_enum = DocumentStatus.ERROR
                    doc_updated_error_msg = (doc_updated_error_msg or "") + " Failed Milvus count."
            elif live_milvus_chunk_count > 0:
                 # --- Use live_gcs_exists ---
                if live_gcs_exists and doc_updated_status_enum in [DocumentStatus.ERROR, DocumentStatus.UPLOADED]:
                    check_log.warning("Inconsistency: Chunks found and file exists but DB status is not 'processed'. Correcting.")
                    doc_needs_update = True
                    doc_updated_status_enum = DocumentStatus.PROCESSED
                    doc_updated_chunk_count = live_milvus_chunk_count
                    doc_updated_error_msg = None
                elif doc_updated_status_enum == DocumentStatus.PROCESSED and doc_updated_chunk_count != live_milvus_chunk_count:
                    doc_needs_update = True
                    doc_updated_chunk_count = live_milvus_chunk_count
            elif live_milvus_chunk_count == 0:
                if doc_updated_status_enum == DocumentStatus.PROCESSED:
                    doc_needs_update = True
                    doc_updated_status_enum = DocumentStatus.ERROR
                    doc_updated_chunk_count = 0
                    doc_updated_error_msg = (doc_updated_error_msg or "") + " Processed data missing."
                elif doc_updated_status_enum == DocumentStatus.ERROR and doc_updated_chunk_count != 0:
                    doc_needs_update = True
                    doc_updated_chunk_count = 0
        except Exception as e:
            check_log.exception("Unexpected error during Milvus count check for list item", error=str(e))
            live_milvus_chunk_count = -1
            if doc_updated_status_enum != DocumentStatus.ERROR:
                doc_needs_update = True
                doc_updated_status_enum = DocumentStatus.ERROR
                doc_updated_error_msg = (doc_updated_error_msg or "") + f" Error checking Milvus."

        return {
            "db_data": doc_db_data,
            "needs_update": doc_needs_update,
            "updated_status_enum": doc_updated_status_enum,
            "updated_chunk_count": doc_updated_chunk_count,
            "final_error_message": doc_updated_error_msg,
            "live_gcs_exists": live_gcs_exists, # Renamed field
            "live_milvus_chunk_count": live_milvus_chunk_count
        }
    # --- End inner function ---

    list_log.info(f"Performing live checks for {len(documents_db)} documents concurrently...")
    check_tasks = [check_single_document(doc) for doc in documents_db]
    check_results = await asyncio.gather(*check_tasks)
    list_log.info("Live checks completed.")

    # Update DB if needed (No changes needed here)
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

    # Build final response
    final_response_items = []
    for result in check_results:
         doc_id_str = str(result["db_data"]["id"])
         current_data = updated_doc_data_map.get(doc_id_str, result["db_data"])
         # Use renamed field in response
         final_response_items.append(StatusResponse(
            document_id=doc_id_str, company_id=str(current_data['company_id']),
            status=current_data['status'], file_name=current_data.get('file_name'),
            file_type=current_data.get('file_type'), file_path=current_data.get('file_path'),
            chunk_count=current_data.get('chunk_count', 0),
            gcs_exists=result["live_gcs_exists"], # Use renamed field
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
                chunk_count=None, error_message=None # Clear error message on retry
            )
        retry_log.info("Document status updated to 'processing' for retry.")
    except Exception as e:
        retry_log.exception("Failed to update document status for retry", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error updating status for retry.")

    # Re-queue Celery task (No change needed)
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
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB, GCS, Milvus)"}, # Updated description
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
    gcs_client: GCSClient = Depends(get_gcs_client), # Uses GCS Client
):
    """
    Deletes a document: removes from Milvus (using corrected sync helper), GCS, and PostgreSQL.
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

    # 1. Delete from Milvus (using corrected sync helper)
    delete_log.info("Attempting to delete chunks from Milvus (pymilvus)...")
    loop = asyncio.get_running_loop()
    try:
        # Use the corrected sync delete helper
        milvus_deleted = await loop.run_in_executor(None, _delete_milvus_sync, str(document_id), company_id)
        if milvus_deleted: delete_log.info("Milvus delete command executed successfully (pymilvus helper).")
        else:
            # _delete_milvus_sync now logs its own errors, just report failure here
            errors.append("Failed Milvus delete (pymilvus helper returned False)")
            delete_log.warning("Milvus delete operation reported failure (check helper logs).")
    except Exception as e:
        # Catch errors from run_in_executor or potential helper errors not caught inside
        delete_log.exception("Unexpected error during Milvus delete execution via helper", error=str(e))
        errors.append(f"Milvus delete exception via helper: {type(e).__name__}")

    # 2. Delete from GCS (No change needed here)
    gcs_path = doc_data.get('file_path')
    if gcs_path:
        delete_log.info("Attempting to delete file from GCS...", object_name=gcs_path)
        try:
            await gcs_client.delete_file_async(gcs_path)
            delete_log.info("Successfully deleted file from GCS.")
        except GCSClientError as gce:
            delete_log.error("Failed to delete file from GCS", object_name=gcs_path, error=str(gce))
            errors.append(f"GCS delete failed: {gce}")
        except Exception as e:
            delete_log.exception("Unexpected error during GCS delete", error=str(e))
            errors.append(f"GCS delete exception: {type(e).__name__}")
    else:
        delete_log.warning("Skipping GCS delete: file path not found in DB record.")
        errors.append("GCS path unknown in DB.")

    # 3. Delete from PostgreSQL (No change needed here)
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
    file_path: Optional[str] = Field(None, description="Path to the original file in GCS.")
    metadata: Optional[Dict[str, Any]] = None
    status: str
    chunk_count: Optional[int] = 0
    error_message: Optional[str] = None
    uploaded_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # --- RENAMED FIELD: gcs_exists ---
    gcs_exists: Optional[bool] = Field(None, description="Indicates if the original file currently exists in GCS.")
    # --- REMOVED minio_exists ---
    # minio_exists: Optional[bool] = None
    milvus_chunk_count: Optional[int] = Field(None, description="Live count of chunks found in Milvus for this document (-1 if check failed).")
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
        # --- UPDATED EXAMPLE: Use file_path and gcs_exists ---
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
                "gcs_exists": True, # Updated field name
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
                        "file_path": "51a66c8f-f6b1-43bd-8038-8768471a8b09/52ad2ba8-cab9-4108-a504-b9822fe99bdc/Anexo-00-Modificaciones-de-la-Guia-5.1.0.pdf",
                        "metadata": {"source": "manual upload", "version": "1.1"},
                        "status": DocumentStatus.ERROR.value,
                        "chunk_count": 0,
                        "error_message": "Processing timed out after 600 seconds.",
                        "uploaded_at": "2025-04-19T19:42:38.671016Z",
                        "updated_at": "2025-04-19T19:42:42.337854Z",
                        "gcs_exists": True, # Updated field name
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
POSTGRES_K8S_SVC = "postgresql-service.nyro-develop.svc.cluster.local"
MILVUS_K8S_SVC = "milvus-standalone.nyro-develop.svc.cluster.local"
REDIS_K8S_SVC = "redis-service-master.nyro-develop.svc.cluster.local"
EMBEDDING_SERVICE_K8S_SVC = "embedding-service.nyro-develop.svc.cluster.local"
DOCPROC_SERVICE_K8S_SVC = "docproc-service.nyro-develop.svc.cluster.local"

# --- Defaults ---
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex"
POSTGRES_K8S_USER_DEFAULT = "postgres"
MILVUS_K8S_PORT_DEFAULT = 19530
MILVUS_DEFAULT_COLLECTION = "document_chunks_minilm"
MILVUS_DEFAULT_INDEX_PARAMS = '{"metric_type": "IP", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 256}}'
MILVUS_DEFAULT_SEARCH_PARAMS = '{"metric_type": "IP", "params": {"ef": 128}}'
DEFAULT_EMBEDDING_DIM = 384
DEFAULT_TIKTOKEN_ENCODING = "cl100k_base"

DEFAULT_EMBEDDING_SERVICE_URL = f"http://{EMBEDDING_SERVICE_K8S_SVC}:8003/api/v1/embed"
DEFAULT_DOCPROC_SERVICE_URL = f"http://{DOCPROC_SERVICE_K8S_SVC}:8005/api/v1/process"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env', env_prefix='INGEST_', env_file_encoding='utf-8',
        case_sensitive=False, extra='ignore'
    )

    PROJECT_NAME: str = "Atenex Ingest Service"
    API_V1_STR: str = "/api/v1/ingest"
    LOG_LEVEL: str = "INFO"

    CELERY_BROKER_URL: RedisDsn = Field(default_factory=lambda: RedisDsn(f"redis://{REDIS_K8S_SVC}:6379/0"))
    CELERY_RESULT_BACKEND: RedisDsn = Field(default_factory=lambda: RedisDsn(f"redis://{REDIS_K8S_SVC}:6379/1"))

    POSTGRES_USER: str = POSTGRES_K8S_USER_DEFAULT
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = POSTGRES_K8S_SVC
    POSTGRES_PORT: int = POSTGRES_K8S_PORT_DEFAULT
    POSTGRES_DB: str = POSTGRES_K8S_DB_DEFAULT

    MILVUS_URI: str = Field(default=f"http://{MILVUS_K8S_SVC}:{MILVUS_K8S_PORT_DEFAULT}")
    MILVUS_COLLECTION_NAME: str = MILVUS_DEFAULT_COLLECTION
    MILVUS_GRPC_TIMEOUT: int = 10
    MILVUS_CONTENT_FIELD: str = "content"
    MILVUS_EMBEDDING_FIELD: str = "embedding"
    MILVUS_CONTENT_FIELD_MAX_LENGTH: int = 20000
    MILVUS_INDEX_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_INDEX_PARAMS))
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_SEARCH_PARAMS))

    GCS_BUCKET_NAME: str = Field(default="atenex", description="Name of the Google Cloud Storage bucket for storing original files.")

    EMBEDDING_DIMENSION: int = Field(default=DEFAULT_EMBEDDING_DIM, description="Dimension of embeddings expected from the embedding service, used for Milvus schema.")
    INGEST_EMBEDDING_SERVICE_URL: AnyHttpUrl = Field(default=DEFAULT_EMBEDDING_SERVICE_URL, description="URL of the external embedding service.")
    INGEST_DOCPROC_SERVICE_URL: AnyHttpUrl = Field(default=DEFAULT_DOCPROC_SERVICE_URL, description="URL of the external document processing service.")

    TIKTOKEN_ENCODING_NAME: str = Field(default=DEFAULT_TIKTOKEN_ENCODING, description="Name of the tiktoken encoding to use for token counting.")

    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 3
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    SUPPORTED_CONTENT_TYPES: List[str] = Field(default=[
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
        "text/plain",
        "text/markdown",
        "text/html"
    ])

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
        logging.debug(f"Using EMBEDDING_DIMENSION for Milvus schema: {v}")
        return v

    @field_validator('POSTGRES_PASSWORD', mode='before')
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
             if "." not in v: # Assume it's a K8s service name without scheme
                 return f"http://{v}"
             # If it has a dot but no scheme, it's likely an invalid format
             raise ValueError(f"Invalid MILVUS_URI format: '{v}'. Must start with 'http://' or 'https://' or be a valid service name.")
        return v

    @field_validator('INGEST_EMBEDDING_SERVICE_URL', 'INGEST_DOCPROC_SERVICE_URL', mode='before')
    @classmethod
    def assemble_service_url(cls, v: Optional[str], info: ValidationInfo) -> str:
        default_map = {
            "INGEST_EMBEDDING_SERVICE_URL": DEFAULT_EMBEDDING_SERVICE_URL,
            "INGEST_DOCPROC_SERVICE_URL": DEFAULT_DOCPROC_SERVICE_URL
        }
        # Ensure info.field_name is not None before using it as a key
        default_url_key = str(info.field_name) if info.field_name else ""
        default_url = default_map.get(default_url_key, "")
        
        url_to_validate = v or default_url
        if not url_to_validate: # Should not happen if defaults are correctly set
            raise ValueError(f"URL for {default_url_key} cannot be empty.")
            
        return str(AnyHttpUrl(url_to_validate))


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
    temp_log.info(f"  PROJECT_NAME:                 {settings.PROJECT_NAME}")
    temp_log.info(f"  LOG_LEVEL:                    {settings.LOG_LEVEL}")
    temp_log.info(f"  API_V1_STR:                   {settings.API_V1_STR}")
    temp_log.info(f"  CELERY_BROKER_URL:            {settings.CELERY_BROKER_URL}")
    temp_log.info(f"  CELERY_RESULT_BACKEND:        {settings.CELERY_RESULT_BACKEND}")
    temp_log.info(f"  POSTGRES_SERVER:              {settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}")
    temp_log.info(f"  POSTGRES_DB:                  {settings.POSTGRES_DB}")
    temp_log.info(f"  POSTGRES_USER:                {settings.POSTGRES_USER}")
    temp_log.info(f"  POSTGRES_PASSWORD:            {'*** SET ***' if settings.POSTGRES_PASSWORD and settings.POSTGRES_PASSWORD.get_secret_value() else '!!! NOT SET !!!'}")
    temp_log.info(f"  MILVUS_URI:                   {settings.MILVUS_URI}")
    temp_log.info(f"  MILVUS_COLLECTION_NAME:       {settings.MILVUS_COLLECTION_NAME}")
    temp_log.info(f"  GCS_BUCKET_NAME:              {settings.GCS_BUCKET_NAME}")
    temp_log.info(f"  EMBEDDING_DIMENSION (Milvus): {settings.EMBEDDING_DIMENSION}")
    temp_log.info(f"  INGEST_EMBEDDING_SERVICE_URL: {settings.INGEST_EMBEDDING_SERVICE_URL}")
    temp_log.info(f"  INGEST_DOCPROC_SERVICE_URL:   {settings.INGEST_DOCPROC_SERVICE_URL}")
    temp_log.info(f"  TIKTOKEN_ENCODING_NAME:       {settings.TIKTOKEN_ENCODING_NAME}")
    temp_log.info(f"  SUPPORTED_CONTENT_TYPES:      {settings.SUPPORTED_CONTENT_TYPES}")
    temp_log.info(f"------------------------------------")

except (ValidationError, ValueError) as e:
    error_details = ""
    if isinstance(e, ValidationError):
        try: error_details = f"\nValidation Errors:\n{e.json(indent=2)}"
        except Exception: error_details = f"\nRaw Errors: {e.errors()}"
    else: # ValueError
        error_details = f"\nError: {str(e)}"
    temp_log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    temp_log.critical(f"! FATAL: Ingest Service configuration validation failed:{error_details}")
    temp_log.critical(f"! Check environment variables (prefixed with INGEST_) or .env file.")
    temp_log.critical(f"! Original Error Type: {type(e).__name__}")
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

# --- Synchronous Imports ---
from sqlalchemy import (
    create_engine, text, Engine, Connection, Table, MetaData, Column,
    Uuid, Integer, Text, JSON, String, DateTime, UniqueConstraint, ForeignKeyConstraint, Index
)
# --- FIX: Import dialect-specific insert for on_conflict ---
from sqlalchemy.dialects.postgresql import insert as pg_insert, JSONB # Use JSONB and import postgresql insert
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.models.domain import DocumentStatus

log = structlog.get_logger(__name__)

# --- Async Pool (for API) ---
_pool: Optional[asyncpg.Pool] = None

# --- Sync Engine (for Worker) ---
_sync_engine: Optional[Engine] = None
_sync_engine_dsn: Optional[str] = None

# --- Sync SQLAlchemy Metadata ---
_metadata = MetaData()
document_chunks_table = Table(
    'document_chunks',
    _metadata,
    Column('id', Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")), # Use Uuid type
    Column('document_id', Uuid(as_uuid=True), nullable=False),
    Column('company_id', Uuid(as_uuid=True), nullable=False),
    Column('chunk_index', Integer, nullable=False),
    Column('content', Text, nullable=False),
    Column('metadata', JSONB), # Use JSONB for better indexing/querying if needed
    Column('embedding_id', String(255)), # Milvus PK (often string)
    Column('created_at', DateTime(timezone=True), server_default=text("timezone('utc', now())")),
    Column('vector_status', String(50), default='pending'),
    UniqueConstraint('document_id', 'chunk_index', name='uq_document_chunk_index'),
    ForeignKeyConstraint(['document_id'], ['documents.id'], name='fk_document_chunks_document', ondelete='CASCADE'),
    # Optional FK to companies table
    # ForeignKeyConstraint(['company_id'], ['companies.id'], name='fk_document_chunks_company', ondelete='CASCADE'),
    Index('idx_document_chunks_document_id', 'document_id'), # Explicit index definition
    Index('idx_document_chunks_company_id', 'company_id'),
    Index('idx_document_chunks_embedding_id', 'embedding_id'),
)


# --- Async Pool Management ---
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
                # Handle potential double-encoded JSON if DB returns string
                if isinstance(value, str):
                    try:
                        return json.loads(value)
                    except json.JSONDecodeError:
                         log.warning("Failed to decode JSON string from DB", raw_value=value)
                         return None # Or return the raw string?
                return value # Assume it's already decoded by asyncpg

            async def init_connection(conn):
                await conn.set_type_codec(
                    'jsonb',
                    encoder=_json_encoder,
                    decoder=_json_decoder,
                    schema='pg_catalog',
                    format='text' # Important for custom encoder/decoder
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
                statement_cache_size=0 # Disable cache for safety with type codecs
            )
            log.info("PostgreSQL async connection pool created successfully.")
        except (asyncpg.exceptions.InvalidPasswordError, OSError, ConnectionRefusedError) as conn_err:
            log.critical("CRITICAL: Failed to connect to PostgreSQL (async pool)", error=str(conn_err), exc_info=True)
            _pool = None
            raise ConnectionError(f"Failed to connect to PostgreSQL (async pool): {conn_err}") from conn_err
        except Exception as e:
            log.critical("CRITICAL: Failed to create PostgreSQL async connection pool", error=str(e), exc_info=True)
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


# --- Synchronous Engine Management ---
# LLM_FLAG: SENSITIVE_CODE_BLOCK_START - DB Sync Engine Management
def get_sync_engine() -> Engine:
    """
    Creates and returns a SQLAlchemy synchronous engine instance.
    Caches the engine globally per process.
    """
    global _sync_engine, _sync_engine_dsn
    sync_log = log.bind(component="SyncEngine")

    if not _sync_engine_dsn:
         _sync_engine_dsn = f"postgresql+psycopg2://{settings.POSTGRES_USER}:{settings.POSTGRES_PASSWORD.get_secret_value()}@{settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}"

    if _sync_engine is None:
        sync_log.info("Creating SQLAlchemy synchronous engine...")
        try:
            _sync_engine = create_engine(
                _sync_engine_dsn,
                pool_size=5,
                max_overflow=5,
                pool_timeout=30,
                pool_recycle=1800,
                json_serializer=json.dumps, # Ensure JSON is serialized correctly
                json_deserializer=json.loads # Ensure JSON is deserialized correctly
            )
            with _sync_engine.connect() as conn_test:
                conn_test.execute(text("SELECT 1"))
            sync_log.info("SQLAlchemy synchronous engine created and tested successfully.")
        except ImportError as ie:
            sync_log.critical("SQLAlchemy or psycopg2 not installed! Cannot create sync engine.", error=str(ie))
            raise RuntimeError("Missing SQLAlchemy/psycopg2 dependency") from ie
        except SQLAlchemyError as sa_err:
            sync_log.critical("Failed to create or connect SQLAlchemy synchronous engine", error=str(sa_err), exc_info=True)
            _sync_engine = None
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


# --- Synchronous Document Operations ---

# LLM_FLAG: FUNCTIONAL_CODE - Synchronous DB update function for documents table
def set_status_sync(
    engine: Engine,
    document_id: uuid.UUID,
    status: DocumentStatus,
    chunk_count: Optional[int] = None,
    error_message: Optional[str] = None
) -> bool:
    """
    Synchronously updates the status, chunk_count, and/or error_message of a document.
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

    if status == DocumentStatus.ERROR:
        set_clauses.append("error_message = :error_message")
        params["error_message"] = error_message
    else:
        set_clauses.append("error_message = NULL")

    set_clause_str = ", ".join(set_clauses)
    query = text(f"UPDATE documents SET {set_clause_str} WHERE id = :doc_id")

    try:
        with engine.connect() as connection:
            with connection.begin():
                result = connection.execute(query, params)
            if result.rowcount == 0:
                update_log.warning("Attempted to update status for non-existent document_id (sync).")
                return False
            elif result.rowcount == 1:
                update_log.info("Document status updated successfully in PostgreSQL (sync).", updated_fields=set_clauses)
                return True
            else:
                 update_log.error("Unexpected number of rows updated (sync).", row_count=result.rowcount)
                 return False
    except SQLAlchemyError as e:
        update_log.error("SQLAlchemyError during synchronous status update", error=str(e), query=str(query), params=params, exc_info=True)
        raise Exception(f"Sync DB update failed: {e}") from e
    except Exception as e:
        update_log.error("Unexpected error during synchronous status update", error=str(e), query=str(query), params=params, exc_info=True)
        raise Exception(f"Unexpected sync DB update error: {e}") from e

# LLM_FLAG: NEW FUNCTION - Synchronous bulk chunk insertion for document_chunks table
def bulk_insert_chunks_sync(engine: Engine, chunks_data: List[Dict[str, Any]]) -> int:
    """
    Synchronously inserts multiple document chunks into the document_chunks table.
    Uses SQLAlchemy Core API for efficient bulk insertion and handles conflicts.
    """
    if not chunks_data:
        log.warning("bulk_insert_chunks_sync called with empty data list.")
        return 0

    insert_log = log.bind(component="SyncDBBulkInsert", num_chunks=len(chunks_data), document_id=chunks_data[0].get('document_id'))
    insert_log.info("Attempting synchronous bulk insert of document chunks.")

    prepared_data = []
    for chunk in chunks_data:
        prepared_chunk = chunk.copy()
        # Ensure UUIDs are handled correctly by SQLAlchemy/psycopg2
        if isinstance(prepared_chunk.get('document_id'), uuid.UUID):
            prepared_chunk['document_id'] = prepared_chunk['document_id'] # Keep as UUID
        if isinstance(prepared_chunk.get('company_id'), uuid.UUID):
             prepared_chunk['company_id'] = prepared_chunk['company_id'] # Keep as UUID

        if 'metadata' in prepared_chunk and not isinstance(prepared_chunk['metadata'], (dict, list, type(None))):
             log.warning("Invalid metadata type for chunk, attempting conversion", chunk_index=prepared_chunk.get('chunk_index'))
             prepared_chunk['metadata'] = {}
        elif isinstance(prepared_chunk['metadata'], dict):
             # Optionally clean metadata further if needed (e.g., remove non-JSON serializable types)
             pass

        prepared_data.append(prepared_chunk)

    try:
        with engine.connect() as connection:
            with connection.begin(): # Start transaction
                # --- FIX: Use pg_insert from dialect import ---
                stmt = pg_insert(document_chunks_table).values(prepared_data)
                # Specify the conflict target and action
                stmt = stmt.on_conflict_do_nothing(
                     index_elements=['document_id', 'chunk_index']
                )
                result = connection.execute(stmt)

            inserted_count = len(prepared_data) # Assume success if no exception
            insert_log.info("Bulk insert statement executed successfully (ON CONFLICT DO NOTHING).", intended_count=len(prepared_data), reported_rowcount=result.rowcount if result else -1)
            return inserted_count

    except SQLAlchemyError as e:
        insert_log.error("SQLAlchemyError during synchronous bulk chunk insert", error=str(e), exc_info=True)
        raise Exception(f"Sync DB bulk chunk insert failed: {e}") from e
    except Exception as e:
        insert_log.error("Unexpected error during synchronous bulk chunk insert", error=str(e), exc_info=True)
        raise Exception(f"Unexpected sync DB bulk chunk insert error: {e}") from e


# --- Async Document Operations ---

# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH create_document_record DB logic lightly
async def create_document_record(
    conn: asyncpg.Connection,
    doc_id: uuid.UUID,
    company_id: uuid.UUID,
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
    # asyncpg handles dict -> jsonb directly with codec
    params = [ doc_id, company_id, filename, file_type, file_path, metadata, status.value, 0 ]
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
        return dict(record) # asyncpg handles jsonb decoding with codec
    except Exception as e: get_log.error("Failed to get document by ID (async)", error=str(e), exc_info=True); raise

# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH list_documents_paginated DB logic lightly
async def list_documents_paginated(conn: asyncpg.Connection, company_id: uuid.UUID, limit: int, offset: int) -> Tuple[List[Dict[str, Any]], int]:
    """Lista documentos paginados para una compañía y devuelve el conteo total (Async)."""
    query = """SELECT id, company_id, file_name, file_type, file_path, metadata, status, chunk_count, error_message, uploaded_at, updated_at, COUNT(*) OVER() AS total_count FROM documents WHERE company_id = $1 ORDER BY updated_at DESC LIMIT $2 OFFSET $3;"""
    list_log = log.bind(company_id=str(company_id), limit=limit, offset=offset)
    try:
        rows = await conn.fetch(query, company_id, limit, offset); total = 0; results = []
        if rows:
            total = rows[0]['total_count']
            results = []
            for r in rows:
                row_dict = dict(r)
                row_dict.pop('total_count', None)
                # metadata should be dict due to codec
                results.append(row_dict)
        list_log.debug("Fetched paginated documents (async)", count=len(results), total=total)
        return results, total
    except Exception as e: list_log.error("Failed to list paginated documents (async)", error=str(e), exc_info=True); raise

# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH delete_document DB logic lightly
async def delete_document(conn: asyncpg.Connection, doc_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    """Elimina un documento verificando la compañía (Async). Assumes ON DELETE CASCADE."""
    query = "DELETE FROM documents WHERE id = $1 AND company_id = $2 RETURNING id;"
    delete_log = log.bind(document_id=str(doc_id), company_id=str(company_id))
    try:
        deleted_id = await conn.fetchval(query, doc_id, company_id)
        if deleted_id:
            delete_log.info("Document deleted from PostgreSQL (async), associated chunks deleted via CASCADE.", deleted_id=str(deleted_id))
            return True
        else:
            delete_log.warning("Document not found or company mismatch during delete attempt (async).")
            return False
    except Exception as e:
        delete_log.error("Error deleting document record (async)", error=str(e), exc_info=True)
        raise
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
    port = 8001
    log_level_str = settings.LOG_LEVEL.lower()
    print(f"----- Starting {settings.PROJECT_NAME} locally on port {port} -----")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True, log_level=log_level_str)
```

## File: `app\models\__init__.py`
```py

```

## File: `app\models\domain.py`
```py
# ingest-service/app/models/domain.py
import uuid
from enum import Enum
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any

class DocumentStatus(str, Enum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    PROCESSED = "processed"
    # INDEXED = "indexed" # Merged into PROCESSED
    ERROR = "error"
    PENDING = "pending"

class ChunkVectorStatus(str, Enum):
    """Possible status values for the vector associated with a chunk."""
    PENDING = "pending"     # Initial state before Milvus insertion attempt
    CREATED = "created"     # Successfully inserted into Milvus
    ERROR = "error"         # Failed to insert into Milvus

class DocumentChunkMetadata(BaseModel):
    """Structure for metadata stored in the JSONB field of document_chunks."""
    page: Optional[int] = None
    title: Optional[str] = None
    tokens: Optional[int] = None
    content_hash: Optional[str] = Field(None, max_length=64) # e.g., SHA256 hex digest

class DocumentChunkData(BaseModel):
    """Internal representation of a chunk before DB insertion."""
    document_id: uuid.UUID
    company_id: uuid.UUID
    chunk_index: int
    content: str
    metadata: DocumentChunkMetadata = Field(default_factory=DocumentChunkMetadata)
    embedding_id: Optional[str] = None # Populated after Milvus insertion
    vector_status: ChunkVectorStatus = ChunkVectorStatus.PENDING
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

## File: `app\services\clients\docproc_service_client.py`
```py
# ingest-service/app/services/clients/docproc_service_client.py
from typing import List, Dict, Any, Tuple, Optional
import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, before_sleep_log
import logging

from app.core.config import settings
from app.services.base_client import BaseServiceClient

log = structlog.get_logger(__name__)

class DocProcServiceClientError(Exception):
    """Custom exception for Document Processing Service client errors."""
    def __init__(self, message: str, status_code: int = None, detail: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail

class DocProcServiceClient(BaseServiceClient):
    """
    Client for interacting with the Atenex Document Processing Service.
    """
    def __init__(self, base_url: str = None):
        effective_base_url = base_url or str(settings.INGEST_DOCPROC_SERVICE_URL).rstrip('/')
        
        parsed_url = httpx.URL(effective_base_url)
        self.service_endpoint_path = parsed_url.path
        client_base_url = f"{parsed_url.scheme}://{parsed_url.host}:{parsed_url.port}" if parsed_url.port else f"{parsed_url.scheme}://{parsed_url.host}"

        super().__init__(base_url=client_base_url, service_name="DocProcService")
        self.log = log.bind(service_client="DocProcServiceClient", service_url=effective_base_url)

    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=1, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
        before_sleep=before_sleep_log(log, logging.WARNING)
    )
    async def process_document(
        self,
        file_bytes: bytes,
        original_filename: str,
        content_type: str,
        document_id: Optional[str] = None,
        company_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Sends a document file to the DocProc Service for text extraction and chunking.
        """
        if not file_bytes:
            self.log.error("process_document called with empty file_bytes.")
            raise DocProcServiceClientError("File content cannot be empty.")

        files = {'file': (original_filename, file_bytes, content_type)}
        data: Dict[str, Any] = {
            'original_filename': original_filename,
            'content_type': content_type
        }
        if document_id:
            data['document_id'] = document_id
        if company_id:
            data['company_id'] = company_id
        
        self.log.debug(f"Requesting document processing from {self.service_endpoint_path}", 
                       filename=original_filename, content_type=content_type, size_len=len(file_bytes))
        
        try:
            response = await self._request(
                method="POST",
                endpoint=self.service_endpoint_path,
                files=files,
                data=data
            )
            response_data = response.json()
            
            if "data" not in response_data or \
               "document_metadata" not in response_data["data"] or \
               "chunks" not in response_data["data"]:
                self.log.error("Invalid response format from DocProc Service", response_data_preview=str(response_data)[:200])
                raise DocProcServiceClientError(
                    message="Invalid response format from DocProc Service.",
                    status_code=response.status_code,
                    detail=response_data
                )
            
            self.log.info("Successfully processed document via DocProc Service.", 
                          filename=original_filename, 
                          num_chunks=len(response_data["data"]["chunks"]))
            return response_data

        except httpx.HTTPStatusError as e:
            self.log.error(
                "HTTP error from DocProc Service",
                status_code=e.response.status_code,
                response_text=e.response.text,
                filename=original_filename
            )
            error_detail = e.response.text
            try:
                error_detail_json = e.response.json()
                if isinstance(error_detail_json, dict) and "detail" in error_detail_json:
                    error_detail = error_detail_json["detail"]
            except:
                pass
            
            raise DocProcServiceClientError(
                message=f"DocProc Service returned HTTP error: {e.response.status_code}",
                status_code=e.response.status_code,
                detail=error_detail
            ) from e
        except httpx.RequestError as e:
            self.log.error("Request error calling DocProc Service", error_msg=str(e), filename=original_filename)
            raise DocProcServiceClientError(
                message=f"Request to DocProc Service failed: {type(e).__name__}",
                detail=str(e)
            ) from e
        except Exception as e:
            self.log.exception("Unexpected error in DocProcServiceClient", filename=original_filename)
            raise DocProcServiceClientError(message=f"Unexpected error: {e}") from e

    async def health_check(self) -> bool:
        """
        Checks the health of the Document Processing Service.
        """
        health_log = self.log.bind(action="docproc_health_check")
        try:
            parsed_service_url = httpx.URL(str(settings.INGEST_DOCPROC_SERVICE_URL))
            health_endpoint_url_base = f"{parsed_service_url.scheme}://{parsed_service_url.host}"
            if parsed_service_url.port:
                health_endpoint_url_base += f":{parsed_service_url.port}"
            
            async with httpx.AsyncClient(base_url=health_endpoint_url_base, timeout=5) as client:
                response = await client.get("/health")
            response.raise_for_status()
            health_data = response.json()
            if health_data.get("status") == "ok":
                health_log.info("DocProc Service is healthy.")
                return True
            else:
                health_log.warning("DocProc Service reported unhealthy.", health_data=health_data)
                return False
        except httpx.HTTPStatusError as e:
            health_log.error("DocProc Service health check failed (HTTP error)", status_code=e.response.status_code, response_text=e.response.text)
            return False
        except httpx.RequestError as e:
            health_log.error("DocProc Service health check failed (Request error)", error_msg=str(e))
            return False
        except Exception as e:
            health_log.exception("Unexpected error during DocProc Service health check")
            return False
```

## File: `app\services\clients\embedding_service_client.py`
```py
# ingest-service/app/services/clients/embedding_service_client.py
from typing import List, Dict, Any, Tuple
import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, before_sleep_log
import logging # Required for before_sleep_log

from app.core.config import settings
from app.services.base_client import BaseServiceClient # Reutilizar BaseServiceClient si es adecuado

log = structlog.get_logger(__name__)

class EmbeddingServiceClientError(Exception):
    """Custom exception for Embedding Service client errors."""
    def __init__(self, message: str, status_code: int = None, detail: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail

class EmbeddingServiceClient(BaseServiceClient):
    """
    Client for interacting with the Atenex Embedding Service.
    """
    def __init__(self, base_url: str = None):
        effective_base_url = base_url or str(settings.INGEST_EMBEDDING_SERVICE_URL).rstrip('/')
        # The BaseServiceClient expects the full base URL up to, but not including, the specific endpoint path.
        # So, if INGEST_EMBEDDING_SERVICE_URL is "http://host/api/v1/embed",
        # base_url for BaseServiceClient should be "http://host"
        # and the endpoint path used in _request would be "/api/v1/embed".
        # However, INGEST_EMBEDDING_SERVICE_URL already points to the specific endpoint.
        # For now, let's adapt by making the `endpoint` parameter in `get_embeddings` an empty string.
        # A more robust BaseServiceClient might take the full path in _request.

        # Let's parse the URL to get the base path for BaseServiceClient
        parsed_url = httpx.URL(effective_base_url)
        self.service_endpoint_path = parsed_url.path # e.g., /api/v1/embed
        client_base_url = f"{parsed_url.scheme}://{parsed_url.host}:{parsed_url.port}" if parsed_url.port else f"{parsed_url.scheme}://{parsed_url.host}"

        super().__init__(base_url=client_base_url, service_name="EmbeddingService")
        self.log = log.bind(service_client="EmbeddingServiceClient", service_url=effective_base_url)

    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=1, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
        before_sleep=before_sleep_log(log, logging.WARNING) # Use log from structlog
    )
    async def get_embeddings(self, texts: List[str]) -> Tuple[List[List[float]], Dict[str, Any]]:
        """
        Sends a list of texts to the Embedding Service and returns their embeddings.

        Args:
            texts: A list of strings to embed.

        Returns:
            A tuple containing:
                - A list of embeddings (list of lists of floats).
                - ModelInfo dictionary.

        Raises:
            EmbeddingServiceClientError: If the request fails or the service returns an error.
        """
        if not texts:
            self.log.warning("get_embeddings called with an empty list of texts.")
            return [], {"model_name": "unknown", "dimension": 0}

        request_payload = {"texts": texts}
        self.log.debug(f"Requesting embeddings for {len(texts)} texts from {self.service_endpoint_path}", num_texts=len(texts))

        try:
            response = await self._request(
                method="POST",
                endpoint=self.service_endpoint_path, # Use the parsed endpoint path
                json=request_payload
            )
            response_data = response.json()

            if "embeddings" not in response_data or "model_info" not in response_data:
                self.log.error("Invalid response format from Embedding Service", response_data=response_data)
                raise EmbeddingServiceClientError(
                    message="Invalid response format from Embedding Service.",
                    status_code=response.status_code,
                    detail=response_data
                )

            embeddings = response_data["embeddings"]
            model_info = response_data["model_info"]
            self.log.info(f"Successfully retrieved {len(embeddings)} embeddings.", model_name=model_info.get("model_name"))
            return embeddings, model_info

        except httpx.HTTPStatusError as e:
            self.log.error(
                "HTTP error from Embedding Service",
                status_code=e.response.status_code,
                response_text=e.response.text
            )
            error_detail = e.response.text
            try: error_detail = e.response.json()
            except: pass
            raise EmbeddingServiceClientError(
                message=f"Embedding Service returned error: {e.response.status_code}",
                status_code=e.response.status_code,
                detail=error_detail
            ) from e
        except httpx.RequestError as e:
            self.log.error("Request error calling Embedding Service", error=str(e))
            raise EmbeddingServiceClientError(
                message=f"Request to Embedding Service failed: {type(e).__name__}",
                detail=str(e)
            ) from e
        except Exception as e:
            self.log.exception("Unexpected error in EmbeddingServiceClient")
            raise EmbeddingServiceClientError(message=f"Unexpected error: {e}") from e

    async def health_check(self) -> bool:
        """
        Checks the health of the Embedding Service.
        Assumes the embedding service has a /health endpoint at its root.
        """
        health_log = self.log.bind(action="health_check")
        try:
            # BaseServiceClient's _request uses the base_url.
            # The health endpoint is typically at the root of the service, not under /api/v1/embed
            # We need to construct the health URL carefully.
            # If embedding_service_url is "http://host:port/api/v1/embed", health is "http://host:port/health"
            
            parsed_service_url = httpx.URL(str(settings.INGEST_EMBEDDING_SERVICE_URL)) # Full URL to embed endpoint
            health_endpoint_url_base = f"{parsed_service_url.scheme}://{parsed_service_url.host}"
            if parsed_service_url.port:
                health_endpoint_url_base += f":{parsed_service_url.port}"
            
            async with httpx.AsyncClient(base_url=health_endpoint_url_base, timeout=5) as client:
                response = await client.get("/health")
            response.raise_for_status()
            health_data = response.json()
            if health_data.get("status") == "ok" and health_data.get("model_status") == "loaded":
                health_log.info("Embedding Service is healthy and model is loaded.")
                return True
            else:
                health_log.warning("Embedding Service reported unhealthy or model not loaded.", health_data=health_data)
                return False
        except httpx.HTTPStatusError as e:
            health_log.error("Embedding Service health check failed (HTTP error)", status_code=e.response.status_code, response_text=e.response.text)
            return False
        except httpx.RequestError as e:
            health_log.error("Embedding Service health check failed (Request error)", error=str(e))
            return False
        except Exception as e:
            health_log.exception("Unexpected error during Embedding Service health check")
            return False
```

## File: `app\services\gcs_client.py`
```py
import structlog
import asyncio
from typing import Optional
from google.cloud import storage
from google.api_core.exceptions import NotFound, GoogleAPIError
from app.core.config import settings

log = structlog.get_logger(__name__)

class GCSClientError(Exception):
    """Custom exception for GCS related errors."""
    def __init__(self, message: str, original_exception: Optional[Exception] = None):
        self.message = message
        self.original_exception = original_exception
        super().__init__(message)

    def __str__(self):
        if self.original_exception:
            return f"{self.message}: {type(self.original_exception).__name__} - {str(self.original_exception)}"
        return self.message

class GCSClient:
    """Client to interact with Google Cloud Storage using configured settings."""
    def __init__(self, bucket_name: Optional[str] = None):
        self.bucket_name = bucket_name or settings.GCS_BUCKET_NAME
        self._client = storage.Client()
        self._bucket = self._client.bucket(self.bucket_name)
        self.log = log.bind(gcs_bucket=self.bucket_name)

    async def upload_file_async(self, object_name: str, data: bytes, content_type: str) -> str:
        self.log.info("Uploading file to GCS...", object_name=object_name, content_type=content_type, length=len(data))
        loop = asyncio.get_running_loop()
        def _upload():
            blob = self._bucket.blob(object_name)
            blob.upload_from_string(data, content_type=content_type)
            return object_name
        try:
            uploaded_object_name = await loop.run_in_executor(None, _upload)
            self.log.info("File uploaded successfully to GCS", object_name=object_name)
            return uploaded_object_name
        except GoogleAPIError as e:
            self.log.error("GCS upload failed", error=str(e))
            raise GCSClientError(f"GCS error uploading {object_name}", e) from e
        except Exception as e:
            self.log.exception("Unexpected error during GCS upload", error=str(e))
            raise GCSClientError(f"Unexpected error uploading {object_name}", e) from e

    async def download_file_async(self, object_name: str, file_path: str):
        self.log.info("Downloading file from GCS...", object_name=object_name, target_path=file_path)
        loop = asyncio.get_running_loop()
        def _download():
            blob = self._bucket.blob(object_name)
            blob.download_to_filename(file_path)
        try:
            await loop.run_in_executor(None, _download)
            self.log.info("File downloaded successfully from GCS", object_name=object_name)
        except NotFound as e:
            self.log.error("Object not found in GCS", object_name=object_name)
            raise GCSClientError(f"Object not found in GCS: {object_name}", e) from e
        except GoogleAPIError as e:
            self.log.error("GCS download failed", error=str(e))
            raise GCSClientError(f"GCS error downloading {object_name}", e) from e
        except Exception as e:
            self.log.exception("Unexpected error during GCS download", error=str(e))
            raise GCSClientError(f"Unexpected error downloading {object_name}", e) from e

    async def check_file_exists_async(self, object_name: str) -> bool:
        self.log.debug("Checking file existence in GCS", object_name=object_name)
        loop = asyncio.get_running_loop()
        def _exists():
            blob = self._bucket.blob(object_name)
            return blob.exists()
        try:
            exists = await loop.run_in_executor(None, _exists)
            self.log.debug("File existence check completed in GCS", object_name=object_name, exists=exists)
            return exists
        except Exception as e:
            self.log.exception("Unexpected error during GCS existence check", error=str(e))
            return False

    async def delete_file_async(self, object_name: str):
        self.log.info("Deleting file from GCS...", object_name=object_name)
        loop = asyncio.get_running_loop()
        def _delete():
            blob = self._bucket.blob(object_name)
            blob.delete()
        try:
            await loop.run_in_executor(None, _delete)
            self.log.info("File deleted successfully from GCS", object_name=object_name)
        except NotFound:
            self.log.info("Object already deleted or not found in GCS", object_name=object_name)
        except GoogleAPIError as e:
            self.log.error("GCS delete failed", error=str(e))
            raise GCSClientError(f"GCS error deleting {object_name}", e) from e
        except Exception as e:
            self.log.exception("Unexpected error during GCS delete", error=str(e))
            raise GCSClientError(f"Unexpected error deleting {object_name}", e) from e

    # Synchronous methods for worker compatibility
    def download_file_sync(self, object_name: str, file_path: str):
        self.log.info("Downloading file from GCS (sync)...", object_name=object_name, target_path=file_path)
        try:
            blob = self._bucket.blob(object_name)
            blob.download_to_filename(file_path)
            self.log.info("File downloaded successfully from GCS (sync)", object_name=object_name)
        except NotFound as e:
            self.log.error("Object not found in GCS (sync)", object_name=object_name)
            raise GCSClientError(f"Object not found in GCS: {object_name}", e) from e
        except GoogleAPIError as e:
            self.log.error("GCS download failed (sync)", error=str(e))
            raise GCSClientError(f"GCS error downloading {object_name}", e) from e
        except Exception as e:
            self.log.exception("Unexpected error during GCS download (sync)", error=str(e))
            raise GCSClientError(f"Unexpected error downloading {object_name}", e) from e

    def check_file_exists_sync(self, object_name: str) -> bool:
        self.log.debug("Checking file existence in GCS (sync)", object_name=object_name)
        try:
            blob = self._bucket.blob(object_name)
            exists = blob.exists()
            self.log.debug("File existence check completed in GCS (sync)", object_name=object_name, exists=exists)
            return exists
        except Exception as e:
            self.log.exception("Unexpected error during GCS existence check (sync)", error=str(e))
            return False

    def delete_file_sync(self, object_name: str):
        self.log.info("Deleting file from GCS (sync)...", object_name=object_name)
        try:
            blob = self._bucket.blob(object_name)
            blob.delete()
            self.log.info("File deleted successfully from GCS (sync)", object_name=object_name)
        except NotFound:
            self.log.info("Object already deleted or not found in GCS (sync)", object_name=object_name)
        except GoogleAPIError as e:
            self.log.error("GCS delete failed (sync)", error=str(e))
            raise GCSClientError(f"GCS error deleting {object_name}", e) from e
        except Exception as e:
            self.log.exception("Unexpected error during GCS delete (sync)", error=str(e))
            raise GCSClientError(f"Unexpected error deleting {object_name}", e) from e

```

## File: `app\services\ingest_pipeline.py`
```py
# ingest-service/app/services/ingest_pipeline.py
from __future__ import annotations

import os
import uuid
import hashlib
import structlog
from typing import List, Dict, Any, Optional, Tuple, Union

from app.core.config import settings
log = structlog.get_logger(__name__)

try:
    import tiktoken
    tiktoken_enc = tiktoken.get_encoding(settings.TIKTOKEN_ENCODING_NAME)
    log.info(f"Tiktoken encoder loaded: {settings.TIKTOKEN_ENCODING_NAME}")
except ImportError:
    log.warning("tiktoken not installed, token count metadata will be unavailable.")
    tiktoken_enc = None
except Exception as e:
    log.warning(f"Failed to load tiktoken encoder '{settings.TIKTOKEN_ENCODING_NAME}', token count metadata will be unavailable.", error=str(e))
    tiktoken_enc = None

from pymilvus import (
    Collection, CollectionSchema, FieldSchema, DataType, connections,
    utility, MilvusException
)

from app.models.domain import DocumentChunkMetadata, DocumentChunkData, ChunkVectorStatus

# --- Constantes Milvus ---
MILVUS_COLLECTION_NAME = settings.MILVUS_COLLECTION_NAME
MILVUS_EMBEDDING_DIM = settings.EMBEDDING_DIMENSION
MILVUS_PK_FIELD = "pk_id"
MILVUS_VECTOR_FIELD = settings.MILVUS_EMBEDDING_FIELD
MILVUS_CONTENT_FIELD = settings.MILVUS_CONTENT_FIELD
MILVUS_COMPANY_ID_FIELD = "company_id"
MILVUS_DOCUMENT_ID_FIELD = "document_id"
MILVUS_FILENAME_FIELD = "file_name"
MILVUS_PAGE_FIELD = "page"
MILVUS_TITLE_FIELD = "title"
MILVUS_TOKENS_FIELD = "tokens"
MILVUS_CONTENT_HASH_FIELD = "content_hash"

_milvus_collection_pipeline: Optional[Collection] = None

def _ensure_milvus_connection_and_collection_for_pipeline(alias: str = "pipeline_worker_indexing") -> Collection:
    global _milvus_collection_pipeline
    connect_log = log.bind(milvus_alias=alias, component="MilvusPipelineOps")

    connection_exists = alias in connections.list_connections()
    
    # Re-evaluate connection and collection state more robustly
    if connection_exists:
        try:
            # Test existing connection
            utility.get_connection_addr(alias) # This can raise if connection is stale
            connect_log.debug("Milvus connection alias exists and seems active.", alias=alias)
            if _milvus_collection_pipeline and _milvus_collection_pipeline.name == MILVUS_COLLECTION_NAME:
                 # Basic check, might not guarantee collection is loaded in this specific alias
                pass # Use existing _milvus_collection_pipeline if set
            else: # Collection not set or different, reset flag to force re-initialization
                connection_exists = False # Force re-init logic for collection
        except Exception as conn_check_err:
            connect_log.warning("Error checking existing Milvus connection status, will attempt reconnect.", error=str(conn_check_err))
            try: connections.disconnect(alias)
            except: pass
            connection_exists = False # Force re-init

    if not connection_exists:
        uri = settings.MILVUS_URI
        connect_log.info("Connecting to Milvus for pipeline worker indexing...", uri=uri)
        try:
            connections.connect(alias=alias, uri=uri, timeout=settings.MILVUS_GRPC_TIMEOUT)
            connect_log.info("Connected to Milvus for pipeline worker indexing.")
        except MilvusException as e:
            connect_log.error("Failed to connect to Milvus for pipeline worker indexing.", error=str(e))
            raise ConnectionError(f"Milvus connection failed: {e}") from e
        except Exception as e:
            connect_log.error("Unexpected error connecting to Milvus for pipeline worker indexing.", error=str(e))
            raise ConnectionError(f"Unexpected Milvus connection error: {e}") from e

    # Check and initialize collection if needed
    if _milvus_collection_pipeline is None or _milvus_collection_pipeline.name != MILVUS_COLLECTION_NAME:
        try:
            if not utility.has_collection(MILVUS_COLLECTION_NAME, using=alias):
                connect_log.warning(f"Milvus collection '{MILVUS_COLLECTION_NAME}' not found. Attempting to create.")
                collection_obj = _create_milvus_collection_for_pipeline(alias)
            else:
                collection_obj = Collection(name=MILVUS_COLLECTION_NAME, using=alias)
                connect_log.debug(f"Using existing Milvus collection '{MILVUS_COLLECTION_NAME}'.")
                _check_and_create_indexes_for_pipeline(collection_obj)

            connect_log.info("Loading Milvus collection into memory for indexing...", collection_name=collection_obj.name)
            collection_obj.load()
            connect_log.info("Milvus collection loaded into memory for indexing.")
            _milvus_collection_pipeline = collection_obj
        except MilvusException as coll_err:
             connect_log.error("Failed during Milvus collection access/load for indexing", error=str(coll_err), exc_info=True)
             raise RuntimeError(f"Milvus collection access error for indexing: {coll_err}") from coll_err
        except Exception as e:
             connect_log.error("Unexpected error during Milvus collection access for indexing", error=str(e), exc_info=True)
             raise RuntimeError(f"Unexpected Milvus collection error for indexing: {e}") from e
    
    if not isinstance(_milvus_collection_pipeline, Collection):
        connect_log.critical("Milvus collection object is unexpectedly None or invalid type after initialization for indexing.")
        raise RuntimeError("Failed to obtain a valid Milvus collection object for indexing.")
    return _milvus_collection_pipeline


def _create_milvus_collection_for_pipeline(alias: str) -> Collection:
    create_log = log.bind(collection_name=MILVUS_COLLECTION_NAME, embedding_dim=MILVUS_EMBEDDING_DIM, component="MilvusPipelineOps")
    create_log.info("Defining schema for new Milvus collection (pipeline).")
    fields = [
        FieldSchema(name=MILVUS_PK_FIELD, dtype=DataType.VARCHAR, max_length=255, is_primary=True),
        FieldSchema(name=MILVUS_VECTOR_FIELD, dtype=DataType.FLOAT_VECTOR, dim=MILVUS_EMBEDDING_DIM),
        FieldSchema(name=MILVUS_CONTENT_FIELD, dtype=DataType.VARCHAR, max_length=settings.MILVUS_CONTENT_FIELD_MAX_LENGTH),
        FieldSchema(name=MILVUS_COMPANY_ID_FIELD, dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name=MILVUS_DOCUMENT_ID_FIELD, dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name=MILVUS_FILENAME_FIELD, dtype=DataType.VARCHAR, max_length=512),
        FieldSchema(name=MILVUS_PAGE_FIELD, dtype=DataType.INT64, default_value=-1),
        FieldSchema(name=MILVUS_TITLE_FIELD, dtype=DataType.VARCHAR, max_length=512, default_value=""),
        FieldSchema(name=MILVUS_TOKENS_FIELD, dtype=DataType.INT64, default_value=-1),
        FieldSchema(name=MILVUS_CONTENT_HASH_FIELD, dtype=DataType.VARCHAR, max_length=64)
    ]
    schema = CollectionSchema(
        fields, description="Atenex Document Chunks with Enhanced Metadata", enable_dynamic_field=False
    )
    create_log.info("Schema defined. Creating collection (pipeline)...")
    try:
        collection = Collection(
            name=MILVUS_COLLECTION_NAME, schema=schema, using=alias, consistency_level="Strong"
        )
        create_log.info(f"Collection '{MILVUS_COLLECTION_NAME}' created (pipeline). Creating indexes...")
        index_params = settings.MILVUS_INDEX_PARAMS
        create_log.info("Creating HNSW index for vector field (pipeline)", field_name=MILVUS_VECTOR_FIELD, index_params=index_params)
        collection.create_index(field_name=MILVUS_VECTOR_FIELD, index_params=index_params, index_name=f"{MILVUS_VECTOR_FIELD}_hnsw_idx")
        
        scalar_fields_to_index = [
            MILVUS_COMPANY_ID_FIELD, MILVUS_DOCUMENT_ID_FIELD, MILVUS_CONTENT_HASH_FIELD
        ]
        for field_name in scalar_fields_to_index:
            create_log.info(f"Creating scalar index for {field_name} field (pipeline)...")
            collection.create_index(field_name=field_name, index_name=f"{field_name}_idx")

        create_log.info("All required indexes created successfully (pipeline).")
        return collection
    except MilvusException as e:
        create_log.error("Failed to create Milvus collection or index (pipeline)", error=str(e), exc_info=True)
        raise RuntimeError(f"Milvus collection/index creation failed (pipeline): {e}") from e


def _check_and_create_indexes_for_pipeline(collection: Collection):
    check_log = log.bind(collection_name=collection.name, component="MilvusPipelineOps")
    try:
        existing_indexes = collection.indexes
        existing_index_fields = {idx.field_name for idx in existing_indexes}
        required_indexes_map = {
            MILVUS_VECTOR_FIELD: (settings.MILVUS_INDEX_PARAMS, f"{MILVUS_VECTOR_FIELD}_hnsw_idx"),
            MILVUS_COMPANY_ID_FIELD: (None, f"{MILVUS_COMPANY_ID_FIELD}_idx"),
            MILVUS_DOCUMENT_ID_FIELD: (None, f"{MILVUS_DOCUMENT_ID_FIELD}_idx"),
            MILVUS_CONTENT_HASH_FIELD: (None, f"{MILVUS_CONTENT_HASH_FIELD}_idx"),
        }
        for field_name, (index_params, index_name) in required_indexes_map.items():
            if field_name not in existing_index_fields:
                check_log.warning(f"Index missing for field '{field_name}' (pipeline). Creating '{index_name}'...")
                collection.create_index(field_name=field_name, index_params=index_params, index_name=index_name)
                check_log.info(f"Index '{index_name}' created for field '{field_name}' (pipeline).")
            else:
                check_log.debug(f"Index already exists for field '{field_name}' (pipeline).")
    except MilvusException as e:
        check_log.error("Failed during index check/creation on existing collection (pipeline)", error=str(e))
        # Do not re-raise, allow processing to continue if some indexes exist.


def delete_milvus_chunks(company_id: str, document_id: str) -> int:
    del_log = log.bind(company_id=company_id, document_id=document_id, component="MilvusPipelineOps")
    expr = f'{MILVUS_COMPANY_ID_FIELD} == "{company_id}" and {MILVUS_DOCUMENT_ID_FIELD} == "{document_id}"'
    pks_to_delete: List[str] = []
    deleted_count = 0
    try:
        collection = _ensure_milvus_connection_and_collection_for_pipeline()
        del_log.info("Querying Milvus for PKs to delete (pipeline)...", filter_expr=expr)
        query_res = collection.query(expr=expr, output_fields=[MILVUS_PK_FIELD])
        pks_to_delete = [item[MILVUS_PK_FIELD] for item in query_res if MILVUS_PK_FIELD in item]
        if not pks_to_delete:
            del_log.info("No matching primary keys found in Milvus for deletion (pipeline).")
            return 0
        
        del_log.info(f"Found {len(pks_to_delete)} primary keys to delete (pipeline).")
        delete_expr = f'{MILVUS_PK_FIELD} in {json.dumps(pks_to_delete)}'
        del_log.info("Attempting to delete chunks from Milvus using PK list expression (pipeline).", filter_expr=delete_expr)
        delete_result = collection.delete(expr=delete_expr)
        deleted_count = delete_result.delete_count
        del_log.info("Milvus delete operation by PK list executed (pipeline).", deleted_count=deleted_count)
        
        if deleted_count != len(pks_to_delete):
             del_log.warning("Milvus delete count mismatch (pipeline).", expected=len(pks_to_delete), reported=deleted_count)
        return deleted_count
    except MilvusException as e:
        del_log.error("Milvus delete error (query or delete phase) (pipeline)", error=str(e), exc_info=True)
        return 0
    except Exception as e:
        del_log.exception("Unexpected error during Milvus chunk deletion (pipeline)")
        return 0


def index_chunks_in_milvus_and_prepare_for_pg(
    processed_chunks_from_docproc: List[Dict[str, Any]],
    embeddings: List[List[float]],
    filename: str,
    company_id_str: str,
    document_id_str: str,
    delete_existing_milvus_chunks: bool = True
) -> Tuple[int, List[str], List[Dict[str, Any]]]:
    """
    Takes chunks from docproc-service and embeddings, indexes them in Milvus,
    and prepares data for PostgreSQL insertion.
    """
    index_log = log.bind(
        company_id=company_id_str, document_id=document_id_str, filename=filename,
        num_input_chunks=len(processed_chunks_from_docproc), num_embeddings=len(embeddings),
        component="MilvusPGPipeline"
    )
    index_log.info("Starting Milvus indexing and PG data preparation")

    if len(processed_chunks_from_docproc) != len(embeddings):
        index_log.error("Mismatch between number of processed chunks and embeddings.",
                        chunks_count=len(processed_chunks_from_docproc), embeddings_count=len(embeddings))
        raise ValueError("Number of chunks and embeddings must match.")

    if not processed_chunks_from_docproc:
        index_log.warning("No chunks to process for Milvus/PG indexing.")
        return 0, [], []

    chunks_for_milvus_pg: List[DocumentChunkData] = []
    milvus_data_content: List[str] = []
    milvus_data_company_ids: List[str] = []
    milvus_data_document_ids: List[str] = []
    milvus_data_filenames: List[str] = []
    milvus_data_pages: List[int] = []
    milvus_data_titles: List[str] = []
    milvus_data_tokens: List[int] = []
    milvus_data_content_hashes: List[str] = []
    milvus_pk_ids: List[str] = []
    
    # Filter out empty texts from embeddings list to match content
    valid_embeddings: List[List[float]] = []
    valid_chunk_indices_from_docproc: List[int] = []


    for i, chunk_from_docproc in enumerate(processed_chunks_from_docproc):
        chunk_text = chunk_from_docproc.get('text', '')
        if not chunk_text or chunk_text.isspace():
            index_log.warning("Skipping empty chunk from docproc during Milvus/PG prep.", original_chunk_index=i)
            continue # Skip this chunk if it has no text
        
        # If chunk is valid, add its embedding and note its original index
        valid_embeddings.append(embeddings[i])
        valid_chunk_indices_from_docproc.append(i)

        source_metadata = chunk_from_docproc.get('source_metadata', {})
        
        tokens = len(tiktoken_enc.encode(chunk_text)) if tiktoken_enc else -1
        content_hash = hashlib.sha256(chunk_text.encode('utf-8', errors='ignore')).hexdigest()
        page_number_from_source = source_metadata.get('page_number')
        
        current_sequential_chunk_index = len(chunks_for_milvus_pg)
        title_for_chunk = f"{filename[:30]}... (Page {page_number_from_source or 'N/A'}, Chunk {current_sequential_chunk_index + 1})"

        pg_metadata = DocumentChunkMetadata(
            page=page_number_from_source,
            title=title_for_chunk[:500],
            tokens=tokens,
            content_hash=content_hash
        )

        chunk_data_obj = DocumentChunkData(
            document_id=uuid.UUID(document_id_str),
            company_id=uuid.UUID(company_id_str),
            chunk_index=current_sequential_chunk_index,
            content=chunk_text,
            metadata=pg_metadata
        )
        chunks_for_milvus_pg.append(chunk_data_obj)

        milvus_pk_id = f"{document_id_str}_{chunk_data_obj.chunk_index}"
        milvus_pk_ids.append(milvus_pk_id)
        
        milvus_data_content.append(chunk_text)
        milvus_data_company_ids.append(company_id_str)
        milvus_data_document_ids.append(document_id_str)
        milvus_data_filenames.append(filename)
        milvus_data_pages.append(page_number_from_source if page_number_from_source is not None else -1)
        milvus_data_titles.append(title_for_chunk[:512])
        milvus_data_tokens.append(tokens)
        milvus_data_content_hashes.append(content_hash)

    if not chunks_for_milvus_pg:
        index_log.warning("No valid (non-empty) chunks remained after initial processing for Milvus/PG.")
        return 0, [], []
    
    if len(valid_embeddings) != len(milvus_data_content):
        index_log.error("Critical mismatch: Valid embeddings count differs from valid content count for Milvus.",
                        num_valid_embeddings=len(valid_embeddings), num_valid_content=len(milvus_data_content))
        raise ValueError("Internal error: Embedding count does not match valid chunk content count.")

    max_content_len = settings.MILVUS_CONTENT_FIELD_MAX_LENGTH
    def truncate_utf8_bytes(s, max_bytes):
        b = s.encode('utf-8', errors='ignore')
        if len(b) <= max_bytes: return s
        return b[:max_bytes].decode('utf-8', errors='ignore')

    data_to_insert_milvus = [
        milvus_pk_ids,
        valid_embeddings, # Use the filtered list of embeddings
        [truncate_utf8_bytes(text, max_content_len) for text in milvus_data_content],
        milvus_data_company_ids,
        milvus_data_document_ids,
        milvus_data_filenames,
        milvus_data_pages,
        milvus_data_titles,
        milvus_data_tokens,
        milvus_data_content_hashes
    ]
    index_log.debug(f"Prepared {len(milvus_pk_ids)} valid entities for Milvus insertion.")

    if delete_existing_milvus_chunks:
        index_log.info("Attempting to delete existing Milvus chunks before insertion...")
        try:
            deleted_count = delete_milvus_chunks(company_id_str, document_id_str)
            index_log.info(f"Deleted {deleted_count} existing Milvus chunks.")
        except Exception as del_err:
            index_log.error("Failed to delete existing Milvus chunks, proceeding with insert anyway.", error=str(del_err))

    index_log.debug(f"Inserting {len(milvus_pk_ids)} chunks into Milvus collection '{MILVUS_COLLECTION_NAME}'...")
    inserted_milvus_count = 0
    returned_milvus_pks: List[str] = []
    
    try:
        collection = _ensure_milvus_connection_and_collection_for_pipeline()
        mutation_result = collection.insert(data_to_insert_milvus)
        inserted_milvus_count = mutation_result.insert_count

        if inserted_milvus_count != len(milvus_pk_ids):
             index_log.error("Milvus insert count mismatch!", expected=len(milvus_pk_ids), inserted=inserted_milvus_count, errors=mutation_result.err_indices)
        else:
             index_log.info(f"Successfully inserted {inserted_milvus_count} chunks into Milvus.")

        returned_milvus_pks = [str(pk) for pk in mutation_result.primary_keys]
        chunks_for_pg_prepared: List[Dict[str, Any]] = []

        if len(returned_milvus_pks) != inserted_milvus_count:
             index_log.error("Milvus returned PK count mismatch!", returned_count=len(returned_milvus_pks), inserted_count=inserted_milvus_count)
             # Do not proceed with PG prep if PKs are unreliable
        else:
            index_log.info("Milvus returned PKs match inserted count.")
            for i in range(inserted_milvus_count):
                chunks_for_milvus_pg[i].embedding_id = returned_milvus_pks[i]
                chunks_for_milvus_pg[i].vector_status = ChunkVectorStatus.CREATED
                chunks_for_pg_prepared.append(chunks_for_milvus_pg[i].model_dump(mode='json'))

        index_log.debug("Flushing Milvus collection...")
        collection.flush()
        index_log.info("Milvus collection flushed.")

        return inserted_milvus_count, returned_milvus_pks, chunks_for_pg_prepared

    except MilvusException as e:
        index_log.error("Failed to insert data into Milvus", error=str(e), exc_info=True)
        raise RuntimeError(f"Milvus insertion failed: {e}") from e
    except Exception as e:
        index_log.exception("Unexpected error during Milvus insertion")
        raise RuntimeError(f"Unexpected Milvus insertion error: {e}") from e
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
import httpx
from celery import Task, states
from celery.exceptions import Ignore, Reject, MaxRetriesExceededError, Retry
from celery.signals import worker_process_init
from sqlalchemy import Engine

from app.services.clients.embedding_service_client import EmbeddingServiceClient, EmbeddingServiceClientError
from app.services.clients.docproc_service_client import DocProcServiceClient, DocProcServiceClientError

from app.core.config import settings
from app.db.postgres_client import get_sync_engine, set_status_sync, bulk_insert_chunks_sync
from app.models.domain import DocumentStatus
from app.services.gcs_client import GCSClient, GCSClientError
from app.services.ingest_pipeline import (
    index_chunks_in_milvus_and_prepare_for_pg,
    delete_milvus_chunks
)
from app.tasks.celery_app import celery_app
import asyncio

task_struct_log = structlog.get_logger(__name__)
IS_WORKER = "worker" in sys.argv

# --- Global Resources for Worker Process ---
sync_engine: Optional[Engine] = None
gcs_client_global: Optional[GCSClient] = None # Renamed to avoid conflict
embedding_service_client_global: Optional[EmbeddingServiceClient] = None
docproc_service_client_global: Optional[DocProcServiceClient] = None


@worker_process_init.connect(weak=False)
def init_worker_resources(**kwargs):
    global sync_engine, gcs_client_global, embedding_service_client_global, docproc_service_client_global
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

        if embedding_service_client_global is None:
            log.info("Initializing Embedding Service client for worker...")
            embedding_service_client_global = EmbeddingServiceClient()
            log.info("Embedding Service client initialized for worker.")
        else:
            log.info("Embedding Service client already initialized for worker.")
        
        if docproc_service_client_global is None:
            log.info("Initializing Document Processing Service client for worker...")
            docproc_service_client_global = DocProcServiceClient()
            log.info("Document Processing Service client initialized for worker.")
        else:
            log.info("Document Processing Service client already initialized for worker.")

    except Exception as e:
        log.critical("CRITICAL FAILURE during worker resource initialization!", error=str(e), exc_info=True)
        sync_engine = None
        gcs_client_global = None
        embedding_service_client_global = None
        docproc_service_client_global = None


def run_async_from_sync(awaitable):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running(): # Check if a loop is already running in this thread
             # If so, create a new loop for this task execution to avoid interference
             # This is a common pattern for sync Celery workers calling async code
             temp_loop = asyncio.new_event_loop()
             asyncio.set_event_loop(temp_loop)
             result = temp_loop.run_until_complete(awaitable)
             temp_loop.close() # Close the temporary loop
             asyncio.set_event_loop(loop) # Restore original loop if it was running
             return result
    except RuntimeError: # No current event loop in thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(awaitable)
        finally:
            loop.close() # Ensure the new loop is closed
            # No need to restore if there wasn't one before


@celery_app.task(
    bind=True,
    name="ingest.process_document",
    autoretry_for=(
        EmbeddingServiceClientError, 
        DocProcServiceClientError,
        httpx.RequestError, 
        httpx.HTTPStatusError,
        GCSClientError,
        ConnectionRefusedError, # For network issues to dependent services
        asyncio.TimeoutError, # For timeouts in async calls
        Exception
    ),
    exclude=(
        Reject, Ignore, ValueError, ConnectionError, RuntimeError, TypeError,
        # Errors from client-side validation (4xx from services) should not be retried by Celery default
        # but EmbeddingServiceClientError/DocProcServiceClientError might wrap them.
        # Custom logic handles retry for 5xx within the task.
    ),
    retry_backoff=True,
    retry_backoff_max=600,
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

    global_resources = {
        "Sync DB Engine": sync_engine, "GCS Client": gcs_client_global,
        "Embedding Service Client": embedding_service_client_global,
        "DocProc Service Client": docproc_service_client_global
    }
    for name, resource in global_resources.items():
        if not resource:
            log.critical(f"Worker resource '{name}' is not initialized. Task cannot proceed.")
            error_msg = f"Worker resource '{name}' initialization failed."
            # Attempt to update DB status only if engine is available
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

        # 1. Descargar archivo de GCS
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = pathlib.Path(temp_dir)
            temp_file_path_obj = temp_dir_path / normalized_filename
            log.info(f"Downloading GCS object: {object_name} -> {str(temp_file_path_obj)}")
            
            max_gcs_retries = 3; gcs_delay = 2; download_success = False
            for attempt_num in range(1, max_gcs_retries + 1):
                try:
                    gcs_client_global.download_file_sync(object_name, str(temp_file_path_obj))
                    log.info("File downloaded successfully from GCS.")
                    file_bytes = temp_file_path_obj.read_bytes()
                    log.info(f"File content read into memory ({len(file_bytes)} bytes).")
                    download_success = True; break
                except GCSClientError as gce_dl:
                    is_not_found = "Object not found" in str(gce_dl) or \
                                   (hasattr(gce_dl, 'original_exception') and \
                                    isinstance(gce_dl.original_exception, FileNotFoundError))
                    if is_not_found:
                        log.warning(f"GCS object not found on download attempt {attempt_num}/{max_gcs_retries}. Retrying in {gcs_delay}s...", error_short=str(gce_dl).splitlines()[0])
                        if attempt_num == max_gcs_retries: log.error(f"File still not found in GCS after {max_gcs_retries} attempts. Aborting."); raise
                        time.sleep(gcs_delay); gcs_delay *= 2
                    else: log.error("Non-retriable GCS error during download", error=str(gce_dl)); raise
                except Exception as read_err:
                    log.error("Failed to read downloaded file into bytes", error=str(read_err), exc_info=True)
                    raise RuntimeError(f"Failed to read temp file: {read_err}") from read_err
            if not download_success or file_bytes is None:
                 raise RuntimeError("File download or read failed, bytes not available.")

        # 2. Llamar al DocProc Service
        log.info("Calling Document Processing Service...")
        try:
            docproc_response = run_async_from_sync(
                docproc_service_client_global.process_document(
                    file_bytes=file_bytes,
                    original_filename=normalized_filename,
                    content_type=content_type,
                    document_id=document_id_str,
                    company_id=company_id_str
                )
            )
            processed_chunks_from_docproc = docproc_response.get("data", {}).get("chunks", [])
            log.info(f"Received {len(processed_chunks_from_docproc)} chunks from DocProc Service.")
        except DocProcServiceClientError as dpce:
            log.error("DocProc Service Client Error", error_msg=str(dpce), status_code=dpce.status_code, details_preview=str(dpce.detail)[:200], exc_info=True)
            error_msg_dpce = f"DocProc Error: {str(dpce.detail or dpce.message)[:350]}"
            set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_dpce)
            if dpce.status_code and 500 <= dpce.status_code < 600: # Server-side errors on docproc
                raise self.retry(exc=dpce, countdown=int(self.default_retry_delay * (self.request.retries + 1)))
            else: # Client-side errors (4xx) or connection issues
                raise Reject(f"DocProc Service critical error: {error_msg_dpce}", requeue=False) from dpce
        
        if not processed_chunks_from_docproc:
            log.warning("DocProc Service returned no chunks. Document processing considered complete with 0 chunks.")
            set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSED, chunk_count=0, error_message=None)
            return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": 0, "document_id": document_id_str}

        # 3. Preparar textos para Embedding Service
        chunk_texts_for_embedding = [chunk['text'] for chunk in processed_chunks_from_docproc if chunk.get('text','').strip()]
        if not chunk_texts_for_embedding:
            log.warning("No non-empty text found in chunks received from DocProc. Finishing with 0 chunks.")
            set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSED, chunk_count=0, error_message=None)
            return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": 0, "document_id": document_id_str}

        # 4. Llamar al Embedding Service
        log.info(f"Calling Embedding Service for {len(chunk_texts_for_embedding)} texts...")
        embeddings: List[List[float]] = []
        try:
            embeddings, model_info = run_async_from_sync(
                embedding_service_client_global.get_embeddings(chunk_texts_for_embedding)
            )
            log.info(f"Embeddings received from service for {len(embeddings)} chunks. Model: {model_info}")
            if len(embeddings) != len(chunk_texts_for_embedding):
                log.error("Embedding count mismatch from Embedding Service.", expected=len(chunk_texts_for_embedding), received=len(embeddings))
                raise RuntimeError(f"Embedding count mismatch. Expected {len(chunk_texts_for_embedding)}, got {len(embeddings)}.")
            if embeddings and len(embeddings[0]) != settings.EMBEDDING_DIMENSION:
                 log.error(f"Received embedding dimension ({len(embeddings[0])}) from service does not match configured Milvus dimension ({settings.EMBEDDING_DIMENSION}).")
                 raise RuntimeError(f"Embedding dimension mismatch. Expected {settings.EMBEDDING_DIMENSION}, got {len(embeddings[0])}")
        except EmbeddingServiceClientError as esce:
            log.error("Embedding Service Client Error", error_msg=str(esce), status_code=esce.status_code, details_preview=str(esce.detail)[:200], exc_info=True)
            error_msg_esc = f"Embedding Service Error: {str(esce.detail or esce.message)[:350]}"
            set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_esc)
            if esce.status_code and 500 <= esce.status_code < 600: # Server-side errors on embedding service
                raise self.retry(exc=esce, countdown=int(self.default_retry_delay * (self.request.retries + 1)))
            else: # Client-side errors (4xx) or connection issues
                raise Reject(f"Embedding Service critical error: {error_msg_esc}", requeue=False) from esce

        # 5. Indexar en Milvus y preparar para PostgreSQL
        log.info("Preparing chunks for Milvus and PostgreSQL indexing...")
        inserted_milvus_count, milvus_pks, chunks_for_pg_insert = index_chunks_in_milvus_and_prepare_for_pg(
            processed_chunks_from_docproc=[chunk for chunk in processed_chunks_from_docproc if chunk.get('text','').strip()], # Pass only non-empty chunks
            embeddings=embeddings, # Embeddings match non-empty chunks
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

        # 6. Indexar en PostgreSQL
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

        # 7. Actualización de Estado Final
        log.debug("Setting final status to PROCESSED in DB.")
        set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSED, chunk_count=inserted_pg_count, error_message=None)
        log.info(f"Document processing finished successfully. Final chunk count (PG): {inserted_pg_count}")
        return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": inserted_pg_count, "document_id": document_id_str}

    except GCSClientError as gce:
        log.error(f"GCS Error during processing", error_msg=str(gce), exc_info=True)
        error_msg_gcs = f"GCS Error: {str(gce)[:400]}"
        set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_gcs)
        if "Object not found" in str(gce): # Non-retriable if object not found after initial upload
            raise Reject(f"GCS Error: Object not found: {object_name}", requeue=False) from gce
        # For other GCS errors, let Celery's autoretry_for handle it.
        raise # Re-raise for Celery to catch for retry

    except (ValueError, RuntimeError, TypeError) as non_retriable_err:
         log.error(f"Non-retriable Error: {non_retriable_err}", exc_info=True)
         error_msg_pipe = f"Pipeline/Data Error: {type(non_retriable_err).__name__} - {str(non_retriable_err)[:400]}"
         set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_pipe)
         raise Reject(f"Pipeline failed: {error_msg_pipe}", requeue=False) from non_retriable_err

    except Reject as r:
         log.error(f"Task rejected permanently: {r.reason}")
         if sync_engine and doc_uuid and r.reason: # Ensure sync_engine and doc_uuid are valid
             set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=f"Rejected: {str(r.reason)[:500]}")
         raise 
    except Ignore:
         log.info("Task ignored.")
         raise 
    except Retry as retry_exc: # Specific catch for Celery's Retry exception
        log.warning(f"Task is being retried by Celery. Reason: {retry_exc}", exc_info=False)
        # Status is already PROCESSING, no need to update for retry.
        raise # Re-raise for Celery to handle the retry
    except MaxRetriesExceededError as mree:
        log.error("Max retries exceeded for task.", exc_info=True, cause_type=type(mree.cause).__name__, cause_message=str(mree.cause))
        final_error = mree.cause if mree.cause else mree
        error_msg_mree = f"Max retries exceeded ({max_attempts}). Last error: {type(final_error).__name__} - {str(final_error)[:300]}"
        set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_mree)
        self.update_state(state=states.FAILURE, meta={'exc_type': type(final_error).__name__, 'exc_message': str(final_error)})
        # Do not re-raise MaxRetriesExceededError, Celery handles it.
    except Exception as exc: 
        log.exception(f"An unexpected error occurred, attempting Celery retry if possible.")
        error_msg_exc = f"Attempt {attempt} failed: {type(exc).__name__} - {str(exc)[:400]}"
        # Set status to PROCESSING with error for visibility during retries.
        set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSING, error_message=error_msg_exc)
        # Let Celery's autoretry_for handle it by re-raising the original exception.
        raise
```

## File: `pyproject.toml`
```toml
[tool.poetry]
name = "ingest-service"
version = "0.3.2"
description = "Ingest service for Atenex B2B SaaS (Postgres/GCS/Milvus/Remote Embedding & DocProc Services - CPU - Prefork)"
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
asyncpg = "^0.29.0"
tenacity = "^8.2.3"
python-multipart = "^0.0.9"
structlog = "^24.1.0"

google-cloud-storage = "^2.16.0"

# --- Core Processing Dependencies (v0.3.2) ---
pymilvus = ">=2.4.1,<2.5.0"
tiktoken = "^0.7.0"


# --- HTTP Client (API & Service Client - Keep) ---
httpx = {extras = ["http2"], version = "^0.27.0"}
h2 = "^4.1.0"

# --- Synchronous DB Dependencies (Worker - Keep) ---
sqlalchemy = "^2.0.28"
psycopg2-binary = "^2.9.9"


[tool.poetry.group.dev.dependencies]
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
