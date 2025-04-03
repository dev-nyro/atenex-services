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
    "/ingest/upload", # Ruta relativa al prefijo /api/v1/ingest añadido en main.py
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
    "/ingest/status/{document_id}", # Ruta relativa correcta
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
    "/ingest/status", # Ruta relativa correcta
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
import uuid
from pydantic import BaseModel, Field, Json # Json no es necesario aquí ahora
from typing import Optional, Dict, Any, List
from app.models.domain import DocumentStatus
from datetime import datetime

# Pydantic schema for metadata validation (optional but recommended)
# class DocumentMetadata(BaseModel):
#     category: Optional[str] = None
#     author: Optional[str] = None
#     # Add other expected metadata fields

class IngestRequest(BaseModel):
    # company_id vendrá de la dependencia/header, no de este modelo
    # metadata ahora se maneja como Form(metadata_json) en el endpoint
    pass # Este modelo ya no es estrictamente necesario para el endpoint actual

class IngestResponse(BaseModel):
    document_id: uuid.UUID
    task_id: Optional[str] = None # Devolver ID de tarea Celery para seguimiento
    status: DocumentStatus = DocumentStatus.UPLOADED # Estado inicial devuelto
    message: str = "Document upload received and queued for processing."

# Schema para la respuesta de estado (usado para GET individual y lista)
class StatusResponse(BaseModel):
    document_id: uuid.UUID = Field(..., alias="id") # Mapear 'id' de la DB a 'document_id'
    status: DocumentStatus
    file_name: Optional[str] = None
    file_type: Optional[str] = None
    chunk_count: Optional[int] = None
    error_message: Optional[str] = None
    last_updated: Optional[datetime] = Field(None, alias="updated_at") # Mapear 'updated_at' de la DB
    message: Optional[str] = None # Mensaje descriptivo (añadido en el endpoint)

    # Pydantic v2: Configuración para permitir alias y populación desde atributos
    model_config = {
        "populate_by_name": True, # Permite usar alias para mapear nombres de campos de DB
        "from_attributes": True # Necesario si se crean instancias desde objetos con atributos (como asyncpg.Record)
    }

# (Opcional) Si prefieres un modelo específico para la lista sin el campo 'message'
# class StatusListItemResponse(BaseModel):
#     document_id: uuid.UUID = Field(..., alias="id")
#     status: DocumentStatus
#     file_name: Optional[str] = None
#     file_type: Optional[str] = None
#     chunk_count: Optional[int] = None
#     error_message: Optional[str] = None
#     last_updated: Optional[datetime] = Field(None, alias="updated_at")
#
#     model_config = {
#         "populate_by_name": True,
#         "from_attributes": True
#     }
# En ese caso, el endpoint de lista usaría response_model=List[StatusListItemResponse]
# Pero usar StatusResponse para ambos es más simple para MVP.
```

## File: `app\core\__init__.py`
```py

```

## File: `app\core\config.py`
```py
# ./app/core/config.py (CORREGIDO - Defaults para Session Pooler)
import logging
import os
from typing import Optional, List, Any, Dict
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import RedisDsn, AnyHttpUrl, SecretStr, Field, validator, ValidationError, HttpUrl
import sys

# --- Supabase Connection Defaults (Usando Session Pooler por defecto) ---
# *** CORREGIDO: Defaults actualizados a Session Pooler y puerto correcto ***
SUPABASE_SESSION_POOLER_HOST = "aws-0-sa-east-1.pooler.supabase.com"
SUPABASE_SESSION_POOLER_PORT_INT = 6543 # Puerto estándar del Session Pooler
SUPABASE_SESSION_POOLER_USER = "postgres.ymsilkrhstwxikjiqqog" # Cambiar ymsilkrhstwxikjiqqog si tu project-ref es diferente
SUPABASE_DEFAULT_DB = "postgres"

# --- Milvus Kubernetes Defaults ---
MILVUS_K8S_DEFAULT_URI = "http://milvus-service.nyro-develop.svc.cluster.local:19530"

