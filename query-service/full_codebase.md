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
import re # LLM_COMMENT: Import regex for greeting detection

from fastapi import APIRouter, Depends, HTTPException, status, Header, Body, Request

from app.api.v1 import schemas
from app.core.config import settings
from app.db import postgres_client
# LLM_COMMENT: Import the refactored pipeline function
from app.pipelines.rag_pipeline import run_rag_pipeline
from haystack import Document # LLM_COMMENT: Keep Haystack Document import
from app.utils.helpers import truncate_text
# LLM_COMMENT: Import shared dependencies from chat module
from .chat import get_current_company_id, get_current_user_id

log = structlog.get_logger(__name__)

router = APIRouter()

# LLM_COMMENT: Simple regex to detect greetings (adapt as needed)
GREETING_REGEX = re.compile(r"^\s*(hola|hello|hi|buenos días|buenas tardes|buenas noches|hey|qué tal|hi there)\s*[\.,!?]*\s*$", re.IGNORECASE)

# --- Endpoint Principal Modificado para usar dependencias de X- Headers ---
@router.post(
    "/ask", # LLM_COMMENT: Standardized internal endpoint path
    response_model=schemas.QueryResponse,
    status_code=status.HTTP_200_OK,
    summary="Process Query / Manage Chat", # LLM_COMMENT: Updated summary
    description="Handles user queries, manages chat state (create/continue), performs RAG or simple response, and logs interaction. Uses X-Company-ID and X-User-ID.", # LLM_COMMENT: Updated description
)
async def process_query(
    request_body: schemas.QueryRequest = Body(...),
    # LLM_COMMENT: Use dependencies to get authenticated user/company IDs
    company_id: uuid.UUID = Depends(get_current_company_id),
    user_id: uuid.UUID = Depends(get_current_user_id),
    request: Request = None # LLM_COMMENT: Inject Request for potential header access
):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4())) if request else str(uuid.uuid4())
    endpoint_log = log.bind(
        request_id=request_id,
        company_id=str(company_id),
        user_id=str(user_id),
        # LLM_COMMENT: Log truncated query for brevity
        query=truncate_text(request_body.query, 100),
        provided_chat_id=str(request_body.chat_id) if request_body.chat_id else "None"
    )
    endpoint_log.info("Processing query request")

    current_chat_id: uuid.UUID
    is_new_chat = False

    try:
        # --- 1. Determine Chat ID ---
        # LLM_COMMENT: Check if continuing an existing chat or starting a new one
        if request_body.chat_id:
            endpoint_log.debug("Checking ownership of existing chat", chat_id=str(request_body.chat_id))
            # LLM_COMMENT: Verify user owns the chat using DB check
            if not await postgres_client.check_chat_ownership(request_body.chat_id, user_id, company_id):
                 endpoint_log.warning("Access denied or chat not found for provided chat_id")
                 raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Chat not found or access denied.")
            current_chat_id = request_body.chat_id
            endpoint_log = endpoint_log.bind(chat_id=str(current_chat_id))
            endpoint_log.info("Continuing existing chat")
        else:
            endpoint_log.info("No chat_id provided, creating new chat")
            # LLM_COMMENT: Generate a title based on the first query, truncated
            initial_title = f"Chat: {truncate_text(request_body.query, 40)}"
            current_chat_id = await postgres_client.create_chat(user_id=user_id, company_id=company_id, title=initial_title)
            is_new_chat = True
            endpoint_log = endpoint_log.bind(chat_id=str(current_chat_id))
            endpoint_log.info("New chat created", title=initial_title)

        # --- 2. Save User Message ---
        # LLM_COMMENT: Always save the user's message regardless of intent
        endpoint_log.info("Saving user message to DB")
        await postgres_client.save_message(
            chat_id=current_chat_id, role='user', content=request_body.query
        )
        endpoint_log.info("User message saved")

        # --- 3. Handle Greetings / Simple Intents (Bypass RAG) ---
        # LLM_COMMENT: Check if the query matches a simple greeting pattern
        if GREETING_REGEX.match(request_body.query):
            endpoint_log.info("Greeting detected, bypassing RAG pipeline")
            # LLM_COMMENT: Provide a canned, friendly response for greetings
            answer = "¡Hola! ¿En qué puedo ayudarte hoy con la información de tus documentos?"
            retrieved_docs_for_response: List[schemas.RetrievedDocument] = [] # LLM_COMMENT: No documents retrieved for greetings
            log_id = None # LLM_COMMENT: Optionally skip logging greetings, or log differently

            # Save canned assistant response
            await postgres_client.save_message(
                 chat_id=current_chat_id, role='assistant', content=answer, sources=None
            )
            endpoint_log.info("Saved canned greeting response")

        # --- 4. Execute RAG Pipeline (for non-greetings) ---
        else:
            endpoint_log.info("Proceeding with RAG pipeline execution")
            # LLM_COMMENT: Call the refactored pipeline function
            answer, retrieved_docs_haystack, log_id = await run_rag_pipeline(
                query=request_body.query,
                company_id=str(company_id),
                user_id=str(user_id),
                top_k=request_body.retriever_top_k, # LLM_COMMENT: Pass top_k if provided
                chat_id=current_chat_id # LLM_COMMENT: Pass chat_id for logging context
            )
            endpoint_log.info("RAG pipeline finished")

            # LLM_COMMENT: Format retrieved Haystack Documents into response schema
            retrieved_docs_for_response = [schemas.RetrievedDocument.from_haystack_doc(doc)
                                           for doc in retrieved_docs_haystack]

            # LLM_COMMENT: Prepare sources list specifically for saving in the assistant message
            assistant_sources_for_db: List[Dict[str, Any]] = []
            for schema_doc in retrieved_docs_for_response:
                source_info = {
                    "chunk_id": schema_doc.id,
                    "document_id": schema_doc.document_id,
                    "file_name": schema_doc.file_name,
                    "score": schema_doc.score,
                    "preview": schema_doc.content_preview # LLM_COMMENT: Include preview in stored sources
                }
                assistant_sources_for_db.append(source_info)

            # LLM_COMMENT: Save the actual assistant message generated by the pipeline
            endpoint_log.info("Saving assistant message from pipeline")
            await postgres_client.save_message(
                chat_id=current_chat_id,
                role='assistant',
                content=answer,
                sources=assistant_sources_for_db if assistant_sources_for_db else None # LLM_COMMENT: Store formatted sources
            )
            endpoint_log.info("Assistant message saved")

        # --- 5. Return Response ---
        endpoint_log.info("Query processed successfully, returning response", is_new_chat=is_new_chat, num_retrieved=len(retrieved_docs_for_response))
        return schemas.QueryResponse(
            answer=answer,
            retrieved_documents=retrieved_docs_for_response, # LLM_COMMENT: Return formatted documents
            query_log_id=log_id, # LLM_COMMENT: Include the log_id if logging was successful
            chat_id=current_chat_id # LLM_COMMENT: Always return the current chat_id
        )

    # LLM_COMMENT: Keep existing robust error handling, map pipeline errors if needed
    except ValueError as ve:
        endpoint_log.warning("Input validation error", error=str(ve), exc_info=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))
    except ConnectionError as ce: # LLM_COMMENT: Catch ConnectionError from pipeline steps
        endpoint_log.error("Dependency connection error", error=str(ce), exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"A required service is unavailable: {ce}")
    except HTTPException as http_exc:
        # LLM_COMMENT: Re-raise specific HTTP exceptions (like 403 from check_chat_ownership)
        raise http_exc
    except Exception as e:
        endpoint_log.exception("Unhandled exception during query processing")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"An internal error occurred: {type(e).__name__}")
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
# query-service/app/core/config.py
import logging
import os
from typing import Optional, List, Any, Dict
from pydantic_settings import BaseSettings, SettingsConfigDict
# LLM_COMMENT: Correct Pydantic v2 imports
from pydantic import AnyHttpUrl, SecretStr, Field, field_validator, ValidationError, ValidationInfo
import sys
import json # LLM_COMMENT: Added json import for default factories

