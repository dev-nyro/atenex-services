# Estructura de la Codebase

```
app/
├── __init__.py
├── api
│   ├── __init__.py
│   └── v1
│       ├── __init__.py
│       ├── endpoints
│       │   ├── __init__.py
│       │   └── ingest.py
│       └── schemas.py
├── core
│   ├── __init__.py
│   ├── config.py
│   └── logging_config.py
├── db
│   ├── __init__.py
│   ├── base.py
│   └── postgres_client.py
├── main.py
├── models
│   ├── __init__.py
│   └── domain.py
├── services
│   ├── __init__.py
│   ├── base_client.py
│   └── minio_client.py
├── tasks
│   ├── __init__.py
│   ├── celery_app.py
│   └── process_document.py
└── utils
    ├── __init__.py
    └── helpers.py
```

# Codebase: `app`

## File: `app\__init__.py`
```py

```

## File: `app\api\__init__.py`
```py

```

## File: `app\api\v1\__init__.py`
```py

```

## File: `app\api\v1\endpoints\__init__.py`
```py

```

## File: `app\api\v1\endpoints\ingest.py`
```py
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
    response_model=List[schemas.StatusResponse],
    status_code=status.HTTP_200_OK,
    summary="Listar estados de ingesta para la compañía (con estado real)",
    description="Recupera lista de documentos con verificación en MinIO y conteo real de Milvus en paralelo.",
)
async def list_ingestion_statuses(
    company_id: uuid.UUID = Depends(get_current_company_id),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    request: Request = None
):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4())) if request else str(uuid.uuid4())
    list_log = log.bind(request_id=request_id, company_id=str(company_id), limit=limit, offset=offset)
    list_log.info("Listing document statuses with real-time checks")
    try:
        docs = await postgres_client.list_documents_by_company(company_id, limit=limit, offset=offset)
        # Mapear datos base a Pydantic
        base_statuses: List[schemas.StatusResponse] = []
        for doc in docs:
            try:
                base_statuses.append(schemas.StatusResponse.model_validate(doc))
            except Exception as e:
                list_log.error("Error validating base status", doc_id=doc.get("id"), error=str(e))
        # Función para enriquecer cada status en paralelo
        async def enrich(status_obj: schemas.StatusResponse, doc: Dict[str, Any]) -> schemas.StatusResponse:
            # Verificar MinIO
            file_path = doc.get("file_path")
            try:
                status_obj.minio_exists = await MinioStorageClient().file_exists(file_path) if file_path else False
            except Exception as ex:
                list_log.error("MinIO check failed", document_id=str(status_obj.document_id), error=str(ex))
                status_obj.minio_exists = False
            # Contar chunks en Milvus
            try:
                count = await get_milvus_chunk_count(status_obj.document_id)
                status_obj.milvus_chunk_count = count
                status_obj.chunk_count = count
                # Si hay chunks, actualizar estado y mensaje
                if count > 0 and status_obj.status != DocumentStatus.ERROR:
                    status_obj.status = DocumentStatus.PROCESSED
                    status_obj.message = "Document processed successfully."
                # Persistir estado real en la DB para mantener datos actualizados
                await postgres_client.update_document_status(
                    document_id=status_obj.document_id,
                    status=status_obj.status,
                    chunk_count=count
                )
            except Exception as ex:
                list_log.error("Error enriching/persisting status", document_id=str(status_obj.document_id), error=str(ex))
            return status_obj
        # Lanzar verificaciones en paralelo
        tasks = [asyncio.create_task(enrich(st, doc)) for st, doc in zip(base_statuses, docs)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        final_list: List[schemas.StatusResponse] = []
        for res in results:
            if isinstance(res, Exception):
                list_log.error("Error enriching status", error=str(res))
            else:
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
```

## File: `app\api\v1\schemas.py`
```py
# ingest-service/app/api/v1/schemas.py
import uuid
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from app.models.domain import DocumentStatus
from datetime import datetime

# Ya no se usa IngestRequest aquí, se maneja con Form y File en el endpoint
# class IngestRequest(BaseModel):
#     pass

class IngestResponse(BaseModel):
    """Respuesta devuelta al iniciar la ingesta."""
    document_id: uuid.UUID
    task_id: Optional[str] = None # ID de la tarea Celery
    status: DocumentStatus = DocumentStatus.UPLOADED # Estado inicial UPLOADED
    message: str = "Document upload received and queued for processing."

class StatusResponse(BaseModel):
    """Schema para representar el estado de un documento."""
    # Usar alias para mapear nombres de columnas de DB a nombres de campo API
    document_id: uuid.UUID = Field(..., alias="id")
    status: DocumentStatus # El enum se valida automáticamente
    file_name: Optional[str] = None
    file_type: Optional[str] = None
    chunk_count: Optional[int] = None
    # Estado actual en MinIO
    minio_exists: Optional[bool] = None
    # Número de chunks indexados en Milvus
    milvus_chunk_count: Optional[int] = None
    last_updated: Optional[datetime] = Field(None, alias="updated_at")
    # Mensaje descriptivo añadido en el endpoint, no viene de la DB directamente
    message: Optional[str] = Field(None, exclude=False) # Incluir en respuesta si se añade

    # Configuración Pydantic v2 para mapeo y creación desde atributos
    model_config = {
        "populate_by_name": True, # Permite usar 'alias' para mapear desde nombres de DB/dict
        "from_attributes": True   # Permite crear instancia desde un objeto con atributos (como asyncpg.Record)
    }
```

## File: `app\core\__init__.py`
```py

```

