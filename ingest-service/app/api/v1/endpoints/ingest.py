# ./app/api/v1/endpoints/ingest.py (AÑADIDO endpoint GET /ingest/status)
import uuid
from typing import Dict, Any, Optional, List # Añadir List
import json
import structlog
import io

from fastapi import APIRouter, UploadFile, File, Depends, HTTPException, status, Form, Header
from minio.error import S3Error # Importar S3Error

from app.api.v1 import schemas # Importar schemas
from app.core.config import settings
from app.db import postgres_client
from app.models.domain import DocumentStatus # Importar DocumentStatus
from app.tasks.process_document import process_document_haystack_task
from app.services.minio_client import MinioStorageClient

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Dependency for Company ID ---
async def get_current_company_id(x_company_id: Optional[str] = Header(None)) -> uuid.UUID:
    """Obtiene y valida el X-Company-ID del header."""
    if not x_company_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Company-ID header",
        )
    try:
        # Validar que es un UUID válido
        company_uuid = uuid.UUID(x_company_id)
        # Podrías añadir aquí una verificación contra un servicio de Auth/Tenant si fuera necesario
        log.debug("Validated X-Company-ID", company_id=str(company_uuid))
        return company_uuid
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
    summary="Ingestar un nuevo documento",
    description="Sube un documento, lo almacena, crea un registro en BD y lo encola para procesamiento Haystack.",
)
async def ingest_document_haystack(
    metadata_json: str = Form(default="{}", description="String JSON de metadatos del documento"),
    file: UploadFile = File(..., description="El archivo del documento a ingestar"),
    company_id: uuid.UUID = Depends(get_current_company_id),
):
    """
    Endpoint para iniciar la ingesta de documentos usando pipeline Haystack.
    1. Valida input y metadatos.
    2. Sube el archivo a MinIO.
    3. Crea el registro inicial en PostgreSQL.
    4. Encola la tarea de procesamiento Haystack en Celery.
    """
    request_log = log.bind(company_id=str(company_id), filename=file.filename, content_type=file.content_type)
    request_log.info("Received document ingestion request (Haystack)")

    # 1. Validate Content Type
    if not file.content_type or file.content_type not in settings.SUPPORTED_CONTENT_TYPES:
        request_log.warning("Unsupported or missing content type received", received_type=file.content_type)
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type: {file.content_type or 'Unknown'}. Supported types: {settings.SUPPORTED_CONTENT_TYPES}",
        )
    content_type = file.content_type # Use validated type

    # 2. Validate Metadata
    try:
        metadata = json.loads(metadata_json)
        if not isinstance(metadata, dict):
            raise ValueError("Metadata must be a JSON object")
    except json.JSONDecodeError:
         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON format for metadata")
    except ValueError as e: # Catch our specific ValueError
         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e: # Catch any other parsing errors
         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid metadata: {e}")

    minio_client = MinioStorageClient() # Initialize MinIO client
    minio_object_name: Optional[str] = None
    document_id: Optional[uuid.UUID] = None
    task_id: Optional[str] = None

    try:
        # 3. Create initial DB record FIRST to get document_id
        document_id = await postgres_client.create_document(
            company_id=company_id,
            file_name=file.filename or "untitled",
            file_type=content_type,
            metadata=metadata,
        )
        request_log = request_log.bind(document_id=str(document_id))
        request_log.info("Initial document record created in DB")

        # 4. Upload to MinIO using document_id in the object name
        request_log.info("Uploading file to MinIO...")
        file_content = await file.read() # Read content
        content_length = len(file_content)
        if content_length == 0:
            # Marcar como error inmediatamente si el archivo está vacío
            await postgres_client.update_document_status(
                document_id=document_id,
                status=DocumentStatus.ERROR,
                error_message="Uploaded file is empty."
            )
            request_log.error("Uploaded file is empty. Marked as error.")
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file cannot be empty.")

        file_stream = io.BytesIO(file_content) # Create stream
        minio_object_name = await minio_client.upload_file(
            company_id=company_id,
            document_id=document_id, # Use the generated ID
            file_name=file.filename or "untitled",
            file_content_stream=file_stream,
            content_type=content_type,
            content_length=content_length
        )
        request_log.info("File uploaded successfully to MinIO", object_name=minio_object_name)

        # 5. Update DB record with MinIO path (object name)
        await postgres_client.update_document_status(
            document_id=document_id,
            status=DocumentStatus.UPLOADED, # Keep as UPLOADED until task starts
            file_path=minio_object_name # Store the object name
        )
        request_log.info("Document record updated with MinIO object name")

        # 6. Enqueue Celery Task with MinIO object name
        task = process_document_haystack_task.delay(
            document_id_str=str(document_id),
            company_id_str=str(company_id),
            minio_object_name=minio_object_name, # Pass object name
            file_name=file.filename or "untitled",
            content_type=content_type,
            original_metadata=metadata,
        )
        task_id = task.id
        request_log.info("Haystack document processing task queued", task_id=task_id)

        # Devolver el ID del documento y el ID de la tarea Celery
        return schemas.IngestResponse(
            document_id=document_id,
            task_id=task_id,
            status=DocumentStatus.UPLOADED, # El estado inicial devuelto es UPLOADED
            message="Document upload received and queued for processing."
        )

    except HTTPException as http_exc:
        # Re-raise HTTP exceptions (like validation errors) directly
        raise http_exc
    except S3Error as s3_err: # Catch MinIO errors specifically
        request_log.error("MinIO S3 Error during ingestion trigger", error=str(s3_err), code=s3_err.code, exc_info=True)
        if document_id: # Try to mark as error if record was created
            try:
                await postgres_client.update_document_status(
                    document_id, DocumentStatus.ERROR, error_message=f"Ingestion API Error: S3 Error ({s3_err.code})"
                )
            except Exception as db_err: request_log.error("Failed to mark document as error after S3 failure", nested_error=str(db_err))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Storage service error: {s3_err.code}")
    except Exception as e:
        request_log.error("Unexpected error during ingestion trigger", error=str(e), exc_info=True)
        if document_id: # Try to mark as error if record was created
            try:
                await postgres_client.update_document_status(
                    document_id, DocumentStatus.ERROR, error_message=f"Ingestion API Error: {type(e).__name__}: {str(e)[:250]}"
                )
            except Exception as db_err: request_log.error("Failed to mark document as error after API failure", nested_error=str(db_err))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to process ingestion request.")
    finally:
        await file.close()


