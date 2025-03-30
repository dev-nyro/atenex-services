# Estructura de la Codebase

```
app/
├── api
│   └── v1
│       ├── endpoints
│       │   ├── __init__.py
│       │   └── query.py
│       └── schemas.py
├── core
│   ├── __init__.py
│   ├── config.py
│   └── logging_config.py
├── db
│   ├── __init__.py
│   └── postgres_client.py
├── main.py
├── models
│   └── __init__.py
├── pipelines
│   ├── __init__.py
│   └── rag_pipeline.py
├── services
│   ├── __init__.py
│   ├── base_client.py
│   └── gemini_client.py
└── utils
    ├── __init__.py
    └── helpers.py
```

# Codebase: `app`

## File: `app\api\v1\endpoints\__init__.py`
```py

```

## File: `app\api\v1\endpoints\query.py`
```py
# ./app/api/v1/endpoints/query.py
import uuid
from typing import Dict, Any, Optional
import structlog
import asyncio

from fastapi import APIRouter, Depends, HTTPException, status, Header, Body

from app.api.v1 import schemas
from app.core.config import settings
from app.db import postgres_client # Para logging
from app.pipelines import rag_pipeline # Importar funciones del pipeline
from haystack import Document # Para type hints

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Dependency for Company ID ---
# (Reutilizado de ingest-service, adaptado si es necesario)
async def get_current_company_id(x_company_id: Optional[str] = Header(None)) -> uuid.UUID:
    """Obtiene y valida el X-Company-ID del header."""
    if not x_company_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, # O 400 Bad Request si se considera error de cliente
            detail="Missing X-Company-ID header",
        )
    try:
        company_uuid = uuid.UUID(x_company_id)
        # Aquí podrías añadir una validación extra contra la tabla COMPANIES si fuera necesario
        # por ahora, solo validamos el formato UUID
        return company_uuid
    except ValueError:
         raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid X-Company-ID header format (must be UUID)",
        )

# --- Placeholder Dependency for User ID ---
# En una implementación real, esto extraería el user_id del token JWT
# validado por el API Gateway o un middleware de autenticación.
async def get_current_user_id(authorization: Optional[str] = Header(None)) -> Optional[uuid.UUID]:
    """Placeholder para obtener el User ID (ej: de un token JWT)."""
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ")[1]
        # Aquí iría la lógica para decodificar el token y extraer el user_id
        # Ejemplo hardcodeado (REEMPLAZAR CON LÓGICA REAL):
        try:
            # Simular extracción de un UUID del token
            # En un caso real, usarías python-jose u otra librería
            payload = {"user_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479"} # Ejemplo
            user_id_str = payload.get("user_id")
            if user_id_str:
                return uuid.UUID(user_id_str)
        except Exception as e:
            log.warning("Failed to simulate JWT decoding", error=str(e))
            return None # O lanzar HTTPException si el token es inválido/requerido
    return None # Retorna None si no hay token o no se puede extraer


# --- Endpoint ---
@router.post(
    "/query",
    response_model=schemas.QueryResponse,
    status_code=status.HTTP_200_OK,
    summary="Process a user query using RAG pipeline",
    description="Receives a query, retrieves relevant documents for the company, generates an answer using an LLM, logs the interaction, and returns the result.",
)
async def process_query(
    request_body: schemas.QueryRequest = Body(...),
    company_id: uuid.UUID = Depends(get_current_company_id),
    user_id: Optional[uuid.UUID] = Depends(get_current_user_id), # Obtener user_id (puede ser None)
):
    """
    Endpoint principal para procesar consultas de usuario.
    """
    endpoint_log = log.bind(
        company_id=str(company_id),
        user_id=str(user_id) if user_id else "anonymous",
        query=request_body.query[:100] + "..." if len(request_body.query) > 100 else request_body.query
    )
    endpoint_log.info("Received query request")

    try:
        # Ejecutar el pipeline RAG
        answer, retrieved_docs, log_id = await rag_pipeline.run_rag_pipeline(
            query=request_body.query,
            company_id=str(company_id), # Pasar como string
            user_id=str(user_id) if user_id else None, # Pasar como string o None
            top_k=request_body.retriever_top_k # Pasar el top_k del request si existe
        )

        # Formatear documentos recuperados para la respuesta
        response_docs = []
        for doc in retrieved_docs:
            # Extraer metadatos relevantes si existen
            doc_meta = doc.meta or {}
            original_doc_id = doc_meta.get("document_id")
            file_name = doc_meta.get("file_name")

            response_docs.append(schemas.RetrievedDocument(
                id=doc.id, # ID del chunk de Milvus
                score=doc.score,
                # Generar un preview corto del contenido
                content_preview=(doc.content[:150] + '...') if doc.content and len(doc.content) > 150 else doc.content,
                metadata=doc_meta, # Incluir todos los metadatos recuperados
                document_id=original_doc_id,
                file_name=file_name
            ))

        endpoint_log.info("Query processed successfully", log_id=str(log_id) if log_id else "Log Failed", num_retrieved=len(response_docs))

        return schemas.QueryResponse(
            answer=answer,
            retrieved_documents=response_docs,
            query_log_id=log_id # Será None si el logging falló
        )

    except ValueError as ve:
        endpoint_log.warning("Value error during query processing", error=str(ve))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))
    except ConnectionError as ce: # Podría venir de Milvus o DB si fallan las conexiones
         endpoint_log.error("Connection error during query processing", error=str(ce), exc_info=True)
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Service dependency unavailable: {ce}")
    except Exception as e:
        endpoint_log.exception("Unhandled exception during query processing")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An internal error occurred: {type(e).__name__}"
        )
```