## File: `app\core\config.py`
```py
# ingest-service/app/core/config.py
import logging
import os
from typing import Optional, List, Any, Dict, Union
from pydantic_settings import BaseSettings, SettingsConfigDict
# LLM_COMMENT: Use Pydantic v2 imports
from pydantic import (
    RedisDsn, AnyHttpUrl, SecretStr, Field, field_validator, ValidationError,
    ValidationInfo
)
import sys
import json

# --- Service Names en K8s ---
POSTGRES_K8S_SVC = "postgresql.nyro-develop.svc.cluster.local"
MINIO_K8S_SVC = "minio-service.nyro-develop.svc.cluster.local"
MILVUS_K8S_SVC = "milvus-milvus.default.svc.cluster.local" # Milvus en default ns
REDIS_K8S_SVC = "redis-service-master.nyro-develop.svc.cluster.local"

# --- Defaults ---
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex"
POSTGRES_K8S_USER_DEFAULT = "postgres"
MINIO_K8S_PORT_DEFAULT = 9000
MINIO_BUCKET_DEFAULT = "atenex" # LLM_COMMENT: Ensure bucket name matches architecture diagram
MILVUS_K8S_PORT_DEFAULT = 19530
REDIS_K8S_PORT_DEFAULT = 6379
MILVUS_DEFAULT_COLLECTION = "atenex_doc_chunks" # LLM_COMMENT: Ensure collection name consistency
MILVUS_DEFAULT_INDEX_PARAMS = '{"metric_type": "COSINE", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 256}}'
MILVUS_DEFAULT_SEARCH_PARAMS = '{"metric_type": "COSINE", "params": {"ef": 128}}'
# LLM_COMMENT: Keep OpenAI embedding settings for ingest service as per current design
OPENAI_DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
# LLM_COMMENT: Ensure dimension matches the selected OpenAI model
DEFAULT_EMBEDDING_DIM = 1536 # Dimension for text-embedding-3-small & ada-002

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env', env_prefix='INGEST_', env_file_encoding='utf-8',
        case_sensitive=False, extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = "Atenex Ingest Service"
    # ¡¡¡¡NUNCA MODIFICAR ESTA RUTA!!!
    # Esta ruta DEBE ser exactamente '/api/v1/ingest' para que el API Gateway funcione correctamente.
    # Si la cambias, romperás la integración y el proxy de rutas. Si tienes dudas, consulta con el equipo de plataforma.
    API_V1_STR: str = "/api/v1/ingest"
    LOG_LEVEL: str = "INFO"

    # --- Celery ---
    CELERY_BROKER_URL: RedisDsn = Field(default=RedisDsn(f"redis://{REDIS_K8S_SVC}:{REDIS_K8S_PORT_DEFAULT}/0"))
    CELERY_RESULT_BACKEND: RedisDsn = Field(default=RedisDsn(f"redis://{REDIS_K8S_SVC}:{REDIS_K8S_PORT_DEFAULT}/1"))

    # --- Database ---
    POSTGRES_USER: str = POSTGRES_K8S_USER_DEFAULT
    POSTGRES_PASSWORD: SecretStr # LLM_COMMENT: Loaded from secrets
    POSTGRES_SERVER: str = POSTGRES_K8S_SVC
    POSTGRES_PORT: int = POSTGRES_K8S_PORT_DEFAULT
    POSTGRES_DB: str = POSTGRES_K8S_DB_DEFAULT

    # --- Milvus ---
    MILVUS_URI: str = Field(default=f"http://{MILVUS_K8S_SVC}:{MILVUS_K8S_PORT_DEFAULT}")
    MILVUS_COLLECTION_NAME: str = MILVUS_DEFAULT_COLLECTION
    # LLM_COMMENT: Ensure metadata fields include filtering keys and display keys
    MILVUS_METADATA_FIELDS: List[str] = Field(default=["company_id", "document_id", "file_name", "file_type"])
    MILVUS_CONTENT_FIELD: str = "content" # LLM_COMMENT: Field name for text content in Milvus
    MILVUS_EMBEDDING_FIELD: str = "embedding" # LLM_COMMENT: Field name for vectors in Milvus
    MILVUS_INDEX_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_INDEX_PARAMS))
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_SEARCH_PARAMS))

    # --- MinIO ---
    MINIO_ENDPOINT: str = Field(default=f"{MINIO_K8S_SVC}:{MINIO_K8S_PORT_DEFAULT}")
    MINIO_ACCESS_KEY: SecretStr # LLM_COMMENT: Loaded from secrets
    MINIO_SECRET_KEY: SecretStr # LLM_COMMENT: Loaded from secrets
    MINIO_BUCKET_NAME: str = MINIO_BUCKET_DEFAULT
    MINIO_USE_SECURE: bool = False # LLM_COMMENT: Typically false for internal k8s communication

    # --- Embeddings (OpenAI for Ingestion) ---
    OPENAI_API_KEY: SecretStr # LLM_COMMENT: Loaded from secrets
    OPENAI_EMBEDDING_MODEL: str = OPENAI_DEFAULT_EMBEDDING_MODEL
    # LLM_COMMENT: Dimension must match the chosen OpenAI model
    EMBEDDING_DIMENSION: int = DEFAULT_EMBEDDING_DIM

    # --- Clients ---
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 2
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    # --- Processing ---
    SUPPORTED_CONTENT_TYPES: List[str] = Field(default=[
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", # DOCX
        "application/msword", # DOC (handled by DOCX converter often)
        "text/plain",
        "text/markdown",
        "text/html"
    ])
    SPLITTER_CHUNK_SIZE: int = 500
    SPLITTER_CHUNK_OVERLAP: int = 50
    SPLITTER_SPLIT_BY: str = "word" # Other options: "sentence", "passage"

    # --- Validators (Pydantic V2 Style) ---
    # LLM_COMMENT: Keep validators for log level and secret presence

    @field_validator("LOG_LEVEL")
    @classmethod
    def check_log_level(cls, v: str) -> str:
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        normalized_v = v.upper()
        if normalized_v not in valid_levels: raise ValueError(f"Invalid LOG_LEVEL '{v}'. Must be one of {valid_levels}")
        return normalized_v

    @field_validator('EMBEDDING_DIMENSION', mode='before', check_fields=False)
    @classmethod
    def set_embedding_dimension(cls, v: Optional[int], info: ValidationInfo) -> int:
        # LLM_COMMENT: Ensure dimension calculation matches the OpenAI model used here
        config_values = info.data
        model = config_values.get('OPENAI_EMBEDDING_MODEL', OPENAI_DEFAULT_EMBEDDING_MODEL)
        calculated_dim = DEFAULT_EMBEDDING_DIM # Default from constants
        # Update calculated_dim based on known OpenAI models
        if model == "text-embedding-3-large": calculated_dim = 3072
        elif model in ["text-embedding-3-small", "text-embedding-ada-002"]: calculated_dim = 1536
        # Add other models if needed

        if v is not None and v != calculated_dim:
             logging.warning(f"Provided INGEST_EMBEDDING_DIMENSION {v} conflicts with INGEST_OPENAI_EMBEDDING_MODEL {model} ({calculated_dim} expected). Using calculated value: {calculated_dim}")
             return calculated_dim
        elif v is None:
             # LLM_COMMENT: Log which dimension is being set if not provided
             logging.debug(f"EMBEDDING_DIMENSION not set, defaulting to {calculated_dim} based on model {model}")
             return calculated_dim
        else:
             # LLM_COMMENT: Log if provided dimension matches calculated
             if v == calculated_dim:
                 logging.debug(f"Provided EMBEDDING_DIMENSION {v} matches model {model}")
             return v # Return user-provided value if it matches

    @field_validator('POSTGRES_PASSWORD', 'MINIO_ACCESS_KEY', 'MINIO_SECRET_KEY', 'OPENAI_API_KEY', mode='before')
    @classmethod
    def check_secret_value_present(cls, v: Any, info: ValidationInfo) -> Any:
        """Valida que el valor para un campo secreto no esté vacío antes de convertir a SecretStr."""
        if v is None or v == "":
             field_name = info.field_name if info.field_name else "Unknown Secret Field"
             raise ValueError(f"Required secret field '{field_name}' cannot be empty.")
        return v

# --- Instancia Global ---
# LLM_COMMENT: Keep global settings instance logic, update logging format
temp_log = logging.getLogger("ingest_service.config.loader")
if not temp_log.handlers:
    handler = logging.StreamHandler(sys.stdout)
    # LLM_COMMENT: Use a more informative startup log format
    formatter = logging.Formatter('%(levelname)-8s [%(asctime)s] [%(name)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    handler.setFormatter(formatter)
    temp_log.addHandler(handler)
    temp_log.setLevel(logging.INFO) # Set level for startup logs

try:
    temp_log.info("Loading Ingest Service settings...")
    settings = Settings()
    temp_log.info("--- Ingest Service Settings Loaded ---")
    temp_log.info(f"  PROJECT_NAME:             {settings.PROJECT_NAME}")
    temp_log.info(f"  LOG_LEVEL:                {settings.LOG_LEVEL}")
    temp_log.info(f"  API_V1_STR:               {settings.API_V1_STR}")
    temp_log.info(f"  CELERY_BROKER_URL:        {settings.CELERY_BROKER_URL}")
    temp_log.info(f"  CELERY_RESULT_BACKEND:    {settings.CELERY_RESULT_BACKEND}")
    temp_log.info(f"  POSTGRES_SERVER:          {settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}")
    temp_log.info(f"  POSTGRES_DB:              {settings.POSTGRES_DB}")
    temp_log.info(f"  POSTGRES_USER:            {settings.POSTGRES_USER}")
    temp_log.info(f"  POSTGRES_PASSWORD:        *** SET ***")
    temp_log.info(f"  MILVUS_URI:               {settings.MILVUS_URI}")
    temp_log.info(f"  MILVUS_COLLECTION_NAME:   {settings.MILVUS_COLLECTION_NAME}")
    temp_log.info(f"  MINIO_ENDPOINT:           {settings.MINIO_ENDPOINT}")
    temp_log.info(f"  MINIO_BUCKET_NAME:        {settings.MINIO_BUCKET_NAME}")
    temp_log.info(f"  MINIO_ACCESS_KEY:         *** SET ***")
    temp_log.info(f"  MINIO_SECRET_KEY:         *** SET ***")
    temp_log.info(f"  OPENAI_API_KEY:           *** SET ***")
    temp_log.info(f"  OPENAI_EMBEDDING_MODEL:   {settings.OPENAI_EMBEDDING_MODEL}")
    temp_log.info(f"  EMBEDDING_DIMENSION:      {settings.EMBEDDING_DIMENSION}")
    temp_log.info(f"  SUPPORTED_CONTENT_TYPES:  {settings.SUPPORTED_CONTENT_TYPES}")
    temp_log.info(f"  SPLITTER_CHUNK_SIZE:      {settings.SPLITTER_CHUNK_SIZE}")
    temp_log.info(f"  SPLITTER_CHUNK_OVERLAP:   {settings.SPLITTER_CHUNK_OVERLAP}")
    temp_log.info(f"------------------------------------")

except (ValidationError, ValueError) as e:
    error_details = ""
    if isinstance(e, ValidationError):
        try: error_details = f"\nValidation Errors:\n{e.json(indent=2)}"
        except Exception: error_details = f"\nRaw Errors: {e.errors()}"
    temp_log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    temp_log.critical(f"! FATAL: Ingest Service configuration validation failed:{error_details}")
    temp_log.critical(f"! Check environment variables (prefixed with INGEST_) or .env file.")
    temp_log.critical(f"! Original Error: {e}")
    temp_log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    sys.exit(1)
except Exception as e:
    temp_log.exception(f"FATAL: Unexpected error loading Ingest Service settings: {e}")
    sys.exit(1)
```