# --- Redis Kubernetes Defaults ---
REDIS_K8S_DEFAULT_HOST = "redis-service-master.nyro-develop.svc.cluster.local"
REDIS_K8S_DEFAULT_PORT = 6379

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='INGEST_',
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = "Ingest Service (Haystack/K8s/Supabase/SessionPooler)"
    API_V1_STR: str = "/api/v1"
    LOG_LEVEL: str = "INFO"

    # --- Celery ---
    CELERY_BROKER_URL: RedisDsn = RedisDsn(f"redis://{REDIS_K8S_DEFAULT_HOST}:{REDIS_K8S_DEFAULT_PORT}/0")
    CELERY_RESULT_BACKEND: RedisDsn = RedisDsn(f"redis://{REDIS_K8S_DEFAULT_HOST}:{REDIS_K8S_DEFAULT_PORT}/1")

    # --- Database (Supabase Session Pooler Settings) ---
    # *** CORREGIDO: Defaults cambiados a Session Pooler con puerto 6543 ***
    POSTGRES_USER: str = SUPABASE_SESSION_POOLER_USER
    POSTGRES_PASSWORD: SecretStr # Obligatorio desde Secrets
    POSTGRES_SERVER: str = SUPABASE_SESSION_POOLER_HOST
    POSTGRES_PORT: int = SUPABASE_SESSION_POOLER_PORT_INT # Usará 6543 por defecto
    POSTGRES_DB: str = SUPABASE_DEFAULT_DB

    # --- Milvus ---
    MILVUS_URI: AnyHttpUrl = AnyHttpUrl(MILVUS_K8S_DEFAULT_URI)
    MILVUS_COLLECTION_NAME: str = "document_chunks_haystack"
    MILVUS_INDEX_PARAMS: Dict[str, Any] = Field(default={
        "metric_type": "COSINE", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 256}
    })
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default={
        "metric_type": "COSINE", "params": {"ef": 128}
    })
    MILVUS_CONTENT_FIELD: str = "content"
    MILVUS_EMBEDDING_FIELD: str = "embedding"
    MILVUS_METADATA_FIELDS: List[str] = Field(default=[
        "company_id", "document_id", "file_name", "file_type",
    ])

    # --- MinIO Storage ---
    MINIO_ENDPOINT: str = "minio-service.nyro-develop.svc.cluster.local:9000"
    MINIO_ACCESS_KEY: SecretStr # Obligatorio desde Secrets
    MINIO_SECRET_KEY: SecretStr # Obligatorio desde Secrets
    MINIO_BUCKET_NAME: str = "ingested-documents"
    MINIO_USE_SECURE: bool = False

    # --- External Services ---
    OCR_SERVICE_URL: Optional[AnyHttpUrl] = None

    # --- Service Client Config ---
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 2
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    # --- File Processing & Haystack ---
    SUPPORTED_CONTENT_TYPES: List[str] = Field(default=[
        "application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain", "text/markdown", "text/html", "image/jpeg", "image/png",
    ])
    EXTERNAL_OCR_REQUIRED_CONTENT_TYPES: List[str] = Field(default=["image/jpeg", "image/png"])
    SPLITTER_CHUNK_SIZE: int = 500
    SPLITTER_CHUNK_OVERLAP: int = 50
    SPLITTER_SPLIT_BY: str = "word"

    # --- OpenAI ---
    OPENAI_API_KEY: SecretStr # Obligatorio desde Secrets
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMENSION: int = 1536 # Default, ajustado por validador

    # --- Validators ---
    @validator("EMBEDDING_DIMENSION", pre=True, always=True)
    def set_embedding_dimension(cls, v: Optional[int], values: dict[str, Any]) -> int:
        model = values.get("OPENAI_EMBEDDING_MODEL")
        # Ajusta la dimensión según el modelo especificado
        if model == "text-embedding-3-large": return 3072
        elif model in ["text-embedding-3-small", "text-embedding-ada-002"]: return 1536
        # Si no se especifica o es 0, intenta deducir del modelo o usa default
        if v is None or v == 0:
            if model:
                 if model == "text-embedding-3-large": return 3072
                 if model in ["text-embedding-3-small", "text-embedding-ada-002"]: return 1536
            return 1536 # Default general si no se puede determinar
        return v # Devuelve el valor si se proporcionó explícitamente

# --- Instancia Global ---
try:
    settings = Settings()
    # *** CORREGIDO: Mensajes de debug para reflejar la configuración real ***
    print("DEBUG: Settings loaded successfully.")
    print(f"DEBUG: Using Postgres Server: {settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}") # Reflejará el puerto 6543 si usa default
    print(f"DEBUG: Using Postgres User: {settings.POSTGRES_USER}")
    print(f"DEBUG: Using Milvus URI: {settings.MILVUS_URI}")
    print(f"DEBUG: Using Redis Broker: {settings.CELERY_BROKER_URL}")
    print(f"DEBUG: Using Minio Endpoint: {settings.MINIO_ENDPOINT}")

except (ValidationError, ValueError) as e:
    error_details = ""
    if isinstance(e, ValidationError):
        try: error_details = f"\nValidation Errors:\n{e.json(indent=2)}"
        except Exception:
             try: error_details = f"\nRaw Errors: {e.errors()}"
             except Exception: error_details = f"\nError details unavailable: {e}"
    print(f"FATAL: Configuration validation failed:{error_details}\nOriginal Error: {e}")
    sys.exit(1)
except Exception as e:
    print(f"FATAL: Unexpected error during Settings instantiation:\n{e}")
    import traceback; traceback.print_exc()
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
# ./app/db/postgres_client.py (AÑADIDA función list_documents_by_company)
import uuid
from typing import Any, Optional, Dict, List
import asyncpg
import structlog
import json

from app.core.config import settings
from app.models.domain import DocumentStatus

log = structlog.get_logger(__name__)

_pool: Optional[asyncpg.Pool] = None

async def get_db_pool() -> asyncpg.Pool:
    """
    Obtiene o crea el pool de conexiones a la base de datos (Supabase).
    Deshabilita la caché de prepared statements (statement_cache_size=0)
    para compatibilidad con PgBouncer en modo transaction/statement (Supabase Pooler).
    """
    global _pool
    if _pool is None or _pool._closed:
        try:
            log.info("Creating Supabase/PostgreSQL connection pool using arguments...",
                     host=settings.POSTGRES_SERVER,
                     port=settings.POSTGRES_PORT,
                     user=settings.POSTGRES_USER,
                     database=settings.POSTGRES_DB)

            _pool = await asyncpg.create_pool(
                user=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD.get_secret_value(),
                database=settings.POSTGRES_DB,
                host=settings.POSTGRES_SERVER,
                port=settings.POSTGRES_PORT,
                min_size=5,
                max_size=20,
                # *** CORREGIDO: Deshabilitar caché de prepared statements ***
                # Necesario para compatibilidad con PgBouncer en modo 'transaction' o 'statement'
                # (como el Session Pooler de Supabase) que no soporta prepared statements a nivel de sesión.
                statement_cache_size=0,
                # command_timeout=60, # Timeout para comandos individuales
                # timeout=300, # Timeout general? Revisar docs de asyncpg
                init=lambda conn: conn.set_type_codec(
                    'jsonb',
                    encoder=json.dumps,
                    decoder=json.loads,
                    schema='pg_catalog',
                    format='text'
                )
                # Podrías considerar añadir el codec de UUID aquí también si lo usas frecuentemente
                # init=setup_connection_codecs # Ver ejemplo abajo si es necesario
            )
            log.info("Supabase/PostgreSQL connection pool created successfully (statement_cache_size=0).")
        except OSError as e:
             log.error("Network/OS error creating Supabase/PostgreSQL connection pool",
                      error=str(e), errno=getattr(e, 'errno', None),
                      host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT,
                      db=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
                      exc_info=True)
             raise
        except asyncpg.exceptions.InvalidPasswordError:
             log.error("Invalid password for Supabase/PostgreSQL connection",
                       host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT, user=settings.POSTGRES_USER)
             raise
        # Capturar específicamente el error de prepared statement duplicado
        except asyncpg.exceptions.DuplicatePreparedStatementError as e:
            log.error("Failed to create Supabase/PostgreSQL connection pool due to DuplicatePreparedStatementError "
                      "(Confirm statement_cache_size=0 is set correctly for PgBouncer/Pooler)",
                      error=str(e), error_type=type(e).__name__,
                      host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT,
                      db=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
                      exc_info=True) # Incluir traceback en este caso es útil
            raise # Re-lanzar para que falle el startup
        except Exception as e: # Otros errores (incluyendo TimeoutError si volviera a ocurrir)
            log.error("Failed to create Supabase/PostgreSQL connection pool",
                      error=str(e), error_type=type(e).__name__,
                      host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT,
                      db=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
                      exc_info=True)
            raise
    return _pool