## File: `app\api\v1\schemas.py`
```py
# ./app/api/v1/schemas.py
import uuid
from pydantic import BaseModel, Field, validator
from typing import Optional, List, Dict, Any, Union
from datetime import datetime

# --- Request Schemas ---

class QueryRequest(BaseModel):
    query: str = Field(..., min_length=3, description="The user's query in natural language.")
    retriever_top_k: Optional[int] = Field(None, gt=0, le=20, description="Number of documents to retrieve (overrides server default).")
    # chat_history: Optional[List[Dict[str, str]]] = Field(None, description="Optional chat history for context.") # TODO: Implementar

    @validator('query')
    def query_must_not_be_empty(cls, v):
        if not v.strip():
            raise ValueError('Query cannot be empty or just whitespace')
        return v

# --- Response Schemas ---

class RetrievedDocument(BaseModel):
    id: str = Field(..., description="The unique ID of the retrieved document chunk (usually from Milvus).")
    score: Optional[float] = Field(None, description="Relevance score assigned by the retriever (e.g., cosine similarity).")
    content_preview: Optional[str] = Field(None, description="A short preview of the document chunk's content.")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Metadata associated with the document chunk.")
    # Podríamos añadir el document_id original si está en los metadatos
    document_id: Optional[str] = Field(None, description="ID of the original source document.")
    file_name: Optional[str] = Field(None, description="Name of the original source file.")


class QueryResponse(BaseModel):
    answer: str = Field(..., description="The final answer generated by the LLM based on the query and retrieved documents.")
    retrieved_documents: List[RetrievedDocument] = Field(default_factory=list, description="List of documents retrieved and used as context for the answer.")
    query_log_id: Optional[uuid.UUID] = Field(None, description="The unique ID of the logged interaction in the database (if logging was successful).")


class HealthCheckDependency(BaseModel):
    status: str = Field(..., description="Status of the dependency ('ok', 'error', 'pending')")
    details: Optional[str] = Field(None, description="Additional details in case of error.")

class HealthCheckResponse(BaseModel):
    status: str = Field(default="ok", description="Overall status of the service.")
    service: str = Field(..., description="Name of the service.")
    ready: bool = Field(..., description="Indicates if the service is ready to serve requests.")
    dependencies: Dict[str, str] # Simplificado para el ejemplo inicial

# class HealthCheckResponse(BaseModel):
#     status: str = Field(default="ok", description="Overall status of the service.")
#     service: str = Field(..., description="Name of the service.")
#     ready: bool = Field(..., description="Indicates if the service is ready to serve requests.")
#     dependencies: Dict[str, HealthCheckDependency] = Field(..., description="Status of critical dependencies.")
```

## File: `app\core\__init__.py`
```py

```

## File: `app\core\config.py`
```py
# ./app/core/config.py
import logging
import os
from typing import Optional, List, Any, Dict
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import AnyHttpUrl, SecretStr, Field, validator, ValidationError, HttpUrl
import sys

# --- Supabase Connection Defaults (Usando Session Pooler) ---
SUPABASE_SESSION_POOLER_HOST = "aws-0-sa-east-1.pooler.supabase.com"
SUPABASE_SESSION_POOLER_PORT_INT = 5432
SUPABASE_SESSION_POOLER_USER_TEMPLATE = "postgres.{project_ref}"
SUPABASE_DEFAULT_DB = "postgres"

# --- Milvus Kubernetes Defaults ---
# *** CORRECCIÓN: Usar el nombre y namespace correctos del servicio Milvus ***
MILVUS_K8S_DEFAULT_URI = "http://milvus-milvus.default.svc.cluster.local:19530"

# --- RAG Defaults ---
DEFAULT_RAG_PROMPT_TEMPLATE = """
Basándote estrictamente en los siguientes documentos recuperados, responde a la pregunta del usuario.
Si los documentos no contienen la respuesta, indica explícitamente que no puedes responder con la información proporcionada.
No inventes información ni uses conocimiento externo.

Documentos:
{% for doc in documents %}
--- Documento {{ loop.index }} ---
{{ doc.content }}
--- Fin Documento {{ loop.index }} ---
{% endfor %}

Pregunta: {{ query }}

Respuesta:
"""

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='QUERY_',
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = "Query Service (Haystack RAG)"
    API_V1_STR: str = "/api/v1"
    LOG_LEVEL: str = "INFO"

    # --- Database ---
    POSTGRES_USER: str = Field(default_factory=lambda: SUPABASE_SESSION_POOLER_USER_TEMPLATE.format(project_ref=os.getenv("SUPABASE_PROJECT_REF", "YOUR_PROJECT_REF_HERE")))
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = SUPABASE_SESSION_POOLER_HOST
    POSTGRES_PORT: int = SUPABASE_SESSION_POOLER_PORT_INT
    POSTGRES_DB: str = SUPABASE_DEFAULT_DB

    # --- Milvus ---
    # Usar el default corregido
    MILVUS_URI: AnyHttpUrl = AnyHttpUrl(MILVUS_K8S_DEFAULT_URI)
    MILVUS_COLLECTION_NAME: str = "document_chunks_haystack"
    # *** CORRECCIÓN: Asegurar que estos nombres coincidan con cómo ingest-service los guardó en Milvus ***
    # (Los nombres por defecto de milvus-haystack suelen ser 'content' y 'embedding')
    MILVUS_EMBEDDING_FIELD: str = "embedding" # Mantener si ingest usó este nombre
    MILVUS_CONTENT_FIELD: str = "content"     # Mantener si ingest usó este nombre
    MILVUS_METADATA_FIELDS: List[str] = Field(default=[
        "company_id", "document_id", "file_name", "file_type",
    ])
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default={
        "metric_type": "COSINE", "params": {"ef": 128}
    })
    MILVUS_COMPANY_ID_FIELD: str = "company_id" # Campo usado para filtrar

    # --- OpenAI Embedding ---
    OPENAI_API_KEY: SecretStr
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMENSION: int = 1536

    # --- Gemini LLM ---
    GEMINI_API_KEY: SecretStr
    GEMINI_MODEL_NAME: str = "gemini-1.5-flash-latest"

    # --- RAG Pipeline Settings ---
    RETRIEVER_TOP_K: int = 5
    RAG_PROMPT_TEMPLATE: str = DEFAULT_RAG_PROMPT_TEMPLATE
    MAX_PROMPT_TOKENS: Optional[int] = 7000

    # --- Service Client Config ---
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 2
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    # --- Validators ---
    # (Sin cambios en validadores)
    @validator("EMBEDDING_DIMENSION", pre=True, always=True)
    def set_embedding_dimension(cls, v: Optional[int], values: dict[str, Any]) -> int:
        model = values.get("OPENAI_EMBEDDING_MODEL")
        if model == "text-embedding-3-large": return 3072
        elif model in ["text-embedding-3-small", "text-embedding-ada-002"]: return 1536
        if v is None or v == 0:
            return 1536
        return v

    @validator("POSTGRES_USER", pre=True, always=True)
    def check_postgres_user(cls, v: str) -> str:
        if "YOUR_PROJECT_REF_HERE" in v:
            print("WARNING: SUPABASE_PROJECT_REF environment variable not set. Using placeholder for POSTGRES_USER.")
        return v

# --- Instancia Global ---
# (Sin cambios en la lógica de instanciación)
try:
    settings = Settings()
    print("DEBUG: Query Service Settings loaded successfully.")
    print(f"DEBUG: Using Postgres Server: {settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}")
    print(f"DEBUG: Using Postgres User: {settings.POSTGRES_USER}")
    print(f"DEBUG: Using Milvus URI: {settings.MILVUS_URI}") # Ahora mostrará la URI corregida
    print(f"DEBUG: Using Milvus Collection: {settings.MILVUS_COLLECTION_NAME}")
    print(f"DEBUG: Using OpenAI Embedding Model: {settings.OPENAI_EMBEDDING_MODEL} (Dim: {settings.EMBEDDING_DIMENSION})")
    print(f"DEBUG: Using Gemini Model: {settings.GEMINI_MODEL_NAME}")

except (ValidationError, ValueError) as e:
    error_details = ""
    if isinstance(e, ValidationError):
        try: error_details = f"\nValidation Errors:\n{e.json(indent=2)}"
        except Exception: error_details = f"\nRaw Errors: {e.errors()}"
    print(f"FATAL: Configuration validation failed:{error_details}\nOriginal Error: {e}")
    sys.exit(1)
except Exception as e:
    print(f"FATAL: Unexpected error during Settings instantiation:\n{e}")
    import traceback; traceback.print_exc()
    sys.exit(1)
```

