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
    status, Form, Header, Query # Importar Query para parámetros de consulta si fueran necesarios
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

# --- Dependency for Company ID (Correcta) ---
async def get_current_company_id(x_company_id: Optional[str] = Header(None)) -> uuid.UUID:
    if not x_company_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-Company-ID header")
    try:
        company_uuid = uuid.UUID(x_company_id)
        log.debug("Validated X-Company-ID", company_id=str(company_uuid))
        return company_uuid
    except ValueError:
         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid X-Company-ID header format (must be UUID)")

# --- Endpoints ---

@router.post(
    "/upload", # Ruta relativa al prefijo /api/v1/ingest añadido en main.py
    response_model=schemas.IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingestar un nuevo documento",
    description="Sube un documento, lo almacena, crea un registro en BD y lo encola para procesamiento Haystack.",
)
async def ingest_document_haystack(
    # Dependencias primero
    company_id: uuid.UUID = Depends(get_current_company_id),
    # Datos del Formulario
    metadata_json: str = Form(default="{}", description="String JSON de metadatos del documento"),
    file: UploadFile = File(..., description="El archivo del documento a ingestar"),
    # NINGÚN OTRO PARÁMETRO QUE PUEDA SER INTERPRETADO COMO BODY O QUERY['query']
):
    request_log = log.bind(company_id=str(company_id), filename=file.filename, content_type=file.content_type)
    request_log.info("Received document ingestion request (Haystack)")

    # 1. Validate Content Type
    if not file.content_type or file.content_type not in settings.SUPPORTED_CONTENT_TYPES:
        request_log.warning("Unsupported or missing content type received", received_type=file.content_type)
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=f"Unsupported file type: {file.content_type or 'Unknown'}. Supported types: {settings.SUPPORTED_CONTENT_TYPES}")
    content_type = file.content_type

    # 2. Validate Metadata
    try:
        metadata = json.loads(metadata_json); assert isinstance(metadata, dict)
    except (json.JSONDecodeError, AssertionError):
         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON object format for metadata")
    except Exception as e:
         raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid metadata: {e}")

    minio_client = MinioStorageClient(); minio_object_name: Optional[str] = None
    document_id: Optional[uuid.UUID] = None; task_id: Optional[str] = None

    try:
        # --- Lógica de creación, subida y encolado (sin cambios) ---
        document_id = await postgres_client.create_document(company_id=company_id, file_name=file.filename or "untitled", file_type=content_type, metadata=metadata)
        request_log = request_log.bind(document_id=str(document_id)); request_log.info("Initial document record created")
        file_content = await file.read(); content_length = len(file_content)
        if content_length == 0:
            await postgres_client.update_document_status(document_id=document_id, status=DocumentStatus.ERROR, error_message="Uploaded file is empty.")
            request_log.error("Uploaded file is empty."); raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file cannot be empty.")
        file_stream = io.BytesIO(file_content)
        minio_object_name = await minio_client.upload_file(company_id=company_id, document_id=document_id, file_name=file.filename or "untitled", file_content_stream=file_stream, content_type=content_type, content_length=content_length)
        request_log.info("File uploaded to MinIO", object_name=minio_object_name)
        await postgres_client.update_document_status(document_id=document_id, status=DocumentStatus.UPLOADED, file_path=minio_object_name)
        request_log.info("Document record updated with MinIO path")
        task = process_document_haystack_task.delay(document_id_str=str(document_id), company_id_str=str(company_id), minio_object_name=minio_object_name, file_name=file.filename or "untitled", content_type=content_type, original_metadata=metadata)
        task_id = task.id
        request_log.info("Haystack processing task queued", task_id=task_id)
        return schemas.IngestResponse(document_id=document_id, task_id=task_id, status=DocumentStatus.UPLOADED, message="Document upload received and queued for processing.")
    # --- Manejo de errores (sin cambios) ---
    except HTTPException as http_exc: raise http_exc
    except S3Error as s3_err: request_log.error("MinIO S3 Error", error=str(s3_err), code=s3_err.code, exc_info=True); raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Storage service error: {s3_err.code}")
    except Exception as e: request_log.error("Unexpected ingestion error", error=str(e), exc_info=True); raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to process ingestion request.")
    finally: await file.close()

