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
│   ├── embedder.py
│   ├── extractors
│   │   ├── __init__.py
│   │   ├── docx_extractor.py
│   │   ├── html_extractor.py
│   │   ├── md_extractor.py
│   │   ├── pdf_extractor.py
│   │   └── txt_extractor.py
│   ├── gcs_client.py
│   ├── ingest_pipeline.py
│   └── text_splitter.py
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
from app.services.gcs_client import GCSClient, GCSError
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


# GCS Client Dependency
def get_gcs_client():
    """Dependency to get GCS client instance."""
    try:
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

def _get_milvus_collection_sync() -> Collection:
    """Synchronously connects to Milvus and returns the Collection object."""
    alias = "api_sync_helper_zilliz"
    sync_milvus_log = log.bind(component="MilvusHelperSync", alias=alias)

    # Check if connection exists for this alias
    if alias not in connections.list_connections():
        sync_milvus_log.info("Connecting to Zilliz Cloud for sync helper...")
        try:
            connections.connect(
                alias=alias,
                uri=settings.ZILLIZ_URI,
                token=settings.ZILLIZ_API_KEY.get_secret_value(),
                secure=True
            )
            sync_milvus_log.info("Zilliz Cloud connection established for sync helper.")
        except MilvusException as e:
            sync_milvus_log.error("Failed to connect to Zilliz Cloud for sync helper", error=str(e))
            raise RuntimeError(f"Zilliz connection failed for API helper: {e}") from e
    else:
        sync_milvus_log.debug("Reusing existing Zilliz connection for sync helper.")

    # Get the collection object
    try:
        collection = Collection(name=MILVUS_COLLECTION_NAME, using=alias)
        # Always load collection into memory (necessary for sync queries)
        sync_milvus_log.info("Loading Zilliz collection for sync helper...")
        collection.load()
        sync_milvus_log.info("Zilliz collection loaded for sync helper.")
        return collection
    except MilvusException as e:
        sync_milvus_log.error("Failed to get or load Zilliz collection for sync helper", error=str(e))
        raise RuntimeError(f"Zilliz collection access error for API helper: {e}") from e


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
        count_log.info("Zilliz chunk count successful (pymilvus)", count=count)
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
        delete_log.info("Zilliz delete operation executed (pymilvus)", deleted_count=delete_result.delete_count)
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
    gcs_client: GCSClient = Depends(get_gcs_client),
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

    # Normalizar el nombre del archivo
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

    try:
        file_content = await file.read()
        endpoint_log.info("[INGEST] Preparando subida a GCS", object_name=file_path_in_storage, filename=normalized_filename, size=len(file_content), content_type=file.content_type)
        try:
            await gcs_client.upload_file_async(
                object_name=file_path_in_storage, data=file_content, content_type=file.content_type
            )
            endpoint_log.info("[INGEST] File uploaded successfully to GCS", object_name=file_path_in_storage)
        except Exception as upload_exc:
            endpoint_log.error("[INGEST] Exception during GCS upload", object_name=file_path_in_storage, error=str(upload_exc))
            raise

        # --- Validación extra: verificar que el archivo existe en GCS ---
        try:
            file_exists = await gcs_client.check_file_exists_async(file_path_in_storage)
            endpoint_log.info("[INGEST] GCS existence check after upload", object_name=file_path_in_storage, exists=file_exists)
        except Exception as check_exc:
            endpoint_log.error("[INGEST] Exception during GCS existence check", object_name=file_path_in_storage, error=str(check_exc))
            file_exists = False

        if not file_exists:
            endpoint_log.error("[INGEST] File not found in MinIO after upload", object_name=file_path_in_storage, filename=normalized_filename)
            async with get_db_conn() as conn:
                await api_db_retry_strategy(db_client.update_document_status)(
                    document_id=document_id, status=DocumentStatus.ERROR, error_message="File not found in MinIO after upload", conn=conn
                )
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="File not found in MinIO after upload.")

        async with get_db_conn() as conn:
            await api_db_retry_strategy(db_client.update_document_status)(
                document_id=document_id, status=DocumentStatus.UPLOADED, conn=conn # Use keyword arg for conn
            )
        endpoint_log.info("[INGEST] Document status updated to 'uploaded'", document_id=str(document_id))
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
            "filename": normalized_filename, "content_type": file.content_type
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


    from datetime import datetime, timedelta, timezone
    needs_update = False
    current_status_enum = DocumentStatus(doc_data['status'])
    current_chunk_count = doc_data.get('chunk_count')
    current_error_message = doc_data.get('error_message')
    updated_status_enum = current_status_enum
    updated_chunk_count = current_chunk_count
    updated_error_message = current_error_message

    # Parse last update time for grace period logic
    updated_at = doc_data.get('updated_at')
    now_utc = datetime.now(timezone.utc)
    updated_at_dt = None
    if updated_at:
        if isinstance(updated_at, datetime):
            updated_at_dt = updated_at.astimezone(timezone.utc)
        else:
            try:
                updated_at_dt = datetime.fromisoformat(updated_at.replace('Z', '+00:00'))
            except Exception:
                updated_at_dt = None

    GRACE_PERIOD_SECONDS = 120  # 2 minutos

    minio_path = doc_data.get('file_path')
    minio_exists = False
    if not minio_path:
        status_log.warning("MinIO file path missing in DB record", db_id=doc_data['id'])
        if updated_status_enum not in [DocumentStatus.ERROR, DocumentStatus.PENDING]:
            # Solo permitir cambio a error si no está en processed recientemente
            if not (updated_status_enum == DocumentStatus.PROCESSED and updated_at_dt and (now_utc - updated_at_dt).total_seconds() < GRACE_PERIOD_SECONDS):
                needs_update = True
                updated_status_enum = DocumentStatus.ERROR
                updated_error_message = (updated_error_message or "") + " File path missing."
            else:
                status_log.warning("Grace period: no se cambia a ERROR por falta de file_path, status=processed reciente")
    else:
        status_log.debug("Checking MinIO for file existence", object_name=minio_path)
        try:
            minio_exists = await minio_client.check_file_exists_async(minio_path)
            status_log.info("MinIO existence check complete", exists=minio_exists)
            if not minio_exists and updated_status_enum not in [DocumentStatus.ERROR, DocumentStatus.PENDING]:
                status_log.warning("File missing in MinIO but DB status suggests otherwise.", current_db_status=updated_status_enum.value)
                if updated_status_enum != DocumentStatus.ERROR:
                    # Solo permitir cambio a error si no está en processed recientemente
                    if not (updated_status_enum == DocumentStatus.PROCESSED and updated_at_dt and (now_utc - updated_at_dt).total_seconds() < GRACE_PERIOD_SECONDS):
                        needs_update = True
                        updated_status_enum = DocumentStatus.ERROR
                        updated_error_message = "File missing from storage."
                        updated_chunk_count = 0
                    else:
                        status_log.warning("Grace period: no se cambia a ERROR por falta de archivo en MinIO, status=processed reciente")
        except Exception as minio_e:
            status_log.error("MinIO check failed", error=str(minio_e))
            minio_exists = False
            if updated_status_enum != DocumentStatus.ERROR:
                # Solo permitir cambio a error si no está en processed recientemente
                if not (updated_status_enum == DocumentStatus.PROCESSED and updated_at_dt and (now_utc - updated_at_dt).total_seconds() < GRACE_PERIOD_SECONDS):
                    needs_update = True
                    updated_status_enum = DocumentStatus.ERROR
                    updated_error_message = (updated_error_message or "") + f" MinIO check error ({type(minio_e).__name__})."
                    updated_chunk_count = 0
                else:
                    status_log.warning("Grace period: no se cambia a ERROR por excepción en MinIO, status=processed reciente")


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
                # Solo permitir cambio a error si no está en processed recientemente
                if not (updated_status_enum == DocumentStatus.PROCESSED and updated_at_dt and (now_utc - updated_at_dt).total_seconds() < GRACE_PERIOD_SECONDS):
                    needs_update = True
                    updated_status_enum = DocumentStatus.ERROR
                    updated_error_message = (updated_error_message or "") + " Failed Milvus count check."
                else:
                    status_log.warning("Grace period: no se cambia a ERROR por fallo en Milvus, status=processed reciente")
        elif milvus_chunk_count > 0:
            # Si hay chunks y el archivo existe, y el estado es error o uploaded, corregir a PROCESSED
            if minio_exists and updated_status_enum in [DocumentStatus.ERROR, DocumentStatus.UPLOADED]:
                status_log.warning("Inconsistency: Chunks found and file exists but DB status is not 'processed'. Corrigiendo.")
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
                # Solo permitir cambio a error si no está en processed recientemente
                if not (updated_status_enum == DocumentStatus.PROCESSED and updated_at_dt and (now_utc - updated_at_dt).total_seconds() < GRACE_PERIOD_SECONDS):
                    needs_update = True
                    updated_status_enum = DocumentStatus.ERROR
                    updated_chunk_count = 0
                    updated_error_message = (updated_error_message or "") + " Processed data missing."
                else:
                    status_log.warning("Grace period: no se cambia a ERROR por chunks=0, status=processed reciente")
            elif updated_status_enum == DocumentStatus.ERROR and updated_chunk_count != 0:
                needs_update = True
                updated_chunk_count = 0

    except Exception as e:
        status_log.exception("Unexpected error during Milvus count check execution", error=str(e))
        milvus_chunk_count = -1
        if updated_status_enum != DocumentStatus.ERROR:
            # Solo permitir cambio a error si no está en processed recientemente
            if not (updated_status_enum == DocumentStatus.PROCESSED and updated_at_dt and (now_utc - updated_at_dt).total_seconds() < GRACE_PERIOD_SECONDS):
                needs_update = True
                updated_status_enum = DocumentStatus.ERROR
                updated_error_message = (updated_error_message or "") + f" Error checking Milvus ({type(e).__name__})."
            else:
                status_log.warning("Grace period: no se cambia a ERROR por excepción en Milvus, status=processed reciente")

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
                        doc_needs_update = True
                        doc_updated_status_enum = DocumentStatus.ERROR
                        doc_updated_error_msg = "File missing from storage."
                        doc_updated_chunk_count = 0
            except Exception as e:
                check_log.error("MinIO check failed for list item", error=str(e))
                live_minio_exists = False
                if doc_updated_status_enum != DocumentStatus.ERROR:
                    doc_needs_update = True
                    doc_updated_status_enum = DocumentStatus.ERROR
                    doc_updated_error_msg = (doc_updated_error_msg or "") + f" MinIO check error."
                    doc_updated_chunk_count = 0
        else:
            live_minio_exists = False

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
                # Si hay chunks y el archivo existe, y el estado es error o uploaded, corregir a PROCESSED
                if live_minio_exists and doc_updated_status_enum in [DocumentStatus.ERROR, DocumentStatus.UPLOADED]:
                    check_log.warning("Inconsistency: Chunks found and file exists but DB status is not 'processed'. Corrigiendo.")
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
            "live_minio_exists": live_minio_exists,
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
    return None # Return 204 No Conten
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
POSTGRES_K8S_SVC = "postgresql-service.nyro-develop.svc.cluster.local"
MINIO_K8S_SVC = "minio-service.nyro-develop.svc.cluster.local"
MILVUS_K8S_SVC = "milvus-standalone.nyro-develop.svc.cluster.local"
REDIS_K8S_SVC = "redis-service-master.nyro-develop.svc.cluster.local"

