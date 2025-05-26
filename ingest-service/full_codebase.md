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
from datetime import date
from app.api.v1.schemas import DocumentStatsResponse, DocumentStatsByStatus, DocumentStatsByType

import uuid
import mimetypes
import json
from typing import List, Optional, Dict, Any
import asyncio
from contextlib import asynccontextmanager
import logging

from fastapi import (
    APIRouter, Depends, HTTPException, status,
    UploadFile, File, Form, Header, Query, Path, BackgroundTasks, Request, Body
)
import structlog
import asyncpg
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type, before_sleep_log

from pymilvus import Collection, connections, utility, MilvusException

from app.core.config import settings
from app.db import postgres_client as db_client
from app.models.domain import DocumentStatus
from app.api.v1.schemas import IngestResponse, StatusResponse, PaginatedStatusResponse, ErrorDetail
from app.services.gcs_client import GCSClient, GCSClientError
from app.tasks.celery_app import celery_app
from app.tasks.process_document import process_document_standalone as process_document_task
from app.services.ingest_pipeline import (
    MILVUS_COLLECTION_NAME,
    MILVUS_COMPANY_ID_FIELD,
    MILVUS_DOCUMENT_ID_FIELD,
    MILVUS_PK_FIELD,
)

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Helper Functions ---

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