# Ejemplo de función init más compleja si necesitas más codecs (opcional)
# async def setup_connection_codecs(connection):
#     await connection.set_type_codec(
#         'jsonb',
#         encoder=json.dumps,
#         decoder=json.loads,
#         schema='pg_catalog',
#         format='text'
#     )
#     # Añadir codec para UUID si no está por defecto o quieres asegurar el manejo
#     await connection.set_type_codec(
#         'uuid',
#         encoder=str,
#         decoder=uuid.UUID,
#         schema='pg_catalog',
#         format='text'
#     )
#     log.debug("Custom type codecs (jsonb, uuid) registered for new connection.", connection=connection)


async def close_db_pool():
    """Cierra el pool de conexiones."""
    global _pool
    if _pool and not _pool._closed:
        log.info("Closing Supabase/PostgreSQL connection pool...")
        await _pool.close()
        _pool = None
        log.info("Supabase/PostgreSQL connection pool closed.")

async def create_document(
    company_id: uuid.UUID,
    file_name: str,
    file_type: str,
    metadata: Dict[str, Any]
) -> uuid.UUID:
    """Crea un registro inicial para el documento en la tabla DOCUMENTS."""
    pool = await get_db_pool()
    doc_id = uuid.uuid4()
    query = """
        INSERT INTO documents (id, company_id, file_name, file_type, metadata, status, file_path, uploaded_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, '', NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC')
        RETURNING id;
    """
    try:
        async with pool.acquire() as connection:
            # Con statement_cache_size=0, asyncpg no usará prepared statements internamente aquí
            result = await connection.fetchval(
                query, doc_id, company_id, file_name, file_type, json.dumps(metadata), DocumentStatus.UPLOADED.value # Asegurar que metadata se guarda como JSON
            )
        if result:
            log.info("Document record created in Supabase", document_id=doc_id, company_id=company_id)
            return result
        else:
             log.error("Failed to create document record, no ID returned.", document_id=doc_id)
             raise RuntimeError("Failed to create document record, no ID returned.")
    except asyncpg.exceptions.UniqueViolationError as e:
        log.error("Failed to create document record due to unique constraint violation.", error=str(e), document_id=doc_id, company_id=company_id, constraint=e.constraint_name, exc_info=False)
        raise ValueError(f"Document creation failed: unique constraint violated ({e.constraint_name})") from e
    except Exception as e:
        log.error("Failed to create document record in Supabase", error=str(e), document_id=doc_id, company_id=company_id, file_name=file_name, exc_info=True)
        raise

async def update_document_status(
    document_id: uuid.UUID,
    status: DocumentStatus,
    file_path: Optional[str] = None,
    chunk_count: Optional[int] = None,
    error_message: Optional[str] = None,
) -> bool:
    """Actualiza el estado y otros campos de un documento en la tabla DOCUMENTS."""
    pool = await get_db_pool()
    fields_to_update = ["status = $2", "updated_at = NOW() AT TIME ZONE 'UTC'"]
    params: List[Any] = [document_id, status.value]
    current_param_index = 3
    if file_path is not None:
        fields_to_update.append(f"file_path = ${current_param_index}")
        params.append(file_path)
        current_param_index += 1
    if chunk_count is not None:
        fields_to_update.append(f"chunk_count = ${current_param_index}")
        params.append(chunk_count)
        current_param_index += 1
    if status == DocumentStatus.ERROR:
        safe_error_message = (error_message or "Unknown processing error")[:1000] # Limitar longitud
        fields_to_update.append(f"error_message = ${current_param_index}")
        params.append(safe_error_message)
        current_param_index += 1
    else:
        # Limpiar error_message si el estado no es ERROR
        fields_to_update.append("error_message = NULL")

    query = f"UPDATE documents SET {', '.join(fields_to_update)} WHERE id = $1;"
    try:
        async with pool.acquire() as connection:
             # Con statement_cache_size=0, asyncpg no usará prepared statements internamente aquí
             result = await connection.execute(query, *params)
        affected_rows = 0
        if isinstance(result, str) and result.startswith("UPDATE "):
            try: affected_rows = int(result.split(" ")[1])
            except (IndexError, ValueError): log.warning("Could not parse affected rows from DB result", result_string=result)
        success = affected_rows > 0
        if success: log.info("Document status updated in Supabase", document_id=document_id, new_status=status.value, file_path=file_path, chunk_count=chunk_count, has_error=(status == DocumentStatus.ERROR))
        else: log.warning("Document status update did not affect any rows", document_id=document_id, new_status=status.value)
        return success
    except Exception as e:
        log.error("Failed to update document status in Supabase", error=str(e), document_id=document_id, new_status=status.value, exc_info=True)
        raise

