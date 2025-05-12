from fastapi import Body
from typing import List, Dict, Any
# --- Bulk Delete Endpoint ---
@router.delete(
    "/bulk",
    status_code=status.HTTP_200_OK,
    summary="Bulk delete documents and all associated data (DB, GCS, Zilliz)",
    response_model=Dict[str, Any],
    responses={
        200: {"description": "Bulk delete result: lists of deleted and failed IDs."},
        401: {"model": ErrorDetail, "description": "Unauthorized"},
        422: {"model": ErrorDetail, "description": "Validation Error (Missing Headers or Invalid Body)"},
        500: {"model": ErrorDetail, "description": "Internal Server Error"},
    }
)
async def bulk_delete_documents(
    request: Request,
    body: Dict[str, List[str]] = Body(..., example={"document_ids": ["id1", "id2"]}),
    gcs_client: GCSClient = Depends(get_gcs_client),
):
    """
    Bulk deletes documents: removes from Milvus (Zilliz), GCS, and PostgreSQL.
    Continues on error, returns lists of deleted and failed IDs with reasons.
    """
    company_id = request.headers.get("X-Company-ID")
    req_id = getattr(request.state, 'request_id', 'N/A')
    if not company_id:
        log.bind(request_id=req_id).warning("Missing X-Company-ID header in bulk_delete_documents")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")
    try:
        company_uuid = uuid.UUID(company_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")

    document_ids = body.get("document_ids")
    if not document_ids or not isinstance(document_ids, list):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Body must include 'document_ids' as a list.")

    deleted: List[str] = []
    failed: List[Dict[str, str]] = []

    for doc_id in document_ids:
        try:
            doc_uuid = uuid.UUID(doc_id)
        except Exception:
            failed.append({"id": doc_id, "error": "ID inválido"})
            continue
        try:
            # Reuse the single delete logic for each document
            # (simulate a request for each, but keep the same company_id)
            # This logic is similar to delete_document_endpoint
            doc_data = None
            async with get_db_conn() as conn:
                doc_data = await api_db_retry_strategy(db_client.get_document_by_id)(
                    conn, doc_id=doc_uuid, company_id=company_uuid
                )
            if not doc_data:
                failed.append({"id": doc_id, "error": "No encontrado"})
                continue
            errors = []
            # 1. Milvus
            loop = asyncio.get_running_loop()
            try:
                milvus_deleted = await loop.run_in_executor(None, _delete_milvus_sync, str(doc_uuid), company_id)
                if not milvus_deleted:
                    errors.append("Milvus")
            except Exception as e:
                errors.append(f"Milvus: {type(e).__name__}")
            # 2. GCS
            gcs_path = doc_data.get('file_path')
            if gcs_path:
                try:
                    await gcs_client.delete_file_async(gcs_path)
                except Exception as e:
                    errors.append(f"GCS: {type(e).__name__}")
            else:
                errors.append("GCS: path desconocido")
            # 3. PostgreSQL
            try:
                async with get_db_conn() as conn:
                    deleted_in_db = await api_db_retry_strategy(db_client.delete_document)(
                        conn=conn, doc_id=doc_uuid, company_id=company_uuid
                    )
                if not deleted_in_db:
                    errors.append("DB: no eliminado")
            except Exception as e:
                errors.append(f"DB: {type(e).__name__}")
            if errors:
                failed.append({"id": doc_id, "error": ", ".join(errors)})
            else:
                deleted.append(doc_id)
        except Exception as e:
            failed.append({"id": doc_id, "error": str(e)})

    return {"deleted": deleted, "failed": failed}
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
        sync_milvus_log.info("Connecting to Milvus (Zilliz) for sync helper...")
        try:
            connections.connect(
                alias=alias,
                uri=settings.MILVUS_URI,
                timeout=settings.MILVUS_GRPC_TIMEOUT,
                token=settings.ZILLIZ_API_KEY.get_secret_value() if settings.ZILLIZ_API_KEY else None
            )
            sync_milvus_log.info("Milvus (Zilliz) connection established for sync helper.")
        except MilvusException as e:
            sync_milvus_log.error("Failed to connect to Milvus (Zilliz) for sync helper", error=str(e))
            raise RuntimeError(f"Milvus (Zilliz) connection failed for API helper: {e}") from e
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