api_db_retry_strategy = retry(
    stop=stop_after_attempt(2),
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
    except HTTPException: 
        raise
    except Exception as e: 
        log.error("Database connection error occurred during request processing", error=str(e), exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database connection error.")
    finally:
        if conn and pool:
            try:
                await pool.release(conn)
            except Exception as release_err:
                log.error("Error releasing DB connection back to pool", error=str(release_err), exc_info=True)


def _get_milvus_collection_sync() -> Optional[Collection]:
    alias = "api_sync_helper"
    sync_milvus_log = log.bind(component="MilvusHelperSync", alias=alias, collection_name=MILVUS_COLLECTION_NAME)
    
    try:
        if alias in connections.list_connections():
            connections.get_connection_addr(alias)
            sync_milvus_log.debug("Reusing existing Milvus connection for sync helper.")
        else:
            raise MilvusException(message="Alias not found, attempting new connection.") 
            
    except Exception: 
        sync_milvus_log.info("Connecting to Milvus (Zilliz) for sync helper...")
        try:
            connections.connect(
                alias=alias,
                uri=settings.MILVUS_URI,
                timeout=settings.MILVUS_GRPC_TIMEOUT,
                token=settings.ZILLIZ_API_KEY.get_secret_value() if settings.ZILLIZ_API_KEY else None
            )
            sync_milvus_log.info("Milvus (Zilliz) connection established for sync helper.")
        except MilvusException as me:
            sync_milvus_log.error("Failed to connect to Milvus (Zilliz) for sync helper", error=str(me))
            raise RuntimeError(f"Milvus (Zilliz) connection failed for API helper: {me}") from me
        except Exception as e:
            sync_milvus_log.error("Unexpected error connecting to Milvus (Zilliz)", error=str(e))
            raise RuntimeError(f"Unexpected Milvus (Zilliz) connection error for API helper: {e}") from e

    try:
        if not utility.has_collection(MILVUS_COLLECTION_NAME, using=alias):
            sync_milvus_log.warning("Milvus collection does not exist for sync helper.")
            return None

        collection = Collection(name=MILVUS_COLLECTION_NAME, using=alias)
        sync_milvus_log.debug(f"Collection object for '{MILVUS_COLLECTION_NAME}' obtained for sync helper.")
        return collection

    except MilvusException as e:
        sync_milvus_log.error("Milvus error during collection check/instantiation for sync helper", error=str(e))
        return None 
    except Exception as e:
        sync_milvus_log.exception("Unexpected error during Milvus collection access for sync helper", error=str(e))
        return None


def _get_milvus_chunk_count_sync(document_id: str, company_id: str) -> int:
    count_log = log.bind(document_id=document_id, company_id=company_id, component="MilvusHelperSync")
    try:
        collection = _get_milvus_collection_sync()
        if collection is None:
            count_log.warning("Cannot count chunks: Milvus collection does not exist or is not accessible.")
            return -1 

        expr = f'{MILVUS_COMPANY_ID_FIELD} == "{company_id}" and {MILVUS_DOCUMENT_ID_FIELD} == "{document_id}"'
        count_log.debug("Attempting to query Milvus chunk count", filter_expr=expr)
        
        query_res = collection.query(expr=expr, output_fields=[MILVUS_PK_FIELD], consistency_level="Strong")
        count = len(query_res) if query_res else 0

        count_log.info("Milvus chunk count successful (pymilvus)", count=count)
        return count
    except RuntimeError as re: 
        count_log.error("Failed to get Milvus count due to connection error", error=str(re))
        return -1
    except MilvusException as e:
        count_log.error("Milvus query error during count", error=str(e), exc_info=True)
        return -1
    except Exception as e:
        count_log.exception("Unexpected error during Milvus count", error=str(e))
        return -1

def _delete_milvus_sync(document_id: str, company_id: str) -> bool:
    delete_log = log.bind(document_id=document_id, company_id=company_id, component="MilvusHelperSync")
    expr = f'{MILVUS_COMPANY_ID_FIELD} == "{company_id}" and {MILVUS_DOCUMENT_ID_FIELD} == "{document_id}"'
    pks_to_delete: List[str] = []

    try:
        collection = _get_milvus_collection_sync()
        if collection is None:
            delete_log.warning("Cannot delete chunks: Milvus collection does not exist or is not accessible.")
            return False 

        delete_log.info("Querying Milvus for PKs to delete (sync)...", filter_expr=expr)
        query_res = collection.query(expr=expr, output_fields=[MILVUS_PK_FIELD], consistency_level="Strong")
        pks_to_delete = [item[MILVUS_PK_FIELD] for item in query_res if MILVUS_PK_FIELD in item]

        if not pks_to_delete:
            delete_log.info("No matching primary keys found in Milvus for deletion (sync).")
            return True 

        delete_log.info(f"Found {len(pks_to_delete)} primary keys to delete (sync).")

        delete_expr = f'{MILVUS_PK_FIELD} in {json.dumps(pks_to_delete)}'
        delete_log.info("Attempting to delete chunks from Milvus using PK list expression (sync).", filter_expr=delete_expr)
        delete_result = collection.delete(expr=delete_expr)
        collection.flush() 
        delete_log.info("Milvus delete operation by PK list executed and flushed (sync).", deleted_count=delete_result.delete_count)

        if delete_result.delete_count != len(pks_to_delete):
             delete_log.warning("Milvus delete count mismatch (sync).", expected=len(pks_to_delete), reported=delete_result.delete_count)
        return True

    except RuntimeError as re: 
        delete_log.error("Failed to delete Milvus chunks due to connection error", error=str(re))
        return False
    except MilvusException as e:
        delete_log.error("Milvus query or delete error (sync)", error=str(e), exc_info=True)
        return False
    except Exception as e:
        delete_log.exception("Unexpected error during Milvus delete (sync)", error=str(e))
        return False


def normalize_filename(filename: str) -> str:
    return " ".join(filename.strip().split())

# --- API Endpoints ---

@router.get(
    "/stats",
    response_model=DocumentStatsResponse,
    summary="Get aggregated document statistics for a company",
    description="Provides aggregated statistics about documents for the specified company, with optional filters.",
    responses={
        200: {"description": "Successfully retrieved document statistics."},
        401: {"model": ErrorDetail, "description": "Unauthorized (Not used if gateway handles auth)"},
        422: {"model": ErrorDetail, "description": "Validation Error (e.g., missing X-Company-ID or invalid date format)"},
        500: {"model": ErrorDetail, "description": "Internal Server Error"},
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB error)"},
    }
)
async def get_document_statistics(
    request: Request,
    from_date: Optional[date] = Query(None, description="Filter statistics from this date (YYYY-MM-DD). Inclusive."),
    to_date: Optional[date] = Query(None, description="Filter statistics up to this date (YYYY-MM-DD). Inclusive."),
    status_filter: Optional[DocumentStatus] = Query(None, alias="status", description="Filter statistics by a specific document status."),
):
    company_id_str = request.headers.get("X-Company-ID")
    req_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    stats_log = log.bind(request_id=req_id)

    if not company_id_str:
        stats_log.warning("Missing X-Company-ID header for document statistics")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")
    try:
        company_uuid = uuid.UUID(company_id_str)
    except ValueError:
        stats_log.warning("Invalid Company ID format for document statistics", company_id_received=company_id_str)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Company ID format.")

    stats_log = stats_log.bind(company_id=company_id_str, from_date=from_date, to_date=to_date, status_filter=status_filter)
    stats_log.info("Request for document statistics received")

    where_clauses = ["company_id = $1"]
    params: List[Any] = [company_uuid]
    param_idx = 2

    if from_date:
        where_clauses.append(f"uploaded_at >= ${param_idx}")
        params.append(from_date)
        param_idx += 1
    
    if to_date:
        where_clauses.append(f"DATE(uploaded_at) <= ${param_idx}")
        params.append(to_date)
        param_idx += 1

    if status_filter:
        where_clauses.append(f"status = ${param_idx}")
        params.append(status_filter.value)
        param_idx += 1
    
    where_sql = " AND ".join(where_clauses)

    try:
        async with get_db_conn() as db_conn:
            total_docs_query = f"SELECT COUNT(*) FROM documents WHERE {where_sql};"
            total_documents = await db_conn.fetchval(total_docs_query, *params)
            total_documents = total_documents or 0

            total_chunks_processed = 0 # Inicializar
            # Construir la query para total_chunks_processed
            # Esta query debe considerar los filtros existentes Y el status='processed'
            
            # Start with the existing filters
            chunk_where_clauses = list(where_clauses) 
            chunk_params = list(params)
            
            # Add the 'processed' status filter if not already present
            if status_filter:
                if status_filter != DocumentStatus.PROCESSED:
                    # If filtering by a status other than 'processed', then chunks from processed docs is 0
                    total_chunks_processed = 0
                # If status_filter IS 'processed', the condition is already in where_clauses
            else: # No status_filter, so add condition for status = 'processed'
                chunk_where_clauses.append(f"status = ${param_idx}") # Use the next available param index
                chunk_params.append(DocumentStatus.PROCESSED.value)
                # param_idx += 1 # Increment for safety, though not strictly needed if this is the last addition

            # Only run the sum query if we expect there might be processed chunks
            if not (status_filter and status_filter != DocumentStatus.PROCESSED):
                final_chunk_where_sql = " AND ".join(chunk_where_clauses)
                total_chunks_query_sql = f"SELECT SUM(chunk_count) FROM documents WHERE {final_chunk_where_sql};"
                val = await db_conn.fetchval(total_chunks_query_sql, *chunk_params)
                total_chunks_processed = val or 0


            by_status_query = f"SELECT status, COUNT(*) as count FROM documents WHERE {where_sql} GROUP BY status;"
            status_rows = await db_conn.fetch(by_status_query, *params)
            stats_by_status = DocumentStatsByStatus()
            for row in status_rows:
                # Check if the status from DB is a valid member of DocumentStatus enum
                try:
                    status_enum_member = DocumentStatus(row['status'])
                    setattr(stats_by_status, status_enum_member.value, row['count'])
                except ValueError:
                    stats_log.warning("Unknown status value from DB", db_status=row['status'], count=row['count'])
            
            by_type_query = f"SELECT file_type, COUNT(*) as count FROM documents WHERE {where_sql} GROUP BY file_type;"
            type_rows = await db_conn.fetch(by_type_query, *params)
            stats_by_type = DocumentStatsByType()
            
            type_mapping = {
                "application/pdf": "pdf",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
                "application/msword": "docx", 
                "text/plain": "txt",
                "text/markdown": "md", # Corrected mapping
                "text/html": "html",   # Corrected mapping
            }
            for row in type_rows:
                mapped_type_key = row['file_type']
                stat_field = type_mapping.get(mapped_type_key)

                if stat_field and hasattr(stats_by_type, stat_field):
                    current_val = getattr(stats_by_type, stat_field, 0) # Default to 0 if not set
                    setattr(stats_by_type, stat_field, current_val + row['count'])
                else: 
                    stats_by_type.other += row['count']

            dates_query = f"SELECT MIN(uploaded_at) as oldest, MAX(uploaded_at) as newest FROM documents WHERE {where_sql};"
            date_row = await db_conn.fetchrow(dates_query, *params)
            oldest_document_date = date_row['oldest'] if date_row else None
            newest_document_date = date_row['newest'] if date_row else None

            stats_log.info("Successfully retrieved document statistics from DB")

            return DocumentStatsResponse(
                total_documents=total_documents,
                total_chunks_processed=total_chunks_processed, # CORREGIDO EL NOMBRE DEL CAMPO
                by_status=stats_by_status,
                by_type=stats_by_type,
                by_user=[], 
                recent_activity=[], 
                oldest_document_date=oldest_document_date,
                newest_document_date=newest_document_date,
            )

    except asyncpg.exceptions.PostgresConnectionError as db_conn_err:
        stats_log.error("Database connection error while fetching statistics", error=str(db_conn_err), exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database connection error.")
    except Exception as e:
        stats_log.exception("Unexpected error fetching document statistics", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error.")


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
    company_id = request.headers.get("X-Company-ID")
    req_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
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

    for doc_id_str in document_ids:
        single_delete_log = log.bind(request_id=req_id, current_doc_id_bulk=doc_id_str, company_id=company_id)
        try:
            doc_uuid = uuid.UUID(doc_id_str)
        except ValueError:
            single_delete_log.warning("Invalid document ID format in bulk list.")
            failed.append({"id": doc_id_str, "error": "ID inválido"})
            continue
        
        doc_data = None
        try:
            async with get_db_conn() as conn:
                doc_data = await api_db_retry_strategy(db_client.get_document_by_id)(
                    conn, doc_id=doc_uuid, company_id=company_uuid
                )
            if not doc_data:
                single_delete_log.warning("Document not found in DB for this company during bulk delete.")
                failed.append({"id": doc_id_str, "error": "No encontrado"})
                continue

            errors_for_this_doc: List[str] = []
            loop = asyncio.get_running_loop()
            
            single_delete_log.debug("Bulk: Attempting Milvus delete for document.")
            try:
                milvus_deleted_ok = await loop.run_in_executor(None, _delete_milvus_sync, str(doc_uuid), company_id)
                if not milvus_deleted_ok:
                    errors_for_this_doc.append("Milvus (check logs)")
                    single_delete_log.warning("Bulk: Milvus delete for document reported failure or collection missing.")
                else:
                    single_delete_log.info("Bulk: Milvus delete for document successful or no data to delete.")
            except Exception as e_milvus:
                single_delete_log.exception("Bulk: Unexpected error during Milvus delete execution for document.", error=str(e_milvus))
                errors_for_this_doc.append(f"Milvus: {type(e_milvus).__name__}")

            gcs_path = doc_data.get('file_path')
            if gcs_path:
                single_delete_log.debug("Bulk: Attempting GCS delete for document.", gcs_path=gcs_path)
                try:
                    await gcs_client.delete_file_async(gcs_path)
                    single_delete_log.info("Bulk: GCS delete for document successful.")
                except Exception as e_gcs:
                    single_delete_log.exception("Bulk: GCS delete for document failed.", error=str(e_gcs))
                    errors_for_this_doc.append(f"GCS: {type(e_gcs).__name__}")
            else:
                single_delete_log.warning("Bulk: GCS path unknown for document, skipping GCS delete.")

            single_delete_log.debug("Bulk: Attempting PostgreSQL delete for document.")
            try:
                async with get_db_conn() as conn:
                    deleted_in_db = await api_db_retry_strategy(db_client.delete_document)(
                        conn=conn, doc_id=doc_uuid, company_id=company_uuid
                    )
                if not deleted_in_db:
                    single_delete_log.warning("Bulk: PostgreSQL delete for document returned false (already gone or company mismatch).")
                else:
                    single_delete_log.info("Bulk: PostgreSQL delete for document successful.")
            except Exception as e_db:
                single_delete_log.exception("Bulk: PostgreSQL delete for document failed.", error=str(e_db))
                errors_for_this_doc.append(f"DB: {type(e_db).__name__}")

            if errors_for_this_doc:
                failed.append({"id": doc_id_str, "error": ", ".join(errors_for_this_doc)})
            else:
                deleted.append(doc_id_str)
                single_delete_log.info("Bulk: Document deleted successfully from all stores.")

        except Exception as e_outer:
            single_delete_log.exception("Bulk: Outer exception processing document.", error=str(e_outer))
            failed.append({"id": doc_id_str, "error": f"Error general: {type(e_outer).__name__}"})
    
    log.info("Bulk delete operation completed.", num_requested=len(document_ids), num_deleted=len(deleted), num_failed=len(failed))
    return {"deleted": deleted, "failed": failed}

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
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB or GCS)"},
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
    except HTTPException as http_exc: 
        raise http_exc
    except Exception as e: 
        endpoint_log.exception("Unexpected error checking for duplicate document", error=str(e))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error during duplicate check.")


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
        if not isinstance(e, HTTPException):
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database error creating record.")
        raise

    try:
        file_content = await file.read()
        endpoint_log.info("Preparing upload to GCS", object_name=file_path_in_storage, filename=normalized_filename, size=len(file_content), content_type=file.content_type)
        
        await gcs_client.upload_file_async(
            object_name=file_path_in_storage, data=file_content, content_type=file.content_type
        )
        endpoint_log.info("File uploaded successfully to GCS", object_name=file_path_in_storage)
        
        file_exists = await gcs_client.check_file_exists_async(file_path_in_storage)
        endpoint_log.info("GCS existence check after upload", object_name=file_path_in_storage, exists=file_exists)

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

    except GCSClientError as gce:
        endpoint_log.error("Failed to upload file to GCS", object_name=file_path_in_storage, error=str(gce))
        try:
            async with get_db_conn() as conn_err:
                await api_db_retry_strategy(db_client.update_document_status)(
                    document_id=document_id, status=DocumentStatus.ERROR, error_message=f"GCS upload failed: {str(gce)[:200]}", conn=conn_err
                )
        except Exception as db_err_gcs:
            endpoint_log.exception("Failed to update status to ERROR after GCS failure", error=str(db_err_gcs))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Storage service error: {gce}")
    except Exception as e:
         endpoint_log.exception("Unexpected error during file upload or DB update", error=str(e))
         try:
             async with get_db_conn() as conn_err_unexp:
                 await api_db_retry_strategy(db_client.update_document_status)(
                     document_id=document_id, status=DocumentStatus.ERROR, error_message=f"Unexpected upload error: {type(e).__name__}", conn=conn_err_unexp
                 )
         except Exception as db_err_unexp:
             endpoint_log.exception("Failed to update status to ERROR after unexpected upload failure", error=str(db_err_unexp))
         if not isinstance(e, HTTPException):
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Internal server error during upload.")
         raise 
    finally:
        await file.close()

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
            async with get_db_conn() as conn_celery_err:
                await api_db_retry_strategy(db_client.update_document_status)(
                    document_id=document_id, status=DocumentStatus.ERROR, error_message=f"Failed to queue processing task: {e}", conn=conn_celery_err
                )
        except Exception as db_err_celery:
            endpoint_log.exception("Failed to update status to ERROR after Celery failure", error=str(db_err_celery))
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
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB, GCS, Milvus)"},
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
    gcs_client: GCSClient = Depends(get_gcs_client),
):
    company_id = request.headers.get("X-Company-ID")
    req_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
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

    updated_at = doc_data.get('updated_at')
    now_utc = datetime.now(timezone.utc)
    updated_at_dt = None
    if updated_at:
        if isinstance(updated_at, datetime): updated_at_dt = updated_at.astimezone(timezone.utc)
        else:
            try: updated_at_dt = datetime.fromisoformat(str(updated_at).replace('Z', '+00:00'))
            except Exception: updated_at_dt = None

    GRACE_PERIOD_SECONDS = 120

    gcs_path = doc_data.get('file_path')
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
            gcs_exists = False 
            if updated_status_enum != DocumentStatus.ERROR:
                if not (updated_status_enum == DocumentStatus.PROCESSED and updated_at_dt and (now_utc - updated_at_dt).total_seconds() < GRACE_PERIOD_SECONDS):
                    needs_update = True
                    updated_status_enum = DocumentStatus.ERROR
                    updated_error_message = (updated_error_message or "") + f" GCS check error ({type(gcs_e).__name__})."
                    updated_chunk_count = 0 
                else:
                     status_log.warning("Grace period: no status change for GCS exception (recent processed)")

    status_log.debug("Checking Milvus for chunk count using pymilvus helper...")
    loop = asyncio.get_running_loop()
    milvus_chunk_count = -1 
    try:
        milvus_chunk_count = await loop.run_in_executor(
            None, _get_milvus_chunk_count_sync, str(document_id), company_id
        )
        status_log.info("Milvus chunk count check complete (pymilvus)", count=milvus_chunk_count)

        if milvus_chunk_count == -1: 
            status_log.error("Milvus count check failed or collection not accessible (returned -1).")
            if updated_status_enum != DocumentStatus.ERROR:
                if updated_status_enum == DocumentStatus.PROCESSED:
                     if not (updated_at_dt and (now_utc - updated_at_dt).total_seconds() < GRACE_PERIOD_SECONDS):
                        needs_update = True
                        updated_status_enum = DocumentStatus.ERROR
                        updated_error_message = (updated_error_message or "") + " Milvus check failed or processed data missing."
                     else:
                         status_log.warning("Grace period: no status change for failed Milvus count check (recent processed)")
                else:
                     status_log.info("Milvus check failed, but status is not 'processed'. No status change needed yet.", current_status=updated_status_enum.value)
        
        elif milvus_chunk_count > 0: 
            if gcs_exists and updated_status_enum in [DocumentStatus.ERROR, DocumentStatus.UPLOADED, DocumentStatus.PROCESSING]:
                status_log.warning("Inconsistency: Chunks found in Milvus and GCS file exists, but DB status is not 'processed'. Correcting.")
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
                status_log.warning("Inconsistency: DB status 'processed' but no chunks found in Milvus. Correcting.")
                if not (updated_at_dt and (now_utc - updated_at_dt).total_seconds() < GRACE_PERIOD_SECONDS):
                    needs_update = True
                    updated_status_enum = DocumentStatus.ERROR
                    updated_chunk_count = 0
                    updated_error_message = (updated_error_message or "") + " Processed data missing from Milvus."
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
                           new_status=updated_status_enum.value, new_count=updated_chunk_count, new_error=updated_error_message)
        try:
             async with get_db_conn() as conn_update:
                 await api_db_retry_strategy(db_client.update_document_status)(
                     document_id=document_id, status=updated_status_enum,
                     chunk_count=updated_chunk_count, error_message=updated_error_message,
                     conn=conn_update
                 )
             status_log.info("Document status updated successfully in DB after inconsistency check.")
             doc_data['status'] = updated_status_enum.value
             doc_data['chunk_count'] = updated_chunk_count
             doc_data['error_message'] = updated_error_message
        except Exception as e_db_update:
            status_log.exception("Failed to update document status in DB after inconsistency check", error=str(e_db_update))

    final_status_val = doc_data['status']
    final_chunk_count_val = doc_data.get('chunk_count', 0 if doc_data['status'] != DocumentStatus.PROCESSED.value else None) 
    if doc_data['status'] == DocumentStatus.PROCESSED.value and final_chunk_count_val is None : final_chunk_count_val = 0 
    final_error_message_val = doc_data.get('error_message')

    return StatusResponse(
        document_id=str(doc_data['id']), company_id=str(doc_data['company_id']),
        status=final_status_val, file_name=doc_data.get('file_name'),
        file_type=doc_data.get('file_type'), file_path=doc_data.get('file_path'),
        chunk_count=final_chunk_count_val,
        gcs_exists=gcs_exists,
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
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB, GCS, Milvus)"},
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
    gcs_client: GCSClient = Depends(get_gcs_client),
):
    company_id = request.headers.get("X-Company-ID")
    req_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
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

        gcs_path_db = doc_db_data.get('file_path')
        live_gcs_exists = False
        if gcs_path_db:
            try:
                live_gcs_exists = await gcs_client.check_file_exists_async(gcs_path_db)
                if not live_gcs_exists and doc_updated_status_enum not in [DocumentStatus.ERROR, DocumentStatus.PENDING]:
                    doc_needs_update = True
                    doc_updated_status_enum = DocumentStatus.ERROR
                    doc_updated_error_msg = "File missing from storage."
                    doc_updated_chunk_count = 0 
            except Exception as e_gcs_list:
                check_log.error("GCS check failed for list item", object_name=gcs_path_db, error=str(e_gcs_list))
                live_gcs_exists = False 
                if doc_updated_status_enum != DocumentStatus.ERROR:
                    doc_needs_update = True
                    doc_updated_status_enum = DocumentStatus.ERROR
                    doc_updated_error_msg = (doc_updated_error_msg or "").strip() + " GCS check error."
                    doc_updated_chunk_count = 0
        else: 
            live_gcs_exists = False 
            if doc_updated_status_enum not in [DocumentStatus.ERROR, DocumentStatus.PENDING]:
                 doc_needs_update = True
                 doc_updated_status_enum = DocumentStatus.ERROR
                 doc_updated_error_msg = (doc_updated_error_msg or "").strip() + " File path missing."
                 doc_updated_chunk_count = 0


        live_milvus_chunk_count = -1
        loop = asyncio.get_running_loop()
        try:
            live_milvus_chunk_count = await loop.run_in_executor(None, _get_milvus_chunk_count_sync, doc_id_str, company_id)
            
            if live_milvus_chunk_count == -1: 
                if doc_updated_status_enum == DocumentStatus.PROCESSED: 
                    doc_needs_update = True
                    doc_updated_status_enum = DocumentStatus.ERROR
                    doc_updated_error_msg = (doc_updated_error_msg or "").strip() + " Milvus check failed/processed data missing."
            elif live_milvus_chunk_count > 0: 
                if live_gcs_exists and doc_updated_status_enum in [DocumentStatus.ERROR, DocumentStatus.UPLOADED, DocumentStatus.PROCESSING]:
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
                    doc_updated_error_msg = (doc_updated_error_msg or "").strip() + " Processed data missing from Milvus."
                elif doc_updated_status_enum == DocumentStatus.ERROR and doc_updated_chunk_count != 0: 
                    doc_needs_update = True
                    doc_updated_chunk_count = 0
        except Exception as e_milvus_list:
            check_log.exception("Unexpected error during Milvus count check for list item", error=str(e_milvus_list))
            live_milvus_chunk_count = -1
            if doc_updated_status_enum != DocumentStatus.ERROR: 
                doc_needs_update = True
                doc_updated_status_enum = DocumentStatus.ERROR
                doc_updated_error_msg = (doc_updated_error_msg or "").strip() + " Error checking Milvus."

        if doc_updated_status_enum != DocumentStatus.ERROR and doc_current_status_enum == DocumentStatus.ERROR:
            doc_updated_error_msg = None
        elif doc_updated_status_enum == DocumentStatus.ERROR and not doc_updated_error_msg: 
            doc_updated_error_msg = "Error detected during status check."


        return {
            "db_data": doc_db_data,
            "needs_update": doc_needs_update,
            "updated_status_enum": doc_updated_status_enum,
            "updated_chunk_count": doc_updated_chunk_count,
            "final_error_message": doc_updated_error_msg.strip() if doc_updated_error_msg else None,
            "live_gcs_exists": live_gcs_exists,
            "live_milvus_chunk_count": live_milvus_chunk_count
        }

    list_log.info(f"Performing live checks for {len(documents_db)} documents concurrently...")
    check_tasks = [check_single_document(doc) for doc in documents_db]
    check_results = await asyncio.gather(*check_tasks, return_exceptions=True) 
    list_log.info("Live checks completed for list items.")

    updated_doc_data_map = {} 
    docs_to_update_in_db = []
    
    processed_results = []
    for i, result_or_exc in enumerate(check_results):
        original_doc_data = documents_db[i]
        if isinstance(result_or_exc, Exception):
            list_log.error("Error processing single document check in gather", doc_id=str(original_doc_data['id']), error=str(result_or_exc), exc_info=result_or_exc)
            processed_results.append({
                "db_data": original_doc_data, "needs_update": False, 
                "updated_status_enum": DocumentStatus(original_doc_data['status']),
                "updated_chunk_count": original_doc_data.get('chunk_count'),
                "final_error_message": original_doc_data.get('error_message', "Error during status refresh"),
                "live_gcs_exists": False, "live_milvus_chunk_count": -1 
            })
            continue
        processed_results.append(result_or_exc)


    for result in processed_results:
        doc_id_str = str(result["db_data"]["id"])
        updated_doc_data_map[doc_id_str] = {
            **result["db_data"], 
            "status": result["updated_status_enum"].value,
            "chunk_count": result["updated_chunk_count"], 
            "error_message": result["final_error_message"]
        }
        if result["needs_update"]:
            docs_to_update_in_db.append({
                "id": result["db_data"]["id"], 
                "status_enum": result["updated_status_enum"],
                "chunk_count": result["updated_chunk_count"], 
                "error_message": result["final_error_message"]
            })

    if docs_to_update_in_db:
        list_log.warning("Updating statuses sequentially in DB for inconsistent documents", count=len(docs_to_update_in_db))
        updated_count_db = 0; failed_update_count_db = 0
        try:
             async with get_db_conn() as conn_batch_update: 
                 for update_info in docs_to_update_in_db:
                     try:
                         await api_db_retry_strategy(db_client.update_document_status)(
                             conn=conn_batch_update, 
                             document_id=update_info["id"], 
                             status=update_info["status_enum"],
                             chunk_count=update_info["chunk_count"], 
                             error_message=update_info["error_message"]
                         )
                         updated_count_db += 1
                     except Exception as single_update_err:
                         failed_update_count_db += 1
                         list_log.error("Failed DB update for single doc during list check", document_id=str(update_info["id"]), error=str(single_update_err))
             list_log.info("Finished sequential DB updates for list items.", updated=updated_count_db, failed=failed_update_count_db)
        except Exception as bulk_db_conn_err: 
            list_log.exception("Error acquiring DB connection for sequential updates in list", error=str(bulk_db_conn_err))

    final_response_items = []
    for result in processed_results: 
         doc_id_str = str(result["db_data"]["id"])
         current_data_for_response = updated_doc_data_map.get(doc_id_str, result["db_data"])
         
         final_chunk_count_resp = current_data_for_response.get('chunk_count')
         if current_data_for_response['status'] != DocumentStatus.PROCESSED.value:
             final_chunk_count_resp = 0
         elif final_chunk_count_resp is None: 
             final_chunk_count_resp = 0


         final_response_items.append(StatusResponse(
            document_id=doc_id_str, 
            company_id=str(current_data_for_response['company_id']),
            status=current_data_for_response['status'], 
            file_name=current_data_for_response.get('file_name'),
            file_type=current_data_for_response.get('file_type'), 
            file_path=current_data_for_response.get('file_path'),
            chunk_count=final_chunk_count_resp,
            gcs_exists=result["live_gcs_exists"], 
            milvus_chunk_count=result["live_milvus_chunk_count"], 
            last_updated=current_data_for_response.get('updated_at'),
            uploaded_at=current_data_for_response.get('uploaded_at'), 
            error_message=current_data_for_response.get('error_message'),
            metadata=current_data_for_response.get('metadata')
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
    company_id = request.headers.get("X-Company-ID")
    user_id = request.headers.get("X-User-ID") 
    req_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
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
        503: {"model": ErrorDetail, "description": "Service Unavailable (DB, GCS, Milvus)"},
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
    gcs_client: GCSClient = Depends(get_gcs_client),
):
    company_id = request.headers.get("X-Company-ID")
    req_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
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

    errors: List[str] = []

    delete_log.info("Attempting to delete chunks from Milvus (pymilvus)...")
    loop = asyncio.get_running_loop()
    try:
        milvus_deleted_ok = await loop.run_in_executor(None, _delete_milvus_sync, str(document_id), company_id)
        if milvus_deleted_ok:
            delete_log.info("Milvus delete operation completed or collection not found (pymilvus helper).")
        else:
            errors.append("Failed Milvus delete (check helper logs)")
            delete_log.warning("Milvus delete operation reported failure.")
    except Exception as e:
        delete_log.exception("Unexpected error during Milvus delete execution via helper", error=str(e))
        errors.append(f"Milvus delete exception via helper: {type(e).__name__}")

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

    delete_log.info("Attempting to delete record from PostgreSQL...")
    try:
         async with get_db_conn() as conn:
            deleted_in_db = await api_db_retry_strategy(db_client.delete_document)(
                conn=conn, doc_id=document_id, company_id=company_uuid
            )
            if deleted_in_db: delete_log.info("Document record deleted successfully from PostgreSQL")
            else: delete_log.warning("PostgreSQL delete command executed but no record was deleted (already gone?).") 
    except Exception as e:
        delete_log.exception("CRITICAL: Failed to delete document record from PostgreSQL", error=str(e))
        error_detail = f"Deleted/Attempted delete from storage/vectors (errors: {', '.join(errors)}) but FAILED to delete DB record: {e}"
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=error_detail)

    if errors: delete_log.warning("Document deletion process completed with non-critical errors", errors=errors)

    delete_log.info("Document deletion process finished.")
    # Return None for 204 No Content
