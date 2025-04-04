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
# ./app/api/v1/endpoints/chat.py
import uuid
from typing import List, Optional
import structlog

from fastapi import APIRouter, Depends, HTTPException, status, Path, Query

from app.api.v1 import schemas
from app.db import postgres_client
# Importar dependencias de autenticación/autorización
from .query import get_current_user_id, get_current_company_id # Reutilizar las dependencias existentes

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Endpoint para Listar Chats ---
@router.get(
    "/chats",
    response_model=List[schemas.ChatSummary],
    status_code=status.HTTP_200_OK,
    summary="List User Chats",
    description="Retrieves a list of chat summaries for the authenticated user and company, ordered by last updated time.",
)
async def list_chats(
    user_id: uuid.UUID = Depends(get_current_user_id),
    company_id: uuid.UUID = Depends(get_current_company_id),
):
    endpoint_log = log.bind(user_id=str(user_id), company_id=str(company_id))
    endpoint_log.info("Request received to list chats")

    if not user_id: # Asegurarse de que el usuario está identificado
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not authenticated")

    try:
        chats = await postgres_client.get_chats_for_user(user_id=user_id, company_id=company_id)
        endpoint_log.info("Chats listed successfully", count=len(chats))
        return chats
    except Exception as e:
        endpoint_log.exception("Error listing chats")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve chat list.")

# --- Endpoint para Obtener Mensajes de un Chat ---
@router.get(
    "/chats/{chat_id}/messages",
    response_model=List[schemas.ChatMessage],
    status_code=status.HTTP_200_OK,
    summary="Get Chat Messages",
    description="Retrieves all messages for a specific chat, verifying user ownership.",
    responses={
        404: {"description": "Chat not found or user does not have access."},
    }
)
async def get_chat_messages(
    chat_id: uuid.UUID = Path(..., description="The ID of the chat to retrieve messages from."),
    user_id: uuid.UUID = Depends(get_current_user_id),
    company_id: uuid.UUID = Depends(get_current_company_id),
):
    endpoint_log = log.bind(user_id=str(user_id), company_id=str(company_id), chat_id=str(chat_id))
    endpoint_log.info("Request received to get chat messages")

    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not authenticated")

    try:
        # La función get_messages_for_chat ya incluye la verificación de propiedad
        messages = await postgres_client.get_messages_for_chat(
            chat_id=chat_id,
            user_id=user_id,
            company_id=company_id
        )

        # Si get_messages_for_chat devuelve [] porque no se encontró o no hay acceso,
        # podríamos querer devolver 404 en lugar de 200 con lista vacía.
        # Para hacer eso, necesitaríamos que check_chat_ownership lance una excepción o
        # hacer el check aquí primero. Por simplicidad, mantenemos el comportamiento de devolver 200 OK [].
        # Si se requiere 404, descomentar y ajustar:
        # owner_check = await postgres_client.check_chat_ownership(chat_id, user_id, company_id)
        # if not owner_check:
        #     endpoint_log.warning("Chat not found or access denied")
        #     raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found or access denied.")
        # messages = await postgres_client.get_messages_for_chat(...) # Llamar sin el check interno

        endpoint_log.info("Chat messages retrieved successfully", count=len(messages))
        return messages
    except Exception as e:
        endpoint_log.exception("Error getting chat messages")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve chat messages.")


# --- Endpoint para Borrar un Chat ---
@router.delete(
    "/chats/{chat_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Chat",
    description="Deletes a specific chat and all its associated messages, verifying user ownership.",
    responses={
        404: {"description": "Chat not found or user does not have access."},
        204: {"description": "Chat deleted successfully."},
    }
)
async def delete_chat_endpoint(
    chat_id: uuid.UUID = Path(..., description="The ID of the chat to delete."),
    user_id: uuid.UUID = Depends(get_current_user_id),
    company_id: uuid.UUID = Depends(get_current_company_id),
):
    endpoint_log = log.bind(user_id=str(user_id), company_id=str(company_id), chat_id=str(chat_id))
    endpoint_log.info("Request received to delete chat")

    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not authenticated")

    try:
        deleted = await postgres_client.delete_chat(
            chat_id=chat_id,
            user_id=user_id,
            company_id=company_id
        )
        if deleted:
            endpoint_log.info("Chat deleted successfully")
            # No se devuelve contenido en un 204
            return None
        else:
            # Si delete_chat devuelve False, significa que no se encontró o no se tenía permiso
            endpoint_log.warning("Chat not found or access denied for deletion")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found or access denied.")
    except Exception as e:
        endpoint_log.exception("Error deleting chat")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to delete chat.")