# --- PostgreSQL Kubernetes Defaults ---
POSTGRES_K8S_HOST_DEFAULT = "postgresql-service.nyro-develop.svc.cluster.local" # Corrected service name
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex"
POSTGRES_K8S_USER_DEFAULT = "postgres"

# --- Milvus Kubernetes Defaults ---
MILVUS_K8S_DEFAULT_URI = "http://milvus-standalone.nyro-develop.svc.cluster.local:19530" # Corrected service name
# --- CORRECTION: Align collection name with ingest ---
MILVUS_DEFAULT_COLLECTION = "document_chunks_haystack" # Match ingest config
# --- CORRECTION: Align field names with ingest ---
MILVUS_DEFAULT_EMBEDDING_FIELD = "embedding"
MILVUS_DEFAULT_CONTENT_FIELD = "content"
MILVUS_DEFAULT_COMPANY_ID_FIELD = "company_id"
# ---------------------------------------------------
MILVUS_DEFAULT_INDEX_PARAMS = '{"metric_type": "COSINE", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 256}}'
MILVUS_DEFAULT_SEARCH_PARAMS = '{"metric_type": "COSINE", "params": {"ef": 128}}'

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

Respuesta concisa y directa:
"""
DEFAULT_GENERAL_PROMPT_TEMPLATE = """
Eres un asistente de IA llamado Atenex. Responde a la siguiente pregunta del usuario de forma útil y conversacional.
Si no sabes la respuesta o la pregunta no está relacionada con tus capacidades, indícalo amablemente.

Pregunta: {{ query }}

