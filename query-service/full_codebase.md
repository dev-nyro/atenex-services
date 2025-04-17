# Estructura de la Codebase

```
app/
├── api
│   └── v1
│       ├── endpoints
│       │   ├── __init__.py
│       │   ├── chat.py
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

## File: `app\api\v1\endpoints\chat.py`
```py
# query-service/app/api/v1/endpoints/chat.py
import uuid
from typing import List, Optional
import structlog
from fastapi import (
    APIRouter, Depends, HTTPException, status, Path, Query, Header, Request,
    Response # Importar Response para el 204
)

from app.api.v1 import schemas
from app.db import postgres_client

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Dependencias para usar cabeceras X-* ---
async def get_current_company_id(x_company_id: Optional[str] = Header(None, alias="X-Company-ID")) -> uuid.UUID:
    """Obtiene y valida el Company ID de la cabecera X-Company-ID."""
    if not x_company_id:
        log.warning("Missing required X-Company-ID header")
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

# --- Endpoints Modificados para USAR las nuevas dependencias ---

@router.get(
    "/chats",
    response_model=List[schemas.ChatSummary],
    status_code=status.HTTP_200_OK,
    summary="List User Chats",
    description="Retrieves a list of chat summaries using X-Company-ID and X-User-ID headers.",
)
async def list_chats(
    user_id: uuid.UUID = Depends(get_current_user_id),
    company_id: uuid.UUID = Depends(get_current_company_id),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    request: Request = None
):
    request_id = request.headers.get("x-request-id") if request else str(uuid.uuid4())
    endpoint_log = log.bind(request_id=request_id, user_id=str(user_id), company_id=str(company_id), limit=limit, offset=offset)
    endpoint_log.info("Request received to list chats")

    try:
        # *** LLAMADA CORREGIDA ***
        chats = await postgres_client.get_user_chats(
            user_id=user_id, company_id=company_id, limit=limit, offset=offset
        )
        endpoint_log.info("Chats listed successfully", count=len(chats))
        return chats
    except Exception as e:
        endpoint_log.exception("Error listing chats")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve chat list.")


@router.get(
    "/chats/{chat_id}/messages",
    response_model=List[schemas.ChatMessage],
    status_code=status.HTTP_200_OK,
    summary="Get Chat Messages",
    description="Retrieves messages for a specific chat using X-Company-ID and X-User-ID headers.",
    responses={404: {"description": "Chat not found or access denied."}}
)
async def get_chat_messages_endpoint(
    chat_id: uuid.UUID = Path(..., description="The ID of the chat."),
    user_id: uuid.UUID = Depends(get_current_user_id),
    company_id: uuid.UUID = Depends(get_current_company_id),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    request: Request = None
):
    request_id = request.headers.get("x-request-id") if request else str(uuid.uuid4())
    endpoint_log = log.bind(request_id=request_id, user_id=str(user_id), company_id=str(company_id), chat_id=str(chat_id), limit=limit, offset=offset)
    endpoint_log.info("Request received to get chat messages")

    try:
        # *** LLAMADA CORREGIDA ***
        messages = await postgres_client.get_chat_messages(
            chat_id=chat_id, user_id=user_id, company_id=company_id, limit=limit, offset=offset
        )
        endpoint_log.info("Chat messages retrieved successfully", count=len(messages))
        return messages
    except Exception as e:
        endpoint_log.exception("Error getting chat messages")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve chat messages.")


@router.delete(
    "/chats/{chat_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Chat",
    description="Deletes a chat using X-Company-ID and X-User-ID headers.",
    responses={404: {"description": "Chat not found or access denied."}, 204: {}}
)
async def delete_chat_endpoint(
    chat_id: uuid.UUID = Path(..., description="The ID of the chat to delete."),
    user_id: uuid.UUID = Depends(get_current_user_id),
    company_id: uuid.UUID = Depends(get_current_company_id),
    request: Request = None
):
    request_id = request.headers.get("x-request-id") if request else str(uuid.uuid4())
    endpoint_log = log.bind(request_id=request_id, user_id=str(user_id), company_id=str(company_id), chat_id=str(chat_id))
    endpoint_log.info("Request received to delete chat")

    try:
        # Llamada a la función DB (nombre no cambia)
        deleted = await postgres_client.delete_chat(chat_id=chat_id, user_id=user_id, company_id=company_id)
        if deleted:
            endpoint_log.info("Chat deleted successfully")
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        else:
            endpoint_log.warning("Chat not found or access denied for deletion")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found or access denied.")
    except Exception as e:
        endpoint_log.exception("Error deleting chat")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to delete chat.")
```

## File: `app\api\v1\endpoints\query.py`
```py
# query-service/app/api/v1/endpoints/query.py
import uuid
from typing import Dict, Any, Optional, List
import structlog
import asyncio

from fastapi import APIRouter, Depends, HTTPException, status, Header, Body, Request