## File: `app\core\logging_config.py`
```py
# ./app/core/logging_config.py
import logging
import sys
import structlog
from app.core.config import settings
import os

def setup_logging():
    """Configura el logging estructurado con structlog."""

    # Determine if running inside a known runner like Gunicorn/Uvicorn if needed
    # is_gunicorn_worker = "gunicorn" in sys.argv[0]

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    # Add caller info only in debug mode for performance
    if settings.LOG_LEVEL == "DEBUG":
         shared_processors.append(structlog.processors.CallsiteParameterAdder(
             {
                 structlog.processors.CallsiteParameter.FILENAME,
                 structlog.processors.CallsiteParameter.LINENO,
             }
         ))

    # Configure structlog processors for eventual output
    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Configure the formatter for stdlib logging handler
    formatter = structlog.stdlib.ProcessorFormatter(
        # These run ONCE per log event before formatting
        foreign_pre_chain=shared_processors,
        # These run on the final structured dict before rendering
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(), # Render logs as JSON
        ],
    )

    # Configure root logger handler (usually StreamHandler to stdout)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()

    # Avoid adding handler multiple times if already configured (e.g., by Uvicorn/Gunicorn)
    # Check if a handler with our specific formatter already exists
    handler_exists = any(isinstance(h, logging.StreamHandler) and isinstance(h.formatter, structlog.stdlib.ProcessorFormatter) for h in root_logger.handlers)
    if not handler_exists:
        # Clear existing handlers if we are sure we want to replace them
        # Be cautious with this in production if other libraries add handlers
        # root_logger.handlers.clear()
        root_logger.addHandler(handler)
        root_logger.setLevel(settings.LOG_LEVEL.upper())
    else:
        # Update level of existing handler if necessary
        root_logger.setLevel(settings.LOG_LEVEL.upper())


    # Silence verbose libraries
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("gunicorn").setLevel(logging.INFO)
    logging.getLogger("asyncpg").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("google.generativeai").setLevel(logging.INFO)
    logging.getLogger("haystack").setLevel(logging.INFO) # Or DEBUG for more Haystack details
    logging.getLogger("milvus_haystack").setLevel(logging.INFO)

    log = structlog.get_logger("query_service") # Specific logger name
    log.info("Logging configured", log_level=settings.LOG_LEVEL)
```

## File: `app\db\__init__.py`
```py
# ./app/db/__init__.py
# (Puede estar vacío o importar selectivamente)
from .postgres_client import get_db_pool, close_db_pool, log_query_interaction

__all__ = ["get_db_pool", "close_db_pool", "log_query_interaction"]
```