# --- Defaults ---
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex"
POSTGRES_K8S_USER_DEFAULT = "postgres"
MINIO_K8S_PORT_DEFAULT = 9000
MINIO_BUCKET_DEFAULT = "ingested-documents"
MILVUS_K8S_PORT_DEFAULT = 19530
MILVUS_DEFAULT_COLLECTION = "document_chunks_minilm" # Cambiado nombre default
MILVUS_DEFAULT_INDEX_PARAMS = '{"metric_type": "IP", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 256}}'
MILVUS_DEFAULT_SEARCH_PARAMS = '{"metric_type": "IP", "params": {"ef": 128}}'
DEFAULT_EMBEDDING_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_EMBEDDING_DIM = 384 # <- Actualizado para MiniLM

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


    # --- Database (Cloud SQL) ---
    POSTGRES_USER: str = POSTGRES_K8S_USER_DEFAULT
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_DB: str = POSTGRES_K8S_DB_DEFAULT
    POSTGRES_INSTANCE_CONNECTION_NAME: str = "praxis-study-458413-f4:southamerica-west1:atenex-db"
    POSTGRES_IP_TYPE: str = "PRIVATE"  # Opcional, default PRIVATE

    # --- GCS ---
    GCS_BUCKET_NAME: str = "atenex"

    # --- Zilliz Cloud (Vector DB) ---
    ZILLIZ_URI: str = "https://in03-0afab716eb46d7f.serverless.gcp-us-west1.cloud.zilliz.com"
    ZILLIZ_API_KEY: SecretStr
    MILVUS_COLLECTION_NAME: str = "atenex-vector-db"
    MILVUS_GRPC_TIMEOUT: int = 10
    MILVUS_METADATA_FIELDS: List[str] = Field(default=["company_id", "document_id", "file_name"])
    MILVUS_CONTENT_FIELD: str = "content"
    MILVUS_EMBEDDING_FIELD: str = "embedding"
    MILVUS_CONTENT_FIELD_MAX_LENGTH: int = 20000
    MILVUS_INDEX_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_INDEX_PARAMS))
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_SEARCH_PARAMS))

    # --- Embeddings (ACTUALIZADO) ---
    EMBEDDING_MODEL_ID: str = Field(default=DEFAULT_EMBEDDING_MODEL_ID)
    EMBEDDING_DIMENSION: int = Field(default=DEFAULT_EMBEDDING_DIM)
    # OPENAI_API_KEY: Optional[SecretStr] = None # Opcional, no usado en ingesta

    # --- ELIMINADO ---
    # FASTEMBED_MODEL: str = ...
    # USE_GPU: bool = False

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
    SPLITTER_CHUNK_SIZE: int = Field(default=1000)
    SPLITTER_CHUNK_OVERLAP: int = Field(default=200)
    # SPLITTER_SPLIT_BY: str = "word" # No relevante para el splitter actual

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
        model_id = info.data.get('EMBEDDING_MODEL_ID', DEFAULT_EMBEDDING_MODEL_ID)
        if 'all-MiniLM-L6-v2' in model_id and v != 384:
             logging.warning(f"Configured EMBEDDING_DIMENSION ({v}) differs from standard MiniLM dimension (384).")
        elif 'bge-large' in model_id and v != 1024:
             logging.warning(f"Configured EMBEDDING_DIMENSION ({v}) differs from standard BGE-Large dimension (1024).")
        logging.debug(f"Using EMBEDDING_DIMENSION: {v}")
        return v

    @field_validator('POSTGRES_PASSWORD', 'ZILLIZ_API_KEY', mode='before')
    @classmethod
    def check_required_secret_value_present(cls, v: Any, info: ValidationInfo) -> Any:
        if v is None or v == "":
            field_name = info.field_name if info.field_name else "Unknown Secret Field"
            raise ValueError(f"Required secret field '{field_name}' cannot be empty.")
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
    temp_log.info(f"  POSTGRES_INSTANCE_CONNECTION_NAME: {settings.POSTGRES_INSTANCE_CONNECTION_NAME}")
    temp_log.info(f"  POSTGRES_DB:              {settings.POSTGRES_DB}")
    temp_log.info(f"  POSTGRES_USER:            {settings.POSTGRES_USER}")
    temp_log.info(f"  POSTGRES_PASSWORD:        {'*** SET ***' if settings.POSTGRES_PASSWORD else '!!! NOT SET !!!'}")
    temp_log.info(f"  POSTGRES_IP_TYPE:         {settings.POSTGRES_IP_TYPE}")
    temp_log.info(f"  GCS_BUCKET_NAME:          {settings.GCS_BUCKET_NAME}")
    temp_log.info(f"  ZILLIZ_URI:               {settings.ZILLIZ_URI}")
    temp_log.info(f"  ZILLIZ_API_KEY:           {'*** SET ***' if settings.ZILLIZ_API_KEY else '!!! NOT SET !!!'}")
    temp_log.info(f"  MILVUS_COLLECTION_NAME:   {settings.MILVUS_COLLECTION_NAME}")
    temp_log.info(f"  MILVUS_GRPC_TIMEOUT:      {settings.MILVUS_GRPC_TIMEOUT}s")
    temp_log.info(f"  EMBEDDING_MODEL_ID:       {settings.EMBEDDING_MODEL_ID}")
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
from google.cloud.sql.connector import Connector, IPTypes
from sqlalchemy.pool import NullPool
_connector: Optional[Connector] = None
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
    global _pool, _connector
    if _pool is None or _pool._closed:
        log.info("Creating PostgreSQL connection pool using Cloud SQL Python Connector...", instance=settings.POSTGRES_INSTANCE_CONNECTION_NAME)
        if _connector is None:
            _connector = Connector()
        def getconn():
            return _connector.connect(
                settings.POSTGRES_INSTANCE_CONNECTION_NAME,
                "asyncpg",
                user=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD.get_secret_value(),
                db=settings.POSTGRES_DB,
                ip_type=IPTypes.PRIVATE if settings.POSTGRES_IP_TYPE.upper() == "PRIVATE" else IPTypes.PUBLIC
            )
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
            min_size=2,
            max_size=10,
            timeout=30.0,
            command_timeout=60.0,
            init=init_connection,
            statement_cache_size=0,
            connection_class=None,
            connection_factory=getconn
        )
        log.info("PostgreSQL async connection pool created successfully (Cloud SQL Connector).")
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
    global _sync_engine, _connector
    sync_log = log.bind(component="SyncEngine")
    if _connector is None:
        _connector = Connector()
    def getconn():
        return _connector.connect(
            settings.POSTGRES_INSTANCE_CONNECTION_NAME,
            "pg8000",
            user=settings.POSTGRES_USER,
            password=settings.POSTGRES_PASSWORD.get_secret_value(),
            db=settings.POSTGRES_DB,
            ip_type=IPTypes.PRIVATE if settings.POSTGRES_IP_TYPE.upper() == "PRIVATE" else IPTypes.PUBLIC
        )
    if _sync_engine is None:
        sync_log.info("Creating SQLAlchemy synchronous engine using Cloud SQL Connector...")
        try:
            _sync_engine = create_engine(
                "postgresql+pg8000://",
                creator=getconn,
                poolclass=NullPool
            )
            with _sync_engine.connect() as conn_test:
                conn_test.execute(text("SELECT 1"))
            sync_log.info("SQLAlchemy synchronous engine created and tested successfully (Cloud SQL Connector).")
        except ImportError as ie:
            sync_log.critical("SQLAlchemy or pg8000 not installed! Cannot create sync engine.", error=str(ie))
            raise RuntimeError("Missing SQLAlchemy/pg8000 dependency") from ie
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
    global _sync_engine, _connector
    sync_log = log.bind(component="SyncEngine")
    if _sync_engine:
        sync_log.info("Disposing SQLAlchemy synchronous engine pool...")
        _sync_engine.dispose()
        _sync_engine = None
        sync_log.info("SQLAlchemy synchronous engine pool disposed.")
    if _connector:
        _connector.close()
        _connector = None
        sync_log.info("Cloud SQL connector closed.")
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

