import uuid
from typing import Dict, Any, Optional
import json
import structlog
import io

from fastapi import APIRouter, UploadFile, File, Depends, HTTPException, status, Form, Header
from minio.error import S3Error # Importar S3Error si se usa en el handler

from app.api.v1 import schemas
from app.core.config import settings
from app.db import postgres_client
from app.tasks.process_document import process_document_haystack_task
from app.services.minio_client import MinioStorageClient
# *** CORREGIDO: Importar DocumentStatus desde models.domain ***
from app.models.domain import DocumentStatus

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Dependency for Company ID (Sin cambios) ---
async def get_current_company_id(x_company_id: Optional[str] = Header(None)) -> uuid.UUID:
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
    """
    request_log = log.bind(company_id=str(company_id), filename=file.filename, content_type=file.content_type)
    request_log.info("Received document ingestion request (Haystack)")

    # 1. Validate Content Type (Sin cambios)
    if not file.content_type or file.content_type not in settings.SUPPORTED_CONTENT_TYPES:
        request_log.warning("Unsupported or missing content type received", received_type=file.content_type)
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type: {file.content_type or 'Unknown'}. Supported types: {settings.SUPPORTED_CONTENT_TYPES}",
        )
    content_type = file.content_type

    # 2. Validate Metadata (Sin cambios)
    try:
        metadata = json.loads(metadata_json)
        if not isinstance(metadata, dict):
            raise ValueError("Metadata must be a JSON object")
    except json.JSONDecodeError:
         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON format for metadata")
    except Exception as e:
         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid metadata: {e}")

    minio_client = MinioStorageClient()
    minio_object_name: Optional[str] = None
    document_id: Optional[uuid.UUID] = None
    task_id: Optional[str] = None

    try:
        # 3. Create initial DB record (Sin cambios)
        document_id = await postgres_client.create_document(
            company_id=company_id,
            file_name=file.filename or "untitled",
            file_type=content_type,
            metadata=metadata,
        )
        request_log = request_log.bind(document_id=str(document_id))
        request_log.info("Initial document record created in DB")

        # 4. Upload to MinIO (Sin cambios)
        request_log.info("Uploading file to MinIO...")
        file_content = await file.read()
        content_length = len(file_content)
        if content_length == 0:
            raise ValueError("Uploaded file is empty.")
        file_stream = io.BytesIO(file_content)
        minio_object_name = await minio_client.upload_file(
            company_id=company_id, document_id=document_id, file_name=file.filename or "untitled",
            file_content_stream=file_stream, content_type=content_type, content_length=content_length
        )
        request_log.info("File uploaded successfully to MinIO", object_name=minio_object_name)

        # 5. Update DB record (Sin cambios)
        await postgres_client.update_document_status(
            document_id=document_id,
            status=DocumentStatus.UPLOADED, # Usando el Enum importado
            file_path=minio_object_name
        )
        request_log.info("Document record updated with MinIO object name")

        # 6. Enqueue Celery Task (Sin cambios)
        task = process_document_haystack_task.delay(
            document_id_str=str(document_id),
            company_id_str=str(company_id),
            minio_object_name=minio_object_name,
            file_name=file.filename or "untitled",
            content_type=content_type,
            original_metadata=metadata,
        )
        task_id = task.id
        request_log.info("Haystack document processing task queued", task_id=task_id)

        return schemas.IngestResponse(document_id=document_id, task_id=task_id)

    except Exception as e:
        request_log.error("Error during ingestion trigger", error=str(e), exc_info=True)
        if document_id:
            try:
                # Attempt to mark as error, don't overwrite file_path if it was set
                # *** CORREGIDO: Usar el Enum DocumentStatus importado ***
                await postgres_client.update_document_status(
                    document_id,
                    DocumentStatus.ERROR, # Usar el Enum correcto
                    error_message=f"Ingestion API Error: {type(e).__name__}: {str(e)[:250]}"
                )
            except Exception as db_err:
                 request_log.error("Failed to mark document as error after API failure", nested_error=str(db_err))

        # Mapeo de excepciones a respuestas HTTP (Sin cambios)
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        detail = "Failed to process ingestion request."
        if isinstance(e, ValueError):
             status_code = status.HTTP_400_BAD_REQUEST
             detail = str(e)
        elif isinstance(e, S3Error):
             status_code = status.HTTP_503_SERVICE_UNAVAILABLE
             detail = f"Storage service error: {e.code}"
             request_log.warning("MinIO S3 error during ingestion trigger", code=e.code, error_details=str(e))

        raise HTTPException(status_code=status_code, detail=detail)

    finally:
        # Asegurar que el archivo se cierra (Sin cambios)
        if file:
            await file.close()


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
    (Sin cambios necesarios aquí)
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

    if doc_data.get("company_id") != company_id:
        status_log.warning("Company ID mismatch for document status request", owner_company_id=doc_data.get("company_id"))
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this document's status.",
        )

    # Mapear estado de DB a respuesta (asegurarse que el status de la DB es un string válido del Enum)
    try:
        db_status = DocumentStatus(doc_data["status"])
    except ValueError:
        status_log.error("Invalid status value found in database for document", db_status=doc_data["status"])
        db_status = DocumentStatus.ERROR # O manejar de otra forma
        doc_data["error_message"] = f"Internal Error: Invalid status '{doc_data['status']}' in DB."


    response_data = schemas.StatusResponse(
        document_id=doc_data["id"],
        status=db_status, # Usar el valor validado/convertido del Enum
        file_name=doc_data.get("file_name"),
        file_type=doc_data.get("file_type"),
        chunk_count=doc_data.get("chunk_count"),
        error_message=doc_data.get("error_message"),
        last_updated=doc_data.get("updated_at"),
    )

    # Añadir mensaje descriptivo
    status_messages = {
        DocumentStatus.UPLOADED: "Document uploaded, awaiting processing.",
        DocumentStatus.PROCESSING: "Document is currently being processed by the Haystack pipeline.",
        DocumentStatus.PROCESSED: f"Document processed successfully with {response_data.chunk_count or 0} chunks indexed.",
        DocumentStatus.ERROR: f"Processing failed: {response_data.error_message or 'Unknown error'}",
        DocumentStatus.INDEXED: f"Document processed and indexed successfully with {response_data.chunk_count or 0} chunks.",
    }
    response_data.message = status_messages.get(response_data.status, "Unknown status.")

    status_log.info("Returning document status", status=response_data.status.value)
    return response_data