async def get_document_status(document_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """Obtiene el estado y otros datos de un documento de la tabla DOCUMENTS."""
    pool = await get_db_pool()
    # Asegurar que seleccionamos las columnas correctas para StatusResponse
    query = """
        SELECT id, status, file_name, file_type, chunk_count, error_message, updated_at, company_id
        FROM documents
        WHERE id = $1;
    """
    try:
        async with pool.acquire() as connection:
            # Con statement_cache_size=0, asyncpg no usará prepared statements internamente aquí
            record = await connection.fetchrow(query, document_id)
        if record:
            log.debug("Document status retrieved from Supabase", document_id=document_id)
            # Convertir asyncpg.Record a Dict para consistencia
            return dict(record)
        else:
            log.warning("Document status requested for non-existent ID", document_id=document_id)
            return None
    except Exception as e:
        log.error("Failed to get document status from Supabase", error=str(e), document_id=document_id, exc_info=True)
        raise

# --- NUEVA FUNCIÓN ---
async def list_documents_by_company(company_id: uuid.UUID) -> List[Dict[str, Any]]:
    """
    Obtiene una lista de documentos (con su estado) para una compañía específica,
    ordenados por fecha de actualización descendente.
    """
    pool = await get_db_pool()
    # Seleccionar las columnas necesarias para el schema StatusResponse
    query = """
        SELECT id, status, file_name, file_type, chunk_count, error_message, updated_at
        FROM documents
        WHERE company_id = $1
        ORDER BY updated_at DESC;
    """
    db_log = log.bind(company_id=str(company_id))
    try:
        async with pool.acquire() as connection:
            # Con statement_cache_size=0, asyncpg no usará prepared statements internamente aquí
            records = await connection.fetch(query, company_id)

        result_list = [dict(record) for record in records] # Convertir lista de Records a lista de Dicts
        db_log.info(f"Retrieved {len(result_list)} documents for company")
        return result_list
    except Exception as e:
        db_log.error("Failed to list documents by company from Supabase", error=str(e), exc_info=True)
        raise # Relanzar para que el endpoint maneje el error
```

## File: `app\main.py`
```py
# ingest-service/app/main.py
from fastapi import FastAPI, HTTPException, status as fastapi_status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response # Importar Response
import structlog
import uvicorn
import logging
import sys
import asyncio

# Configurar logging primero
from app.core.config import settings
from app.core.logging_config import setup_logging
setup_logging()
log = structlog.get_logger(__name__)

# Importar routers y otros módulos
from app.api.v1.endpoints import ingest # Importar el router directamente
from app.db import postgres_client

SERVICE_READY = False

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.1.0",
    description="Microservice for document ingestion and preprocessing using Haystack.",
)

# --- Event Handlers (Sin cambios) ---
@app.on_event("startup")
async def startup_event():
    global SERVICE_READY
    log.info("Starting up Ingest Service...")
    db_pool_initialized = False
    try:
        await postgres_client.get_db_pool()
        pool = await postgres_client.get_db_pool()
        async with pool.acquire() as conn:
            await asyncio.wait_for(conn.execute("SELECT 1"), timeout=10.0)
        log.info("PostgreSQL connection pool initialized and verified.")
        db_pool_initialized = True
    except Exception as e:
        log.critical("CRITICAL: Failed PostgreSQL connection on startup.", error=str(e), exc_info=True)
    if db_pool_initialized:
        SERVICE_READY = True
        log.info("Ingest Service startup sequence completed. READY.")
    else:
        SERVICE_READY = False
        log.warning("Ingest Service startup completed but DB connection failed. NOT READY.")

@app.on_event("shutdown")
async def shutdown_event():
    log.info("Shutting down Ingest Service..."); await postgres_client.close_db_pool(); log.info("Shutdown complete.")

# --- Exception Handlers (Sin cambios) ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc): log.warning("HTTP Exception", s=exc.status_code, d=exc.detail); return JSONResponse(s=exc.status_code, c={"detail": exc.detail})
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc): log.warning("Validation Error", e=exc.errors()); return JSONResponse(s=422, c={"detail": "Validation Error", "errors": exc.errors()})
@app.exception_handler(Exception)
async def generic_exception_handler(request, exc): log.exception("Unhandled Exception"); return JSONResponse(s=500, c={"detail": "Internal Server Error"})

# --- Router ---
# *** CORRECCIÓN: Usar el prefijo correcto ***
app.include_router(ingest.router, prefix=settings.API_V1_STR, tags=["Ingestion"])
# Asegúrate que el prefijo aquí + la ruta en el endpoint coincidan con lo que llama el gateway
# Gateway llama a /api/v1/ingest/status -> Debe mapear a GET /status aquí
# Gateway llama a /api/v1/ingest/status/{id} -> Debe mapear a GET /status/{id} aquí
# Gateway llama a /api/v1/ingest/upload -> Debe mapear a POST /ingest aquí (porque el endpoint usa "/ingest")
# El prefijo parece correcto si las rutas en ingest.py son "/ingest", "/status", "/status/{id}"

# --- Root Endpoint / Health Check (Sin cambios) ---
@app.get("/", tags=["Health Check"], status_code=fastapi_status.HTTP_200_OK)
async def read_root():
    global SERVICE_READY; health_log = log.bind(check="liveness/readiness")
    if not SERVICE_READY: health_log.warning("Health check failed: Not Ready"); raise HTTPException(status_code=503, detail="Service not ready")
    try:
        pool = await postgres_client.get_db_pool()
        async with pool.acquire() as conn:
            await asyncio.wait_for(conn.execute("SELECT 1"), timeout=5.0)
        health_log.debug("Health check: DB ping successful.")
    except Exception as db_ping_err: health_log.error("Health check failed: DB ping error", e=db_ping_err); SERVICE_READY = False; raise HTTPException(status_code=503, detail="DB connection error")
    return Response(content="OK", status_code=fastapi_status.HTTP_200_OK, media_type="text/plain") # Cambiado a  respuesta simple OK
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
# ./app/services/minio_client.py (CORREGIDO - run_in_executor para llamadas sync)
import io
import uuid
from typing import IO, BinaryIO # Usar BinaryIO para type hint
from minio import Minio
from minio.error import S3Error
import structlog
import asyncio # Import asyncio para run_in_executor

from app.core.config import settings

log = structlog.get_logger(__name__)