@router.get(
    "/status/{document_id}", # Ruta relativa correcta
    response_model=schemas.StatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Consultar estado de ingesta de un documento",
    description="Recupera el estado actual del procesamiento y la información básica de un documento específico.",
)
async def get_ingestion_status(
    # Path parameter
    document_id: uuid.UUID,
    # Header dependency
    company_id: uuid.UUID = Depends(get_current_company_id),
    # NINGÚN OTRO PARÁMETRO (especialmente ninguno llamado 'query' que pueda ser un modelo)
):
    status_log = log.bind(document_id=str(document_id), company_id=str(company_id))
    status_log.info("Received request for single document status")
    # --- Lógica de obtención y validación (sin cambios) ---
    try: doc_data = await postgres_client.get_document_status(document_id)
    except Exception as e: status_log.error("DB error getting status", e=e); raise HTTPException(status_code=500)
    if not doc_data: raise HTTPException(status_code=404)
    if doc_data.get("company_id") != company_id: raise HTTPException(status_code=403)
    try: response_data = schemas.StatusResponse.model_validate(doc_data)
    except Exception as p_err: status_log.error("Schema validation error", e=p_err); raise HTTPException(status_code=500)
    # --- Mensaje descriptivo (sin cambios) ---
    status_messages = {
        DocumentStatus.UPLOADED: "The document has been successfully uploaded.",
        DocumentStatus.PROCESSING: "The document is currently being processed.",
        DocumentStatus.COMPLETED: "The document has been processed successfully.",
        DocumentStatus.ERROR: "An error occurred during document processing.",
    }
    response_data.message = status_messages.get(response_data.status, "Unknown status.")
    status_log.info("Returning document status", status=response_data.status)
    return response_data


@router.get(
    "/status", # Ruta relativa correcta
    response_model=List[schemas.StatusResponse],
    status_code=status.HTTP_200_OK,
    summary="Listar estados de ingesta para la compañía",
    description="Recupera una lista de todos los documentos y sus estados de procesamiento para la compañía actual.",
)
async def list_ingestion_statuses(
    # Header dependency
    company_id: uuid.UUID = Depends(get_current_company_id),
    # Opcional: Añadir parámetros de consulta explícitos si se necesitan
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0)
    # NINGÚN OTRO PARÁMETRO (especialmente ninguno llamado 'query' que pueda ser un modelo)
):
    list_log = log.bind(company_id=str(company_id), limit=limit, offset=offset)
    list_log.info("Received request to list document statuses")
    try:
        # Pasar limit y offset a la función DB si la soporta
        # documents_data = await postgres_client.list_documents_by_company(company_id, limit=limit, offset=offset)
        # Si no, obtener todos y paginar aquí (menos eficiente)
        documents_data = await postgres_client.list_documents_by_company(company_id, limit=limit, offset=offset) # Implementar paginación en la consulta DB

    except Exception as e: list_log.error("DB error listing statuses", e=e); raise HTTPException(status_code=500)

    response_list = [schemas.StatusResponse.model_validate(doc_data) for doc_data in paginated_data]
    list_log.info(f"Returning status list", count=len(response_list))
    return response_list
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
from pydantic import RedisDsn, AnyHttpUrl, SecretStr, Field, validator, ValidationError, HttpUrl
import sys
import json # Para parsear MILVUS_INDEX_PARAMS/SEARCH_PARAMS