## File: `app\core\logging_config.py`
```py
import logging
import sys
import structlog
from app.core.config import settings
import os

def setup_logging():
    """Configura el logging estructurado con structlog."""

    # Disable existing handlers if running in certain environments (like Uvicorn default)
    # to avoid duplicate logs. This might need adjustment based on deployment.
    # logging.getLogger().handlers.clear()

    # Determine if running inside Celery worker
    is_celery_worker = "celery" in sys.argv[0]

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.LOG_LEVEL == logging.DEBUG:
         # Add caller info only in debug mode for performance
         shared_processors.append(structlog.processors.CallsiteParameterAdder(
             {
                 structlog.processors.CallsiteParameter.FILENAME,
                 structlog.processors.CallsiteParameter.LINENO,
             }
         ))

    # Configure structlog
    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Configure the formatter for stdlib logging
    formatter = structlog.stdlib.ProcessorFormatter(
        # These run ONCE per log structuralization
        foreign_pre_chain=shared_processors,
         # These run on EVERY record
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(), # Render as JSON
        ],
    )

    # Configure root logger handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    # Avoid adding handler twice if already configured (e.g., by Uvicorn/Gunicorn)
    if not any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers):
         root_logger.addHandler(handler)

    root_logger.setLevel(settings.LOG_LEVEL)

    # Silence verbose libraries
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("asyncpg").setLevel(logging.WARNING)
    logging.getLogger("haystack").setLevel(logging.INFO) # Or DEBUG for more Haystack details
    logging.getLogger("milvus_haystack").setLevel(logging.INFO) # Adjust as needed

    log = structlog.get_logger("ingest_service")
    log.info("Logging configured", log_level=settings.LOG_LEVEL, is_celery_worker=is_celery_worker)
```

## File: `app\db\__init__.py`
```py

```

## File: `app\db\base.py`
```py

```