## File: `app\services\embedder.py`
```py
from functools import lru_cache
from typing import List, Optional
from sentence_transformers import SentenceTransformer
import structlog
import numpy as np
from app.core.config import settings

log = structlog.get_logger(__name__)

MODEL_ID = getattr(settings, 'EMBEDDING_MODEL_ID', 'all-MiniLM-L6-v2')
EXPECTED_DIM = getattr(settings, 'EMBEDDING_DIMENSION', 384)

_model_instance: Optional[SentenceTransformer] = None

def get_embedding_model() -> SentenceTransformer:
    global _model_instance
    if _model_instance is None:
        log.info("Loading SentenceTransformer model", model_id=MODEL_ID)
        _model_instance = SentenceTransformer(MODEL_ID)
        # Validar dimensión
        test_vec = _model_instance.encode(["test"])
        if test_vec.shape[1] != EXPECTED_DIM:
            raise ValueError(f"Embedding dimension mismatch: expected {EXPECTED_DIM}, got {test_vec.shape[1]}")
        log.info("Model loaded and validated", dimension=test_vec.shape[1])
    return _model_instance

def embed_chunks(chunks: List[str]) -> List[List[float]]:
    model = get_embedding_model()
    log.debug("Generating embeddings for chunks", num_chunks=len(chunks))
    embeddings = model.encode(chunks, show_progress_bar=False)
    return embeddings.tolist()

```

## File: `app\services\extractors\__init__.py`
```py

```

