# ingest-service/app/api/v1/endpoints/ingest.py
import uuid
from typing import Dict, Any, Optional, List
import json
import structlog
import io
import asyncio
from milvus_haystack import MilvusDocumentStore # Asegúrate que la importación es correcta
import traceback

from fastapi import (
    APIRouter, UploadFile, File, Depends, HTTPException,
    status, Form, Header, Query, Request, Response
)
from minio.error import S3Error
from pydantic import ValidationError

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
    milvus_log = log.bind(document_id=str(document_id))
    def _count_chunks():
        try:
            store = MilvusDocumentStore(
                connection_args={"uri": str(settings.MILVUS_URI)},
                collection_name=settings.MILVUS_COLLECTION_NAME,
                search_params=settings.MILVUS_SEARCH_PARAMS, # Esencial si usas búsquedas, aunque aquí solo contamos
                consistency_level="Strong", # Para asegurar lectura de datos recién escritos
                # dim=settings.EMBEDDING_DIMENSION, # Añadir si la colección puede no existir
            )
            # Filtrar por document_id usando los metadatos
            docs = store.get_all_documents(filters={"document_id": str(document_id)})
            return len(docs or [])
        except Exception as e:
            milvus_log.error("Error connecting to or querying Milvus in get_milvus_chunk_count", error=str(e), exc_info=True)
            return 0 # Devuelve 0 en caso de error para no bloquear

    try:
        milvus_log.debug("Executing Milvus count via executor")
        count = await loop.run_in_executor(None, _count_chunks)
        milvus_log.debug("Milvus count execution finished", count=count)
        return count
    except Exception as e:
        milvus_log.error("Executor error in get_milvus_chunk_count", error=str(e))
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
        # Asegurarse que doc['file_name'] existe antes de comparar
        db_filename = doc.get("file_name")
        db_status = doc.get("status")
        if db_filename and file.filename and db_filename == file.filename and db_status != DocumentStatus.ERROR.value:
            request_log.warning("Intento de subida duplicada detectado", filename=file.filename, status=db_status)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Ya existe un documento con el mismo nombre en estado '{db_status}'. Elimina o reintenta el anterior antes de subir uno nuevo.")

    minio_client = MinioStorageClient()
    document_id = uuid.uuid4() # Generar ID antes para consistencia
    request_log = request_log.bind(document_id=str(document_id))
    try:
        # 1) Persistir registro inicial en DB
        await postgres_client.create_document(document_id, company_id, file.filename, file.content_type, metadata)

        # 2) Subir archivo a MinIO
        file_bytes = await file.read()
        file_stream = io.BytesIO(file_bytes)
        object_name = await minio_client.upload_file(company_id, document_id, file.filename, file_stream, file.content_type, len(file_bytes))

        # 3) Actualizar file_path en DB
        await postgres_client.update_document_status(document_id, DocumentStatus.UPLOADED, file_path=object_name)

        # 4) Encolar tarea Celery
        task = process_document_haystack_task.delay(
            str(document_id), str(company_id), object_name, file.filename, file.content_type, metadata
        )
        request_log.info("Document ingestion task queued successfully", task_id=task.id)
        # El estado 'processing' será actualizado por la tarea al empezar, pero podemos devolverlo ya
        return schemas.IngestResponse(document_id=document_id, task_id=task.id, status=DocumentStatus.PROCESSING.value, message="Document upload received and queued for processing.")

    except HTTPException as http_exc:
        raise http_exc # Re-raise known HTTP exceptions
    except Exception as e:
        request_log.exception("Failed during initial document creation/upload")
        # Attempt to cleanup if DB record was created but upload failed
        if await postgres_client.get_document_status(document_id):
             await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=f"Failed during initial upload: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate document ingestion: {e}")


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
        record = await postgres_client.get_document_status(document_id)
        if not record:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Documento no encontrado.")
        if record.get("company_id") != company_id:
            status_log.warning("Attempt to access document status from another company", owner_company=str(record.get('company_id')))
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Documento no encontrado.")

        # Crear diccionario base
        enriched_data = dict(record)

        # ---- Enriquecimiento ----
        # Parsear metadata si es string
        if isinstance(enriched_data.get('metadata'), str):
            try:
                enriched_data['metadata'] = json.loads(enriched_data['metadata'])
            except json.JSONDecodeError:
                status_log.warning("Failed to parse metadata JSON string from DB", raw_metadata=enriched_data['metadata'])
                enriched_data['metadata'] = {"error": "invalid metadata format in DB"}

        # Verificar existencia real en MinIO
        file_path = enriched_data.get("file_path")
        minio_exists_check = False # Default a False
        if file_path:
            try:
                minio_client = MinioStorageClient()
                minio_exists_check = await minio_client.file_exists(file_path)
                status_log.debug("MinIO existence check result", object_path=file_path, exists=minio_exists_check)
            except Exception as minio_err:
                status_log.error("Error checking file existence in MinIO", object_path=file_path, error=str(minio_err))
        else:
            status_log.warning("Document has no file_path in DB, cannot check MinIO.")
        enriched_data['minio_exists'] = minio_exists_check

        # Contar chunks en Milvus
        status_log.debug("Checking chunk count in Milvus...")
        milvus_count = await get_milvus_chunk_count(document_id)
        status_log.debug("Milvus chunk count result", count=milvus_count)
        enriched_data['milvus_chunk_count'] = milvus_count
        enriched_data['chunk_count'] = milvus_count # Actualizar chunk_count con el valor real de Milvus

        # Actualizar estado si Milvus tiene chunks pero DB no lo refleja
        db_status = DocumentStatus(enriched_data["status"])
        if milvus_count > 0 and db_status != DocumentStatus.PROCESSED and db_status != DocumentStatus.ERROR:
             status_log.warning("DB status mismatch, chunks found in Milvus. Updating DB.", db_status=db_status.value, milvus_count=milvus_count)
             await postgres_client.update_document_status(document_id, DocumentStatus.PROCESSED, chunk_count=milvus_count)
             enriched_data["status"] = DocumentStatus.PROCESSED.value

        # Generar mensaje descriptivo basado en el estado *actualizado*
        current_status = DocumentStatus(enriched_data["status"])
        status_messages = {
            DocumentStatus.UPLOADED: "Document uploaded, awaiting processing.",
            DocumentStatus.PROCESSING: "Document is currently being processed.",
            DocumentStatus.PROCESSED: "Document processed successfully.",
            DocumentStatus.INDEXED: "Document processed and indexed.",
            DocumentStatus.ERROR: f"Processing error: {enriched_data.get('error_message') or 'Unknown error'}",
        }
        enriched_data['message'] = status_messages.get(current_status, "Unknown status.")

        # ---- Validación Final ----
        try:
            response_data = schemas.StatusResponse.model_validate(enriched_data)
            status_log.info("Returning detailed document status", status=response_data.status, minio_exists=response_data.minio_exists, milvus_chunks=response_data.milvus_chunk_count)
            return response_data
        except ValidationError as val_err:
            status_log.error("Final validation failed after enrichment", errors=val_err.errors(), data_validated=enriched_data)
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to format final document status.")

    except HTTPException as http_exc: raise http_exc
    except Exception as e:
        status_log.exception("Error retrieving detailed document status")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve detailed document status.")


