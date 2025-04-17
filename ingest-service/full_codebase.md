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

from fastapi import (
    APIRouter, UploadFile, File, Depends, HTTPException,
    status, Form, Header, Query, Request
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

# Helper para obtención dinámica de estado en Milvus
async def get_milvus_chunk_count(document_id: uuid.UUID) -> int:
    """Cuenta los chunks indexados en Milvus para un documento específico."""
    loop = asyncio.get_running_loop()
    def _count_chunks():
        # Inicializar conexión con Milvus
        store = MilvusDocumentStore(
            connection_args={"uri": str(settings.MILVUS_URI)},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            search_params=settings.MILVUS_SEARCH_PARAMS,
            consistency_level="Strong",
        )
        # Recuperar todos los documentos que coincidan con este document_id
        docs = store.get_all_documents(filters={"document_id": str(document_id)})
        return len(docs or [])
    try:
        return await loop.run_in_executor(None, _count_chunks)
    except Exception:
        return 0

# --- Dependencias CORREGIDAS para usar X- Headers ---
# Estas dependencias AHORA son la fuente de verdad para ID de compañía y usuario

async def get_current_company_id(x_company_id: Optional[str] = Header(None, alias="X-Company-ID")) -> uuid.UUID:
    """Obtiene y valida el Company ID de la cabecera X-Company-ID."""
    if not x_company_id:
        log.warning("Missing required X-Company-ID header")
        # Usar 422 ya que el gateway DEBERÍA haberla añadido. Si falta, es un error de procesamiento/configuración.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")
    try:
        return uuid.UUID(x_company_id)
    except ValueError:
        log.warning("Invalid UUID format in X-Company-ID header", header_value=x_company_id)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid X-Company-ID header format")

async def get_current_user_id(x_user_id: Optional[str] = Header(None, alias="X-User-ID")) -> uuid.UUID:
    """Obtiene y valida el User ID de la cabecera X-User-ID."""
    if not x_user_id:
        log.warning("Missing required X-User-ID header")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-User-ID")
    try:
        return uuid.UUID(x_user_id)
    except ValueError:
        log.warning("Invalid UUID format in X-User-ID header", header_value=x_user_id)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid X-User-ID header format")

# Opcional: obtener email si se necesita
# async def get_current_user_email(x_user_email: Optional[str] = Header(None, alias="X-User-Email")) -> Optional[str]:
#    return x_user_email

# --- Endpoints Modificados para USAR las nuevas dependencias ---

@router.post(
    "/upload",
    response_model=schemas.IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingestar un nuevo documento",
    description="Sube un documento via API Gateway. Usa X-Company-ID y X-User-ID.",
)
async def ingest_document_haystack(
    # *** CORRECCIÓN CLAVE: Usar las dependencias definidas arriba ***
    company_id: uuid.UUID = Depends(get_current_company_id),
    user_id: uuid.UUID = Depends(get_current_user_id),
    # Ya no se depende de Authorization
    # --- Parámetros del Form ---
    metadata_json: str = Form(default="{}", description="String JSON de metadatos opcionales"),
    file: UploadFile = File(..., description="El archivo a ingestar"),
    request: Request = None # Para obtener request_id opcionalmente
):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4())) if request else str(uuid.uuid4())
    request_log = log.bind(
        request_id=request_id,
        company_id=str(company_id),
        user_id=str(user_id), # Ya no es opcional aquí
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

    # PREVENCIÓN DE DUPLICADOS: Buscar documento existente por company_id y file_name en estado != error
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
            # user_id=user_id, # Descomentar si la tabla `documents` tiene y requiere `user_id`
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

        # Pasar IDs como string a la tarea Celery
        task = process_document_haystack_task.delay(
            document_id_str=str(document_id),
            company_id_str=str(company_id),
            # user_id_str=str(user_id), # Pasar si la tarea lo necesita
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
    description="Recupera el estado de procesamiento de un documento específico usando su ID.",
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
        # Validar pertenencia a la compañía
        if doc_data.get("company_id") != company_id:
            status_log.warning("Attempt to access document status from another company", owner_company=str(doc_data.get('company_id')))
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

        response_data = schemas.StatusResponse.model_validate(doc_data)
        # Verificar existencia real en MinIO
        file_path = doc_data.get("file_path")
        if file_path:
            try:
                minio_client = MinioStorageClient()
                exists = await minio_client.file_exists(file_path)
            except Exception:
                exists = False
        else:
            exists = False
        # Contar chunks en Milvus
        milvus_count = await get_milvus_chunk_count(document_id)
        # Asignar campos adicionales
        response_data.minio_exists = exists
        response_data.milvus_chunk_count = milvus_count
        status_messages = {
            DocumentStatus.UPLOADED: "Document uploaded, awaiting processing.",
            DocumentStatus.PROCESSING: "Document is currently being processed.",
            DocumentStatus.PROCESSED: "Document processed successfully.",
            DocumentStatus.INDEXED: "Document processed and indexed.", # Añadir si se usa este estado
            DocumentStatus.ERROR: f"Processing error: {response_data.error_message or 'Unknown error'}",
        }
        response_data.message = status_messages.get(response_data.status, "Unknown status.")
        status_log.info("Returning document status", status=response_data.status)
        return response_data
    except HTTPException as http_exc: raise http_exc
    except Exception as e: status_log.exception("Error retrieving status"); raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve document status.")

@router.get(
    "/status",
    response_model=List[schemas.StatusResponse],
    status_code=status.HTTP_200_OK,
    summary="Listar estados de ingesta para la compañía",
    description="Recupera una lista paginada de documentos y sus estados para la compañía.",
)
async def list_ingestion_statuses(
    # *** CORRECCIÓN CLAVE: Usar la dependencia correcta ***
    company_id: uuid.UUID = Depends(get_current_company_id),
    # Ya no se depende de Authorization
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    request: Request = None
):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4())) if request else str(uuid.uuid4())
    list_log = log.bind(request_id=request_id, company_id=str(company_id), limit=limit, offset=offset)
    list_log.info("Received request to list document statuses")
    try:
        documents_data = await postgres_client.list_documents_by_company(company_id, limit=limit, offset=offset)
        response_list = []
        for doc in documents_data:
            # Mark processed if DB chunk_count > 0
            current = DocumentStatus(doc.get("status"))
            count = doc.get("chunk_count") or 0
            if current in (DocumentStatus.UPLOADED, DocumentStatus.PROCESSING) and count > 0:
                await postgres_client.update_document_status(uuid.UUID(doc["id"]), DocumentStatus.PROCESSED, chunk_count=count)
                doc["status"] = DocumentStatus.PROCESSED.value
            # Verify MinIO existence, mark error if missing
            file_path = doc.get("file_path")
            try:
                exists = await MinioStorageClient().file_exists(file_path) if file_path else False
            except Exception:
                exists = False
            if not exists and current not in (DocumentStatus.ERROR,):
                await postgres_client.update_document_status(uuid.UUID(doc["id"]), DocumentStatus.ERROR, error_message="File missing in MinIO")
                doc["status"] = DocumentStatus.ERROR.value
            # Append enriched response
            doc["minio_exists"] = exists
            response_list.append(schemas.StatusResponse.model_validate(doc))
        list_log.info(f"Returning status list for {len(response_list)} documents")
        return response_list
    except Exception as e:
        list_log.exception("Error listing document statuses")
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
    user_id: uuid.UUID = Depends(get_current_user_id),
    request: Request = None
):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4())) if request else str(uuid.uuid4())
    retry_log = log.bind(request_id=request_id, company_id=str(company_id), user_id=str(user_id), document_id=str(document_id))
    # 1. Buscar el documento y validar estado
    doc = await postgres_client.get_document_status(document_id)
    if not doc or doc.get("company_id") != str(company_id):
        retry_log.warning("Documento no encontrado o no pertenece a la compañía")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Documento no encontrado.")
    if doc["status"] != DocumentStatus.ERROR.value:
        retry_log.warning("Solo se puede reintentar si el estado es 'error'", current_status=doc["status"])
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Solo se puede reintentar la ingesta si el estado es 'error'.")
    # 2. Reencolar la tarea Celery
    task = process_document_haystack_task.delay(
        document_id_str=str(document_id),
        company_id_str=str(company_id),
        minio_object_name=doc["file_path"],
        file_name=doc["file_name"],
        content_type=doc["file_type"],
        original_metadata=doc.get("metadata", {})
    )
    retry_log.info("Reintento de ingesta encolado", task_id=task.id)
    # 3. Actualizar estado a 'processing'
    await postgres_client.update_document_status(document_id=document_id, status=DocumentStatus.PROCESSING, error_message=None)
    return schemas.IngestResponse(document_id=document_id, task_id=task.id, status=DocumentStatus.PROCESSING, message="Reintento de ingesta encolado correctamente.")
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
# *** CORREGIDO: Importar field_validator y ValidationInfo ***
from pydantic import (
    RedisDsn, AnyHttpUrl, SecretStr, Field, field_validator, ValidationError,
    ValidationInfo # Importar ValidationInfo para V2
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
MINIO_BUCKET_DEFAULT = "atenex"
MILVUS_K8S_PORT_DEFAULT = 19530
REDIS_K8S_PORT_DEFAULT = 6379
MILVUS_DEFAULT_COLLECTION = "atenex_doc_chunks"
MILVUS_DEFAULT_INDEX_PARAMS = '{"metric_type": "COSINE", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 256}}'
MILVUS_DEFAULT_SEARCH_PARAMS = '{"metric_type": "COSINE", "params": {"ef": 128}}'
OPENAI_DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_DIM = 1536

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env', env_prefix='INGEST_', env_file_encoding='utf-8',
        case_sensitive=False, extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = "Atenex Ingest Service"
    API_V1_STR: str = "/api/v1"
    LOG_LEVEL: str = "INFO"

    # --- Celery ---
    CELERY_BROKER_URL: RedisDsn = RedisDsn(f"redis://{REDIS_K8S_SVC}:{REDIS_K8S_PORT_DEFAULT}/0")
    CELERY_RESULT_BACKEND: RedisDsn = RedisDsn(f"redis://{REDIS_K8S_SVC}:{REDIS_K8S_PORT_DEFAULT}/1")

    # --- Database ---
    POSTGRES_USER: str = POSTGRES_K8S_USER_DEFAULT
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = POSTGRES_K8S_SVC
    POSTGRES_PORT: int = POSTGRES_K8S_PORT_DEFAULT
    POSTGRES_DB: str = POSTGRES_K8S_DB_DEFAULT

    # --- Milvus ---
    MILVUS_URI: str = f"http://{MILVUS_K8S_SVC}:{MILVUS_K8S_PORT_DEFAULT}"
    MILVUS_COLLECTION_NAME: str = MILVUS_DEFAULT_COLLECTION
    MILVUS_INDEX_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_INDEX_PARAMS))
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_SEARCH_PARAMS))
    MILVUS_CONTENT_FIELD: str = "content"
    MILVUS_EMBEDDING_FIELD: str = "embedding"
    MILVUS_METADATA_FIELDS: List[str] = Field(default=["company_id", "document_id", "file_name", "file_type"])

    # --- MinIO ---
    MINIO_ENDPOINT: str = f"{MINIO_K8S_SVC}:{MINIO_K8S_PORT_DEFAULT}"
    MINIO_ACCESS_KEY: SecretStr
    MINIO_SECRET_KEY: SecretStr
    MINIO_BUCKET_NAME: str = MINIO_BUCKET_DEFAULT
    MINIO_USE_SECURE: bool = False

    # --- OpenAI ---
    OPENAI_API_KEY: SecretStr
    OPENAI_EMBEDDING_MODEL: str = OPENAI_DEFAULT_EMBEDDING_MODEL
    EMBEDDING_DIMENSION: int = DEFAULT_EMBEDDING_DIM

    # --- Clients ---
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 2
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    # --- Processing ---
    SUPPORTED_CONTENT_TYPES: List[str] = Field(default=["application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "text/plain", "text/markdown", "text/html"])
    SPLITTER_CHUNK_SIZE: int = 500
    SPLITTER_CHUNK_OVERLAP: int = 50
    SPLITTER_SPLIT_BY: str = "word"

    # --- Validadores (Pydantic V2 Style) ---

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
             logging.warning(f"Provided EMBEDDING_DIMENSION {v} conflicts with model {model} ({calculated_dim} expected). Using calculated value: {calculated_dim}")
             return calculated_dim
        elif v is None: return calculated_dim
        else: return v

    # *** CORREGIDO: Usar field_validator en lugar de validator ***
    @field_validator('POSTGRES_PASSWORD', 'MINIO_ACCESS_KEY', 'MINIO_SECRET_KEY', 'OPENAI_API_KEY', mode='before')
    @classmethod
    def check_secret_value_present(cls, v: Any, info: ValidationInfo) -> Any:
        """Valida que el valor para un campo secreto no esté vacío antes de convertir a SecretStr."""
        if v is None or v == "":
             field_name = info.field_name if info.field_name else "Unknown Secret Field"
             raise ValueError(f"Required secret field '{field_name}' cannot be empty.")
        return v

# --- Instancia Global ---
temp_log = logging.getLogger("ingest_service.config.loader")
if not temp_log.handlers:
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(levelname)s: [%(name)s] %(message)s')
    handler.setFormatter(formatter)
    temp_log.addHandler(handler)
    temp_log.setLevel(logging.INFO)

try:
    temp_log.info("Loading Ingest Service settings...")
    settings = Settings()
    temp_log.info("Ingest Service Settings Loaded Successfully:")
    # (Mensajes de log sin cambios)...
    temp_log.info(f"  PROJECT_NAME: {settings.PROJECT_NAME}")
    temp_log.info(f"  LOG_LEVEL: {settings.LOG_LEVEL}")
    temp_log.info(f"  API_V1_STR: {settings.API_V1_STR}")
    temp_log.info(f"  CELERY_BROKER_URL: {settings.CELERY_BROKER_URL}")
    temp_log.info(f"  CELERY_RESULT_BACKEND: {settings.CELERY_RESULT_BACKEND}")
    temp_log.info(f"  POSTGRES_SERVER: {settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}")
    temp_log.info(f"  POSTGRES_DB: {settings.POSTGRES_DB}")
    temp_log.info(f"  POSTGRES_USER: {settings.POSTGRES_USER}")
    temp_log.info(f"  POSTGRES_PASSWORD: *** SET ***")
    temp_log.info(f"  MILVUS_URI: {settings.MILVUS_URI} (Points to 'default' namespace service)")
    temp_log.info(f"  MILVUS_COLLECTION_NAME: {settings.MILVUS_COLLECTION_NAME}")
    temp_log.info(f"  MINIO_ENDPOINT: {settings.MINIO_ENDPOINT}")
    temp_log.info(f"  MINIO_BUCKET_NAME: {settings.MINIO_BUCKET_NAME}")
    temp_log.info(f"  MINIO_ACCESS_KEY: *** SET ***")
    temp_log.info(f"  MINIO_SECRET_KEY: *** SET ***")
    temp_log.info(f"  OPENAI_API_KEY: *** SET ***")
    temp_log.info(f"  OPENAI_EMBEDDING_MODEL: {settings.OPENAI_EMBEDDING_MODEL}")
    temp_log.info(f"  EMBEDDING_DIMENSION: {settings.EMBEDDING_DIMENSION}")
    temp_log.info(f"  SUPPORTED_CONTENT_TYPES: {settings.SUPPORTED_CONTENT_TYPES}")

except (ValidationError, ValueError) as e:
    error_details = ""
    if isinstance(e, ValidationError):
        try: error_details = f"\nValidation Errors:\n{e.json(indent=2)}"
        except Exception: error_details = f"\nRaw Errors: {e.errors()}"
    temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    temp_log.critical(f"! FATAL: Ingest Service configuration validation failed:{error_details}")
    temp_log.critical(f"! Check environment variables (prefixed with INGEST_) or .env file.")
    temp_log.critical(f"! Original Error: {e}")
    temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
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

# --- Pool Management (sin cambios) ---
async def get_db_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None or _pool._closed:
        log.info("Creating PostgreSQL connection pool...", host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT, user=settings.POSTGRES_USER, db=settings.POSTGRES_DB)
        try:
            def _json_encoder(value): return json.dumps(value)
            def _json_decoder(value): return json.loads(value)
            async def init_connection(conn):
                await conn.set_type_codec('jsonb', encoder=_json_encoder, decoder=_json_decoder, schema='pg_catalog')
                await conn.set_type_codec('json', encoder=_json_encoder, decoder=_json_decoder, schema='pg_catalog')

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

# --- Document Operations (sin cambios) ---
async def create_document(company_id: uuid.UUID, file_name: str, file_type: str, metadata: Dict[str, Any]) -> uuid.UUID:
    pool = await get_db_pool(); doc_id = uuid.uuid4()
    # Incluye file_path como string vacío para cumplir NOT NULL
    query = "INSERT INTO documents (id, company_id, file_name, file_type, file_path, metadata, status, uploaded_at, updated_at) VALUES ($1, $2, $3, $4, $5, $6, $7, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC') RETURNING id;"
    insert_log = log.bind(company_id=str(company_id), filename=file_name, content_type=file_type, proposed_doc_id=str(doc_id))
    try:
        async with pool.acquire() as connection:
            result_id = await connection.fetchval(query, doc_id, company_id, file_name, file_type, '', json.dumps(metadata), DocumentStatus.UPLOADED.value)
        if result_id and result_id == doc_id:
            insert_log.info("Document record created", document_id=str(doc_id)); return result_id
        else:
            insert_log.error("Failed to create document record", returned_id=result_id); raise RuntimeError(f"Failed create document, return mismatch ({result_id})")
    except asyncpg.exceptions.UniqueViolationError as e:
        insert_log.error("Unique constraint violation", error=str(e), constraint=e.constraint_name); raise ValueError(f"Document creation failed: unique constraint ({e.constraint_name})") from e
    except Exception as e:
        insert_log.error("Failed to create document record", error=str(e), exc_info=True); raise

async def update_document_status(document_id: uuid.UUID, status: DocumentStatus, file_path: Optional[str] = None, chunk_count: Optional[int] = None, error_message: Optional[str] = None) -> bool:
    pool = await get_db_pool(); update_log = log.bind(document_id=str(document_id), new_status=status.value)
    fields_to_set: List[str] = []; params: List[Any] = [document_id]; param_index = 2
    fields_to_set.append(f"status = ${param_index}"); params.append(status.value); param_index += 1
    fields_to_set.append(f"updated_at = NOW() AT TIME ZONE 'UTC'")
    if file_path is not None: fields_to_set.append(f"file_path = ${param_index}"); params.append(file_path); param_index += 1
    if chunk_count is not None: fields_to_set.append(f"chunk_count = ${param_index}"); params.append(chunk_count); param_index += 1
    # Elimina manejo de error_message
    query = f"UPDATE documents SET {', '.join(fields_to_set)} WHERE id = $1;"; update_log.debug("Executing status update", query=query)
    try:
        async with pool.acquire() as connection: result_str = await connection.execute(query, *params)
        if isinstance(result_str, str) and result_str.startswith("UPDATE "): affected_rows = int(result_str.split(" ")[1])
        else: affected_rows = 0 # Assume 0 if result format unexpected
        if affected_rows > 0: update_log.info("Document status updated", rows=affected_rows); return True
        else: update_log.warning("Document status update affected 0 rows"); return False
    except Exception as e: update_log.error("Failed to update status", error=str(e), exc_info=True); raise

async def get_document_status(document_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    pool = await get_db_pool(); get_log = log.bind(document_id=str(document_id))
    # Seleccionar también file_path para verificar almacenamiento externo
    query = "SELECT id, status, file_name, file_type, chunk_count, file_path, updated_at, company_id FROM documents WHERE id = $1;"
    try:
        async with pool.acquire() as connection: record = await connection.fetchrow(query, document_id)
        if record: get_log.debug("Document status retrieved"); return dict(record)
        else: get_log.warning("Document status requested for non-existent ID"); return None
    except Exception as e: get_log.error("Failed to get status", error=str(e), exc_info=True); raise

async def list_documents_by_company(company_id: uuid.UUID, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    pool = await get_db_pool(); list_log = log.bind(company_id=str(company_id), limit=limit, offset=offset)
    # Incluir file_path para permitir validación en MinIO
    query = "SELECT id, status, file_name, file_type, chunk_count, file_path, updated_at FROM documents WHERE company_id = $1 ORDER BY updated_at DESC LIMIT $2 OFFSET $3;"
    try:
        async with pool.acquire() as connection: records = await connection.fetch(query, company_id, limit, offset)
        result_list = [dict(record) for record in records]; list_log.info(f"Retrieved {len(result_list)} docs")
        return result_list
    except Exception as e: list_log.error("Failed to list docs", error=str(e), exc_info=True); raise

# --- Funciones de Chat (Nombres Corregidos para consistencia) ---
# Aunque este servicio no las use directamente, mantenemos los nombres consistentes

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
    version="0.1.1",
    description="Microservicio Atenex para ingesta de documentos usando Haystack.",
    lifespan=lifespan
)

# --- Middlewares ---
@app.middleware("http")
async def add_request_context_timing_logging(request: Request, call_next):
    start_time = time.perf_counter()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    req_log = log.bind(request_id=request_id, method=request.method, path=request.url.path)
    req_log.info("Request received")
    request.state.request_id = request_id

    response = None
    try:
        response = await call_next(request)
        process_time_ms = (time.perf_counter() - start_time) * 1000
        resp_log = req_log.bind(status_code=response.status_code, duration_ms=round(process_time_ms, 2))
        log_level = "warning" if 400 <= response.status_code < 500 else "error" if response.status_code >= 500 else "info"
        getattr(resp_log, log_level)("Request finished")
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
    except Exception as e:
        process_time_ms = (time.perf_counter() - start_time) * 1000
        exc_log = req_log.bind(status_code=500, duration_ms=round(process_time_ms, 2))
        exc_log.exception("Unhandled exception during request processing")
        response = JSONResponse(
            status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal Server Error"}
        )
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time_ms:.2f}"
    return response

# --- Exception Handlers ---
@app.exception_handler(ResponseValidationError)
async def response_validation_exception_handler(request: Request, exc: ResponseValidationError):
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Error de validación en la respuesta", "errors": exc.errors()},
    )

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail if isinstance(exc.detail, str) else "Error HTTP"},
        headers=getattr(exc, "headers", None)
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Error de validación en la petición", "errors": exc.errors()},
    )

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.error("Excepción no controlada", error=str(exc))
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Error interno del servidor"}
    )