class MinioStorageClient:
    """Cliente para interactuar con MinIO."""

    def __init__(self):
        # La inicialización sigue siendo síncrona
        try:
            self.client = Minio(
                settings.MINIO_ENDPOINT,
                access_key=settings.MINIO_ACCESS_KEY.get_secret_value(),
                secret_key=settings.MINIO_SECRET_KEY.get_secret_value(),
                secure=settings.MINIO_USE_SECURE
            )
            self._ensure_bucket_exists()
            log.info("MinIO client initialized", endpoint=settings.MINIO_ENDPOINT, bucket=settings.MINIO_BUCKET_NAME)
        except Exception as e:
            log.error("Failed to initialize MinIO client", error=str(e), exc_info=True)
            raise

    def _ensure_bucket_exists(self):
        """Crea el bucket si no existe (síncrono)."""
        try:
            found = self.client.bucket_exists(settings.MINIO_BUCKET_NAME)
            if not found:
                self.client.make_bucket(settings.MINIO_BUCKET_NAME)
                log.info(f"MinIO bucket '{settings.MINIO_BUCKET_NAME}' created.")
            else:
                log.debug(f"MinIO bucket '{settings.MINIO_BUCKET_NAME}' already exists.")
        except S3Error as e:
            log.error(f"Error checking/creating MinIO bucket '{settings.MINIO_BUCKET_NAME}'", error=str(e), exc_info=True)
            raise

    # *** CORREGIDO: Usar run_in_executor para la llamada síncrona put_object ***
    async def upload_file(
        self,
        company_id: uuid.UUID,
        document_id: uuid.UUID,
        file_name: str,
        file_content_stream: IO[bytes], # Acepta cualquier stream de bytes
        content_type: str,
        content_length: int
    ) -> str:
        """
        Sube un archivo a MinIO de forma asíncrona (ejecutando la operación síncrona en un executor).
        Retorna el nombre del objeto en MinIO (object_name).
        """
        object_name = f"{str(company_id)}/{str(document_id)}/{file_name}"
        upload_log = log.bind(bucket=settings.MINIO_BUCKET_NAME, object_name=object_name, content_type=content_type, length=content_length)
        upload_log.info("Queueing file upload to MinIO executor...")

        loop = asyncio.get_running_loop()
        try:
            # Ejecutar la operación síncrona de MinIO en un executor
            # Asegurarse que el stream está al inicio antes de pasarlo al thread
            file_content_stream.seek(0)
            result = await loop.run_in_executor(
                None, # Usa el ThreadPoolExecutor por defecto
                lambda: self.client.put_object(
                    settings.MINIO_BUCKET_NAME,
                    object_name,
                    file_content_stream, # Pasar el stream directamente
                    length=content_length,
                    content_type=content_type,
                )
            )
            upload_log.info("File uploaded successfully to MinIO via executor", etag=result.etag, version_id=result.version_id)
            return object_name
        except S3Error as e:
            upload_log.error("Failed to upload file to MinIO via executor", error=str(e), code=e.code, exc_info=True)
            raise # Re-raise the specific S3Error
        except Exception as e:
            upload_log.error("Unexpected error during file upload via executor", error=str(e), exc_info=True)
            raise # Re-raise generic exceptions


    # *** CORREGIDO: Crear función síncrona para la lógica de descarga ***
    def download_file_stream_sync(
        self,
        object_name: str
    ) -> io.BytesIO:
        """
        Descarga un archivo de MinIO como un stream en memoria (BytesIO).
        Esta es una operación SÍNCRONA. Lanza FileNotFoundError si no existe.
        """
        download_log = log.bind(bucket=settings.MINIO_BUCKET_NAME, object_name=object_name)
        download_log.info("Downloading file from MinIO (sync)...")
        response = None
        try:
            # Operación bloqueante de red/IO
            response = self.client.get_object(settings.MINIO_BUCKET_NAME, object_name)
            file_data = response.read() # Leer todo el contenido (bloqueante)
            file_stream = io.BytesIO(file_data)
            download_log.info(f"File downloaded successfully from MinIO (sync, {len(file_data)} bytes)")
            file_stream.seek(0) # Reset stream position
            return file_stream
        except S3Error as e:
            download_log.error("Failed to download file from MinIO (sync)", error=str(e), code=e.code, exc_info=False) # No need for full trace on known errors like NoSuchKey
            # Es importante lanzar una excepción clara si el archivo no se encuentra
            if e.code == 'NoSuchKey':
                 raise FileNotFoundError(f"Object not found in MinIO: {object_name}") from e
            else:
                 # Otro error de S3
                 raise IOError(f"S3 error downloading file {object_name}: {e.code}") from e
        except Exception as e:
             # Capturar otros posibles errores
             download_log.error("Unexpected error during sync file download", error=str(e), exc_info=True)
             raise IOError(f"Unexpected error downloading file {object_name}") from e
        finally:
            # Asegurar que la conexión se libera siempre
            if response:
                response.close()
                response.release_conn()

    # *** CORREGIDO: La versión async ahora llama a la sync en el executor ***
    async def download_file_stream(
        self,
        object_name: str
    ) -> io.BytesIO:
        """
        Descarga un archivo de MinIO como un stream en memoria (BytesIO) de forma asíncrona.
        Ejecuta la descarga síncrona en un executor. Lanza FileNotFoundError si no existe.
        """
        download_log = log.bind(bucket=settings.MINIO_BUCKET_NAME, object_name=object_name)
        download_log.info("Queueing file download from MinIO executor...")
        loop = asyncio.get_running_loop()
        try:
            file_stream = await loop.run_in_executor(
                None, # Usa el ThreadPoolExecutor por defecto
                self.download_file_stream_sync, # Llama a la función síncrona
                object_name
            )
            download_log.info("File download successful via executor")
            return file_stream
        except FileNotFoundError: # Capturar el error específico de archivo no encontrado
            download_log.error("File not found in MinIO via executor", object_name=object_name)
            raise # Relanzar FileNotFoundError para que la tarea Celery lo maneje
        except Exception as e: # Captura IOError u otros errores del sync helper
            download_log.error("Error downloading file via executor", error=str(e), error_type=type(e).__name__, exc_info=True)
            raise # Relanzar otras excepciones
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
# ./app/tasks/process_document.py (CORREGIDO - Llamada a MinIO/Haystack en executor y manejo async)
import uuid
import asyncio
from typing import Dict, Any, Optional, List, Type
import tempfile
import os
from pathlib import Path
import structlog
import base64
import io
import time # Para medir tiempos si es necesario