## File: `app\services\extractors\docx_extractor.py`
```py
import io
import docx  # python-docx
import structlog

log = structlog.get_logger(__name__)

class DocxExtractionError(Exception):
    pass

def extract_text_from_docx(file_bytes: bytes, filename: str = "unknown.docx") -> str:
    """Extrae texto de los bytes de un DOCX preservando saltos de párrafo."""
    log.debug("Extracting text from DOCX bytes", filename=filename)
    try:
        doc = docx.Document(io.BytesIO(file_bytes))
        text = "\n".join([p.text for p in doc.paragraphs])
        log.info("DOCX extraction successful", filename=filename, num_paragraphs=len(doc.paragraphs))
        return text
    except Exception as e:
        log.error("DOCX extraction failed", filename=filename, error=str(e))
        raise DocxExtractionError(f"Error extracting DOCX: {filename}") from e

```

## File: `app\services\extractors\html_extractor.py`
```py
from bs4 import BeautifulSoup
import structlog

log = structlog.get_logger(__name__)

class HtmlExtractionError(Exception):
    pass

def extract_text_from_html(file_bytes: bytes, filename: str = "unknown.html", encoding: str = "utf-8") -> str:
    """Extrae texto de los bytes de un archivo HTML."""
    log.debug("Extracting text from HTML bytes", filename=filename)
    try:
        html = file_bytes.decode(encoding)
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(separator="\n")
        log.info("HTML extraction successful", filename=filename, length=len(text))
        return text
    except Exception as e:
        log.error("HTML extraction failed", filename=filename, error=str(e))
        raise HtmlExtractionError(f"Error extracting HTML: {filename}") from e

```

## File: `app\services\extractors\md_extractor.py`
```py
import markdown
import html2text
import structlog

log = structlog.get_logger(__name__)

class MdExtractionError(Exception):
    pass

def extract_text_from_md(file_bytes: bytes, filename: str = "unknown.md", encoding: str = "utf-8") -> str:
    """Extrae texto de los bytes de un archivo Markdown."""
    log.debug("Extracting text from MD bytes", filename=filename)
    try:
        md_text = file_bytes.decode(encoding)
        html = markdown.markdown(md_text)
        text = html2text.html2text(html)
        log.info("MD extraction successful", filename=filename, length=len(text))
        return text
    except Exception as e:
        log.error("MD extraction failed", filename=filename, error=str(e))
        raise MdExtractionError(f"Error extracting MD: {filename}") from e

```

## File: `app\services\extractors\pdf_extractor.py`
```py
import fitz  # PyMuPDF
import structlog

log = structlog.get_logger(__name__)

class PdfExtractionError(Exception):
    pass

def extract_text_from_pdf(file_bytes: bytes, filename: str = "unknown.pdf") -> str:
    """Extrae texto plano de los bytes de un PDF usando PyMuPDF."""
    log.debug("Extracting text from PDF bytes", filename=filename)
    try:
        with fitz.open(stream=file_bytes, filetype="pdf") as doc:
            text_content = ""
            for page_num, page in enumerate(doc):
                try:
                    text_content += page.get_text("text") + "\n"
                except Exception as page_err:
                    log.warning("Error extracting text from PDF page", filename=filename, page=page_num + 1, error=str(page_err))
            log.info("PDF extraction successful", filename=filename, num_pages=len(doc))
            return text_content
    except Exception as e:
        log.error("PDF extraction failed", filename=filename, error=str(e))
        raise PdfExtractionError(f"Error extracting PDF: {filename}") from e

```

## File: `app\services\extractors\txt_extractor.py`
```py
import structlog

log = structlog.get_logger(__name__)

class TxtExtractionError(Exception):
    pass

def extract_text_from_txt(file_bytes: bytes, filename: str = "unknown.txt", encoding: str = "utf-8") -> str:
    """Extrae texto de los bytes de un archivo TXT."""
    log.debug("Extracting text from TXT bytes", filename=filename)
    try:
        text = file_bytes.decode(encoding)
        log.info("TXT extraction successful", filename=filename, length=len(text))
        return text
    except Exception as e:
        log.error("TXT extraction failed", filename=filename, error=str(e))
        raise TxtExtractionError(f"Error extracting TXT: {filename}") from e

```

## File: `app\services\gcs_client.py`
```py
# ingest-service/app/services/gcs_client.py
import io
import uuid
from typing import Optional
import structlog
import asyncio
from google.cloud import storage
from google.api_core.exceptions import NotFound, Forbidden, GoogleAPIError
from app.core.config import settings

log = structlog.get_logger(__name__)

class GCSError(Exception):
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
    def download_file_sync(self, object_name: str, file_path: str):
        """Synchronously downloads a file from GCS to a local path."""
        try:
            blob = self._bucket.blob(object_name)
            blob.download_to_filename(file_path)
            self.log.info("File downloaded from GCS", object_name=object_name, file_path=file_path)
        except NotFound:
            self.log.error("GCS file not found for download", object_name=object_name)
            raise GCSError("GCS file not found for download")
        except GoogleAPIError as e:
            self.log.error("GCS download failed", error=str(e))
            raise GCSError("GCS download failed", e)
        except Exception as e:
            self.log.error("Unexpected error during GCS download", error=str(e))
            raise GCSError("Unexpected error during GCS download", e)
    """Client to interact with Google Cloud Storage using configured settings."""
    def __init__(self, bucket_name: Optional[str] = None):
        self.bucket_name = bucket_name or settings.GCS_BUCKET_NAME
        self._client: Optional[storage.Client] = None
        self._bucket: Optional[storage.Bucket] = None
        self.log = log.bind(gcs_bucket=self.bucket_name)
        self._initialize_client()

    def _initialize_client(self):
        self.log.debug("Initializing GCS client...")
        try:
            self._client = storage.Client()
            self._bucket = self._client.get_bucket(self.bucket_name)
            self.log.info("GCS client initialized successfully.")
        except NotFound as e:
            self.log.critical("CRITICAL: GCS bucket not found", error=str(e), exc_info=True)
            raise GCSError("GCS bucket not found", e)
        except GoogleAPIError as e:
            self.log.critical("CRITICAL: Failed to initialize GCS client", error=str(e), exc_info=True)
            raise GCSError("GCS client initialization failed", e)
        except Exception as e:
            self.log.critical("CRITICAL: Unexpected error initializing GCS client", error=str(e), exc_info=True)
            raise GCSError("Unexpected error initializing GCS client", e)

    async def upload_file_async(self, object_name: str, data: bytes, content_type: Optional[str] = None):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.upload_file_sync, object_name, data, content_type)

    def upload_file_sync(self, object_name: str, data: bytes, content_type: Optional[str] = None):
        try:
            blob = self._bucket.blob(object_name)
            blob.upload_from_string(data, content_type=content_type)
            self.log.info("File uploaded to GCS", object_name=object_name)
        except GoogleAPIError as e:
            self.log.error("GCS upload failed", error=str(e))
            raise GCSError("GCS upload failed", e)
        except Exception as e:
            self.log.error("Unexpected error during GCS upload", error=str(e))
            raise GCSError("Unexpected error during GCS upload", e)

    async def check_file_exists_async(self, object_name: str) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.check_file_exists_sync, object_name)

    def check_file_exists_sync(self, object_name: str) -> bool:
        try:
            blob = self._bucket.blob(object_name)
            exists = blob.exists()
            self.log.debug("Checked GCS file existence", object_name=object_name, exists=exists)
            return exists
        except GoogleAPIError as e:
            self.log.error("GCS existence check failed", error=str(e))
            raise GCSError("GCS existence check failed", e)
        except Exception as e:
            self.log.error("Unexpected error during GCS existence check", error=str(e))
            raise GCSError("Unexpected error during GCS existence check", e)

    async def delete_file_async(self, object_name: str):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.delete_file_sync, object_name)

    def delete_file_sync(self, object_name: str):
        try:
            blob = self._bucket.blob(object_name)
            blob.delete()
            self.log.info("File deleted from GCS", object_name=object_name)
        except NotFound:
            self.log.info("GCS file already deleted or not found", object_name=object_name)
        except GoogleAPIError as e:
            self.log.error("GCS delete failed", error=str(e))
            raise GCSError("GCS delete failed", e)
        except Exception as e:
            self.log.error("Unexpected error during GCS delete", error=str(e))
            raise GCSError("Unexpected error during GCS delete", e)

    def read_file_sync(self, object_name: str) -> bytes:
        try:
            blob = self._bucket.blob(object_name)
            data = blob.download_as_bytes()
            self.log.info("File read from GCS", object_name=object_name)
            return data
        except NotFound:
            self.log.error("GCS file not found for reading", object_name=object_name)
            raise GCSError("GCS file not found for reading")
        except GoogleAPIError as e:
            self.log.error("GCS read failed", error=str(e))
            raise GCSError("GCS read failed", e)
        except Exception as e:
            self.log.error("Unexpected error during GCS read", error=str(e))
            raise GCSError("Unexpected error during GCS read", e)

```

