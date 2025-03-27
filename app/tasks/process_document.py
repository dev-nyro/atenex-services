import uuid
from typing import Dict, Any, Optional
import base64
import json

from fastapi import APIRouter, UploadFile, File, Depends, HTTPException, status, Form, Header
import structlog

from app.api.v1 import schemas
from app.core.config import settings
from app.db import postgres_client
from app.tasks.process_document import process_document_task
# Dependencia para obtener company_id (simulada aquí, debería venir de Auth Service/Gateway)
from app.core.security import get_company_id_from_token # Implementar esta función

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Dependencia Simulada ---
# En un sistema real, el API Gateway validaría el JWT y pasaría el company_id
# quizás en un header personalizado (ej. X-Company-ID)
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
    status_code=status.HTTP_202_ACCEPTED, # 202 Accepted porque es asíncrono
    summary="Ingest a new document",
    description="Uploads a document file and its metadata, then queues it for processing.",
)
async def ingest_document(
    metadata_json: str = Form(default="{}", description="JSON string of document metadata"),
    file: UploadFile = File(..., description="The document file to ingest"),
    company_id: uuid.UUID = Depends(get_current_company_id), # Obtener company_id
):
    """
    Endpoint para iniciar la ingestión de un documento.
    Recibe el archivo y metadatos, crea un registro inicial en la BD,
    y encola la tarea de procesamiento en Celery.
    """
    request_log = log.bind(company_id=str(company_id), filename=file.filename, content_type=file.content_type)
    request_log.info("Received document ingestion request")

    # Validar tipo de contenido
    if file.content_type not in settings.SUPPORTED_CONTENT_TYPES:
        request_log.warning("Unsupported content type received")
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type: {file.content_type}. Supported types: {settings.SUPPORTED_CONTENT_TYPES}",
        )

    # Validar y parsear metadata JSON
    try:
        metadata = json.loads(metadata_json)
        if not isinstance(metadata, dict):
            raise ValueError("Metadata must be a JSON object")
        # Validar con Pydantic si es necesario (requiere un modelo específico)
        # schemas.DocumentMetadata(**metadata)
    except json.JSONDecodeError:
         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON format for metadata")
    except ValueError as e:
         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid metadata structure: {e}")

    # Crear registro inicial en la base de datos
    try:
        document_id = await postgres_client.create_document(
            company_id=company_id,
            file_name=file.filename or "untitled",
            file_type=file.content_type or "application/octet-stream",
            metadata=metadata,
        )
        request_log = request_log.bind(document_id=str(document_id))
        request_log.info("Initial document record created in DB")
    except Exception as e:
        request_log.error("Failed to create initial document record", error=e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to initiate document ingestion process.",
        )

    # Leer contenido del archivo y pasarlo a la tarea Celery
    # Para archivos grandes, esto puede consumir mucha memoria.
    # Alternativa: guardar temporalmente y pasar la ruta a Celery.
    # Aquí usamos base64 para simplicidad, pero ¡cuidado con el tamaño!
    try:
        file_content = await file.read()
        file_content_b64 = base64.b64encode(file_content).decode('utf-8')
        request_log.info(f"Read file content ({len(file_content)} bytes), encoded to base64")
    except Exception as e:
        request_log.error("Failed to read uploaded file content", error=e, exc_info=True)
        # Intentar marcar el documento como error si ya se creó
        try:
            await postgres_client.update_document_status(document_id, schemas.DocumentStatus.ERROR, error_message="Failed to read uploaded file")
        except: pass # Ignorar error al actualizar estado
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not read uploaded file.",
        )
    finally:
        await file.close()

    # Encolar la tarea en Celery
    try:
        task = process_document_task.delay(
            document_id_str=str(document_id),
            company_id_str=str(company_id),
            file_name=file.filename or "untitled",
            content_type=file.content_type or "application/octet-stream",
            file_content_b64=file_content_b64,
        )
        request_log.info("Document processing task queued", task_id=task.id)
    except Exception as e:
        request_log.error("Failed to queue document processing task", error=e, exc_info=True)
         # Intentar marcar el documento como error si ya se creó
        try:
            await postgres_client.update_document_status(document_id, schemas.DocumentStatus.ERROR, error_message="Failed to queue processing task")
        except: pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to queue document for processing.",
        )

    return schemas.IngestResponse(document_id=document_id)


@router.get(
    "/ingest/status/{document_id}",
    response_model=schemas.StatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Get document ingestion status",
    description="Retrieves the current processing status and basic information of a document.",
)
async def get_ingestion_status(
    document_id: uuid.UUID,
    company_id: uuid.UUID = Depends(get_current_company_id), # Asegurar que el usuario pertenece a la compañía dueña
):
    """
    Endpoint para consultar el estado de procesamiento de un documento.
    """
    status_log = log.bind(document_id=str(document_id), company_id=str(company_id))
    status_log.info("Received request for document status")

    try:
        doc_data = await postgres_client.get_document_status(document_id)
    except Exception as e:
        status_log.error("Failed to retrieve document status from DB", error=e, exc_info=True)
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

    # Aquí deberíamos verificar si el company_id del token coincide con el del documento
    # doc_company_id = doc_data.get("company_id") # Asumiendo que get_document_status lo devuelve
    # if doc_company_id != company_id:
    #     status_log.warning("Company ID mismatch for document status request")
    #     raise HTTPException(
    #         status_code=status.HTTP_403_FORBIDDEN,
    #         detail="You do not have permission to view this document's status.",
    #     )

    response_data = schemas.StatusResponse(
        document_id=doc_data["id"],
        status=doc_data["status"],
        file_name=doc_data.get("file_name"),
        file_type=doc_data.get("file_type"),
        chunk_count=doc_data.get("chunk_count"),
        error_message=doc_data.get("error_message"),
        last_updated=str(doc_data.get("updated_at")) if doc_data.get("updated_at") else None,
    )

    # Añadir un mensaje más descriptivo
    if response_data.status == schemas.DocumentStatus.ERROR:
        response_data.message = f"Processing failed: {response_data.error_message or 'Unknown error'}"
    elif response_data.status == schemas.DocumentStatus.PROCESSED:
         response_data.message = f"Document processed successfully with {response_data.chunk_count or 0} chunks."
    elif response_data.status == schemas.DocumentStatus.PROCESSING:
         response_data.message = "Document is currently being processed."
    elif response_data.status == schemas.DocumentStatus.UPLOADED:
         response_data.message = "Document uploaded, awaiting processing."

    status_log.info("Returning document status", status=response_data.status)
    return response_data