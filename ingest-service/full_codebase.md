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
import uuid
from typing import Dict, Any, Optional
import json
import structlog
import io

from fastapi import APIRouter, UploadFile, File, Depends, HTTPException, status, Form, Header

from app.api.v1 import schemas
from app.core.config import settings
from app.db import postgres_client
from app.tasks.process_document import process_document_haystack_task # Use the new task
from app.services.minio_client import MinioStorageClient

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Dependency for Company ID (Keep as is or adapt to your auth) ---
async def get_current_company_id(x_company_id: Optional[str] = Header(None)) -> uuid.UUID:
    # ... (implementation from previous version is fine) ...
    if not x_company_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Company-ID header",
        )
    try:
        return uuid.UUID(x_company_id)
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
    summary="Ingest a new document (Haystack)",
    description="Uploads a document, stores it, creates a DB record, and queues it for Haystack processing.",
)
async def ingest_document_haystack(
    metadata_json: str = Form(default="{}", description="JSON string of document metadata"),
    file: UploadFile = File(..., description="The document file to ingest"),
    company_id: uuid.UUID = Depends(get_current_company_id),
):
    """
    Endpoint to initiate document ingestion using Haystack pipeline.
    1. Validates input and metadata.
    2. Uploads file to Storage Service.
    3. Creates initial record in PostgreSQL.
    4. Queues the Haystack processing task in Celery.
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
    except Exception as e:
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
            raise ValueError("Uploaded file is empty.")

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
        #    This could also be done in the Celery task, but doing it here confirms upload
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
            minio_object_name=minio_object_name, # Pass object name instead of local path
            file_name=file.filename or "untitled",
            content_type=content_type,
            original_metadata=metadata,
        )
        task_id = task.id
        request_log.info("Haystack document processing task queued", task_id=task_id)

        return schemas.IngestResponse(document_id=document_id, task_id=task_id)

    except Exception as e:
        request_log.error("Error during ingestion trigger", error=str(e), exc_info=True)
        if document_id:
            try:
                # Attempt to mark as error, don't overwrite file_path if it was set
                await postgres_client.update_document_status(
                    document_id,
                    schemas.DocumentStatus.ERROR,
                    error_message=f"Ingestion API Error: {type(e).__name__}: {str(e)[:250]}"
                )
            except Exception as db_err:
                 request_log.error("Failed to mark document as error after API failure", nested_error=str(db_err))

        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
        detail = "Failed to process ingestion request."
        if isinstance(e, ValueError):
             status_code = status.HTTP_400_BAD_REQUEST
             detail = str(e)
        elif isinstance(e, S3Error): # Catch MinIO errors specifically
             status_code = status.HTTP_503_SERVICE_UNAVAILABLE # Indicate storage issue
             detail = f"Storage service error: {e.code}"

        raise HTTPException(status_code=status_code, detail=detail)

    finally:
        await file.close()


@router.get(
    "/ingest/status/{document_id}",
    response_model=schemas.StatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Get document ingestion status",
    description="Retrieves the current processing status and basic information of a document.",
)
async def get_ingestion_status(
    document_id: uuid.UUID,
    company_id: uuid.UUID = Depends(get_current_company_id),
):
    """
    Endpoint to consult the processing status of a document.
    (Implementation remains largely the same as previous version, but ensure
     it correctly reflects Haystack pipeline outcomes like PROCESSED/ERROR)
    """
    status_log = log.bind(document_id=str(document_id), company_id=str(company_id))
    status_log.info("Received request for document status")

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
    if doc_data.get("company_id") != company_id:
        status_log.warning("Company ID mismatch for document status request", owner_company_id=doc_data.get("company_id"))
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this document's status.",
        )

    # Map DB status to response model
    response_data = schemas.StatusResponse(
        document_id=doc_data["id"],
        status=doc_data["status"], # Assumes DB status enum matches response enum
        file_name=doc_data.get("file_name"),
        file_type=doc_data.get("file_type"),
        chunk_count=doc_data.get("chunk_count"),
        error_message=doc_data.get("error_message"),
        last_updated=doc_data.get("updated_at"), # Pass datetime directly
    )

    # Add descriptive message based on status
    status_messages = {
        DocumentStatus.UPLOADED: "Document uploaded, awaiting processing.",
        DocumentStatus.PROCESSING: "Document is currently being processed by the Haystack pipeline.",
        DocumentStatus.PROCESSED: f"Document processed successfully with {response_data.chunk_count or 0} chunks indexed.",
        DocumentStatus.ERROR: f"Processing failed: {response_data.error_message or 'Unknown error'}",
        DocumentStatus.INDEXED: f"Document processed and indexed successfully with {response_data.chunk_count or 0} chunks.", # If using INDEXED status
    }
    response_data.message = status_messages.get(response_data.status, "Unknown status.")


    status_log.info("Returning document status", status=response_data.status)
    return response_data
```

## File: `app\api\v1\schemas.py`
```py
import uuid
from pydantic import BaseModel, Field, Json # Use Json for automatic validation
from typing import Optional, Dict, Any, List
from app.models.domain import DocumentStatus
from datetime import datetime

# Pydantic schema for metadata validation (optional but recommended)
# class DocumentMetadata(BaseModel):
#     category: Optional[str] = None
#     author: Optional[str] = None
#     # Add other expected metadata fields

class IngestRequest(BaseModel):
    # company_id will come from dependency/header, not this model
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Metadata JSON for the document")

class IngestResponse(BaseModel):
    document_id: uuid.UUID
    task_id: Optional[str] = None # Return Celery task ID for tracking
    status: DocumentStatus = DocumentStatus.UPLOADED
    message: str = "Document upload received and queued for processing."

class StatusResponse(BaseModel):
    document_id: uuid.UUID
    status: DocumentStatus
    file_name: Optional[str] = None
    file_type: Optional[str] = None
    chunk_count: Optional[int] = None
    error_message: Optional[str] = None
    last_updated: Optional[datetime] = None # Use datetime for proper typing
    message: Optional[str] = None
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
# ./app/db/postgres_client.py (CORREGIDO - Log)
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
    """Obtiene o crea el pool de conexiones a la base de datos (Supabase)."""
    global _pool
    if _pool is None or _pool._closed:
        try:
            # *** CORREGIDO: Loguear los parámetros que SÍ se usan para conectar ***
            log.info("Creating Supabase/PostgreSQL connection pool using arguments...",
                     host=settings.POSTGRES_SERVER,
                     port=settings.POSTGRES_PORT, # Usará el puerto del Pooler (6543 por defecto) desde config
                     user=settings.POSTGRES_USER,
                     database=settings.POSTGRES_DB)

            _pool = await asyncpg.create_pool(
                user=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD.get_secret_value(),
                database=settings.POSTGRES_DB,
                host=settings.POSTGRES_SERVER,
                port=settings.POSTGRES_PORT, # Tomará el valor de settings (6543 por defecto o de ConfigMap)
                min_size=5,  # Ajustar según necesidad
                max_size=20, # Ajustar según necesidad
                # Añadir configuraciones de timeout si es necesario
                # command_timeout=60, # Timeout para comandos individuales
                # timeout=300, # Timeout general? Revisar docs de asyncpg
                # El init para jsonb sigue siendo útil
                init=lambda conn: conn.set_type_codec(
                    'jsonb',
                    encoder=json.dumps,
                    decoder=json.loads,
                    schema='pg_catalog',
                    format='text'
                )
            )
            log.info("Supabase/PostgreSQL connection pool created successfully.")
        except OSError as e:
             # Capturar específicamente errores de red/OS como 'Network unreachable'
             log.error("Network/OS error creating Supabase/PostgreSQL connection pool",
                      error=str(e),
                      errno=e.errno if hasattr(e, 'errno') else None,
                      host=settings.POSTGRES_SERVER,
                      port=settings.POSTGRES_PORT,
                      db=settings.POSTGRES_DB,
                      user=settings.POSTGRES_USER,
                      exc_info=True) # Incluir traceback
             raise # Re-lanzar para que falle el startup
        except asyncpg.exceptions.InvalidPasswordError:
             log.error("Invalid password for Supabase/PostgreSQL connection",
                       host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT, user=settings.POSTGRES_USER)
             raise
        except Exception as e:
            # Loguear el error específico
            log.error("Failed to create Supabase/PostgreSQL connection pool",
                      error=str(e),
                      host=settings.POSTGRES_SERVER,
                      port=settings.POSTGRES_PORT,
                      db=settings.POSTGRES_DB,
                      user=settings.POSTGRES_USER,
                      exc_info=True) # Incluir traceback
            raise # Re-lanzar la excepción para que falle el startup si no conecta
    return _pool

async def close_db_pool():
    """Cierra el pool de conexiones."""
    global _pool
    if _pool and not _pool._closed:
        log.info("Closing Supabase/PostgreSQL connection pool...")
        await _pool.close()
        _pool = None
        log.info("Supabase/PostgreSQL connection pool closed.")

# --- Funciones create_document, update_document_status, get_document_status ---
# (Sin cambios necesarios en la lógica interna, ya usan los parámetros individuales
# y el pool correctamente. Se mantienen como estaban en tu versión original)

async def create_document(
    company_id: uuid.UUID,
    file_name: str,
    file_type: str,
    metadata: Dict[str, Any] # Pass the original dictionary
) -> uuid.UUID:
    """Crea un registro inicial para el documento en la tabla DOCUMENTS."""
    pool = await get_db_pool()
    doc_id = uuid.uuid4()

    query = """
        INSERT INTO documents (id, company_id, file_name, file_type, metadata, status, file_path, uploaded_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, '', NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC')
        RETURNING id;
    """
    # Note: $5 is now directly the metadata dict, asyncpg's codec handles it.

    try:
        async with pool.acquire() as connection:
            # async with connection.transaction(): # Consider transaction if needed
                result = await connection.fetchval(
                    query,
                    doc_id,
                    company_id,
                    file_name,
                    file_type,
                    metadata, # Pass the dictionary directly
                    DocumentStatus.UPLOADED.value
                )
        if result:
            log.info("Document record created in Supabase", document_id=doc_id, company_id=company_id)
            return result
        else:
             log.error("Failed to create document record, no ID returned.", document_id=doc_id)
             raise RuntimeError("Failed to create document record, no ID returned.")
    except asyncpg.exceptions.UniqueViolationError as e:
        log.error("Failed to create document record due to unique constraint violation.", error=str(e), document_id=doc_id, company_id=company_id, exc_info=False) # No need for full traceback usually
        # Decide how to handle this - raise a specific error?
        raise ValueError(f"Document creation failed: unique constraint violated ({e.constraint_name})") from e
    except Exception as e:
        log.error("Failed to create document record in Supabase", error=str(e), document_id=doc_id, company_id=company_id, file_name=file_name, exc_info=True)
        raise # Re-raise generic exceptions


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
        # Limitar longitud del mensaje de error
        safe_error_message = (error_message or "Unknown processing error")[:1000]
        fields_to_update.append(f"error_message = ${current_param_index}")
        params.append(safe_error_message)
        current_param_index += 1
    else:
        # Limpiar mensaje de error si el estado no es ERROR
        fields_to_update.append("error_message = NULL")

    query = f"""
        UPDATE documents
        SET {', '.join(fields_to_update)}
        WHERE id = $1;
    """
    try:
        async with pool.acquire() as connection:
             # async with connection.transaction(): # Consider transaction
                result = await connection.execute(query, *params)
        # Parse result string like 'UPDATE 1'
        affected_rows = 0
        if isinstance(result, str) and result.startswith("UPDATE "):
            try:
                affected_rows = int(result.split(" ")[1])
            except (IndexError, ValueError):
                log.warning("Could not parse affected rows from DB result", result_string=result)

        success = affected_rows > 0
        if success:
            log.info("Document status updated in Supabase", document_id=document_id, new_status=status.value, file_path=file_path, chunk_count=chunk_count, has_error=(status == DocumentStatus.ERROR))
        else:
            log.warning("Document status update did not affect any rows (document might not exist?)", document_id=document_id, new_status=status.value)
        return success
    except Exception as e:
        log.error("Failed to update document status in Supabase", error=str(e), document_id=document_id, new_status=status.value, exc_info=True)
        raise # Re-raise generic exceptions


async def get_document_status(document_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """Obtiene el estado y otros datos de un documento de la tabla DOCUMENTS."""
    pool = await get_db_pool()
    query = """
        SELECT id, company_id, status, file_name, file_type, chunk_count, error_message, updated_at
        FROM documents
        WHERE id = $1;
    """
    try:
        async with pool.acquire() as connection:
            record = await connection.fetchrow(query, document_id)
        if record:
            log.debug("Document status retrieved from Supabase", document_id=document_id)
            # Convert asyncpg.Record to dict for easier handling
            return dict(record)
        else:
            log.warning("Document status requested for non-existent ID", document_id=document_id)
            return None
    except Exception as e:
        log.error("Failed to get document status from Supabase", error=str(e), document_id=document_id, exc_info=True)
        raise # Re-raise generic exceptions
```

## File: `app\main.py`
```py
# ./app/main.py (CORREGIDO - Posición de 'global SERVICE_READY')
from fastapi import FastAPI, HTTPException, status as fastapi_status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import structlog
import uvicorn
import logging # Import logging
import sys # Import sys for SystemExit
import asyncio # Import asyncio for health check timeout

from app.api.v1.endpoints import ingest
from app.core.config import settings
from app.core.logging_config import setup_logging
from app.db import postgres_client
# Remove milvus_client import if not used directly (Haystack handles it)
# from app.db import milvus_client

# Configurar logging ANTES de importar cualquier otra cosa que loguee
setup_logging()
log = structlog.get_logger(__name__)

# Estado global simple para verificar dependencias críticas al inicio
SERVICE_READY = False

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.1.0",
    description="Microservice for document ingestion and preprocessing using Haystack.",
)

# --- Event Handlers ---
@app.on_event("startup")
async def startup_event():
    # *** CORREGIDO: Declarar global al inicio si se modifica ***
    global SERVICE_READY
    log.info("Starting up Ingest Service (Haystack)...")
    db_pool_initialized = False
    try:
        # Intenta obtener el pool (lo crea si no existe)
        await postgres_client.get_db_pool()
        # Verifica la conexión activa
        pool = await postgres_client.get_db_pool()
        async with pool.acquire() as conn:
            # Verificar conexión con timeout corto
            await asyncio.wait_for(conn.execute("SELECT 1"), timeout=10.0)
        log.info("PostgreSQL connection pool initialized and connection verified.")
        db_pool_initialized = True
    except asyncio.TimeoutError:
        log.critical("CRITICAL: Timed out (>10s) verifying PostgreSQL connection on startup.", exc_info=False)
    except Exception as e:
        log.critical("CRITICAL: Failed to establish/verify essential PostgreSQL connection pool on startup.", error=str(e), exc_info=True)

    # Marca el servicio como listo SOLO si la BD conectó
    if db_pool_initialized:
        SERVICE_READY = True
        log.info("Ingest Service startup sequence completed and service marked as READY.")
    else:
        SERVICE_READY = False
        log.warning("Ingest Service startup sequence completed but essential DB connection failed. Service marked as NOT READY.")


@app.on_event("shutdown")
async def shutdown_event():
    log.info("Shutting down Ingest Service (Haystack)...")
    await postgres_client.close_db_pool()
    log.info("PostgreSQL connection pool closed.")
    log.info("Ingest Service shutdown complete.")

# --- Exception Handlers (Mantener como estaban) ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    log.warning("HTTP Exception caught", status_code=exc.status_code, detail=exc.detail, path=str(request.url))
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    log.warning("Request Validation Error", errors=exc.errors(), path=str(request.url))
    error_details = []
    for error in exc.errors():
        field = " -> ".join(map(str, error.get("loc", [])))
        error_details.append({"field": field, "message": error.get("msg", ""), "type": error.get("type", "")})
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Validation Error", "errors": error_details},
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    log.error("Unhandled Exception caught", error=str(exc), path=str(request.url), exc_info=True)
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal server error occurred."},
    )


# --- Routers ---
app.include_router(ingest.router, prefix=settings.API_V1_STR, tags=["Ingestion"])

# --- Root Endpoint / Health Check ---
@app.get("/", tags=["Health Check"], status_code=fastapi_status.HTTP_200_OK)
async def read_root():
    """
    Health check endpoint. Checks if the service started successfully
    and can still connect to the database.
    Returns 503 Service Unavailable if startup failed or DB connection is lost.
    """
    # *** CORREGIDO: Mover la declaración 'global' al inicio de la función ***
    global SERVICE_READY
    log.debug("Root endpoint accessed (health check)")

    if not SERVICE_READY:
         log.warning("Health check failed: Service did not start successfully (SERVICE_READY is False).")
         raise HTTPException(
             status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
             detail="Service is not ready, essential connections likely failed during startup."
         )

    # Chequeo activo de la base de datos (ping)
    try:
        pool = await postgres_client.get_db_pool()
        async with pool.acquire() as conn:
            # Usa un timeout bajo para el ping
            await asyncio.wait_for(conn.execute("SELECT 1"), timeout=5.0)
        log.debug("Health check: DB ping successful.")
        # Si el ping tiene éxito pero SERVICE_READY era False (raro, pero posible si hubo un error temporal), lo corregimos.
        if not SERVICE_READY:
             log.warning("DB Ping successful, but service was marked as not ready. Setting SERVICE_READY=True now.")
             SERVICE_READY = True # Marcar como listo si el ping funciona ahora
    except asyncio.TimeoutError:
        log.error("Health check failed: DB ping timed out (> 5 seconds).")
        # Marcar como no listo si falla el ping
        SERVICE_READY = False
        raise HTTPException(
            status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service is unhealthy, database connection check timed out."
        )
    except Exception as db_ping_err:
        log.error("Health check failed: DB ping error.", error=str(db_ping_err))
        # Marcar como no listo si falla el ping
        SERVICE_READY = False
        raise HTTPException(
            status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Service is unhealthy, cannot connect to database: {db_ping_err}"
        )

    # Si todo está bien
    return {"status": "ok", "service": settings.PROJECT_NAME, "ready": SERVICE_READY}

# --- Main execution (for local development) ---
if __name__ == "__main__":
    log.info("Starting Uvicorn server for local development...")
    log_level_str = settings.LOG_LEVEL.lower()
    if log_level_str not in logging._nameToLevel:
        log.warning(f"Invalid LOG_LEVEL '{settings.LOG_LEVEL}', defaulting Uvicorn log level to 'info'.")
        log_level_str = "info"

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True, # Activa reload para desarrollo local
        log_level=log_level_str
    )

#V 0.0.2
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
            upload_log.error("Failed to upload file to MinIO via executor", error=str(e), exc_info=True)
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
            download_log.error("Failed to download file from MinIO (sync)", error=str(e), code=e.code, exc_info=False)
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
        except Exception as e:
            download_log.error("Error downloading file via executor", error=str(e), exc_info=True)
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
# ./app/tasks/process_document.py (CORREGIDO - Llamada a MinIO en executor)
import uuid
import asyncio
from typing import Dict, Any, Optional, List, Type
import tempfile
import os
from pathlib import Path
import structlog
import base64
import io

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
from app.db import postgres_client
from app.models.domain import DocumentStatus
from app.services.minio_client import MinioStorageClient # Importar cliente corregido

log = structlog.get_logger(__name__)

# --- Funciones de inicialización de Haystack (sin cambios necesarios) ---
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
    if not settings.OPENAI_API_KEY.get_secret_value():
         log.warning(f"OpenAI API Key not found in settings. Haystack embedding might fail.")
    return OpenAIDocumentEmbedder(
        api_key=Secret.from_env_var(api_key_env_var) if os.getenv(api_key_env_var) else Secret.from_token(settings.OPENAI_API_KEY.get_secret_value()),
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
    autoretry_for=(Exception,), # Reintenta en cualquier excepción (ajusta si es necesario)
    retry_kwargs={'max_retries': 2, 'countdown': 60}, # 2 reintentos con 60s de espera
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
    """
    document_id = uuid.UUID(document_id_str)
    company_id = uuid.UUID(company_id_str)
    task_log = log.bind(document_id=str(document_id), company_id=str(company_id),
                      task_id=self.request.id, file_name=file_name, object_name=minio_object_name, content_type=content_type)
    task_log.info("Starting Haystack document processing task")

    # Usar una función async interna para poder usar await con postgres_client y run_in_executor
    async def async_process():
        haystack_pipeline = Pipeline()
        processed_docs_count = 0
        # Crear instancia del cliente MinIO aquí dentro
        minio_client = MinioStorageClient()
        downloaded_file_stream: Optional[io.BytesIO] = None
        document_store: Optional[MilvusDocumentStore] = None

        try:
            # 0. Marcar como procesando en Supabase
            await postgres_client.update_document_status(document_id, DocumentStatus.PROCESSING)
            task_log.info("Document status set to PROCESSING")

            # 1. Descargar archivo de MinIO (usando executor para la llamada síncrona)
            task_log.info("Downloading file from MinIO via executor...")
            loop = asyncio.get_running_loop()
            try:
                # *** CORREGIDO: Llamar a la función síncrona en el executor ***
                downloaded_file_stream = await loop.run_in_executor(
                    None, # Usa el default ThreadPoolExecutor
                    minio_client.download_file_stream_sync, # Llama a la función sync corregida
                    minio_object_name
                )
            except FileNotFoundError as fnf_err:
                 # Si el archivo no existe, no tiene sentido reintentar. Marcar como error y salir.
                 task_log.error("File not found in MinIO storage. Cannot process.", object_name=minio_object_name, error=str(fnf_err))
                 await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message="File not found in storage")
                 # No relanzamos la excepción aquí para que Celery no intente reintentar por FileNotFoundError
                 return # Salir de la función async_process
            except Exception as download_err:
                 # Otros errores de descarga sí podrían beneficiarse de un reintento
                 task_log.error("Failed to download file from MinIO.", error=str(download_err), exc_info=True)
                 raise download_err # Relanzar para que Celery reintente

            file_bytes = downloaded_file_stream.getvalue()
            if not file_bytes:
                # Si el archivo está vacío, marcar como error y salir.
                task_log.error("Downloaded file from MinIO is empty.")
                await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message="Downloaded file is empty")
                return # Salir de la función async_process
            task_log.info(f"File downloaded successfully via executor ({len(file_bytes)} bytes)")

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

            # 3. Seleccionar Conversor o Manejar OCR
            # (Sin cambios en esta lógica)
            ConverterClass = get_converter_for_content_type(content_type)
            if content_type in settings.EXTERNAL_OCR_REQUIRED_CONTENT_TYPES:
                # Aquí iría la lógica para llamar a un servicio OCR externo si estuviera implementado
                task_log.error("OCR processing required but not implemented.", content_type=content_type)
                raise NotImplementedError(f"OCR processing for {content_type} not implemented.")
            elif ConverterClass:
                 task_log.info(f"Using Haystack converter: {ConverterClass.__name__}")
                 document_store = get_haystack_document_store() # Inicializar store
                 haystack_pipeline.add_component("converter", ConverterClass())
                 haystack_pipeline.add_component("splitter", get_haystack_splitter())
                 haystack_pipeline.add_component("embedder", get_haystack_embedder())
                 haystack_pipeline.add_component("writer", DocumentWriter(document_store=document_store))
                 # Conectar los componentes en orden
                 haystack_pipeline.connect("converter.documents", "splitter.documents")
                 haystack_pipeline.connect("splitter.documents", "embedder.documents")
                 haystack_pipeline.connect("embedder.documents", "writer.documents")
                 pipeline_input = {"converter": {"sources": [source_stream]}}
            else:
                 # Si no hay conversor y no es OCR, es un tipo no soportado
                 task_log.error("Unsupported content type for Haystack processing", content_type=content_type)
                 raise ValueError(f"Unsupported content type for processing: {content_type}")

            # 4. Ejecutar el Pipeline Haystack (usando executor porque puede ser bloqueante)
            if not haystack_pipeline.inputs: # Verificar si la pipeline se construyó
                 raise RuntimeError("Haystack pipeline construction failed or is empty.")

            task_log.info("Running Haystack indexing pipeline via executor...", pipeline_input_keys=list(pipeline_input.keys()))
            loop = asyncio.get_running_loop()
            # Ejecutar el pipeline síncrono en el executor
            pipeline_result = await loop.run_in_executor(
                None, # Default executor
                lambda: haystack_pipeline.run(pipeline_input)
            )
            task_log.info("Haystack pipeline finished via executor.")

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

        except Exception as e:
            # Captura cualquier excepción durante el proceso
            task_log.error("Error during Haystack document processing", error=str(e), exc_info=True)
            try:
                # Intenta marcar como error en la BD
                await postgres_client.update_document_status(
                    document_id, DocumentStatus.ERROR, error_message=f"Task Error: {type(e).__name__}: {str(e)[:500]}" # Limita longitud del error
                )
                task_log.info("Document status set to ERROR in Supabase due to processing failure.")
            except Exception as db_update_err:
                # Loguea si falla la actualización de estado a ERROR
                task_log.error("CRITICAL: Failed to update document status to ERROR after processing failure", nested_error=str(db_update_err), exc_info=True)
            # Re-lanza la excepción original para que Celery la vea y maneje reintentos/fallo
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
        # Ejecuta la función async_process hasta que complete
        asyncio.run(async_process())
        task_log.info("Haystack document processing task finished successfully.")
    except Exception as task_exception:
        # Si async_process lanzó una excepción (y no fue FileNotFoundError o archivo vacío manejados internamente)
        # Celery necesita ver la excepción para marcar la tarea como fallida y potencialmente reintentar.
        task_log.exception("Haystack processing task failed at top level after potential retries.")
        # La excepción ya fue relanzada desde async_process, Celery la capturará.
        # No es necesario relanzar explícitamente aquí si ya se hizo en async_process.
        # Si quieres asegurarte, puedes añadir: raise task_exception
        pass # La excepción ya se propagó y Celery la manejará
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
# markdown = "^3.5"
# beautifulsoup4 = "^4.12.3"


[tool.poetry.dev-dependencies]
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"
httpx = "^0.27.0" # Para test client

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
