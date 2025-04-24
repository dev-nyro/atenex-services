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
    return None # FastAPI handles 204 automatically for None retur