# ingest-service/app/api/v1/endpoints/ingest.py
import uuid
from typing import Dict, Any, Optional, List
import json
import structlog
import io
import asyncio
from milvus_haystack import MilvusDocumentStore
from pymilvus.exceptions import MilvusException
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

# Helper para obtención dinámica de estado en Milvus
async def get_milvus_chunk_count(document_id: uuid.UUID) -> int:
    """Cuenta los chunks indexados en Milvus para un documento específico."""
    loop = asyncio.get_running_loop()
    milvus_log = log.bind(document_id=str(document_id))
    def _count_chunks():
        store = None # Inicializar fuera del try
        try:
            milvus_log.info("Initializing Milvus store for counting...")
            # *** CORRECCIÓN DEFINITIVA: Usar embedding_dim aquí también ***
            store = MilvusDocumentStore(
                connection_args={"uri": str(settings.MILVUS_URI)},
                collection_name=settings.MILVUS_COLLECTION_NAME,
                embedding_dim=settings.EMBEDDING_DIMENSION, # CORREGIDO
                consistency_level="Strong",
            )
            milvus_log.info("Milvus store initialized for counting.")
            milvus_log.info("Counting documents in Milvus...")
            count = store.count_documents(filters={"document_id": str(document_id)})
            milvus_log.info("Milvus count result", count=count)
            return count
        except MilvusException as me:
             milvus_log.error("Milvus connection/query error in get_milvus_chunk_count", error=str(me), code=getattr(me, 'code', None), exc_info=True)
             return -1 # Indicar error en el conteo
        except Exception as e:
            # Captura el TypeError si el argumento es incorrecto u otros errores
            milvus_log.error(f"Error connecting to or querying Milvus in get_milvus_chunk_count: {type(e).__name__}", error=str(e), exc_info=True)
            return -1 # Indicar error en el conteo

    try:
        milvus_log.debug("Executing Milvus count via executor")
        count = await loop.run_in_executor(None, _count_chunks)
        milvus_log.debug("Milvus count execution finished", count=count)
        return count
    except Exception as e:
        milvus_log.error("Executor error in get_milvus_chunk_count", error=str(e))
        return -1 # Indicar error en el conteo

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

    # Verificar duplicados ANTES de crear registro DB
    existing_docs = await postgres_client.list_documents_by_company(company_id, limit=1000, offset=0)
    for doc in existing_docs:
        db_filename = doc.get("file_name")
        db_status = doc.get("status")
        if db_filename and file.filename and db_filename == file.filename and db_status != DocumentStatus.ERROR.value:
            request_log.warning("Intento de subida duplicada detectado", filename=file.filename, status=db_status)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Ya existe un documento con el mismo nombre en estado '{db_status}'. Elimina o reintenta el anterior antes de subir uno nuevo.")

    minio_client = MinioStorageClient()
    document_id = uuid.uuid4() # Generar ID antes
    request_log = request_log.bind(document_id=str(document_id))
    object_name = f"{str(company_id)}/{str(document_id)}/{file.filename}"

    try:
        # 1) Persistir registro inicial en DB (Estado UPLOADED)
        await postgres_client.create_document(document_id, company_id, file.filename, content_type, metadata)

        # 2) Subir archivo a MinIO
        file_bytes = await file.read()
        file_stream = io.BytesIO(file_bytes)
        await minio_client.upload_file(company_id, document_id, file.filename, file_stream, content_type, len(file_bytes))

        # 3) Actualizar file_path en DB (Status sigue UPLOADED)
        await postgres_client.update_document_status(document_id, DocumentStatus.UPLOADED, file_path=object_name)

        # 4) Encolar tarea Celery
        task = process_document_haystack_task.delay(
            str(document_id), str(company_id), object_name, file.filename, content_type, metadata
        )
        request_log.info("Document ingestion task queued successfully", task_id=task.id)

        # Devolver respuesta 202 con estado UPLOADED (el worker lo cambiará a PROCESSING)
        return schemas.IngestResponse(document_id=document_id, task_id=task.id, status=DocumentStatus.UPLOADED.value, message="Document upload received and queued for processing.")

    except HTTPException as http_exc:
        raise http_exc # Re-raise known HTTP exceptions
    except Exception as e:
        request_log.exception("Failed during initial document creation/upload")
        # Intentar marcar como error si el registro DB se creó
        try:
            if await postgres_client.get_document_status(document_id):
                 await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=f"Failed during initial upload: {e}")
        except Exception as db_err:
             request_log.error("Failed to update status to ERROR after initial upload failure", db_error=str(db_err))
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

        enriched_data = dict(record)
        current_status_str = enriched_data["status"]
        current_status = DocumentStatus(current_status_str)
        original_chunk_count = enriched_data.get('chunk_count', 0) # Guardar valor original de la DB

        # ---- Enriquecimiento ----
        if isinstance(enriched_data.get('metadata'), str):
            try: enriched_data['metadata'] = json.loads(enriched_data['metadata'])
            except json.JSONDecodeError: enriched_data['metadata'] = {"error": "invalid metadata format in DB"}

        file_path = enriched_data.get("file_path")
        minio_exists = False
        if file_path:
            try:
                minio_exists = await MinioStorageClient().file_exists(file_path)
                status_log.debug("MinIO existence check result", object_path=file_path, exists=minio_exists)
            except Exception as minio_err: status_log.error("Error checking MinIO", error=str(minio_err))
        enriched_data['minio_exists'] = minio_exists

        milvus_count = -1 # -1 indica error o no aplicable
        milvus_check_error = False
        # Solo verificar Milvus si el estado sugiere que *debería* haber chunks
        if current_status in [DocumentStatus.PROCESSING, DocumentStatus.PROCESSED, DocumentStatus.INDEXED]:
            status_log.debug("Checking chunk count in Milvus...")
            milvus_count = await get_milvus_chunk_count(document_id)
            if milvus_count == -1:
                status_log.error("Milvus check failed during status retrieval.")
                milvus_check_error = True # Marcar que hubo error en la verificación
            else:
                status_log.debug("Milvus chunk count result", count=milvus_count)
        elif current_status == DocumentStatus.UPLOADED:
             milvus_count = 0 # Asumir 0 si aún no debería estar procesado
        # Si está en ERROR, milvus_count permanece -1 (no relevante o incierto)
        enriched_data['milvus_chunk_count'] = milvus_count if not milvus_check_error else None # Poner None si hubo error

        # --- Lógica de Actualización de Estado Basada en Verificaciones ---
        needs_db_update = False
        new_status = current_status
        # Usar milvus_count si es válido, sino el de la DB
        new_chunk_count = milvus_count if milvus_count >= 0 else original_chunk_count
        new_error_message = enriched_data.get('error_message')

        if not minio_exists and current_status != DocumentStatus.ERROR:
             status_log.warning("MinIO file missing but status is not ERROR. Updating DB.", db_status=current_status_str)
             new_status = DocumentStatus.ERROR
             new_error_message = "Error: Archivo original no encontrado en almacenamiento."
             new_chunk_count = 0 # Resetear chunks si el archivo fuente no está
             needs_db_update = True
        elif not milvus_check_error: # Solo actuar si la verificación Milvus fue exitosa
             if milvus_count > 0 and current_status in [DocumentStatus.UPLOADED, DocumentStatus.PROCESSING]:
                 status_log.warning("DB status mismatch, chunks found in Milvus. Updating DB.", db_status=current_status_str, milvus_count=milvus_count)
                 new_status = DocumentStatus.PROCESSED
                 new_chunk_count = milvus_count
                 new_error_message = None # Limpiar error si ahora está procesado
                 needs_db_update = True
             elif milvus_count == 0 and current_status == DocumentStatus.PROCESSING:
                 status_log.warning("Milvus has 0 chunks but DB status is PROCESSING. Updating DB to ERROR.", db_status=current_status_str)
                 new_status = DocumentStatus.ERROR
                 new_error_message = "Error: Procesamiento no generó contenido indexable o falló inesperadamente."
                 new_chunk_count = 0
                 needs_db_update = True
             elif milvus_count != original_chunk_count and current_status == DocumentStatus.PROCESSED:
                  # Si está procesado pero el conteo no coincide, actualizar el conteo
                  status_log.warning("DB chunk count mismatch. Updating DB count.", db_count=original_chunk_count, milvus_count=milvus_count)
                  new_chunk_count = milvus_count
                  needs_db_update = True # Solo actualiza count si status ya es correcto

        # Si se necesita actualizar la DB
        if needs_db_update:
             status_log.info("Updating document status/chunks in DB based on real-time checks.", new_status=new_status.value, new_chunk_count=new_chunk_count)
             try:
                 await postgres_client.update_document_status(
                     document_id, new_status, chunk_count=new_chunk_count, error_message=new_error_message
                 )
                 # Actualizar los datos locales para la respuesta
                 enriched_data["status"] = new_status.value
                 enriched_data["chunk_count"] = new_chunk_count
                 enriched_data["error_message"] = new_error_message
             except Exception as db_update_err:
                  status_log.error("Failed to update DB status during consistency check", error=str(db_update_err))
                  # Continuar con los datos originales de la DB si la actualización falla

        # Generar mensaje descriptivo basado en el estado *final*
        final_status = DocumentStatus(enriched_data["status"])
        status_messages = {
            DocumentStatus.UPLOADED: "Documento subido, pendiente de procesamiento.",
            DocumentStatus.PROCESSING: "Documento está siendo procesado.",
            DocumentStatus.PROCESSED: "Documento procesado correctamente.",
            DocumentStatus.INDEXED: "Documento procesado e indexado.",
            DocumentStatus.ERROR: f"Error de procesamiento: {enriched_data.get('error_message') or 'Error desconocido'}",
        }
        # Añadir nota si hubo error al verificar Milvus
        message = status_messages.get(final_status, "Estado desconocido.")
        if milvus_check_error and final_status != DocumentStatus.ERROR:
            message += " (No se pudo verificar el estado de indexación en Milvus)"
        enriched_data['message'] = message

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
    limit: int = Query(default=30, ge=1, le=500),
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
            enriched_data = dict(record)
            current_status_str = enriched_data["status"]
            current_status = DocumentStatus(current_status_str)
            original_chunk_count = enriched_data.get('chunk_count', 0)

            # Parsear metadata
            if isinstance(enriched_data.get('metadata'), str):
                try: enriched_data['metadata'] = json.loads(enriched_data['metadata'])
                except json.JSONDecodeError: enriched_data['metadata'] = {"error": "invalid metadata format in DB"}

            # Verificar MinIO
            file_path = enriched_data.get("file_path")
            minio_exists = False
            try: minio_exists = await MinioStorageClient().file_exists(file_path) if file_path else False
            except Exception as ex: enrich_log.error("MinIO check failed", error=str(ex))
            enriched_data['minio_exists'] = minio_exists

            # Contar chunks en Milvus
            milvus_count = -1
            milvus_check_error = False
            if current_status in [DocumentStatus.PROCESSING, DocumentStatus.PROCESSED, DocumentStatus.INDEXED]:
                 try: milvus_count = await get_milvus_chunk_count(doc_id)
                 except Exception as ex:
                      enrich_log.error("Milvus check failed", error=str(ex))
                      milvus_check_error = True
                 if milvus_count == -1 and not milvus_check_error: milvus_check_error = True
            elif current_status == DocumentStatus.UPLOADED:
                 milvus_count = 0
            enriched_data['milvus_chunk_count'] = milvus_count if not milvus_check_error else None

            # Lógica de Actualización de Estado Basada en Verificaciones
            needs_db_update = False
            new_status = current_status
            new_chunk_count = milvus_count if milvus_count >= 0 else original_chunk_count
            new_error_message = enriched_data.get('error_message')

            if not minio_exists and current_status != DocumentStatus.ERROR:
                 new_status = DocumentStatus.ERROR; new_error_message = "Error: Archivo original no encontrado."; needs_db_update = True
            elif not milvus_check_error:
                 if milvus_count > 0 and current_status in [DocumentStatus.UPLOADED, DocumentStatus.PROCESSING]:
                     new_status = DocumentStatus.PROCESSED; new_chunk_count = milvus_count; new_error_message = None; needs_db_update = True
                 elif milvus_count == 0 and current_status == DocumentStatus.PROCESSING:
                     new_status = DocumentStatus.ERROR; new_error_message = "Error: Procesamiento sin resultado indexable."; needs_db_update = True
                 elif milvus_count >= 0 and current_status != DocumentStatus.ERROR and original_chunk_count != milvus_count:
                      new_chunk_count = milvus_count; needs_db_update = True

            if needs_db_update:
                 enrich_log.info("Updating document status/chunks in DB based on real-time checks.", new_status=new_status.value, new_chunk_count=new_chunk_count)
                 try:
                     await postgres_client.update_document_status(
                         doc_id, new_status, chunk_count=new_chunk_count, error_message=new_error_message
                     )
                     enriched_data["status"] = new_status.value
                     enriched_data["chunk_count"] = new_chunk_count
                     enriched_data["error_message"] = new_error_message
                 except Exception as db_err:
                     enrich_log.error("Failed to update DB during enrichment", error=str(db_err))

            # Generar mensaje final
            final_status = DocumentStatus(enriched_data["status"])
            status_messages = {
                DocumentStatus.UPLOADED: "Pendiente de procesamiento.",
                DocumentStatus.PROCESSING: "Procesando...",
                DocumentStatus.PROCESSED: "Procesado.",
                DocumentStatus.INDEXED: "Indexado.",
                DocumentStatus.ERROR: f"Error: {enriched_data.get('error_message') or 'Desconocido'}",
            }
            message = status_messages.get(final_status, "Estado desconocido.")
            if milvus_check_error and final_status != DocumentStatus.ERROR:
                message += " (Verificación Milvus falló)"
            enriched_data['message'] = message

            # Validar finalmente
            try:
                return schemas.StatusResponse.model_validate(enriched_data)
            except ValidationError as val_err:
                enrich_log.error(
                     "Error validating status after enrichment",
                     error=str(val_err),
                     data_validated=enriched_data
                 )
                return None # Excluir de la lista final

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
    user_id: uuid.UUID = Depends(get_current_user_id),
    request: Request = None
):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4())) if request else str(uuid.uuid4())
    retry_log = log.bind(request_id=request_id, company_id=str(company_id), user_id=str(user_id), document_id=str(document_id))
    retry_log.info("Received request to retry document ingestion")

    doc = await postgres_client.get_document_status(document_id)
    if not doc:
        retry_log.warning("Document not found for retry attempt")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Documento no encontrado.")
    try: doc_company_id = uuid.UUID(str(doc.get("company_id")))
    except (ValueError, TypeError): raise HTTPException(status_code=500, detail="Error interno al verificar documento.")
    if doc_company_id != company_id: raise HTTPException(status_code=404, detail="Documento no encontrado.")

    if doc.get("status") != DocumentStatus.ERROR.value:
        retry_log.warning("Retry attempt on document not in 'error' state", current_status=doc.get("status"))
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Solo se puede reintentar la ingesta si el estado es 'error'.")
    if not doc.get("file_path"):
        retry_log.error("Cannot retry document without a valid file_path in DB")
        await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message="Cannot retry: Original file path missing.")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No se puede reintentar: falta la ruta del archivo original.")

    metadata_to_pass = doc.get("metadata", {})
    if isinstance(metadata_to_pass, str):
         try: metadata_to_pass = json.loads(metadata_to_pass)
         except: metadata_to_pass = {}
    elif not isinstance(metadata_to_pass, dict): metadata_to_pass = {}

    try:
        # 1. Actualizar estado a 'PROCESSING' y limpiar error ANTES de encolar
        await postgres_client.update_document_status(
            document_id=document_id, status=DocumentStatus.PROCESSING, error_message=None
        )
        retry_log.info("Document status updated to PROCESSING for retry.")

        # 2. Reencolar la tarea Celery
        task = process_document_haystack_task.delay(
            document_id_str=str(document_id), company_id_str=str(company_id),
            minio_object_name=doc["file_path"], file_name=doc["file_name"],
            content_type=doc["file_type"], original_metadata=metadata_to_pass
        )
        retry_log.info("Retry ingestion task queued", task_id=task.id)
        return schemas.IngestResponse(document_id=document_id, task_id=task.id, status=DocumentStatus.PROCESSING.value, message="Reintento de ingesta encolado correctamente.")

    except Exception as e:
        retry_log.exception("Failed during retry initiation (DB update or Celery queue)")
        # Intentar volver a poner en error si falló el encolado
        try: await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=f"Fallo al iniciar reintento: {e}")
        except: pass # Ignorar error secundario
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error al iniciar reintento: {e}")


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
    if not doc: raise HTTPException(status_code=404, detail="Documento no encontrado.")
    try: doc_company_id = uuid.UUID(str(doc.get("company_id")))
    except: raise HTTPException(status_code=500, detail="Error interno al verificar documento.")
    if doc_company_id != company_id: raise HTTPException(status_code=404, detail="Documento no encontrado.")

    errors = []
    loop = asyncio.get_running_loop()

    # 2. Eliminar chunks de Milvus
    delete_log.info("Attempting to delete chunks from Milvus...")
    try:
        def _delete_milvus_sync():
            # *** CORRECCIÓN DEFINITIVA: Usar embedding_dim aquí también ***
            store = MilvusDocumentStore(
                connection_args={"uri": str(settings.MILVUS_URI)},
                collection_name=settings.MILVUS_COLLECTION_NAME,
                embedding_dim=settings.EMBEDDING_DIMENSION, # CORREGIDO
                consistency_level="Strong"
            )
            store.delete_documents(filters={"document_id": str(document_id)})
        await loop.run_in_executor(None, _delete_milvus_sync)
        delete_log.info("Successfully deleted chunks from Milvus.")
    except Exception as milvus_err:
        error_msg = f"Failed to delete chunks from Milvus: {milvus_err}"
        delete_log.error(error_msg, exc_info=True)
        errors.append(error_msg)

    # 3. Eliminar archivo de MinIO
    file_path = doc.get("file_path")
    if file_path:
        delete_log.info("Attempting to delete file from MinIO...")
        try:
            minio_client = MinioStorageClient()
            await minio_client.delete_file(file_path)
            delete_log.info("Successfully deleted file from MinIO.")
        except Exception as minio_err:
            error_msg = f"Failed to delete file from MinIO: {minio_err}"
            delete_log.error(error_msg, exc_info=True)
            errors.append(error_msg)
    else:
        delete_log.warning("No file_path found in DB, skipping MinIO deletion.")

    # 4. Eliminar registro de PostgreSQL
    delete_log.info("Attempting to delete record from PostgreSQL...")
    try:
        deleted = await postgres_client.delete_document(document_id)
        if not deleted:
             error_msg = "Failed to delete document from PostgreSQL (record not found during deletion)."
             delete_log.error(error_msg)
             errors.append(error_msg)
        else:
             delete_log.info("Document record deleted successfully from PostgreSQL")
    except Exception as db_err:
        error_msg = f"Error deleting document record from PostgreSQL: {db_err}"
        delete_log.exception(error_msg)
        errors.append(error_msg)

    # Si hubo errores, devolver 500, si no, 204
    if errors:
        delete_log.error("Document deletion completed with errors", errors=errors)
        # Aunque falle algo, devolvemos 204 para no bloquear UI, pero logueamos el error
        # Si se requiere comportamiento estricto, cambiar a HTTPException 500
        # raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Document deletion failed for some components: {'; '.join(errors)}")

    delete_log.info("Document deletion process finished.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)