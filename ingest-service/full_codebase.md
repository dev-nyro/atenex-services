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

from fastapi import (
    APIRouter, UploadFile, File, Depends, HTTPException,
    status, Form, Header, Query
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

# --- Dependency for Company ID (sin cambios) ---
async def get_current_company_id(x_company_id: Optional[str] = Header(None)) -> uuid.UUID:
    if not x_company_id: raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-Company-ID header")
    try: return uuid.UUID(x_company_id)
    except ValueError: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid X-Company-ID header format")

# --- Endpoints ---

@router.post(
    "/upload",
    response_model=schemas.IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingestar un nuevo documento",
    description="Sube un documento, lo almacena en MinIO (bucket 'atenex'), crea registro en DB y encola para procesamiento Haystack.",
)
async def ingest_document_haystack(
    company_id: uuid.UUID = Depends(get_current_company_id),
    metadata_json: str = Form(default="{}", description="String JSON de metadatos opcionales del documento"),
    file: UploadFile = File(..., description="El archivo del documento a ingestar"),
):
    request_log = log.bind(company_id=str(company_id), filename=file.filename, content_type=file.content_type)
    request_log.info("Received document ingestion request")
    # (Validaciones y lógica de subida/encolado sin cambios)...
    # ... (Igual que la versión anterior hasta el final del try/except/finally) ...
    # --- Lógica de creación, subida y encolado (sin cambios relevantes aquí) ---
    content_type = file.content_type or "application/octet-stream"
    if content_type not in settings.SUPPORTED_CONTENT_TYPES:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=f"Unsupported file type: '{content_type}'.")
    try: metadata = json.loads(metadata_json); assert isinstance(metadata, dict)
    except: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON format for metadata")

    minio_client = None; minio_object_name = None; document_id = None; task_id = None
    try:
        document_id = await postgres_client.create_document(company_id=company_id, file_name=file.filename or "untitled", file_type=content_type, metadata=metadata)
        request_log = request_log.bind(document_id=str(document_id))
        file_content = await file.read(); content_length = len(file_content)
        if content_length == 0: await postgres_client.update_document_status(document_id=document_id, status=DocumentStatus.ERROR, error_message="Uploaded file is empty."); raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file empty.")
        file_stream = io.BytesIO(file_content)
        minio_client = MinioStorageClient()
        minio_object_name = await minio_client.upload_file(company_id=company_id, document_id=document_id, file_name=file.filename or "untitled", file_content_stream=file_stream, content_type=content_type, content_length=content_length)
        await postgres_client.update_document_status(document_id=document_id, status=DocumentStatus.UPLOADED, file_path=minio_object_name)
        task = process_document_haystack_task.delay(document_id_str=str(document_id), company_id_str=str(company_id), minio_object_name=minio_object_name, file_name=file.filename or "untitled", content_type=content_type, original_metadata=metadata)
        task_id = task.id; request_log.info("Haystack task queued", task_id=task_id)
        return schemas.IngestResponse(document_id=document_id, task_id=task_id, status=DocumentStatus.UPLOADED, message="Document received and queued.")
    except HTTPException as http_exc: raise http_exc
    except (IOError, S3Error) as storage_err:
        request_log.error("Storage error", error=str(storage_err))
        if document_id:
            try:
                await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=f"Storage upload failed: {storage_err}")
            except Exception as db_err:
                request_log.error("Failed to update document status after storage error", db_error=str(db_err))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Storage service error.")
    except Exception as e:
        request_log.exception("Unexpected ingestion error")
        if document_id:
            try:
                await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=f"Ingestion error: {e}")
            except Exception as db_err:
                 request_log.error("Failed to update document status after ingestion error", db_error=str(db_err))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Ingestion failed.")
    finally: await file.close()