# --- Router Inclusion ---
app.include_router(ingest.router, prefix=f"{settings.API_V1_STR}/ingest", tags=["Ingestion"])
log.info(f"Included ingestion router with prefix: {settings.API_V1_STR}/ingest")

# --- Root Endpoint / Health Check ---
# *** CORREGIDO: Verificar DB en cada llamada ***
@app.get("/", tags=["Health Check"], status_code=fastapi_status.HTTP_200_OK, response_class=PlainTextResponse)
async def health_check():
    """
    Verifica la disponibilidad del servicio, incluyendo la conexión a la BD.
    Usado por Kubernetes Liveness/Readiness Probes.
    Devuelve 'OK' y 200 si está listo, 503 si no.
    """
    global SERVICE_READY, DB_CONNECTION_OK # Usar flags globales
    health_log = log.bind(check="liveness_readiness")

    # 1. Verificar bandera de inicio (rápido)
    if not SERVICE_READY:
        health_log.warning("Health check failed: Service did not initialize correctly (check startup logs).")
        raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service Not Initialized")

    # 2. Verificar conexión actual a la BD (más costoso, pero más preciso)
    db_ok_now = False
    try:
        db_ok_now = await postgres_client.check_db_connection()
        if not db_ok_now:
             # Actualizar estado global si la conexión se pierde
             DB_CONNECTION_OK = False
             SERVICE_READY = False # Marcar como no listo si la DB falla ahora
             health_log.error("Health check failed: Database connection check returned false.")
             raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service Unavailable (DB Connection Lost)")
        else:
             # Si estaba mal y ahora está bien, actualizar estado
             if not DB_CONNECTION_OK:
                  log.info("Database connection re-established.")
                  DB_CONNECTION_OK = True
                  SERVICE_READY = True # Podría marcarse como listo de nuevo

    except Exception as db_check_err:
        DB_CONNECTION_OK = False
        SERVICE_READY = False
        health_log.error("Health check failed: Error during database connection check.", error=str(db_check_err))
        raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service Unavailable (DB Check Error)")

    # Si pasa ambas verificaciones
    health_log.debug("Health check passed.")
    return PlainTextResponse("OK", status_code=fastapi_status.HTTP_200_OK)
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
        log.debug(f"Requesting {self.service_name}", method=method, endpoint=endpoint, params=params, json_keys=list(json.keys()) if json else None)
        try:
            response = await self.client.request(
                method,
                endpoint,
                params=params,
                json=json,
                data=data,
                files=files,
                headers=headers
            )
            response.raise_for_status() # Lanza HTTPStatusError para 4xx/5xx
            log.debug(f"{self.service_name} request successful", method=method, endpoint=endpoint, status_code=response.status_code)
            return response
        except httpx.HTTPStatusError as e:
            log.error(
                f"{self.service_name} request failed with status code",
                method=method, endpoint=endpoint, status_code=e.response.status_code, response_text=e.response.text,
                exc_info=False # No mostrar traceback completo para errores HTTP esperados
            )
            raise # Re-lanzar para que tenacity lo capture si es necesario
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            log.error(
                f"{self.service_name} request failed due to network/timeout issue",
                method=method, endpoint=endpoint, error=type(e).__name__,
                exc_info=True
            )
            raise # Re-lanzar para que tenacity lo capture
        except Exception as e:
             log.error(
                f"An unexpected error occurred during {self.service_name} request",
                method=method, endpoint=endpoint, error=e,
                exc_info=True
            )
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
        self.bucket_name = settings.MINIO_BUCKET_NAME # Usar siempre el bucket de config ('atenex')
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
            # Si MinIO es esencial, fallar el inicio del servicio
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
            raise # Re-lanzar para indicar fallo crítico

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
        try:
            file_content_stream.seek(0)
            # Wrapper para pasar argumentos correctamente a put_object
            def _put_object():
                return self.client.put_object(
                    self.bucket_name,
                    object_name,
                    file_content_stream,
                    content_length,
                    content_type=content_type
                )
            result = await loop.run_in_executor(None, _put_object)
            upload_log.info("File uploaded successfully to MinIO via executor", etag=getattr(result, 'etag', None), version_id=getattr(result, 'version_id', None))
            return object_name
        except S3Error as e:
            upload_log.error("Failed to upload file to MinIO via executor", error=str(e), code=e.code, exc_info=True)
            raise IOError(f"Failed to upload to storage: {e.code}") from e
        except Exception as e:
            upload_log.error("Unexpected error during file upload via executor", error=str(e), exc_info=True)
            raise IOError(f"Unexpected storage upload error") from e

    def download_file_stream_sync(self, object_name: str) -> io.BytesIO:
        """Operación SÍNCRONA para descargar un archivo a BytesIO."""
        download_log = log.bind(bucket=self.bucket_name, object_name=object_name)
        download_log.info("Downloading file from MinIO (sync operation starting)...")
        response = None
        try:
            # get_object es bloqueante
            response = self.client.get_object(self.bucket_name, object_name)
            file_data = response.read() # Leer todo en memoria (bloqueante)
            file_stream = io.BytesIO(file_data)
            download_log.info(f"File downloaded successfully from MinIO (sync, {len(file_data)} bytes)")
            file_stream.seek(0) # Importante resetear posición
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
            # Asegurar liberación de conexión
            if response:
                response.close()
                response.release_conn()

    async def download_file_stream(self, object_name: str) -> io.BytesIO:
        """Descarga un archivo de MinIO como BytesIO de forma asíncrona."""
        download_log = log.bind(bucket=self.bucket_name, object_name=object_name)
        download_log.info("Queueing file download from MinIO executor")
        loop = asyncio.get_running_loop()
        try:
            # Llamar a la función síncrona en el executor
            file_stream = await loop.run_in_executor(
                None, # Default executor
                self.download_file_stream_sync, # La función bloqueante
                object_name # Argumento para la función
            )
            download_log.info("File download successful via executor")
            return file_stream
        except FileNotFoundError: # Capturar y relanzar específicamente
            download_log.error("File not found in MinIO via executor", object_name=object_name)
            raise
        except Exception as e: # Captura IOError u otros errores del sync helper
            download_log.error("Error downloading file via executor", error=str(e), error_type=type(e).__name__, exc_info=True)
            raise IOError(f"Failed to download file via executor: {e}") from e

    async def file_exists(self, object_name: str) -> bool:
        """Verifica si un objeto existe en MinIO."""
        check_log = log.bind(bucket=self.bucket_name, object_name=object_name)
        loop = asyncio.get_running_loop()
        try:
            # stat_object es bloqueante
            await loop.run_in_executor(None, self.client.stat_object, self.bucket_name, object_name)
            check_log.info("Objeto encontrado en MinIO")
            return True
        except S3Error as e:
            # NoSuchKey indica objeto no encontrado
            if getattr(e, 'code', None) == 'NoSuchKey':
                check_log.warning("Objeto no existe en MinIO", code=e.code)
                return False
            check_log.error("Error al verificar existencia en MinIO", error=str(e), code=e.code)
            raise IOError(f"Error al verificar almacenamiento: {e.code}") from e
        except Exception as e:
            check_log.error("Error inesperado al verificar existencia en MinIO", error=str(e))
            raise IOError("Error inesperado verificando almacenamiento") from e
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