## File: `app\db\postgres_client.py`
```py
# ./app/db/postgres_client.py
import uuid
from typing import Any, Optional, Dict, List
import asyncpg
import structlog
import json
from datetime import datetime

from app.core.config import settings
# No domain models needed here for logging

log = structlog.get_logger(__name__)

_pool: Optional[asyncpg.Pool] = None

async def get_db_pool() -> asyncpg.Pool:
    """Obtiene o crea el pool de conexiones a la base de datos (Supabase)."""
    global _pool
    if _pool is None or _pool._closed:
        try:
            log.info("Creating Supabase/PostgreSQL connection pool...",
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
                # Setup to automatically encode/decode JSONB
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
             log.error("Network/OS error creating Supabase/PostgreSQL connection pool", error=str(e), host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT, exc_info=True)
             raise
        except asyncpg.exceptions.InvalidPasswordError:
             log.error("Invalid password for Supabase/PostgreSQL connection", user=settings.POSTGRES_USER)
             raise
        except Exception as e:
            log.error("Failed to create Supabase/PostgreSQL connection pool", error=str(e), exc_info=True)
            raise
    return _pool

async def close_db_pool():
    """Cierra el pool de conexiones."""
    global _pool
    if _pool and not _pool._closed:
        log.info("Closing Supabase/PostgreSQL connection pool...")
        await _pool.close()
        _pool = None
        log.info("Supabase/PostgreSQL connection pool closed.")

async def log_query_interaction(
    company_id: uuid.UUID,
    user_id: Optional[uuid.UUID], # User ID might be optional depending on auth setup
    query: str,
    response: str,
    retrieved_doc_ids: List[str],
    retrieved_doc_scores: List[float],
    metadata: Optional[Dict[str, Any]] = None
) -> uuid.UUID:
    """
    Registra una interacción de consulta en la tabla QUERY_LOGS.

    Args:
        company_id: ID de la empresa.
        user_id: ID del usuario que realizó la consulta (puede ser None).
        query: Texto de la consulta del usuario.
        response: Texto de la respuesta generada por el LLM.
        retrieved_doc_ids: Lista de IDs de los documentos/chunks recuperados por el retriever.
        retrieved_doc_scores: Lista de scores de los documentos/chunks recuperados.
        metadata: Diccionario opcional con información adicional (tiempos, modelo usado, etc.).

    Returns:
        El ID del registro de log creado.
    """
    pool = await get_db_pool()
    log_id = uuid.uuid4()

    # Preparar metadatos para JSONB
    log_metadata = metadata or {}
    log_metadata["retrieved_documents"] = [
        {"id": doc_id, "score": score}
        for doc_id, score in zip(retrieved_doc_ids, retrieved_doc_scores)
    ]
    log_metadata["llm_model"] = settings.GEMINI_MODEL_NAME
    log_metadata["embedding_model"] = settings.OPENAI_EMBEDDING_MODEL

    # Calcular un score promedio de relevancia (opcional, podría ser más complejo)
    avg_relevance_score = sum(retrieved_doc_scores) / len(retrieved_doc_scores) if retrieved_doc_scores else None

    query_sql = """
        INSERT INTO query_logs
               (id, company_id, user_id, query, response, relevance_score, metadata, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, NOW() AT TIME ZONE 'UTC')
        RETURNING id;
    """

    try:
        async with pool.acquire() as connection:
            result = await connection.fetchval(
                query_sql,
                log_id,
                company_id,
                user_id, # Puede ser NULL si user_id es None
                query,
                response,
                avg_relevance_score, # Puede ser NULL
                log_metadata # Directamente el diccionario, asyncpg lo codifica a JSONB
            )
        if result:
            log.info("Query interaction logged successfully", query_log_id=str(result), company_id=str(company_id), user_id=str(user_id) if user_id else "N/A")
            return result
        else:
             log.error("Failed to log query interaction, no ID returned.", log_id_attempted=str(log_id))
             raise RuntimeError("Failed to log query interaction.")
    except Exception as e:
        log.error("Failed to log query interaction to Supabase",
                  error=str(e), log_id_attempted=str(log_id), company_id=str(company_id),
                  exc_info=True)
        # Podríamos decidir no lanzar excepción aquí para no fallar la respuesta al usuario,
        # pero es importante saber que el log falló. Por ahora, la lanzamos.
        raise

async def check_db_connection() -> bool:
    """Verifica si se puede establecer una conexión básica con la BD."""
    pool = None
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
        return True
    except Exception:
        return False
    finally:
        # No cerramos el pool aquí, solo liberamos la conexión adquirida
        pass
```

