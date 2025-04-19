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
            # *** RE-VERIFICACIÓN: Usar embedding_dim consistentemente ***
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
             milvus_log.error("Milvus connection/query error in get_milvus_chunk_count", error=str(me), code=getattr(me, 'code', None), exc_info=False)
             return -1 # Indicar error en el conteo
        except TypeError as te:
             # Capturar específicamente el TypeError visto en los logs
             milvus_log.error(f"MilvusDocumentStore init TypeError in count check: {te}. Check arguments.", exc_info=False)
             return -1 # Indicar error
        except Exception as e:
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
    # Sanitize filename for MinIO object name if necessary (optional)
    safe_filename = "".join(c if c.isalnum() or c in ['.', '-', '_'] else '_' for c in file.filename)
    object_name = f"{str(company_id)}/{str(document_id)}/{safe_filename}"

    try:
        # 1) Persistir registro inicial en DB (Estado UPLOADED)
        await postgres_client.create_document(document_id, company_id, file.filename, content_type, metadata)

        # 2) Subir archivo a MinIO
        file_bytes = await file.read()
        file_stream = io.BytesIO(file_bytes)
        await minio_client.upload_file(company_id, document_id, safe_filename, file_stream, content_type, len(file_bytes)) # Use safe_filename

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
             elif milvus_count >= 0 and current_status != DocumentStatus.ERROR and original_chunk_count != milvus_count:
                  # Si el conteo de Milvus es válido y difiere del de la DB (y no estamos ya en Error)
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
            # *** RE-VERIFICACIÓN: Usar embedding_dim consistentemente ***
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
        # Capturar el TypeError específico aquí también
        if isinstance(milvus_err, TypeError) and 'embedding_dim' in str(milvus_err):
             error_msg = f"Milvus TypeError on delete (check arguments like embedding_dim): {milvus_err}"
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

    # Si hubo errores críticos (falla en PG), devolver 500.
    # Si solo falló Milvus/Minio, loguear pero devolver 204 igual.
    if any("PostgreSQL" in err for err in errors):
        delete_log.error("Document deletion failed critically (PostgreSQL)", errors=errors)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Document deletion failed for critical components: {'; '.join(errors)}")
    elif errors:
         delete_log.warning("Document deletion completed with non-critical errors (Milvus/MinIO)", errors=errors)

    delete_log.info("Document deletion process finished.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

## File: `app\api\v1\schemas.py`
```py
# ingest-service/app/api/v1/schemas.py
import uuid
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Dict, Any, List
from app.models.domain import DocumentStatus
from datetime import datetime
import json

class IngestResponse(BaseModel):
    document_id: uuid.UUID
    task_id: str
    status: str
    message: str = "Document upload received and queued for processing."

    class Config:
        json_schema_extra = { # Corregido de schema_extra
            "example": {
                "document_id": "123e4567-e89b-12d3-a456-426614174000",
                "task_id": "abcd1234efgh",
                "status": "uploaded", # Estado inicial devuelto por la API
                "message": "Document upload received and queued for processing."
            }
        }

class StatusResponse(BaseModel):
    document_id: uuid.UUID = Field(..., alias="id")
    company_id: uuid.UUID
    file_name: str
    file_type: str
    file_path: Optional[str] = None # Puede ser None inicialmente
    metadata: Optional[Dict[str, Any]] = None
    status: str
    chunk_count: Optional[int] = 0 # Default a 0, puede ser None si hay error
    error_message: Optional[str] = None
    uploaded_at: datetime
    updated_at: datetime

    # Fields added dynamically by status endpoints
    minio_exists: Optional[bool] = None # Puede ser None si no se pudo verificar
    milvus_chunk_count: Optional[int] = None # Puede ser None si no se pudo verificar o -1 si hubo error
    message: Optional[str] = None # Mensaje descriptivo del estado actual

    # Validador para convertir metadata de string a dict si es necesario
    @field_validator('metadata', mode='before')
    def metadata_to_dict(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return {"error": "invalid metadata JSON in DB"}
        return v

    class Config:
        validate_assignment = True # Permitir validar al asignar campos dinámicos
        populate_by_name = True # Permitir usar 'id' en lugar de 'document_id'
        json_schema_extra = { # Corregido de schema_extra
            "example": {
                "id": "123e4567-e89b-12d3-a456-426614174000",
                "company_id": "51a66c8f-f6b1-43bd-8038-8768471a8b09",
                "file_name": "document.pdf",
                "file_type": "application/pdf",
                "file_path": "51a66c8f-f6b1-43bd-8038-8768471a8b09/123e4567-e89b-12d3-a456-426614174000/document.pdf",
                "metadata": {"source": "web upload"},
                "status": "processed",
                "chunk_count": 10, # DB value
                "error_message": None,
                "uploaded_at": "2025-04-18T20:00:00Z",
                "updated_at": "2025-04-18T20:30:00Z",
                "minio_exists": True, # Realtime check
                "milvus_chunk_count": 10, # Realtime check (-1 if error)
                "message": "Documento procesado correctamente." # Descriptive message
            }
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
from pydantic import (
    RedisDsn, AnyHttpUrl, SecretStr, Field, field_validator, ValidationError,
    ValidationInfo
)
import sys
import json

# --- Service Names en K8s ---
POSTGRES_K8S_SVC = "postgresql-service.nyro-develop.svc.cluster.local" # Corregido nombre servicio
MINIO_K8S_SVC = "minio-service.nyro-develop.svc.cluster.local"
MILVUS_K8S_SVC = "milvus-milvus.default.svc.cluster.local" # Servicio en namespace 'default'
REDIS_K8S_SVC = "redis-service-master.nyro-develop.svc.cluster.local"

# --- Defaults ---
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex"
POSTGRES_K8S_USER_DEFAULT = "postgres"
MINIO_K8S_PORT_DEFAULT = 9000
MINIO_BUCKET_DEFAULT = "ingested-documents"
MILVUS_K8S_PORT_DEFAULT = 19530
MILVUS_DEFAULT_COLLECTION = "document_chunks_haystack"
MILVUS_DEFAULT_INDEX_PARAMS = '{"metric_type": "COSINE", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 256}}'
MILVUS_DEFAULT_SEARCH_PARAMS = '{"metric_type": "COSINE", "params": {"ef": 128}}'
OPENAI_DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_DIM = 1536 # Dimension for text-embedding-3-small & ada-002

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env', env_prefix='INGEST_', env_file_encoding='utf-8',
        case_sensitive=False, extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = "Atenex Ingest Service"
    API_V1_STR: str = "/api/v1/ingest"
    LOG_LEVEL: str = "INFO"

    # --- Celery ---
    CELERY_BROKER_URL: RedisDsn = Field(default_factory=lambda: RedisDsn(f"redis://{REDIS_K8S_SVC}:{REDIS_K8S_PORT_DEFAULT}/0"))
    CELERY_RESULT_BACKEND: RedisDsn = Field(default_factory=lambda: RedisDsn(f"redis://{REDIS_K8S_SVC}:{REDIS_K8S_PORT_DEFAULT}/1"))


    # --- Database ---
    POSTGRES_USER: str = POSTGRES_K8S_USER_DEFAULT
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = POSTGRES_K8S_SVC
    POSTGRES_PORT: int = POSTGRES_K8S_PORT_DEFAULT
    POSTGRES_DB: str = POSTGRES_K8S_DB_DEFAULT

    # --- Milvus ---
    MILVUS_URI: str = Field(default=f"http://{MILVUS_K8S_SVC}:{MILVUS_K8S_PORT_DEFAULT}")
    MILVUS_COLLECTION_NAME: str = MILVUS_DEFAULT_COLLECTION
    MILVUS_METADATA_FIELDS: List[str] = Field(default=["company_id", "document_id", "file_name", "file_type"])
    MILVUS_CONTENT_FIELD: str = "content"
    MILVUS_EMBEDDING_FIELD: str = "embedding"
    MILVUS_INDEX_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_INDEX_PARAMS))
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_SEARCH_PARAMS))

    # --- MinIO ---
    MINIO_ENDPOINT: str = Field(default=f"{MINIO_K8S_SVC}:{MINIO_K8S_PORT_DEFAULT}")
    MINIO_ACCESS_KEY: SecretStr
    MINIO_SECRET_KEY: SecretStr
    MINIO_BUCKET_NAME: str = MINIO_BUCKET_DEFAULT
    MINIO_USE_SECURE: bool = False

    # --- Embeddings (OpenAI for Ingestion) ---
    OPENAI_API_KEY: SecretStr
    OPENAI_EMBEDDING_MODEL: str = OPENAI_DEFAULT_EMBEDDING_MODEL
    EMBEDDING_DIMENSION: int = DEFAULT_EMBEDDING_DIM

    # --- Clients ---
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 2
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    # --- Processing ---
    SUPPORTED_CONTENT_TYPES: List[str] = Field(default=[
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", # DOCX
        "application/msword", # DOC
        "text/plain",
        "text/markdown",
        "text/html"
    ])
    SPLITTER_CHUNK_SIZE: int = 500
    SPLITTER_CHUNK_OVERLAP: int = 50
    SPLITTER_SPLIT_BY: str = "word"

    # --- Validators ---
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
        config_values = info.data
        model = config_values.get('OPENAI_EMBEDDING_MODEL', OPENAI_DEFAULT_EMBEDDING_MODEL)
        calculated_dim = DEFAULT_EMBEDDING_DIM
        if model == "text-embedding-3-large": calculated_dim = 3072
        elif model in ["text-embedding-3-small", "text-embedding-ada-002"]: calculated_dim = 1536

        if v is not None and v != calculated_dim:
             logging.warning(f"Provided INGEST_EMBEDDING_DIMENSION {v} conflicts with INGEST_OPENAI_EMBEDDING_MODEL {model} ({calculated_dim} expected). Using calculated value: {calculated_dim}")
             return calculated_dim
        elif v is None:
             logging.debug(f"EMBEDDING_DIMENSION not set, defaulting to {calculated_dim} based on model {model}")
             return calculated_dim
        else:
             if v == calculated_dim:
                 logging.debug(f"Provided EMBEDDING_DIMENSION {v} matches model {model}")
             return v

    @field_validator('POSTGRES_PASSWORD', 'MINIO_ACCESS_KEY', 'MINIO_SECRET_KEY', 'OPENAI_API_KEY', mode='before')
    @classmethod
    def check_secret_value_present(cls, v: Any, info: ValidationInfo) -> Any:
        if v is None or v == "":
             field_name = info.field_name if info.field_name else "Unknown Secret Field"
             raise ValueError(f"Required secret field '{field_name}' cannot be empty.")
        return v

    @field_validator('MILVUS_URI', mode='before')
    @classmethod
    def validate_milvus_uri(cls, v: str) -> str:
        if not v.startswith("http://") and not v.startswith("https://"):
             raise ValueError(f"Invalid MILVUS_URI format: '{v}'. Must start with 'http://' or 'https://'")
        return v

# --- Instancia Global ---
temp_log = logging.getLogger("ingest_service.config.loader")
if not temp_log.handlers:
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(levelname)-8s [%(asctime)s] [%(name)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    handler.setFormatter(formatter)
    temp_log.addHandler(handler)
    temp_log.setLevel(logging.INFO)

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
    if (_pool is None or _pool._closed):
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
    if (_pool and not _pool._closed): log.info("Closing PostgreSQL connection pool..."); await _pool.close(); _pool = None; log.info("PostgreSQL connection pool closed.")
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
async def create_document(document_id: uuid.UUID, company_id: uuid.UUID, file_name: str, file_type: str, metadata: Dict[str, Any]) -> None:
    """Crea un registro inicial para un documento en la base de datos."""
    pool = await get_db_pool()
    query = """
    INSERT INTO documents (id, company_id, file_name, file_type, file_path, metadata, status, chunk_count, error_message, uploaded_at, updated_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NULL, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC');
    """
    # Usar "" como placeholder inicial para file_path y DocumentStatus.UPLOADED
    params = [document_id, company_id, file_name, file_type, "", json.dumps(metadata), DocumentStatus.UPLOADED.value, 0]
    insert_log = log.bind(company_id=str(company_id), filename=file_name, doc_id=str(document_id))
    try:
        async with pool.acquire() as conn:
            await conn.execute(query, *params)
        insert_log.info("Document record created in PostgreSQL")
    except Exception as e:
        insert_log.error("Failed to create document record", error=str(e), exc_info=True)
        raise

async def update_document_status(
    document_id: uuid.UUID,
    status: DocumentStatus,
    file_path: Optional[str] = None,
    chunk_count: Optional[int] = None,
    error_message: Optional[str] = None
) -> bool:
    """Actualiza el estado, file_path, chunk_count y/o error_message de un documento."""
    pool = await get_db_pool()
    params: List[Any] = [document_id]
    fields: List[str] = ["status = $2", "updated_at = NOW() AT TIME ZONE 'UTC'"]
    params.append(status.value)
    param_index = 3
    if file_path is not None:
        fields.append(f"file_path = ${param_index}"); params.append(file_path); param_index += 1
    if chunk_count is not None:
        fields.append(f"chunk_count = ${param_index}"); params.append(chunk_count); param_index += 1

    # Manejo de error_message: Limpiar si no es estado ERROR, setear si es ERROR y se provee
    if status == DocumentStatus.ERROR:
        # Solo añadir/actualizar error_message si se proporciona uno
        if error_message is not None:
            fields.append(f"error_message = ${param_index}"); params.append(error_message); param_index += 1
        # Si status es ERROR pero no viene mensaje, se mantiene el existente (no añadir "error_message = NULL")
    else:
        # Si el status NO es ERROR, limpiar el mensaje de error explícitamente
        fields.append("error_message = NULL")

    set_clause = ", ".join(fields)
    query = f"UPDATE documents SET {set_clause} WHERE id = $1;"
    update_log = log.bind(document_id=str(document_id), new_status=status.value)
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(query, *params)
            # Check if update affected any row
            if result == 'UPDATE 0':
                 update_log.warning("Attempted to update status for non-existent document_id")
                 return False
        update_log.info("Document status updated in PostgreSQL")
        return True
    except Exception as e:
        update_log.error("Failed to update document status", error=str(e), exc_info=True)
        raise

async def get_document_status(document_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    pool = await get_db_pool()
    query = """
    SELECT id, company_id, file_name, file_type, file_path, metadata, status, chunk_count, error_message, uploaded_at, updated_at
    FROM documents WHERE id = $1;
    """
    get_log = log.bind(document_id=str(document_id))
    try:
        async with pool.acquire() as conn:
            record = await conn.fetchrow(query, document_id)
        if not record:
            get_log.warning("Queried non-existent document_id")
            return None
        return dict(record)
    except Exception as e:
        get_log.error("Failed to get document status", error=str(e), exc_info=True)
        raise

async def list_documents_by_company(company_id: uuid.UUID, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    pool = await get_db_pool()
    query = """
    SELECT id, company_id, file_name, file_type, file_path, metadata, status, chunk_count, error_message, uploaded_at, updated_at
    FROM documents WHERE company_id = $1 ORDER BY updated_at DESC LIMIT $2 OFFSET $3;
    """
    list_log = log.bind(company_id=str(company_id), limit=limit, offset=offset)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, company_id, limit, offset)
        return [dict(r) for r in rows]
    except Exception as e:
        list_log.error("Failed to list documents by company", error=str(e), exc_info=True)
        raise

async def delete_document(document_id: uuid.UUID) -> bool:
    pool = await get_db_pool()
    query = "DELETE FROM documents WHERE id = $1 RETURNING id;"
    delete_log = log.bind(document_id=str(document_id))
    try:
        async with pool.acquire() as conn:
            deleted_id = await conn.fetchval(query, document_id)
        delete_log.info("Document deleted from PostgreSQL", deleted_id=str(deleted_id))
        return deleted_id is not None
    except Exception as e:
        delete_log.error("Error deleting document record", error=str(e), exc_info=True)
        raise

# --- Funciones de Chat (Se mantienen, sin cambios) ---
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
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR),
        retry=retry_if_exception_type(httpx.RequestError)
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

import asyncpg # Importar para tipos de excepción

# --- Haystack Imports ---
from haystack import Pipeline, Document
from haystack.utils import Secret
from haystack.components.converters import (
    PyPDFToDocument, TextFileToDocument, MarkdownToDocument,
    HTMLToDocument, DOCXToDocument,
)
from haystack.components.preprocessors import DocumentSplitter
from haystack.components.embedders import OpenAIDocumentEmbedder
from milvus_haystack import MilvusDocumentStore # Importación correcta
from pymilvus.exceptions import MilvusException # Importar excepciones de Milvus
from minio.error import S3Error # Importar excepciones de Minio
from haystack.components.writers import DocumentWriter
from haystack.dataclasses import ByteStream

# --- Local Imports ---
from app.tasks.celery_app import celery_app
from app.core.config import settings
from app.db import postgres_client # Cliente DB async
from app.models.domain import DocumentStatus
from app.services.minio_client import MinioStorageClient # Cliente MinIO async

log = structlog.get_logger(__name__)

# --- Funciones Helper Síncronas para Haystack ---
def _initialize_milvus_store() -> MilvusDocumentStore:
    """Función interna SÍNCRONA para inicializar MilvusDocumentStore."""
    init_log = log.bind(component="MilvusDocumentStore")
    init_log.info("Attempting to initialize MilvusDocumentStore...")
    try:
        # *** RE-VERIFICACIÓN: Usar embedding_dim consistentemente ***
        store = MilvusDocumentStore(
            connection_args={"uri": str(settings.MILVUS_URI)},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            embedding_dim=settings.EMBEDDING_DIMENSION, # CORREGIDO
            embedding_field=settings.MILVUS_EMBEDDING_FIELD,
            content_field=settings.MILVUS_CONTENT_FIELD,
            metadata_fields=settings.MILVUS_METADATA_FIELDS,
            index_params=settings.MILVUS_INDEX_PARAMS,
            search_params=settings.MILVUS_SEARCH_PARAMS,
            consistency_level="Strong",
        )
        init_log.info("MilvusDocumentStore initialization successful.")
        return store
    except MilvusException as me:
        init_log.error("Milvus connection/initialization failed", code=getattr(me, 'code', None), message=str(me), exc_info=True)
        raise ConnectionError(f"Milvus connection failed: {me}") from me
    except TypeError as te:
        # Este es el error específico visto en los logs
        init_log.error(f"MilvusDocumentStore init TypeError: {te}. Check arguments (e.g., 'embedding_dim').", exc_info=True)
        raise RuntimeError(f"Milvus TypeError (check arguments like embedding_dim): {te}") from te
    except Exception as e:
        init_log.exception("Unexpected error during MilvusDocumentStore initialization")
        raise RuntimeError(f"Unexpected Milvus init error: {e}") from e

# --- (El resto de helpers _initialize_* no cambian) ---
def _initialize_openai_embedder() -> OpenAIDocumentEmbedder:
    init_log = log.bind(component="OpenAIDocumentEmbedder")
    init_log.info("Initializing...")
    api_key_value = settings.OPENAI_API_KEY.get_secret_value()
    if not api_key_value:
        init_log.error("OpenAI API Key is missing!")
        raise ValueError("OpenAI API Key is required.")
    embedder = OpenAIDocumentEmbedder(
        api_key=Secret.from_token(api_key_value),
        model=settings.OPENAI_EMBEDDING_MODEL,
        meta_fields_to_embed=[]
    )
    init_log.info("Initialization successful.", model=settings.OPENAI_EMBEDDING_MODEL)
    return embedder

def _initialize_splitter() -> DocumentSplitter:
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
    init_log = log.bind(component="DocumentWriter")
    init_log.info("Initializing...")
    writer = DocumentWriter(document_store=store, policy="OVERWRITE")
    init_log.info("Initialization successful.")
    return writer

def get_converter_for_content_type(content_type: str) -> Optional[Type]:
    converters = {
        "application/pdf": PyPDFToDocument,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DOCXToDocument,
        "application/msword": DOCXToDocument,
        "text/plain": TextFileToDocument,
        "text/markdown": MarkdownToDocument,
        "text/html": HTMLToDocument,
        "application/vnd.ms-excel": None,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": None,
        "image/png": None,
        "image/jpeg": None,
        "image/jpg": None,
    }
    normalized_content_type = content_type.lower().split(';')[0].strip()
    converter = converters.get(normalized_content_type)
    if converter is None:
        log.warning("Unsupported content type received for conversion", provided_type=content_type, normalized_type=normalized_content_type)
        raise ValueError(f"Tipo de archivo '{normalized_content_type}' no soportado actualmente.")
    return converter

# --- Celery Task Definition ---
# Añadir RuntimeError a los no reintentables (por si falla init de Milvus por TypeError)
NON_RETRYABLE_ERRORS = (FileNotFoundError, ValueError, TypeError, NotImplementedError, KeyError, AttributeError, asyncpg.exceptions.DataError, asyncpg.exceptions.IntegrityConstraintViolationError, RuntimeError)
# Añadir ConnectionError y ConnectionDoesNotExistError
RETRYABLE_ERRORS = (IOError, ConnectionError, TimeoutError, S3Error, MilvusException, asyncpg.exceptions.PostgresConnectionError, asyncpg.exceptions.InterfaceError, asyncpg.exceptions.ConnectionDoesNotExistError, asyncio.TimeoutError)

# --- Helper para actualizar estado de forma robusta ---
async def _robust_update_status(doc_id: uuid.UUID, status: DocumentStatus, message: Optional[str] = None, chunk_count: Optional[int] = None):
    """Intenta actualizar el estado, reintentando una vez en caso de error de conexión."""
    update_log = log.bind(document_id=str(doc_id), target_status=status.value)
    for attempt in range(2): # Intentar 2 veces
        try:
            await postgres_client.get_db_pool() # Re-asegura que el pool esté vivo
            success = await postgres_client.update_document_status(doc_id, status, error_message=message, chunk_count=chunk_count)
            if success:
                 update_log.info("Successfully updated final document status.", attempt=attempt+1)
                 return True
            else:
                 update_log.error("Document not found during final status update attempt.", attempt=attempt+1)
                 return False
        except (asyncpg.exceptions.PostgresConnectionError, asyncpg.exceptions.InterfaceError, asyncpg.exceptions.ConnectionDoesNotExistError) as db_conn_err:
            update_log.warning("Connection error during final status update", attempt=attempt+1, error=str(db_conn_err))
            if attempt == 0:
                update_log.info("Waiting before retrying final status update...")
                await asyncio.sleep(3) # Esperar 3 segundos antes de reintentar
            else:
                update_log.critical("Failed to update final document status after retry due to connection error.", error=str(db_conn_err))
                return False # Falló después de reintentar
        except Exception as e:
            update_log.critical("Unexpected error during final status update", attempt=attempt+1, error=str(e), exc_info=True)
            return False # Falló por otra razón
    return False # Si por alguna razón sale del bucle


@celery_app.task(
    bind=True,
    autoretry_for=RETRYABLE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    retry_kwargs={'max_retries': 3},
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
    processed_chunk_count = 0
    # Establecer valores por defecto para el bloque finally
    final_status = DocumentStatus.ERROR
    final_error_message = "Error inesperado al inicio de la tarea."
    task_exception = None # Para guardar la excepción original si ocurre

    async def async_process_flow():
        # Usar nonlocal para modificar las variables del scope exterior
        nonlocal processed_chunk_count, final_status, final_error_message
        minio_client = None
        downloaded_file_stream: Optional[io.BytesIO] = None
        pipeline: Optional[Pipeline] = None

        try:
            # 0. Marcar como PROCESSING en DB
            task_log.info("Updating document status to PROCESSING")
            update_success = await postgres_client.update_document_status(document_id, DocumentStatus.PROCESSING, error_message=None)
            if not update_success:
                task_log.warning("Document record not found in DB before processing started. Aborting task.")
                final_status = DocumentStatus.ERROR
                final_error_message = "Registro del documento no encontrado al iniciar procesamiento."
                # Levantar error no reintentable para detener aquí
                raise FileNotFoundError(final_error_message)

            # 1. Descargar archivo de MinIO
            task_log.info("Downloading file from MinIO")
            minio_client = MinioStorageClient()
            downloaded_file_stream = await minio_client.download_file_stream(minio_object_name)
            file_bytes = downloaded_file_stream.getvalue()
            if not file_bytes: raise ValueError("Downloaded file is empty.")
            task_log.info(f"File downloaded successfully ({len(file_bytes)} bytes)")

            # 2. Inicializar componentes Haystack y construir pipeline
            task_log.info("Initializing Haystack components and building pipeline via executor...")
            loop = asyncio.get_running_loop()
            # --- Ejecutar inicializaciones síncronas en executor ---
            store = await loop.run_in_executor(None, _initialize_milvus_store) # Puede lanzar error
            embedder = await loop.run_in_executor(None, _initialize_openai_embedder)
            splitter = await loop.run_in_executor(None, _initialize_splitter)
            writer = await loop.run_in_executor(None, _initialize_document_writer, store)
            ConverterClass = get_converter_for_content_type(content_type)
            converter_instance = ConverterClass()

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
            doc_meta = {
                "company_id": str(company_id),
                "document_id": str(document_id),
                "file_name": file_name or "unknown",
                "file_type": content_type or "unknown",
            }
            added_original_meta = 0
            for key, value in original_metadata.items():
                if key in allowed_meta_keys and key not in doc_meta:
                    doc_meta[key] = str(value) if value is not None else None
                    added_original_meta += 1
            task_log.debug("Prepared metadata for Haystack Document", final_meta=doc_meta, added_original_count=added_original_meta)

            source_stream = ByteStream(data=file_bytes, meta=doc_meta)
            pipeline_input = {"converter": {"sources": [source_stream]}}

            # 4. Ejecutar Pipeline Haystack
            task_log.info("Running Haystack pipeline via executor...")
            start_time = time.monotonic()
            pipeline_result = await loop.run_in_executor(None, pipeline.run, pipeline_input)
            duration = time.monotonic() - start_time
            task_log.info(f"Haystack pipeline execution finished", duration_sec=round(duration, 2))

            # 5. Procesar Resultado y Contar Chunks
            writer_output = pipeline_result.get("writer", {})
            if isinstance(writer_output, dict) and "documents_written" in writer_output:
                processed_chunk_count = writer_output["documents_written"]
                task_log.info(f"Chunks written to Milvus determined by writer: {processed_chunk_count}")
            else:
                 task_log.warning("Writer output missing 'documents_written', attempting fallback count from splitter", writer_output=writer_output)
                 splitter_output = pipeline_result.get("splitter", {})
                 if isinstance(splitter_output, dict) and "documents" in splitter_output and isinstance(splitter_output["documents"], list):
                      processed_chunk_count = len(splitter_output["documents"])
                      task_log.warning(f"Inferred chunk count from splitter output: {processed_chunk_count}")
                 else:
                      task_log.error("Pipeline finished but failed to determine processed chunk count.", pipeline_output=pipeline_result)
                      raise RuntimeError("Pipeline execution yielded unclear results regarding written documents.")

            if processed_chunk_count == 0:
                 task_log.warning("Pipeline ran successfully but resulted in 0 chunks being written to Milvus.")

            # 6. Éxito: Marcar estado final como PROCESSED
            final_status = DocumentStatus.PROCESSED
            final_error_message = None # Limpiar mensaje de error en éxito
            task_log.info("Haystack pipeline completed successfully.")

        # *** Manejo de errores dentro del flujo async ***
        except NON_RETRYABLE_ERRORS as e_non_retry:
            err_type = type(e_non_retry).__name__
            err_msg_detail = str(e_non_retry)[:500]
            user_error_msg = f"Error irrecuperable ({err_type}). Verifique el archivo o contacte a soporte."
            if isinstance(e_non_retry, ValueError) and ("Unsupported content type" in err_msg_detail or "API Key" in err_msg_detail):
                 user_error_msg = err_msg_detail
            elif isinstance(e_non_retry, RuntimeError) and "Milvus TypeError" in err_msg_detail:
                 user_error_msg = f"Error config./código Milvus ({err_type}). Contacte soporte."
            elif isinstance(e_non_retry, FileNotFoundError):
                 user_error_msg = final_error_message # Usar el mensaje ya definido

            formatted_traceback = traceback.format_exc()
            task_log.error(f"Processing failed permanently: {err_type}: {err_msg_detail}", traceback=formatted_traceback)
            final_status = DocumentStatus.ERROR
            final_error_message = user_error_msg
            # Re-levantar para que el bloque exterior lo capture
            raise e_non_retry

        except RETRYABLE_ERRORS as e_retry:
            err_type = type(e_retry).__name__
            err_msg_detail = str(e_retry)[:500]
            max_retries = self.max_retries if hasattr(self, 'max_retries') else 3
            current_attempt = self.request.retries + 1
            user_error_msg = f"Error temporal ({err_type} - Intento {current_attempt}/{max_retries+1}). Reintentando..."
            if isinstance(e_retry, ConnectionError) and "Milvus connection failed" in err_msg_detail:
                 user_error_msg = f"Error temporal conexión base vectorial (Intento {current_attempt}/{max_retries+1}). Reintentando..."

            task_log.warning(f"Processing failed, will retry: {err_type}: {err_msg_detail}", traceback=traceback.format_exc())
            final_status = DocumentStatus.ERROR
            final_error_message = user_error_msg
            # Re-levantar la excepción ORIGINAL para que Celery la capture y reintente
            raise e_retry

        finally:
            if downloaded_file_stream:
                downloaded_file_stream.close()
            task_log.debug("Cleaned up async flow resources.")

    # --- Ejecutar el flujo async y manejar resultado final / actualización DB ---
    try:
        TIMEOUT_SECONDS = 600 # 10 minutos
        asyncio.run(asyncio.wait_for(async_process_flow(), timeout=TIMEOUT_SECONDS))
    except asyncio.TimeoutError as toe:
        task_log.error(f"Processing timed out after {TIMEOUT_SECONDS} seconds.")
        final_status = DocumentStatus.ERROR
        final_error_message = "El procesamiento del documento tardó demasiado."
        task_exception = toe # Guardar para posible re-raise
    except Exception as outer_exc:
        task_log.exception("Exception caught after running async_process_flow.", exc_info=outer_exc)
        # El estado y mensaje ya deberían estar seteados dentro del flujo
        if final_status != DocumentStatus.ERROR:
             final_error_message = final_error_message or f"Error inesperado: {outer_exc}"
             final_status = DocumentStatus.ERROR
        task_exception = outer_exc # Guardar excepción original

    # --- Actualización Final de Estado (Robusta) ---
    task_log.info("Attempting final status update in DB.", status=final_status.value, chunks=processed_chunk_count, error=final_error_message)
    # Usar la función robusta para la actualización final
    update_success = asyncio.run(_robust_update_status(document_id, final_status, final_error_message, processed_chunk_count))

    if not update_success:
         task_log.critical("CRITICAL: Failed to update final document status in DB after processing attempt!",
                           target_status=final_status.value, error_msg=final_error_message)

    # --- Devolver resultado a Celery ---
    if final_status == DocumentStatus.PROCESSED:
        task_log.info("Haystack document processing task completed successfully.")
        return {"status": "success", "document_id": str(document_id), "chunk_count": processed_chunk_count}
    else:
        # *** CORRECCIÓN: Usar RETRYABLE_ERRORS para la comparación ***
        # Si la excepción original guardada NO es una de las que Celery reintenta automáticamente
        # (definidas en RETRYABLE_ERRORS), la levantamos para marcar como FAILED.
        if task_exception and not isinstance(task_exception, RETRYABLE_ERRORS):
            task_log.error("Raising final non-retryable exception to Celery.", exception_type=type(task_exception).__name__)
            raise task_exception
        # Si no hubo excepción O era reintentable (y Celery ya no reintentará más),
        # simplemente devolvemos 'failure'.
        task_log.error("Task finished with error status.", final_db_status=final_status.value, error_msg=final_error_message)
        return {"status": "failure", "document_id": str(document_id), "error": final_error_message or "Error desconocido"}
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
version = "0.1.2" # Incremento de versión a 0.1.2
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
asyncpg = "^0.29.0" # Para PostgreSQL directo
tenacity = "^8.2.3"
python-multipart = "^0.0.9" # Para subir archivos
structlog = "^24.1.0"
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

# --- CORRECCIÓN: httpx definido UNA SOLA VEZ con extras ---
# Cliente HTTP asíncrono
httpx = {extras = ["http2"], version = "^0.27.0"}
# Dependencia necesaria para httpx[http2]
h2 = "^4.1.0"

[tool.poetry.group.dev.dependencies] # Grupo dev corregido
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"
# httpx ya está en dependencias principales

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api" 
```
