import uuid
from typing import Dict, Any, Optional
import json
import structlog

from fastapi import APIRouter, UploadFile, File, Depends, HTTPException, status, Form, Header

from app.api.v1 import schemas
from app.core.config import settings
from app.db import postgres_client
from app.tasks.process_document import process_document_haystack_task # Use the new task
from app.services.storage_client import StorageServiceClient # Need this client

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Dependency for Company ID (Keep as is or adapt to your auth) ---
async def get_current_company_id(x_company_id: Optional[str] = Header(None)) -> uuid.UUID:
    # ... (implementation from previous version is fine) ...
    if not x_company_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Company-ID header",
        )
    try:
        return uuid.UUID(x_company_id)
    except ValueError:
         raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid X-Company-ID header format (must be UUID)",
        )

# --- Endpoints ---
@router.post(
    "/ingest",
    response_model=schemas.IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a new document (Haystack)",
    description="Uploads a document, stores it, creates a DB record, and queues it for Haystack processing.",
)
async def ingest_document_haystack(
    metadata_json: str = Form(default="{}", description="JSON string of document metadata"),
    file: UploadFile = File(..., description="The document file to ingest"),
    company_id: uuid.UUID = Depends(get_current_company_id),
):
    """
    Endpoint to initiate document ingestion using Haystack pipeline.
    1. Validates input and metadata.
    2. Uploads file to Storage Service.
    3. Creates initial record in PostgreSQL.
    4. Queues the Haystack processing task in Celery.
    """
    request_log = log.bind(company_id=str(company_id), filename=file.filename, content_type=file.content_type)
    request_log.info("Received document ingestion request (Haystack)")

    # 1. Validate Content Type
    if file.content_type not in settings.SUPPORTED_CONTENT_TYPES:
        request_log.warning("Unsupported content type received")
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type: {file.content_type}. Supported types: {settings.SUPPORTED_CONTENT_TYPES}",
        )

    # 2. Validate Metadata
    try:
        metadata = json.loads(metadata_json)
        if not isinstance(metadata, dict):
            raise ValueError("Metadata must be a JSON object")
        # Optional: Validate metadata against a Pydantic schema here
        # schemas.DocumentMetadata(**metadata)
    except json.JSONDecodeError:
         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON format for metadata")
    except Exception as e: # Catch validation errors too
         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid metadata: {e}")

    # --- Refactored Flow: Store first, then queue ---
    storage_client = StorageServiceClient()
    file_path: Optional[str] = None
    document_id: Optional[uuid.UUID] = None
    task_id: Optional[str] = None

    try:
        # 3. Upload to Storage Service
        request_log.info("Uploading file to storage service...")
        # Read content once
        file_content = await file.read()
        if not file_content:
             raise ValueError("Uploaded file is empty.")

        # Use BytesIO to pass content to the client
        file_stream = io.BytesIO(file_content)
        file_path = await storage_client.upload_file(
            company_id=company_id,
            file_name=file.filename or "untitled",
            file_content=file_stream, # Pass the stream
            content_type=file.content_type or "application/octet-stream"
        )
        request_log.info("File uploaded successfully", file_path=file_path)

        # 4. Create initial record in PostgreSQL AFTER successful upload
        document_id = await postgres_client.create_document(
            company_id=company_id,
            file_name=file.filename or "untitled",
            file_type=file.content_type or "application/octet-stream",
            metadata=metadata,
            # file_path is now set via update_document_status in the task
        )
        request_log = request_log.bind(document_id=str(document_id))
        request_log.info("Initial document record created in DB")

        # 5. Enqueue Celery Task with file_path
        task = process_document_haystack_task.delay(
            document_id_str=str(document_id),
            company_id_str=str(company_id),
            file_path=file_path, # Pass the path from storage
            file_name=file.filename or "untitled",
            content_type=file.content_type or "application/octet-stream",
            original_metadata=metadata,
        )
        task_id = task.id
        request_log.info("Haystack document processing task queued", task_id=task_id)

        return schemas.IngestResponse(document_id=document_id, task_id=task_id)

    except Exception as e:
        request_log.error("Error during ingestion trigger", error=str(e), exc_info=True)
        # Attempt to mark as error if DB record was created
        if document_id:
            try:
                await postgres_client.update_document_status(
                    document_id,
                    schemas.DocumentStatus.ERROR,
                    error_message=f"Ingestion API Error: {type(e).__name__}: {str(e)[:250]}"
                )
            except Exception as db_err:
                 request_log.error("Failed to mark document as error after API failure", nested_error=str(db_err))
        # Determine appropriate status code
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        detail = "Failed to process ingestion request."
        if isinstance(e, ValueError): # e.g., empty file
             status_code = status.HTTP_400_BAD_REQUEST
             detail = str(e)
        elif isinstance(e, FileNotFoundError): # Should not happen if storage upload succeeded
             status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
             detail = "Storage inconsistency detected."

        raise HTTPException(status_code=status_code, detail=detail)

    finally:
        await file.close()
        await storage_client.close()


@router.get(
    "/ingest/status/{document_id}",
    response_model=schemas.StatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Get document ingestion status",
    description="Retrieves the current processing status and basic information of a document.",
)
async def get_ingestion_status(
    document_id: uuid.UUID,
    company_id: uuid.UUID = Depends(get_current_company_id),
):
    """
    Endpoint to consult the processing status of a document.
    (Implementation remains largely the same as previous version, but ensure
     it correctly reflects Haystack pipeline outcomes like PROCESSED/ERROR)
    """
    status_log = log.bind(document_id=str(document_id), company_id=str(company_id))
    status_log.info("Received request for document status")

    try:
        doc_data = await postgres_client.get_document_status(document_id)
    except Exception as e:
        status_log.error("Failed to retrieve document status from DB", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not retrieve document status.",
        )

    if not doc_data:
        status_log.warning("Document ID not found")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found.",
        )

    # Authorization Check: Ensure the requesting company owns the document
    if doc_data.get("company_id") != company_id:
        status_log.warning("Company ID mismatch for document status request", owner_company_id=doc_data.get("company_id"))
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this document's status.",
        )

    # Map DB status to response model
    response_data = schemas.StatusResponse(
        document_id=doc_data["id"],
        status=doc_data["status"], # Assumes DB status enum matches response enum
        file_name=doc_data.get("file_name"),
        file_type=doc_data.get("file_type"),
        chunk_count=doc_data.get("chunk_count"),
        error_message=doc_data.get("error_message"),
        last_updated=doc_data.get("updated_at"), # Pass datetime directly
    )

    # Add descriptive message based on status
    status_messages = {
        DocumentStatus.UPLOADED: "Document uploaded, awaiting processing.",
        DocumentStatus.PROCESSING: "Document is currently being processed by the Haystack pipeline.",
        DocumentStatus.PROCESSED: f"Document processed successfully with {response_data.chunk_count or 0} chunks indexed.",
        DocumentStatus.ERROR: f"Processing failed: {response_data.error_message or 'Unknown error'}",
        DocumentStatus.INDEXED: f"Document processed and indexed successfully with {response_data.chunk_count or 0} chunks.", # If using INDEXED status
    }
    response_data.message = status_messages.get(response_data.status, "Unknown status.")


    status_log.info("Returning document status", status=response_data.status)
    return response_data