@router.get(
    "/status/{document_id}",
    response_model=schemas.StatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Consultar estado de ingesta de un documento",
    description="Recupera el estado actual del procesamiento y la información básica de un documento específico.",
)
async def get_ingestion_status(
    document_id: uuid.UUID,
    company_id: uuid.UUID = Depends(get_current_company_id),
):
    status_log = log.bind(document_id=str(document_id), company_id=str(company_id))
    status_log.info("Received request for single document status")
    try:
        doc_data = await postgres_client.get_document_status(document_id)
        if not doc_data: raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
        if doc_data.get("company_id") != company_id: raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found") # Security
        response_data = schemas.StatusResponse.model_validate(doc_data)
        status_messages = {
            DocumentStatus.UPLOADED: "Document uploaded, awaiting processing.",
            DocumentStatus.PROCESSING: "Document is currently being processed.",
            DocumentStatus.PROCESSED: "Document processed successfully.",
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
    company_id: uuid.UUID = Depends(get_current_company_id),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0)
):
    list_log = log.bind(company_id=str(company_id), limit=limit, offset=offset)
    list_log.info("Received request to list document statuses")
    try:
        documents_data = await postgres_client.list_documents_by_company(company_id, limit=limit, offset=offset)
        # *** CORREGIDO: Usar la variable correcta 'documents_data' ***
        response_list = [schemas.StatusResponse.model_validate(doc) for doc in documents_data]
        list_log.info(f"Returning status list for {len(response_list)} documents")
        return response_list
    except Exception as e:
        list_log.exception("Error listing document statuses")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error listing statuses.")
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
    error_message: Optional[str] = None # Mensaje de error de la DB
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
# *** CORREGIDO: Importar field_validator en lugar de validator ***
from pydantic import (
    RedisDsn, AnyHttpUrl, SecretStr, Field, field_validator, ValidationError, # Usar field_validator
    ValidationInfo # No se usa aquí, pero se mantiene por si acaso
)
import sys
import json # Para parsear MILVUS_INDEX_PARAMS/SEARCH_PARAMS

# --- Service Names en K8s ---
POSTGRES_K8S_SVC = "postgresql.nyro-develop.svc.cluster.local"           # Namespace: nyro-develop
MINIO_K8S_SVC = "minio-service.nyro-develop.svc.cluster.local"            # Namespace: nyro-develop
MILVUS_K8S_SVC = "milvus-milvus.default.svc.cluster.local"                # Namespace: default
REDIS_K8S_SVC = "redis-service-master.nyro-develop.svc.cluster.local"     # Namespace: nyro-develop