```

## File: `app\api\v1\schemas.py`
```py
# ingest-service/app/api/v1/schemas.py
import uuid
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Dict, Any, List
from app.models.domain import DocumentStatus
from datetime import datetime, date # Asegurar que date está importado
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

    gcs_exists: Optional[bool] = Field(None, description="Indicates if the original file currently exists in GCS.")
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
                "gcs_exists": True,
                "milvus_chunk_count": 0,
                "message": "El procesamiento falló: Timeout."
            }
        }

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
                        "gcs_exists": True,
                        "milvus_chunk_count": 0,
                        "message": "El procesamiento falló: Timeout."
                    }
                ],
                "total": 1,
                "limit": 30,
                "offset": 0
            }
        }

# --- NUEVOS SCHEMAS PARA ESTADÍSTICAS DE DOCUMENTOS ---
class DocumentStatsByStatus(BaseModel):
    processed: int = 0
    processing: int = 0
    uploaded: int = 0
    error: int = 0
    pending: int = 0 # Añadir pending ya que es un DocumentStatus

class DocumentStatsByType(BaseModel):
    pdf: int = Field(0, alias="application/pdf")
    docx: int = Field(0, alias="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    doc: int = Field(0, alias="application/msword")
    txt: int = Field(0, alias="text/plain")
    md: int = Field(0, alias="text/markdown")
    html: int = Field(0, alias="text/html")
    other: int = 0

    class Config:
        populate_by_name = True


class DocumentStatsByUser(BaseModel):
    # Esta parte es más compleja de implementar en ingest-service si no tiene acceso a la tabla de users
    # Por ahora, la definición del schema está aquí, pero su implementación podría ser básica o diferida.
    user_id: str # Podría ser UUID del user que subió el doc, pero no está en la tabla 'documents'
    name: Optional[str] = None
    count: int

class DocumentRecentActivity(BaseModel):
    # Similar, agrupar por fecha y estado es una query más compleja.
    # Definición aquí, implementación podría ser básica.
    date: date
    uploaded: int = 0
    processing: int = 0
    processed: int = 0
    error: int = 0
    pending: int = 0

class DocumentStatsResponse(BaseModel):
    total_documents: int
    total_chunks_processed: int # Suma de chunk_count para documentos en estado 'processed'
    by_status: DocumentStatsByStatus
    by_type: DocumentStatsByType
    # by_user: List[DocumentStatsByUser] # Omitir por ahora para simplificar
    # recent_activity: List[DocumentRecentActivity] # Omitir por ahora para simplificar
    oldest_document_date: Optional[datetime] = None
    newest_document_date: Optional[datetime] = None

    model_config = { # Anteriormente Config
        "from_attributes": True # Anteriormente orm_mode
    }
```

## File: `app\core\__init__.py`
```py

```

## File: `app\core\config.py`
```py
# ingest-service/app/core/config.py
# LLM: NO COMMENTS unless absolutely necessary for processing logic.
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
from urllib.parse import urlparse

# --- Service Names en K8s ---
POSTGRES_K8S_SVC = "postgresql-service.nyro-develop.svc.cluster.local"
# MILVUS_K8S_SVC = "milvus-standalone.nyro-develop.svc.cluster.local" # No longer default for URI
REDIS_K8S_SVC = "redis-service-master.nyro-develop.svc.cluster.local"
EMBEDDING_SERVICE_K8S_SVC = "embedding-service.nyro-develop.svc.cluster.local"
DOCPROC_SERVICE_K8S_SVC = "docproc-service.nyro-develop.svc.cluster.local"

# --- Defaults ---
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex"
POSTGRES_K8S_USER_DEFAULT = "postgres"
# MILVUS_K8S_PORT_DEFAULT = 19530 # No longer default for URI
ZILLIZ_ENDPOINT_DEFAULT = "https://in03-0afab716eb46d7f.serverless.gcp-us-west1.cloud.zilliz.com"
# DEFAULT_MILVUS_URI = f"http://{MILVUS_K8S_SVC}:{MILVUS_K8S_PORT_DEFAULT}" # Replaced by ZILLIZ_ENDPOINT_DEFAULT
MILVUS_DEFAULT_COLLECTION = "atenex_collection"
MILVUS_DEFAULT_INDEX_PARAMS = '{"metric_type": "IP", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 256}}'
MILVUS_DEFAULT_SEARCH_PARAMS = '{"metric_type": "IP", "params": {"ef": 128}}'
DEFAULT_EMBEDDING_DIM = 384
DEFAULT_TIKTOKEN_ENCODING = "cl100k_base"

DEFAULT_EMBEDDING_SERVICE_URL = f"http://{EMBEDDING_SERVICE_K8S_SVC}:80/api/v1/embed"
DEFAULT_DOCPROC_SERVICE_URL = f"http://{DOCPROC_SERVICE_K8S_SVC}:80/api/v1/process"


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

    ZILLIZ_API_KEY: SecretStr = Field(description="API Key for Zilliz Cloud connection.")
    MILVUS_URI: str = Field(
        default=ZILLIZ_ENDPOINT_DEFAULT,
        description="Milvus connection URI (Zilliz Cloud HTTPS endpoint)."
    )
    MILVUS_COLLECTION_NAME: str = MILVUS_DEFAULT_COLLECTION
    MILVUS_GRPC_TIMEOUT: int = 10 # Default timeout, can be adjusted via ENV
    MILVUS_CONTENT_FIELD: str = "content"
    MILVUS_EMBEDDING_FIELD: str = "embedding"
    MILVUS_CONTENT_FIELD_MAX_LENGTH: int = 20000
    MILVUS_INDEX_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_INDEX_PARAMS))
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_SEARCH_PARAMS))

    GCS_BUCKET_NAME: str = Field(default="atenex", description="Name of the Google Cloud Storage bucket for storing original files.")

    EMBEDDING_DIMENSION: int = Field(default=DEFAULT_EMBEDDING_DIM, description="Dimension of embeddings expected from the embedding service, used for Milvus schema.")
    EMBEDDING_SERVICE_URL: AnyHttpUrl = Field(default_factory=lambda: AnyHttpUrl(DEFAULT_EMBEDDING_SERVICE_URL), description="URL of the external embedding service.")
    DOCPROC_SERVICE_URL: AnyHttpUrl = Field(default_factory=lambda: AnyHttpUrl(DEFAULT_DOCPROC_SERVICE_URL), description="URL of the external document processing service.")

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
        "text/html",        
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", # XLSX
        "application/vnd.ms-excel"
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
             raise ValueError(f"Required secret field '{field_name}' (mapped from INGEST_{field_name.upper()}) cannot be empty.")
        return v

    @field_validator('MILVUS_URI', mode='before')
    @classmethod
    def validate_milvus_uri(cls, v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError("MILVUS_URI must be a string.")
        v_strip = v.strip()
        if not v_strip.startswith("https://"): 
            raise ValueError(f"Invalid Zilliz URI: Must start with https://. Received: '{v_strip}'")
        try:
            parsed = urlparse(v_strip)
            if not parsed.hostname:
                raise ValueError(f"Invalid URI: Missing hostname in '{v_strip}'")
            return v_strip 
        except Exception as e:
            raise ValueError(f"Invalid Milvus URI format '{v_strip}': {e}") from e

    @field_validator('ZILLIZ_API_KEY', mode='before')
    @classmethod
    def check_zilliz_key(cls, v: Any, info: ValidationInfo) -> Any:
         if v is None or v == "" or (isinstance(v, SecretStr) and not v.get_secret_value()):
            raise ValueError(f"Required secret field for Zilliz (expected as INGEST_ZILLIZ_API_KEY) cannot be empty.")
         return v

    @field_validator('EMBEDDING_SERVICE_URL', 'DOCPROC_SERVICE_URL', mode='before')
    @classmethod
    def assemble_service_url(cls, v: Optional[str], info: ValidationInfo) -> str:
        default_map = {
            "EMBEDDING_SERVICE_URL": DEFAULT_EMBEDDING_SERVICE_URL,
            "DOCPROC_SERVICE_URL": DEFAULT_DOCPROC_SERVICE_URL
        }
        default_url_key = str(info.field_name) 
        default_url = default_map.get(default_url_key, "")

        url_to_validate = v if v is not None else default_url 
        if not url_to_validate:
            raise ValueError(f"URL for {default_url_key} cannot be empty and no default is set.")

        try:
            validated_url = AnyHttpUrl(url_to_validate)
            return str(validated_url)
        except ValidationError as ve:
             raise ValueError(f"Invalid URL format for {default_url_key}: {ve}") from ve


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
    pg_pass_status = '*** SET ***' if settings.POSTGRES_PASSWORD and settings.POSTGRES_PASSWORD.get_secret_value() else '!!! NOT SET !!!'
    temp_log.info(f"  POSTGRES_PASSWORD:            {pg_pass_status}")
    temp_log.info(f"  MILVUS_URI (for Pymilvus):    {settings.MILVUS_URI}")
    zilliz_api_key_status = '*** SET ***' if settings.ZILLIZ_API_KEY and settings.ZILLIZ_API_KEY.get_secret_value() else '!!! NOT SET !!!'
    temp_log.info(f"  ZILLIZ_API_KEY:               {zilliz_api_key_status}")
    temp_log.info(f"  MILVUS_COLLECTION_NAME:       {settings.MILVUS_COLLECTION_NAME}")
    temp_log.info(f"  MILVUS_GRPC_TIMEOUT:          {settings.MILVUS_GRPC_TIMEOUT}")
    temp_log.info(f"  GCS_BUCKET_NAME:              {settings.GCS_BUCKET_NAME}")
    temp_log.info(f"  EMBEDDING_DIMENSION (Milvus): {settings.EMBEDDING_DIMENSION}")
    temp_log.info(f"  INGEST_EMBEDDING_SERVICE_URL (from env INGEST_EMBEDDING_SERVICE_URL): {settings.EMBEDDING_SERVICE_URL}")
    temp_log.info(f"  INGEST_DOCPROC_SERVICE_URL (from env INGEST_DOCPROC_SERVICE_URL):   {settings.DOCPROC_SERVICE_URL}")
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

# 0.3.1 version
# jfu 2
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
        effective_base_url = base_url or str(settings.DOCPROC_SERVICE_URL).rstrip('/')
        
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
            parsed_service_url = httpx.URL(str(settings.DOCPROC_SERVICE_URL))
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
from app.services.base_client import BaseServiceClient 

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
        effective_base_url = base_url or str(settings.EMBEDDING_SERVICE_URL).rstrip('/')
        
        parsed_url = httpx.URL(effective_base_url)
        self.service_endpoint_path = parsed_url.path 
        client_base_url = f"{parsed_url.scheme}://{parsed_url.host}:{parsed_url.port}" if parsed_url.port else f"{parsed_url.scheme}://{parsed_url.host}"

        super().__init__(base_url=client_base_url, service_name="EmbeddingService")
        self.log = log.bind(service_client="EmbeddingServiceClient", service_url=effective_base_url)

    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=1, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
        before_sleep=before_sleep_log(log, logging.WARNING) 
    )
    async def get_embeddings(self, texts: List[str], text_type: str = "passage") -> Tuple[List[List[float]], Dict[str, Any]]:
        """
        Sends a list of texts to the Embedding Service and returns their embeddings.

        Args:
            texts: A list of strings to embed.
            text_type: The type of text, e.g., "passage" or "query". Defaults to "passage".

        Returns:
            A tuple containing:
                - A list of embeddings (list of lists of floats).
                - ModelInfo dictionary.

        Raises:
            EmbeddingServiceClientError: If the request fails or the service returns an error.
        """
        if not texts:
            self.log.warning("get_embeddings called with an empty list of texts.")
            return [], {"model_name": "unknown", "dimension": 0, "provider": "unknown"}

        request_payload = {
            "texts": texts,
            "text_type": text_type 
        }
        self.log.debug(f"Requesting embeddings for {len(texts)} texts from {self.service_endpoint_path}", num_texts=len(texts), text_type=text_type)

        try:
            response = await self._request(
                method="POST",
                endpoint=self.service_endpoint_path, 
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
            self.log.info(f"Successfully retrieved {len(embeddings)} embeddings.", model_name=model_info.get("model_name"), provider=model_info.get("provider"))
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
        health_log = self.log.bind(action="health_check")
        try:
            parsed_service_url = httpx.URL(str(settings.EMBEDDING_SERVICE_URL)) 
            health_endpoint_url_base = f"{parsed_service_url.scheme}://{parsed_service_url.host}"
            if parsed_service_url.port:
                health_endpoint_url_base += f":{parsed_service_url.port}"
            
            async with httpx.AsyncClient(base_url=health_endpoint_url_base, timeout=5) as client:
                response = await client.get("/health")
            response.raise_for_status()
            health_data = response.json()
            
            model_is_ready = health_data.get("model_status") in ["client_ready", "loaded"] 
            if health_data.get("status") == "ok" and model_is_ready:
                health_log.info("Embedding Service is healthy and model is loaded/ready.", health_data=health_data)
                return True
            else:
                health_log.warning("Embedding Service reported unhealthy or model not ready.", health_data=health_data)
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
import json
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
        connect_log.info("Connecting to Milvus (Zilliz) for pipeline worker indexing...", uri=uri)
        try:
            connections.connect(
                alias=alias,
                uri=uri,
                timeout=settings.MILVUS_GRPC_TIMEOUT,
                token=settings.ZILLIZ_API_KEY.get_secret_value() if settings.ZILLIZ_API_KEY else None
            )
            connect_log.info("Connected to Milvus (Zilliz) for pipeline worker indexing.")
        except MilvusException as e:
            connect_log.error("Failed to connect to Milvus (Zilliz) for pipeline worker indexing.", error=str(e))
            raise ConnectionError(f"Milvus (Zilliz) connection failed: {e}") from e
        except Exception as e:
            connect_log.error("Unexpected error connecting to Milvus (Zilliz) for pipeline worker indexing.", error=str(e))
            raise ConnectionError(f"Unexpected Milvus (Zilliz) connection error: {e}") from e

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
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, before_sleep_log
import logging 

logging.basicConfig(
    stream=sys.stdout, 
    level=logging.DEBUG, 
    format='%(asctime)s - %(name)s - %(levelname)s - [StdLib] - %(message)s',
    force=True 
)
stdlib_task_logger = logging.getLogger("app.tasks.process_document.stdlib")


def normalize_filename(filename: str) -> str:
    return " ".join(filename.strip().split())


from app.core.config import settings
from app.db.postgres_client import get_sync_engine, set_status_sync, bulk_insert_chunks_sync
from app.models.domain import DocumentStatus
from app.services.gcs_client import GCSClient, GCSClientError
from app.services.ingest_pipeline import (
    index_chunks_in_milvus_and_prepare_for_pg,
    delete_milvus_chunks
)
from app.tasks.celery_app import celery_app

task_struct_log = structlog.get_logger(__name__) 
IS_WORKER = "worker" in sys.argv

sync_engine: Optional[Engine] = None
gcs_client_global: Optional[GCSClient] = None

sync_http_retry_strategy = retry(
    stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES +1),
    wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=1, max=10),
    retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
    before_sleep=before_sleep_log(task_struct_log, logging.WARNING), 
    reraise=True
)