# *** CORRECCIÓN: Añadir import faltante ***
import asyncpg

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

# --- Funciones Helper Síncronas para Haystack ---
# Estas funciones *definen* cómo crear los componentes, pero no los crean todavía.
# La creación real se hará dentro de la tarea.

def _initialize_milvus_store() -> MilvusDocumentStore:
    """Función interna SÍNCRONA para inicializar MilvusDocumentStore."""
    init_log = log.bind(component="MilvusDocumentStore")
    init_log.info("Attempting to initialize...")
    try:
        store = MilvusDocumentStore(
            connection_args={"uri": str(settings.MILVUS_URI)},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            dim=settings.EMBEDDING_DIMENSION,
            embedding_field=settings.MILVUS_EMBEDDING_FIELD,
            content_field=settings.MILVUS_CONTENT_FIELD,
            metadata_fields=settings.MILVUS_METADATA_FIELDS,
            index_params=settings.MILVUS_INDEX_PARAMS,
            search_params=settings.MILVUS_SEARCH_PARAMS,
            consistency_level="Strong",
        )
        # Opcional: realizar una operación ligera para verificar conexión aquí si es necesario
        # store.count_documents() # Puede ser lento
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
    writer = DocumentWriter(document_store=store)
    init_log.info("Initialization successful.")
    return writer

