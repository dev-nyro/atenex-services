# ingest-service/app/api/v1/endpoints/ingest.py
import uuid
from typing import Dict, Any, Optional, List
import json
import structlog
import io
import asyncio
from milvus_haystack import MilvusDocumentStore # Asegúrate que la importación es correcta

from fastapi import (
    APIRouter, UploadFile, File, Depends, HTTPException,
    status, Form, Header, Query, Request, Response
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

# Helper para obtención dinámica de estado en Milvus (Se mantiene para el endpoint individual)
async def get_milvus_chunk_count(document_id: uuid.UUID) -> int:
    """Cuenta los chunks indexados en Milvus para un documento específico."""
    loop = asyncio.get_running_loop()
    def _count_chunks():
        # Inicializar conexión con Milvus
        try:
            # Asegúrate que MilvusDocumentStore se inicializa correctamente aquí
            store = MilvusDocumentStore(
                connection_args={"uri": str(settings.MILVUS_URI)},
                collection_name=settings.MILVUS_COLLECTION_NAME,
                search_params=settings.MILVUS_SEARCH_PARAMS, # Esencial si usas búsquedas, aunque aquí solo contamos
                consistency_level="Strong", # Para asegurar lectura de datos recién escritos
                # Añadir dimension si la colección podría no existir y debe crearse implicitamente
                # dim=settings.EMBEDDING_DIMENSION,
            )
            # Filtrar por document_id usando los metadatos
            # Ajusta el filtro según cómo almacenas document_id en los metadatos de Milvus
            # Asumiendo que 'document_id' es un campo en los metadatos
            docs = store.get_all_documents(filters={"document_id": str(document_id)})
            return len(docs or [])
        except Exception as e:
            log.error("Error connecting to or querying Milvus in get_milvus_chunk_count", document_id=str(document_id), error=str(e), exc_info=True)
            return 0 # Devuelve 0 en caso de error para no bloquear

    try:
        # Ejecuta la función síncrona en el executor
        return await loop.run_in_executor(None, _count_chunks)
    except Exception as e:
        log.error("Executor error in get_milvus_chunk_count", document_id=str(document_id), error=str(e))
        return 0

# --- Dependencias (Sin cambios) ---
async def get_current_company_id(x_company_id: Optional[str] = Header(None, alias="X-Company-ID")) -> uuid.UUID:
    if not x_company_id:
        log.warning("Missing required X-Company-ID header")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")
    try:
        return uuid.UUID(x_company_id)
    except ValueError:
        log.warning("Invalid UUID format in X-Company-ID header", header_value=x_company_id)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid X-Company-ID header format")

async def get_current_user_id(x_user_id: Optional[str] = Header(None, alias="X-User-ID")) -> uuid.UUID:
    if not x_user_id:
        log.warning("Missing required X-User-ID header")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-User-ID")
    try:
        return uuid.UUID(x_user_id)
    except ValueError:
        log.warning("Invalid UUID format in X-User-ID header", header_value=x_user_id)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid X-User-ID header format")

# --- Endpoints ---

@router.post(
    "/upload",
    response_model=schemas.IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingestar un nuevo documento",
    description="Sube un documento via API Gateway. Usa X-Company-ID y X-User-ID.",
)
async def ingest_document_haystack(
    company_id: uuid.UUID = Depends(get_current_company_id),
    user_id: uuid.UUID = Depends(get_current_user_id),
    metadata_json: str = Form(default="{}", description="String JSON de metadatos opcionales"),
    file: UploadFile = File(..., description="El archivo a ingestar"),
    request: Request = None
):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4())) if request else str(uuid.uuid4())
    request_log = log.bind(
        request_id=request_id,
        company_id=str(company_id),
        user_id=str(user_id),
        filename=file.filename,
        content_type=file.content_type
    )
    request_log.info("Processing document ingestion request from gateway")

    content_type = file.content_type or "application/octet-stream"
    if content_type not in settings.SUPPORTED_CONTENT_TYPES:
        request_log.warning("Unsupported file type received", received_type=content_type)
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=f"Unsupported file type: '{content_type}'. Supported: {settings.SUPPORTED_CONTENT_TYPES}")
    try:
        metadata = json.loads(metadata_json)
        if not isinstance(metadata, dict): raise ValueError("Metadata is not a JSON object")
    except (json.JSONDecodeError, ValueError) as json_err:
        request_log.warning("Invalid metadata JSON received", error=str(json_err))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid JSON format for metadata: {json_err}")

    existing_docs = await postgres_client.list_documents_by_company(company_id, limit=1000, offset=0)
    for doc in existing_docs:
        if doc["file_name"] == file.filename and doc["status"] != DocumentStatus.ERROR.value:
            request_log.warning("Intento de subida duplicada detectado", filename=file.filename, status=doc["status"])
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Ya existe un documento con el mismo nombre en estado '{doc['status']}'. Elimina o reintenta el anterior antes de subir uno nuevo.")

    minio_client = None; minio_object_name = None; document_id = None; task_id = None
    try:
        document_id = await postgres_client.create_document(
            company_id=company_id,
            file_name=file.filename or "untitled",
            file_type=content_type,
            metadata=metadata
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

        task = process_document_haystack_task.delay(
            document_id_str=str(document_id),
            company_id_str=str(company_id),
            minio_object_name=minio_object_name,
            file_name=file.filename or "untitled",
            content_type=content_type,
            original_metadata=metadata
        )
        task_id = task.id
        request_log.info("Haystack processing task queued", task_id=task_id)
        return schemas.IngestResponse(document_id=document_id, task_id=task_id, status=DocumentStatus.UPLOADED, message="Document received and queued.")

    except HTTPException as http_exc: raise http_exc
    except (IOError, S3Error) as storage_err:
        request_log.error("Storage error during upload", error=str(storage_err))
        if document_id: await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=f"Storage upload failed: {storage_err}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Storage service error.")
    except Exception as e:
        request_log.exception("Unexpected error during document ingestion")
        if document_id: await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=f"Ingestion error: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error during ingestion.")
    finally:
        if file: await file.close()