@worker_process_init.connect(weak=False)
def init_worker_resources(**kwargs):
    global sync_engine, gcs_client_global
    
    init_log_struct = structlog.get_logger("app.tasks.worker_init.struct")
    init_log_std = logging.getLogger("app.tasks.worker_init.std")
    
    init_log_struct.info("Worker process initializing resources (structlog)...", signal="worker_process_init")
    init_log_std.info("Worker process initializing resources (std)...")
    try:
        if sync_engine is None:
            sync_engine = get_sync_engine()
            init_log_struct.info("Synchronous DB engine initialized for worker (structlog).")
            init_log_std.info("Synchronous DB engine initialized for worker (std).")
        else:
             init_log_struct.info("Synchronous DB engine already initialized for worker (structlog).")
             init_log_std.info("Synchronous DB engine already initialized for worker (std).")

        if gcs_client_global is None:
            gcs_client_global = GCSClient() 
            init_log_struct.info("GCS client initialized for worker (structlog).")
            init_log_std.info("GCS client initialized for worker (std).")
        else:
            init_log_struct.info("GCS client already initialized for worker (structlog).")
            init_log_std.info("GCS client already initialized for worker (std).")
        
        init_log_std.info("Worker resources initialization complete (std).")

    except Exception as e:
        init_log_struct.critical("CRITICAL FAILURE during worker resource initialization (structlog)!", error=str(e), exc_info=True)
        init_log_std.critical(f"CRITICAL FAILURE during worker resource initialization (std)! Error: {str(e)}", exc_info=True)
        print(f"CRITICAL WORKER INIT FAILURE (print): {e}", file=sys.stderr, flush=True)
        sync_engine = None
        gcs_client_global = None