# --- Haystack Imports ---
from haystack import Pipeline, Document
from haystack.utils import Secret
from haystack.components.converters import (
    PyPDFToDocument,
    TextFileToDocument,
    MarkdownToDocument,
    HTMLToDocument,
    DOCXToDocument,
)
from haystack.components.preprocessors import DocumentSplitter
from haystack.components.embedders import OpenAIDocumentEmbedder
from milvus_haystack import MilvusDocumentStore # Asegúrate que esté importado
from haystack.components.writers import DocumentWriter
from haystack.dataclasses import ByteStream

# --- Local Imports ---
from app.tasks.celery_app import celery_app
from app.core.config import settings
from app.db import postgres_client # Importar funciones async del cliente DB
from app.models.domain import DocumentStatus
from app.services.minio_client import MinioStorageClient # Importar cliente MinIO corregido

log = structlog.get_logger(__name__)

# --- Funciones de inicialización de Haystack (sin cambios necesarios) ---
# (Se asume que estas funciones son síncronas y seguras para llamarse desde el executor o antes)
def get_haystack_document_store() -> MilvusDocumentStore:
    """Initializes the MilvusDocumentStore."""
    log.debug("Initializing MilvusDocumentStore",
             uri=str(settings.MILVUS_URI),
             collection=settings.MILVUS_COLLECTION_NAME,
             dim=settings.EMBEDDING_DIMENSION,
             metadata_fields=settings.MILVUS_METADATA_FIELDS)
    # Asegúrate que los parámetros coinciden con tu versión de Milvus y Haystack
    return MilvusDocumentStore(
        uri=str(settings.MILVUS_URI),
        collection_name=settings.MILVUS_COLLECTION_NAME,
        dim=settings.EMBEDDING_DIMENSION,
        embedding_field=settings.MILVUS_EMBEDDING_FIELD,
        content_field=settings.MILVUS_CONTENT_FIELD,
        metadata_fields=settings.MILVUS_METADATA_FIELDS,
        index_params=settings.MILVUS_INDEX_PARAMS,
        search_params=settings.MILVUS_SEARCH_PARAMS,
        consistency_level="Strong", # O el nivel que necesites
    )

def get_haystack_embedder() -> OpenAIDocumentEmbedder:
    """Initializes the OpenAI Embedder for documents."""
    api_key_env_var = "INGEST_OPENAI_API_KEY" # La variable de entorno real según tu config
    api_key = settings.OPENAI_API_KEY.get_secret_value()
    if not api_key:
         log.warning(f"OpenAI API Key not found in settings. Haystack embedding might fail.")
         # Considerar lanzar un error si la clave es esencial
         # raise ValueError("OpenAI API Key is missing in configuration")
    return OpenAIDocumentEmbedder(
        # Usar Secret.from_env_var si la clave viene de env var, sino from_token
        api_key=Secret.from_env_var(api_key_env_var) if os.getenv(api_key_env_var) else Secret.from_token(api_key),
        model=settings.OPENAI_EMBEDDING_MODEL,
        meta_fields_to_embed=[] # Ajusta si necesitas embeber metadatos
    )

def get_haystack_splitter() -> DocumentSplitter:
    """Initializes the DocumentSplitter."""
    return DocumentSplitter(
        split_by=settings.SPLITTER_SPLIT_BY,
        split_length=settings.SPLITTER_CHUNK_SIZE,
        split_overlap=settings.SPLITTER_CHUNK_OVERLAP
    )

def get_converter_for_content_type(content_type: str) -> Optional[Type]:
     """Returns the appropriate Haystack Converter class."""
     if content_type == "application/pdf": return PyPDFToDocument
     elif content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document": return DOCXToDocument
     elif content_type == "text/plain": return TextFileToDocument
     elif content_type == "text/markdown": return MarkdownToDocument
     elif content_type == "text/html": return HTMLToDocument
     # Añadir más conversores si son necesarios
     else:
         log.warning("No specific Haystack converter found for content type", content_type=content_type)
         return None