@router.get(
    "/status/{document_id}",
    response_model=schemas.StatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Consultar estado de ingesta de un documento",
    description="Recupera el estado de procesamiento de un documento específico, incluyendo verificación real en MinIO y Milvus.",
)
async def get_ingestion_status(
    document_id: uuid.UUID,
    company_id: uuid.UUID = Depends(get_current_company_id),
    request: Request = None
):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4())) if request else str(uuid.uuid4())
    status_log = log.bind(request_id=request_id, document_id=str(document_id), company_id=str(company_id))
    status_log.info("Received request for single document status")
    try:
        doc_data = await postgres_client.get_document_status(document_id)
        if not doc_data:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
        if doc_data.get("company_id") != company_id:
            status_log.warning("Attempt to access document status from another company", owner_company=str(doc_data.get('company_id')))
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

        # Convertir a Pydantic ANTES de añadir campos extra
        response_data = schemas.StatusResponse.model_validate(doc_data)

        # Verificar existencia real en MinIO
        file_path = doc_data.get("file_path")
        minio_exists_check = False # Default a False
        if file_path:
            try:
                minio_client = MinioStorageClient()
                minio_exists_check = await minio_client.file_exists(file_path)
                status_log.info("MinIO existence check result", object_path=file_path, exists=minio_exists_check)
            except Exception as minio_err:
                status_log.error("Error checking file existence in MinIO", object_path=file_path, error=str(minio_err))
                # No relanzar error, solo reportar como no existente
        else:
            status_log.warning("Document has no file_path in DB, cannot check MinIO.")

        # Contar chunks en Milvus
        status_log.info("Checking chunk count in Milvus...")
        milvus_count = await get_milvus_chunk_count(document_id)
        status_log.info("Milvus chunk count result", count=milvus_count)

        # Asignar campos adicionales a la instancia Pydantic
        response_data.minio_exists = minio_exists_check
        response_data.milvus_chunk_count = milvus_count

        # Generar mensaje descriptivo basado en el estado de la DB
        status_messages = {
            DocumentStatus.UPLOADED: "Document uploaded, awaiting processing.",
            DocumentStatus.PROCESSING: "Document is currently being processed.",
            DocumentStatus.PROCESSED: "Document processed successfully.",
            DocumentStatus.INDEXED: "Document processed and indexed.", # Si se usa
            DocumentStatus.ERROR: f"Processing error: {doc_data.get('error_message') or 'Unknown error'}", # Usar error_message de doc_data
        }
        response_data.message = status_messages.get(DocumentStatus(doc_data["status"]), "Unknown status.")

        status_log.info("Returning detailed document status", status=response_data.status, minio_exists=response_data.minio_exists, milvus_chunks=response_data.milvus_chunk_count)
        return response_data

    except HTTPException as http_exc: raise http_exc
    except Exception as e:
        status_log.exception("Error retrieving detailed document status")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve detailed document status.")