@celery_app.task(
    bind=True,
    name="ingest.process_document",
    autoretry_for=(
        httpx.RequestError, 
        httpx.HTTPStatusError, 
        GCSClientError, 
        ConnectionRefusedError,
        Exception 
    ),
    exclude=(
        Reject, Ignore, ValueError, ConnectionError, RuntimeError, TypeError,
    ),
    retry_backoff=True,
    retry_backoff_max=600, 
    retry_jitter=True,
    max_retries=5, 
    acks_late=True
)
def process_document_standalone(self: Task, *args, **kwargs) -> Dict[str, Any]:
    sys.stdout.flush()
    sys.stderr.flush()

    early_task_id = str(self.request.id or uuid.uuid4()) 
    stdlib_task_logger.info(f"--- TASK ENTRY ID: {early_task_id} --- RAW KWARGS: {kwargs}")
    print(f"--- PRINT TASK ENTRY ID: {early_task_id} ---", flush=True)


    document_id_str = kwargs.get('document_id')
    company_id_str = kwargs.get('company_id')
    filename = kwargs.get('filename')
    content_type = kwargs.get('content_type')
    
    attempt = self.request.retries + 1
    max_attempts = (self.max_retries or 0) + 1
    
    log_context = {
        "task_id": early_task_id, "attempt": f"{attempt}/{max_attempts}", "doc_id": document_id_str,
        "company_id": company_id_str, "filename": filename, "content_type": content_type
    }
    log = structlog.get_logger("app.tasks.process_document.task_exec").bind(**log_context)
    
    log.info("Structlog: Payload parsed, task instance bound.")
    stdlib_task_logger.info(f"StdLib: Starting document processing task for doc_id: {document_id_str}, task_id: {early_task_id}, attempt: {attempt}/{max_attempts}")


    if not IS_WORKER:
         err_msg_not_worker = "Task function called outside of a worker context! Rejecting."
         log.critical(err_msg_not_worker)
         stdlib_task_logger.critical(err_msg_not_worker)
         raise Reject(err_msg_not_worker, requeue=False)

    if not all([document_id_str, company_id_str, filename, content_type]):
        err_msg_args = "Missing required arguments (doc_id, company_id, filename, content_type)"
        log.error(err_msg_args, payload_kwargs=kwargs)
        stdlib_task_logger.error(f"{err_msg_args} - Payload: {kwargs}")
        raise Reject(err_msg_args, requeue=False)

    stdlib_task_logger.debug("Checking global resources...")
    global_resources_check = {"Sync DB Engine": sync_engine, "GCS Client": gcs_client_global}
    for name, resource in global_resources_check.items():
        if not resource:
            error_msg_resource = f"Worker resource '{name}' is not initialized. Task {early_task_id} cannot proceed."
            log.critical(error_msg_resource)
            stdlib_task_logger.critical(error_msg_resource)
            if name != "Sync DB Engine" and sync_engine and document_id_str:
                try: 
                    doc_uuid_for_error_res = uuid.UUID(document_id_str)
                    set_status_sync(engine=sync_engine, document_id=doc_uuid_for_error_res, status=DocumentStatus.ERROR, error_message=error_msg_resource)
                except Exception as db_err_res: 
                    crit_msg_db_fail = f"Failed to update status after {name} check failure!"
                    log.critical(crit_msg_db_fail, error=str(db_err_res))
                    stdlib_task_logger.critical(f"{crit_msg_db_fail} Error: {db_err_res}")
            raise Reject(error_msg_resource, requeue=False)
    stdlib_task_logger.debug("Global resources check passed.")

    doc_uuid: uuid.UUID
    try:
        doc_uuid = uuid.UUID(document_id_str)
    except ValueError:
         err_msg_uuid = "Invalid document_id format."
         log.error(err_msg_uuid, received_doc_id=document_id_str)
         stdlib_task_logger.error(f"{err_msg_uuid} - Received: {document_id_str}")
         raise Reject(err_msg_uuid, requeue=False)

    if content_type not in settings.SUPPORTED_CONTENT_TYPES:
        error_msg_content = f"Unsupported content type by ingest-service: {content_type}"
        log.error(error_msg_content)
        stdlib_task_logger.error(error_msg_content)
        if sync_engine: 
             set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_content)
        raise Reject(error_msg_content, requeue=False)

    normalized_filename = normalize_filename(filename) 
    object_name = f"{company_id_str}/{document_id_str}/{normalized_filename}"
    file_bytes: Optional[bytes] = None
    processed_chunks_from_docproc: List[Dict[str, Any]] = []
    
    stdlib_task_logger.info(f"Task {early_task_id}: Pre-processing checks passed. Normalized filename: {normalized_filename}, GCS object: {object_name}")

    try:
        log.info("Attempting to set status to PROCESSING in DB.") 
        stdlib_task_logger.info(f"StdLib: Attempting to set status to PROCESSING for {doc_uuid}")
        status_updated = set_status_sync(
            engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSING, error_message=None
        )
        if not status_updated:
            warn_msg_status = "Failed to update status to PROCESSING (document possibly deleted?). Ignoring task."
            log.warning(warn_msg_status)
            stdlib_task_logger.warning(warn_msg_status)
            raise Ignore()
        log.info("Successfully set status to PROCESSING.")
        stdlib_task_logger.info(f"StdLib: Successfully set status to PROCESSING for {doc_uuid}")


        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = pathlib.Path(temp_dir)
            temp_file_path_obj = temp_dir_path / normalized_filename
            
            log.info(f"Attempting GCS download: {object_name} -> {str(temp_file_path_obj)}")
            stdlib_task_logger.info(f"StdLib: Attempting GCS download: {object_name} -> {str(temp_file_path_obj)}")
            
            gcs_client_global.download_file_sync(object_name, str(temp_file_path_obj))
            
            log.info("File downloaded successfully from GCS.")
            stdlib_task_logger.info(f"StdLib: File downloaded successfully from GCS. Object: {object_name}")
            
            file_bytes = temp_file_path_obj.read_bytes()
            
            log.info(f"File content read into memory ({len(file_bytes)} bytes).")
            stdlib_task_logger.info(f"StdLib: File content read into memory ({len(file_bytes)} bytes). Object: {object_name}")

        if file_bytes is None:
            fb_none_err = "File_bytes is None after GCS download and read, indicating an issue."
            log.error(fb_none_err, object_name=object_name)
            stdlib_task_logger.error(f"StdLib: {fb_none_err}. Object: {object_name}")
            raise RuntimeError(fb_none_err) 


        log.info("Calling Document Processing Service (synchronous)...")
        stdlib_task_logger.info("StdLib: Calling Document Processing Service (synchronous)...")
        docproc_url = str(settings.DOCPROC_SERVICE_URL)
        files_payload = {'file': (normalized_filename, file_bytes, content_type)}
        data_payload = {
            'original_filename': normalized_filename, 
            'content_type': content_type,
            'document_id': document_id_str,
            'company_id': company_id_str
        }
        try:
            with httpx.Client(timeout=settings.HTTP_CLIENT_TIMEOUT) as client:
                @sync_http_retry_strategy
                def call_docproc():
                    log.debug(f"Attempting POST to DocProc: {docproc_url}")
                    stdlib_task_logger.debug(f"StdLib: Attempting POST to DocProc: {docproc_url}")
                    return client.post(docproc_url, files=files_payload, data=data_payload)
                
                response = call_docproc()
                response.raise_for_status() 
                docproc_response_data = response.json()

            if "data" not in docproc_response_data or "chunks" not in docproc_response_data.get("data", {}):
                val_err_msg = "Invalid response format from DocProc Service."
                log.error(val_err_msg, response_data_preview=str(docproc_response_data)[:200])
                stdlib_task_logger.error(f"StdLib: {val_err_msg} - Preview: {str(docproc_response_data)[:200]}")
                raise ValueError(val_err_msg)
            
            processed_chunks_from_docproc = docproc_response_data.get("data", {}).get("chunks", [])
            log.info(f"Received {len(processed_chunks_from_docproc)} chunks from DocProc Service.")
            stdlib_task_logger.info(f"StdLib: Received {len(processed_chunks_from_docproc)} chunks from DocProc.")


        except httpx.HTTPStatusError as hse:
            error_msg_dpce = f"DocProc Error ({hse.response.status_code}): {str(hse.response.text)[:300]}"
            log.error("DocProc Service HTTP Error", status_code=hse.response.status_code, response_text=hse.response.text, exc_info=True)
            stdlib_task_logger.error(f"StdLib: DocProc Service HTTP Error {hse.response.status_code} - {hse.response.text[:100]}", exc_info=True)
            if sync_engine: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_dpce)
            if 500 <= hse.response.status_code < 600: 
                raise 
            else: 
                raise Reject(f"DocProc Service critical error: {error_msg_dpce}", requeue=False) from hse
        except httpx.RequestError as re: 
            req_err_msg = f"DocProc Network Error: {type(re).__name__}"
            log.error("DocProc Service Request Error", error_msg=str(re), exc_info=True)
            stdlib_task_logger.error(f"StdLib: DocProc Service Request Error {type(re).__name__} - {str(re)}", exc_info=True)
            if sync_engine: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=req_err_msg)
            raise


        if not processed_chunks_from_docproc:
            no_chunks_msg = "DocProc Service returned no chunks. Document processing considered complete with 0 chunks."
            log.warning(no_chunks_msg)
            stdlib_task_logger.warning(f"StdLib: {no_chunks_msg}")
            if sync_engine: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSED, chunk_count=0, error_message=None)
            return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": 0, "document_id": document_id_str}

        chunk_texts_for_embedding = [chunk['text'] for chunk in processed_chunks_from_docproc if chunk.get('text','').strip()]
        if not chunk_texts_for_embedding:
            no_text_chunks_msg = "No non-empty text found in chunks received from DocProc. Finishing with 0 chunks."
            log.warning(no_text_chunks_msg)
            stdlib_task_logger.warning(f"StdLib: {no_text_chunks_msg}")
            if sync_engine: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSED, chunk_count=0, error_message=None)
            return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": 0, "document_id": document_id_str}


        log.info(f"Calling Embedding Service for {len(chunk_texts_for_embedding)} texts (synchronous)...")
        stdlib_task_logger.info(f"StdLib: Calling Embedding Service for {len(chunk_texts_for_embedding)} texts...")
        embedding_service_url = str(settings.EMBEDDING_SERVICE_URL)
        
        embedding_request_payload = {
            "texts": chunk_texts_for_embedding,
            "text_type": "passage"
        }
        embeddings: List[List[float]] = []
        try:
            with httpx.Client(timeout=settings.HTTP_CLIENT_TIMEOUT) as client:
                @sync_http_retry_strategy
                def call_embedding_svc():
                    log.debug(f"Attempting POST to EmbeddingSvc: {embedding_service_url}")
                    stdlib_task_logger.debug(f"StdLib: Attempting POST to EmbeddingSvc: {embedding_service_url} with payload: {json.dumps(embedding_request_payload)[:200]}...")
                    return client.post(embedding_service_url, json=embedding_request_payload)

                response_embed = call_embedding_svc()
                response_embed.raise_for_status()
                embedding_response_data = response_embed.json()

            if "embeddings" not in embedding_response_data or "model_info" not in embedding_response_data:
                emb_val_err_msg = "Invalid response format from Embedding Service."
                log.error(emb_val_err_msg, response_data_preview=str(embedding_response_data)[:200])
                stdlib_task_logger.error(f"StdLib: {emb_val_err_msg} - Preview: {str(embedding_response_data)[:200]}")
                raise ValueError(emb_val_err_msg)

            embeddings = embedding_response_data["embeddings"]
            model_info = embedding_response_data["model_info"]
            log.info(f"Embeddings received from service for {len(embeddings)} chunks. Model: {model_info}")
            stdlib_task_logger.info(f"StdLib: Embeddings received for {len(embeddings)} chunks. Model: {model_info}")


            if len(embeddings) != len(chunk_texts_for_embedding):
                emb_count_err = f"Embedding count mismatch. Expected {len(chunk_texts_for_embedding)}, got {len(embeddings)}."
                log.error("Embedding count mismatch from Embedding Service.", expected=len(chunk_texts_for_embedding), received=len(embeddings))
                stdlib_task_logger.error(f"StdLib: {emb_count_err}")
                raise RuntimeError(emb_count_err)
            
            if embeddings and model_info.get("dimension") != settings.EMBEDDING_DIMENSION:
                 emb_dim_err = (f"Embedding dimension from service ({model_info.get('dimension')}) "
                                f"does not match ingest-service's configured EMBEDDING_DIMENSION ({settings.EMBEDDING_DIMENSION}). "
                                "Ensure configurations are aligned across services (embedding-service and ingest-service).")
                 log.error(emb_dim_err, 
                           service_reported_dim=model_info.get('dimension'), 
                           ingest_configured_dim=settings.EMBEDDING_DIMENSION)
                 stdlib_task_logger.error(f"StdLib: {emb_dim_err}")
                 if sync_engine: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=emb_dim_err[:500])
                 raise Reject(emb_dim_err, requeue=False)

        except httpx.HTTPStatusError as hse_embed:
            error_msg_esc = f"Embedding Service Error ({hse_embed.response.status_code}): {str(hse_embed.response.text)[:300]}"
            log.error("Embedding Service HTTP Error", status_code=hse_embed.response.status_code, response_text=hse_embed.response.text, exc_info=True)
            stdlib_task_logger.error(f"StdLib: Embedding Service HTTP Error {hse_embed.response.status_code} - {hse_embed.response.text[:100]}", exc_info=True)
            if sync_engine: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_esc)
            if 500 <= hse_embed.response.status_code < 600:
                raise
            else:
                raise Reject(f"Embedding Service critical error: {error_msg_esc}", requeue=False) from hse_embed
        except httpx.RequestError as re_embed:
            emb_req_err_msg = f"EmbeddingSvc Network Error: {type(re_embed).__name__}"
            log.error("Embedding Service Request Error", error_msg=str(re_embed), exc_info=True)
            stdlib_task_logger.error(f"StdLib: Embedding Service Request Error {type(re_embed).__name__} - {str(re_embed)}", exc_info=True)
            if sync_engine: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=emb_req_err_msg)
            raise

        log.info("Preparing chunks for Milvus and PostgreSQL indexing...")
        stdlib_task_logger.info("StdLib: Preparing chunks for Milvus and PostgreSQL indexing...")
        inserted_milvus_count, milvus_pks, chunks_for_pg_insert = index_chunks_in_milvus_and_prepare_for_pg(
            processed_chunks_from_docproc=[chunk for chunk in processed_chunks_from_docproc if chunk.get('text','').strip()],
            embeddings=embeddings,
            filename=normalized_filename,
            company_id_str=company_id_str,
            document_id_str=document_id_str,
            delete_existing_milvus_chunks=True 
        )
        log.info(f"Milvus indexing complete. Inserted: {inserted_milvus_count}, PKs: {len(milvus_pks)}")
        stdlib_task_logger.info(f"StdLib: Milvus indexing complete. Inserted: {inserted_milvus_count}, PKs: {len(milvus_pks)}")


        if inserted_milvus_count == 0 or not chunks_for_pg_insert:
            zero_milvus_msg = "Milvus indexing resulted in zero inserted chunks or no data for PG. Assuming processing complete with 0 chunks."
            log.warning(zero_milvus_msg)
            stdlib_task_logger.warning(f"StdLib: {zero_milvus_msg}")
            if sync_engine: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSED, chunk_count=0, error_message=None)
            return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": 0, "document_id": document_id_str}

        log.info(f"Attempting bulk insert of {len(chunks_for_pg_insert)} chunks into PostgreSQL.") 
        stdlib_task_logger.info(f"StdLib: Attempting bulk insert of {len(chunks_for_pg_insert)} chunks into PostgreSQL.")
        inserted_pg_count = 0
        try:
            inserted_pg_count = bulk_insert_chunks_sync(engine=sync_engine, chunks_data=chunks_for_pg_insert)
            log.info(f"Successfully bulk inserted {inserted_pg_count} chunks into PostgreSQL.")
            stdlib_task_logger.info(f"StdLib: Successfully bulk inserted {inserted_pg_count} chunks into PostgreSQL.")
            if inserted_pg_count != len(chunks_for_pg_insert):
                 pg_mismatch_warn = "Mismatch between prepared PG chunks and inserted PG count."
                 log.warning(pg_mismatch_warn, prepared=len(chunks_for_pg_insert), inserted=inserted_pg_count)
                 stdlib_task_logger.warning(f"StdLib: {pg_mismatch_warn} - Prepared: {len(chunks_for_pg_insert)}, Inserted: {inserted_pg_count}")
        except Exception as pg_insert_err:
             pg_crit_err_msg = f"PG bulk insert failed: {type(pg_insert_err).__name__}"
             log.critical("CRITICAL: Failed to bulk insert chunks into PostgreSQL after successful Milvus insert!", error=str(pg_insert_err), exc_info=True)
             stdlib_task_logger.critical(f"StdLib: CRITICAL: Failed to bulk insert chunks into PostgreSQL! Error: {pg_insert_err}", exc_info=True)
             log.warning("Attempting to clean up Milvus chunks due to PG insert failure.")
             stdlib_task_logger.warning("StdLib: Attempting to clean up Milvus chunks due to PG insert failure.")
             try: delete_milvus_chunks(company_id=company_id_str, document_id=document_id_str)
             except Exception as cleanup_err: 
                 log.error("Failed to cleanup Milvus chunks after PG failure.", cleanup_error=str(cleanup_err))
                 stdlib_task_logger.error(f"StdLib: Failed to cleanup Milvus chunks after PG failure. Error: {cleanup_err}")
             if sync_engine: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=pg_crit_err_msg[:500])
             raise Reject(f"PostgreSQL bulk insert failed: {pg_insert_err}", requeue=False) from pg_insert_err

        log.info("Setting final status to PROCESSED in DB.") 
        stdlib_task_logger.info("StdLib: Setting final status to PROCESSED in DB.")
        if sync_engine: set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSED, chunk_count=inserted_pg_count, error_message=None)
        
        final_success_msg = f"Document processing finished successfully. Final chunk count (PG): {inserted_pg_count}"
        log.info(final_success_msg)
        stdlib_task_logger.info(f"StdLib: {final_success_msg}")
        return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": inserted_pg_count, "document_id": document_id_str}

    except GCSClientError as gce:
        gcs_err_msg_task = f"GCS Error: {str(gce)[:400]}"
        log.error(f"GCS Error during processing task {early_task_id}", error_msg=str(gce), exc_info=True)
        stdlib_task_logger.error(f"StdLib: GCS Error during processing task {early_task_id} - {str(gce)}", exc_info=True)
        if sync_engine and 'doc_uuid' in locals(): 
             set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=gcs_err_msg_task)
        if "Object not found" in str(gce): 
            raise Reject(f"GCS Error: Object not found: {object_name}", requeue=False) from gce
        raise 

    except (ValueError, RuntimeError, TypeError) as non_retriable_err: 
         error_msg_pipe = f"Pipeline/Data Error: {type(non_retriable_err).__name__} - {str(non_retriable_err)[:400]}"
         log.error(f"Non-retriable Error in task {early_task_id}: {non_retriable_err}", exc_info=True)
         stdlib_task_logger.error(f"StdLib: Non-retriable Error in task {early_task_id}: {type(non_retriable_err).__name__} - {str(non_retriable_err)}", exc_info=True)
         if sync_engine and 'doc_uuid' in locals():
             set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_pipe)
         raise Reject(f"Pipeline failed for task {early_task_id}: {error_msg_pipe}", requeue=False) from non_retriable_err
    
    except Reject as r: 
         reject_reason = getattr(r, 'reason', 'Unknown reason')
         log.error(f"Task {early_task_id} rejected permanently: {reject_reason}")
         stdlib_task_logger.error(f"StdLib: Task {early_task_id} rejected permanently: {reject_reason}")
         if sync_engine and 'doc_uuid' in locals() and reject_reason: 
             set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=f"Rejected: {str(reject_reason)[:500]}")
         raise 
    except Ignore: 
         log.info(f"Task {early_task_id} ignored.")
         stdlib_task_logger.info(f"StdLib: Task {early_task_id} ignored.")
         raise 
    except Retry as retry_exc: 
        log.warning(f"Task {early_task_id} is being retried by Celery. Reason: {retry_exc}", exc_info=False) 
        stdlib_task_logger.warning(f"StdLib: Task {early_task_id} is being retried by Celery. Reason: {retry_exc}")
        raise 
    except MaxRetriesExceededError as mree:
        final_error = mree.cause if mree.cause else mree
        error_msg_mree = f"Max retries exceeded for task {early_task_id} ({max_attempts}). Last error: {type(final_error).__name__} - {str(final_error)[:300]}"
        log.error(error_msg_mree, exc_info=True, cause_type=type(final_error).__name__, cause_message=str(final_error))
        stdlib_task_logger.error(f"StdLib: {error_msg_mree}", exc_info=True)
        if sync_engine and 'doc_uuid' in locals(): 
            set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.ERROR, error_message=error_msg_mree)
        self.update_state(state=states.FAILURE, meta={'exc_type': type(final_error).__name__, 'exc_message': str(final_error)})
    except Exception as exc: 
        error_msg_exc_for_db = f"Attempt {attempt} failed: {type(exc).__name__} - {str(exc)[:200]}"
        log.exception(f"An unexpected error occurred in task {early_task_id}, attempting Celery retry if possible.")
        stdlib_task_logger.exception(f"StdLib: An unexpected error occurred in task {early_task_id}.")
        if sync_engine and 'doc_uuid' in locals(): 
            set_status_sync(engine=sync_engine, document_id=doc_uuid, status=DocumentStatus.PROCESSING, error_message=error_msg_exc_for_db) 
        raise 
    finally:
        stdlib_task_logger.info(f"--- TASK EXIT: {early_task_id} --- Attempt: {attempt}/{max_attempts}")
        print(f"--- PRINT TASK EXIT: {early_task_id} --- Attempt: {attempt}/{max_attempts}", flush=True)
        sys.stdout.flush()
        sys.stderr.flush()
```

## File: `pyproject.toml`
```toml
[tool.poetry]
name = "ingest-service"
version = "1.3.2"
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
pymilvus = "==2.5.3"
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