def get_converter_for_content_type(content_type: str) -> Optional[Type]:
    """Devuelve la clase del conversor Haystack apropiada para el tipo de archivo."""
    converters = {
        "application/pdf": PyPDFToDocument,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DOCXToDocument,
        "application/msword": DOCXToDocument,  # Word antiguo
        "application/vnd.ms-excel": None,  # Excel antiguo (no soportado nativo, requiere integración extra)
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": None,  # Excel moderno (ver nota abajo)
        "image/png": None,  # Imágenes requieren OCR externo
        "image/jpeg": None,
        "image/jpg": None,
        "text/plain": TextFileToDocument,
        "text/markdown": MarkdownToDocument,
        "text/html": HTMLToDocument,
    }
    converter = converters.get(content_type)
    if converter is None:
        # Mensaje de error claro y en español para el frontend
        raise ValueError("El tipo de archivo no es soportado actualmente. Solo se permiten PDF, Word, Excel y algunas imágenes. Si necesitas soporte para este tipo de archivo, contacta al administrador.")
    return converter
# NOTA: Para Excel e imágenes, devolveremos error claro y en español si se intenta subir uno, hasta que se integre OCR o parser de Excel.

# --- Celery Task Definition ---
NON_RETRYABLE_ERRORS = (FileNotFoundError, ValueError, TypeError, NotImplementedError, KeyError, AttributeError)
# *** CORREGIDO: asyncpg ahora está definido porque se importó arriba ***
RETRYABLE_ERRORS = (IOError, ConnectionError, TimeoutError, asyncpg.PostgresConnectionError, S3Error, MilvusException, Exception)

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
    # Configurar logger con contexto de la tarea
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
        pipeline: Optional[Pipeline] = None # Inicializar pipeline como None

        try:
            # 0. Marcar como PROCESSING en DB
            task_log.info("Updating document status to PROCESSING")
            # Asegurarse que la conexión DB esté disponible (puede requerir get_db_pool si no está globalmente disponible aquí)
            # Si postgres_client maneja el pool internamente, esto está bien.
            await postgres_client.update_document_status(document_id, DocumentStatus.PROCESSING)

            # 1. Descargar archivo de MinIO
            task_log.info("Attempting to download file from MinIO")
            minio_client = MinioStorageClient() # Asume que maneja errores de conexión internamente
            downloaded_file_stream = await minio_client.download_file_stream(minio_object_name)
            file_bytes = downloaded_file_stream.getvalue()
            if not file_bytes:
                raise ValueError("Downloaded file is empty.")
            task_log.info(f"File downloaded successfully ({len(file_bytes)} bytes)")

            # 2. Inicializar componentes Haystack y construir pipeline (Síncrono -> Executor)
            task_log.info("Initializing Haystack components and building pipeline via executor...")
            loop = asyncio.get_running_loop()
            # Ejecutar inicialización síncrona en executor
            store = await loop.run_in_executor(None, _initialize_milvus_store)
            embedder = await loop.run_in_executor(None, _initialize_openai_embedder)
            splitter = await loop.run_in_executor(None, _initialize_splitter)
            writer = await loop.run_in_executor(None, _initialize_document_writer, store) # Pasar store inicializado

            ConverterClass = get_converter_for_content_type(content_type)
            if not ConverterClass:
                raise ValueError(f"Unsupported content type: {content_type}")
            converter_instance = ConverterClass()

            # Construir el pipeline (esto es rápido, no necesita executor)
            pipeline = Pipeline()
            pipeline.add_component("converter", converter_instance)
            pipeline.add_component("splitter", splitter)
            pipeline.add_component("embedder", embedder)
            pipeline.add_component("writer", writer)
            pipeline.connect("converter.documents", "splitter.documents")
            pipeline.connect("splitter.documents", "embedder.documents")
            pipeline.connect("embedder.documents", "writer.documents")
            task_log.info("Haystack components initialized and pipeline built.")

            # 3. Preparar Metadatos y ByteStream
            allowed_meta_keys = set(settings.MILVUS_METADATA_FIELDS)
            # Asegurar que los metadatos clave siempre estén presentes
            doc_meta = {
                "company_id": str(company_id),
                "document_id": str(document_id),
                "file_name": file_name or "unknown",
                "file_type": content_type or "unknown"
            }
            # Añadir metadatos originales si están permitidos y no colisionan
            added_original_meta = 0
            for key, value in original_metadata.items():
                # Solo añadir si está en la lista permitida Y no es uno de los campos clave ya definidos
                if key in allowed_meta_keys and key not in ["company_id", "document_id", "file_name", "file_type"]:
                    doc_meta[key] = str(value) if value is not None else None # Convertir a string por si acaso
                    added_original_meta += 1
            task_log.debug("Prepared metadata for Haystack", final_meta=doc_meta, added_original_count=added_original_meta)

            source_stream = ByteStream(data=file_bytes, meta=doc_meta)
            pipeline_input = {"converter": {"sources": [source_stream]}}

            # 4. Ejecutar Pipeline Haystack (Síncrono -> Executor)
            task_log.info("Running Haystack pipeline via executor...")
            start_time = time.monotonic()
            # Usar el pipeline construido previamente
            pipeline_result = await loop.run_in_executor(None, pipeline.run, pipeline_input)
            duration = time.monotonic() - start_time
            task_log.info(f"Haystack pipeline execution finished via executor", duration_sec=round(duration, 2))

            # 5. Procesar Resultado y Contar Chunks
            processed_chunk_count = 0
            writer_output = pipeline_result.get("writer", {})
            if isinstance(writer_output, dict) and "documents_written" in writer_output:
                processed_chunk_count = writer_output["documents_written"]
                task_log.info(f"Chunks written to Milvus: {processed_chunk_count}")
            else:
                # Fallback si 'documents_written' no está (podría indicar error o versión distinta)
                task_log.warning("Could not determine count from writer output, attempting fallback", output=writer_output)
                splitter_output = pipeline_result.get("splitter", {})
                if isinstance(splitter_output, dict) and "documents" in splitter_output and isinstance(splitter_output["documents"], list):
                     processed_chunk_count = len(splitter_output["documents"])
                     task_log.warning(f"Inferred chunk count from splitter output: {processed_chunk_count}")
                else:
                    task_log.error("Pipeline failed or did not produce expected output structure. No documents processed/written.", pipeline_output=pipeline_result)
                    raise RuntimeError("Pipeline execution failed or yielded unexpected results.")

            if processed_chunk_count == 0:
                 task_log.warning("Pipeline ran but resulted in 0 chunks being written.")
                 # Considerar si 0 chunks es un error o un caso válido (documento vacío post-conversión?)
                 # Por ahora, lo marcamos como procesado pero con 0 chunks.

            # 6. Actualizar Estado Final en DB
            final_status = DocumentStatus.PROCESSED # O INDEXED si quieres ese estado
            task_log.info(f"Updating document status to {final_status.value} with {processed_chunk_count} chunks.")
            await postgres_client.update_document_status(
                document_id=document_id,
                status=final_status,
                chunk_count=processed_chunk_count,
                error_message=None # Limpiar cualquier error previo
            )
            task_log.info("Document status updated successfully in PostgreSQL.")

        except NON_RETRYABLE_ERRORS as e_non_retry:
            err_msg = f"Non-retryable error: {type(e_non_retry).__name__}: {str(e_non_retry)[:500]}"
            formatted_traceback = traceback.format_exc()
            task_log.error(f"Processing failed permanently: {err_msg}", traceback=formatted_traceback)
            try:
                await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=err_msg)
            except Exception as db_err:
                task_log.critical("Failed update status to ERROR after non-retryable failure!", db_error=str(db_err))
            # No relanzar para que Celery no reintente

        except RETRYABLE_ERRORS as e_retry:
            # Obtener el número máximo de reintentos de forma segura
            max_retries = getattr(self, 'max_retries', 3)
            err_msg = f"Error reintentable (intento {self.request.retries + 1} de {max_retries}): {type(e_retry).__name__}: {str(e_retry)[:500]}"
            formatted_traceback = traceback.format_exc()
            # Mensaje para el frontend en español
            frontend_msg = "Ocurrió un error temporal durante el procesamiento. El sistema intentará nuevamente."
            task_log.warning(f"Processing failed, will retry: {err_msg}", traceback=formatted_traceback)
            try:
                # Actualizar estado a ERROR pero indicando que es parte de un reintento
                await postgres_client.update_document_status(
                    document_id, DocumentStatus.ERROR,
                    error_message=frontend_msg
                )
            except Exception as db_err:
                task_log.error("Failed update status to ERROR during retryable failure!", db_error=str(db_err))
            # Relanzar la excepción para que Celery maneje el reintento
            raise e_retry

        finally:
            # Limpieza de recursos
            if downloaded_file_stream:
                downloaded_file_stream.close()
            # Si se crearon archivos temporales, limpiarlos aquí
            task_log.debug("Cleaned up task resources.")

    # --- Ejecutar el flujo async ---
    try:
        # Timeout global para el procesamiento (5 minutos)
        TIMEOUT_SECONDS = 300
        try:
            asyncio.run(asyncio.wait_for(async_process_flow(), timeout=TIMEOUT_SECONDS))
            task_log.info("Haystack document processing task completed successfully.")
            return {"status": "success", "document_id": str(document_id)}
        except asyncio.TimeoutError:
            timeout_msg = f"Processing exceeded timeout of {TIMEOUT_SECONDS} seconds. Marking as ERROR."
            task_log.error(timeout_msg)
            # Intentar actualizar el estado en la base de datos
            try:
                asyncio.run(postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=timeout_msg))
            except Exception as db_err:
                task_log.critical("Failed to update status to ERROR after timeout!", db_error=str(db_err))
            return {"status": "failure", "document_id": str(document_id), "error": timeout_msg}
    except Exception as top_level_exc:
        # Mensaje para el frontend en español
        frontend_msg = "Ocurrió un error inesperado durante el procesamiento del documento. Por favor, inténtalo de nuevo o contacta soporte."
        task_log.exception("Haystack processing task failed at top level (after potential retries). This indicates a final failure.", exc_info=top_level_exc)
        try:
            asyncio.run(postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=frontend_msg))
        except Exception as db_err:
            task_log.critical("Failed to update status to ERROR after top-level failure!", db_error=str(db_err))
        return {"status": "failure", "document_id": str(document_id), "error": frontend_msg}
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
