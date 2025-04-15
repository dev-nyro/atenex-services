# ingest-service/app/api/v1/endpoints/ingest.py
import uuid
from typing import Dict, Any, Optional, List
import json
import structlog
import io

from fastapi import (
    APIRouter, UploadFile, File, Depends, HTTPException,
    status, Form, Header, Query, Request # Importar Request
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

# --- CORRECCIÓN: Cambiar dependencias para usar cabeceras X-* ---
# Dependencia para Company ID (sin cambios, ya usa X-Company-ID)
async def get_current_company_id(x_company_id: Optional[str] = Header(None, alias="X-Company-ID")) -> uuid.UUID: # Añadir alias explícito
    if not x_company_id:
        log.warning("Missing required X-Company-ID header")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing X-Company-ID header")
    try:
        return uuid.UUID(x_company_id)
    except ValueError:
        log.warning("Invalid UUID format in X-Company-ID header", header_value=x_company_id)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid X-Company-ID header format")

# NUEVA Dependencia para User ID desde X-User-ID
async def get_current_user_id(x_user_id: Optional[str] = Header(None, alias="X-User-ID")) -> uuid.UUID: # Hacerlo requerido
    if not x_user_id:
        log.warning("Missing required X-User-ID header")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing X-User-ID header")
    try:
        return uuid.UUID(x_user_id)
    except ValueError:
        log.warning("Invalid UUID format in X-User-ID header", header_value=x_user_id)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid X-User-ID header format")

# (Opcional) Dependencia para User Email desde X-User-Email
async def get_current_user_email(x_user_email: Optional[str] = Header(None, alias="X-User-Email")) -> Optional[str]:
    # No lanzamos error si falta, puede ser opcional
    if not x_user_email:
        log.debug("X-User-Email header missing (optional)")
    return x_user_email

# --- Endpoints Modificados ---

@router.post(
    "/upload",
    response_model=schemas.IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingestar un nuevo documento",
    description="Sube un documento, lo almacena en MinIO (bucket 'atenex'), crea registro en DB y encola para procesamiento Haystack.",
)
async def ingest_document_haystack(
    # Usar las nuevas dependencias para obtener IDs/Email
    company_id: uuid.UUID = Depends(get_current_company_id),
    user_id: uuid.UUID = Depends(get_current_user_id), # Asumimos que se requiere user_id
    # user_email: Optional[str] = Depends(get_current_user_email), # Descomentar si se usa el email
    metadata_json: str = Form(default="{}", description="String JSON de metadatos opcionales del documento"),
    file: UploadFile = File(..., description="El archivo del documento a ingestar"),
    request: Request = None # Obtener request para logs si es necesario
):
    # Usar request_id del gateway si existe, o generar uno nuevo
    request_id = request.headers.get("x-request-id") if request else str(uuid.uuid4())
    request_log = log.bind(
        request_id=request_id,
        company_id=str(company_id),
        user_id=str(user_id), # Loguear user_id obtenido
        filename=file.filename,
        content_type=file.content_type
    )
    request_log.info("Received document ingestion request via gateway headers")

    # --- El resto de la lógica permanece igual ---
    content_type = file.content_type or "application/octet-stream"
    if content_type not in settings.SUPPORTED_CONTENT_TYPES:
        request_log.warning("Unsupported file type received", received_type=content_type)
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=f"Unsupported file type: '{content_type}'.")
    try:
        metadata = json.loads(metadata_json)
        if not isinstance(metadata, dict): raise ValueError("Metadata is not a JSON object")
    except (json.JSONDecodeError, ValueError) as json_err:
        request_log.warning("Invalid metadata JSON received", error=str(json_err))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid JSON format for metadata: {json_err}")

    minio_client = None; minio_object_name = None; document_id = None; task_id = None
    try:
        # Añadir user_id al crear el documento si la tabla lo soporta/requiere
        document_id = await postgres_client.create_document(
            company_id=company_id,
            file_name=file.filename or "untitled",
            file_type=content_type,
            metadata=metadata
            # user_id=user_id, # <--- Añadir si la tabla `documents` tiene `user_id`
        )
        request_log = request_log.bind(document_id=str(document_id))
        file_content = await file.read(); content_length = len(file_content)
        if content_length == 0:
            await postgres_client.update_document_status(document_id=document_id, status=DocumentStatus.ERROR, error_message="Uploaded file is empty.")
            request_log.warning("Uploaded file is empty")
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file cannot be empty.")

        file_stream = io.BytesIO(file_content)
        minio_client = MinioStorageClient()
        minio_object_name = await minio_client.upload_file(company_id=company_id, document_id=document_id, file_name=file.filename or "untitled", file_content_stream=file_stream, content_type=content_type, content_length=content_length)
        await postgres_client.update_document_status(document_id=document_id, status=DocumentStatus.UPLOADED, file_path=minio_object_name)

        # Pasar company_id y user_id a la tarea Celery
        task = process_document_haystack_task.delay(
            document_id_str=str(document_id),
            company_id_str=str(company_id), # <-- Pasar company_id
            # user_id_str=str(user_id),      # <-- Pasar user_id si la tarea lo necesita
            minio_object_name=minio_object_name,
            file_name=file.filename or "untitled",
            content_type=content_type,
            original_metadata=metadata
        )
        task_id = task.id
        request_log.info("Haystack task queued", task_id=task_id)
        return schemas.IngestResponse(document_id=document_id, task_id=task_id, status=DocumentStatus.UPLOADED, message="Document received and queued.")
    except HTTPException as http_exc:
        raise http_exc # Re-raise known HTTP errors
    except (IOError, S3Error) as storage_err:
        request_log.error("Storage error", error=str(storage_err))
        if document_id:
            try: await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=f"Storage upload failed: {storage_err}")
            except Exception as db_err: request_log.error("Failed update status after storage error", db_error=str(db_err))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Storage service error.")
    except Exception as e:
        request_log.exception("Unexpected ingestion error")
        if document_id:
            try: await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=f"Ingestion error: {e}")
            except Exception as db_err: request_log.error("Failed update status after ingestion error", db_error=str(db_err))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Ingestion failed.")
    finally:
        if file: await file.close() # Asegurar cierre del archivo