## File: `app\main.py`
```py
# ./app/main.py
from fastapi import FastAPI, HTTPException, status as fastapi_status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import structlog
import uvicorn
import logging
import sys
import asyncio

# Importar configuraciones y logging primero
from app.core.config import settings
from app.core.logging_config import setup_logging

# Configurar logging ANTES de importar cualquier otra cosa que loguee
setup_logging()
log = structlog.get_logger(__name__)

# Importar routers y otros módulos después de configurar logging
from app.api.v1.endpoints import query
from app.db import postgres_client
# *** CORRECCIÓN: Importar explícitamente desde rag_pipeline ***
from app.pipelines.rag_pipeline import build_rag_pipeline, check_pipeline_dependencies
from app.api.v1 import schemas # Importar schemas para health check

# Estado global para verificar dependencias críticas al inicio
SERVICE_READY = False
# Almacenar el pipeline construido globalmente si se decide inicializar en startup
GLOBAL_RAG_PIPELINE = None

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.1.0",
    description="Microservice to handle user queries using a Haystack RAG pipeline with Milvus and Gemini.",
)

# --- Event Handlers ---
@app.on_event("startup")
async def startup_event():
    global SERVICE_READY, GLOBAL_RAG_PIPELINE
    log.info(f"Starting up {settings.PROJECT_NAME}...")
    db_pool_initialized = False
    pipeline_built = False

    # 1. Inicializar Pool de Base de Datos (Supabase)
    try:
        await postgres_client.get_db_pool()
        db_ready = await postgres_client.check_db_connection()
        if db_ready:
            log.info("PostgreSQL connection pool initialized and connection verified.")
            db_pool_initialized = True
        else:
            log.critical("CRITICAL: Failed to verify PostgreSQL connection on startup.")
    except Exception as e:
        log.critical("CRITICAL: Failed to establish essential PostgreSQL connection pool on startup.", error=str(e), exc_info=True)

    # 2. Construir el Pipeline Haystack
    if db_pool_initialized:
        try:
            # *** CORRECCIÓN: Usar la función importada correctamente ***
            GLOBAL_RAG_PIPELINE = build_rag_pipeline()
            dep_status = await check_pipeline_dependencies()
            if dep_status.get("milvus_connection") == "ok":
                 log.info("Haystack RAG pipeline built and Milvus connection verified.")
                 pipeline_built = True
            else:
                 log.warning("Haystack RAG pipeline built, but Milvus check failed.", milvus_status=dep_status.get("milvus_connection"))
                 pipeline_built = True # Marcar como construido, pero health check lo refinará
        except Exception as e:
            log.error("Failed to build Haystack RAG pipeline during startup.", error=str(e), exc_info=True)

    # Marcar servicio como listo SOLO si las dependencias MÍNIMAS (DB) están OK
    if db_pool_initialized:
        SERVICE_READY = True
        log.info(f"{settings.PROJECT_NAME} startup sequence completed. Service marked as READY (DB OK). Pipeline status: {'Built' if pipeline_built else 'Build Failed'}")
    else:
        SERVICE_READY = False
        log.critical(f"{settings.PROJECT_NAME} startup failed. Essential DB connection could not be established. Service marked as NOT READY.")
        # sys.exit(1) # Podría forzar salida


@app.on_event("shutdown")
async def shutdown_event():
    log.info(f"Shutting down {settings.PROJECT_NAME}...")
    await postgres_client.close_db_pool()
    log.info("PostgreSQL connection pool closed.")
    log.info(f"{settings.PROJECT_NAME} shutdown complete.")

# --- Exception Handlers (Sin cambios) ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    log_level = log.warning if exc.status_code < 500 and exc.status_code != 404 else log.error
    log_level("HTTP Exception caught", status_code=exc.status_code, detail=exc.detail, path=str(request.url))
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    log.warning("Request Validation Error", errors=exc.errors(), path=str(request.url))
    error_details = [{"loc": err.get("loc"), "msg": err.get("msg"), "type": err.get("type")} for err in exc.errors()]
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Validation Error", "errors": error_details},
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    log.exception("Unhandled Exception caught", path=str(request.url))
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal server error occurred."},
    )


# --- Routers ---
app.include_router(query.router, prefix=settings.API_V1_STR, tags=["Query"])

# --- Root Endpoint / Health Check ---
@app.get(
    "/",
    tags=["Health Check"],
    response_model=schemas.HealthCheckResponse,
    summary="Service Health Check"
)
async def read_root():
    """
    Health check endpoint. Checks service readiness and critical dependencies like Database and Milvus.
    Returns 503 Service Unavailable if the service is not ready or dependencies are down.
    """
    global SERVICE_READY
    log.debug("Root endpoint accessed (health check)")

    if not SERVICE_READY:
         log.warning("Health check failed: Service did not start successfully (SERVICE_READY is False).")
         raise HTTPException(
             status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
             detail="Service is not ready, essential connections likely failed during startup."
         )

    # Realizar chequeos activos de dependencias
    db_status = "pending"
    milvus_status = "pending"
    overall_ready = True

    # Chequeo activo de la base de datos
    try:
        db_ok = await postgres_client.check_db_connection()
        db_status = "ok" if db_ok else "error: Connection failed"
        if not db_ok: overall_ready = False; log.error("Health check failed: DB ping failed.")
    except Exception as db_err:
        db_status = f"error: {type(db_err).__name__}"
        overall_ready = False
        log.error("Health check failed: DB ping error.", error=str(db_err))

    # Chequeo activo de Milvus
    try:
        # *** CORRECCIÓN: Usar la función importada correctamente ***
        milvus_dep_status = await check_pipeline_dependencies()
        milvus_status = milvus_dep_status.get("milvus_connection", "error: Check failed")
        if "error" in milvus_status:
            log.warning("Health check: Milvus check indicates an issue.", status=milvus_status)
            # Considerar si Milvus es crítico para 'ready'
            # overall_ready = False
    except Exception as milvus_err:
        milvus_status = f"error: {type(milvus_err).__name__}"
        log.error("Health check failed: Milvus check error.", error=str(milvus_err))
        # overall_ready = False

    http_status = fastapi_status.HTTP_200_OK if overall_ready else fastapi_status.HTTP_503_SERVICE_UNAVAILABLE

    response_data = schemas.HealthCheckResponse(
        status="ok" if overall_ready else "error",
        service=settings.PROJECT_NAME,
        ready=overall_ready,
        dependencies={
            "database": db_status,
            "vector_store": milvus_status
        }
    )

    if not overall_ready:
         log.warning("Health check determined service is not ready.", dependencies=response_data.dependencies)
         raise HTTPException(status_code=http_status, detail=response_data.model_dump())
    else:
         log.info("Health check successful.", dependencies=response_data.dependencies)
         return response_data


# --- Main execution (for local development) ---
if __name__ == "__main__":
    log.info(f"Starting Uvicorn server for {settings.PROJECT_NAME} local development...")
    log_level_str = settings.LOG_LEVEL.lower()
    if log_level_str not in logging._nameToLevel:
        log.warning(f"Invalid LOG_LEVEL '{settings.LOG_LEVEL}', defaulting Uvicorn log level to 'info'.")
        log_level_str = "info"

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8001,
        reload=True,
        log_level=log_level_str,
    )
```

## File: `app\models\__init__.py`
```py

```

## File: `app\pipelines\__init__.py`
```py
# ./app/pipelines/__init__.py
from .rag_pipeline import build_rag_pipeline, run_rag_pipeline

__all__ = ["build_rag_pipeline", "run_rag_pipeline"]
```