## File: `app\db\postgres_client.py`
```py
# ingest-service/app/db/postgres_client.py
import uuid
from typing import Any, Optional, Dict, List
import asyncpg
import structlog
import json
from datetime import datetime, timezone

from app.core.config import settings
from app.models.domain import DocumentStatus

log = structlog.get_logger(__name__)

_pool: Optional[asyncpg.Pool] = None

# --- Pool Management (Sin cambios) ---
async def get_db_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None or _pool._closed:
        log.info("Creating PostgreSQL connection pool...", host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT, user=settings.POSTGRES_USER, db=settings.POSTGRES_DB)
        try:
            def _json_encoder(value): return json.dumps(value)
            def _json_decoder(value): return json.loads(value)
            async def init_connection(conn):
                await conn.set_type_codec('jsonb', encoder=_json_encoder, decoder=_json_decoder, schema='pg_catalog', format='text')
                await conn.set_type_codec('json', encoder=_json_encoder, decoder=_json_decoder, schema='pg_catalog', format='text')

            _pool = await asyncpg.create_pool(
                user=settings.POSTGRES_USER, password=settings.POSTGRES_PASSWORD.get_secret_value(),
                database=settings.POSTGRES_DB, host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT,
                min_size=2, max_size=10, timeout=30.0, command_timeout=60.0,
                init=init_connection, statement_cache_size=0
            )
            log.info("PostgreSQL connection pool created successfully.")
        except (asyncpg.exceptions.InvalidPasswordError, OSError, ConnectionRefusedError) as conn_err:
            log.critical("CRITICAL: Failed to connect to PostgreSQL", error=str(conn_err), exc_info=True)
            _pool = None; raise ConnectionError(f"Failed to connect to PostgreSQL: {conn_err}") from conn_err
        except Exception as e:
            log.critical("CRITICAL: Failed to create PostgreSQL connection pool", error=str(e), exc_info=True)
            _pool = None; raise RuntimeError(f"Failed to create PostgreSQL pool: {e}") from e
    return _pool

async def close_db_pool():
    global _pool
    if _pool and not _pool._closed: log.info("Closing PostgreSQL connection pool..."); await _pool.close(); _pool = None; log.info("PostgreSQL connection pool closed.")
    elif _pool and _pool._closed: log.warning("Attempted to close an already closed PostgreSQL pool."); _pool = None
    else: log.info("No active PostgreSQL connection pool to close.")

async def check_db_connection() -> bool:
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            async with conn.transaction(): result = await conn.fetchval("SELECT 1")
        return result == 1
    except Exception as e: log.error("Database connection check failed", error=str(e)); return False

# --- Document Operations ---
async def create_document(company_id: uuid.UUID, file_name: str, file_type: str, metadata: Dict[str, Any]) -> uuid.UUID:
    pool = await get_db_pool(); doc_id = uuid.uuid4()
    # LLM_COMMENT: Set initial chunk_count to 0 and error_message to NULL
    query = """
    INSERT INTO documents (id, company_id, file_name, file_type, file_path, metadata, status, chunk_count, error_message, uploaded_at, updated_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NULL, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC') RETURNING id;
    """
    insert_log = log.bind(company_id=str(company_id), filename=file_name, content_type=file_type, proposed_doc_id=str(doc_id))
    try:
        metadata_json = json.dumps(metadata)
        async with pool.acquire() as connection:
            # LLM_COMMENT: Parameter index adjusted for new columns (chunk_count=0, error_message=NULL)
            result_id = await connection.fetchval(query, doc_id, company_id, file_name, file_type, '', metadata_json, DocumentStatus.UPLOADED.value, 0)
        if result_id and result_id == doc_id:
            insert_log.info("Document record created", document_id=str(doc_id)); return result_id
        else:
            insert_log.error("Failed to create document record, ID mismatch or no ID returned", returned_id=result_id)
            raise RuntimeError(f"Failed create document, return mismatch ({result_id})")
    except asyncpg.exceptions.UniqueViolationError as e:
        insert_log.error("Unique constraint violation on document creation", error=str(e), constraint=e.constraint_name)
        raise ValueError(f"Document creation failed: unique constraint ({e.constraint_name})") from e
    except Exception as e:
        insert_log.error("Failed to create document record", error=str(e), exc_info=True); raise

# LLM_COMMENT: Update function now includes error_message handling
async def update_document_status(
    document_id: uuid.UUID,
    status: DocumentStatus,
    file_path: Optional[str] = None,
    chunk_count: Optional[int] = None,
    error_message: Optional[str] = None
) -> bool:
    pool = await get_db_pool();
    update_log = log.bind(document_id=str(document_id), new_status=status.value)
    fields_to_set: List[str] = []
    params: List[Any] = [document_id]
    param_index = 2

    fields_to_set.append(f"status = ${param_index}"); params.append(status.value); param_index += 1
    fields_to_set.append(f"updated_at = NOW() AT TIME ZONE 'UTC'")

    if file_path is not None: fields_to_set.append(f"file_path = ${param_index}"); params.append(file_path); param_index += 1
    if chunk_count is not None: fields_to_set.append(f"chunk_count = ${param_index}"); params.append(chunk_count); param_index += 1

    # LLM_COMMENT: Handle error_message. Set it if status is ERROR, clear it (set to NULL) otherwise.
    # LLM_COMMENT: ASSUMES `error_message` COLUMN EXISTS IN `documents` TABLE NOW.
    if status == DocumentStatus.ERROR:
        # Truncate error message if needed
        truncated_error = (error_message[:497] + '...') if error_message and len(error_message) > 500 else error_message
        fields_to_set.append(f"error_message = ${param_index}"); params.append(truncated_error); param_index += 1
        update_log = update_log.bind(error=truncated_error)
    else:
        # Clear error message for non-error statuses
        fields_to_set.append(f"error_message = NULL");
        # No parameter needed for NULL

    query = f"UPDATE documents SET {', '.join(fields_to_set)} WHERE id = $1;";
    update_log.debug("Executing status update", query=query, params_count=len(params))
    try:
        async with pool.acquire() as connection:
             result_str = await connection.execute(query, *params)
        affected_rows = 0
        if isinstance(result_str, str) and result_str.startswith("UPDATE "):
            try: affected_rows = int(result_str.split(" ")[1])
            except (IndexError, ValueError): update_log.warning("Could not parse affected rows from UPDATE result", result=result_str)

        if affected_rows > 0:
             update_log.info("Document status updated", rows=affected_rows)
             return True
        else:
             update_log.warning("Document status update affected 0 rows (document might not exist?)")
             return False
    except Exception as e:
        update_log.error("Failed to update document status", error=str(e), exc_info=True); raise

# LLM_COMMENT: Get status includes error_message
async def get_document_status(document_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    pool = await get_db_pool(); get_log = log.bind(document_id=str(document_id))
    # LLM_COMMENT: Query now includes error_message
    query = """
    SELECT id, status, file_name, file_type, chunk_count, file_path, updated_at, company_id, error_message
    FROM documents WHERE id = $1;
    """
    try:
        async with pool.acquire() as connection: record = await connection.fetchrow(query, document_id)
        if record: get_log.debug("Document status retrieved"); return dict(record)
        else: get_log.warning("Document status requested for non-existent ID"); return None
    except Exception as e: get_log.error("Failed to get status", error=str(e), exc_info=True); raise

# LLM_COMMENT: List function corrected - REMOVE error_message selection as it doesn't exist per schema
async def list_documents_by_company(company_id: uuid.UUID, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    pool = await get_db_pool(); list_log = log.bind(company_id=str(company_id), limit=limit, offset=offset)
    # LLM_COMMENT: CORRECTED QUERY - Removed error_message from SELECT list
    query = """
    SELECT id, status, file_name, file_type, chunk_count, file_path, updated_at
    FROM documents
    WHERE company_id = $1 ORDER BY updated_at DESC LIMIT $2 OFFSET $3;
    """
    try:
        async with pool.acquire() as connection: records = await connection.fetch(query, company_id, limit, offset)
        result_list = [dict(record) for record in records]; list_log.info(f"Retrieved {len(result_list)} docs for company")
        return result_list
    except asyncpg.exceptions.UndefinedColumnError as col_err:
        # LLM_COMMENT: Log specific column error if it happens again
        list_log.critical("Undefined column error listing documents - Schema mismatch?", error=str(col_err), query=query)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Database schema error.")
    except Exception as e: list_log.error("Failed to list docs", error=str(e), exc_info=True); raise

# LLM_COMMENT: Delete document remains the same
async def delete_document(document_id: uuid.UUID) -> bool:
    """
    Elimina un documento de la tabla `documents` por su ID.
    Devuelve True si se eliminó al menos una fila, False si no existía.
    """
    pool = await get_db_pool()
    delete_log = log.bind(document_id=str(document_id))
    query = "DELETE FROM documents WHERE id = $1;"
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(query, document_id)
        deleted = isinstance(result, str) and result.startswith("DELETE") and int(result.split(" ")[1]) > 0
        if deleted:
            delete_log.info("Document record deleted from PostgreSQL.", result=result)
        else:
            delete_log.warning("Attempted to delete non-existent document record.", result=result)
        return deleted
    except Exception as e:
        delete_log.error("Error deleting document record from PostgreSQL", error=str(e), exc_info=True)
        raise

# --- Funciones de Chat (Sin cambios) ---
async def create_chat(user_id: uuid.UUID, company_id: uuid.UUID, title: Optional[str] = None) -> uuid.UUID:
    pool = await get_db_pool()
    chat_id = uuid.uuid4()
    query = """INSERT INTO chats (id, user_id, company_id, title, created_at, updated_at) VALUES ($1, $2, $3, $4, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC') RETURNING id;"""
    try:
        async with pool.acquire() as conn:
            result = await conn.fetchval(query, chat_id, user_id, company_id, title or f"Chat {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}")
            return result
    except Exception as e:
        log.error("Failed create_chat (ingest context)", error=str(e))
        raise

async def get_user_chats(user_id: uuid.UUID, company_id: uuid.UUID, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    pool = await get_db_pool()
    query = """SELECT id, title, updated_at FROM chats WHERE user_id = $1 AND company_id = $2 ORDER BY updated_at DESC LIMIT $3 OFFSET $4;"""
    try:
        async with pool.acquire() as conn: rows = await conn.fetch(query, user_id, company_id, limit, offset); return [dict(row) for row in rows]
    except Exception as e: log.error("Failed get_user_chats (ingest context)", error=str(e)); raise

async def check_chat_ownership(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    pool = await get_db_pool()
    query = "SELECT EXISTS (SELECT 1 FROM chats WHERE id = $1 AND user_id = $2 AND company_id = $3);"
    try:
        async with pool.acquire() as conn: exists = await conn.fetchval(query, chat_id, user_id, company_id); return exists is True
    except Exception as e: log.error("Failed check_chat_ownership (ingest context)", error=str(e)); return False

async def get_chat_messages(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    pool = await get_db_pool(); owner = await check_chat_ownership(chat_id, user_id, company_id)
    if not owner: return []
    messages_query = """SELECT id, role, content, sources, created_at FROM messages WHERE chat_id = $1 ORDER BY created_at ASC LIMIT $2 OFFSET $3;"""
    try:
        async with pool.acquire() as conn: message_rows = await conn.fetch(messages_query, chat_id, limit, offset); return [dict(row) for row in message_rows]
    except Exception as e: log.error("Failed get_chat_messages (ingest context)", error=str(e)); raise

async def save_message(chat_id: uuid.UUID, role: str, content: str, sources: Optional[List[Dict[str, Any]]] = None) -> uuid.UUID:
    pool = await get_db_pool(); message_id = uuid.uuid4()
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                update_chat_query = "UPDATE chats SET updated_at = NOW() AT TIME ZONE 'UTC' WHERE id = $1 RETURNING id;"; chat_updated = await conn.fetchval(update_chat_query, chat_id)
                if not chat_updated: raise ValueError(f"Chat {chat_id} not found (ingest context).")
                insert_message_query = """INSERT INTO messages (id, chat_id, role, content, sources, created_at) VALUES ($1, $2, $3, $4, $5, NOW() AT TIME ZONE 'UTC') RETURNING id;"""
                result = await conn.fetchval(insert_message_query, message_id, chat_id, role, content, json.dumps(sources or [])); return result
            except Exception as e: log.error("Failed save_message (ingest context)", error=str(e)); raise

async def delete_chat(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    pool = await get_db_pool()
    query = "DELETE FROM chats WHERE id = $1 AND user_id = $2 AND company_id = $3 RETURNING id;"; delete_log = log.bind(chat_id=str(chat_id), user_id=str(user_id))
    try:
        async with pool.acquire() as conn: deleted_id = await conn.fetchval(query, chat_id, user_id, company_id); return deleted_id is not None
    except Exception as e: delete_log.error("Failed to delete chat (ingest context)", error=str(e)); raise
```