## File: `app\services\ingest_pipeline.py`
```py
# ingest-service/app/services/ingest_pipeline.py
from __future__ import annotations

import os
import uuid
import structlog
from typing import List, Dict, Any, Optional

# Direct library imports
from pymilvus import (
    Collection, CollectionSchema, FieldSchema, DataType, connections,
    utility, MilvusException
)
# LLM_FLAG: Type hinting only
from sentence_transformers import SentenceTransformer

# Local application imports
from app.core.config import settings
# LLM_FLAG: Import NEW helpers
from .extractors.pdf_extractor import extract_text_from_pdf, PdfExtractionError
from .extractors.docx_extractor import extract_text_from_docx, DocxExtractionError
from .extractors.txt_extractor import extract_text_from_txt, TxtExtractionError
from .extractors.md_extractor import extract_text_from_md, MdExtractionError
from .extractors.html_extractor import extract_text_from_html, HtmlExtractionError
from .text_splitter import split_text
from .embedder import embed_chunks

log = structlog.get_logger(__name__)

# --- Mapeo de Content-Type a Funciones Extractoras (ACTUALIZADO) ---
EXTRACTORS = {
    "application/pdf": extract_text_from_pdf,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": extract_text_from_docx,
    "application/msword": extract_text_from_docx,
    "text/plain": extract_text_from_txt,
    "text/markdown": extract_text_from_md,
    "text/html": extract_text_from_html,
}
EXTRACTION_ERRORS = (
    PdfExtractionError, DocxExtractionError, TxtExtractionError,
    MdExtractionError, HtmlExtractionError, ValueError # Include generic ValueError too
)

# --- Constantes Milvus (Dimensión ACTUALIZADA) ---
MILVUS_COLLECTION_NAME = settings.MILVUS_COLLECTION_NAME
MILVUS_EMBEDDING_DIM = settings.EMBEDDING_DIMENSION # From config (e.g., 384)
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
    alias = "pipeline_worker_zilliz"
    if not _milvus_connected or alias not in connections.list_connections():
        uri = settings.ZILLIZ_URI
        token = settings.ZILLIZ_API_KEY.get_secret_value()
        log.info("Connecting to Zilliz Cloud for pipeline worker...", uri=uri, alias=alias)
        try:
            connections.connect(alias=alias, uri=uri, token=token, secure=True)
            _milvus_connected = True
            log.info("Connected to Zilliz Cloud for pipeline worker.")
        except MilvusException as e:
            log.error("Failed to connect to Zilliz Cloud for pipeline worker.", error=str(e))
            _milvus_connected = False
            raise ConnectionError(f"Zilliz connection failed: {e}") from e
        except Exception as e:
            log.error("Unexpected error connecting to Zilliz Cloud for pipeline worker.", error=str(e))
            _milvus_connected = False
            raise ConnectionError(f"Unexpected Zilliz connection error: {e}") from e

    if _milvus_collection is None:
        if not utility.has_collection(MILVUS_COLLECTION_NAME, using=alias):
            log.warning(f"Zilliz collection '{MILVUS_COLLECTION_NAME}' not found. Attempting to create.")
            try:
                collection = _create_milvus_collection(alias)
            except Exception as create_e:
                log.critical("Failed to create Zilliz collection", error=str(create_e), exc_info=True)
                raise RuntimeError(f"Zilliz collection creation failed: {create_e}") from create_e
        else:
            collection = Collection(name=MILVUS_COLLECTION_NAME, using=alias)
            log.debug(f"Using existing Zilliz collection '{MILVUS_COLLECTION_NAME}'.")
            try:
                if not any(idx.field_name == MILVUS_VECTOR_FIELD for idx in collection.indexes):
                    log.warning(f"Vector index missing on field '{MILVUS_VECTOR_FIELD}'. Creating...")
                    collection.create_index(field_name=MILVUS_VECTOR_FIELD, index_params=settings.MILVUS_INDEX_PARAMS)
                    log.info(f"Vector index created for '{MILVUS_VECTOR_FIELD}'.")
            except Exception as idx_e:
                log.error("Failed to check or create index on existing collection", error=str(idx_e))

        try:
            log.info("Loading Zilliz collection into memory...", collection_name=collection.name)
            collection.load()
            log.info("Zilliz collection loaded into memory.")
        except MilvusException as load_e:
            log.error("Failed to load Zilliz collection into memory", error=str(load_e))
            raise RuntimeError(f"Zilliz collection load failed: {load_e}") from load_e

        _milvus_collection = collection

    if not isinstance(_milvus_collection, Collection):
        log.critical("Zilliz collection object is unexpectedly None or invalid type after initialization attempt.")
        raise RuntimeError("Failed to obtain a valid Zilliz collection object.")

    return _milvus_collection

def _create_milvus_collection(alias: str) -> Collection:
    log.info(f"Defining schema for collection '{settings.MILVUS_COLLECTION_NAME}' with dim={MILVUS_EMBEDDING_DIM}")
    fields = [
        FieldSchema(name=MILVUS_PK_FIELD, dtype=DataType.INT64, is_primary=True, auto_id=True),
        FieldSchema(name=MILVUS_VECTOR_FIELD, dtype=DataType.FLOAT_VECTOR, dim=MILVUS_EMBEDDING_DIM),
        FieldSchema(name=MILVUS_CONTENT_FIELD, dtype=DataType.VARCHAR, max_length=settings.MILVUS_CONTENT_FIELD_MAX_LENGTH),
        FieldSchema(name=MILVUS_COMPANY_ID_FIELD, dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name=MILVUS_DOCUMENT_ID_FIELD, dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name=MILVUS_FILENAME_FIELD, dtype=DataType.VARCHAR, max_length=512),
    ]
    schema = CollectionSchema(fields, description="Document Chunks for Atenex RAG (SentenceTransformer)")
    log.info(f"Creating collection '{settings.MILVUS_COLLECTION_NAME}'...")
    try:
        collection = Collection(name=settings.MILVUS_COLLECTION_NAME, schema=schema, using=alias, consistency_level="Strong")
        log.info(f"Collection '{settings.MILVUS_COLLECTION_NAME}' created. Creating indexes...")

        index_params = settings.MILVUS_INDEX_PARAMS
        log.info("Creating index for vector field", field_name=MILVUS_VECTOR_FIELD, index_params=index_params)
        collection.create_index(field_name=MILVUS_VECTOR_FIELD, index_params=index_params)

        log.info("Creating scalar index for company_id field...")
        collection.create_index(field_name=MILVUS_COMPANY_ID_FIELD, index_name="company_id_idx")
        log.info("Creating scalar index for document_id field...")
        collection.create_index(field_name=MILVUS_DOCUMENT_ID_FIELD, index_name="document_id_idx")

        log.info("All indexes created successfully.")
        return collection
    except MilvusException as e:
        log.error("Failed to create Zilliz collection or index", collection_name=settings.MILVUS_COLLECTION_NAME, error=str(e), exc_info=True)
        raise RuntimeError(f"Zilliz collection/index creation failed: {e}") from e

def delete_milvus_chunks(company_id: str, document_id: str) -> int:
    del_log = log.bind(company_id=company_id, document_id=document_id)
    try:
        collection = _ensure_milvus_connection_and_collection()
        expr = f'{MILVUS_COMPANY_ID_FIELD} == "{company_id}" and {MILVUS_DOCUMENT_ID_FIELD} == "{document_id}"'
        del_log.info("Querying chunks to delete by primary key", filter_expr=expr)

        pk_results = collection.query(expr=expr, output_fields=[MILVUS_PK_FIELD])
        num_to_delete = len(pk_results)

        if num_to_delete == 0:
            del_log.info("No existing chunks found in Zilliz to delete.")
            return 0

        del_log.info(f"Attempting to delete {num_to_delete} chunks from Zilliz using expression.")
        delete_result = collection.delete(expr=expr)
        actual_deleted_count = delete_result.delete_count
        del_log.info("Zilliz delete operation executed", deleted_count=actual_deleted_count)

        if actual_deleted_count != num_to_delete:
            del_log.warning("Mismatch between expected chunks to delete and actual deleted count",
                            expected=num_to_delete, actual=actual_deleted_count)

        return actual_deleted_count
    except MilvusException as e:
        del_log.error("Zilliz delete error", error=str(e), exc_info=True)
        return 0
    except Exception as e:
        del_log.exception("Unexpected error during Zilliz chunk deletion")
        raise RuntimeError(f"Unexpected Zilliz deletion error: {e}") from e


# --- REFACTORIZADO: Main Ingestion Pipeline Function ---
def ingest_document_pipeline(
    file_bytes: bytes,
    filename: str,
    company_id: str,
    document_id: str,
    content_type: str,
    embedding_model: SentenceTransformer, # LLM_FLAG: Type updated
    delete_existing: bool = True
) -> int:
    ingest_log = log.bind(
        company_id=company_id,
        document_id=document_id,
        filename=filename,
        content_type=content_type
    )
    ingest_log.info("Starting REFACTORED ingestion pipeline (MiniLM/PyMuPDF)")

    # --- 1. Select Extractor ---
    extractor = EXTRACTORS.get(content_type)
    if not extractor:
        ingest_log.error("Unsupported content type for extraction.")
        raise ValueError(f"Unsupported content type: {content_type}")

    # --- 2. Extract Text ---
    ingest_log.debug("Extracting text content from bytes...")
    try:
        text_content = extractor(file_bytes, filename=filename)
        if not text_content or text_content.isspace():
            ingest_log.warning("No text content extracted from the document. Skipping.")
            return 0
        ingest_log.info(f"Text extracted successfully, length: {len(text_content)} chars.")
    except EXTRACTION_ERRORS as ve:
        ingest_log.error("Text extraction failed.", error=str(ve), exc_info=True)
        raise ValueError(f"Extraction failed for {filename}: {ve}") from ve
    except Exception as e:
        ingest_log.exception("Unexpected error during text extraction")
        raise RuntimeError(f"Unexpected extraction error: {e}") from e

    # --- 3. Chunk Text ---
    ingest_log.debug("Chunking extracted text...")
    try:
        chunks = split_text(text_content)
        if not chunks:
            ingest_log.warning("Text content resulted in zero chunks. Skipping.")
            return 0
        ingest_log.info(f"Text chunked into {len(chunks)} chunks.")
    except Exception as e:
        ingest_log.error("Failed to chunk text", error=str(e), exc_info=True)
        raise RuntimeError(f"Chunking failed: {e}") from e

    # --- 4. Embed Chunks ---
    ingest_log.debug(f"Generating embeddings for {len(chunks)} chunks...")
    try:
        embeddings = embed_chunks(chunks)
        ingest_log.info(f"Embeddings generated successfully for {len(embeddings)} chunks.")
        if len(embeddings) != len(chunks):
            ingest_log.error("CRITICAL: Mismatch between number of chunks and generated embeddings!",
                             num_chunks=len(chunks), num_embeddings=len(embeddings))
            raise RuntimeError("Embedding count mismatch error.")
        if embeddings and len(embeddings[0]) != MILVUS_EMBEDDING_DIM:
             ingest_log.error("CRITICAL: Generated embedding dimension mismatch!",
                              actual_dim=len(embeddings[0]), expected_dim=MILVUS_EMBEDDING_DIM)
             raise RuntimeError("Embedding dimension mismatch error.")
    except Exception as e:
        ingest_log.error("Failed to generate embeddings", error=str(e), exc_info=True)
        raise RuntimeError(f"Embedding generation failed: {e}") from e

    # --- 5. Prepare Data for Milvus ---
    max_content_len = settings.MILVUS_CONTENT_FIELD_MAX_LENGTH
    def truncate_utf8_bytes(s, max_bytes):
        b = s.encode('utf-8', errors='ignore')
        if len(b) <= max_bytes: return s
        return b[:max_bytes].decode('utf-8', errors='ignore')

    truncated_chunks = [truncate_utf8_bytes(c, max_content_len) for c in chunks]

    data_to_insert = [
        embeddings,
        truncated_chunks,
        [company_id] * len(chunks),
        [document_id] * len(chunks),
        [filename] * len(chunks),
    ]
    field_names_for_insert = [
        MILVUS_VECTOR_FIELD, MILVUS_CONTENT_FIELD, MILVUS_COMPANY_ID_FIELD,
        MILVUS_DOCUMENT_ID_FIELD, MILVUS_FILENAME_FIELD
    ]
    ingest_log.debug(f"Prepared {len(chunks)} entities for Milvus insertion.", fields=field_names_for_insert)

    # --- 6. Delete Existing Chunks ---
    if delete_existing:
        ingest_log.info("Attempting to delete existing chunks before insertion...")
        try:
            deleted_count = delete_milvus_chunks(company_id, document_id)
            ingest_log.info(f"Deleted {deleted_count} existing chunks.")
        except Exception as del_err:
            ingest_log.error("Failed to delete existing chunks, proceeding with insert anyway.", error=str(del_err))

    # --- 7. Insert into Milvus ---
    ingest_log.debug(f"Inserting {len(chunks)} chunks into Milvus collection '{MILVUS_COLLECTION_NAME}'...")
    try:
        collection = _ensure_milvus_connection_and_collection()
        mutation_result = collection.insert(data_to_insert)
        inserted_count = mutation_result.insert_count

        if inserted_count == len(chunks):
            ingest_log.info(f"Successfully inserted {inserted_count} chunks into Milvus.")
        else:
            ingest_log.error(f"CRITICAL: Milvus insert count mismatch!",
                             expected=len(chunks), inserted=inserted_count, pk_errors=mutation_result.err_indices)
            return 0 # Indicate partial/failed insert

        ingest_log.debug("Flushing Milvus collection...")
        collection.flush()
        ingest_log.info("Milvus collection flushed.")

        return inserted_count

    except MilvusException as e:
        ingest_log.error("Failed to insert data into Milvus", error=str(e), exc_info=True)
        raise RuntimeError(f"Milvus insertion failed: {e}") from e
    except Exception as e:
        ingest_log.exception("Unexpected error during Milvus insertion")
        raise RuntimeError(f"Unexpected Milvus insertion error: {e}") from e
```