from app.api.v1 import schemas
from app.core.config import settings
from app.db import postgres_client
from app.pipelines import rag_pipeline
from haystack import Document
from app.utils.helpers import truncate_text
# Importar dependencias definidas en chat.py (o módulo compartido)
from .chat import get_current_company_id, get_current_user_id

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Endpoint Principal Modificado para usar dependencias de X- Headers ---
# *** RUTA CORREGIDA ***
@router.post(
    "/ask", # Ruta interna estandarizada a /ask
    response_model=schemas.QueryResponse,
    status_code=status.HTTP_200_OK,
    summary="Process a user query using RAG pipeline and manage chat history",
    description="Receives query via API Gateway. Uses X-Company-ID and X-User-ID. Continues or creates chat.",
)
async def process_query(
    request_body: schemas.QueryRequest = Body(...),
    # *** CORRECCIÓN CLAVE: Usar las dependencias importadas/definidas ***
    company_id: uuid.UUID = Depends(get_current_company_id),
    user_id: uuid.UUID = Depends(get_current_user_id),
    request: Request = None
):
    request_id = request.headers.get("x-request-id") if request else str(uuid.uuid4())
    endpoint_log = log.bind(
        request_id=request_id,
        company_id=str(company_id),
        user_id=str(user_id),
        query=truncate_text(request_body.query, 100),
        provided_chat_id=str(request_body.chat_id) if request_body.chat_id else "None"
    )
    endpoint_log.info("Received query request with chat context")

    current_chat_id: uuid.UUID
    is_new_chat = False

    try:
        # Lógica de Chat ID (sin cambios, ya usa los user_id/company_id de Depends)
        if request_body.chat_id:
            if not await postgres_client.check_chat_ownership(request_body.chat_id, user_id, company_id):
                 raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Chat not found or access denied.")
            current_chat_id = request_body.chat_id
            endpoint_log = endpoint_log.bind(chat_id=str(current_chat_id))
            endpoint_log.info("Continuing existing chat")
        else:
            endpoint_log.info("Creating a new chat...")
            initial_title = f"Chat: {truncate_text(request_body.query, 50)}"
            current_chat_id = await postgres_client.create_chat(user_id=user_id, company_id=company_id, title=initial_title)
            is_new_chat = True
            endpoint_log = endpoint_log.bind(chat_id=str(current_chat_id))
            endpoint_log.info("New chat created", title=initial_title)

        # Guardar Mensaje Usuario (sin cambios, usa user_id de Depends)
        endpoint_log.info("Saving user message...")
        await postgres_client.save_message(
            chat_id=current_chat_id, role='user', content=request_body.query
        )
        endpoint_log.info("User message saved")

        # Ejecutar Pipeline RAG (sin cambios, usa user_id/company_id de Depends)
        endpoint_log.info("Running RAG pipeline...")
        answer, retrieved_docs_haystack, log_id = await rag_pipeline.run_rag_pipeline(
            query=request_body.query, company_id=str(company_id), user_id=str(user_id),
            top_k=request_body.retriever_top_k, chat_id=current_chat_id
        )
        endpoint_log.info("RAG pipeline finished")

        # Formatear Documentos y Fuentes (sin cambios)
        response_docs_schema: List[schemas.RetrievedDocument] = []
        assistant_sources: List[Dict[str, Any]] = []
        for doc in retrieved_docs_haystack:
            schema_doc = schemas.RetrievedDocument.from_haystack_doc(doc)
            response_docs_schema.append(schema_doc)
            source_info = { "chunk_id": schema_doc.id, "document_id": schema_doc.document_id, "file_name": schema_doc.file_name, "score": schema_doc.score, "preview": schema_doc.content_preview }
            assistant_sources.append(source_info)

        # Guardar Mensaje Asistente (sin cambios)
        endpoint_log.info("Saving assistant message...")
        await postgres_client.save_message(
            chat_id=current_chat_id, role='assistant', content=answer,
            sources=assistant_sources if assistant_sources else None
        )
        endpoint_log.info("Assistant message saved")

        endpoint_log.info("Query processed successfully", log_id=str(log_id) if log_id else "Log Failed", num_retrieved=len(response_docs_schema))

        # Devolver Respuesta (sin cambios)
        return schemas.QueryResponse(
            answer=answer, retrieved_documents=response_docs_schema,
            query_log_id=log_id, chat_id=current_chat_id
        )

    # Manejo de Errores (sin cambios)
    except ValueError as ve: endpoint_log.warning("Value error", error=str(ve)); raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))
    except ConnectionError as ce: endpoint_log.error("Connection error", error=str(ce), exc_info=True); raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Dependency unavailable: {ce}")
    except HTTPException as http_exc: raise http_exc
    except Exception as e: endpoint_log.exception("Unhandled exception"); raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Internal error: {type(e).__name__}")
```

## File: `app\api\v1\schemas.py`
```py
# ./app/api/v1/schemas.py
import uuid
from pydantic import BaseModel, Field, validator
from typing import Optional, List, Dict, Any, Union
from datetime import datetime

# --- Chat Schemas ---

class ChatSummary(BaseModel):
    id: uuid.UUID = Field(..., description="Unique ID of the chat.")
    title: Optional[str] = Field(None, description="Title of the chat (may be null).")
    updated_at: datetime = Field(..., description="Timestamp of the last update (last message or creation).")

class ChatMessage(BaseModel):
    id: uuid.UUID = Field(..., description="Unique ID of the message.")
    role: str = Field(..., description="Role of the sender ('user' or 'assistant').")
    content: str = Field(..., description="Content of the message.")
    sources: Optional[List[Dict[str, Any]]] = Field(None, description="List of source documents cited by the assistant, if any.")
    created_at: datetime = Field(..., description="Timestamp when the message was created.")

class CreateMessageRequest(BaseModel):
    role: str = Field(..., description="Role of the sender ('user' or 'assistant'). Currently only 'user' expected from client.")
    content: str = Field(..., min_length=1, description="Content of the message.")

    @validator('role')
    def role_must_be_user_or_assistant(cls, v):
        if v not in ['user', 'assistant']:
            raise ValueError("Role must be either 'user' or 'assistant'")
        return v

# --- Request Schemas ---

class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, description="The user's query in natural language.")
    retriever_top_k: Optional[int] = Field(None, gt=0, le=20, description="Number of documents to retrieve (overrides server default).")
    # --- Añadir chat_id opcional ---
    chat_id: Optional[uuid.UUID] = Field(None, description="ID of the existing chat to continue, or null/omitted to start a new chat.")

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
    document_id: Optional[str] = Field(None, description="ID of the original source document.")
    file_name: Optional[str] = Field(None, description="Name of the original source file.")

    # Helper para convertir Document de Haystack a este schema si es necesario
    @classmethod
    def from_haystack_doc(cls, doc: Any): # Usar Any para evitar dependencia directa de Haystack aquí
        meta = doc.meta or {}
        return cls(
            id=doc.id,
            score=doc.score,
            content_preview=(doc.content[:150] + '...') if doc.content and len(doc.content) > 150 else doc.content,
            metadata=meta,
            document_id=meta.get("document_id"),
            file_name=meta.get("file_name")
        )


class QueryResponse(BaseModel):
    answer: str = Field(..., description="The final answer generated by the LLM based on the query and retrieved documents.")
    retrieved_documents: List[RetrievedDocument] = Field(default_factory=list, description="List of documents retrieved and used as context for the answer.")
    query_log_id: Optional[uuid.UUID] = Field(None, description="The unique ID of the logged interaction in the database (if logging was successful).")
    # --- Añadir chat_id ---
    chat_id: uuid.UUID = Field(..., description="The ID of the chat (either existing or newly created) this interaction belongs to.")

class HealthCheckDependency(BaseModel):
    status: str = Field(..., description="Status of the dependency ('ok', 'error', 'pending')")
    details: Optional[str] = Field(None, description="Additional details in case of error.")

class HealthCheckResponse(BaseModel):
    status: str = Field(default="ok", description="Overall status of the service.")
    service: str = Field(..., description="Name of the service.")
    ready: bool = Field(..., description="Indicates if the service is ready to serve requests.")
    dependencies: Dict[str, str]
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