## File: `app\main.py`
```py
# ingest-service/app/main.py
from fastapi import FastAPI, HTTPException, status as fastapi_status, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
import structlog
import uvicorn
import logging
import sys
import asyncio
import time
import uuid
from contextlib import asynccontextmanager # Importar asynccontextmanager

# Configurar logging ANTES de importar otros módulos
from app.core.logging_config import setup_logging
setup_logging()

# Importaciones post-logging
from app.core.config import settings
log = structlog.get_logger("ingest_service.main")
from app.api.v1.endpoints import ingest
from app.db import postgres_client

# Flag global para indicar si el servicio está listo
SERVICE_READY = False
DB_CONNECTION_OK = False # Flag específico para DB

# --- Lifespan Manager (Startup/Shutdown) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global SERVICE_READY, DB_CONNECTION_OK
    log.info("Executing Ingest Service startup sequence...")
    db_pool_ok_startup = False
    try:
        # Intenta obtener y verificar el pool de DB
        await postgres_client.get_db_pool()
        db_pool_ok_startup = await postgres_client.check_db_connection()
        if db_pool_ok_startup:
            log.info("PostgreSQL connection pool initialized and verified successfully.")
            DB_CONNECTION_OK = True
            SERVICE_READY = True # Marcar listo si DB está ok
        else:
            log.critical("PostgreSQL connection check FAILED after pool initialization attempt.")
            DB_CONNECTION_OK = False
            SERVICE_READY = False
    except Exception as e:
        log.critical("CRITICAL FAILURE during PostgreSQL startup verification", error=str(e), exc_info=True)
        DB_CONNECTION_OK = False
        SERVICE_READY = False

    if SERVICE_READY:
        log.info("Ingest Service startup successful. SERVICE IS READY.")
    else:
        log.error("Ingest Service startup completed BUT SERVICE IS NOT READY (DB connection issue).")

    yield # La aplicación se ejecuta aquí

    # --- Shutdown ---
    log.info("Executing Ingest Service shutdown sequence...")
    await postgres_client.close_db_pool()
    log.info("Shutdown sequence complete.")


# --- Creación de la App FastAPI ---
app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.1.2", # Incrementar versión
    description="Microservicio Atenex para ingesta de documentos usando Haystack.",
    lifespan=lifespan
)

# --- Middlewares ---
@app.middleware("http")
async def add_request_context_timing_logging(request: Request, call_next):
    start_time = time.perf_counter()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    # LLM_COMMENT: Bind request context early
    structlog.contextvars.bind_contextvars(request_id=request_id)
    req_log = log.bind(method=request.method, path=request.url.path)
    req_log.info("Request received")
    request.state.request_id = request_id # Store for access in endpoints if needed

    response = None
    try:
        response = await call_next(request)
        process_time_ms = (time.perf_counter() - start_time) * 1000
        # LLM_COMMENT: Bind response context for final log
        resp_log = req_log.bind(status_code=response.status_code, duration_ms=round(process_time_ms, 2))
        log_level = "warning" if 400 <= response.status_code < 500 else "error" if response.status_code >= 500 else "info"
        getattr(resp_log, log_level)("Request finished")
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
    except Exception as e:
        process_time_ms = (time.perf_counter() - start_time) * 1000
        # LLM_COMMENT: Log unhandled exceptions at middleware level
        exc_log = req_log.bind(status_code=500, duration_ms=round(process_time_ms, 2))
        exc_log.exception("Unhandled exception during request processing")
        response = JSONResponse(
            status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal Server Error"}
        )
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
    finally:
         # LLM_COMMENT: Clear contextvars after request is done
         structlog.contextvars.clear_contextvars()
    return response

# --- Exception Handlers ---
@app.exception_handler(ResponseValidationError)
async def response_validation_exception_handler(request: Request, exc: ResponseValidationError):
    log.error("Response Validation Error", errors=exc.errors(), exc_info=True)
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Error de validación en la respuesta", "errors": exc.errors()},
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    log_level = log.warning if exc.status_code < 500 else log.error
    log_level("HTTP Exception", status_code=exc.status_code, detail=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail if isinstance(exc.detail, str) else "Error HTTP"},
        headers=getattr(exc, "headers", None)
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    log.warning("Request Validation Error", errors=exc.errors())
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Error de validación en la petición", "errors": exc.errors()},
    )

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.exception("Excepción no controlada") # Log con traceback
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Error interno del servidor"}
    )

# --- Router Inclusion ---
# ¡¡¡¡NUNCA MODIFICAR ESTA LÍNEA NI EL PREFIJO DE RUTA!!!
# El prefijo DEBE ser settings.API_V1_STR == '/api/v1/ingest' para que el API Gateway funcione correctamente.
# Si cambias esto, romperás la integración y el proxy de rutas. Si tienes dudas, consulta con el equipo de plataforma.
app.include_router(ingest.router, prefix=settings.API_V1_STR, tags=["Ingestion"])
log.info(f"Included ingestion router with prefix: {settings.API_V1_STR}")

# --- Root Endpoint / Health Check ---
@app.get("/", tags=["Health Check"], status_code=fastapi_status.HTTP_200_OK, response_class=PlainTextResponse)
async def health_check():
    """
    Simple health check endpoint. Returns 200 OK if the app is running.
    """
    return PlainTextResponse("OK", status_code=fastapi_status.HTTP_200_OK)

# --- Local execution ---
if __name__ == "__main__":
    port = 8001 # Default port for ingest-service
    log_level_str = settings.LOG_LEVEL.lower()
    print(f"----- Starting {settings.PROJECT_NAME} locally on port {port} -----")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True, log_level=log_level_str)
```

## File: `app\models\__init__.py`
```py

```

## File: `app\models\domain.py`
```py
from enum import Enum

class DocumentStatus(str, Enum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    PROCESSED = "processed"
    INDEXED = "indexed" # Podríamos unir processed e indexed
    ERROR = "error"
```

## File: `app\services\__init__.py`
```py

```

## File: `app\services\base_client.py`
```py
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import structlog
from typing import Any, Dict, Optional

from app.core.config import settings

log = structlog.get_logger(__name__)

class BaseServiceClient:
    """Cliente HTTP base asíncrono con reintentos."""

    def __init__(self, base_url: str, service_name: str):
        self.base_url = base_url
        self.service_name = service_name
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=settings.HTTP_CLIENT_TIMEOUT
        )

    async def close(self):
        """Cierra el cliente HTTP."""
        await self.client.aclose()
        log.info(f"{self.service_name} client closed.")

    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES),
        wait=wait_exponential(multiplier=1, min=settings.HTTP_CLIENT_BACKOFF_FACTOR, max=10),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError)),
        reraise=True, # Vuelve a lanzar la excepción después de los reintentos
        before_sleep=lambda retry_state: log.warning(
            f"Retrying {retry_state.fn.__name__} for {self.service_name}",
            attempt=retry_state.attempt_number,
            wait_time=retry_state.next_action.sleep,
            error=retry_state.outcome.exception()
        )
    )
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        """Realiza una petición HTTP con reintentos."""
        log.debug(f"Requesting {self.service_name}", method=method, endpoint=endpoint, params=params)
        try:
            response = await self.client.request(
                method=method,
                url=endpoint,
                params=params,
                json=json,
                data=data,
                files=files,
                headers=headers,
            )
            response.raise_for_status()
            log.info(f"Received response from {self.service_name}", status_code=response.status_code)
            return response
        except httpx.HTTPStatusError as e:
            log.error(f"HTTP error from {self.service_name}", status_code=e.response.status_code, detail=e.response.text)
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            log.error(f"Network error when calling {self.service_name}", error=str(e))
            raise
        except Exception as e:
            log.error(f"Unexpected error when calling {self.service_name}", error=str(e), exc_info=True)
            raise
```

