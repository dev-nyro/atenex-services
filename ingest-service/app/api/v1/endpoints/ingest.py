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