# --- PostgreSQL Kubernetes Defaults ---
POSTGRES_K8S_HOST_DEFAULT = "postgres-service.nyro-develop.svc.cluster.local"
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "nyro"
POSTGRES_K8S_USER_DEFAULT = "postgres"

# --- Milvus Kubernetes Defaults ---
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
    POSTGRES_USER: str = POSTGRES_K8S_USER_DEFAULT
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = POSTGRES_K8S_HOST_DEFAULT
    POSTGRES_PORT: int = POSTGRES_K8S_PORT_DEFAULT
    POSTGRES_DB: str = POSTGRES_K8S_DB_DEFAULT

    # --- Milvus ---
    MILVUS_URI: AnyHttpUrl = AnyHttpUrl(MILVUS_K8S_DEFAULT_URI)
    MILVUS_COLLECTION_NAME: str = "document_chunks_haystack"
    MILVUS_EMBEDDING_FIELD: str = "embedding"
    MILVUS_CONTENT_FIELD: str = "content"
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
    @validator("EMBEDDING_DIMENSION", pre=True, always=True)
    def set_embedding_dimension(cls, v: Optional[int], values: dict[str, Any]) -> int:
        model = values.get("OPENAI_EMBEDDING_MODEL")
        if model == "text-embedding-3-large": return 3072
        elif model in ["text-embedding-3-small", "text-embedding-ada-002"]: return 1536
        if v is None or v == 0:
            return 1536
        return v

# --- Instancia Global ---
try:
    settings = Settings()
    # Mensajes de debug
    print("DEBUG: Settings loaded successfully.")
    print(f"DEBUG: Using Postgres Server: {settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}")
    print(f"DEBUG: Using Postgres User: {settings.POSTGRES_USER}")
    print(f"DEBUG: Using Milvus URI: {settings.MILVUS_URI}")
    print(f"DEBUG: Using OpenAI Model: {settings.OPENAI_EMBEDDING_MODEL}")
    print(f"DEBUG: Using Gemini Model: {settings.GEMINI_MODEL_NAME}")
except Exception as e:
    print(f"FATAL: Error loading settings: {e}")
    import traceback
    traceback.print_exc()
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
# query-service/app/db/postgres_client.py
import uuid
from typing import Any, Optional, Dict, List, Tuple
import asyncpg
import structlog
import json
from datetime import datetime, timezone

from app.core.config import settings
from app.api.v1 import schemas # Para type hints

log = structlog.get_logger(__name__)

_pool: Optional[asyncpg.Pool] = None