## File: `app\services\minio_client.py`
```py
# ingest-service/app/services/minio_client.py
import io
import uuid
from typing import IO, BinaryIO
from minio import Minio
from minio.error import S3Error
import structlog
import asyncio

from app.core.config import settings

log = structlog.get_logger(__name__)

class MinioStorageClient:
    """Cliente para interactuar con MinIO usando el bucket configurado."""

    def __init__(self):
        self.bucket_name = settings.MINIO_BUCKET_NAME
        try:
            self.client = Minio(
                settings.MINIO_ENDPOINT,
                access_key=settings.MINIO_ACCESS_KEY.get_secret_value(),
                secret_key=settings.MINIO_SECRET_KEY.get_secret_value(),
                secure=settings.MINIO_USE_SECURE
            )
            self._ensure_bucket_exists()
            log.info("MinIO client initialized", endpoint=settings.MINIO_ENDPOINT, bucket=self.bucket_name)
        except Exception as e:
            log.critical("CRITICAL: Failed to initialize MinIO client", bucket=self.bucket_name, error=str(e), exc_info=True)
            raise RuntimeError(f"MinIO client initialization failed: {e}") from e

    def _ensure_bucket_exists(self):
        """Crea el bucket especificado si no existe (síncrono)."""
        try:
            found = self.client.bucket_exists(self.bucket_name)
            if not found:
                self.client.make_bucket(self.bucket_name)
                log.info(f"MinIO bucket '{self.bucket_name}' created.")
            else:
                log.debug(f"MinIO bucket '{self.bucket_name}' already exists.")
        except S3Error as e:
            log.error(f"Error checking/creating MinIO bucket '{self.bucket_name}'", error=str(e), exc_info=True)
            raise

    async def upload_file(
        self,
        company_id: uuid.UUID,
        document_id: uuid.UUID,
        file_name: str,
        file_content_stream: IO[bytes],
        content_type: str,
        content_length: int
    ) -> str:
        object_name = f"{str(company_id)}/{str(document_id)}/{file_name}"
        upload_log = log.bind(bucket=self.bucket_name, object_name=object_name, content_type=content_type, length=content_length)
        upload_log.info("Queueing file upload to MinIO executor")

        loop = asyncio.get_running_loop()
        def _upload():
            file_content_stream.seek(0)
            return self.client.put_object(
                bucket_name=self.bucket_name,
                object_name=object_name,
                data=file_content_stream,
                length=content_length,
                content_type=content_type
            )
        try:
            await loop.run_in_executor(None, _upload)
            upload_log.info("File uploaded successfully to MinIO via executor")
            return object_name
        except S3Error as e:
            upload_log.error("Failed to upload file to MinIO", error=str(e), code=e.code)
            raise IOError(f"Failed to upload to storage: {e.code}") from e
        except Exception as e:
            upload_log.error("Unexpected error during file upload", error=str(e), exc_info=True)
            raise IOError("Unexpected storage upload error") from e

    def download_file_stream_sync(self, object_name: str) -> io.BytesIO:
        """Operación SÍNCRONA para descargar un archivo a BytesIO."""
        download_log = log.bind(bucket=self.bucket_name, object_name=object_name)
        download_log.info("Downloading file from MinIO (sync operation starting)...")
        response = None
        try:
            response = self.client.get_object(self.bucket_name, object_name)
            file_data = response.read()
            file_stream = io.BytesIO(file_data)
            download_log.info(f"File downloaded successfully from MinIO (sync, {len(file_data)} bytes)")
            file_stream.seek(0)
            return file_stream
        except S3Error as e:
            download_log.error("Failed to download file from MinIO (sync)", error=str(e), code=e.code, exc_info=False)
            if e.code == 'NoSuchKey':
                 raise FileNotFoundError(f"Object not found in MinIO bucket '{self.bucket_name}': {object_name}") from e
            else:
                 raise IOError(f"S3 error downloading file {object_name}: {e.code}") from e
        except Exception as e:
             download_log.error("Unexpected error during sync file download", error=str(e), exc_info=True)
             raise IOError(f"Unexpected error downloading file {object_name}") from e
        finally:
            if response:
                response.close()
                response.release_conn()

    async def download_file_stream(self, object_name: str) -> io.BytesIO:
        """Descarga un archivo de MinIO como BytesIO de forma asíncrona."""
        download_log = log.bind(bucket=self.bucket_name, object_name=object_name)
        download_log.info("Queueing file download from MinIO executor")
        loop = asyncio.get_running_loop()
        try:
            file_stream = await loop.run_in_executor(None, self.download_file_stream_sync, object_name)
            download_log.info("File download successful via executor")
            return file_stream
        except FileNotFoundError:
            download_log.error("File not found in MinIO via executor", object_name=object_name)
            raise
        except Exception as e:
            download_log.error("Error downloading file via executor", error=str(e), error_type=type(e).__name__, exc_info=True)
            raise IOError(f"Failed to download file via executor: {e}") from e

    async def file_exists(self, object_name: str) -> bool:
        check_log = log.bind(bucket=self.bucket_name, object_name=object_name)
        loop = asyncio.get_running_loop()
        def _stat():
            self.client.stat_object(self.bucket_name, object_name)
            return True
        try:
            return await loop.run_in_executor(None, _stat)
        except S3Error as e:
            if getattr(e, 'code', None) in ('NoSuchKey', 'NoSuchBucket'):
                check_log.warning("Object not found in MinIO", code=e.code)
                return False
            check_log.error("Error checking MinIO object existence", error=str(e), code=e.code)
            raise IOError(f"Error checking storage existence: {e.code}") from e
        except Exception as e:
            check_log.error("Unexpected error checking MinIO object existence", error=str(e), exc_info=True)
            raise IOError("Unexpected error checking storage existence") from e

    async def delete_file(self, object_name: str) -> None:
        delete_log = log.bind(bucket=self.bucket_name, object_name=object_name)
        delete_log.info("Queueing file deletion from MinIO executor")
        loop = asyncio.get_running_loop()
        def _remove():
            self.client.remove_object(self.bucket_name, object_name)
        try:
            await loop.run_in_executor(None, _remove)
            delete_log.info("File deleted successfully from MinIO")
        except S3Error as e:
            delete_log.error("Failed to delete file from MinIO", error=str(e), code=e.code)
            # No raise para que eliminación parcial no bloquee flujo
        except Exception as e:
            delete_log.error("Unexpected error during file deletion", error=str(e), exc_info=True)
            raise IOError("Unexpected storage deletion error") from e
```

## File: `app\tasks\__init__.py`
```py

```

## File: `app\tasks\celery_app.py`
```py
from celery import Celery
from app.core.config import settings
import structlog

log = structlog.get_logger(__name__)

celery_app = Celery(
    "ingest_tasks",
    broker=str(settings.CELERY_BROKER_URL),
    backend=str(settings.CELERY_RESULT_BACKEND),
    include=["app.tasks.process_document"] # Importante para que Celery descubra la tarea
)

# Configuración opcional de Celery
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Ajustar concurrencia y otros parámetros según sea necesario
    # worker_concurrency=4,
    task_track_started=True,
    # Configuración de reintentos por defecto (puede sobreescribirse por tarea)
    task_reject_on_worker_lost=True,
    task_acks_late=True,
)

log.info("Celery app configured", broker=settings.CELERY_BROKER_URL)
```