@router.get(
    "/status",
    response_model=List[schemas.StatusResponse],
    status_code=status.HTTP_200_OK,
    summary="Listar estados de ingesta para la compañía (con estado real)",
    description="Recupera lista de documentos con verificación en MinIO y conteo real de Milvus en paralelo.",
)
async def list_ingestion_statuses(
    company_id: uuid.UUID = Depends(get_current_company_id),
    limit: int = Query(default=30, ge=1, le=500), # Default 30 según logs
    offset: int = Query(default=0, ge=0),
    request: Request = None
):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4())) if request else str(uuid.uuid4())
    list_log = log.bind(request_id=request_id, company_id=str(company_id), limit=limit, offset=offset)
    list_log.info("Listing document statuses with real-time checks")
    try:
        records = await postgres_client.list_documents_by_company(company_id, limit=limit, offset=offset)
        if not records:
             list_log.info("No documents found for this company.")
             return []

        # Función para enriquecer y validar cada status en paralelo
        async def enrich_and_validate(record: Dict[str, Any]) -> Optional[schemas.StatusResponse]:
            doc_id = record.get("id")
            enrich_log = list_log.bind(document_id=str(doc_id))
            enriched_data = dict(record) # Copiar para modificar

            # Parsear metadata
            if isinstance(enriched_data.get('metadata'), str):
                try:
                    enriched_data['metadata'] = json.loads(enriched_data['metadata'])
                except json.JSONDecodeError:
                    enrich_log.warning("Failed to parse metadata JSON string from DB", raw_metadata=enriched_data['metadata'])
                    enriched_data['metadata'] = {"error": "invalid metadata format in DB"}

            # Verificar MinIO
            file_path = enriched_data.get("file_path")
            try:
                enriched_data['minio_exists'] = await MinioStorageClient().file_exists(file_path) if file_path else False
            except Exception as ex:
                enrich_log.error("MinIO check failed during list enrichment", error=str(ex))
                enriched_data['minio_exists'] = False

            # Contar chunks en Milvus
            try:
                count = await get_milvus_chunk_count(doc_id)
                enriched_data['milvus_chunk_count'] = count
                enriched_data['chunk_count'] = count # Actualizar con valor real

                # Actualizar estado si es inconsistente
                db_status = DocumentStatus(enriched_data["status"])
                if count > 0 and db_status != DocumentStatus.PROCESSED and db_status != DocumentStatus.ERROR:
                     enrich_log.warning("DB status mismatch during list, chunks found in Milvus. Updating DB.", db_status=db_status.value, milvus_count=count)
                     await postgres_client.update_document_status(doc_id, DocumentStatus.PROCESSED, chunk_count=count)
                     enriched_data["status"] = DocumentStatus.PROCESSED.value

            except Exception as ex:
                enrich_log.error("Milvus check failed during list enrichment", error=str(ex))
                enriched_data['milvus_chunk_count'] = -1 # Indicar error en conteo

            # Generar mensaje
            current_status = DocumentStatus(enriched_data["status"])
            status_messages = {
                DocumentStatus.UPLOADED: "Document uploaded, awaiting processing.",
                DocumentStatus.PROCESSING: "Document is currently being processed.",
                DocumentStatus.PROCESSED: "Document processed successfully.",
                DocumentStatus.INDEXED: "Document processed and indexed.",
                DocumentStatus.ERROR: f"Processing error: {enriched_data.get('error_message') or 'Unknown error'}",
            }
            enriched_data['message'] = status_messages.get(current_status, "Unknown status.")

            # Validar finalmente
            try:
                return schemas.StatusResponse.model_validate(enriched_data)
            except ValidationError as val_err:
                enrich_log.error("Final validation failed during list enrichment", errors=val_err.errors(), data_validated=enriched_data)
                # Loguear el error de validación que se veía en los logs originales
                list_log.error(
                     "Error validating base status", # Mismo mensaje de log que antes
                     doc_id=enriched_data.get("id"),
                     error=str(val_err) # Formato similar al log original
                 )
                return None # Excluir de la lista final si la validación falla

        # Lanzar enriquecimiento en paralelo
        tasks = [asyncio.create_task(enrich_and_validate(record)) for record in records]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Filtrar resultados válidos
        final_list: List[schemas.StatusResponse] = []
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                list_log.error("Unhandled exception during status enrichment task", document_id=str(records[i].get('id')), error=str(res), tb=traceback.format_exc())
            elif res is not None: # Si no es None (validación exitosa)
                final_list.append(res)
            # Si res es None, ya se logueó el error de validación dentro de enrich_and_validate

        list_log.info("Returning enriched statuses", count=len(final_list))
        return final_list
    except Exception as e:
        list_log.exception("Error listing enriched statuses")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error retrieving document statuses.")


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
    try:
        doc_company_id = uuid.UUID(str(doc.get("company_id")))
    except (ValueError, TypeError):
        retry_log.error("Invalid company_id format found in DB for document", doc_id=str(document_id))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error interno al verificar documento.")

    if doc_company_id != company_id:
        retry_log.warning("Attempt to retry document from another company", owner_company=str(doc_company_id))
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Documento no encontrado.")

    if doc.get("status") != DocumentStatus.ERROR.value:
        retry_log.warning("Retry attempt on document not in 'error' state", current_status=doc.get("status"))
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Solo se puede reintentar la ingesta si el estado es 'error'.")

    if not doc.get("file_path"):
        retry_log.error("Cannot retry document without a valid file_path in DB", doc_id=str(document_id))
        await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message="Cannot retry: Original file path missing.")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No se puede reintentar: falta la ruta del archivo original.")

    # Preparar metadata para la tarea Celery
    metadata_to_pass = doc.get("metadata", {})
    # Asegurarse que es un dict, si no, usar un dict vacío
    if not isinstance(metadata_to_pass, dict):
        if isinstance(metadata_to_pass, str):
             try: metadata_to_pass = json.loads(metadata_to_pass)
             except: metadata_to_pass = {}
        else: metadata_to_pass = {}

    # 2. Reencolar la tarea Celery
    try:
        task = process_document_haystack_task.delay(
            document_id_str=str(document_id),
            company_id_str=str(company_id),
            minio_object_name=doc["file_path"],
            file_name=doc["file_name"],
            content_type=doc["file_type"],
            original_metadata=metadata_to_pass
        )
        retry_log.info("Retry ingestion task queued", task_id=task.id)
    except Exception as celery_err:
        retry_log.exception("Failed to queue Celery retry task")
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
        pass # Continuar y devolver la respuesta 202

    return schemas.IngestResponse(document_id=document_id, task_id=task.id, status=DocumentStatus.PROCESSING.value, message="Reintento de ingesta encolado correctamente.")


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Eliminar un documento",
    description="Elimina completamente un documento (registro DB, archivo MinIO, chunks Milvus)."
)
async def delete_document_endpoint(
    document_id: uuid.UUID,
    company_id: uuid.UUID = Depends(get_current_company_id),
    request: Request = None
):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4())) if request else str(uuid.uuid4())
    delete_log = log.bind(request_id=request_id, document_id=str(document_id), company_id=str(company_id))
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

    milvus_deleted = False
    minio_deleted = False
    postgres_deleted = False
    errors = []

    # 2. Eliminar chunks de Milvus
    delete_log.info("Attempting to delete chunks from Milvus...")
    try:
        loop = asyncio.get_running_loop()
        store = MilvusDocumentStore(
            connection_args={"uri": str(settings.MILVUS_URI)},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            # No need for index/search params for deletion
            consistency_level="Strong",
        )
        await loop.run_in_executor(None, lambda: store.delete_documents(filters={"document_id": str(document_id)}))
        milvus_deleted = True
        delete_log.info("Successfully deleted chunks from Milvus.")
    except Exception as milvus_err:
        error_msg = f"Failed to delete chunks from Milvus: {milvus_err}"
        delete_log.error(error_msg)
        errors.append(error_msg)

    # 3. Eliminar archivo de MinIO
    file_path = doc.get("file_path")
    if file_path:
        delete_log.info("Attempting to delete file from MinIO...")
        try:
            minio_client = MinioStorageClient()
            await minio_client.delete_file(file_path)
            minio_deleted = True
            delete_log.info("Successfully deleted file from MinIO.")
        except Exception as minio_err:
            error_msg = f"Failed to delete file from MinIO: {minio_err}"
            delete_log.error(error_msg)
            errors.append(error_msg)
    else:
        delete_log.warning("No file_path found in DB, skipping MinIO deletion.")
        minio_deleted = True # Consider it "deleted" if there was nothing to delete

    # 4. Eliminar registro de PostgreSQL (Solo si MinIO y Milvus tuvieron éxito o no eran necesarios)
    #    Opcionalmente, podrías borrarlo siempre, pero dejar huérfanos en MinIO/Milvus
    if minio_deleted and milvus_deleted:
        delete_log.info("Attempting to delete record from PostgreSQL...")
        try:
            deleted = await postgres_client.delete_document(document_id)
            if not deleted:
                 error_msg = "Failed to delete document from PostgreSQL (record not found during deletion)."
                 delete_log.error(error_msg)
                 errors.append(error_msg)
            else:
                 postgres_deleted = True
                 delete_log.info("Document record deleted successfully from PostgreSQL")
        except Exception as db_err:
            error_msg = f"Error deleting document record from PostgreSQL: {db_err}"
            delete_log.exception(error_msg)
            errors.append(error_msg)
    else:
        error_msg = "Skipping PostgreSQL deletion because MinIO or Milvus deletion failed."
        delete_log.warning(error_msg)
        errors.append(error_msg)

    # Si hubo errores, devolver 500, si no, 204
    if errors:
        delete_log.error("Document deletion completed with errors", errors=errors)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Document deletion failed for some components: {'; '.join(errors)}")

    delete_log.info("Document deleted successfully from all components.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)