# --- Pool Management (sin cambios) ---
async def get_db_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None or _pool._closed:
        log.info("Creating PostgreSQL connection pool...",
                 host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT,
                 user=settings.POSTGRES_USER, db=settings.POSTGRES_DB)
        try:
            # Codecs para manejar JSON/JSONB automáticamente
            def _json_encoder(value): return json.dumps(value)
            def _json_decoder(value): return json.loads(value)
            async def init_connection(conn):
                await conn.set_type_codec('jsonb', encoder=_json_encoder, decoder=_json_decoder, schema='pg_catalog', format='text') # Añadir format='text' puede ayudar
                await conn.set_type_codec('json', encoder=_json_encoder, decoder=_json_decoder, schema='pg_catalog', format='text')  # Añadir format='text' puede ayudar

            _pool = await asyncpg.create_pool(
                user=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD.get_secret_value(),
                database=settings.POSTGRES_DB,
                host=settings.POSTGRES_SERVER,
                port=settings.POSTGRES_PORT,
                min_size=2, max_size=10, timeout=30.0, command_timeout=60.0,
                init=init_connection, # Asegurar que init se aplica
                statement_cache_size=0 # Deshabilitar cache para evitar problemas con codecs
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
    global _pool
    if _pool and not _pool._closed:
        log.info("Closing PostgreSQL connection pool..."); await _pool.close(); _pool = None; log.info("PostgreSQL connection pool closed.")
    elif _pool and _pool._closed: log.warning("Attempted to close an already closed PostgreSQL pool."); _pool = None
    else: log.info("No active PostgreSQL connection pool to close.")


async def check_db_connection() -> bool:
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            async with conn.transaction(): result = await conn.fetchval("SELECT 1")
        return result == 1
    except Exception as e: log.error("Database connection check failed", error=str(e)); return False

# --- Query Logging (sin cambios) ---
async def log_query_interaction(
    user_id: Optional[uuid.UUID],
    company_id: uuid.UUID,
    query: str,
    answer: str,
    retrieved_documents_data: List[Dict[str, Any]],
    metadata: Optional[Dict[str, Any]] = None,
    chat_id: Optional[uuid.UUID] = None,
) -> uuid.UUID:
    """Registra una interacción de consulta en query_logs."""
    pool = await get_db_pool()
    log_id = uuid.uuid4()
    log_entry = log.bind(log_id=str(log_id), company_id=str(company_id), chat_id=str(chat_id) if chat_id else "None")

    query_sql = """
    INSERT INTO query_logs (
        id, user_id, company_id, query, response,
        metadata, chat_id, created_at
    ) VALUES (
        $1, $2, $3, $4, $5, $6, $7, NOW() AT TIME ZONE 'UTC'
    ) RETURNING id;
    """
    log_metadata = metadata or {}
    log_metadata["retrieved_summary"] = [{"id": d.get("id"), "score": d.get("score"), "file_name": d.get("file_name")} for d in retrieved_documents_data]

    try:
        async with pool.acquire() as connection:
            result = await connection.fetchval(
                query_sql,
                log_id, user_id, company_id, query, answer,
                json.dumps(log_metadata), # Usar json.dumps aquí está bien
                chat_id
            )
        if not result or result != log_id:
            log_entry.error("Failed to create query log entry, ID mismatch or no ID returned", returned_id=result)
            raise RuntimeError("Failed to create query log entry")
        log_entry.info("Query interaction logged successfully")
        return log_id
    except Exception as e:
        log_entry.error("Failed to log query interaction", error=str(e), exc_info=True)
        # No relanzar como RuntimeError aquí necesariamente, podría ser manejado en el caller
        # Lanzamos de nuevo para que el endpoint sepa que falló
        raise RuntimeError(f"Failed to log query interaction: {e}") from e


# --- Funciones para gestión de chats ---

async def create_chat(user_id: uuid.UUID, company_id: uuid.UUID, title: Optional[str] = None) -> uuid.UUID:
    """Crea un nuevo chat."""
    pool = await get_db_pool()
    chat_id = uuid.uuid4()
    query = """
    INSERT INTO chats (id, user_id, company_id, title, created_at, updated_at)
    VALUES ($1, $2, $3, $4, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC') RETURNING id;
    """
    try:
        async with pool.acquire() as conn:
            result = await conn.fetchval(query, chat_id, user_id, company_id, title or f"Chat {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}")
        if result and result == chat_id:
            log.info("Chat created successfully", chat_id=str(chat_id), user_id=str(user_id), company_id=str(company_id))
            return chat_id
        else: raise RuntimeError("Failed to create chat, no ID returned")
    except Exception as e: log.error("Failed to create chat", error=str(e), exc_info=True); raise

async def get_user_chats(user_id: uuid.UUID, company_id: uuid.UUID, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Obtiene la lista de chats para un usuario/compañía (sumario)."""
    pool = await get_db_pool()
    query = """
    SELECT id, title, updated_at FROM chats
    WHERE user_id = $1 AND company_id = $2
    ORDER BY updated_at DESC LIMIT $3 OFFSET $4;
    """
    try:
        async with pool.acquire() as conn: rows = await conn.fetch(query, user_id, company_id, limit, offset)
        # Convertir asyncpg.Record a dict
        chats = [dict(row) for row in rows]
        log.info(f"Retrieved {len(chats)} chat summaries for user", user_id=str(user_id), company_id=str(company_id))
        return chats
    except Exception as e: log.error("Failed to get user chats", error=str(e), exc_info=True); raise

async def check_chat_ownership(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    """Verifica si un chat existe y pertenece al usuario/compañía."""
    pool = await get_db_pool()
    query = "SELECT EXISTS (SELECT 1 FROM chats WHERE id = $1 AND user_id = $2 AND company_id = $3);"
    try:
        async with pool.acquire() as conn: exists = await conn.fetchval(query, chat_id, user_id, company_id)
        return exists is True
    except Exception as e: log.error("Failed to check chat ownership", chat_id=str(chat_id), error=str(e), exc_info=True); return False # Asumir no propietario en caso de error

# *** FUNCIÓN CORREGIDA: get_chat_messages ***
async def get_chat_messages(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    """Obtiene mensajes de un chat, verificando propiedad y decodificando sources."""
    pool = await get_db_pool()
    owner = await check_chat_ownership(chat_id, user_id, company_id)
    if not owner:
        log.warning("Attempt to get messages for chat not owned or non-existent", chat_id=str(chat_id), user_id=str(user_id), company_id=str(company_id))
        return [] # Devolver lista vacía si no es propietario o no existe

    # Usar columna 'sources'
    messages_query = """
    SELECT id, role, content, sources, created_at FROM messages
    WHERE chat_id = $1 ORDER BY created_at ASC LIMIT $2 OFFSET $3;
    """
    try:
        async with pool.acquire() as conn:
            message_rows = await conn.fetch(messages_query, chat_id, limit, offset)

        messages = []
        for row in message_rows:
            msg_dict = dict(row)
            # *** CORRECCIÓN: Asegurar que 'sources' sea una lista Python ***
            # asyncpg con el codec debería devolver una lista/dict directamente si es JSONB válido.
            # Si devuelve una string (ej. '[]' o '{}'), intentamos cargarla.
            if isinstance(msg_dict.get('sources'), str):
                try:
                    msg_dict['sources'] = json.loads(msg_dict['sources'])
                except json.JSONDecodeError:
                    log.warning("Failed to decode 'sources' field from string", chat_id=str(chat_id), message_id=str(msg_dict.get('id')), raw_sources=msg_dict['sources'])
                    msg_dict['sources'] = None # O devolver lista vacía? None es más seguro
            # Si es None o ya es una lista/dict (codec funcionó), no hacer nada
            elif msg_dict.get('sources') is None:
                 msg_dict['sources'] = None # Asegurar que sea None explícito si es NULL en DB

            # Asegurarse que 'sources' sea una Lista o None para el schema
            if not isinstance(msg_dict.get('sources'), (list, type(None))):
                log.warning("Unexpected type for 'sources' after potential decoding", type=type(msg_dict['sources']).__name__, message_id=str(msg_dict.get('id')))
                msg_dict['sources'] = None # Forzar a None si sigue siendo inválido

            messages.append(msg_dict)

        log.info(f"Retrieved {len(messages)} messages for chat", chat_id=str(chat_id))
        return messages
    except Exception as e:
        log.error("Failed to get chat messages", error=str(e), exc_info=True)
        # Relanzar la excepción para que el endpoint devuelva 500
        raise

async def save_message(chat_id: uuid.UUID, role: str, content: str, sources: Optional[List[Dict[str, Any]]] = None) -> uuid.UUID:
    """Guarda un mensaje en un chat y actualiza el timestamp del chat."""
    pool = await get_db_pool()
    message_id = uuid.uuid4()
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                # Actualizar timestamp del chat
                update_chat_query = "UPDATE chats SET updated_at = NOW() AT TIME ZONE 'UTC' WHERE id = $1 RETURNING id;"
                chat_updated = await conn.fetchval(update_chat_query, chat_id)
                if not chat_updated:
                     log.error("Failed to update chat timestamp, chat might not exist", chat_id=str(chat_id))
                     raise ValueError(f"Chat with ID {chat_id} not found for saving message.")

                # Insertar mensaje (usando json.dumps para 'sources')
                insert_message_query = """
                INSERT INTO messages (id, chat_id, role, content, sources, created_at)
                VALUES ($1, $2, $3, $4, $5, NOW() AT TIME ZONE 'UTC') RETURNING id;
                """
                # Asegurar que sources sea JSON válido antes de insertar
                sources_json = json.dumps(sources) if sources is not None else None # Guardar NULL si sources es None

                result = await conn.fetchval(insert_message_query, message_id, chat_id, role, content, sources_json)

                if result and result == message_id:
                    log.info("Message saved successfully", message_id=str(message_id), chat_id=str(chat_id), role=role)
                    return message_id
                else:
                    log.error("Failed to save message, unexpected or no ID returned", chat_id=str(chat_id), returned_id=result)
                    raise RuntimeError("Failed to save message, ID mismatch or not returned.")
            except Exception as e:
                log.error("Failed to save message within transaction", error=str(e), chat_id=str(chat_id), exc_info=True)
                raise # Relanzar para abortar transacción

# *** FUNCIÓN CORREGIDA: delete_chat ***
async def delete_chat(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    """Elimina un chat y sus mensajes si pertenece al usuario/compañía."""
    pool = await get_db_pool()
    delete_log = log.bind(chat_id=str(chat_id), user_id=str(user_id), company_id=str(company_id))

    # Verificar propiedad primero
    owner = await check_chat_ownership(chat_id, user_id, company_id)
    if not owner:
        delete_log.warning("Chat not found or does not belong to user, deletion skipped")
        return False

    async with pool.acquire() as conn:
        async with conn.transaction(): # Usar transacción
            try:
                # 1. Eliminar mensajes asociados
                delete_messages_query = "DELETE FROM messages WHERE chat_id = $1;"
                await conn.execute(delete_messages_query, chat_id)
                delete_log.info("Associated messages deleted (if any)")

                # 2. Eliminar el chat
                delete_chat_query = "DELETE FROM chats WHERE id = $1 RETURNING id;"
                deleted_id = await conn.fetchval(delete_chat_query, chat_id)

                success = deleted_id is not None
                if success:
                    delete_log.info("Chat deleted successfully after deleting messages")
                else:
                    # Esto no debería ocurrir si la verificación de propiedad pasó y estamos en transacción
                    delete_log.error("Chat deletion failed after deleting messages, despite ownership check")
                return success
            except Exception as e:
                # Capturar ForeignKeyViolationError específicamente si ocurre aquí (inesperado)
                if isinstance(e, asyncpg.exceptions.ForeignKeyViolationError):
                     delete_log.error("Foreign key violation during chat deletion transaction (unexpected)", error=str(e), exc_info=True)
                else:
                     delete_log.error("Failed to delete chat within transaction", error=str(e), exc_info=True)
                # La transacción se revertirá automáticamente al salir del bloque with
                raise # Relanzar para que el endpoint devuelva 500
```

## File: `app\main.py`
```py
# query-service/app/main.py
from fastapi import FastAPI, HTTPException, status as fastapi_status, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse, Response, PlainTextResponse
import structlog
import uvicorn
import logging
import sys
import asyncio
import json

# Configurar logging primero
from app.core.config import settings
from app.core.logging_config import setup_logging
setup_logging()
log = structlog.get_logger("query_service.main")

# Importar routers y otros módulos
from app.api.v1.endpoints import query as query_router_module
from app.api.v1.endpoints import chat as chat_router_module
from app.db import postgres_client
from app.pipelines.rag_pipeline import build_rag_pipeline, check_pipeline_dependencies
from app.api.v1 import schemas
# from app.utils import helpers

# Estado global
SERVICE_READY = False
GLOBAL_RAG_PIPELINE = None

# Crear instancia de FastAPI
app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json", # Restaurar openapi_url con prefijo
    version="0.2.3", # Incrementar versión por corrección de prefijo
    description="Microservice to handle user queries using a Haystack RAG pipeline with Milvus and Gemini, including chat history management. Expects /api/v1 prefix.", # Descripción actualizada
)

# --- Event Handlers (Sin cambios en lógica interna) ---
@app.on_event("startup")
async def startup_event():
    global SERVICE_READY, GLOBAL_RAG_PIPELINE
    log.info(f"Starting up {settings.PROJECT_NAME}...")
    db_pool_initialized = False
    pipeline_built = False
    milvus_startup_ok = False
    # Intenta inicializar la BD
    try:
        await postgres_client.get_db_pool()
        db_ready = await postgres_client.check_db_connection()
        if db_ready:
            log.info("PostgreSQL connection pool initialized and verified.")
            db_pool_initialized = True
        else:
            log.critical("CRITICAL: Failed PostgreSQL connection verification during startup.")
            db_pool_initialized = False
    except Exception as e:
        log.critical("CRITICAL: Failed PostgreSQL pool initialization during startup.", error=str(e), exc_info=True)
        db_pool_initialized = False

    # Intenta construir el pipeline RAG
    try:
        GLOBAL_RAG_PIPELINE = build_rag_pipeline()
        dep_status = await check_pipeline_dependencies()
        milvus_status = dep_status.get("milvus_connection", "error: check failed")
        if "ok" in milvus_status: # Aceptar 'ok' u 'ok (collection not found yet)'
            log.info("Haystack RAG pipeline built and Milvus connection status is OK.", status=milvus_status)
            pipeline_built = True
            milvus_startup_ok = True
        else:
             log.warning("Haystack RAG pipeline built, but initial Milvus check failed or pending.", status=milvus_status)
             pipeline_built = True
             milvus_startup_ok = False
    except Exception as e:
        log.error("Failed building Haystack RAG pipeline during startup.", error=str(e), exc_info=True)
        pipeline_built = False

    # Marcar como listo solo si las dependencias críticas (DB, Pipeline) están OK
    if db_pool_initialized and pipeline_built:
        SERVICE_READY = True
        log.info(f"{settings.PROJECT_NAME} service components initialized. SERVICE READY. Initial Milvus Check: {'OK' if milvus_startup_ok else 'WARN/FAIL'}")
    else:
        SERVICE_READY = False
        log.critical(f"{settings.PROJECT_NAME} startup FAILED critical components. DB OK: {db_pool_initialized}, Pipeline Built: {pipeline_built}. Service NOT READY.")


@app.on_event("shutdown")
async def shutdown_event():
    log.info(f"Shutting down {settings.PROJECT_NAME}...")
    await postgres_client.close_db_pool()
    log.info("Shutdown complete.")

# --- Exception Handlers (Sin cambios) ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    request_id = request.headers.get("x-request-id", "N/A")
    log_level = log.warning if exc.status_code < 500 else log.error
    log_level("HTTP Exception caught by handler",
              status_code=exc.status_code,
              detail=exc.detail,
              path=str(request.url),
              method=request.method,
              client=request.client.host if request.client else "unknown",
              request_id=request_id)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    request_id = request.headers.get("x-request-id", "N/A")
    error_details = []
    try:
         error_details = exc.errors()
    except Exception:
         error_details = [{"loc": [], "msg": "Failed to parse validation errors.", "type": "internal_parsing_error"}]

    log.warning("Request Validation Error",
                path=str(request.url),
                method=request.method,
                client=request.client.host if request.client else "unknown",
                errors=error_details,
                request_id=request_id)

    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": error_details},
    )

@app.exception_handler(ResponseValidationError)
async def response_validation_exception_handler(request: Request, exc: ResponseValidationError):
    request_id = request.headers.get("x-request-id", "N/A")
    log.error("Response Validation Error - Server failed to construct valid response",
              path=str(request.url),
              method=request.method,
              errors=exc.errors(),
              request_id=request_id,
              exc_info=True)

    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal Server Error: Failed to serialize response."},
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    request_id = request.headers.get("x-request-id", "N/A")
    log.exception("Unhandled Exception caught by generic handler",
                  path=str(request.url),
                  method=request.method,
                  client=request.client.host if request.client else "unknown",
                  request_id=request_id)
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal Server Error"},
    )


# --- Routers ---
# *** REVERSIÓN: Volver a añadir el prefijo API_V1_STR ***
# El API Gateway reenvía la ruta completa, por lo que el servicio debe esperarla.
app.include_router(query_router_module.router, prefix=settings.API_V1_STR, tags=["Query Interaction"]) # Ruta esperada: /api/v1/ask
app.include_router(chat_router_module.router, prefix=settings.API_V1_STR, tags=["Chat Management"])   # Rutas esperadas: /api/v1/chats, etc.
log.info("Routers included with prefix", prefix=settings.API_V1_STR) # Actualizar log

# --- Root Endpoint / Health Check (Sin cambios) ---
@app.get("/", tags=["Health Check"], summary="Service Liveness/Readiness Check")
async def read_root(request: Request):
    global SERVICE_READY
    request_id = request.headers.get("x-request-id", "N/A")
    health_log = log.bind(request_id=request_id)
    health_log.debug("Health check endpoint '/' requested")

    if not SERVICE_READY:
         health_log.warning("Health check failed: Service not ready (SERVICE_READY is False).")
         return PlainTextResponse("Service Not Ready", status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE)

    db_ok = await postgres_client.check_db_connection()
    if db_ok:
        health_log.info("Health check successful (Service Ready, DB connection OK).")
        return PlainTextResponse("OK", status_code=fastapi_status.HTTP_200_OK)
    else:
        health_log.error("Health check failed: Service is READY but DB check FAILED.")
        return PlainTextResponse("Service Ready but DB check failed", status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE)


# --- Main execution (for local development, sin cambios) ---
if __name__ == "__main__":
    port = 8002
    log_level_str = settings.LOG_LEVEL.lower()
    print(f"----- Starting Query Service locally on port {port} -----")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True, log_level=log_level_str)
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
# query-service/app/pipelines/rag_pipeline.py
import structlog
import asyncio
import uuid
import functools # LLM_COMMENT: Import functools to use partial for run_in_executor keyword arguments.
from typing import Dict, Any, List, Tuple, Optional

from pymilvus.exceptions import MilvusException, ErrorCode
from fastapi import HTTPException, status

from haystack import Pipeline, Document
from haystack.components.embedders import OpenAITextEmbedder
from haystack.components.builders.prompt_builder import PromptBuilder
from milvus_haystack import MilvusDocumentStore, MilvusEmbeddingRetriever
from haystack.utils import Secret

from app.core.config import settings
from app.db import postgres_client
from app.services.gemini_client import gemini_client
from app.api.v1.schemas import RetrievedDocument

log = structlog.get_logger(__name__)

# --- Component Initialization Functions (sin cambios) ---
def get_milvus_document_store() -> MilvusDocumentStore:
    connection_uri = str(settings.MILVUS_URI)
    connection_timeout = 30.0
    log.debug("Initializing MilvusDocumentStore...", uri=connection_uri, collection=settings.MILVUS_COLLECTION_NAME)
    try:
        store = MilvusDocumentStore(
            connection_args={"uri": connection_uri, "timeout": connection_timeout},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            search_params=settings.MILVUS_SEARCH_PARAMS,
            consistency_level="Strong",
        )
        log.info("MilvusDocumentStore parameters configured.", uri=connection_uri, collection=settings.MILVUS_COLLECTION_NAME)
        return store
    except MilvusException as e:
        log.error("Failed to initialize MilvusDocumentStore", error_code=e.code, error_message=e.message, exc_info=True)
        raise RuntimeError(f"Milvus connection/initialization failed: {e.message}") from e
    except TypeError as te:
        log.error("TypeError during MilvusDocumentStore initialization (likely invalid argument)", error=str(te), exc_info=True)
        raise RuntimeError(f"Milvus initialization error (Invalid argument): {te}") from te
    except Exception as e:
        log.error("Unexpected error during MilvusDocumentStore initialization", error=str(e), exc_info=True)
        raise RuntimeError(f"Unexpected error initializing Milvus: {e}") from e

def get_openai_text_embedder() -> OpenAITextEmbedder:
    log.debug("Initializing OpenAITextEmbedder", model=settings.OPENAI_EMBEDDING_MODEL)
    api_key_value = settings.OPENAI_API_KEY.get_secret_value()
    if not api_key_value: log.warning("QUERY_OPENAI_API_KEY is missing or empty!")
    return OpenAITextEmbedder(
        api_key=Secret.from_token(api_key_value or "dummy-key"),
        model=settings.OPENAI_EMBEDDING_MODEL
    )

def get_milvus_retriever(document_store: MilvusDocumentStore) -> MilvusEmbeddingRetriever:
    log.debug("Initializing MilvusEmbeddingRetriever")
    return MilvusEmbeddingRetriever(document_store=document_store, top_k=settings.RETRIEVER_TOP_K)

def get_prompt_builder() -> PromptBuilder:
    log.debug("Initializing PromptBuilder")
    return PromptBuilder(template=settings.RAG_PROMPT_TEMPLATE)


# --- Pipeline Construction (sin cambios) ---
_rag_pipeline_instance: Optional[Pipeline] = None
def build_rag_pipeline() -> Pipeline:
    global _rag_pipeline_instance
    if _rag_pipeline_instance: return _rag_pipeline_instance
    log.info("Building Haystack RAG pipeline...")
    rag_pipeline = Pipeline()
    try:
        doc_store = get_milvus_document_store()
        text_embedder = get_openai_text_embedder()
        retriever = get_milvus_retriever(document_store=doc_store)
        prompt_builder = get_prompt_builder()

        rag_pipeline.add_component("text_embedder", text_embedder)
        rag_pipeline.add_component("retriever", retriever)
        rag_pipeline.add_component("prompt_builder", prompt_builder)

        rag_pipeline.connect("text_embedder.embedding", "retriever.query_embedding")
        rag_pipeline.connect("retriever.documents", "prompt_builder.documents")

        log.info("Haystack RAG pipeline built successfully.")
        _rag_pipeline_instance = rag_pipeline
        return rag_pipeline
    except Exception as e:
        log.error("Failed to build Haystack RAG pipeline", error=str(e), exc_info=True)
        raise RuntimeError("Could not build the RAG pipeline") from e

# --- Pipeline Execution ---
# *** FUNCIÓN CORREGIDA: run_rag_pipeline ***
async def run_rag_pipeline(
    query: str,
    company_id: str,
    user_id: Optional[str],
    top_k: Optional[int] = None,
    chat_id: Optional[uuid.UUID] = None
) -> Tuple[str, List[Document], Optional[uuid.UUID]]:
    """
    Ejecuta el pipeline RAG usando `run_in_executor` con `functools.partial`
    para pasar argumentos de palabra clave `data` y `params` a `pipeline.run`.
    Luego llama a Gemini y loguea la interacción.
    """
    # LLM_COMMENT: This function orchestrates the RAG pipeline execution.
    # LLM_COMMENT: It takes user query, company/user context, and optional chat info.
    # LLM_COMMENT: Key step: Calls Haystack's pipeline.run asynchronously using run_in_executor.
    run_log = log.bind(query=query, company_id=company_id, user_id=user_id or "N/A", chat_id=str(chat_id) if chat_id else "N/A")
    run_log.info("Running RAG pipeline execution flow...")

    try:
        pipeline = build_rag_pipeline() # Obtener/construir el pipeline
        # LLM_COMMENT: Ensure pipeline instance is available before proceeding.
    except Exception as build_err:
         run_log.error("Failed to get or build RAG pipeline for execution", error=str(build_err))
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="RAG pipeline is not available.")

    # Determinar parámetros para el retriever
    retriever_top_k = top_k if top_k is not None else settings.RETRIEVER_TOP_K
    retriever_filters = {"field": settings.MILVUS_COMPANY_ID_FIELD, "operator": "==", "value": company_id}
    # LLM_COMMENT: Filters are prepared for the Milvus retriever component based on company_id.

    run_log.debug("Pipeline execution parameters set", filters=retriever_filters, top_k=retriever_top_k)

    # Preparar argumentos para pipeline.run
    pipeline_input_data = {
        "text_embedder": {"text": query},
        "prompt_builder": {"query": query}
    }
    pipeline_params = {
        "retriever": {"filters": [retriever_filters], "top_k": retriever_top_k}
    }
    # LLM_COMMENT: Define 'data' for initial pipeline inputs and 'params' for component-specific runtime configurations.

    run_log.debug("Constructed pipeline run arguments", data_keys=list(pipeline_input_data.keys()), params_keys=list(pipeline_params.keys()))

    try:
        loop = asyncio.get_running_loop()

        # *** CORRECCIÓN: Usar functools.partial con run_in_executor ***
        # LLM_COMMENT: Use functools.partial to wrap the pipeline.run call with its keyword arguments (data, params).
        # LLM_COMMENT: This partial object is then passed to run_in_executor, which expects a callable without keyword args itself.
        # LLM_COMMENT: This avoids the TypeError: run_in_executor() got an unexpected keyword argument 'data'.
        pipeline_run_partial = functools.partial(
            pipeline.run,
            data=pipeline_input_data,
            params=pipeline_params
        )
        # Ejecutar el pipeline envuelto en el executor
        pipeline_result = await loop.run_in_executor(
            None,       # Usa el executor por defecto (ThreadPoolExecutor)
            pipeline_run_partial # La función envuelta a ejecutar
        )

        run_log.info("Haystack pipeline (embed, retrieve, prompt) executed successfully.")

        # Extraer resultados (sin cambios en esta parte)
        retrieved_docs: List[Document] = pipeline_result.get("retriever", {}).get("documents", [])
        prompt_builder_output = pipeline_result.get("prompt_builder", {})
        generated_prompt: Optional[str] = prompt_builder_output.get("prompt") if isinstance(prompt_builder_output, dict) else None
        # LLM_COMMENT: Process the pipeline output, extracting retrieved documents and the generated prompt.

        if not retrieved_docs:
             run_log.warning("No relevant documents found by retriever for the query.")
        else:
             run_log.info(f"Retriever found {len(retrieved_docs)} documents.")

        if not generated_prompt:
             run_log.error("Failed to extract prompt from prompt_builder output", component_output=prompt_builder_output)
             generated_prompt = f"Pregunta: {query}\n\n(No se pudo construir el prompt con documentos recuperados). Por favor responde a la pregunta."
             # LLM_COMMENT: Handle cases where prompt generation might fail.

        run_log.debug("Generated prompt for LLM", prompt_length=len(generated_prompt))

        # Llamar a Gemini (sin cambios)
        answer = await gemini_client.generate_answer(generated_prompt)
        # LLM_COMMENT: Send the final prompt to the external LLM (Gemini).
        run_log.info("Answer generated by Gemini", answer_length=len(answer))

        # Loguear la interacción en la BD (sin cambios)
        log_id: Optional[uuid.UUID] = None
        try:
            formatted_docs_for_log = [
                RetrievedDocument.from_haystack_doc(doc).model_dump(exclude_none=True)
                for doc in retrieved_docs
            ]
            user_uuid = uuid.UUID(user_id) if user_id and isinstance(user_id, str) else None
            company_uuid = uuid.UUID(company_id) if isinstance(company_id, str) else company_id

            log_id = await postgres_client.log_query_interaction(
                company_id=company_uuid, user_id=user_uuid, query=query, answer=answer,
                retrieved_documents_data=formatted_docs_for_log, chat_id=chat_id,
                metadata={"retriever_top_k": retriever_top_k, "llm_model": settings.GEMINI_MODEL_NAME}
            )
            run_log.info("Query interaction logged to database", db_log_id=str(log_id))
            # LLM_COMMENT: Log the complete interaction (query, answer, context) to the database.
        except Exception as log_err:
             run_log.error("Failed to log query interaction to database after successful generation", error=str(log_err), exc_info=True)
             # LLM_COMMENT: Log database logging errors but don't fail the user request.

        # Devolver la respuesta (sin cambios)
        return answer, retrieved_docs, log_id

    # Manejo de errores (sin cambios)
    # LLM_COMMENT: Catch potential errors during pipeline execution (HTTP, Milvus, ValueErrors, general exceptions).
    except HTTPException as http_exc:
        raise http_exc
    except MilvusException as milvus_err:
        run_log.error("Milvus error during pipeline execution", error_code=milvus_err.code, error_message=milvus_err.message, exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Vector database error: {milvus_err.message}")
    except ValueError as val_err:
         # LLM_COMMENT: Specifically catch ValueErrors which might indicate pipeline configuration issues.
         run_log.error("ValueError during pipeline execution", error=str(val_err), exc_info=True)
         raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Pipeline configuration or input error: {val_err}")
    except Exception as e:
        # LLM_COMMENT: Catch-all for any other unexpected errors during the process.
        run_log.exception("Unexpected error occurred during RAG pipeline execution")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error processing query: {type(e).__name__}")

# --- Dependency Check Function (sin cambios desde la última versión) ---
# LLM_COMMENT: This function checks the status of external dependencies (Milvus, API keys) during startup or health checks.
async def check_pipeline_dependencies() -> Dict[str, str]:
    results = {"milvus_connection": "pending", "openai_api": "pending", "gemini_api": "pending"}
    try:
        store = get_milvus_document_store()
        count = await asyncio.to_thread(store.count_documents)
        results["milvus_connection"] = "ok"
        log.debug("Milvus dependency check successful.", document_count=count)
        # LLM_COMMENT: Milvus check involves connecting and performing a simple operation like counting documents.
    except MilvusException as e:
        # LLM_COMMENT: Handle specific Milvus errors like CollectionNotFound (acceptable) or connection errors (warning).
        if e.code == ErrorCode.COLLECTION_NOT_FOUND:
            results["milvus_connection"] = "ok (collection not found yet)"
            log.info("Milvus dependency check: Collection not found (expected if empty, will be created on write).")
        elif e.code == ErrorCode.UNEXPECTED_ERROR and "connect failed" in e.message.lower():
            results["milvus_connection"] = f"error: Connection Failed (code={e.code}, msg={e.message})"
            log.warning("Milvus dependency check failed: Connection Error", error_code=e.code, error_message=e.message, exc_info=False)
        else:
            results["milvus_connection"] = f"error: MilvusException (code={e.code}, msg={e.message})"
            log.warning("Milvus dependency check failed with Milvus error", error_code=e.code, error_message=e.message, exc_info=False)
    except RuntimeError as rte:
         results["milvus_connection"] = f"error: Initialization Failed ({rte})"
         log.warning("Milvus dependency check failed during store initialization", error=str(rte), exc_info=False)
    except Exception as e:
        results["milvus_connection"] = f"error: Unexpected {type(e).__name__}"
        log.warning("Milvus dependency check failed with unexpected error", error=str(e), exc_info=True)

    # LLM_COMMENT: Check for the presence of necessary API keys.
    if settings.OPENAI_API_KEY.get_secret_value() and settings.OPENAI_API_KEY.get_secret_value() != "dummy-key":
        results["openai_api"] = "key_present"
    else:
        results["openai_api"] = "key_missing"
        log.warning("OpenAI API Key missing or is dummy key in config.")

    if settings.GEMINI_API_KEY.get_secret_value():
        results["gemini_api"] = "key_present"
    else:
        results["gemini_api"] = "key_missing"
        log.warning("Gemini API Key missing in config.")

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
# query-service/app/services/base_client.py
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, retry_if_exception
import structlog
from typing import Any, Dict, Optional

from app.core.config import settings

log = structlog.get_logger(__name__)

# Define qué errores HTTP son recuperables (Server errors)
RETRYABLE_HTTP_STATUS = (500, 502, 503, 504)

class BaseServiceClient:
    """Cliente HTTP base asíncrono con reintentos configurables."""

    def __init__(self, base_url: str, service_name: str):
        self.base_url = base_url
        self.service_name = service_name
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=settings.HTTP_CLIENT_TIMEOUT)
        log.debug(f"{self.service_name} client initialized", base_url=base_url)

    async def close(self):
        await self.client.aclose()
        log.info(f"{self.service_name} client closed.")

    # Decorador de reintentos Tenacity
    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES + 1), # Total attempts = initial + retries
        wait=wait_exponential(multiplier=1, min=settings.HTTP_CLIENT_BACKOFF_FACTOR, max=10),
        # *** CORREGIDO: Lógica de reintento explícita ***
        retry=(
            # Reintentar en errores de red o timeout
            retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout)) |
            # Reintentar si es un error HTTP y el status code está en la lista de recuperables
            retry_if_exception(lambda e: isinstance(e, httpx.HTTPStatusError) and e.response.status_code in RETRYABLE_HTTP_STATUS)
        ),
        reraise=True, # Relanzar la última excepción si todos los reintentos fallan
        before_sleep=lambda retry_state: log.warning(
            f"Retrying {self.service_name} request",
            # Intenta obtener detalles del intento fallido
            method=getattr(retry_state.args[0], 'method', 'N/A') if retry_state.args and isinstance(retry_state.args[0], httpx.Request) else 'N/A',
            url=str(getattr(retry_state.args[0], 'url', 'N/A')) if retry_state.args and isinstance(retry_state.args[0], httpx.Request) else 'N/A',
            attempt=retry_state.attempt_number,
            wait_time=f"{retry_state.next_action.sleep:.2f}s",
            error_type=type(retry_state.outcome.exception()).__name__,
            error_details=str(retry_state.outcome.exception())
        )
    )
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None, # Renombrado
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        """Realiza una petición HTTP asíncrona con reintentos."""
        request_log = log.bind(service=self.service_name, method=method, endpoint=endpoint)
        request_log.debug("Sending request...")
        request_obj = self.client.build_request( # Construir objeto request para logging
             method, endpoint, params=params, json=json_data, data=data, files=files, headers=headers
        )
        try:
            response = await self.client.send(request_obj) # Usar send con el objeto request
            response.raise_for_status() # Lanza excepción para 4xx/5xx
            request_log.debug("Request successful", status_code=response.status_code)
            return response
        except httpx.HTTPStatusError as e:
            log_level = log.warning if e.response.status_code < 500 else log.error
            log_level(
                "Request failed with HTTP status code",
                status_code=e.response.status_code,
                response_preview=e.response.text[:200], # Preview de la respuesta
                exc_info=False
            )
            raise # Re-lanzar para que Tenacity maneje reintentos (para 5xx) o falle (para 4xx)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as net_err:
            request_log.error("Request failed due to network/timeout issue", error_type=type(net_err).__name__, error_details=str(net_err))
            raise # Re-lanzar para Tenacity
        except Exception as e:
             request_log.exception("An unexpected error occurred during request")
             raise # Re-lanzar excepción inesperada
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