# --- Defaults ---
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex" # Base de datos para Atenex
POSTGRES_K8S_USER_DEFAULT = "postgres" # Usuario por defecto
MINIO_K8S_PORT_DEFAULT = 9000
MINIO_BUCKET_DEFAULT = "atenex" # Bucket específico
MILVUS_K8S_PORT_DEFAULT = 19530
REDIS_K8S_PORT_DEFAULT = 6379
MILVUS_DEFAULT_COLLECTION = "atenex_doc_chunks" # Nombre de colección más específico
MILVUS_DEFAULT_INDEX_PARAMS = '{"metric_type": "COSINE", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 256}}'
MILVUS_DEFAULT_SEARCH_PARAMS = '{"metric_type": "COSINE", "params": {"ef": 128}}'
OPENAI_DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_DIM = 1536 # Para text-embedding-3-small / ada-002

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='INGEST_',
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = "Atenex Ingest Service"
    API_V1_STR: str = "/api/v1/ingest" # Prefijo base para este servicio
    LOG_LEVEL: str = "INFO"

    # --- Celery (Usando Redis en K8s 'nyro-develop') ---
    CELERY_BROKER_URL: RedisDsn = RedisDsn(f"redis://{REDIS_K8S_SVC}:{REDIS_K8S_PORT_DEFAULT}/0")
    CELERY_RESULT_BACKEND: RedisDsn = RedisDsn(f"redis://{REDIS_K8S_SVC}:{REDIS_K8S_PORT_DEFAULT}/1")

    # --- Database (PostgreSQL Directo en K8s 'nyro-develop') ---
    POSTGRES_USER: str = POSTGRES_K8S_USER_DEFAULT
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = POSTGRES_K8S_SVC
    POSTGRES_PORT: int = POSTGRES_K8S_PORT_DEFAULT
    POSTGRES_DB: str = POSTGRES_K8S_DB_DEFAULT

    # --- Milvus (en K8s 'default') ---
    MILVUS_URI: str = f"http://{MILVUS_K8S_SVC}:{MILVUS_K8S_PORT_DEFAULT}"
    MILVUS_COLLECTION_NAME: str = MILVUS_DEFAULT_COLLECTION
    MILVUS_INDEX_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_INDEX_PARAMS))
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_SEARCH_PARAMS))
    MILVUS_CONTENT_FIELD: str = "content"
    MILVUS_EMBEDDING_FIELD: str = "embedding"
    MILVUS_METADATA_FIELDS: List[str] = Field(default=[
        "company_id", "document_id", "file_name", "file_type",
    ])

    # --- MinIO Storage (en K8s 'nyro-develop', bucket 'atenex') ---
    MINIO_ENDPOINT: str = f"{MINIO_K8S_SVC}:{MINIO_K8S_PORT_DEFAULT}"
    MINIO_ACCESS_KEY: SecretStr
    MINIO_SECRET_KEY: SecretStr
    MINIO_BUCKET_NAME: str = MINIO_BUCKET_DEFAULT
    MINIO_USE_SECURE: bool = False

    # --- External Services (OpenAI) ---
    OPENAI_API_KEY: SecretStr
    OPENAI_EMBEDDING_MODEL: str = OPENAI_DEFAULT_EMBEDDING_MODEL
    EMBEDDING_DIMENSION: int = DEFAULT_EMBEDDING_DIM

    # --- Service Client Config (Genérico) ---
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 2
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    # --- File Processing & Haystack ---
    SUPPORTED_CONTENT_TYPES: List[str] = Field(default=[
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", # DOCX
        "text/plain",
        "text/markdown",
        "text/html",
    ])
    SPLITTER_CHUNK_SIZE: int = 500
    SPLITTER_CHUNK_OVERLAP: int = 50
    SPLITTER_SPLIT_BY: str = "word"

    # --- Validadores (Pydantic V2 Style) ---

    @field_validator("LOG_LEVEL") # Usar field_validator si se aplica a un campo específico
    @classmethod # Los validadores de campo suelen ser classmethods
    def check_log_level(cls, v: str) -> str:
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        normalized_v = v.upper()
        if normalized_v not in valid_levels:
            raise ValueError(f"Invalid LOG_LEVEL '{v}'. Must be one of {valid_levels}")
        return normalized_v

    # Validador para parsear params de Milvus (default_factory es más simple aquí)

    @field_validator('EMBEDDING_DIMENSION', mode='before', check_fields=False) # Ejecutar antes de la validación del tipo
    @classmethod
    def set_embedding_dimension(cls, v: Optional[int], info: ValidationInfo) -> int:
        # En Pydantic V2, se accede a otros valores a través de info.data
        config_values = info.data # Diccionario con los valores ya parseados hasta este punto
        model = config_values.get('OPENAI_EMBEDDING_MODEL', OPENAI_DEFAULT_EMBEDDING_MODEL)

        calculated_dim = DEFAULT_EMBEDDING_DIM # Default inicial
        if model == "text-embedding-3-large": calculated_dim = 3072
        elif model in ["text-embedding-3-small", "text-embedding-ada-002"]: calculated_dim = 1536

        if v is not None and v != calculated_dim:
             logging.warning(f"Provided EMBEDDING_DIMENSION {v} conflicts with model {model} ({calculated_dim} expected). Using calculated value: {calculated_dim}")
             return calculated_dim # Devolver el valor calculado si hay conflicto
        elif v is None:
            return calculated_dim # Devolver el calculado si no se proporcionó explícitamente
        else: # v is not None and v == calculated_dim
            return v # Devolver el valor proporcionado si coincide con el calculado

    # *** CORREGIDO: Usar field_validator en lugar de validator y quitar 'mode' incorrecto ***
    @field_validator('POSTGRES_PASSWORD', 'MINIO_ACCESS_KEY', 'MINIO_SECRET_KEY', 'OPENAI_API_KEY', mode='before')
    @classmethod
    def check_secret_value_present(cls, v: Any, info: ValidationInfo) -> Any:
        """Valida que el valor para un campo secreto no esté vacío antes de convertir a SecretStr."""
        if v is None or v == "":
             # Obtener el nombre del campo desde ValidationInfo
             field_name = info.field_name if info.field_name else "Unknown Secret Field"
             raise ValueError(f"Required secret field '{field_name}' cannot be empty.")
        # Pydantic se encargará de convertir a SecretStr después de este validador 'before'
        return v