## File: `app\pipelines\rag_pipeline.py`
```py
# ./app/pipelines/rag_pipeline.py
import structlog
import asyncio
import uuid
from typing import Dict, Any, List, Tuple, Optional

from haystack import Pipeline, Document
from haystack.components.embedders import OpenAITextEmbedder
from haystack.components.builders.prompt_builder import PromptBuilder
from milvus_haystack import MilvusDocumentStore, MilvusEmbeddingRetriever
from haystack.utils import Secret

from app.core.config import settings
from app.db import postgres_client
from app.services.gemini_client import gemini_client

log = structlog.get_logger(__name__)

# --- Component Initialization Functions ---

def get_milvus_document_store() -> MilvusDocumentStore:
    """Initializes the MilvusDocumentStore connection."""
    log.debug("Initializing MilvusDocumentStore for Query Service",
             connection_uri=str(settings.MILVUS_URI),
             collection=settings.MILVUS_COLLECTION_NAME,
             # Ya no se loguean los campos aquí porque no se pasan al constructor
             search_params=settings.MILVUS_SEARCH_PARAMS)
    try:
        # *** CORRECCIÓN: Eliminar embedding_field, content_field, metadata_fields ***
        store = MilvusDocumentStore(
            connection_args={"uri": str(settings.MILVUS_URI)},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            # Los siguientes campos NO son argumentos válidos para el constructor:
            # embedding_field=settings.MILVUS_EMBEDDING_FIELD,
            # content_field=settings.MILVUS_CONTENT_FIELD,
            # metadata_fields=settings.MILVUS_METADATA_FIELDS,
            search_params=settings.MILVUS_SEARCH_PARAMS, # Parámetros de búsqueda sí son válidos
            consistency_level="Strong",                 # Nivel de consistencia sí es válido
        )
        log.info("MilvusDocumentStore initialized successfully")
        return store
    except Exception as e:
        log.error("Failed to initialize MilvusDocumentStore", error=str(e), exc_info=True)
        raise RuntimeError(f"Could not initialize Milvus Document Store: {e}") from e


def get_openai_text_embedder() -> OpenAITextEmbedder:
    """Initializes the OpenAI Embedder for text (queries)."""
    log.debug("Initializing OpenAITextEmbedder", model=settings.OPENAI_EMBEDDING_MODEL)
    api_key_secret = Secret.from_env_var("QUERY_OPENAI_API_KEY")
    if not api_key_secret.resolve_value():
         log.warning("QUERY_OPENAI_API_KEY environment variable not found or empty for OpenAI Embedder.")

    return OpenAITextEmbedder(
        api_key=api_key_secret,
        model=settings.OPENAI_EMBEDDING_MODEL,
    )

def get_milvus_retriever(document_store: MilvusDocumentStore) -> MilvusEmbeddingRetriever:
    """Initializes the MilvusEmbeddingRetriever."""
    log.debug("Initializing MilvusEmbeddingRetriever")
    # El retriever usará los nombres de campo por defecto ('embedding', 'content')
    # o los configurados en la colección Milvus. No necesita pasarlos explícitamente aquí
    # a menos que quieras sobreescribir los defaults del store/colección.
    return MilvusEmbeddingRetriever(
        document_store=document_store,
        # top_k y filters se pasarán dinámicamente
    )

def get_prompt_builder() -> PromptBuilder:
    """Initializes the PromptBuilder with the RAG template."""
    log.debug("Initializing PromptBuilder", template_preview=settings.RAG_PROMPT_TEMPLATE[:100] + "...")
    return PromptBuilder(template=settings.RAG_PROMPT_TEMPLATE)

# --- Pipeline Construction ---

_rag_pipeline_instance: Optional[Pipeline] = None

def build_rag_pipeline() -> Pipeline:
    """
    Builds the Haystack RAG pipeline by initializing and connecting components.
    Caches the pipeline instance globally after the first successful build.
    """
    global _rag_pipeline_instance
    if _rag_pipeline_instance:
        log.debug("Returning existing RAG pipeline instance.")
        return _rag_pipeline_instance

    log.info("Building Haystack RAG pipeline...")
    rag_pipeline = Pipeline()

    try:
        # 1. Initialize components (get_milvus_document_store ya corregido)
        doc_store = get_milvus_document_store()
        text_embedder = get_openai_text_embedder()
        retriever = get_milvus_retriever(document_store=doc_store)
        prompt_builder = get_prompt_builder()

        # 2. Add components
        rag_pipeline.add_component("text_embedder", text_embedder)
        rag_pipeline.add_component("retriever", retriever)
        rag_pipeline.add_component("prompt_builder", prompt_builder)

        # 3. Connect components
        rag_pipeline.connect("text_embedder.embedding", "retriever.query_embedding")
        rag_pipeline.connect("retriever.documents", "prompt_builder.documents")

        log.info("Haystack RAG pipeline built successfully.")
        _rag_pipeline_instance = rag_pipeline
        return rag_pipeline

    except Exception as e:
        log.error("Failed to build Haystack RAG pipeline", error=str(e), exc_info=True)
        raise RuntimeError("Could not build the RAG pipeline") from e


# --- Pipeline Execution ---
# (El resto de la función run_rag_pipeline y check_pipeline_dependencies no necesita cambios
#  respecto a la versión anterior, ya que el error estaba en get_milvus_document_store)
async def run_rag_pipeline(
    query: str,
    company_id: str,
    user_id: Optional[str],
    top_k: Optional[int] = None
) -> Tuple[str, List[Document], Optional[uuid.UUID]]:
    """
    Runs the RAG pipeline for a given query and company_id.
    """
    run_log = log.bind(query=query, company_id=company_id, user_id=user_id or "N/A")
    run_log.info("Running RAG pipeline...")

    try:
        pipeline = build_rag_pipeline()
    except Exception as build_err:
         run_log.error("Failed to get or build RAG pipeline for execution", error=str(build_err))
         raise HTTPException(status_code=503, detail="RAG pipeline is not available.")


    retriever_top_k = top_k if top_k is not None else settings.RETRIEVER_TOP_K
    retriever_filters = {settings.MILVUS_COMPANY_ID_FIELD: company_id}
    run_log.debug("Retriever filters prepared", filters=retriever_filters, top_k=retriever_top_k)

    pipeline_input = {
        "text_embedder": {"text": query},
        "retriever": {"filters": retriever_filters, "top_k": retriever_top_k},
        "prompt_builder": {"query": query}
    }
    run_log.debug("Pipeline input prepared", input_data=pipeline_input)

    try:
        loop = asyncio.get_running_loop()
        pipeline_result = await loop.run_in_executor(
            None,
            lambda: pipeline.run(pipeline_input, include_outputs_from=["retriever", "prompt_builder"])
        )
        run_log.info("Haystack pipeline (embed, retrieve, prompt) executed successfully.")

        retrieved_docs: List[Document] = pipeline_result.get("retriever", {}).get("documents", [])
        prompt_builder_output = pipeline_result.get("prompt_builder", {})
        generated_prompt: Optional[str] = None

        if "prompt" in prompt_builder_output:
             prompt_data = prompt_builder_output["prompt"]
             if isinstance(prompt_data, list):
                 text_parts = [msg.content for msg in prompt_data if hasattr(msg, 'content') and isinstance(msg.content, str)]
                 generated_prompt = "\n".join(text_parts)
             elif isinstance(prompt_data, str):
                  generated_prompt = prompt_data
             else:
                  run_log.warning("Unexpected prompt format from prompt_builder", prompt_type=type(prompt_data))
                  generated_prompt = str(prompt_data)

        if not retrieved_docs:
            run_log.warning("No relevant documents found by retriever.")
            if not generated_prompt:
                 generated_prompt = f"Pregunta: {query}\n\nNo se encontraron documentos relevantes. Intenta responder brevemente si es posible, o indica que no tienes información."

        if not generated_prompt:
             run_log.error("Failed to extract or generate prompt from pipeline output", output=prompt_builder_output)
             raise ValueError("Could not construct prompt for LLM.")

        run_log.debug("Generated prompt for LLM", prompt_preview=generated_prompt[:200] + "...")

        answer = await gemini_client.generate_answer(generated_prompt)
        run_log.info("Answer generated by Gemini", answer_preview=answer[:100] + "...")

        log_id: Optional[uuid.UUID] = None
        try:
            doc_ids = [doc.id for doc in retrieved_docs]
            doc_scores = [doc.score for doc in retrieved_docs if doc.score is not None]
            user_uuid = uuid.UUID(user_id) if user_id else None
            log_id = await postgres_client.log_query_interaction(
                company_id=uuid.UUID(company_id),
                user_id=user_uuid,
                query=query,
                response=answer,
                retrieved_doc_ids=doc_ids,
                retrieved_doc_scores=doc_scores,
                metadata={"retriever_top_k": retriever_top_k}
            )
        except Exception as log_err:
             run_log.error("Failed to log query interaction to database", error=str(log_err), exc_info=True)

        return answer, retrieved_docs, log_id

    except Exception as e:
        run_log.exception("Error occurred during RAG pipeline execution")
        raise HTTPException(status_code=500, detail=f"Error processing query: {type(e).__name__}")


async def check_pipeline_dependencies() -> Dict[str, str]:
    """Checks critical dependencies for the pipeline (e.g., Milvus)."""
    results = {"milvus_connection": "pending"}
    try:
        store = get_milvus_document_store() # Ahora debería funcionar o lanzar error claro
        count = await asyncio.to_thread(store.count_documents)
        results["milvus_connection"] = "ok"
        log.debug("Milvus dependency check successful (count documents)", count=count)
    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        results["milvus_connection"] = f"error: {error_msg[:100]}"
        log.warning("Milvus dependency check failed", error=error_msg, exc_info=False)
    return results
```