@router.get(
    "/ingest/status/{document_id}",
    response_model=schemas.StatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Consultar estado de ingesta de un documento",
    description="Recupera el estado actual del procesamiento y la información básica de un documento específico.",
)
async def get_ingestion_status(
    document_id: uuid.UUID,
    company_id: uuid.UUID = Depends(get_current_company_id),
):
    """
    Endpoint para consultar el estado de procesamiento de un documento.
    Verifica que la compañía que solicita sea la propietaria del documento.
    """
    status_log = log.bind(document_id=str(document_id), company_id=str(company_id))
    status_log.info("Received request for single document status")

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
    # La función get_document_status ahora devuelve company_id
    if doc_data.get("company_id") != company_id:
        status_log.warning("Company ID mismatch for document status request", owner_company_id=doc_data.get("company_id"))
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this document's status.",
        )

    # Mapear datos de DB al modelo de respuesta Pydantic
    # Pydantic manejará la conversión de tipos (ej: DB status string a Enum) si coinciden
    try:
        response_data = schemas.StatusResponse(
            document_id=doc_data["id"],
            status=doc_data["status"], # Conversión str -> Enum automática
            file_name=doc_data.get("file_name"),
            file_type=doc_data.get("file_type"),
            chunk_count=doc_data.get("chunk_count"),
            error_message=doc_data.get("error_message"),
            last_updated=doc_data.get("updated_at"), # Pydantic maneja datetime
        )
    except Exception as pydantic_err: # Capturar error de validación de Pydantic si el estado de la DB es inválido
        status_log.error("Failed to map DB data to StatusResponse schema", db_data=doc_data, error=str(pydantic_err), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error processing document status data."
        )


    # Añadir mensaje descriptivo basado en el estado (opcional pero útil)
    status_messages = {
        DocumentStatus.UPLOADED: "Documento subido, esperando procesamiento.",
        DocumentStatus.PROCESSING: "Documento actualmente en procesamiento por el pipeline Haystack.",
        DocumentStatus.PROCESSED: f"Documento procesado exitosamente con {response_data.chunk_count or 0} chunks indexados.",
        DocumentStatus.ERROR: f"Procesamiento falló: {response_data.error_message or 'Error desconocido'}",
        DocumentStatus.INDEXED: f"Documento procesado e indexado exitosamente con {response_data.chunk_count or 0} chunks.", # Si usas INDEXED
    }
    response_data.message = status_messages.get(response_data.status, "Estado desconocido.")

    status_log.info("Returning document status", status=response_data.status)
    return response_data

# --- NUEVO ENDPOINT ---
@router.get(
    "/ingest/status", # Ruta SIN document_id
    response_model=List[schemas.StatusResponse], # Devuelve una LISTA de StatusResponse
    status_code=status.HTTP_200_OK,
    summary="Listar estados de ingesta para la compañía",
    description="Recupera una lista de todos los documentos y sus estados de procesamiento para la compañía actual, ordenados por fecha de actualización.",
)
async def list_ingestion_statuses(
    company_id: uuid.UUID = Depends(get_current_company_id),
    # Podríamos añadir parámetros de paginación aquí en el futuro (limit, offset)
):
    """
    Endpoint para listar los estados de todos los documentos de una compañía.
    """
    list_log = log.bind(company_id=str(company_id))
    list_log.info("Received request to list document statuses for company")

    try:
        # Llamar a la nueva función del cliente de base de datos
        documents_data = await postgres_client.list_documents_by_company(company_id)
    except Exception as e:
        list_log.error("Failed to retrieve document list from DB", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not retrieve document list.",
        )

    # FastAPI/Pydantic pueden convertir automáticamente la lista de dicts a List[StatusResponse]
    # si los nombres de campo coinciden. No necesitamos añadir el 'message' aquí.
    # Si hubiera problemas de validación, mapearíamos explícitamente:
    # response_list = [schemas.StatusResponse(**doc_data) for doc_data in documents_data]
    # return response_list

    list_log.info(f"Returning status list for {len(documents_data)} documents")
    return documents_data # Devolver directamente la lista de dicts