# --- Instancia Global ---
temp_log = logging.getLogger("ingest_service.config.loader")
# (Configuración del logger temporal sin cambios)...
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

# --- Document Operations (create_document, update_document_status, get_document_status, list_documents_by_company sin cambios) ---
async def create_document(company_id: uuid.UUID, file_name: str, file_type: str, metadata: Dict[str, Any]) -> uuid.UUID:
    pool = await get_db_pool(); doc_id = uuid.uuid4()
    query = "INSERT INTO documents (id, company_id, file_name, file_type, metadata, status, uploaded_at, updated_at) VALUES ($1, $2, $3, $4, $5, $6, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC') RETURNING id;"
    insert_log = log.bind(company_id=str(company_id), filename=file_name, content_type=file_type, proposed_doc_id=str(doc_id))
    try:
        async with pool.acquire() as connection: result_id = await connection.fetchval(query, doc_id, company_id, file_name, file_type, json.dumps(metadata), DocumentStatus.UPLOADED.value)
        if result_id and result_id == doc_id: insert_log.info("Document record created", document_id=str(doc_id)); return result_id
        else: insert_log.error("Failed to create document record", returned_id=result_id); raise RuntimeError(f"Failed create document, return mismatch ({result_id})")
    except asyncpg.exceptions.UniqueViolationError as e: insert_log.error("Unique constraint violation", error=str(e), constraint=e.constraint_name); raise ValueError(f"Document creation failed: unique constraint ({e.constraint_name})") from e
    except Exception as e: insert_log.error("Failed to create document record", error=str(e), exc_info=True); raise

async def update_document_status(document_id: uuid.UUID, status: DocumentStatus, file_path: Optional[str] = None, chunk_count: Optional[int] = None, error_message: Optional[str] = None) -> bool:
    pool = await get_db_pool(); update_log = log.bind(document_id=str(document_id), new_status=status.value)
    fields_to_set: List[str] = []; params: List[Any] = [document_id]; param_index = 2
    fields_to_set.append(f"status = ${param_index}"); params.append(status.value); param_index += 1
    fields_to_set.append(f"updated_at = NOW() AT TIME ZONE 'UTC'")
    if file_path is not None: fields_to_set.append(f"file_path = ${param_index}"); params.append(file_path); param_index += 1
    if chunk_count is not None: fields_to_set.append(f"chunk_count = ${param_index}"); params.append(chunk_count); param_index += 1
    if status == DocumentStatus.ERROR: safe_error = (error_message or "Unknown error")[:2000]; fields_to_set.append(f"error_message = ${param_index}"); params.append(safe_error); param_index += 1; update_log = update_log.bind(error_message=safe_error)
    else: fields_to_set.append("error_message = NULL")
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
    query = "SELECT id, status, file_name, file_type, chunk_count, error_message, updated_at, company_id FROM documents WHERE id = $1;"
    try:
        async with pool.acquire() as connection: record = await connection.fetchrow(query, document_id)
        if record: get_log.debug("Document status retrieved"); return dict(record)
        else: get_log.warning("Document status requested for non-existent ID"); return None
    except Exception as e: get_log.error("Failed to get status", error=str(e), exc_info=True); raise