## File: `app\services\__init__.py`
```py
# ./app/services/__init__.py
from .base_client import BaseServiceClient
from .gemini_client import GeminiClient

__all__ = ["BaseServiceClient", "GeminiClient"]
```

## File: `app\services\base_client.py`
```py
# ./app/services/base_client.py
import httpx
# Importar los objetos necesarios de tenacity
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, retry_if_exception
import structlog
from typing import Any, Dict, Optional

# Asegurarnos de importar Settings desde la ubicación correcta
from app.core.config import settings

log = structlog.get_logger(__name__)

class BaseServiceClient:
    """Cliente HTTP base asíncrono con reintentos."""

    def __init__(self, base_url: str, service_name: str):
        self.base_url = base_url
        self.service_name = service_name
        # Usar httpx.AsyncClient para operaciones asíncronas
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=settings.HTTP_CLIENT_TIMEOUT
        )
        log.debug(f"{self.service_name} client initialized", base_url=base_url)

    async def close(self):
        """Cierra el cliente HTTP asíncrono."""
        await self.client.aclose()
        log.info(f"{self.service_name} client closed.")

    # Decorador de reintentos usando tenacity
    # *** SECCIÓN CORREGIDA ***
    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES + 1), # +1 porque el primer intento cuenta
        wait=wait_exponential(multiplier=1, min=settings.HTTP_CLIENT_BACKOFF_FACTOR, max=10),
        # CORRECCIÓN: Combinar las condiciones de reintento correctamente
        retry=(
            # Reintentar en errores básicos de red/timeout
            retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)) |
            # Reintentar SI la excepción es HTTPStatusError Y el código es >= 500
            retry_if_exception(
                lambda e: isinstance(e, httpx.HTTPStatusError) and e.response.status_code >= 500
            )
        ),
        reraise=True, # Vuelve a lanzar la excepción después de los reintentos si todos fallan
        before_sleep=lambda retry_state: log.warning(
            f"Retrying {self.service_name} request",
            # Intentar obtener detalles del request si están disponibles en args
            method=getattr(retry_state.args[0], 'method', 'N/A') if retry_state.args else 'N/A',
            endpoint=getattr(retry_state.args[0], 'url', 'N/A') if retry_state.args else 'N/A',
            attempt=retry_state.attempt_number,
            wait_time=f"{retry_state.next_action.sleep:.2f}s",
            error=str(retry_state.outcome.exception()) # Mostrar el error
        )
    )
    # *** FIN SECCIÓN CORREGIDA ***
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None, # Renombrado para claridad
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        """Realiza una petición HTTP asíncrona con reintentos."""
        request_log = log.bind(service=self.service_name, method=method, endpoint=endpoint)
        request_log.debug("Sending request...")
        try:
            response = await self.client.request(
                method,
                endpoint,
                params=params,
                json=json_data, # Usar el parámetro renombrado
                data=data,
                files=files,
                headers=headers
            )
            # Lanzar excepción para códigos de error HTTP (4xx, 5xx)
            response.raise_for_status()
            request_log.debug("Request successful", status_code=response.status_code)
            return response
        except httpx.HTTPStatusError as e:
            # Loguear error pero permitir que Tenacity decida si reintentar (para 5xx) o fallar (para 4xx)
            log_level = log.warning if e.response.status_code < 500 else log.error
            log_level(
                "Request failed with HTTP status code",
                status_code=e.response.status_code,
                response_text=e.response.text[:500], # Limitar longitud del texto de respuesta
                exc_info=False # No es necesario el traceback completo para errores HTTP esperados
            )
            raise # Re-lanzar para que Tenacity la maneje
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            request_log.error(
                "Request failed due to network/timeout issue",
                error_type=type(e).__name__,
                error_details=str(e),
                exc_info=False # Traceback puede ser útil aquí, pero puede ser verboso
            )
            raise # Re-lanzar para que Tenacity la maneje
        except Exception as e:
             # Capturar cualquier otra excepción inesperada
             request_log.exception("An unexpected error occurred during request")
             raise # Re-lanzar la excepción inesperada
```