## File: `app\services\text_splitter.py`
```py
import re
from typing import List
import structlog
from app.core.config import settings

log = structlog.get_logger(__name__)

CHUNK_SIZE = getattr(settings, 'SPLITTER_CHUNK_SIZE', 1000)
CHUNK_OVERLAP = getattr(settings, 'SPLITTER_CHUNK_OVERLAP', 250)

def split_text(text: str) -> List[str]:
    """
    Divide el texto en chunks de tamaño CHUNK_SIZE con solapamiento CHUNK_OVERLAP.
    """
    log.debug("Splitting text into chunks", chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
    words = text.split()
    chunks = []
    start = 0
    while start < len(words):
        end = min(start + CHUNK_SIZE, len(words))
        chunk = " ".join(words[start:end])
        chunks.append(chunk)
        if end == len(words):
            break
        start += CHUNK_SIZE - CHUNK_OVERLAP
    log.info("Text split into chunks", num_chunks=len(chunks))
    return chunks

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
def normalize_filename(filename: str) -> str:
    """Normaliza el nombre de archivo eliminando espacios al inicio/final y espacios duplicados."""
    return " ".join(filename.strip().split())
# ingest-service/app/tasks/process_document.py
import os
import tempfile
import uuid
import sys
import pathlib
import time
from typing import Optional, Dict, Any

import structlog
from celery import Task
from celery.exceptions import Ignore, Reject, MaxRetriesExceededError
from celery.signals import worker_process_init
from sqlalchemy import Engine
# LLM_FLAG: Import correct embedding components
from sentence_transformers import SentenceTransformer
from app.services.embedder import get_embedding_model # Function to load/cache model

# --- Custom Application Imports ---
from app.core.config import settings
from app.db.postgres_client import get_sync_engine, set_status_sync
from app.models.domain import DocumentStatus
from app.services.gcs_client import GCSClient, GCSError
# LLM_FLAG: Import REFACTORIZADO pipeline and EXTRACTORS map
from app.services.ingest_pipeline import ingest_document_pipeline, EXTRACTORS, EXTRACTION_ERRORS
from app.tasks.celery_app import celery_app

task_struct_log = structlog.get_logger(__name__)
IS_WORKER = "worker" in sys.argv

# --- Global Resources for Worker Process ---
sync_engine: Optional[Engine] = None
gcs_client: Optional[GCSClient] = None
worker_embedding_model: Optional[SentenceTransformer] = None

# LLM_FLAG: Initialize resources at worker startup
@worker_process_init.connect(weak=False)
def init_worker_resources(**kwargs):
    global sync_engine, minio_client, worker_embedding_model
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

        if worker_embedding_model is None:
            log.info("Preloading embedding model...")
            worker_embedding_model = get_embedding_model() # This handles loading and caching
            log.info("Embedding model preloaded.")
        else:
            log.info("Embedding model already preloaded.")

    except Exception as e:
        log.critical("CRITICAL FAILURE during worker resource initialization!", error=str(e), exc_info=True)
        # Set to None so pre-checks in task will fail cleanly
        sync_engine = None
        gcs_client = None
        worker_embedding_model = None

# --------------------------------------------------------------------------
# Refactored Celery Task Definition
# --------------------------------------------------------------------------
@celery_app.task(
    bind=True,
    name="ingest.process_document",
    autoretry_for=(Exception,),
    # LLM_FLAG: Exclude specific non-retriable errors
    exclude=(Reject, Ignore, ValueError, ConnectionError, RuntimeError, TypeError, *EXTRACTION_ERRORS),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=3
)
def process_document_standalone(self: Task, *args, **kwargs) -> Dict[str, Any]:
    """
    Refactored Celery task: Downloads from MinIO, passes bytes to the new pipeline
    (using SentenceTransformer, PyMuPDF, etc.), updates status sync.
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
    log.info("Starting REFACTORED standalone document processing task (MiniLM/PyMuPDF)")

    # --- Pre-checks ---
    if not IS_WORKER:
         log.critical("Task function called outside of a worker context! Rejecting.")
         raise Reject("Task running outside worker context.", requeue=False)

    if not all([document_id_str, company_id_str, filename, content_type]):
        log.error("Missing required arguments in task payload.", payload_kwargs=kwargs)
        raise Reject("Missing required arguments (doc_id, company_id, filename, content_type)", requeue=False)

    # LLM_FLAG: Check worker resources were initialized correctly
    if not sync_engine:
         log.critical("Worker Sync DB Engine is not initialized. Task cannot proceed.")
         raise Reject("Worker sync DB engine initialization failed.", requeue=False)
    if not worker_embedding_model: # Check the global variable holding the model
         log.critical("Worker Embedding Model (SentenceTransformer) is not available/loaded. Task cannot proceed.")
         error_msg = "Worker embedding model init/preload failed."
         doc_uuid_err = None
         try: doc_uuid_err = uuid.UUID(document_id_str)
         except ValueError: pass
         if doc_uuid_err:
             try: set_status_sync(engine=sync_engine, document_id=doc_uuid_err, status=DocumentStatus.ERROR, error_message=error_msg)
             except Exception as db_err: log.critical("Failed to update status after embedding model check failure!", error=str(db_err))
         raise Reject(error_msg, requeue=False)
    if not gcs_client:
        log.critical("Worker GCS Client is not initialized. Task cannot proceed.")
        error_msg = "Worker GCS client init failed."
        doc_uuid_err = None
        try:
            doc_uuid_err = uuid.UUID(document_id_str)
        except ValueError:
            pass
        if doc_uuid_err:
            try:
                set_status_sync(engine=sync_engine, document_id=doc_uuid_err, status=DocumentStatus.ERROR, error_message=error_msg)
            except Exception as db_err:
                log.critical("Failed to update status after GCS client check failure!", error=str(db_err))
        raise Reject(error_msg, requeue=False)

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
        raise Reject(error_msg, requeue=False) # Non-retriable

    normalized_filename = normalize_filename(filename) if filename else filename
    object_name = f"{company_id_str}/{document_id_str}/{normalized_filename}"
    temp_file_path_obj: Optional[pathlib.Path] = None
    inserted_chunk_count = 0
    file_bytes: Optional[bytes] = None

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

        # 2. Download file and read bytes
        # Download file bytes directly from GCS
        max_gcs_retries = 3
        gcs_delay = 2
        for attempt_num in range(1, max_gcs_retries + 1):
            try:
                file_bytes = gcs_client.read_file_sync(object_name)
                log.info(f"File content read from GCS into memory ({len(file_bytes)} bytes).")
                break
            except GCSError as ge:
                if "not found" in str(ge).lower():
                    log.warning(f"GCS file not found on download attempt {attempt_num}/{max_gcs_retries}. Retrying in {gcs_delay}s...", error=str(ge))
                    if attempt_num == max_gcs_retries:
                        log.error(f"File still not found in GCS after {max_gcs_retries} attempts. Aborting.")
                        raise
                    time.sleep(gcs_delay)
                    gcs_delay *= 2
                else:
                    log.error("Non-retriable GCS error during download", error=str(ge))
                    raise
            except Exception as read_err:
                log.error("Failed to read file from GCS into bytes", error=str(read_err), exc_info=True)
                raise RuntimeError(f"Failed to read file from GCS: {read_err}") from read_err
        # Check if file_bytes was successfully read
            if file_bytes is None:
                 raise RuntimeError("File download or read failed, bytes not available.")

            # 3. Execute Refactored Ingestion Pipeline
            log.info("Executing refactored ingest pipeline...")
        inserted_chunk_count = ingest_document_pipeline(
            file_bytes=file_bytes,
            filename=normalized_filename,
            company_id=company_id_str,
            document_id=document_id_str,
            content_type=content_type,
            embedding_model=worker_embedding_model, # Pass the loaded model
            delete_existing=True
        )
        log.info(f"Ingestion pipeline finished. Inserted chunks reported: {inserted_chunk_count}")

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
    except GCSError as ge:
        log.error(f"GCS Error during processing", error=str(ge), exc_info=True)
        error_msg = f"GCS Error: {str(ge)[:400]}"
        try:
            set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
        except Exception as db_err:
            log.critical("Failed to update status after GCS failure!", error=str(db_err))
        if "not found" in str(ge).lower():
            raise Reject(f"GCS Error: Object not found: {object_name}", requeue=False) from ge
        else:
            raise ge  # Allow retry for other potential GCS errors

    except (*EXTRACTION_ERRORS, ValueError, RuntimeError, TypeError) as pipeline_err:
         # Catches extraction errors, embedding errors, chunking errors, Milvus errors, etc.
         # Considered non-retriable by default based on 'exclude' tuple
         log.error(f"Pipeline Error: {pipeline_err}", exc_info=True)
         error_msg = f"Pipeline Error: {type(pipeline_err).__name__} - {str(pipeline_err)[:400]}"
         try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
         except Exception as db_err: log.critical("Failed to update status after pipeline failure!", error=str(db_err))
         raise Reject(f"Pipeline failed: {error_msg}", requeue=False) from pipeline_err

    except Reject as r:
         log.error(f"Task rejected permanently: {r.reason}")
         if sync_engine and doc_uuid:
             try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=f"Rejected: {r.reason}"[:500])
             except Exception as db_err: log.critical("Failed to update status after task rejection!", error=str(db_err))
         raise r

    except Ignore:
         log.info("Task ignored.")
         raise Ignore()

    except MaxRetriesExceededError as mree:
        log.error("Max retries exceeded for task.", exc_info=True)
        final_error = mree.cause if mree.cause else mree
        error_msg = f"Max retries exceeded ({max_attempts}). Last error: {type(final_error).__name__} - {str(final_error)[:300]}"
        try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
        except Exception as db_err: log.critical("Failed to update status to ERROR after max retries!", error=str(db_err))
        # Let Celery handle logging MaxRetriesExceededError

    except Exception as exc:
        # Catch-all for unexpected errors, triggers Celery's autoretry
        log.exception(f"An unexpected error occurred, will retry if attempts remain.")
        error_msg = f"Attempt {attempt} failed: {type(exc).__name__} - {str(exc)[:400]}"
        try: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg)
        except Exception as db_err: log.critical("CRITICAL: Failed to update status after unexpected failure!", error=str(db_err))
        raise exc

# LLM_FLAG: Keeping alias for potential external references
process_document_haystack_task = process_document_standalone
```