# --- Celery Task ---
@celery_app.task(
    bind=True,
    # *** CORREGIDO: Reintentar solo en excepciones recuperables, NO en FileNotFoundError o ValueError ***
    autoretry_for=(IOError, ConnectionError, TimeoutError, Exception), # Excepciones genéricas/red/IO
    retry_kwargs={'max_retries': 2, 'countdown': 60},
    # No reintentar en errores de lógica/datos como:
    # FileNotFoundError (archivo no existe)
    # ValueError (tipo de contenido no soportado, metadata inválida)
    # NotImplementedError (OCR no implementado)
    reject_on_worker_lost=True, # Re-encolar si el worker muere
    acks_late=True, # Reconoce el mensaje solo después de completar o fallar definitivamente
    name="tasks.process_document_haystack"
)
def process_document_haystack_task(
    self, # Instancia de la tarea (proporcionada por bind=True)
    document_id_str: str,
    company_id_str: str,
    minio_object_name: str,
    file_name: str,
    content_type: str,
    original_metadata: Dict[str, Any],
):
    """
    Procesa un documento usando un pipeline Haystack (MinIO -> Haystack -> Milvus -> Supabase Status).
    Utiliza asyncio.run para manejar operaciones async y run_in_executor para operaciones bloqueantes.
    """
    document_id = uuid.UUID(document_id_str)
    company_id = uuid.UUID(company_id_str)
    task_log = log.bind(document_id=str(document_id), company_id=str(company_id),
                      task_id=self.request.id, file_name=file_name, object_name=minio_object_name, content_type=content_type)
    task_log.info("Starting Haystack document processing task")

    # *** CORREGIDO: Usar una función async interna para la lógica principal ***
    async def async_process():
        haystack_pipeline = Pipeline()
        processed_docs_count = 0
        # Crear instancia del cliente MinIO aquí dentro
        minio_client = MinioStorageClient()
        downloaded_file_stream: Optional[io.BytesIO] = None
        document_store: Optional[MilvusDocumentStore] = None

        try:
            # 0. Marcar como procesando en Supabase (usando await)
            await postgres_client.update_document_status(document_id, DocumentStatus.PROCESSING)
            task_log.info("Document status set to PROCESSING")

            # 1. Descargar archivo de MinIO (usando await en el wrapper async de Minio)
            task_log.info("Downloading file from MinIO via async wrapper...")
            try:
                # *** CORREGIDO: Llamar al método async download_file_stream que usa executor internamente ***
                downloaded_file_stream = await minio_client.download_file_stream(minio_object_name)
            except FileNotFoundError as fnf_err:
                 # Si el archivo no existe, no tiene sentido reintentar. Marcar como error y salir.
                 task_log.error("File not found in MinIO storage. Cannot process.", object_name=minio_object_name, error=str(fnf_err))
                 await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message="File not found in storage")
                 # No relanzamos la excepción aquí para que Celery NO intente reintentar por FileNotFoundError
                 return # Salir de la función async_process
            # Capturar otros errores de descarga (IOError, etc.) que SÍ podrían reintentarse
            except (IOError, Exception) as download_err:
                 task_log.error("Failed to download file from MinIO.", error=str(download_err), error_type=type(download_err).__name__, exc_info=True)
                 raise download_err # Relanzar para que Celery reintente si está configurado

            file_bytes = downloaded_file_stream.getvalue()
            if not file_bytes:
                # Si el archivo está vacío, marcar como error y salir.
                task_log.error("Downloaded file from MinIO is empty.")
                await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message="Downloaded file is empty")
                return # Salir de la función async_process
            task_log.info(f"File downloaded successfully ({len(file_bytes)} bytes)")

            # 2. Preparar Input Haystack (ByteStream) con metadatos FILTRADOS
            # (Sin cambios en esta lógica, parece correcta)
            allowed_meta_keys = set(settings.MILVUS_METADATA_FIELDS)
            # Asegurar que los IDs son strings para Milvus/Haystack
            doc_meta = {
                "company_id": str(company_id),
                "document_id": str(document_id),
                "file_name": file_name or "unknown",
                "file_type": content_type or "unknown",
            }
            # Añadir metadatos originales si están permitidos y no son claves reservadas
            filtered_original_meta_count = 0
            for key, value in original_metadata.items():
                if key in allowed_meta_keys and key not in doc_meta:
                    # Convertir a string para asegurar compatibilidad
                    doc_meta[key] = str(value) if value is not None else None
                    filtered_original_meta_count += 1
                elif key not in doc_meta:
                    task_log.debug("Ignoring metadata field not in MILVUS_METADATA_FIELDS", field=key)

            task_log.debug("Filtered metadata for Haystack/Milvus", final_meta=doc_meta, original_allowed_added=filtered_original_meta_count)
            source_stream = ByteStream(data=file_bytes, meta=doc_meta)

            # 3. Seleccionar Conversor o Manejar OCR / Construir Pipeline
            # (Sin cambios en esta lógica)
            ConverterClass = get_converter_for_content_type(content_type)
            if content_type in settings.EXTERNAL_OCR_REQUIRED_CONTENT_TYPES:
                task_log.error("OCR processing required but not implemented.", content_type=content_type)
                # Lanzar NotImplementedError para que Celery NO reintente
                raise NotImplementedError(f"OCR processing for {content_type} not implemented.")
            elif ConverterClass:
                 task_log.info(f"Using Haystack converter: {ConverterClass.__name__}")
                 # Inicializar componentes (síncrono)
                 document_store = get_haystack_document_store()
                 converter = ConverterClass()
                 splitter = get_haystack_splitter()
                 embedder = get_haystack_embedder()
                 writer = DocumentWriter(document_store=document_store)

                 # Construir pipeline (síncrono)
                 haystack_pipeline.add_component("converter", converter)
                 haystack_pipeline.add_component("splitter", splitter)
                 haystack_pipeline.add_component("embedder", embedder)
                 haystack_pipeline.add_component("writer", writer)
                 haystack_pipeline.connect("converter.documents", "splitter.documents")
                 haystack_pipeline.connect("splitter.documents", "embedder.documents")
                 haystack_pipeline.connect("embedder.documents", "writer.documents")

                 pipeline_input = {"converter": {"sources": [source_stream]}}
            else:
                 # Si no hay conversor y no es OCR, es un tipo no soportado
                 task_log.error("Unsupported content type for Haystack processing", content_type=content_type)
                 # Lanzar ValueError para que Celery NO reintente
                 raise ValueError(f"Unsupported content type for processing: {content_type}")

            # 4. Ejecutar el Pipeline Haystack (usando executor porque es bloqueante)
            if not haystack_pipeline.inputs: # Verificar si la pipeline se construyó
                 raise RuntimeError("Haystack pipeline construction failed or is empty.")

            task_log.info("Running Haystack indexing pipeline via executor...", pipeline_input_keys=list(pipeline_input.keys()))
            start_time = time.monotonic()
            loop = asyncio.get_running_loop()
            # *** CORREGIDO: Ejecutar el pipeline síncrono en el executor ***
            pipeline_result = await loop.run_in_executor(
                None, # Default executor
                lambda: haystack_pipeline.run(pipeline_input)
            )
            duration = time.monotonic() - start_time
            task_log.info(f"Haystack pipeline finished via executor in {duration:.2f} seconds.")

            # 5. Verificar resultado y obtener contador de chunks/documentos procesados
            # (Sin cambios en esta lógica)
            writer_output = pipeline_result.get("writer", {})
            # Haystack 2.x: el output del writer suele ser {"documents_written": count}
            if isinstance(writer_output, dict) and "documents_written" in writer_output:
                 processed_docs_count = writer_output["documents_written"]
                 task_log.info(f"Chunks/Documents written to Milvus (from writer output): {processed_docs_count}")
            else:
                 # Fallback: intentar contar desde el splitter si el writer no informa
                 splitter_output = pipeline_result.get("splitter", {})
                 if isinstance(splitter_output, dict) and "documents" in splitter_output:
                      processed_docs_count = len(splitter_output["documents"])
                      task_log.warning(f"Could not get count from writer, inferred processed chunk count from splitter: {processed_docs_count}", writer_output=writer_output)
                 else:
                      processed_docs_count = 0 # No se pudo determinar
                      task_log.warning("Processed chunk count could not be determined from pipeline output, setting to 0.", pipeline_output=pipeline_result)

            # 6. Actualizar Estado Final en Supabase como PROCESSED (o INDEXED si prefieres)
            final_status = DocumentStatus.PROCESSED # O DocumentStatus.INDEXED
            await postgres_client.update_document_status(
                document_id, final_status, chunk_count=processed_docs_count, error_message=None # Limpiar mensaje de error
            )
            task_log.info("Document status set to PROCESSED/INDEXED in Supabase", chunk_count=processed_docs_count)

        # *** CORREGIDO: Manejo de excepciones específicas para evitar reintentos innecesarios ***
        except (ValueError, NotImplementedError, TypeError) as logical_error:
             # Errores de lógica/datos (tipo no soportado, OCR no implementado, etc.) - NO REINTENTAR
             task_log.error("Logical/Data error during processing, will not retry.", error=str(logical_error), error_type=type(logical_error).__name__, exc_info=True)
             try:
                 await postgres_client.update_document_status(
                     document_id, DocumentStatus.ERROR, error_message=f"Task Error (No Retry): {type(logical_error).__name__}: {str(logical_error)[:500]}"
                 )
                 task_log.info("Document status set to ERROR in Supabase due to logical/data failure.")
             except Exception as db_update_err:
                 task_log.error("CRITICAL: Failed to update document status to ERROR after logical/data failure", nested_error=str(db_update_err), exc_info=True)
             # NO relanzar la excepción para que Celery no la vea como un fallo reintentable
             # La tarea se marcará como SUCCESSFUL en Celery, pero el estado en la BD será ERROR.
             # Si prefieres que Celery la marque como FAILED, puedes relanzarla, pero asegúrate que no está en `autoretry_for`.
             # raise logical_error # Descomentar si quieres que Celery marque como FAILED
        except Exception as e:
            # Captura cualquier OTRA excepción (IOError, TimeoutError, errores inesperados) que SÍ podría reintentarse
            task_log.error("Potentially recoverable error during Haystack processing", error=str(e), error_type=type(e).__name__, exc_info=True)
            try:
                # Intenta marcar como error en la BD (puede que se revierta si hay reintento exitoso)
                await postgres_client.update_document_status(
                    document_id, DocumentStatus.ERROR, error_message=f"Task Error (Retry Pending): {type(e).__name__}: {str(e)[:500]}" # Limita longitud del error
                )
                task_log.info("Document status set to ERROR in Supabase due to potentially recoverable failure.")
            except Exception as db_update_err:
                # Loguea si falla la actualización de estado a ERROR
                task_log.error("CRITICAL: Failed to update document status to ERROR after potentially recoverable failure", nested_error=str(db_update_err), exc_info=True)
            # Re-lanza la excepción original para que Celery la vea y maneje reintentos/fallo según `autoretry_for`
            raise e
        finally:
            # Asegurar limpieza de recursos
            if downloaded_file_stream:
                downloaded_file_stream.close()
            # Si se inicializó el document_store, podrías cerrarlo si es necesario (revisar documentación de MilvusDocumentStore)
            # if document_store: await document_store.close() # O método similar si existe y es async
            task_log.debug("Cleaned up resources for task.")

    # --- Ejecutar la lógica async dentro de la tarea síncrona de Celery ---
    try:
        # *** CORREGIDO: Ejecuta la función async_process hasta que complete ***
        asyncio.run(async_process())
        task_log.info("Haystack document processing task finished.")
    except Exception as task_exception:
        # Si async_process lanzó una excepción (y fue una de las reintentables O una que no se capturó explícitamente arriba),
        # Celery necesita verla para marcar la tarea como fallida y potencialmente reintentar.
        # Las excepciones FileNotFoundError, ValueError, NotImplementedError, etc., ya se manejaron dentro de async_process y no deberían llegar aquí si no se relanzaron.
        task_log.exception("Haystack processing task failed at top level after potential retries or due to unhandled exception.")
        # La excepción ya fue relanzada desde async_process si era reintentable.
        # No es necesario relanzar explícitamente aquí si ya se hizo en async_process.
        # Si quieres asegurarte que Celery la vea, puedes añadir: raise task_exception
        pass # La excepción ya se propagó (si era reintentable) y Celery la manejará
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
version = "0.1.0"
description = "Ingest service for AUDIZOR B2B"
authors = ["Nyro <dev@nyro.com>"]

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
asyncpg = "^0.29.0"
python-jose = {extras = ["cryptography"], version = "^3.3.0"}
tenacity = "^8.2.3"
python-multipart = "^0.0.9"
structlog = "^24.1.0"
aiofiles = "^23.2.1"
minio = "^7.1.17"

# --- Haystack Dependencies ---
haystack-ai = "^2.0.1"
openai = "^1.14.3"
# *** CORREGIDO: Asegurar pymilvus explícitamente como recomienda la doc ***
pymilvus = "^2.4.1" # Añadir pymilvus explícitamente (verifica versión compatible si es necesario)
milvus-haystack = "^0.0.6" # Paquete correcto para la integración Haystack 2.x

# --- Haystack Converter Dependencies ---
pypdf = "^4.0.1"
python-docx = "^1.1.0"
# Añadir 'markdown' y 'beautifulsoup4' si usas MarkdownToDocument y HTMLToDocument
markdown = "^3.5" # Añadido para MarkdownToDocument
beautifulsoup4 = "^4.12.3" # Añadido para HTMLToDocument


[tool.poetry.dev-dependencies]
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"
httpx = "^0.27.0" # Para test client

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