async def list_documents_by_company(company_id: uuid.UUID, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    pool = await get_db_pool(); list_log = log.bind(company_id=str(company_id), limit=limit, offset=offset)
    query = "SELECT id, status, file_name, file_type, chunk_count, error_message, updated_at FROM documents WHERE company_id = $1 ORDER BY updated_at DESC LIMIT $2 OFFSET $3;"
    try:
        async with pool.acquire() as connection: records = await connection.fetch(query, company_id, limit, offset)
        result_list = [dict(record) for record in records]; list_log.info(f"Retrieved {len(result_list)} docs")
        return result_list
    except Exception as e: list_log.error("Failed to list docs", error=str(e), exc_info=True); raise

# --- Funciones de Chat (Añadidas/Corregidas desde el análisis del Query Service) ---

# *** CORREGIDO: Renombrar función para coincidir con llamada en chat.py ***
async def get_chats_for_user(user_id: uuid.UUID, company_id: uuid.UUID, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Obtiene la lista de chats para un usuario/compañía (sumario)."""
    pool = await get_db_pool()
    query = """
    SELECT id, title, updated_at FROM chats
    WHERE user_id = $1 AND company_id = $2
    ORDER BY updated_at DESC LIMIT $3 OFFSET $4;
    """
    try:
        async with pool.acquire() as conn: rows = await conn.fetch(query, user_id, company_id, limit, offset)
        chats = [dict(row) for row in rows]
        log.info(f"Retrieved {len(chats)} chat summaries", user_id=str(user_id), company_id=str(company_id))
        return chats
    except Exception as e: log.error("Failed get_chats_for_user", error=str(e), exc_info=True); raise

async def check_chat_ownership(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    """Verifica si un chat existe y pertenece al usuario/compañía."""
    pool = await get_db_pool()
    query = "SELECT EXISTS (SELECT 1 FROM chats WHERE id = $1 AND user_id = $2 AND company_id = $3);"
    try:
        async with pool.acquire() as conn: exists = await conn.fetchval(query, chat_id, user_id, company_id)
        return exists is True
    except Exception as e: log.error("Failed check_chat_ownership", chat_id=str(chat_id), error=str(e)); return False

# *** CORREGIDO: Renombrar función para coincidir con llamada en chat.py ***
async def get_messages_for_chat(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    """Obtiene mensajes de un chat, verificando propiedad."""
    pool = await get_db_pool()
    owner = await check_chat_ownership(chat_id, user_id, company_id)
    if not owner:
        log.warning("Attempt get_messages_for_chat not owned or non-existent", chat_id=str(chat_id), user_id=str(user_id))
        return []

    # *** CORREGIDO: Usar columna 'sources' ***
    messages_query = """
    SELECT id, role, content, sources, created_at FROM messages
    WHERE chat_id = $1 ORDER BY created_at ASC LIMIT $2 OFFSET $3;
    """
    try:
        async with pool.acquire() as conn: message_rows = await conn.fetch(messages_query, chat_id, limit, offset)
        messages = [dict(row) for row in message_rows]
        log.info(f"Retrieved {len(messages)} messages", chat_id=str(chat_id))
        return messages
    except Exception as e: log.error("Failed get_messages_for_chat", error=str(e), exc_info=True); raise

# *** CORREGIDO: Renombrar función para coincidir con llamada en query.py y usar columna 'sources' ***
async def add_message_to_chat(chat_id: uuid.UUID, role: str, content: str, sources: Optional[List[Dict[str, Any]]] = None) -> uuid.UUID:
    """Guarda un mensaje en un chat y actualiza el timestamp del chat."""
    pool = await get_db_pool()
    message_id = uuid.uuid4()
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                update_chat_query = "UPDATE chats SET updated_at = NOW() AT TIME ZONE 'UTC' WHERE id = $1 RETURNING id;"
                chat_updated = await conn.fetchval(update_chat_query, chat_id)
                if not chat_updated: raise ValueError(f"Chat {chat_id} not found for adding message.")

                # *** CORREGIDO: Usar columna 'sources' ***
                insert_message_query = """
                INSERT INTO messages (id, chat_id, role, content, sources, created_at)
                VALUES ($1, $2, $3, $4, $5, NOW() AT TIME ZONE 'UTC') RETURNING id;
                """
                result = await conn.fetchval(insert_message_query, message_id, chat_id, role, content, json.dumps(sources or []))

                if result and result == message_id:
                    log.info("Message saved", message_id=str(message_id), chat_id=str(chat_id), role=role)
                    return message_id
                else: raise RuntimeError("Failed save message, ID mismatch or not returned.")
            except Exception as e: log.error("Failed add_message_to_chat", error=str(e), exc_info=True); raise

# --- delete_chat (sin cambios necesarios) ---
async def delete_chat(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    pool = await get_db_pool(); delete_log = log.bind(chat_id=str(chat_id), user_id=str(user_id))
    query = "DELETE FROM chats WHERE id = $1 AND user_id = $2 AND company_id = $3 RETURNING id;"
    try:
        async with pool.acquire() as conn: deleted_id = await conn.fetchval(query, chat_id, user_id, company_id)
        success = deleted_id is not None
        if success: delete_log.info("Chat deleted")
        else: delete_log.warning("Chat not found or no permission")
        return success
    except Exception as e: delete_log.error("Failed to delete chat", error=str(e), exc_info=True); raise
```

## File: `app\main.py`
```py
# ingest-service/app/main.py
from fastapi import FastAPI, HTTPException, status as fastapi_status, Request
from fastapi.exceptions import RequestValidationError
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
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=getattr(exc, "headers", None)
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Validation Error", "errors": exc.errors()},
    )

# --- Router Inclusion ---
app.include_router(ingest.router, prefix=settings.API_V1_STR, tags=["Ingestion"])
log.info(f"Included ingestion router with prefix: {settings.API_V1_STR}")

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
        file_content_stream: IO[bytes], # Acepta BytesIO u otro stream
        content_type: str,
        content_length: int
    ) -> str:
        """
        Sube un archivo a MinIO de forma asíncrona usando run_in_executor.
        El nombre del objeto usa company_id/document_id/filename.
        Retorna el nombre completo del objeto en MinIO.
        """
        # Construir nombre del objeto para organización dentro del bucket 'atenex'
        object_name = f"{str(company_id)}/{str(document_id)}/{file_name}"
        upload_log = log.bind(bucket=self.bucket_name, object_name=object_name, content_type=content_type, length=content_length)
        upload_log.info("Queueing file upload to MinIO executor")

        loop = asyncio.get_running_loop()
        try:
            # Asegurarse que el stream está al inicio antes de pasarlo
            file_content_stream.seek(0)
            # Ejecutar la operación síncrona put_object en el executor
            result = await loop.run_in_executor(
                None, # Default ThreadPoolExecutor
                self.client.put_object, # La función síncrona
                # Argumentos para put_object:
                self.bucket_name,
                object_name,
                file_content_stream,
                content_length, # Pasar longitud explícitamente
                content_type=content_type,
            )
            upload_log.info("File uploaded successfully to MinIO via executor", etag=getattr(result, 'etag', None), version_id=getattr(result, 'version_id', None))
            return object_name
        except S3Error as e:
            upload_log.error("Failed to upload file to MinIO via executor", error=str(e), code=e.code, exc_info=True)
            # Re-lanzar S3Error para que el llamador lo maneje
            raise IOError(f"Failed to upload to storage: {e.code}") from e
        except Exception as e:
            upload_log.error("Unexpected error during file upload via executor", error=str(e), exc_info=True)
            # Re-lanzar como IOError genérico
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
# *** Importar excepciones de Milvus para manejo específico si es necesario ***
from pymilvus.exceptions import MilvusException
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
            uri=str(settings.MILVUS_URI),
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
        init_log.error("Milvus connection/initialization failed", code=me.code, message=me.message, exc_info=True)
        raise ConnectionError(f"Milvus connection failed: {me.message}") from me
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
     """Devuelve la clase del conversor Haystack apropiada."""
     converters = {
         "application/pdf": PyPDFToDocument,
         "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DOCXToDocument,
         "text/plain": TextFileToDocument,
         "text/markdown": MarkdownToDocument,
         "text/html": HTMLToDocument,
     }
     converter = converters.get(content_type)
     if not converter:
         log.warning("No Haystack converter found for content type", content_type=content_type)
     return converter

# --- Celery Task Definition ---
NON_RETRYABLE_ERRORS = (FileNotFoundError, ValueError, TypeError, NotImplementedError, KeyError, AttributeError)
RETRYABLE_ERRORS = (IOError, ConnectionError, TimeoutError, asyncpg.PostgresConnectionError, S3Error, MilvusException, Exception) # Añadir MilvusException

@celery_app.task(
    bind=True,
    autoretry_for=RETRYABLE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    retry_kwargs={'max_retries': 3, 'countdown': 60},
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
    """Tarea Celery para procesar un documento."""
    document_id = uuid.UUID(document_id_str)
    company_id = uuid.UUID(company_id_str)
    task_log = log.bind(document_id=str(document_id), company_id=str(company_id), task_id=self.request.id or "unknown", attempt=self.request.retries + 1)
    task_log.info("Starting Haystack document processing task execution")

    # --- Función async interna para orquestar el flujo ---
    async def async_process_flow():
        minio_client = None
        downloaded_file_stream: Optional[io.BytesIO] = None
        pipeline: Optional[Pipeline] = None # Inicializar pipeline como None

        try:
            # 0. Marcar como PROCESSING en DB
            task_log.info("Updating document status to PROCESSING")
            await postgres_client.update_document_status(document_id, DocumentStatus.PROCESSING)

            # 1. Descargar archivo de MinIO
            task_log.info("Attempting to download file from MinIO")
            minio_client = MinioStorageClient()
            downloaded_file_stream = await minio_client.download_file_stream(minio_object_name)
            file_bytes = downloaded_file_stream.getvalue()
            if not file_bytes: raise ValueError("Downloaded file is empty.")
            task_log.info(f"File downloaded successfully ({len(file_bytes)} bytes)")

            # *** CORREGIDO: Inicializar componentes y construir pipeline AQUÍ ***
            task_log.info("Initializing Haystack components and building pipeline...")
            loop = asyncio.get_running_loop()
            # Ejecutar inicialización síncrona en executor
            store = await loop.run_in_executor(None, _initialize_milvus_store)
            embedder = await loop.run_in_executor(None, _initialize_openai_embedder)
            splitter = await loop.run_in_executor(None, _initialize_splitter)
            writer = await loop.run_in_executor(None, _initialize_document_writer, store) # Pasar store inicializado

            ConverterClass = get_converter_for_content_type(content_type)
            if not ConverterClass: raise ValueError(f"Unsupported content type: {content_type}")
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
            # *** FIN CORRECCIÓN INICIALIZACIÓN ***

            # 3. Preparar Metadatos y ByteStream (Sin cambios)
            allowed_meta_keys = set(settings.MILVUS_METADATA_FIELDS)
            doc_meta = {"company_id": str(company_id), "document_id": str(document_id), "file_name": file_name or "unknown", "file_type": content_type or "unknown"}
            added_original_meta = 0
            for key, value in original_metadata.items():
                if key in allowed_meta_keys and key not in doc_meta:
                    doc_meta[key] = str(value) if value is not None else None; added_original_meta += 1
            task_log.debug("Prepared metadata for Haystack", final_meta=doc_meta, added_original_count=added_original_meta)
            source_stream = ByteStream(data=file_bytes, meta=doc_meta)
            pipeline_input = {"converter": {"sources": [source_stream]}}

            # 4. Ejecutar Pipeline Haystack (Síncrono -> Executor)
            task_log.info("Running Haystack pipeline via executor...")
            start_time = time.monotonic()
            pipeline_result = await loop.run_in_executor(None, pipeline.run, pipeline_input)
            duration = time.monotonic() - start_time
            task_log.info(f"Haystack pipeline execution finished via executor", duration_sec=round(duration, 2))

            # 5. Procesar Resultado y Contar Chunks (Sin cambios)
            processed_chunk_count = 0
            writer_output = pipeline_result.get("writer", {})
            if isinstance(writer_output, dict) and "documents_written" in writer_output:
                processed_chunk_count = writer_output["documents_written"]
                task_log.info(f"Chunks written to Milvus: {processed_chunk_count}")
            else: # Fallback
                task_log.warning("Could not determine count from writer", output=writer_output)
                splitter_output = pipeline_result.get("splitter", {})
                if isinstance(splitter_output, dict) and "documents" in splitter_output:
                     processed_chunk_count = len(splitter_output["documents"])
                     task_log.warning(f"Inferred chunk count from splitter: {processed_chunk_count}")
                else: task_log.error("Pipeline failed, no documents processed/written."); raise RuntimeError("Pipeline failed.")

            # 6. Actualizar Estado Final en DB
            final_status = DocumentStatus.PROCESSED
            task_log.info(f"Updating document status to {final_status.value} with {processed_chunk_count} chunks.")
            await postgres_client.update_document_status(document_id, final_status, chunk_count=processed_chunk_count, error_message=None)
            task_log.info("Document status updated successfully in PostgreSQL.")

        except NON_RETRYABLE_ERRORS as e_non_retry:
            err_msg = f"Non-retryable error: {type(e_non_retry).__name__}: {str(e_non_retry)[:500]}"
            task_log.error(f"Processing failed permanently: {err_msg}", exc_info=True)
            try: await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=err_msg)
            except Exception as db_err: task_log.critical("Failed update status to ERROR after non-retryable failure!", db_error=str(db_err))
            # No relanzar

        except RETRYABLE_ERRORS as e_retry:
            err_msg = f"Retryable error: {type(e_retry).__name__}: {str(e_retry)[:500]}"
            task_log.warning(f"Processing failed, will retry: {err_msg}", exc_info=True)
            try: await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=f"Task Error (Retry Attempt {self.request.retries + 1}): {err_msg}")
            except Exception as db_err: task_log.error("Failed update status to ERROR during retryable failure!", db_error=str(db_err))
            raise e_retry # Relanzar para Celery

        finally:
            if downloaded_file_stream: downloaded_file_stream.close()
            task_log.debug("Cleaned up task resources.")

    # --- Ejecutar el flujo async ---
    try:
        asyncio.run(async_process_flow())
        task_log.info("Haystack document processing task completed.")
    except Exception as top_level_exc:
        # Captura excepciones relanzadas por async_process_flow (RETRYABLE)
        task_log.exception("Haystack processing task failed at top level (pending retry or final failure).")
        # Celery manejará el reintento o fallo final basado en la excepción
        pass
```

## File: `app\utils\__init__.py`
```py

```

## File: `app\utils\helpers.py`
```py

```

## File: `pyproject.toml`
```toml
# ingest-service/pyproject.toml
[tool.poetry]
name = "ingest-service"
version = "0.1.1" # Incremento de versión
description = "Ingest service for Atenex B2B SaaS (Haystack/Postgres/Minio/Milvus)" # Descripción actualizada
authors = ["Atenex Team <dev@atenex.com>"] # Autor actualizado

[tool.poetry.dependencies]
python = "^3.10"
fastapi = "^0.110.0"
uvicorn = {extras = ["standard"], version = "^0.28.0"}
gunicorn = "^21.2.0"
pydantic = {extras = ["email"], version = "^2.6.4"}
pydantic-settings = "^2.2.1"
celery = {extras = ["redis"], version = "^5.3.6"}
gevent = "^23.9.1" # Necesario para el pool de workers de Celery
httpx = "^0.27.0"
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


[tool.poetry.group.dev.dependencies] # Grupo dev corregido
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"
httpx = "^0.27.0" # Para test client
# Añadir otros como black, ruff, mypy si los usas

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