@router.get(
    "/status",
    # Devolvemos una lista del mismo schema, pero minio_exists y milvus_chunk_count serán None
    response_model=List[schemas.StatusResponse],
    status_code=status.HTTP_200_OK,
    summary="Listar estados de ingesta para la compañía (desde DB)",
    description="Recupera una lista paginada de documentos y sus estados almacenados en la base de datos.",
)
async def list_ingestion_statuses(
    company_id: uuid.UUID = Depends(get_current_company_id),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    request: Request = None
):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4())) if request else str(uuid.uuid4())
    list_log = log.bind(request_id=request_id, company_id=str(company_id), limit=limit, offset=offset)
    list_log.info("Received request to list document statuses (fast, from DB)")
    try:
        # 1. Obtener documentos de PostgreSQL
        documents_data = await postgres_client.list_documents_by_company(company_id, limit=limit, offset=offset)

        response_list = []
        for doc_dict in documents_data:
            # 2. Validar/Mapear cada documento con el schema Pydantic
            # Los campos minio_exists y milvus_chunk_count serán None por defecto
            # ya que no los estamos consultando aquí.
            try:
                status_response = schemas.StatusResponse.model_validate(doc_dict)

                # 3. (Opcional pero recomendado) Añadir mensaje genérico basado en estado DB
                status_messages = {
                    DocumentStatus.UPLOADED: "Document uploaded, awaiting processing.",
                    DocumentStatus.PROCESSING: "Document is currently being processed.",
                    DocumentStatus.PROCESSED: "Document processed.", # Mensaje más genérico para la lista
                    DocumentStatus.INDEXED: "Document indexed.",    # Si se usa
                    DocumentStatus.ERROR: f"Processing error: {doc_dict.get('error_message') or 'Check details'}",
                }
                status_response.message = status_messages.get(status_response.status, "Unknown status.")

                response_list.append(status_response)
            except Exception as validation_err:
                list_log.error("Error validating document data from DB", doc_id=doc_dict.get("id"), error=str(validation_err), data=doc_dict)
                # Opcionalmente, podrías continuar y omitir este documento o retornar error 500

        list_log.info(f"Returning status list (from DB) for {len(response_list)} documents")
        return response_list
    except Exception as e:
        list_log.exception("Error listing document statuses from DB")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error listing document statuses.")


