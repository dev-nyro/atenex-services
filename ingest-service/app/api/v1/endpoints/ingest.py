# ingest-service/app/api/v1/endpoints/ingest.py
import uuid
from typing import Dict, Any, Optional, List
import json
import structlog
import io

from fastapi import (
    APIRouter, UploadFile, File, Depends, HTTPException,
    status, Form, Header, Query # Importar Query para parámetros de consulta si fueran necesarios
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

# --- Dependency for Company ID (Correcta) ---
async def get_current_company_id(x_company_id: Optional[str] = Header(None)) -> uuid.UUID:
    if not x_company_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-Company-ID header")
    try:
        company_uuid = uuid.UUID(x_company_id)
        log.debug("Validated X-Company-ID", company_id=str(company_uuid))
        return company_uuid
    except ValueError:
         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid X-Company-ID header format (must be UUID)")

# --- Endpoints ---

@router.post(
    "/ingest", # Ruta relativa al prefijo /api/v1/ingest añadido en main.py
    response_model=schemas.IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingestar un nuevo documento",
    description="Sube un documento, lo almacena, crea un registro en BD y lo encola para procesamiento Haystack.",
)
async def ingest_document_haystack(
    # Dependencias primero
    company_id: uuid.UUID = Depends(get_current_company_id),
    # Datos del Formulario
    metadata_json: str = Form(default="{}", description="String JSON de metadatos del documento"),
    file: UploadFile = File(..., description="El archivo del documento a ingestar"),
    # NINGÚN OTRO PARÁMETRO QUE PUEDA SER INTERPRETADO COMO BODY O QUERY['query']
):
    request_log = log.bind(company_id=str(company_id), filename=file.filename, content_type=file.content_type)
    request_log.info("Received document ingestion request (Haystack)")

    # 1. Validate Content Type
    if not file.content_type or file.content_type not in settings.SUPPORTED_CONTENT_TYPES:
        request_log.warning("Unsupported or missing content type received", received_type=file.content_type)
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=f"Unsupported file type: {file.content_type or 'Unknown'}. Supported types: {settings.SUPPORTED_CONTENT_TYPES}")
    content_type = file.content_type

    # 2. Validate Metadata
    try:
        metadata = json.loads(metadata_json); assert isinstance(metadata, dict)
    except (json.JSONDecodeError, AssertionError):
         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON object format for metadata")
    except Exception as e:
         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid metadata: {e}")

    minio_client = MinioStorageClient(); minio_object_name: Optional[str] = None
    document_id: Optional[uuid.UUID] = None; task_id: Optional[str] = None

    try:
        # --- Lógica de creación, subida y encolado (sin cambios) ---
        document_id = await postgres_client.create_document(company_id=company_id, file_name=file.filename or "untitled", file_type=content_type, metadata=metadata)
        request_log = request_log.bind(document_id=str(document_id)); request_log.info("Initial document record created")
        file_content = await file.read(); content_length = len(file_content)
        if content_length == 0:
            await postgres_client.update_document_status(document_id=document_id, status=DocumentStatus.ERROR, error_message="Uploaded file is empty.")
            request_log.error("Uploaded file is empty."); raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file cannot be empty.")
        file_stream = io.BytesIO(file_content)
        minio_object_name = await minio_client.upload_file(company_id=company_id, document_id=document_id, file_name=file.filename or "untitled", file_content_stream=file_stream, content_type=content_type, content_length=content_length)
        request_log.info("File uploaded to MinIO", object_name=minio_object_name)
        await postgres_client.update_document_status(document_id=document_id, status=DocumentStatus.UPLOADED, file_path=minio_object_name)
        request_log.info("Document record updated with MinIO path")
        task = process_document_haystack_task.delay(document_id_str=str(document_id), company_id_str=str(company_id), minio_object_name=minio_object_name, file_name=file.filename or "untitled", content_type=content_type, original_metadata=metadata)
        task_id = task.id
        request_log.info("Haystack processing task queued", task_id=task_id)
        return schemas.IngestResponse(document_id=document_id, task_id=task_id, status=DocumentStatus.UPLOADED, message="Document upload received and queued for processing.")
    # --- Manejo de errores (sin cambios) ---
    except HTTPException as http_exc: raise http_exc
    except S3Error as s3_err: request_log.error("MinIO S3 Error", error=str(s3_err), code=s3_err.code, exc_info=True); raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Storage service error: {s3_err.code}")
    except Exception as e: request_log.error("Unexpected ingestion error", error=str(e), exc_info=True); raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to process ingestion request.")
    finally: await file.close()

@router.get(
    "/status/{document_id}", # Ruta relativa correcta
    response_model=schemas.StatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Consultar estado de ingesta de un documento",
    description="Recupera el estado actual del procesamiento y la información básica de un documento específico.",
)
async def get_ingestion_status(
    # Path parameter
    document_id: uuid.UUID,
    # Header dependency
    company_id: uuid.UUID = Depends(get_current_company_id),
    # NINGÚN OTRO PARÁMETRO (especialmente ninguno llamado 'query' que pueda ser un modelo)
):
    status_log = log.bind(document_id=str(document_id), company_id=str(company_id))
    status_log.info("Received request for single document status")
    # --- Lógica de obtención y validación (sin cambios) ---
    try: doc_data = await postgres_client.get_document_status(document_id)
    except Exception as e: status_log.error("DB error getting status", e=e); raise HTTPException(status_code=500)
    if not doc_data: raise HTTPException(status_code=404)
    if doc_data.get("company_id") != company_id: raise HTTPException(status_code=403)
    try: response_data = schemas.StatusResponse.model_validate(doc_data)
    except Exception as p_err: status_log.error("Schema validation error", e=p_err); raise HTTPException(status_code=500)
    # --- Mensaje descriptivo (sin cambios) ---
    status_messages = {
        DocumentStatus.UPLOADED: "The document has been successfully uploaded.",
        DocumentStatus.PROCESSING: "The document is currently being processed.",
        DocumentStatus.COMPLETED: "The document has been processed successfully.",
        DocumentStatus.ERROR: "An error occurred during document processing.",
    }
    response_data.message = status_messages.get(response_data.status, "Unknown status.")
    status_log.info("Returning document status", status=response_data.status)
    return response_data


@router.get(
    "/status", # Ruta relativa correcta
    response_model=List[schemas.StatusResponse],
    status_code=status.HTTP_200_OK,
    summary="Listar estados de ingesta para la compañía",
    description="Recupera una lista de todos los documentos y sus estados de procesamiento para la compañía actual.",
)
async def list_ingestion_statuses(
    # Header dependency
    company_id: uuid.UUID = Depends(get_current_company_id),
    # Opcional: Añadir parámetros de consulta explícitos si se necesitan
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0)
    # NINGÚN OTRO PARÁMETRO (especialmente ninguno llamado 'query' que pueda ser un modelo)
):
    list_log = log.bind(company_id=str(company_id), limit=limit, offset=offset)
    list_log.info("Received request to list document statuses")
    try:
        # Pasar limit y offset a la función DB si la soporta
        # documents_data = await postgres_client.list_documents_by_company(company_id, limit=limit, offset=offset)
        # Si no, obtener todos y paginar aquí (menos eficiente)
        documents_data = await postgres_client.list_documents_by_company(company_id, limit=limit, offset=offset) # Implementar paginación en la consulta DB

    except Exception as e: list_log.error("DB error listing statuses", e=e); raise HTTPException(status_code=500)

    response_list = [schemas.StatusResponse.model_validate(doc_data) for doc_data in paginated_data]
    list_log.info(f"Returning status list", count=len(response_list))
    return response_list