## File: `app\tasks\process_document.py`
```py
# ingest-service/app/tasks/process_document.py
import uuid
import asyncio
from typing import Dict, Any, Optional, List, Type
import tempfile
import os
from pathlib import Path
import structlog
import io
import time
import traceback # Para formatear excepciones

# LLM_COMMENT: Keep asyncpg import
import asyncpg

# --- Haystack Imports ---
from haystack import Pipeline, Document
from haystack.utils import Secret
from haystack.components.converters import (
    PyPDFToDocument, TextFileToDocument, MarkdownToDocument,
    HTMLToDocument, DOCXToDocument,
)
from haystack.components.preprocessors import DocumentSplitter
# LLM_COMMENT: Keep OpenAI embedder for document ingestion
from haystack.components.embedders import OpenAIDocumentEmbedder
from milvus_haystack import MilvusDocumentStore # Importación correcta
# Importar excepciones de Milvus para manejo específico si es necesario
from pymilvus.exceptions import MilvusException
# Importar excepciones de Minio para manejo específico
from minio.error import S3Error
from haystack.components.writers import DocumentWriter
from haystack.dataclasses import ByteStream

# --- Local Imports ---
from app.tasks.celery_app import celery_app
from app.core.config import settings
from app.db import postgres_client # Cliente DB async
from app.models.domain import DocumentStatus
from app.services.minio_client import MinioStorageClient # Cliente MinIO async

log = structlog.get_logger(__name__)

# --- Funciones Helper Síncronas para Haystack (Sin cambios) ---
# LLM_COMMENT: Initialization helpers remain the same logic

def _initialize_milvus_store() -> MilvusDocumentStore:
    """Función interna SÍNCRONA para inicializar MilvusDocumentStore."""
    init_log = log.bind(component="MilvusDocumentStore")
    init_log.info("Attempting to initialize...")
    try:
        store = MilvusDocumentStore(
            connection_args={"uri": str(settings.MILVUS_URI)},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            dim=settings.EMBEDDING_DIMENSION, # LLM_COMMENT: Crucial dimension setting
            embedding_field=settings.MILVUS_EMBEDDING_FIELD,
            content_field=settings.MILVUS_CONTENT_FIELD,
            metadata_fields=settings.MILVUS_METADATA_FIELDS,
            index_params=settings.MILVUS_INDEX_PARAMS,
            search_params=settings.MILVUS_SEARCH_PARAMS,
            consistency_level="Strong",
        )
        init_log.info("Initialization successful.")
        return store
    except MilvusException as me:
        init_log.error("Milvus connection/initialization failed", code=getattr(me, 'code', None), message=str(me), exc_info=True)
        raise ConnectionError(f"Milvus connection failed: {me}") from me
    except Exception as e:
        init_log.exception("Unexpected error during MilvusDocumentStore initialization")
        raise RuntimeError(f"Unexpected Milvus init error: {e}") from e

def _initialize_openai_embedder() -> OpenAIDocumentEmbedder:
    """Función interna SÍNCRONA para inicializar OpenAIDocumentEmbedder."""
    init_log = log.bind(component="OpenAIDocumentEmbedder")
    init_log.info("Initializing...")
    api_key_value = settings.OPENAI_API_KEY.get_secret_value()
    if not api_key_value:
        init_log.error("OpenAI API Key is missing!")
        raise ValueError("OpenAI API Key is required.")
    embedder = OpenAIDocumentEmbedder(
        api_key=Secret.from_token(api_key_value),
        model=settings.OPENAI_EMBEDDING_MODEL,
        # LLM_COMMENT: meta_fields_to_embed might be useful if metadata should influence embeddings
        meta_fields_to_embed=[]
    )
    init_log.info("Initialization successful.", model=settings.OPENAI_EMBEDDING_MODEL)
    return embedder

def _initialize_splitter() -> DocumentSplitter:
    """Función interna SÍNCRONA para inicializar DocumentSplitter."""
    init_log = log.bind(component="DocumentSplitter")
    init_log.info("Initializing...")
    splitter = DocumentSplitter(
        split_by=settings.SPLITTER_SPLIT_BY,
        split_length=settings.SPLITTER_CHUNK_SIZE,
        split_overlap=settings.SPLITTER_CHUNK_OVERLAP
    )
    init_log.info("Initialization successful.", split_by=settings.SPLITTER_SPLIT_BY, length=settings.SPLITTER_CHUNK_SIZE)
    return splitter

def _initialize_document_writer(store: MilvusDocumentStore) -> DocumentWriter:
    """Función interna SÍNCRONA para inicializar DocumentWriter."""
    init_log = log.bind(component="DocumentWriter")
    init_log.info("Initializing...")
    # LLM_COMMENT: Ensure policy matches desired behavior (e.g., WRITE, SKIP, OVERWRITE)
    # Default is WRITE (adds new documents, fails on ID conflict)
    writer = DocumentWriter(document_store=store, policy="WRITE")
    init_log.info("Initialization successful.")
    return writer

# LLM_COMMENT: Converter selection logic remains the same
def get_converter_for_content_type(content_type: str) -> Optional[Type]:
    """Devuelve la clase del conversor Haystack apropiada para el tipo de archivo."""
    converters = {
        "application/pdf": PyPDFToDocument,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DOCXToDocument,
        "application/msword": DOCXToDocument,
        "text/plain": TextFileToDocument,
        "text/markdown": MarkdownToDocument,
        "text/html": HTMLToDocument,
        # LLM_COMMENT: Explicitly map unsupported types to None
        "application/vnd.ms-excel": None,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": None,
        "image/png": None,
        "image/jpeg": None,
        "image/jpg": None,
    }
    # LLM_COMMENT: Normalize content type lookup
    normalized_content_type = content_type.lower().split(';')[0].strip()
    converter = converters.get(normalized_content_type)
    if converter is None:
        # LLM_COMMENT: Raise specific error for unsupported types
        log.warning("Unsupported content type received for conversion", provided_type=content_type, normalized_type=normalized_content_type)
        raise ValueError(f"Tipo de archivo '{normalized_content_type}' no soportado actualmente.") # User-facing error
    return converter

# --- Celery Task Definition ---
# LLM_COMMENT: Keep retry logic, potentially refine errors based on experience
NON_RETRYABLE_ERRORS = (FileNotFoundError, ValueError, TypeError, NotImplementedError, KeyError, AttributeError, asyncpg.exceptions.DataError, asyncpg.exceptions.IntegrityConstraintViolationError)
RETRYABLE_ERRORS = (IOError, ConnectionError, TimeoutError, S3Error, MilvusException, asyncpg.exceptions.PostgresConnectionError, asyncpg.exceptions.InterfaceError, Exception) # Added InterfaceError

@celery_app.task(
    bind=True,
    autoretry_for=RETRYABLE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=300, # 5 minutos máximo backoff
    retry_jitter=True,
    retry_kwargs={'max_retries': 3}, # Reintentar 3 veces
    reject_on_worker_lost=True,
    acks_late=True,
    name="tasks.process_document_haystack"
)
def process_document_haystack_task(
    self, # Instancia de la tarea Celery
    document_id_str: str,
    company_id_str: str,
    minio_object_name: str,
    file_name: str,
    content_type: str,
    original_metadata: Dict[str, Any],
):
    """Tarea Celery para procesar un documento usando Haystack."""
    document_id = uuid.UUID(document_id_str)
    company_id = uuid.UUID(company_id_str)
    task_log = log.bind(
        document_id=str(document_id),
        company_id=str(company_id),
        task_id=self.request.id or "unknown",
        attempt=self.request.retries + 1,
        filename=file_name,
        content_type=content_type
    )
    task_log.info("Starting Haystack document processing task execution")

    # --- Función async interna para orquestar el flujo ---
    async def async_process_flow():
        minio_client = None
        downloaded_file_stream: Optional[io.BytesIO] = None
        pipeline: Optional[Pipeline] = None
        processed_chunk_count = 0 # Initialize count

        try:
            # 0. Marcar como PROCESSING en DB
            task_log.info("Updating document status to PROCESSING")
            await postgres_client.update_document_status(document_id, DocumentStatus.PROCESSING, error_message=None) # Clear previous error

            # 1. Descargar archivo de MinIO
            task_log.info("Downloading file from MinIO")
            minio_client = MinioStorageClient()
            downloaded_file_stream = await minio_client.download_file_stream(minio_object_name)
            file_bytes = downloaded_file_stream.getvalue()
            if not file_bytes:
                raise ValueError("Downloaded file is empty.")
            task_log.info(f"File downloaded successfully ({len(file_bytes)} bytes)")

            # 2. Inicializar componentes Haystack y construir pipeline
            task_log.info("Initializing Haystack components and building pipeline via executor...")
            loop = asyncio.get_running_loop()
            # LLM_COMMENT: Run sync Haystack initializations in executor
            store = await loop.run_in_executor(None, _initialize_milvus_store)
            embedder = await loop.run_in_executor(None, _initialize_openai_embedder)
            splitter = await loop.run_in_executor(None, _initialize_splitter)
            writer = await loop.run_in_executor(None, _initialize_document_writer, store)

            ConverterClass = get_converter_for_content_type(content_type)
            # LLM_COMMENT: No need to check if ConverterClass is None, error is raised inside the function
            converter_instance = ConverterClass()

            # LLM_COMMENT: Define pipeline structure (unchanged)
            pipeline = Pipeline()
            pipeline.add_component("converter", converter_instance)
            pipeline.add_component("splitter", splitter)
            pipeline.add_component("embedder", embedder)
            pipeline.add_component("writer", writer)
            pipeline.connect("converter.documents", "splitter.documents")
            pipeline.connect("splitter.documents", "embedder.documents")
            pipeline.connect("embedder.documents", "writer.documents")
            task_log.info("Haystack pipeline built successfully.")

            # 3. Preparar Metadatos y ByteStream
            allowed_meta_keys = set(settings.MILVUS_METADATA_FIELDS)
            # LLM_COMMENT: Ensure mandatory fields for filtering/identification are present
            doc_meta = {
                "company_id": str(company_id),
                "document_id": str(document_id),
                "file_name": file_name or "unknown",
                "file_type": content_type or "unknown",
                # LLM_COMMENT: Add original upload timestamp if available and needed
                # "uploaded_at": datetime.now(timezone.utc).isoformat() # Example
            }
            # LLM_COMMENT: Merge original metadata safely, ensuring no overwrite of crucial keys
            added_original_meta = 0
            for key, value in original_metadata.items():
                if key in allowed_meta_keys and key not in doc_meta:
                    # LLM_COMMENT: Convert values to string for broader compatibility with Milvus metadata
                    doc_meta[key] = str(value) if value is not None else None
                    added_original_meta += 1
            task_log.debug("Prepared metadata for Haystack Document", final_meta=doc_meta, added_original_count=added_original_meta)

            source_stream = ByteStream(data=file_bytes, meta=doc_meta)
            pipeline_input = {"converter": {"sources": [source_stream]}}

            # 4. Ejecutar Pipeline Haystack
            task_log.info("Running Haystack pipeline via executor...")
            start_time = time.monotonic()
            # LLM_COMMENT: Run the potentially long-running pipeline.run in executor
            pipeline_result = await loop.run_in_executor(None, pipeline.run, pipeline_input)
            duration = time.monotonic() - start_time
            task_log.info(f"Haystack pipeline execution finished", duration_sec=round(duration, 2))

            # 5. Procesar Resultado y Contar Chunks
            # LLM_COMMENT: Check writer output for document count
            writer_output = pipeline_result.get("writer", {})
            if isinstance(writer_output, dict) and "documents_written" in writer_output:
                processed_chunk_count = writer_output["documents_written"]
                task_log.info(f"Chunks written to Milvus determined by writer: {processed_chunk_count}")
            else:
                # LLM_COMMENT: Fallback: check splitter output if writer doesn't report count
                task_log.warning("Writer output missing 'documents_written', attempting fallback count from splitter", writer_output=writer_output)
                splitter_output = pipeline_result.get("splitter", {})
                if isinstance(splitter_output, dict) and "documents" in splitter_output and isinstance(splitter_output["documents"], list):
                     processed_chunk_count = len(splitter_output["documents"])
                     task_log.warning(f"Inferred chunk count from splitter output: {processed_chunk_count}")
                else:
                    # LLM_COMMENT: If count cannot be determined, log error and potentially mark as error
                    task_log.error("Pipeline finished but failed to determine processed chunk count.", pipeline_output=pipeline_result)
                    # LLM_COMMENT: Decide if 0 chunks is an error or valid (e.g., empty doc after conversion)
                    # Raising an error here will mark the task as failed.
                    raise RuntimeError("Pipeline execution yielded unclear results regarding written documents.")

            if processed_chunk_count == 0:
                 task_log.warning("Pipeline ran successfully but resulted in 0 chunks being written to Milvus.")
                 # LLM_COMMENT: Mark as processed with 0 chunks, not necessarily an error unless expected otherwise.

            # 6. Actualizar Estado Final en DB
            # LLM_COMMENT: Use PROCESSED status after successful pipeline run
            final_status = DocumentStatus.PROCESSED
            task_log.info(f"Updating document status to {final_status.value} with chunk count {processed_chunk_count}.")
            await postgres_client.update_document_status(
                document_id=document_id,
                status=final_status,
                chunk_count=processed_chunk_count,
                error_message=None # LLM_COMMENT: Clear error message on success
            )
            task_log.info("Document status updated successfully in PostgreSQL.")

        # LLM_COMMENT: Handle non-retryable errors (e.g., bad file type, config issues)
        except NON_RETRYABLE_ERRORS as e_non_retry:
            err_type = type(e_non_retry).__name__
            err_msg_detail = str(e_non_retry)[:500] # Truncate long messages
            # LLM_COMMENT: Provide a more user-friendly message for common non-retryable errors
            if isinstance(e_non_retry, FileNotFoundError):
                 user_error_msg = "Error Interno: No se encontró el archivo original en el almacenamiento."
            elif isinstance(e_non_retry, ValueError) and "Unsupported content type" in err_msg_detail:
                 user_error_msg = err_msg_detail # Use the specific message from get_converter
            elif isinstance(e_non_retry, ValueError) and "API Key" in err_msg_detail:
                 user_error_msg = "Error de Configuración: Falta la clave API necesaria."
            else:
                 user_error_msg = f"Error irrecuperable durante el procesamiento ({err_type}). Contacte a soporte si persiste."

            formatted_traceback = traceback.format_exc()
            task_log.error(f"Processing failed permanently: {err_type}: {err_msg_detail}", traceback=formatted_traceback)
            try:
                await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=user_error_msg)
            except Exception as db_err:
                task_log.critical("Failed update status to ERROR after non-retryable failure!", db_error=str(db_err))
            # Do not re-raise, let Celery mark task as failed

        # LLM_COMMENT: Handle retryable errors (e.g., network, temporary service issues)
        except RETRYABLE_ERRORS as e_retry:
            err_type = type(e_retry).__name__
            err_msg_detail = str(e_retry)[:500]
            max_retries = self.max_retries if hasattr(self, 'max_retries') else 3 # Get max retries safely
            current_attempt = self.request.retries + 1
            # LLM_COMMENT: User-friendly message indicating a temporary issue
            user_error_msg = f"Error temporal durante procesamiento ({err_type} - Intento {current_attempt}/{max_retries+1}). Reintentando automáticamente."

            task_log.warning(f"Processing failed, will retry: {err_type}: {err_msg_detail}", traceback=traceback.format_exc())
            try:
                # LLM_COMMENT: Update status to ERROR but include the user-friendly temporary error message
                await postgres_client.update_document_status(
                    document_id, DocumentStatus.ERROR, error_message=user_error_msg
                )
            except Exception as db_err:
                task_log.error("Failed update status to ERROR during retryable failure!", db_error=str(db_err))
            # Re-raise the exception for Celery to handle the retry
            raise e_retry

        finally:
            # LLM_COMMENT: Ensure resources like file streams are closed
            if downloaded_file_stream:
                downloaded_file_stream.close()
            task_log.debug("Cleaned up task resources.")

    # --- Ejecutar el flujo async ---
    try:
        # LLM_COMMENT: Set a reasonable timeout for the entire async flow within the task
        TIMEOUT_SECONDS = 600 # 10 minutes, adjust as needed
        try:
            asyncio.run(asyncio.wait_for(async_process_flow(), timeout=TIMEOUT_SECONDS))
            task_log.info("Haystack document processing task completed successfully.")
            return {"status": "success", "document_id": str(document_id), "chunk_count": processed_chunk_count} # LLM_COMMENT: Return chunk count on success
        except asyncio.TimeoutError:
            timeout_msg = f"Procesamiento excedió el tiempo límite de {TIMEOUT_SECONDS} segundos."
            task_log.error(timeout_msg)
            user_error_msg = "El procesamiento del documento tardó demasiado y fue cancelado."
            try:
                # LLM_COMMENT: Ensure status is updated to ERROR on timeout
                asyncio.run(postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=user_error_msg))
            except Exception as db_err:
                task_log.critical("Failed to update status to ERROR after timeout!", db_error=str(db_err))
            # LLM_COMMENT: Return failure status on timeout
            return {"status": "failure", "document_id": str(document_id), "error": user_error_msg}

    except Exception as top_level_exc:
        # LLM_COMMENT: Catch-all for final failures after retries or unexpected sync errors
        user_error_msg = "Ocurrió un error final inesperado durante el procesamiento. Contacte a soporte."
        task_log.exception("Haystack processing task failed at top level (after retries or sync error). Final failure.", exc_info=top_level_exc)
        try:
            # LLM_COMMENT: Ensure final status is ERROR
            asyncio.run(postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=user_error_msg))
        except Exception as db_err:
            task_log.critical("Failed to update status to ERROR after top-level failure!", db_error=str(db_err))
        # LLM_COMMENT: Return failure status
        return {"status": "failure", "document_id": str(document_id), "error": user_error_msg}
```