@router.post(
    "/retry/{document_id}",
    response_model=schemas.IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Reintentar la ingesta de un documento con error",
    description="Permite reintentar la ingesta de un documento que falló. Solo disponible si el estado es 'error'."
)
async def retry_document_ingest(
    document_id: uuid.UUID,
    company_id: uuid.UUID = Depends(get_current_company_id),
    user_id: uuid.UUID = Depends(get_current_user_id), # User ID necesario para lógica futura quizás
    request: Request = None
):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4())) if request else str(uuid.uuid4())
    retry_log = log.bind(request_id=request_id, company_id=str(company_id), user_id=str(user_id), document_id=str(document_id))
    retry_log.info("Received request to retry document ingestion")

    # 1. Buscar el documento y validar estado y pertenencia
    doc = await postgres_client.get_document_status(document_id)
    if not doc:
        retry_log.warning("Document not found for retry attempt")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Documento no encontrado.")
    # Convertir company_id de la DB (puede ser string) a UUID para comparar
    try:
        doc_company_id = uuid.UUID(str(doc.get("company_id")))
    except (ValueError, TypeError):
        retry_log.error("Invalid company_id format found in DB for document", doc_id=str(document_id))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error interno al verificar documento.")

    if doc_company_id != company_id:
        retry_log.warning("Attempt to retry document from another company", owner_company=str(doc_company_id))
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Documento no encontrado.")

    if doc["status"] != DocumentStatus.ERROR.value:
        retry_log.warning("Retry attempt on document not in 'error' state", current_status=doc["status"])
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Solo se puede reintentar la ingesta si el estado es 'error'.")

    # Asegurarse que file_path existe para poder reintentar
    if not doc.get("file_path"):
        retry_log.error("Cannot retry document without a valid file_path in DB", doc_id=str(document_id))
        await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message="Cannot retry: Original file path missing.")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No se puede reintentar: falta la ruta del archivo original.")

    # 2. Reencolar la tarea Celery
    try:
        task = process_document_haystack_task.delay(
            document_id_str=str(document_id),
            company_id_str=str(company_id),
            minio_object_name=doc["file_path"],
            file_name=doc["file_name"],
            content_type=doc["file_type"],
            original_metadata=doc.get("metadata", {}) # Usar metadata de la DB si existe
        )
        retry_log.info("Retry ingestion task queued", task_id=task.id)
    except Exception as celery_err:
        retry_log.exception("Failed to queue Celery retry task")
        # No cambiar estado en DB si Celery falló
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al encolar la tarea de reintento.")

    # 3. Actualizar estado a 'processing' en DB
    try:
        await postgres_client.update_document_status(
            document_id=document_id,
            status=DocumentStatus.PROCESSING,
            error_message=None # Limpiar mensaje de error anterior
        )
        retry_log.info("Document status updated to PROCESSING for retry.")
    except Exception as db_err:
        retry_log.exception("Failed to update document status to PROCESSING after queueing retry task")
        # La tarea está encolada, pero el estado no se actualizó. Es un estado inconsistente temporalmente.
        # Podría requerir lógica adicional de reconciliación o simplemente dejar que la tarea actualice al terminar.
        # Por simplicidad, devolvemos éxito ya que la tarea fue encolada.
        pass # Continuar y devolver la respuesta 202

    return schemas.IngestResponse(document_id=document_id, task_id=task.id, status=DocumentStatus.PROCESSING, message="Reintento de ingesta encolado correctamente.")


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Eliminar un documento",
    description="Elimina un documento y su registro de la BD."
)
async def delete_document_endpoint(
    document_id: uuid.UUID,
    company_id: uuid.UUID = Depends(get_current_company_id)
):
    delete_log = log.bind(document_id=str(document_id), company_id=str(company_id))
    delete_log.info("Received request to delete document")

    # 1. Validar existencia y pertenencia
    doc = await postgres_client.get_document_status(document_id)
    if not doc:
        delete_log.warning("Document not found for deletion")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Documento no encontrado.")
    try:
        doc_company_id = uuid.UUID(str(doc.get("company_id")))
    except (ValueError, TypeError):
        delete_log.error("Invalid company_id format in DB for document", doc_id=str(document_id))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error interno al verificar documento.")

    if doc_company_id != company_id:
        delete_log.warning("Attempt to delete document from another company", owner_company=str(doc_company_id))
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Documento no encontrado.")

    # 2. Eliminar chunks de Milvus
    delete_log.info("Attempting to delete chunks from Milvus...")
    try:
        loop = asyncio.get_running_loop()
        store = MilvusDocumentStore(
            connection_args={"uri": str(settings.MILVUS_URI)},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            search_params=settings.MILVUS_SEARCH_PARAMS,
            consistency_level="Strong",
        )
        # Ejecutar borrado en executor para no bloquear
        await loop.run_in_executor(None, lambda: store.delete_documents(filters={"document_id": str(document_id)}))
        delete_log.info("Successfully deleted chunks from Milvus.")
    except Exception as milvus_err:
        delete_log.error("Failed to delete chunks from Milvus", error=str(milvus_err))

    # 3. Eliminar archivo de MinIO
    file_path = doc.get("file_path")
    if file_path:
        delete_log.info("Attempting to delete file from MinIO...")
        try:
            minio_client = MinioStorageClient()
            await minio_client.delete_file(file_path)
            delete_log.info("Successfully deleted file from MinIO.")
        except Exception as minio_err:
            delete_log.error("Failed to delete file from MinIO", error=str(minio_err))

    # 4. Eliminar registro de PostgreSQL (Último paso)
    try:
        deleted = await postgres_client.delete_document(document_id)
        if not deleted:
            delete_log.error("Failed to delete document from PostgreSQL (was not found?)")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error eliminando registro del documento.")
        delete_log.info("Document record deleted successfully from PostgreSQL")
    except Exception as db_err:
        delete_log.exception("Error deleting document record from PostgreSQL")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error eliminando registro del documento.")

    return Response(status_code=status.HTTP_204_NO_CONTENT)