## File: `app\services\gemini_client.py`
```py
# ./app/services/gemini_client.py
import google.generativeai as genai
import structlog
from typing import Optional

from app.core.config import settings
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

log = structlog.get_logger(__name__)

class GeminiClient:
    """Cliente para interactuar con la API de Google Gemini."""

    def __init__(self):
        self.api_key = settings.GEMINI_API_KEY.get_secret_value()
        self.model_name = settings.GEMINI_MODEL_NAME
        try:
            genai.configure(api_key=self.api_key)
            self.model = genai.GenerativeModel(self.model_name)
            log.info("Gemini client configured successfully", model_name=self.model_name)
        except Exception as e:
            log.error("Failed to configure Gemini client", error=str(e), exc_info=True)
            # Podríamos fallar aquí o permitir que falle en la primera llamada
            self.model = None # Marcar como no inicializado

    # Añadir reintentos para errores comunes de API (ej: RateLimitError, InternalServerError)
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((
            genai.types.generation_types.StopCandidateException, # Podría indicar filtro de seguridad
            genai.types.generation_types.BlockedPromptException, # Prompt bloqueado
            # Añadir errores específicos de google.api_core.exceptions si se usa cliente gRPC/REST directamente
            # Por ahora, asumimos que el SDK maneja algunos reintentos internos, pero añadimos básicos
            TimeoutError, # Si la llamada subyacente tiene timeout
            # Podríamos necesitar capturar errores más específicos de la librería subyacente
            # Ejemplo: google.api_core.exceptions.ResourceExhausted (Rate limit)
            # Ejemplo: google.api_core.exceptions.InternalServerError
        )),
        reraise=True,
        before_sleep=lambda retry_state: log.warning(
            "Retrying Gemini API call",
            attempt=retry_state.attempt_number,
            wait_time=f"{retry_state.next_action.sleep:.2f}s",
            error=str(retry_state.outcome.exception())
        )
    )
    async def generate_answer(self, prompt: str) -> str:
        """
        Genera una respuesta usando el modelo Gemini configurado.

        Args:
            prompt: El prompt completo a enviar al modelo.

        Returns:
            La respuesta generada como string.

        Raises:
            ValueError: Si el cliente no se inicializó correctamente.
            Exception: Si la API de Gemini devuelve un error inesperado o no se puede extraer texto.
        """
        if not self.model:
            log.error("Gemini client not initialized. Cannot generate answer.")
            raise ValueError("Gemini client is not properly configured.")

        generate_log = log.bind(model_name=self.model_name, prompt_length=len(prompt))
        generate_log.info("Sending request to Gemini API...")

        try:
            # La llamada generate_content puede ser bloqueante, ejecutar en thread si es necesario
            # Por ahora, asumimos que es lo suficientemente rápida o se maneja en el event loop
            # response = await asyncio.to_thread(self.model.generate_content, prompt) # Opción si es bloqueante
            response = await self.model.generate_content_async(prompt) # Usar versión async si está disponible

            # Verificar si la respuesta fue bloqueada o no tiene contenido
            if not response.candidates or not hasattr(response.candidates[0], 'content') or not hasattr(response.candidates[0].content, 'parts'):
                 # Analizar finish_reason si está disponible
                 finish_reason = response.candidates[0].finish_reason if response.candidates else "UNKNOWN"
                 safety_ratings = response.candidates[0].safety_ratings if response.candidates else "N/A"
                 generate_log.warning("Gemini response blocked or empty", finish_reason=finish_reason, safety_ratings=str(safety_ratings))
                 # Devolver un mensaje indicando el bloqueo o vacío
                 return f"[Respuesta bloqueada por Gemini o vacía. Razón: {finish_reason}]"


            # Extraer el texto de la respuesta (asumiendo respuesta simple de texto)
            # La estructura puede variar, ajustar según sea necesario
            generated_text = "".join(part.text for part in response.candidates[0].content.parts)

            generate_log.info("Received response from Gemini API", response_length=len(generated_text))
            return generated_text.strip()

        except (genai.types.generation_types.BlockedPromptException, genai.types.generation_types.StopCandidateException) as security_err:
             generate_log.warning("Gemini request blocked due to safety settings or prompt content", error=str(security_err), finish_reason=getattr(security_err, 'finish_reason', 'N/A'))
             # Considerar devolver un mensaje específico o lanzar una excepción controlada
             return f"[Contenido bloqueado por Gemini: {type(security_err).__name__}]"
        except Exception as e:
            generate_log.exception("Error during Gemini API call")
            # Relanzar la excepción para que sea manejada por el endpoint
            raise

# Instancia global del cliente (opcional, podría instanciarse por request)
# Crear la instancia aquí puede ser beneficioso si la configuración es costosa
# Pero hay que tener cuidado con el estado compartido si el cliente no es thread-safe
# El cliente de google-generativeai debería ser seguro para usar así.
gemini_client = GeminiClient()

async def get_gemini_client() -> GeminiClient:
    """Dependency para inyectar el cliente Gemini."""
    # Podría añadir lógica aquí si la inicialización necesita ser por request
    # o si se necesita verificar el estado del cliente global.
    if not gemini_client.model:
         raise RuntimeError("Gemini client failed to initialize during startup.")
    return gemini_client
```

## File: `app\utils\__init__.py`
```py

```

## File: `app\utils\helpers.py`
```py
# ./app/utils/helpers.py
# (Actualmente vacío, añadir funciones de utilidad si son necesarias)
import structlog

log = structlog.get_logger(__name__)

# Ejemplo de función de utilidad potencial:
def truncate_text(text: str, max_length: int) -> str:
    """Trunca un texto a una longitud máxima, añadiendo puntos suspensivos."""
    if not text:
        return ""
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + "..."
```

## File: `pyproject.toml`
```toml
[tool.poetry]
name = "query-service"
version = "0.1.0"
description = "Query service for SaaS B2B using Haystack RAG"
authors = ["Nyro <dev@nyro.com>"]
readme = "README.md"

[tool.poetry.dependencies]
python = "^3.10"
fastapi = "^0.110.0"
uvicorn = {extras = ["standard"], version = "^0.28.0"}
gunicorn = "^21.2.0"
pydantic = {extras = ["email"], version = "^2.6.4"}
pydantic-settings = "^2.2.1"
httpx = "^0.27.0"
asyncpg = "^0.29.0"
python-jose = {extras = ["cryptography"], version = "^3.3.0"} # Para futura validación JWT si es necesario aquí
tenacity = "^8.2.3"
structlog = "^24.1.0"

# --- Haystack Dependencies ---
haystack-ai = "^2.0.1" # Core Haystack
openai = "^1.14.3"     # Para OpenAITextEmbedder
pymilvus = "^2.4.1"    # Cliente Milvus explícito
milvus-haystack = "^0.0.6" # Integración Milvus para Haystack 2.x

# --- Gemini Dependency ---
google-generativeai = "^0.5.4" # Cliente oficial de Google para Gemini

[tool.poetry.group.dev.dependencies]
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"
# httpx ya está en dependencias principales, no necesita repetirse aquí para test client

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