# --- Endpoints Opcionales (POST /chats, PATCH /chats/{chatId}) ---
# POST /api/v1/chats: No se implementa aquí porque la creación se maneja en POST /query.
# PATCH /api/v1/chats/{chatId}: No implementado por ahora (para renombrar).
```

## File: `app\api\v1\endpoints\query.py`
```py
# ./app/api/v1/endpoints/query.py
import uuid
from typing import Dict, Any, Optional, List # Añadir List
import structlog
import asyncio

from jose import jwt, JWTError
from jose.exceptions import ExpiredSignatureError, JWTClaimsError, JWSError

from fastapi import APIRouter, Depends, HTTPException, status, Header, Body, Request

from app.api.v1 import schemas
from app.core.config import settings
from app.db import postgres_client # Para logging y ahora chat/messages
from app.pipelines import rag_pipeline # Importar funciones del pipeline
from haystack import Document # Para type hints
from app.utils.helpers import truncate_text # Importar helper si se usa

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Dependency for Company ID (Sin cambios) ---
async def get_current_company_id(x_company_id: Optional[str] = Header(None)) -> uuid.UUID:
    if not x_company_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Company-ID header",
        )
    try:
        company_uuid = uuid.UUID(x_company_id)
        return company_uuid
    except ValueError:
         raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid X-Company-ID header format (must be UUID)",
        )

# --- Dependency for User ID (Sin cambios) ---
async def get_current_user_id(authorization: Optional[str] = Header(None)) -> Optional[uuid.UUID]:
    if not authorization or not authorization.startswith("Bearer "):
        log.debug("No Authorization header found or not Bearer type.")
        return None # Usuario anónimo o no autenticado

    token = authorization.split(" ")[1]
    try:
        payload = jwt.decode(
            token, key="dummy",
            options={
                "verify_signature": False, "verify_aud": False, "verify_iss": False, "verify_exp": True,
            }
        )
        user_id_str = payload.get("sub") or payload.get("user_id")
        if not user_id_str: return None
        try:
            user_uuid = uuid.UUID(user_id_str)
            log.debug("Successfully extracted user ID from token", user_id=str(user_uuid))
            return user_uuid
        except ValueError: return None
    except ExpiredSignatureError: return None
    except JWTError: return None
    except Exception: log.exception("Unexpected error during user ID extraction"); return None