## File: `app\utils\__init__.py`
```py

```

## File: `app\utils\helpers.py`
```py

```

## File: `pyproject.toml`
```toml
[tool.poetry]
name = "ingest-service"
version = "0.1.1" # Incremento de versión
description = "Ingest service for Atenex B2B SaaS (Haystack/Postgres/Minio/Milvus)" # Descripción actualizada
authors = ["Atenex Team <dev@atenex.com>"] # Autor actualizado
readme = "README.md"

[tool.poetry.dependencies]
python = "^3.10"
fastapi = "^0.110.0"
uvicorn = {extras = ["standard"], version = "^0.28.0"}
gunicorn = "^21.2.0"
pydantic = {extras = ["email"], version = "^2.6.4"}
pydantic-settings = "^2.2.1"
celery = {extras = ["redis"], version = "^5.3.6"}
gevent = "^23.9.1" # Necesario para el pool de workers de Celery
# --- CORRECCIÓN: Eliminar la primera definición duplicada de httpx ---
# httpx = "^0.27.0" # <-- ESTA LÍNEA SE ELIMINA
asyncpg = "^0.29.0" # Para PostgreSQL directo
# python-jose no es necesario aquí si no se validan tokens
tenacity = "^8.2.3"
python-multipart = "^0.0.9" # Para subir archivos
structlog = "^24.1.0"
# aiofiles no es estrictamente necesario si usamos BytesIO directamente con MinIO
minio = "^7.1.17"

# --- Haystack Dependencies ---
haystack-ai = "^2.0.1" # O la versión estable que uses de Haystack 2.x
openai = "^1.14.3" # Para embeddings
# --- Asegurar pymilvus explícitamente ---
pymilvus = "^2.4.1" # Verifica compatibilidad con tu versión de Milvus
milvus-haystack = "^0.0.6" # Integración Milvus con Haystack 2.x

# --- Haystack Converter Dependencies ---
pypdf = "^4.0.1" # Para PDFs
python-docx = "^1.1.0" # Para DOCX
markdown = "^3.5.1" # Para Markdown (asegura última versión)
beautifulsoup4 = "^4.12.3" # Para HTML

# --- CORRECCIÓN: Mantener esta definición completa de httpx ---
# Cliente HTTP asíncrono (se mantiene esta con extras)
httpx = {extras = ["http2"], version = "^0.27.0"}
# Dependencia necesaria para httpx[http2]
h2 = "^4.1.0"

[tool.poetry.group.dev.dependencies] # Grupo dev corregido
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"
# httpx ya está en dependencias principales, no necesita repetirse aquí para test client
# Añadir otros como black, ruff, mypy si los usas

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