# --- Service Names en K8s ---
POSTGRES_K8S_SVC = "postgresql.nyro-develop.svc.cluster.local"           # Namespace: nyro-develop
MINIO_K8S_SVC = "minio-service.nyro-develop.svc.cluster.local"            # Namespace: nyro-develop
# *** CORREGIDO: Apuntar explícitamente al namespace 'default' para Milvus ***
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
    # *** CORREGIDO: Construir URI usando MILVUS_K8S_SVC que apunta a 'default' ***
    MILVUS_URI: str = f"http://{MILVUS_K8S_SVC}:{MILVUS_K8S_PORT_DEFAULT}"
    MILVUS_COLLECTION_NAME: str = MILVUS_DEFAULT_COLLECTION
    MILVUS_INDEX_PARAMS: Dict[str, Any] = Field(default=json.loads(MILVUS_DEFAULT_INDEX_PARAMS))
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default=json.loads(MILVUS_DEFAULT_SEARCH_PARAMS))
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

    # --- Validadores ---
    @validator("LOG_LEVEL")
    def check_log_level(cls, v):
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if v.upper() not in valid_levels:
            raise ValueError(f"Invalid LOG_LEVEL '{v}'. Must be one of {valid_levels}")
        return v.upper()

    @validator("MILVUS_INDEX_PARAMS", "MILVUS_SEARCH_PARAMS", pre=True)
    def parse_milvus_params(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                raise ValueError("Milvus params must be a valid JSON string if provided as string")
        return v

    @validator('EMBEDDING_DIMENSION', pre=True, always=True)
    def set_embedding_dimension(cls, v: Optional[int], values: Dict[str, Any]) -> int:
        model = values.get('OPENAI_EMBEDDING_MODEL', OPENAI_DEFAULT_EMBEDDING_MODEL)
        if model == "text-embedding-3-large":
            calculated_dim = 3072
        elif model in ["text-embedding-3-small", "text-embedding-ada-002"]:
            calculated_dim = 1536
        else:
            return v if v is not None else DEFAULT_EMBEDDING_DIM

        if v is not None and v != calculated_dim:
             logging.warning(f"Provided EMBEDDING_DIMENSION {v} conflicts with model {model} ({calculated_dim} expected). Using calculated value: {calculated_dim}")
             return calculated_dim
        return calculated_dim

    @validator('POSTGRES_PASSWORD', 'MINIO_ACCESS_KEY', 'MINIO_SECRET_KEY', 'OPENAI_API_KEY')
    def check_secrets_not_empty(cls, v: SecretStr, field): # Corrección nombre field
        field_name = field.alias if field.alias else field.name
        if not v or not v.get_secret_value():
            raise ValueError(f"Secret field '{field_name}' must not be empty.")
        return v

# --- Instancia Global ---
temp_log = logging.getLogger("ingest_service.config.loader")
# ... (resto del código de inicialización y logging de config sin cambios) ...
try:
    temp_log.info("Loading Ingest Service settings...")
    settings = Settings()
    temp_log.info("Ingest Service Settings Loaded Successfully:")
    temp_log.info(f"  PROJECT_NAME: {settings.PROJECT_NAME}")
    temp_log.info(f"  LOG_LEVEL: {settings.LOG_LEVEL}")
    temp_log.info(f"  API_V1_STR: {settings.API_V1_STR}")
    temp_log.info(f"  CELERY_BROKER_URL: {settings.CELERY_BROKER_URL}")
    temp_log.info(f"  CELERY_RESULT_BACKEND: {settings.CELERY_RESULT_BACKEND}")
    temp_log.info(f"  POSTGRES_SERVER: {settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}")
    temp_log.info(f"  POSTGRES_DB: {settings.POSTGRES_DB}")
    temp_log.info(f"  POSTGRES_USER: {settings.POSTGRES_USER}")
    temp_log.info(f"  POSTGRES_PASSWORD: *** SET ***")
    temp_log.info(f"  MILVUS_URI: {settings.MILVUS_URI} (Points to 'default' namespace service)") # Nota aclaratoria
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

# --- Pool Management ---
async def get_db_pool() -> asyncpg.Pool:
    """Obtiene o crea el pool de conexiones a PostgreSQL."""
    global _pool
    if _pool is None or _pool._closed:
        log.info("PostgreSQL pool is not initialized or closed. Creating new pool...",
                 host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT,
                 user=settings.POSTGRES_USER, db=settings.POSTGRES_DB)
        try:
            # Codec para manejar JSONB correctamente
            def _json_encoder(value): return json.dumps(value)
            def _json_decoder(value): return json.loads(value)
            async def init_connection(conn):
                await conn.set_type_codec('jsonb', encoder=_json_encoder, decoder=_json_decoder, schema='pg_catalog')
                await conn.set_type_codec('json', encoder=_json_encoder, decoder=_json_decoder, schema='pg_catalog')

            _pool = await asyncpg.create_pool(
                user=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD.get_secret_value(),
                database=settings.POSTGRES_DB,
                host=settings.POSTGRES_SERVER,
                port=settings.POSTGRES_PORT,
                min_size=2, # Ajusta según carga esperada
                max_size=10,
                timeout=30.0, # Timeout de conexión
                command_timeout=60.0, # Timeout por comando
                init=init_connection,
                statement_cache_size=0 # Deshabilitar si causa problemas
            )
            log.info("PostgreSQL connection pool created successfully.")
        except (asyncpg.exceptions.InvalidPasswordError, OSError, ConnectionRefusedError) as conn_err:
            log.critical("CRITICAL: Failed to connect to PostgreSQL", error=str(conn_err), exc_info=True)
            _pool = None
            raise ConnectionError(f"Failed to connect to PostgreSQL: {conn_err}") from conn_err
        except Exception as e:
            log.critical("CRITICAL: Failed to create PostgreSQL connection pool", error=str(e), exc_info=True)
            _pool = None
            raise RuntimeError(f"Failed to create PostgreSQL pool: {e}") from e
    return _pool

async def close_db_pool():
    """Cierra el pool de conexiones."""
    global _pool
    if _pool and not _pool._closed:
        log.info("Closing PostgreSQL connection pool...")
        await _pool.close()
        _pool = None
        log.info("PostgreSQL connection pool closed.")
    elif _pool and _pool._closed:
        log.warning("Attempted to close an already closed PostgreSQL pool.")
        _pool = None
    else:
        log.info("No active PostgreSQL connection pool to close.")

async def check_db_connection() -> bool:
    """Verifica que la conexión a la base de datos esté funcionando."""
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            async with conn.transaction(): # Usa transacción para asegurar que no haya efectos secundarios
                result = await conn.fetchval("SELECT 1")
        return result == 1
    except Exception as e:
        log.error("Database connection check failed", error=str(e))
        return False

# --- Document Operations ---

async def create_document(
    company_id: uuid.UUID,
    file_name: str,
    file_type: str,
    metadata: Dict[str, Any]
) -> uuid.UUID:
    """
    Crea un registro inicial para el documento en la tabla DOCUMENTS.
    Retorna el UUID del documento creado.
    """
    pool = await get_db_pool()
    doc_id = uuid.uuid4()
    # Usar NOW() para timestamps, status inicial UPLOADED
    # Incluir updated_at en el INSERT inicial
    query = """
        INSERT INTO documents (id, company_id, file_name, file_type, metadata, status, uploaded_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC')
        RETURNING id;
    """
    insert_log = log.bind(company_id=str(company_id), filename=file_name, content_type=file_type, proposed_doc_id=str(doc_id))
    try:
        async with pool.acquire() as connection:
            # Usar json.dumps para el campo jsonb 'metadata'
            result_id = await connection.fetchval(
                query, doc_id, company_id, file_name, file_type, json.dumps(metadata), DocumentStatus.UPLOADED.value
            )
        if result_id and result_id == doc_id:
            insert_log.info("Document record created in PostgreSQL", document_id=str(doc_id))
            return result_id
        else:
             insert_log.error("Failed to create document record, unexpected or no ID returned.", returned_id=result_id)
             raise RuntimeError(f"Failed to create document record, return value mismatch ({result_id})")
    except asyncpg.exceptions.UniqueViolationError as e:
        # Esto es improbable si usamos UUID v4, pero posible si hay constraints en otros campos
        insert_log.error("Unique constraint violation creating document record.", error=str(e), constraint=e.constraint_name, exc_info=False)
        raise ValueError(f"Document creation failed due to unique constraint ({e.constraint_name})") from e
    except Exception as e:
        insert_log.error("Failed to create document record in PostgreSQL", error=str(e), exc_info=True)
        raise # Relanzar para manejo superior

async def update_document_status(
    document_id: uuid.UUID,
    status: DocumentStatus,
    file_path: Optional[str] = None,
    chunk_count: Optional[int] = None,
    error_message: Optional[str] = None,
) -> bool:
    """
    Actualiza el estado y otros campos de un documento.
    Limpia error_message si el estado no es ERROR.
    """
    pool = await get_db_pool()
    update_log = log.bind(document_id=str(document_id), new_status=status.value)

    fields_to_set: List[str] = []
    params: List[Any] = [document_id] # $1 será el ID
    param_index = 2 # Empezar parámetros desde $2

    # Siempre actualizar 'status' y 'updated_at'
    fields_to_set.append(f"status = ${param_index}")
    params.append(status.value); param_index += 1
    fields_to_set.append(f"updated_at = NOW() AT TIME ZONE 'UTC'")

    # Añadir otros campos condicionalmente
    if file_path is not None:
        fields_to_set.append(f"file_path = ${param_index}")
        params.append(file_path); param_index += 1
    if chunk_count is not None:
        fields_to_set.append(f"chunk_count = ${param_index}")
        params.append(chunk_count); param_index += 1

    # Manejar error_message
    if status == DocumentStatus.ERROR:
        safe_error = (error_message or "Unknown processing error")[:2000] # Limitar longitud de error
        fields_to_set.append(f"error_message = ${param_index}")
        params.append(safe_error); param_index += 1
        update_log = update_log.bind(error_message=safe_error)
    else:
        # Si el nuevo estado NO es ERROR, limpiar el campo error_message
        fields_to_set.append("error_message = NULL")

    query = f"UPDATE documents SET {', '.join(fields_to_set)} WHERE id = $1;"
    update_log.debug("Executing document status update", query=query, params_count=len(params))

    try:
        async with pool.acquire() as connection:
             result_str = await connection.execute(query, *params) # Desempaquetar params

        # Verificar si se actualizó alguna fila
        if isinstance(result_str, str) and result_str.startswith("UPDATE "):
            affected_rows = int(result_str.split(" ")[1])
            if affected_rows > 0:
                update_log.info("Document status updated successfully", affected_rows=affected_rows)
                return True
            else:
                update_log.warning("Document status update command executed but no rows were affected (document ID might not exist).")
                return False
        else:
             update_log.error("Unexpected result from document update execution", db_result=result_str)
             return False # Considerar esto como fallo

    except Exception as e:
        update_log.error("Failed to update document status in PostgreSQL", error=str(e), exc_info=True)
        raise # Relanzar para manejo superior

async def get_document_status(document_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """Obtiene datos clave de un documento por su ID."""
    pool = await get_db_pool()
    # Seleccionar columnas necesarias para la API (schema StatusResponse) y validación de company_id
    query = """
        SELECT id, status, file_name, file_type, chunk_count, error_message, updated_at, company_id
        FROM documents
        WHERE id = $1;
    """
    get_log = log.bind(document_id=str(document_id))
    try:
        async with pool.acquire() as connection:
            record = await connection.fetchrow(query, document_id)
        if record:
            get_log.debug("Document status retrieved successfully")
            return dict(record) # Convertir a dict
        else:
            get_log.warning("Document status requested for non-existent ID")
            return None
    except Exception as e:
        get_log.error("Failed to get document status from PostgreSQL", error=str(e), exc_info=True)
        raise

async def list_documents_by_company(
    company_id: uuid.UUID,
    limit: int = 100,
    offset: int = 0
) -> List[Dict[str, Any]]:
    """
    Obtiene una lista paginada de documentos para una compañía, ordenados por updated_at DESC.
    """
    pool = await get_db_pool()
    # Seleccionar columnas para StatusResponse
    query = """
        SELECT id, status, file_name, file_type, chunk_count, error_message, updated_at
        FROM documents
        WHERE company_id = $1
        ORDER BY updated_at DESC
        LIMIT $2 OFFSET $3;
    """
    list_log = log.bind(company_id=str(company_id), limit=limit, offset=offset)
    try:
        async with pool.acquire() as connection:
            records = await connection.fetch(query, company_id, limit, offset)

        result_list = [dict(record) for record in records] # Convertir Records a Dicts
        list_log.info(f"Retrieved {len(result_list)} documents for company listing")
        return result_list
    except Exception as e:
        list_log.error("Failed to list documents by company from PostgreSQL", error=str(e), exc_info=True)
        raise
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
from haystack.components.writers import DocumentWriter
from haystack.dataclasses import ByteStream

# --- Local Imports ---
from app.tasks.celery_app import celery_app
from app.core.config import settings
from app.db import postgres_client # Cliente DB async
from app.models.domain import DocumentStatus
from app.services.minio_client import MinioStorageClient # Cliente MinIO async

log = structlog.get_logger(__name__)

# --- Funciones Helper Síncronas para Haystack (se ejecutarán en executor o sync) ---
def initialize_haystack_components() -> Dict[str, Any]:
    """Inicializa y devuelve un diccionario con los componentes Haystack necesarios."""
    task_log = log.bind(component_init="haystack")
    task_log.info("Initializing Haystack components...")
    try:
        document_store = MilvusDocumentStore(
            uri=str(settings.MILVUS_URI), # Asegurar que URI es string
            collection_name=settings.MILVUS_COLLECTION_NAME,
            dim=settings.EMBEDDING_DIMENSION,
            embedding_field=settings.MILVUS_EMBEDDING_FIELD,
            content_field=settings.MILVUS_CONTENT_FIELD,
            metadata_fields=settings.MILVUS_METADATA_FIELDS,
            index_params=settings.MILVUS_INDEX_PARAMS,
            search_params=settings.MILVUS_SEARCH_PARAMS,
            consistency_level="Strong", # O Bounded, Session, Eventually
        )
        task_log.debug("MilvusDocumentStore initialized")

        # Usar el valor del SecretStr correctamente
        api_key_value = settings.OPENAI_API_KEY.get_secret_value()
        if not api_key_value:
            task_log.error("OpenAI API Key is missing!")
            raise ValueError("OpenAI API Key is required but missing in configuration.")

        embedder = OpenAIDocumentEmbedder(
            api_key=Secret.from_token(api_key_value), # Usar from_token ya que tenemos el valor
            model=settings.OPENAI_EMBEDDING_MODEL,
            meta_fields_to_embed=[] # Ajustar si es necesario
        )
        task_log.debug("OpenAIDocumentEmbedder initialized", model=settings.OPENAI_EMBEDDING_MODEL)

        splitter = DocumentSplitter(
            split_by=settings.SPLITTER_SPLIT_BY,
            split_length=settings.SPLITTER_CHUNK_SIZE,
            split_overlap=settings.SPLITTER_CHUNK_OVERLAP
        )
        task_log.debug("DocumentSplitter initialized", split_by=settings.SPLITTER_SPLIT_BY, len=settings.SPLITTER_CHUNK_SIZE, overlap=settings.SPLITTER_CHUNK_OVERLAP)

        writer = DocumentWriter(document_store=document_store)
        task_log.debug("DocumentWriter initialized")

        task_log.info("Haystack components initialized successfully.")
        return {
            "document_store": document_store,
            "embedder": embedder,
            "splitter": splitter,
            "writer": writer,
        }
    except Exception as e:
        task_log.exception("Failed to initialize Haystack components", error=str(e))
        raise RuntimeError(f"Haystack component initialization failed: {e}") from e

def get_converter_for_content_type(content_type: str) -> Optional[Type]:
     """Devuelve la clase del conversor Haystack apropiada."""
     # Mapeo simple
     converters = {
         "application/pdf": PyPDFToDocument,
         "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DOCXToDocument,
         "text/plain": TextFileToDocument,
         "text/markdown": MarkdownToDocument,
         "text/html": HTMLToDocument,
         # Añadir más tipos si se soportan
     }
     converter = converters.get(content_type)
     if converter:
         log.debug("Selected Haystack converter", converter=converter.__name__, content_type=content_type)
     else:
         log.warning("No specific Haystack converter found for content type", content_type=content_type)
     return converter

def build_haystack_pipeline(converter_instance, splitter, embedder, writer) -> Pipeline:
    """Construye el pipeline Haystack dinámicamente."""
    task_log = log.bind(pipeline_build="haystack")
    task_log.info("Building Haystack processing pipeline...")
    pipeline = Pipeline()
    try:
        pipeline.add_component("converter", converter_instance)
        pipeline.add_component("splitter", splitter)
        pipeline.add_component("embedder", embedder)
        pipeline.add_component("writer", writer)

        pipeline.connect("converter.documents", "splitter.documents")
        pipeline.connect("splitter.documents", "embedder.documents")
        pipeline.connect("embedder.documents", "writer.documents")
        task_log.info("Haystack pipeline built successfully.")
        return pipeline
    except Exception as e:
        task_log.exception("Failed to build Haystack pipeline", error=str(e))
        raise RuntimeError(f"Haystack pipeline construction failed: {e}") from e

# --- Celery Task Definition ---
# Errores NO reintentables: FileNotFoundError, ValueError, TypeError, NotImplementedError, KeyError
NON_RETRYABLE_ERRORS = (FileNotFoundError, ValueError, TypeError, NotImplementedError, KeyError, AttributeError)
# Errores SÍ reintentables: IOError, ConnectionError, TimeoutError, y genérico Exception (con precaución)
RETRYABLE_ERRORS = (IOError, ConnectionError, TimeoutError, asyncpg.PostgresConnectionError, S3Error, Exception)

@celery_app.task(
    bind=True,
    autoretry_for=RETRYABLE_ERRORS, # Reintentar solo en errores recuperables
    retry_backoff=True, # Backoff exponencial
    retry_backoff_max=300, # Máximo 5 minutos de espera
    retry_jitter=True, # Añadir aleatoriedad a la espera
    retry_kwargs={'max_retries': 3, 'countdown': 60}, # Máximo 3 reintentos, empezando con 60s
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
    """
    Tarea Celery para procesar un documento: descarga de MinIO, procesa con Haystack, indexa en Milvus.
    Utiliza asyncio.run para la lógica async y run_in_executor para Haystack.
    """
    document_id = uuid.UUID(document_id_str)
    company_id = uuid.UUID(company_id_str)
    # Logger contextual para la tarea
    task_log = log.bind(
        document_id=str(document_id),
        company_id=str(company_id),
        task_id=self.request.id or "unknown",
        file_name=file_name,
        object_name=minio_object_name,
        content_type=content_type,
        attempt=self.request.retries + 1 # Número de intento actual
    )
    task_log.info("Starting Haystack document processing task execution")

    # --- Función async interna para orquestar el flujo ---
    async def async_process_flow():
        minio_client = None # Inicializar fuera del try
        downloaded_file_stream: Optional[io.BytesIO] = None
        haystack_components = {}
        pipeline_run_successful = False
        processed_chunk_count = 0

        try:
            # 0. Marcar como PROCESSING en DB (async)
            task_log.info("Updating document status to PROCESSING")
            await postgres_client.update_document_status(document_id, DocumentStatus.PROCESSING)

            # 1. Descargar archivo de MinIO (async, usa executor internamente)
            task_log.info("Attempting to download file from MinIO")
            minio_client = MinioStorageClient() # Instanciar aquí
            downloaded_file_stream = await minio_client.download_file_stream(minio_object_name)
            file_bytes = downloaded_file_stream.getvalue()
            if not file_bytes:
                task_log.error("Downloaded file is empty.")
                # Este es un error de datos, no reintentable
                raise ValueError("Downloaded file is empty.")
            task_log.info(f"File downloaded successfully ({len(file_bytes)} bytes)")

            # 2. Inicializar Componentes Haystack (Síncrono, podría ser largo)
            # Ejecutar en executor para no bloquear el worker por mucho tiempo si es lento
            task_log.info("Initializing Haystack components via executor...")
            loop = asyncio.get_running_loop()
            haystack_components = await loop.run_in_executor(None, initialize_haystack_components)
            task_log.info("Haystack components ready.")

            # 3. Preparar Metadatos y ByteStream para Haystack
            # Filtrar metadatos para incluir solo los definidos en config + los esenciales
            allowed_meta_keys = set(settings.MILVUS_METADATA_FIELDS)
            doc_meta = {
                "company_id": str(company_id), # Asegurar string
                "document_id": str(document_id), # Asegurar string
                "file_name": file_name or "unknown",
                "file_type": content_type or "unknown",
            }
            added_original_meta = 0
            for key, value in original_metadata.items():
                if key in allowed_meta_keys and key not in doc_meta:
                    doc_meta[key] = str(value) if value is not None else None
                    added_original_meta += 1
            task_log.debug("Prepared metadata for Haystack", final_meta=doc_meta, added_original_count=added_original_meta)
            source_stream = ByteStream(data=file_bytes, meta=doc_meta)

            # 4. Seleccionar Conversor y Construir Pipeline (Síncrono)
            ConverterClass = get_converter_for_content_type(content_type)
            if not ConverterClass:
                 task_log.error("Unsupported content type for Haystack converters", content_type=content_type)
                 raise ValueError(f"Unsupported content type: {content_type}")

            converter_instance = ConverterClass()
            pipeline = build_haystack_pipeline(
                converter_instance,
                haystack_components["splitter"],
                haystack_components["embedder"],
                haystack_components["writer"]
            )
            pipeline_input = {"converter": {"sources": [source_stream]}}

            # 5. Ejecutar Pipeline Haystack (Síncrono y potencialmente largo -> Executor)
            task_log.info("Running Haystack pipeline via executor...")
            start_time = time.monotonic()
            pipeline_result = await loop.run_in_executor(None, pipeline.run, pipeline_input)
            duration = time.monotonic() - start_time
            task_log.info(f"Haystack pipeline execution finished via executor", duration_sec=round(duration, 2))

            # 6. Procesar Resultado y Contar Chunks
            writer_output = pipeline_result.get("writer", {})
            if isinstance(writer_output, dict) and "documents_written" in writer_output:
                processed_chunk_count = writer_output["documents_written"]
                task_log.info(f"Chunks written to Milvus: {processed_chunk_count}")
                pipeline_run_successful = True # Asumir éxito si el writer reporta algo
            else:
                # Intentar inferir de otro componente si es posible, o marcar como 0/error
                task_log.warning("Could not determine documents written from writer output", output=writer_output)
                # Podrías intentar contar desde splitter output como fallback
                splitter_output = pipeline_result.get("splitter", {})
                if isinstance(splitter_output, dict) and "documents" in splitter_output:
                     processed_chunk_count = len(splitter_output["documents"])
                     task_log.warning(f"Inferred chunk count from splitter: {processed_chunk_count}")
                     pipeline_run_successful = True # Considerar éxito si hubo chunks
                else:
                     processed_chunk_count = 0
                     pipeline_run_successful = False # Marcar como fallo si no se pudo procesar/escribir nada
                     task_log.error("Pipeline execution seems to have failed, no documents processed/written.")
                     # Levantar una excepción para marcar como error en DB
                     raise RuntimeError("Haystack pipeline failed to process or write any documents.")

            # 7. Actualizar Estado Final en DB (async)
            final_status = DocumentStatus.PROCESSED # O INDEXED
            task_log.info(f"Updating document status to {final_status.value} with {processed_chunk_count} chunks.")
            await postgres_client.update_document_status(
                document_id,
                final_status,
                chunk_count=processed_chunk_count,
                error_message=None # Limpiar cualquier error previo
            )
            task_log.info("Document status updated successfully in PostgreSQL.")

        except NON_RETRYABLE_ERRORS as e_non_retry:
            # Errores de datos, lógica, archivo no encontrado, tipo no soportado, etc.
            err_msg = f"Non-retryable task error: {type(e_non_retry).__name__}: {str(e_non_retry)[:500]}"
            task_log.error(f"Processing failed permanently: {err_msg}", exc_info=True)
            try:
                await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=err_msg)
                task_log.info("Document status set to ERROR due to non-retryable failure.")
            except Exception as db_err:
                task_log.critical("Failed to update document status to ERROR after non-retryable failure!", db_error=str(db_err), original_error=err_msg, exc_info=True)
            # No relanzar e_non_retry para que Celery no reintente
            # La tarea se marcará como SUCCESS en Celery, pero la DB indicará ERROR.
            # Si quieres que Celery marque FAILED, elimina el try/except y deja que la excepción se propague,
            # asegurándote que NON_RETRYABLE_ERRORS no estén en autoretry_for.

        except RETRYABLE_ERRORS as e_retry:
            # Errores de red, IO, timeout, DB temporal, S3 temporal, etc.
            err_msg = f"Retryable task error: {type(e_retry).__name__}: {str(e_retry)[:500]}"
            task_log.warning(f"Processing failed, will retry if possible: {err_msg}", exc_info=True)
            try:
                # Marcar error temporalmente en DB (puede ser sobrescrito en reintento exitoso)
                await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=f"Task Error (Retry Attempt {self.request.retries + 1}): {err_msg}")
            except Exception as db_err:
                 task_log.error("Failed to update document status to ERROR during retryable failure!", db_error=str(db_err), original_error=err_msg, exc_info=True)
            # Relanzar la excepción original para que Celery la capture y aplique la lógica de reintento
            raise e_retry

        finally:
            # Limpieza final
            if downloaded_file_stream:
                downloaded_file_stream.close()
            task_log.debug("Cleaned up task resources.")

    # --- Ejecutar el flujo async dentro de la tarea Celery síncrona ---
    try:
        asyncio.run(async_process_flow())
        task_log.info("Haystack document processing task completed.")
    except Exception as top_level_exc:
        # Esta excepción sólo debería ocurrir si async_process_flow relanza una excepción
        # (normalmente una RETRYABLE_ERROR para que Celery la maneje).
        # Las NON_RETRYABLE_ERRORS se capturan dentro de async_process_flow y no se relanzan.
        task_log.exception("Haystack processing task failed at top level (likely pending retry or unexpected issue).")
        # No necesitamos hacer nada más aquí, Celery manejará el reintento o marcará como FAILED
        # si la excepción relanzada coincide con autoretry_for o si se agotan los reintentos.
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