# --- Endpoint /query Modificado ---
@router.post(
    "/ask",
    # --- Actualizar response_model ---
    response_model=schemas.QueryResponse,
    status_code=status.HTTP_200_OK,
    summary="Process a user query using RAG pipeline and manage chat history",
    description="Receives a query. If chat_id is provided, continues the chat. If not, creates a new chat. Saves user and assistant messages, runs RAG, logs the interaction, and returns the result including the chat_id.",
)
async def process_query(
    request_body: schemas.QueryRequest = Body(...),
    company_id: uuid.UUID = Depends(get_current_company_id),
    user_id: Optional[uuid.UUID] = Depends(get_current_user_id),
):
    """
    Endpoint principal para procesar consultas de usuario, integrado con el historial de chat.
    """
    # --- Validación de User ID (Ahora es mandatorio para chats) ---
    if not user_id:
        log.warning("Query request rejected: User ID could not be determined from token. Chat history requires authentication.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required to use the chat feature."
        )

    endpoint_log = log.bind(
        company_id=str(company_id),
        user_id=str(user_id),
        query=truncate_text(request_body.query, 100), # Usar helper
        provided_chat_id=str(request_body.chat_id) if request_body.chat_id else "None"
    )
    endpoint_log.info("Received query request with chat context")

    current_chat_id: uuid.UUID
    is_new_chat = False

    try:
        # --- Lógica de Creación/Obtención de Chat ID ---
        if request_body.chat_id:
            # Verificar si el chat existente pertenece al usuario
            if not await postgres_client.check_chat_ownership(request_body.chat_id, user_id, company_id):
                 endpoint_log.warning("Attempt to use chat_id not owned by user or non-existent", chat_id=str(request_body.chat_id))
                 raise HTTPException(
                     status_code=status.HTTP_403_FORBIDDEN, # O 404 si preferimos
                     detail="Chat not found or access denied."
                 )
            current_chat_id = request_body.chat_id
            endpoint_log = endpoint_log.bind(chat_id=str(current_chat_id))
            endpoint_log.info("Continuing existing chat")
        else:
            # Crear nuevo chat
            endpoint_log.info("No chat_id provided, creating a new chat...")
            # Generar un título inicial simple (se puede mejorar)
            initial_title = f"Chat: {truncate_text(request_body.query, 50)}"
            current_chat_id = await postgres_client.create_chat(
                user_id=user_id,
                company_id=company_id,
                title=initial_title
            )
            is_new_chat = True
            endpoint_log = endpoint_log.bind(chat_id=str(current_chat_id))
            endpoint_log.info("New chat created", title=initial_title)

        # --- Guardar Mensaje del Usuario ---
        endpoint_log.info("Saving user message...")
        await postgres_client.add_message_to_chat(
            chat_id=current_chat_id,
            role='user',
            content=request_body.query
        )
        endpoint_log.info("User message saved")

        # --- Ejecutar el Pipeline RAG ---
        endpoint_log.info("Running RAG pipeline...")
        # Pasar chat_id a run_rag_pipeline para logging
        answer, retrieved_docs_haystack, log_id = await rag_pipeline.run_rag_pipeline(
            query=request_body.query,
            company_id=str(company_id),
            user_id=str(user_id), # run_rag_pipeline espera string
            top_k=request_body.retriever_top_k,
            chat_id=current_chat_id # Pasar el UUID
        )
        endpoint_log.info("RAG pipeline finished")

        # --- Formatear Documentos Recuperados y Extraer Fuentes ---
        response_docs_schema: List[schemas.RetrievedDocument] = []
        assistant_sources: List[Dict[str, Any]] = []
        for doc in retrieved_docs_haystack:
            schema_doc = schemas.RetrievedDocument.from_haystack_doc(doc)
            response_docs_schema.append(schema_doc)
            # Crear estructura de fuentes para guardar en messages.sources
            source_info = {
                "chunk_id": schema_doc.id,
                "document_id": schema_doc.document_id,
                "file_name": schema_doc.file_name,
                "score": schema_doc.score,
                "preview": schema_doc.content_preview # Guardar preview o content completo? Preview es más ligero.
            }
            assistant_sources.append(source_info)

        # --- Guardar Mensaje del Asistente ---
        endpoint_log.info("Saving assistant message...")
        await postgres_client.add_message_to_chat(
            chat_id=current_chat_id,
            role='assistant',
            content=answer,
            sources=assistant_sources if assistant_sources else None # Guardar fuentes si existen
        )
        endpoint_log.info("Assistant message saved")

        endpoint_log.info("Query processed successfully", log_id=str(log_id) if log_id else "Log Failed", num_retrieved=len(response_docs_schema))

        # --- Devolver Respuesta ---
        return schemas.QueryResponse(
            answer=answer,
            retrieved_documents=response_docs_schema,
            query_log_id=log_id,
            chat_id=current_chat_id # Devolver el chat_id usado o creado
        )

    except ValueError as ve: # Errores de validación o de lógica interna (e.g., chat no encontrado en add_message)
        endpoint_log.warning("Value error during query processing", error=str(ve))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))
    except ConnectionError as ce:
         endpoint_log.error("Connection error during query processing", error=str(ce), exc_info=True)
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Service dependency unavailable: {ce}")
    except HTTPException as http_exc: # Re-lanzar HTTPExceptions controladas (e.g., 401, 403)
        raise http_exc
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
from typing import Any, Optional, Dict, List, Tuple
import asyncpg
import structlog
import json
from datetime import datetime