## File: `pyproject.toml`
```toml
[tool.poetry]
name = "ingest-service"
version = "0.3.0" # Incrementamos versión por refactorización MiniLM/PyMuPDF
description = "Ingest service for Atenex B2B SaaS (Postgres/Minio/Milvus/SentenceTransformers - CPU - Prefork)"
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

google-cloud-storage = "^2.16.0"
cloud-sql-python-connector = {extras = ["asyncpg", "psycopg2"], version = "^1.5.0"}
google-auth = "^2.29.0"

# --- ONNX Runtime ---
onnxruntime = "^1.18.0"

# --- Core Processing Dependencies (REFACTORIZADO) ---
pymupdf = "^1.25.0"                # PyMuPDF for PDF extraction
sentence-transformers = "^2.7"     # For MiniLM embeddings
pymilvus = ">=2.4.1,<2.5.0"         # Official Milvus client

# --- Converter Dependencies (Standalone - MANTENER) ---
# pypdf = ">=4.0.1,<5.0.0"          # ELIMINADO/COMENTADO - Reemplazado por PyMuPDF
python-docx = ">=1.1.0,<2.0.0"
markdown = ">=3.5.1,<4.0.0"
beautifulsoup4 = ">=4.12.3,<5.0.0"
html2text = ">=2024.1.0,<2025.0.0"  # For Markdown -> HTML -> Text conversion

# --- HTTP Client (API - MANTENER) ---
httpx = {extras = ["http2"], version = "^0.27.0"}
h2 = "^4.1.0"

# --- Synchronous DB Dependencies (Worker - MANTENER) ---
sqlalchemy = "^2.0.28"
psycopg2-binary = "^2.9.9"

# --- ELIMINADO ---
# fastembed = ">=0.2.1,<0.3.0"      # ELIMINADO

[tool.poetry.group.dev.dependencies]
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
