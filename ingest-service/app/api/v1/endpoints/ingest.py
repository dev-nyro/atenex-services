# ingest-service/app/api/v1/endpoints/ingest.py
import uuid
from typing import Dict, Any, Optional, List
import json
import structlog
import io

from fastapi import (
    APIRouter, UploadFile, File, Depends, HTTPException,
    status, Form, Header, Query
)
from minio.error import S3Error

from app.api.v1 import schemas
from app.core.config import settings
from app.db import postgres_client
from app.models.domain import DocumentStatus
from app.tasks.process_document import process_document_haystack_task
from app.services.minio_client import MinioStorageClient

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Dependency for Company ID (sin cambios) ---
async def get_current_company_id(x_company_id: Optional[str] = Header(None)) -> uuid.UUID:
    if not x_company_id: raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-Company-ID header")
    try: return uuid.UUID(x_company_id)
    except ValueError: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid X-Company-ID header format")

# --- Endpoints ---

@router.post(
    "/upload",
    response_model=schemas.IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingestar un nuevo documento",
    description="Sube un documento, lo almacena en MinIO (bucket 'atenex'), crea registro en DB y encola para procesamiento Haystack.",
)
async def ingest_document_haystack(
    company_id: uuid.UUID = Depends(get_current_company_id),
    metadata_json: str = Form(default="{}", description="String JSON de metadatos opcionales del documento"),
    file: UploadFile = File(..., description="El archivo del documento a ingestar"),
):
    request_log = log.bind(company_id=str(company_id), filename=file.filename, content_type=file.content_type)
    request_log.info("Received document ingestion request")
    # (Validaciones y lógica de subida/encolado sin cambios)...
    # ... (Igual que la versión anterior hasta el final del try/except/finally) ...
    # --- Lógica de creación, subida y encolado (sin cambios relevantes aquí) ---
    content_type = file.content_type or "application/octet-stream"
    if content_type not in settings.SUPPORTED_CONTENT_TYPES:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=f"Unsupported file type: '{content_type}'.")
    try: metadata = json.loads(metadata_json); assert isinstance(metadata, dict)
    except: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON format for metadata")

    minio_client = None; minio_object_name = None; document_id = None; task_id = None
    try:
        document_id = await postgres_client.create_document(company_id=company_id, file_name=file.filename or "untitled", file_type=content_type, metadata=metadata)
        request_log = request_log.bind(document_id=str(document_id))
        file_content = await file.read(); content_length = len(file_content)
        if content_length == 0: await postgres_client.update_document_status(document_id=document_id, status=DocumentStatus.ERROR, error_message="Uploaded file is empty."); raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file empty.")
        file_stream = io.BytesIO(file_content)
        minio_client = MinioStorageClient()
        minio_object_name = await minio_client.upload_file(company_id=company_id, document_id=document_id, file_name=file.filename or "untitled", file_content_stream=file_stream, content_type=content_type, content_length=content_length)
        await postgres_client.update_document_status(document_id=document_id, status=DocumentStatus.UPLOADED, file_path=minio_object_name)
        task = process_document_haystack_task.delay(document_id_str=str(document_id), company_id_str=str(company_id), minio_object_name=minio_object_name, file_name=file.filename or "untitled", content_type=content_type, original_metadata=metadata)
        task_id = task.id; request_log.info("Haystack task queued", task_id=task_id)
        return schemas.IngestResponse(document_id=document_id, task_id=task_id, status=DocumentStatus.UPLOADED, message="Document received and queued.")
    except HTTPException as http_exc: raise http_exc
    except (IOError, S3Error) as storage_err:
        request_log.error("Storage error", error=str(storage_err))
        if document_id:
            try:
                await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=f"Storage upload failed: {storage_err}")
            except Exception as db_err:
                request_log.error("Failed to update document status after storage error", db_error=str(db_err))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Storage service error.")
    except Exception as e:
        request_log.exception("Unexpected ingestion error")
        if document_id:
            try:
                await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=f"Ingestion error: {e}")
            except Exception as db_err:
                 request_log.error("Failed to update document status after ingestion error", db_error=str(db_err))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Ingestion failed.")
    finally: await file.close()


@router.get(
    "/status/{document_id}",
    response_model=schemas.StatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Consultar estado de ingesta de un documento",
    description="Recupera el estado actual del procesamiento y la información básica de un documento específico.",
)
async def get_ingestion_status(
    document_id: uuid.UUID,
    company_id: uuid.UUID = Depends(get_current_company_id),
):
    status_log = log.bind(document_id=str(document_id), company_id=str(company_id))
    status_log.info("Received request for single document status")
    try:
        doc_data = await postgres_client.get_document_status(document_id)
        if not doc_data: raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
        if doc_data.get("company_id") != company_id: raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found") # Security
        response_data = schemas.StatusResponse.model_validate(doc_data)
        status_messages = {
            DocumentStatus.UPLOADED: "Document uploaded, awaiting processing.",
            DocumentStatus.PROCESSING: "Document is currently being processed.",
            DocumentStatus.PROCESSED: "Document processed successfully.",
            DocumentStatus.ERROR: f"Processing error: {response_data.error_message or 'Unknown'}",
        }
        response_data.message = status_messages.get(response_data.status, "Unknown status.")
        status_log.info("Returning document status", status=response_data.status)
        return response_data
    except HTTPException as http_exc: raise http_exc
    except Exception as e: status_log.exception("Error retrieving status"); raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.get(
    "/status",
    response_model=List[schemas.StatusResponse],
    status_code=status.HTTP_200_OK,
    summary="Listar estados de ingesta para la compañía",
    description="Recupera una lista paginada de todos los documentos y sus estados para la compañía actual.",
)
async def list_ingestion_statuses(
    company_id: uuid.UUID = Depends(get_current_company_id),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0)
):
    list_log = log.bind(company_id=str(company_id), limit=limit, offset=offset)
    list_log.info("Received request to list document statuses")
    try:
        documents_data = await postgres_client.list_documents_by_company(company_id, limit=limit, offset=offset)
        # *** CORREGIDO: Usar la variable correcta 'documents_data' ***
        response_list = [schemas.StatusResponse.model_validate(doc) for doc in documents_data]
        list_log.info(f"Returning status list for {len(response_list)} documents")
        return response_list
    except Exception as e:
        list_log.exception("Error listing document statuses")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error listing statuses.")