from app.core.config import settings
# --- Importar Schemas para type hinting (opcional pero útil) ---
from app.api.v1 import schemas # Importar schemas para usar ChatSummary, ChatMessage, etc.

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
                # --- FIX: Add statement_cache_size=0 for PgBouncer compatibility ---
                statement_cache_size=0,
                # -----------------------------------------------------------------
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
        # --- Catch the specific error causing the issue ---
        except asyncpg.exceptions.DuplicatePreparedStatementError as e:
             log.error("Failed to create Supabase/PostgreSQL connection pool due to prepared statement conflict (likely PgBouncer issue). Check statement_cache_size setting.", error=str(e), exc_info=True)
             raise
        # --- Generic catch-all ---
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
    user_id: Optional[uuid.UUID],
    query: str,
    response: str,
    retrieved_doc_ids: List[str],
    retrieved_doc_scores: List[float],
    # --- Añadir chat_id ---
    chat_id: Optional[uuid.UUID] = None,
    metadata: Optional[Dict[str, Any]] = None
) -> uuid.UUID:
    """
    Registra una interacción de consulta en la tabla QUERY_LOGS, incluyendo el chat_id.
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

    avg_relevance_score = sum(retrieved_doc_scores) / len(retrieved_doc_scores) if retrieved_doc_scores else None

    # --- Modificar query para incluir chat_id ---
    query_sql = """
        INSERT INTO query_logs
               (id, company_id, user_id, query, response, relevance_score, metadata, chat_id, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW() AT TIME ZONE 'UTC')
        RETURNING id;
    """

    try:
        async with pool.acquire() as connection:
            result = await connection.fetchval(
                query_sql,
                log_id,
                company_id,
                user_id,
                query,
                response,
                avg_relevance_score,
                log_metadata,
                chat_id # Pasar el chat_id
            )
        if result:
            log.info("Query interaction logged successfully", query_log_id=str(result), company_id=str(company_id), user_id=str(user_id) if user_id else "N/A", chat_id=str(chat_id) if chat_id else "N/A")
            return result
        else:
             log.error("Failed to log query interaction, no ID returned.", log_id_attempted=str(log_id))
             raise RuntimeError("Failed to log query interaction.")
    except Exception as e:
        log.error("Failed to log query interaction to Supabase",
                  error=str(e), log_id_attempted=str(log_id), company_id=str(company_id), chat_id=str(chat_id) if chat_id else "N/A",
                  exc_info=True)
        raise

async def check_db_connection() -> bool:
    """Verifica si se puede establecer una conexión básica con la BD."""
    pool = None
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            # Usar una consulta simple que no dependa de prepared statements por defecto
            await conn.execute("SELECT 1")
        return True
    except Exception:
        log.error("Database connection check failed", exc_info=True) # Loguear el error
        return False

# --- Funciones para CHATS ---

async def create_chat(user_id: uuid.UUID, company_id: uuid.UUID, title: Optional[str] = None) -> uuid.UUID:
    """Crea un nuevo chat para un usuario y empresa."""
    pool = await get_db_pool()
    chat_id = uuid.uuid4()
    # Usar NOW() para created_at y updated_at iniciales
    query = """
        INSERT INTO chats (id, user_id, company_id, title, created_at, updated_at)
        VALUES ($1, $2, $3, $4, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC')
        RETURNING id;
    """
    try:
        async with pool.acquire() as connection:
            created_id = await connection.fetchval(query, chat_id, user_id, company_id, title)
        if created_id:
             log.info("Chat created successfully", chat_id=str(created_id), user_id=str(user_id), company_id=str(company_id))
             return created_id
        else:
             log.error("Failed to create chat, no ID returned", attempted_id=str(chat_id))
             raise RuntimeError("Failed to create chat")
    except Exception as e:
        log.error("Error creating chat in database", error=str(e), user_id=str(user_id), company_id=str(company_id), exc_info=True)
        raise

async def get_chats_for_user(user_id: uuid.UUID, company_id: uuid.UUID) -> List[schemas.ChatSummary]:
    """Obtiene la lista de chats para un usuario y empresa, ordenados por última actualización."""
    pool = await get_db_pool()
    query = """
        SELECT id, title, updated_at
        FROM chats
        WHERE user_id = $1 AND company_id = $2
        ORDER BY updated_at DESC;
    """
    try:
        async with pool.acquire() as connection:
            records = await connection.fetch(query, user_id, company_id)
        chats = [schemas.ChatSummary(id=r['id'], title=r['title'], updated_at=r['updated_at']) for r in records]
        log.debug("Fetched chats for user", user_id=str(user_id), company_id=str(company_id), count=len(chats))
        return chats
    except Exception as e:
        log.error("Error fetching chats for user", error=str(e), user_id=str(user_id), company_id=str(company_id), exc_info=True)
        raise # O devolver lista vacía? Por ahora lanzamos error.

async def delete_chat(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    """Elimina un chat y sus mensajes asociados, verificando la propiedad."""
    pool = await get_db_pool()
    # Usar una transacción para asegurar que se borre el chat y los mensajes atomicamente
    try:
        async with pool.acquire() as connection:
            async with connection.transaction():
                # 1. Verificar propiedad y existencia del chat
                owner_check = await connection.fetchval(
                    "SELECT id FROM chats WHERE id = $1 AND user_id = $2 AND company_id = $3",
                    chat_id, user_id, company_id
                )
                if not owner_check:
                    log.warning("Attempt to delete chat not found or not owned by user", chat_id=str(chat_id), user_id=str(user_id), company_id=str(company_id))
                    return False # O lanzar una excepción específica de "NotFound" o "Forbidden"

                # 2. Borrar mensajes asociados (ON DELETE CASCADE podría manejar esto también si está configurado)
                # Es más explícito borrar primero los mensajes si no hay CASCADE.
                # Usamos execute que devuelve el status tag (e.g., "DELETE 5")
                deleted_messages_status = await connection.execute("DELETE FROM messages WHERE chat_id = $1", chat_id)
                log.debug("Executed delete for associated messages", chat_id=str(chat_id), status=deleted_messages_status)

                # 3. Borrar el chat
                result = await connection.execute("DELETE FROM chats WHERE id = $1", chat_id)

                # El status devuelto es como "DELETE 1" o "DELETE 0"
                deleted_count = int(result.split(" ")[1]) if result and result.startswith("DELETE") else 0
                if deleted_count > 0:
                    log.info("Chat deleted successfully", chat_id=str(chat_id), user_id=str(user_id), company_id=str(company_id))
                    return True
                else:
                    # Esto no debería ocurrir si owner_check pasó, pero por seguridad
                    log.warning("Chat deletion command executed but reported 0 rows affected", chat_id=str(chat_id))
                    return False # O podría indicar un problema si owner_check pasó
    except Exception as e:
        log.error("Error deleting chat", error=str(e), chat_id=str(chat_id), user_id=str(user_id), exc_info=True)
        raise

async def check_chat_ownership(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    """Verifica si un chat pertenece al usuario y compañía dados."""
    pool = await get_db_pool()
    query = "SELECT EXISTS (SELECT 1 FROM chats WHERE id = $1 AND user_id = $2 AND company_id = $3)"
    try:
        async with pool.acquire() as connection:
            exists = await connection.fetchval(query, chat_id, user_id, company_id)
        return bool(exists) # Asegurarse de devolver un booleano
    except Exception as e:
        log.error("Error checking chat ownership", error=str(e), chat_id=str(chat_id), user_id=str(user_id), exc_info=True)
        return False # Asumir que no si hay error


# --- Funciones para MESSAGES ---

async def add_message_to_chat(
    chat_id: uuid.UUID,
    role: str, # 'user' o 'assistant'
    content: str,
    sources: Optional[List[Dict[str, Any]]] = None
) -> uuid.UUID:
    """Añade un mensaje a un chat y actualiza el timestamp del chat."""
    pool = await get_db_pool()
    message_id = uuid.uuid4()

    # Usar una transacción para insertar mensaje y actualizar chat
    try:
        async with pool.acquire() as connection:
            async with connection.transaction():
                # 1. Insertar el mensaje
                insert_query = """
                    INSERT INTO messages (id, chat_id, role, content, sources, created_at)
                    VALUES ($1, $2, $3, $4, $5, NOW() AT TIME ZONE 'UTC')
                    RETURNING id;
                """
                created_id = await connection.fetchval(
                    insert_query, message_id, chat_id, role, content, sources # sources es jsonb
                )

                if not created_id:
                    log.error("Failed to insert message, no ID returned", attempted_id=str(message_id), chat_id=str(chat_id))
                    # Forzar rollback de la transacción lanzando error
                    raise RuntimeError("Failed to insert message, transaction rolled back")

                # 2. Actualizar el timestamp 'updated_at' del chat
                update_query = """
                    UPDATE chats
                    SET updated_at = NOW() AT TIME ZONE 'UTC'
                    WHERE id = $1;
                """
                update_status = await connection.execute(update_query, chat_id)
                # Verificar si la actualización afectó alguna fila (el chat existe)
                if not update_status or not update_status.startswith("UPDATE 1"):
                    log.error("Failed to update chat timestamp after adding message, chat ID might be invalid", chat_id=str(chat_id), update_status=update_status)
                    # Forzar rollback de la transacción lanzando error
                    raise RuntimeError(f"Failed to update timestamp for chat {chat_id}, transaction rolled back")


        log.info("Message added to chat successfully", message_id=str(created_id), chat_id=str(chat_id), role=role)
        return created_id
    except asyncpg.exceptions.ForeignKeyViolationError:
         log.error("Error adding message: Chat ID does not exist", chat_id=str(chat_id), role=role)
         # Lanzar un error específico o devolver None/False podría ser mejor aquí
         raise ValueError(f"Chat with ID {chat_id} not found.") from None # from None para evitar chain de excepciones innecesario
    except Exception as e:
        log.error("Error adding message to chat", error=str(e), chat_id=str(chat_id), role=role, exc_info=True)
        raise

async def get_messages_for_chat(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> List[schemas.ChatMessage]:
    """Obtiene los mensajes de un chat específico, verificando la propiedad y ordenando por fecha."""
    pool = await get_db_pool()

    # Primero, verificar si el usuario tiene acceso a este chat
    if not await check_chat_ownership(chat_id, user_id, company_id):
        log.warning("Attempt to access messages for chat not owned or non-existent", chat_id=str(chat_id), user_id=str(user_id), company_id=str(company_id))
        # Podríamos lanzar una excepción aquí (403 Forbidden o 404 Not Found)
        # O simplemente devolver una lista vacía. Devolver vacío es más simple para el endpoint.
        return []

    # Si tiene acceso, obtener los mensajes
    query = """
        SELECT id, role, content, sources, created_at
        FROM messages
        WHERE chat_id = $1
        ORDER BY created_at ASC;
    """
    try:
        async with pool.acquire() as connection:
            records = await connection.fetch(query, chat_id)
        messages = [
            schemas.ChatMessage(
                id=r['id'],
                role=r['role'],
                content=r['content'],
                sources=r['sources'], # Directamente desde JSONB
                created_at=r['created_at']
            ) for r in records
        ]
        log.debug("Fetched messages for chat", chat_id=str(chat_id), count=len(messages))
        return messages
    except Exception as e:
        log.error("Error fetching messages for chat", error=str(e), chat_id=str(chat_id), exc_info=True)
        raise # O devolver lista vacía? Por ahora lanzamos error.
```

## File: `app\main.py`
```py
# ./app/main.py
from fastapi import FastAPI, HTTPException, status as fastapi_status
# --- CORRECCIÓN: Importar RequestValidationError ---
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
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
from app.api.v1.endpoints import query as query_router
from app.api.v1.endpoints import chat as chat_router
from app.db import postgres_client
from app.pipelines.rag_pipeline import build_rag_pipeline, check_pipeline_dependencies
from app.api.v1 import schemas
from app.utils import helpers # Si se usan helpers globales

# Estado global
SERVICE_READY = False
GLOBAL_RAG_PIPELINE = None

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.2.0", # Mantener versión incrementada
    description="Microservice to handle user queries using a Haystack RAG pipeline with Milvus and Gemini, including chat history management.",
)

# --- Event Handlers (Sin cambios en lógica) ---
@app.on_event("startup")
async def startup_event():
    global SERVICE_READY, GLOBAL_RAG_PIPELINE
    log.info(f"Starting up {settings.PROJECT_NAME}...")
    db_pool_initialized = False
    pipeline_built = False
    milvus_startup_ok = False
    try:
        await postgres_client.get_db_pool()
        db_ready = await postgres_client.check_db_connection()
        if db_ready: log.info("PostgreSQL connection pool initialized and connection verified."); db_pool_initialized = True
        else: log.critical("CRITICAL: Failed to verify PostgreSQL connection on startup."); SERVICE_READY = False; return
    except Exception as e:
        log.critical("CRITICAL: Failed to establish essential PostgreSQL connection pool on startup.", error=str(e), exc_info=True); SERVICE_READY = False; return
    try:
        GLOBAL_RAG_PIPELINE = build_rag_pipeline()
        dep_status = await check_pipeline_dependencies()
        if dep_status.get("milvus_connection") == "ok": log.info("Haystack RAG pipeline built and Milvus connection verified during startup."); pipeline_built = True; milvus_startup_ok = True
        else: log.warning("Haystack RAG pipeline built, but Milvus connection check failed during startup.", milvus_status=dep_status.get("milvus_connection")); pipeline_built = True; milvus_startup_ok = False
    except Exception as e:
        log.error("Failed to build Haystack RAG pipeline during startup.", error=str(e), exc_info=True); pipeline_built = False; milvus_startup_ok = False
    if db_pool_initialized and pipeline_built:
        SERVICE_READY = True
        log.info(f"{settings.PROJECT_NAME} startup sequence completed. Service marked as READY. Initial Milvus Check: {'OK' if milvus_startup_ok else 'Failed'}")
    else:
        SERVICE_READY = False
        if db_pool_initialized and not pipeline_built: log.critical(f"{settings.PROJECT_NAME} startup failed because RAG pipeline could not be built. Service marked as NOT READY.")


@app.on_event("shutdown")
async def shutdown_event():
    log.info(f"Shutting down {settings.PROJECT_NAME}...")
    await postgres_client.close_db_pool()
    log.info("PostgreSQL connection pool closed.")
    log.info(f"{settings.PROJECT_NAME} shutdown complete.")

# --- Exception Handlers ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    log_level = log.warning if exc.status_code < 500 and exc.status_code != 404 else log.error
    log_level("HTTP Exception caught", status_code=exc.status_code, detail=exc.detail, path=str(request.url))
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

# --- Manejador donde ocurría el error ---
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError): # Añadir tipo para claridad
    # Usar exc.errors() para obtener detalles específicos
    log.warning("Request Validation Error", errors=exc.errors(), path=str(request.url))
    # Formatear los errores para la respuesta JSON
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
app.include_router(query_router.router, prefix="/api/v1/query", tags=["Query Interaction"])
app.include_router(chat_router.router, prefix="/api/v1/query", tags=["Chat Management"]) # Chat uses a distinct prefix

# --- Root Endpoint / Health Check (Sin cambios) ---
@app.get("/", tags=["Health Check"], summary="Service Liveness/Readiness Check", description="Basic Kubernetes probe endpoint. Returns 200 OK with 'OK' text if the service started successfully, otherwise 503.")
async def read_root():
    global SERVICE_READY
    log.debug("Root endpoint accessed (health check)")
    if SERVICE_READY:
        log.debug("Health check successful (service ready).")
        return Response(content="OK", status_code=fastapi_status.HTTP_200_OK, media_type="text/plain")
    else:
        log.warning("Health check failed: Service not ready (SERVICE_READY is False).")
        raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service is not ready or failed during startup.")


# --- Main execution (for local development) (Sin cambios)  ---
if __name__ == "__main__":
    log.info(f"Starting Uvicorn server for {settings.PROJECT_NAME} local development...")
    log_level_str = settings.LOG_LEVEL.lower()
    if log_level_str not in logging._nameToLevel: log.warning(f"Invalid LOG_LEVEL '{settings.LOG_LEVEL}', defaulting Uvicorn log level to 'info'."); log_level_str = "info"
    uvicorn.run("app.main:app", host="0.0.0.0", port=8001, reload=True, log_level=log_level_str)
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

from pymilvus.exceptions import MilvusException
# --- Añadir HTTPException para poder lanzarla si falla la construcción ---
from fastapi import HTTPException, status

from haystack import Pipeline, Document
from haystack.components.embedders import OpenAITextEmbedder
from haystack.components.builders.prompt_builder import PromptBuilder
from milvus_haystack import MilvusDocumentStore, MilvusEmbeddingRetriever
from haystack.utils import Secret

from app.core.config import settings
from app.db import postgres_client
from app.services.gemini_client import gemini_client

log = structlog.get_logger(__name__)

# --- Component Initialization Functions (Sin cambios) ---
def get_milvus_document_store() -> MilvusDocumentStore:
    connection_uri = str(settings.MILVUS_URI)
    connection_timeout = 30.0
    log.debug("Initializing MilvusDocumentStore for Query Service", connection_uri=connection_uri, collection=settings.MILVUS_COLLECTION_NAME, search_params=settings.MILVUS_SEARCH_PARAMS, connection_timeout=connection_timeout)
    try:
        store = MilvusDocumentStore(
            connection_args={"uri": connection_uri, "timeout": connection_timeout},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            search_params=settings.MILVUS_SEARCH_PARAMS,
            consistency_level="Strong",
        )
        store.count_documents()
        log.info("MilvusDocumentStore initialized and connection verified successfully")
        return store
    except MilvusException as e:
        log.error("Failed to initialize or connect to MilvusDocumentStore", error_code=e.code, error_message=e.message, connection_uri=connection_uri, collection=settings.MILVUS_COLLECTION_NAME, exc_info=True)
        raise RuntimeError(f"Could not connect to Milvus at {connection_uri}. Error code {e.code}: {e.message}. Check Milvus service status, network connectivity/policies, and credentials.") from e
    except Exception as e:
        log.error("Unexpected error during MilvusDocumentStore initialization", error=str(e), exc_info=True)
        raise RuntimeError(f"Unexpected error initializing Milvus Document Store: {e}") from e

def get_openai_text_embedder() -> OpenAITextEmbedder:
    log.debug("Initializing OpenAITextEmbedder", model=settings.OPENAI_EMBEDDING_MODEL)
    api_key_secret = Secret.from_env_var("QUERY_OPENAI_API_KEY")
    if not api_key_secret.resolve_value(): log.warning("QUERY_OPENAI_API_KEY environment variable not found or empty for OpenAI Embedder.")
    return OpenAITextEmbedder(api_key=api_key_secret, model=settings.OPENAI_EMBEDDING_MODEL)

def get_milvus_retriever(document_store: MilvusDocumentStore) -> MilvusEmbeddingRetriever:
    log.debug("Initializing MilvusEmbeddingRetriever")
    return MilvusEmbeddingRetriever(document_store=document_store)

def get_prompt_builder() -> PromptBuilder:
    log.debug("Initializing PromptBuilder", template_preview=settings.RAG_PROMPT_TEMPLATE[:100] + "...")
    return PromptBuilder(template=settings.RAG_PROMPT_TEMPLATE)

# --- Pipeline Construction (Sin cambios) ---
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

# --- Pipeline Execution (Modificado) ---
async def run_rag_pipeline(
    query: str,
    company_id: str,
    user_id: Optional[str], # Viene como string o None desde el endpoint
    top_k: Optional[int] = None,
    # --- Añadir chat_id ---
    chat_id: Optional[uuid.UUID] = None # Recibir el UUID
) -> Tuple[str, List[Document], Optional[uuid.UUID]]:
    """
    Runs the RAG pipeline for a given query and company_id, passing chat_id for logging.
    """
    run_log = log.bind(query=query, company_id=company_id, user_id=user_id or "N/A", chat_id=str(chat_id) if chat_id else "N/A")
    run_log.info("Running RAG pipeline...")

    try:
        pipeline = build_rag_pipeline()
    except Exception as build_err:
         run_log.error("Failed to get or build RAG pipeline for execution", error=str(build_err))
         # Usar status de FastAPI
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="RAG pipeline is not available.")

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
        # --- Ejecución del pipeline Haystack (embed, retrieve, prompt) ---
        loop = asyncio.get_running_loop()
        pipeline_result = await loop.run_in_executor(
            None,
            lambda: pipeline.run(pipeline_input, include_outputs_from=["retriever", "prompt_builder"])
        )
        run_log.info("Haystack pipeline (embed, retrieve, prompt) executed successfully.")

        retrieved_docs: List[Document] = pipeline_result.get("retriever", {}).get("documents", [])
        prompt_builder_output = pipeline_result.get("prompt_builder", {})
        generated_prompt: Optional[str] = None

        # Extraer prompt (misma lógica que antes)
        if "prompt" in prompt_builder_output:
             prompt_data = prompt_builder_output["prompt"]
             if isinstance(prompt_data, list): text_parts = [msg.content for msg in prompt_data if hasattr(msg, 'content') and isinstance(msg.content, str)]; generated_prompt = "\n".join(text_parts)
             elif isinstance(prompt_data, str): generated_prompt = prompt_data
             else: run_log.warning("Unexpected prompt format from prompt_builder", prompt_type=type(prompt_data)); generated_prompt = str(prompt_data)
        if not retrieved_docs:
             run_log.warning("No relevant documents found by retriever.")
             if not generated_prompt: generated_prompt = f"Pregunta: {query}\n\nNo se encontraron documentos relevantes. Intenta responder brevemente si es posible, o indica que no tienes información."
        if not generated_prompt:
             run_log.error("Failed to extract or generate prompt from pipeline output", output=prompt_builder_output)
             raise ValueError("Could not construct prompt for LLM.")
        run_log.debug("Generated prompt for LLM", prompt_preview=generated_prompt[:200] + "...")

        # --- Llamada a Gemini (sin cambios) ---
        answer = await gemini_client.generate_answer(generated_prompt)
        run_log.info("Answer generated by Gemini", answer_preview=answer[:100] + "...")

        # --- Logging de la interacción (Pasar chat_id) ---
        log_id: Optional[uuid.UUID] = None
        try:
            doc_ids = [doc.id for doc in retrieved_docs]
            doc_scores = [doc.score for doc in retrieved_docs if doc.score is not None]
            # Convertir user_id (string) de nuevo a UUID si no es None
            user_uuid = uuid.UUID(user_id) if user_id else None
            company_uuid = uuid.UUID(company_id) # company_id siempre debe ser un UUID válido aquí

            log_id = await postgres_client.log_query_interaction(
                company_id=company_uuid,
                user_id=user_uuid, # Puede ser None
                query=query,
                response=answer,
                retrieved_doc_ids=doc_ids,
                retrieved_doc_scores=doc_scores,
                chat_id=chat_id, # Pasar el chat_id (UUID o None)
                metadata={"retriever_top_k": retriever_top_k}
            )
        except Exception as log_err:
             run_log.error("Failed to log query interaction to database", error=str(log_err), exc_info=True)
             # No fallar la respuesta principal si el log falla, pero el ID será None

        # Devolver la respuesta, los documentos de Haystack y el log_id
        return answer, retrieved_docs, log_id

    except HTTPException as http_exc: # Re-lanzar excepciones HTTP
        raise http_exc
    except Exception as e:
        run_log.exception("Error occurred during RAG pipeline execution")
        # Usar status de FastAPI
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error processing query: {type(e).__name__}")


# --- check_pipeline_dependencies (Sin cambios) ---
async def check_pipeline_dependencies() -> Dict[str, str]:
    results = {"milvus_connection": "pending"}
    try:
        store = get_milvus_document_store()
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