Respuesta:
"""

# --- CORRECTION: Align Embedding Model and Dimension with ingest ---
DEFAULT_FASTEMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2" # Match ingest
DEFAULT_FASTEMBED_QUERY_PREFIX = "query: "
DEFAULT_EMBEDDING_DIMENSION = 384 # Match ingest (MiniLM dimension)
# ------------------------------------------------------------------

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='QUERY_',
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore'
    )

    # --- General ---
    PROJECT_NAME: str = "Atenex Query Service"
    API_V1_STR: str = "/api/v1"
    LOG_LEVEL: str = "INFO"

    # --- Database (PostgreSQL) ---
    POSTGRES_USER: str = POSTGRES_K8S_USER_DEFAULT
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = POSTGRES_K8S_HOST_DEFAULT
    POSTGRES_PORT: int = POSTGRES_K8S_PORT_DEFAULT
    POSTGRES_DB: str = POSTGRES_K8S_DB_DEFAULT

    # --- Vector Store (Milvus) ---
    MILVUS_URI: AnyHttpUrl = Field(default=AnyHttpUrl(MILVUS_K8S_DEFAULT_URI))
    MILVUS_COLLECTION_NAME: str = MILVUS_DEFAULT_COLLECTION
    # --- CORRECTION: Use defaults aligned with ingest ---
    MILVUS_EMBEDDING_FIELD: str = Field(default=MILVUS_DEFAULT_EMBEDDING_FIELD) # Field name for vectors in Milvus
    MILVUS_CONTENT_FIELD: str = Field(default=MILVUS_DEFAULT_CONTENT_FIELD) # Field name for text content in Milvus
    MILVUS_COMPANY_ID_FIELD: str = Field(default=MILVUS_DEFAULT_COMPANY_ID_FIELD) # Field used for tenant filtering in Milvus
    # --------------------------------------------------
    # LLM_COMMENT: Define metadata fields expected in Milvus, MUST include 'company_id' for filtering
    MILVUS_METADATA_FIELDS: List[str] = Field(default=["company_id", "document_id", "file_name", "file_type"]) # Keep others if needed
    # LLM_COMMENT: Use json.loads with default_factory for complex dict defaults
    MILVUS_INDEX_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_INDEX_PARAMS))
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default_factory=lambda: json.loads(MILVUS_DEFAULT_SEARCH_PARAMS))

    # --- Embedding Model (FastEmbed) ---
    # --- CORRECTION: Use aligned defaults ---
    FASTEMBED_MODEL_NAME: str = Field(default=DEFAULT_FASTEMBED_MODEL)
    EMBEDDING_DIMENSION: int = Field(default=DEFAULT_EMBEDDING_DIMENSION) # Dimension matching the FastEmbed model
    FASTEMBED_QUERY_PREFIX: str = Field(default=DEFAULT_FASTEMBED_QUERY_PREFIX) # Prefix for query embedding
    # --------------------------------------

    # --- LLM (Google Gemini) ---
    GEMINI_API_KEY: SecretStr
    GEMINI_MODEL_NAME: str = "gemini-1.5-flash-latest"

    # --- RAG Pipeline Settings ---
    RETRIEVER_TOP_K: int = 5
    RAG_PROMPT_TEMPLATE: str = DEFAULT_RAG_PROMPT_TEMPLATE
    GENERAL_PROMPT_TEMPLATE: str = DEFAULT_GENERAL_PROMPT_TEMPLATE
    MAX_PROMPT_TOKENS: Optional[int] = 7000

    # --- Service Client Config ---
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_RETRIES: int = 2
    HTTP_CLIENT_BACKOFF_FACTOR: float = 1.0

    # --- Validators ---
    @field_validator('LOG_LEVEL')
    @classmethod
    def check_log_level(cls, v: str) -> str:
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        normalized_v = v.upper()
        if normalized_v not in valid_levels:
            raise ValueError(f"Invalid LOG_LEVEL '{v}'. Must be one of {valid_levels}")
        return normalized_v

    @field_validator('POSTGRES_PASSWORD', 'GEMINI_API_KEY', mode='before')
    @classmethod
    def check_secret_value_present(cls, v: Any, info: ValidationInfo) -> Any:
        if v is None or v == "":
            field_name = info.field_name if info.field_name else "Unknown Secret Field"
            raise ValueError(f"Required secret field '{field_name}' cannot be empty.")
        return v

    # --- CORRECTION: Add validator for embedding dimension vs model ---
    @field_validator('EMBEDDING_DIMENSION')
    @classmethod
    def check_embedding_dimension(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0:
            raise ValueError("EMBEDDING_DIMENSION must be a positive integer.")
        # Check against the actual model being used (from defaults or env var)
        model_name = info.data.get('FASTEMBED_MODEL_NAME', DEFAULT_FASTEMBED_MODEL)
        expected_dim = -1
        if 'all-MiniLM-L6-v2' in model_name:
            expected_dim = 384
        elif 'bge-small-en-v1.5' in model_name:
            expected_dim = 384
        elif 'bge-large-en-v1.5' in model_name:
            expected_dim = 1024
        # Add other known models here

        if expected_dim != -1 and v != expected_dim:
            logging.warning(
                f"Configured EMBEDDING_DIMENSION ({v}) differs from standard dimension ({expected_dim}) "
                f"for model '{model_name}'. Ensure this is intentional."
            )
        elif expected_dim == -1:
            logging.warning(
                 f"Unknown embedding dimension for model '{model_name}'. Using configured dimension {v}. "
                 f"Verify this matches the actual model output."
            )

        logging.debug(f"Using EMBEDDING_DIMENSION: {v} for model: {model_name}")
        return v
    # ------------------------------------------------------------------


# --- Instancia Global ---
temp_log = logging.getLogger("query_service.config.loader")
if not temp_log.handlers:
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(levelname)s: [%(name)s] %(message)s')
    handler.setFormatter(formatter)
    temp_log.addHandler(handler)
    temp_log.setLevel(logging.INFO)

try:
    temp_log.info("Loading Query Service settings...")
    settings = Settings()
    temp_log.info("Query Service Settings Loaded Successfully:")
    temp_log.info(f"  PROJECT_NAME: {settings.PROJECT_NAME}")
    temp_log.info(f"  LOG_LEVEL: {settings.LOG_LEVEL}")
    temp_log.info(f"  API_V1_STR: {settings.API_V1_STR}")
    temp_log.info(f"  POSTGRES_SERVER: {settings.POSTGRES_SERVER}:{settings.POSTGRES_PORT}")
    temp_log.info(f"  POSTGRES_DB: {settings.POSTGRES_DB}")
    temp_log.info(f"  POSTGRES_USER: {settings.POSTGRES_USER}")
    temp_log.info(f"  POSTGRES_PASSWORD: *** SET ***")
    temp_log.info(f"  MILVUS_URI: {settings.MILVUS_URI}")
    temp_log.info(f"  MILVUS_COLLECTION_NAME: {settings.MILVUS_COLLECTION_NAME}")
    # --- Log corrected fields ---
    temp_log.info(f"  MILVUS_EMBEDDING_FIELD: {settings.MILVUS_EMBEDDING_FIELD}")
    temp_log.info(f"  MILVUS_CONTENT_FIELD: {settings.MILVUS_CONTENT_FIELD}")
    temp_log.info(f"  MILVUS_COMPANY_ID_FIELD: {settings.MILVUS_COMPANY_ID_FIELD}")
    # --------------------------
    temp_log.info(f"  FASTEMBED_MODEL_NAME: {settings.FASTEMBED_MODEL_NAME}")
    temp_log.info(f"  EMBEDDING_DIMENSION: {settings.EMBEDDING_DIMENSION}")
    temp_log.info(f"  GEMINI_API_KEY: *** SET ***")
    temp_log.info(f"  GEMINI_MODEL_NAME: {settings.GEMINI_MODEL_NAME}")
    temp_log.info(f"  RETRIEVER_TOP_K: {settings.RETRIEVER_TOP_K}")

except (ValidationError, ValueError) as e:
    error_details = ""
    if isinstance(e, ValidationError):
        try: error_details = f"\nValidation Errors:\n{e.json(indent=2)}"
        except Exception: error_details = f"\nRaw Errors: {e.errors()}"
    temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    temp_log.critical(f"! FATAL: Query Service configuration validation failed:{error_details}")
    temp_log.critical(f"! Check environment variables (prefixed with QUERY_) or .env file.")
    temp_log.critical(f"! Original Error: {e}")
    temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    sys.exit(1)
except Exception as e:
    temp_log.exception(f"FATAL: Unexpected error loading Query Service settings: {e}")
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
        async with conn.transaction():
            try:
                # 1. Eliminar mensajes asociados
                delete_messages_query = "DELETE FROM messages WHERE chat_id = $1;"
                await conn.execute(delete_messages_query, chat_id)
                delete_log.info("Associated messages deleted (if any)")

                # 2. Eliminar logs asociados (query_logs)
                delete_logs_query = "DELETE FROM query_logs WHERE chat_id = $1;"
                await conn.execute(delete_logs_query, chat_id)
                delete_log.info("Associated query_logs deleted (if any)")

                # 3. Eliminar el chat
                delete_chat_query = "DELETE FROM chats WHERE id = $1 RETURNING id;"
                deleted_id = await conn.fetchval(delete_chat_query, chat_id)

                success = deleted_id is not None
                if success:
                    delete_log.info("Chat deleted successfully after deleting messages and logs")
                else:
                    delete_log.error("Chat deletion failed after deleting messages/logs, despite ownership check")
                return success
            except Exception as e:
                if isinstance(e, asyncpg.exceptions.ForeignKeyViolationError):
                    delete_log.error("Foreign key violation during chat deletion transaction (unexpected)", error=str(e), exc_info=True)
                else:
                    delete_log.error("Failed to delete chat within transaction", error=str(e), exc_info=True)
                raise
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
import uuid

# Configurar logging primero
from app.core.config import settings
from app.core.logging_config import setup_logging
setup_logging() # LLM_COMMENT: Initialize logging early
log = structlog.get_logger("query_service.main")

# Importar routers y otros módulos
from app.api.v1.endpoints import query as query_router_module
from app.api.v1.endpoints import chat as chat_router_module
from app.db import postgres_client
# LLM_COMMENT: Import dependency check function
from app.pipelines.rag_pipeline import check_pipeline_dependencies
from app.api.v1 import schemas # LLM_COMMENT: Keep schema import

# Estado global
SERVICE_READY = False # LLM_COMMENT: Flag indicating if service dependencies are met

# --- Lifespan Manager (async context manager for FastAPI >= 0.110) ---
# LLM_COMMENT: Use modern lifespan context manager for startup/shutdown logic
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    global SERVICE_READY
    log.info(f"Starting up {settings.PROJECT_NAME}...")
    db_pool_initialized = False
    dependencies_ok = False

    # 1. Initialize DB Pool
    try:
        await postgres_client.get_db_pool()
        db_ready = await postgres_client.check_db_connection()
        if db_ready:
            log.info("PostgreSQL connection pool initialized and verified.")
            db_pool_initialized = True
        else:
            log.critical("CRITICAL: Failed PostgreSQL connection verification during startup.")
    except Exception as e:
        log.critical("CRITICAL: Failed PostgreSQL pool initialization during startup.", error=str(e), exc_info=True)

    # 2. Check other dependencies (Milvus, Gemini Key) if DB is OK
    if db_pool_initialized:
        try:
            dependency_status = await check_pipeline_dependencies()
            log.info("Pipeline dependency check completed", status=dependency_status)
            # Define "OK" criteria (Milvus connectable, Gemini key present)
            # LLM_COMMENT: Adjust readiness check based on dependency status reporting
            milvus_ok = "ok" in dependency_status.get("milvus_connection", "error")
            gemini_ok = "key_present" in dependency_status.get("gemini_api", "key_missing")
            fastembed_ok = "configured" in dependency_status.get("fastembed_model", "config_missing")

            if milvus_ok and gemini_ok and fastembed_ok:
                 dependencies_ok = True
            else:
                 log.warning("One or more pipeline dependencies are not ready.", milvus=milvus_ok, gemini=gemini_ok, fastembed=fastembed_ok)

        except Exception as dep_err:
            log.error("Error checking pipeline dependencies during startup", error=str(dep_err), exc_info=True)
    else:
        log.error("Skipping dependency checks because DB pool failed to initialize.")


    # 3. Set Service Readiness
    if db_pool_initialized and dependencies_ok:
        SERVICE_READY = True
        log.info(f"{settings.PROJECT_NAME} service components initialized. SERVICE READY.")
    else:
        SERVICE_READY = False
        log.critical(f"{settings.PROJECT_NAME} startup finished. DB OK: {db_pool_initialized}, Deps OK: {dependencies_ok}. SERVICE NOT READY.")

    yield # Application runs here

    # --- Shutdown ---
    log.info(f"Shutting down {settings.PROJECT_NAME}...")
    await postgres_client.close_db_pool()
    # LLM_COMMENT: Add shutdown for other clients if necessary (e.g., Gemini client if it holds resources)
    log.info("Shutdown complete.")


# --- Creación de la App FastAPI ---
# LLM_COMMENT: Apply lifespan manager to FastAPI app instance
app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.2.4", # LLM_COMMENT: Incremented version for lifespan and prefix fix
    description="Microservice to handle user queries using a Haystack RAG pipeline with Milvus and Gemini, including chat history management. Expects /api/v1 prefix.",
    lifespan=lifespan # LLM_COMMENT: Use the new lifespan manager
)

# --- Middlewares (Sin cambios significativos) ---
@app.middleware("http")
async def add_request_id_timing_logging(request: Request, call_next):
    start_time = asyncio.get_event_loop().time()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    # Bind core info early for all request logs
    structlog.contextvars.bind_contextvars(request_id=request_id)
    req_log = log.bind(method=request.method, path=str(request.url.path), client=request.client.host if request.client else "unknown")
    req_log.info("Request received")

    response = None
    try:
        response = await call_next(request)
        process_time = (asyncio.get_event_loop().time() - start_time) * 1000 # milliseconds
        # Bind response info for final log
        resp_log = req_log.bind(status_code=response.status_code, duration_ms=round(process_time, 2))
        log_level = "warning" if 400 <= response.status_code < 500 else "error" if response.status_code >= 500 else "info"
        getattr(resp_log, log_level)("Request finished")
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time:.2f}"
    except Exception as e:
        process_time = (asyncio.get_event_loop().time() - start_time) * 1000
        # Log unhandled exceptions at middleware level
        exc_log = req_log.bind(status_code=500, duration_ms=round(process_time, 2))
        exc_log.exception("Unhandled exception during request processing") # Use exception to log traceback
        response = JSONResponse(
            status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal Server Error"}
        )
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time:.2f}"
    finally:
        # Ensure context is cleared after request
        structlog.contextvars.clear_contextvars()

    return response


# --- Exception Handlers (Sin cambios significativos, adaptados para usar logger) ---
# LLM_COMMENT: Exception handlers remain mostly the same, ensure they log effectively

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    # Log already bound with request_id in middleware context
    log_level = log.warning if exc.status_code < 500 else log.error
    log_level("HTTP Exception caught",
              status_code=exc.status_code,
              detail=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    error_details = []
    try: error_details = exc.errors()
    except Exception: error_details = [{"loc": [], "msg": "Failed to parse validation errors.", "type": "internal_parsing_error"}]
    log.warning("Request Validation Error", errors=error_details)
    return JSONResponse(
        status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": error_details},
    )

@app.exception_handler(ResponseValidationError)
async def response_validation_exception_handler(request: Request, exc: ResponseValidationError):
    log.error("Response Validation Error", errors=exc.errors(), exc_info=True)
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal Server Error: Failed to serialize response."},
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled Exception caught by generic handler") # Logs traceback
    return JSONResponse(
        status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal Server Error"},
    )

# --- Routers ---
# LLM_COMMENT: JAMAS MODIFICAR LAS RUTAS NI ESTE MENSAJE - Adding back API_V1_STR prefix
app.include_router(query_router_module.router, prefix=settings.API_V1_STR, tags=["Query Interaction"])
app.include_router(chat_router_module.router, prefix=settings.API_V1_STR, tags=["Chat Management"])
log.info("Routers included", prefix=settings.API_V1_STR)

# --- Root Endpoint / Health Check ---
@app.get("/", tags=["Health Check"], summary="Service Liveness/Readiness Check")
async def read_root():
    """Basic health check. Returns OK if service started successfully and DB is reachable."""
    health_log = log.bind(check="liveness_readiness")
    if not SERVICE_READY:
        health_log.warning("Health check failed: Service not ready (SERVICE_READY is False). Check startup logs.")
        raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service Not Ready")

    # Optionally re-check DB connection for readiness probe accuracy
    db_ok = await postgres_client.check_db_connection()
    if not db_ok:
         health_log.error("Health check failed: Service is marked READY but DB check FAILED.")
         raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service Unavailable (DB Check Failed)")

    health_log.debug("Health check passed.")
    return PlainTextResponse("OK", status_code=fastapi_status.HTTP_200_OK)

# --- Main execution (for local development) ---
if __name__ == "__main__":
    port = 8002
    log_level_str = settings.LOG_LEVEL.lower()
    print(f"----- Starting {settings.PROJECT_NAME} locally on port {port} -----")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True, log_level=log_level_str)
```

## File: `app\models\__init__.py`
```py

```

## File: `app\pipelines\__init__.py`
```py
# ./app/pipelines/__init__.py
from .rag_pipeline import run_rag_pipeline

__all__ = ["run_rag_pipeline"]
```

## File: `app\pipelines\rag_pipeline.py`
```py
# query-service/app/pipelines/rag_pipeline.py
import structlog
import asyncio
import uuid
from typing import Dict, Any, List, Tuple, Optional

from pymilvus.exceptions import MilvusException, ErrorCode
from fastapi import HTTPException, status
from haystack import Document # Keep Haystack Document import
# Import FastEmbed Text Embedder for Haystack
from haystack_integrations.components.embedders.fastembed import FastembedTextEmbedder
from haystack.components.builders.prompt_builder import PromptBuilder
from milvus_haystack import MilvusDocumentStore, MilvusEmbeddingRetriever # Keep Milvus imports
from haystack.utils import Secret

from app.core.config import settings
from app.db import postgres_client
from app.services.gemini_client import gemini_client # Keep Gemini client import
from app.api.v1.schemas import RetrievedDocument # Import schema for logging

log = structlog.get_logger(__name__)

# --- Component Initialization Functions ---

# --- CORRECTION: Explicitly pass field names to MilvusDocumentStore ---
def get_milvus_document_store() -> MilvusDocumentStore:
    """Initializes and returns a MilvusDocumentStore instance."""
    connection_uri = str(settings.MILVUS_URI)
    store_log = log.bind(
        component="MilvusDocumentStore",
        uri=connection_uri,
        collection=settings.MILVUS_COLLECTION_NAME,
        embedding_field=settings.MILVUS_EMBEDDING_FIELD, # Log field name
        content_field=settings.MILVUS_CONTENT_FIELD      # Log field name
    )
    store_log.debug("Initializing...")
    try:
        store = MilvusDocumentStore(
            connection_args={"uri": connection_uri},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            # --- Pass explicit field names ---
            embedding_field=settings.MILVUS_EMBEDDING_FIELD,
            content_field=settings.MILVUS_CONTENT_FIELD,
            # ---------------------------------
            # --- Optional: Map metadata fields if they differ from Haystack defaults ---
            # field_map={
            #     "content": settings.MILVUS_CONTENT_FIELD,
            #     "embedding": settings.MILVUS_EMBEDDING_FIELD,
            #     # Map other metadata fields if their names differ significantly
            #     # "meta.company_id": settings.MILVUS_COMPANY_ID_FIELD, # Example if names differed
            # },
            # -----------------------------------------------------------------------
            index_params=settings.MILVUS_INDEX_PARAMS,
            search_params=settings.MILVUS_SEARCH_PARAMS,
            consistency_level="Strong",
        )
        store_log.info("Initialization successful.")
        return store
    except Exception as e:
        store_log.error("Initialization failed", error=str(e), exc_info=True)
        raise RuntimeError(f"Milvus initialization error: {e}")
# -----------------------------------------------------------------------

def get_fastembed_text_embedder() -> FastembedTextEmbedder:
    """Initializes and returns a FastembedTextEmbedder instance."""
    embedder_log = log.bind(
        component="FastembedTextEmbedder",
        model=settings.FASTEMBED_MODEL_NAME,
        prefix=settings.FASTEMBED_QUERY_PREFIX,
        dimension=settings.EMBEDDING_DIMENSION # Log dimension
    )
    embedder_log.debug("Initializing...")
    embedder = FastembedTextEmbedder(
        model=settings.FASTEMBED_MODEL_NAME,
        prefix=settings.FASTEMBED_QUERY_PREFIX
    )
    embedder_log.info("Initialization successful.")
    return embedder

def get_prompt_builder(template: str) -> PromptBuilder:
    """Initializes PromptBuilder with a given template."""
    log.debug("Initializing PromptBuilder...")
    return PromptBuilder(template=template)

# --- Pipeline Execution Logic ---
# (No changes needed in the execution flow itself, only in component init)

async def embed_query(query: str) -> List[float]:
    """Embeds the user query using FastEmbed."""
    embed_log = log.bind(action="embed_query")
    try:
        embedder = get_fastembed_text_embedder()
        await asyncio.to_thread(embedder.warm_up)
        result = await asyncio.to_thread(embedder.run, text=query)
        embedding = result.get("embedding")
        if not embedding:
            raise ValueError("Embedding process returned no embedding vector.")
        # --- CORRECTION: Validate embedding dimension ---
        if len(embedding) != settings.EMBEDDING_DIMENSION:
            embed_log.error("Embedding dimension mismatch!",
                            expected=settings.EMBEDDING_DIMENSION,
                            actual=len(embedding),
                            model=settings.FASTEMBED_MODEL_NAME)
            raise ValueError(f"Embedding dimension mismatch: expected {settings.EMBEDDING_DIMENSION}, got {len(embedding)}")
        # ----------------------------------------------
        embed_log.info("Query embedded successfully", vector_dim=len(embedding))
        return embedding
    except Exception as e:
        embed_log.error("Embedding failed", error=str(e), exc_info=True)
        raise ConnectionError(f"Embedding service error: {e}") from e

async def retrieve_documents(embedding: List[float], company_id: str, top_k: int) -> List[Document]:
    """Retrieves relevant documents from Milvus based on the query embedding and company_id."""
    retrieve_log = log.bind(action="retrieve_documents", company_id=company_id, top_k=top_k)
    try:
        document_store = get_milvus_document_store()
        # --- Construct filter using the field name from settings ---
        filters = {settings.MILVUS_COMPANY_ID_FIELD: company_id}
        retrieve_log.debug("Using filter for retrieval", filter_dict=filters)
        # --------------------------------------------------------
        retriever = MilvusEmbeddingRetriever(
            document_store=document_store,
            filters=filters,
            top_k=top_k
            # embedding_field is inferred from document_store if set correctly
        )
        result = await asyncio.to_thread(retriever.run, query_embedding=embedding)
        documents = result.get("documents", [])
        retrieve_log.info("Documents retrieved successfully", count=len(documents))
        return documents
    except MilvusException as me:
         retrieve_log.error("Milvus retrieval failed", error_code=me.code, error_message=me.message, exc_info=False)
         # --- Provide more context in the error ---
         raise ConnectionError(f"Vector DB retrieval error (Collection: {settings.MILVUS_COLLECTION_NAME}, Milvus code: {me.code})") from me
         # -------------------------------------------
    except Exception as e:
        retrieve_log.error("Retrieval failed", error=str(e), exc_info=True)
        raise ConnectionError(f"Retrieval service error: {e}") from e

async def build_prompt(query: str, documents: List[Document]) -> str:
    """Builds the final prompt for the LLM, selecting template based on retrieved documents."""
    build_log = log.bind(action="build_prompt", num_docs=len(documents))
    try:
        if documents:
            template = settings.RAG_PROMPT_TEMPLATE
            prompt_builder = get_prompt_builder(template)
            result = await asyncio.to_thread(prompt_builder.run, query=query, documents=documents)
            build_log.info("RAG prompt built")
        else:
            template = settings.GENERAL_PROMPT_TEMPLATE
            prompt_builder = get_prompt_builder(template)
            result = await asyncio.to_thread(prompt_builder.run, query=query)
            build_log.info("General prompt built (no documents retrieved)")

        prompt = result.get("prompt")
        if not prompt:
            raise ValueError("Prompt generation returned empty prompt.")
        return prompt
    except Exception as e:
        build_log.error("Prompt building failed", error=str(e), exc_info=True)
        raise ValueError(f"Prompt building error: {e}") from e

async def generate_llm_answer(prompt: str) -> str:
    """Generates the final answer using the Gemini client."""
    llm_log = log.bind(action="generate_llm_answer", model=settings.GEMINI_MODEL_NAME)
    try:
        answer = await gemini_client.generate_answer(prompt)
        llm_log.info("Answer generated successfully", answer_length=len(answer))
        return answer
    except Exception as e:
        llm_log.error("LLM generation failed", error=str(e), exc_info=True)
        raise ConnectionError(f"LLM service error: {e}") from e

async def run_rag_pipeline(
    query: str,
    company_id: str,
    user_id: Optional[str],
    top_k: Optional[int] = None,
    chat_id: Optional[uuid.UUID] = None
) -> Tuple[str, List[Document], Optional[uuid.UUID]]:
    """
    Orchestrates the RAG pipeline steps: embed, retrieve, build prompt, generate answer, log interaction.
    """
    run_log = log.bind(company_id=company_id, user_id=user_id or "N/A", chat_id=str(chat_id) if chat_id else "N/A", query=query[:50]+"...")
    run_log.info("Executing RAG pipeline")
    retriever_k = top_k or settings.RETRIEVER_TOP_K
    log_id: Optional[uuid.UUID] = None

    try:
        # 1. Embed Query
        query_embedding = await embed_query(query)

        # 2. Retrieve Documents
        retrieved_docs = await retrieve_documents(query_embedding, company_id, retriever_k)

        # 3. Build Prompt
        final_prompt = await build_prompt(query, retrieved_docs)

        # 4. Generate Answer
        answer = await generate_llm_answer(final_prompt)

        # 5. Log Interaction (Best effort)
        try:
            docs_for_log = [RetrievedDocument.from_haystack_doc(d).model_dump(exclude_none=True)
                            for d in retrieved_docs]
            log_id = await postgres_client.log_query_interaction(
                company_id=uuid.UUID(company_id),
                user_id=uuid.UUID(user_id) if user_id else None,
                query=query, answer=answer,
                retrieved_documents_data=docs_for_log,
                chat_id=chat_id,
                metadata={"top_k": retriever_k, "llm_model": settings.GEMINI_MODEL_NAME, "embedder_model": settings.FASTEMBED_MODEL_NAME, "num_retrieved": len(retrieved_docs)}
            )
            run_log.info("Interaction logged successfully", db_log_id=str(log_id))
        except Exception as log_err:
            run_log.error("Failed to log RAG interaction to DB", error=str(log_err), exc_info=False)

        run_log.info("RAG pipeline completed successfully")
        return answer, retrieved_docs, log_id

    except ConnectionError as ce:
        run_log.error("Connection error during RAG pipeline", error=str(ce), exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"A dependency is unavailable: {ce}")
    except ValueError as ve:
        run_log.error("Value error during RAG pipeline", error=str(ve), exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Data processing error: {ve}")
    except Exception as e:
        run_log.exception("Unexpected error during RAG pipeline execution")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An internal error occurred.")

# --- Dependency Check Function ---
async def check_pipeline_dependencies() -> Dict[str, str]:
    """Checks the status of pipeline dependencies (Milvus, Gemini)."""
    results = {"milvus_connection": "pending", "gemini_api": "pending", "fastembed_model": "pending"}
    check_log = log.bind(action="check_dependencies")

    # Check Milvus
    try:
        store = get_milvus_document_store()
        # Check connection and collection existence using pymilvus directly for reliability
        if store.conn.has_collection(store.collection_name):
            results["milvus_connection"] = "ok"
            check_log.debug("Milvus dependency check: Connection ok, collection exists.")
        else:
            # It's okay if the collection doesn't exist at startup for query service
            results["milvus_connection"] = "ok (collection not found yet)"
            check_log.info("Milvus dependency check: Connection ok, collection does not exist (needs data from ingest).")
    except MilvusException as me:
        results["milvus_connection"] = f"error: MilvusException (code={me.code}, msg={me.message})"
        check_log.warning("Milvus dependency check failed", error_code=me.code, error_message=me.message, exc_info=False)
    except Exception as e:
        results["milvus_connection"] = f"error: Unexpected {type(e).__name__}"
        check_log.warning("Milvus dependency check failed", error=str(e), exc_info=True)

    # Check Gemini (API Key Presence)
    if settings.GEMINI_API_KEY.get_secret_value():
        results["gemini_api"] = "key_present"
        check_log.debug("Gemini dependency check: API Key is present.")
    else:
        results["gemini_api"] = "key_missing"
        check_log.warning("Gemini dependency check: API Key is MISSING.")

    # Check FastEmbed (Model loading is lazy, just check config)
    if settings.FASTEMBED_MODEL_NAME and settings.EMBEDDING_DIMENSION:
         results["fastembed_model"] = f"configured ({settings.FASTEMBED_MODEL_NAME}, dim={settings.EMBEDDING_DIMENSION})"
         check_log.debug("FastEmbed dependency check: Model configured.", model=settings.FASTEMBED_MODEL_NAME, dim=settings.EMBEDDING_DIMENSION)
    else:
         results["fastembed_model"] = "config_missing"
         check_log.error("FastEmbed dependency check: Model name or dimension MISSING in configuration.")

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
version = "0.1.1" # LLM_COMMENT: Increment version after fixing build and embedder refactor
description = "Query service for SaaS B2B using Haystack RAG with FastEmbed" # LLM_COMMENT: Updated description
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
python-jose = {extras = ["cryptography"], version = "^3.3.0"}
tenacity = "^8.2.3"
structlog = "^24.1.0"

# --- Haystack Dependencies ---
haystack-ai = "^2.0.1" # Core Haystack
pymilvus = "^2.4.1"    # Cliente Milvus explícito
milvus-haystack = "^0.0.6" # Integración Milvus para Haystack 2.x
# FastEmbed integration for Haystack (PyPI package name and available version)
fastembed-haystack = "^1.4.1"

# --- Gemini Dependency  ---
google-generativeai = "^0.5.4" # Cliente oficial de Google para Gemini

[tool.poetry.group.dev.dependencies]
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