@router.get(
    "/status/{document_id}",
    response_model=schemas.StatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Consultar estado de ingesta de un documento",
    description="Recupera el estado actual del procesamiento y la información básica de un documento específico.",
)
async def get_ingestion_status(
    document_id: uuid.UUID,
    # Usar la dependencia de company_id corregida
    company_id: uuid.UUID = Depends(get_current_company_id),
    # Añadir dependencia para user_id (opcional, dependiendo si la DB lo necesita para filtrar)
    # user_id: uuid.UUID = Depends(get_current_user_id),
    request: Request = None # Para obtener request_id si es necesario
):
    request_id = request.headers.get("x-request-id") if request else str(uuid.uuid4())
    status_log = log.bind(request_id=request_id, document_id=str(document_id), company_id=str(company_id))
    status_log.info("Received request for single document status via gateway headers")
    try:
        doc_data = await postgres_client.get_document_status(document_id)
        if not doc_data:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
        # Validar que el documento pertenece a la compañía que hace la solicitud
        if doc_data.get("company_id") != company_id:
            status_log.warning("Attempt to access document from another company", owner_company=str(doc_data.get('company_id')))
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found") # Mismo error que not found por seguridad

        response_data = schemas.StatusResponse.model_validate(doc_data)
        status_messages = {
            DocumentStatus.UPLOADED: "Document uploaded, awaiting processing.",
            DocumentStatus.PROCESSING: "Document is currently being processed.",
            DocumentStatus.PROCESSED: "Document processed successfully.",
            # *** CORREGIDO: Usar alias en status processed/indexed si es necesario ***
            # DocumentStatus.INDEXED: "Document processed and indexed successfully.", # Si se usa
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
    # Usar la dependencia de company_id corregida
    company_id: uuid.UUID = Depends(get_current_company_id),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    request: Request = None # Para request_id
):
    request_id = request.headers.get("x-request-id") if request else str(uuid.uuid4())
    list_log = log.bind(request_id=request_id, company_id=str(company_id), limit=limit, offset=offset)
    list_log.info("Received request to list document statuses via gateway headers")
    try:
        documents_data = await postgres_client.list_documents_by_company(company_id, limit=limit, offset=offset)
        response_list = [schemas.StatusResponse.model_validate(doc) for doc in documents_data]
        list_log.info(f"Returning status list for {len(response_list)} documents")
        return response_list
    except Exception as e:
        list_log.exception("Error listing document statuses")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error listing statuses.")