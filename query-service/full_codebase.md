# Estructura de la Codebase

```
app/
├── api
│   └── v1
│       ├── __init__.py
│       ├── endpoints
│       │   ├── __init__.py
│       │   ├── chat.py
│       │   └── query.py
│       ├── mappers.py
│       └── schemas.py
├── application
│   ├── __init__.py
│   ├── ports
│   │   ├── __init__.py
│   │   ├── lllm_port.py
│   │   ├── repository_ports.py
│   │   ├── retrieval_ports.py
│   │   └── vector_store_port.py
│   └── use_cases
│       ├── __init__.py
│       └── ask_query_use_case.py
├── core
│   ├── __init__.py
│   ├── config.py
│   └── logging_config.py
├── domain
│   ├── __init__.py
│   └── models.py
├── infrastructure
│   ├── __init__.py
│   ├── filters
│   │   └── __init__.py
│   ├── llms
│   │   ├── __init__.py
│   │   └── gemini_adapter.py
│   ├── persistence
│   │   ├── __init__.py
│   │   ├── postgres_connector.py
│   │   └── postgres_repositories.py
│   ├── rerankers
│   │   ├── __init__.py
│   │   ├── bge_reranker.py
│   │   └── diversity_filter.py
│   ├── retrievers
│   │   ├── __init__.py
│   │   └── bm25_retriever.py
│   └── vectorstores
│       ├── __init__.py
│       └── milvus_adapter.py
├── main.py
├── models
│   └── __init__.py
└── utils
    ├── __init__.py
    └── helpers.py
```

# Codebase: `app`

## File: `app\api\v1\__init__.py`
```py

```

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
    Response
)

from app.api.v1 import schemas
# LLM_REFACTOR_STEP_4: Import Repository directly for simple operations
from app.infrastructure.persistence.postgres_repositories import PostgresChatRepository
from app.application.ports.repository_ports import ChatRepositoryPort

log = structlog.get_logger(__name__)

router = APIRouter()

# --- Headers Dependencies (Sin cambios) ---
async def get_current_company_id(x_company_id: Optional[str] = Header(None, alias="X-Company-ID")) -> uuid.UUID:
    # ... (código existente sin cambios)
    if not x_company_id:
        log.warning("Missing required X-Company-ID header")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-Company-ID")
    try:
        return uuid.UUID(x_company_id)
    except ValueError:
        log.warning("Invalid UUID format in X-Company-ID header", header_value=x_company_id)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid X-Company-ID header format")

async def get_current_user_id(x_user_id: Optional[str] = Header(None, alias="X-User-ID")) -> uuid.UUID:
    # ... (código existente sin cambios)
    if not x_user_id:
        log.warning("Missing required X-User-ID header")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Missing required header: X-User-ID")
    try:
        return uuid.UUID(x_user_id)
    except ValueError:
        log.warning("Invalid UUID format in X-User-ID header", header_value=x_user_id)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid X-User-ID header format")

# --- Dependency for Chat Repository ---
# LLM_REFACTOR_STEP_4: Inject repository dependency (simplified)
def get_chat_repository() -> ChatRepositoryPort:
    """Provides an instance of the Chat Repository."""
    # In a real setup, this would come from a DI container configured in main.py
    return PostgresChatRepository()


# --- Endpoints Refactored to use Repository Dependency ---

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
    # LLM_REFACTOR_STEP_4: Inject repository
    chat_repo: ChatRepositoryPort = Depends(get_chat_repository),
    request: Request = None
):
    request_id = request.headers.get("x-request-id") if request else str(uuid.uuid4())
    endpoint_log = log.bind(request_id=request_id, user_id=str(user_id), company_id=str(company_id), limit=limit, offset=offset)
    endpoint_log.info("Request received to list chats")

    try:
        # LLM_REFACTOR_STEP_4: Use the injected repository method
        chats_domain = await chat_repo.get_user_chats(
            user_id=user_id, company_id=company_id, limit=limit, offset=offset
        )
        endpoint_log.info("Chats listed successfully", count=len(chats_domain))
        # Map domain to schema if necessary (ChatSummary is compatible for now)
        return [schemas.ChatSummary(**chat.model_dump()) for chat in chats_domain]
    except Exception as e:
        endpoint_log.exception("Error listing chats")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve chat list.")


@router.get(
    "/chats/{chat_id}/messages",
    response_model=List[schemas.ChatMessage],
    status_code=status.HTTP_200_OK,
    summary="Get Chat Messages",
    description="Retrieves messages for a specific chat using X-Company-ID and X-User-ID headers.",
    responses={403: {"description": "Chat not found or access denied."}} # Changed 404 to 403 as check_ownership returns false
)
async def get_chat_messages_endpoint(
    chat_id: uuid.UUID = Path(..., description="The ID of the chat."),
    user_id: uuid.UUID = Depends(get_current_user_id),
    company_id: uuid.UUID = Depends(get_current_company_id),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    # LLM_REFACTOR_STEP_4: Inject repository
    chat_repo: ChatRepositoryPort = Depends(get_chat_repository),
    request: Request = None
):
    request_id = request.headers.get("x-request-id") if request else str(uuid.uuid4())
    endpoint_log = log.bind(request_id=request_id, user_id=str(user_id), company_id=str(company_id), chat_id=str(chat_id), limit=limit, offset=offset)
    endpoint_log.info("Request received to get chat messages")

    try:
        # LLM_REFACTOR_STEP_4: Use the injected repository method
        # The repo method already includes the ownership check
        messages_domain = await chat_repo.get_chat_messages(
            chat_id=chat_id, user_id=user_id, company_id=company_id, limit=limit, offset=offset
        )
        # If ownership check failed inside repo, it returns empty list, no exception needed here.
        endpoint_log.info("Chat messages retrieved successfully", count=len(messages_domain))
        # Map domain to schema if necessary (ChatMessage is compatible for now)
        return [schemas.ChatMessage(**msg.model_dump()) for msg in messages_domain]
    except Exception as e:
        # Catch potential DB errors from the repository
        endpoint_log.exception("Error getting chat messages")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to retrieve chat messages.")


@router.delete(
    "/chats/{chat_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Chat",
    description="Deletes a chat and associated data using X-Company-ID and X-User-ID headers.",
    responses={404: {"description": "Chat not found or access denied."}, 204: {}} # 404 if ownership fails
)
async def delete_chat_endpoint(
    chat_id: uuid.UUID = Path(..., description="The ID of the chat to delete."),
    user_id: uuid.UUID = Depends(get_current_user_id),
    company_id: uuid.UUID = Depends(get_current_company_id),
    # LLM_REFACTOR_STEP_4: Inject repository
    chat_repo: ChatRepositoryPort = Depends(get_chat_repository),
    request: Request = None
):
    request_id = request.headers.get("x-request-id") if request else str(uuid.uuid4())
    endpoint_log = log.bind(request_id=request_id, user_id=str(user_id), company_id=str(company_id), chat_id=str(chat_id))
    endpoint_log.info("Request received to delete chat")

    try:
        # LLM_REFACTOR_STEP_4: Use the injected repository method
        # The repo method includes ownership check and returns bool
        deleted = await chat_repo.delete_chat(chat_id=chat_id, user_id=user_id, company_id=company_id)
        if deleted:
            endpoint_log.info("Chat deleted successfully")
            # Return 204 No Content requires a Response object
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        else:
            # If not deleted, it means ownership check failed or chat didn't exist
            endpoint_log.warning("Chat not found or access denied for deletion")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found or access denied.")
    except Exception as e:
        # Catch potential DB errors from the repository
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
import re

from fastapi import APIRouter, Depends, HTTPException, status, Header, Body, Request

from app.api.v1 import schemas
from app.core.config import settings
# LLM_REFACTOR_STEP_3: Import use case and dependencies for injection (example)
from app.application.use_cases.ask_query_use_case import AskQueryUseCase
from app.infrastructure.persistence.postgres_repositories import (
    PostgresChatRepository, PostgresLogRepository, PostgresChunkContentRepository
)
from app.infrastructure.vectorstores.milvus_adapter import MilvusAdapter
from app.infrastructure.llms.gemini_adapter import GeminiAdapter
# LLM_REFACTOR_STEP_3: Import domain model for mapping
from app.domain.models import RetrievedChunk

from app.utils.helpers import truncate_text
from .chat import get_current_company_id, get_current_user_id

log = structlog.get_logger(__name__)

router = APIRouter()

GREETING_REGEX = re.compile(r"^\s*(hola|hello|hi|buenos días|buenas tardes|buenas noches|hey|qué tal|hi there)\s*[\.,!?]*\s*$", re.IGNORECASE)


# --- Dependency Injection Setup (Simplified Example) ---
# LLM_REFACTOR_STEP_3: In a real app, use a framework like FastAPI Injector or manual setup in main.py
# For now, we instantiate dependencies directly here for simplicity.
def get_ask_query_use_case() -> AskQueryUseCase:
    # Instantiate concrete implementations
    chat_repo = PostgresChatRepository()
    log_repo = PostgresLogRepository()
    vector_store = MilvusAdapter()
    llm_adapter = GeminiAdapter()
    chunk_content_repo = PostgresChunkContentRepository() # Needed for future BM25

    # Instantiate the use case with concrete dependencies
    # For optional ones (BM25, Reranker, Filter), pass None for now
    use_case = AskQueryUseCase(
        chat_repo=chat_repo,
        log_repo=log_repo,
        vector_store=vector_store,
        llm=llm_adapter,
        sparse_retriever=None, # Not implemented yet
        chunk_content_repo=chunk_content_repo, # Pass if needed by sparse retriever
        reranker=None,         # Not implemented yet
        diversity_filter=None  # Not implemented yet
    )
    return use_case

# --- Endpoint Refactored to use AskQueryUseCase ---
@router.post(
    "/ask",
    response_model=schemas.QueryResponse,
    status_code=status.HTTP_200_OK,
    summary="Process Query / Manage Chat",
    description="Handles user queries via RAG pipeline or simple greeting, manages chat state.",
)
async def process_query(
    request_body: schemas.QueryRequest = Body(...),
    company_id: uuid.UUID = Depends(get_current_company_id),
    user_id: uuid.UUID = Depends(get_current_user_id),
    # LLM_REFACTOR_STEP_3: Inject the use case instance
    use_case: AskQueryUseCase = Depends(get_ask_query_use_case),
    request: Request = None # Keep for request ID
):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4())) if request else str(uuid.uuid4())
    endpoint_log = log.bind(
        request_id=request_id,
        company_id=str(company_id),
        user_id=str(user_id),
        query=truncate_text(request_body.query, 100),
        provided_chat_id=str(request_body.chat_id) if request_body.chat_id else "None"
    )
    endpoint_log.info("Processing query request via Use Case")

    try:
        # LLM_REFACTOR_STEP_3: Call the use case execute method
        answer, retrieved_chunks_domain, log_id, final_chat_id = await use_case.execute(
            query=request_body.query,
            company_id=company_id,
            user_id=user_id,
            chat_id=request_body.chat_id,
            top_k=request_body.retriever_top_k
        )

        # LLM_REFACTOR_STEP_3: Map domain results (RetrievedChunk) to API schema (RetrievedDocument)
        retrieved_docs_api = [
            schemas.RetrievedDocument(
                id=chunk.id,
                score=chunk.score,
                content_preview=truncate_text(chunk.content, 150) if chunk.content else None,
                metadata=chunk.metadata,
                document_id=chunk.document_id,
                file_name=chunk.file_name
            ) for chunk in retrieved_chunks_domain
        ]

        endpoint_log.info("Use case executed successfully, returning response", num_retrieved=len(retrieved_docs_api))
        return schemas.QueryResponse(
            answer=answer,
            retrieved_documents=retrieved_docs_api,
            query_log_id=log_id,
            chat_id=final_chat_id
        )

    # Keep specific error handling, Use Case should raise appropriate exceptions
    except HTTPException as http_exc:
        # Re-raise HTTP exceptions directly (like 403, 400 from UseCase)
        raise http_exc
    except ConnectionError as ce:
        # Catch connection errors raised by adapters via UseCase
        endpoint_log.error("Dependency connection error reported by Use Case", error=str(ce), exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"A required service is unavailable.")
    except Exception as e:
        # Catch unexpected errors from UseCase
        endpoint_log.exception("Unhandled exception during use case execution")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"An internal error occurred.")
```

## File: `app\api\v1\mappers.py`
```py
# query-service/app/api/v1/mappers.py
# This file will contain mapping functions between API DTOs (schemas)
# and Domain objects, if needed in later steps.
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

## File: `app\application\__init__.py`
```py
# query-service/app/application/use_cases/__init__.py
```

## File: `app\application\ports\__init__.py`
```py
# query-service/app/application/ports/__init__.py
from .llm_port import LLMPort
from .vector_store_port import VectorStorePort
from .repository_ports import ChatRepositoryPort, LogRepositoryPort, ChunkContentRepositoryPort
# LLM_REFACTOR_STEP_3: Exportar nuevos puertos
from .retrieval_ports import SparseRetrieverPort, RerankerPort, DiversityFilterPort

__all__ = [
    "LLMPort",
    "VectorStorePort",
    "ChatRepositoryPort",
    "LogRepositoryPort",
    "ChunkContentRepositoryPort",
    "SparseRetrieverPort",
    "RerankerPort",
    "DiversityFilterPort",
]
```

## File: `app\application\ports\lllm_port.py`
```py
# query-service/app/application/ports/llm_port.py
import abc
from typing import List

class LLMPort(abc.ABC):
    """Puerto abstracto para interactuar con un Large Language Model."""

    @abc.abstractmethod
    async def generate(self, prompt: str) -> str:
        """
        Genera texto basado en el prompt proporcionado.

        Args:
            prompt: El prompt a enviar al LLM.

        Returns:
            La respuesta generada por el LLM.

        Raises:
            ConnectionError: Si falla la comunicación con el servicio LLM.
            Exception: Para otros errores inesperados.
        """
        raise NotImplementedError
```

## File: `app\application\ports\repository_ports.py`
```py
# query-service/app/application/ports/repository_ports.py
import abc
import uuid
from typing import List, Optional, Dict, Any
# LLM_REFACTOR_STEP_2: Importar modelos de dominio
from app.domain.models import ChatMessage, ChatSummary, QueryLog

class ChatRepositoryPort(abc.ABC):
    """Puerto abstracto para operaciones de persistencia de Chats y Mensajes."""

    @abc.abstractmethod
    async def create_chat(self, user_id: uuid.UUID, company_id: uuid.UUID, title: Optional[str] = None) -> uuid.UUID:
        raise NotImplementedError

    @abc.abstractmethod
    async def get_user_chats(self, user_id: uuid.UUID, company_id: uuid.UUID, limit: int = 50, offset: int = 0) -> List[ChatSummary]:
        raise NotImplementedError

    @abc.abstractmethod
    async def check_chat_ownership(self, chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
        raise NotImplementedError

    @abc.abstractmethod
    async def get_chat_messages(self, chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID, limit: int = 100, offset: int = 0) -> List[ChatMessage]:
        raise NotImplementedError

    @abc.abstractmethod
    async def save_message(self, chat_id: uuid.UUID, role: str, content: str, sources: Optional[List[Dict[str, Any]]] = None) -> uuid.UUID:
        raise NotImplementedError

    @abc.abstractmethod
    async def delete_chat(self, chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
        raise NotImplementedError


class LogRepositoryPort(abc.ABC):
    """Puerto abstracto para operaciones de persistencia de Logs de Consultas."""

    @abc.abstractmethod
    async def log_query_interaction(
        self,
        user_id: Optional[uuid.UUID],
        company_id: uuid.UUID,
        query: str,
        answer: str,
        retrieved_documents_data: List[Dict[str, Any]], # Mantener Dict por simplicidad del log
        metadata: Optional[Dict[str, Any]] = None,
        chat_id: Optional[uuid.UUID] = None,
    ) -> uuid.UUID:
        raise NotImplementedError

# LLM_REFACTOR_STEP_2: Añadir puerto para obtener contenido de chunks para BM25
class ChunkContentRepositoryPort(abc.ABC):
    """Puerto abstracto para obtener contenido textual de chunks desde la persistencia."""

    @abc.abstractmethod
    async def get_chunk_contents_by_company(self, company_id: uuid.UUID) -> Dict[str, str]:
        """
        Obtiene un diccionario de {chunk_id: content} para una compañía.
        Necesario para construir índices BM25 en memoria.
        Considerar alternativas si esto es muy costoso (ej: obtener por IDs específicos).
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def get_chunk_contents_by_ids(self, chunk_ids: List[str]) -> Dict[str, str]:
        """Obtiene un diccionario de {chunk_id: content} para una lista de IDs."""
        raise NotImplementedError
```

## File: `app\application\ports\retrieval_ports.py`
```py
# query-service/app/application/ports/retrieval_ports.py
import abc
from typing import List, Tuple
# LLM_REFACTOR_STEP_3: Importar modelo de dominio
from app.domain.models import RetrievedChunk

# Puerto para Retrievers dispersos (como BM25)
class SparseRetrieverPort(abc.ABC):
    """Puerto abstracto para recuperar chunks usando métodos dispersos (keyword-based)."""

    @abc.abstractmethod
    async def search(self, query: str, company_id: str, top_k: int) -> List[Tuple[str, float]]:
        """
        Busca chunks relevantes basados en la consulta textual y filtra por compañía.

        Args:
            query: La consulta del usuario.
            company_id: El ID de la compañía para filtrar.
            top_k: El número máximo de IDs de chunks a devolver.

        Returns:
            Una lista de tuplas (chunk_id: str, score: float).
            Nota: Este puerto solo devuelve IDs y scores, el contenido se recupera por separado.
        """
        raise NotImplementedError

# Puerto para Rerankers
class RerankerPort(abc.ABC):
    """Puerto abstracto para reordenar chunks recuperados."""

    @abc.abstractmethod
    async def rerank(self, query: str, chunks: List[RetrievedChunk]) -> List[RetrievedChunk]:
        """
        Reordena una lista de chunks basada en la consulta.

        Args:
            query: La consulta original del usuario.
            chunks: Lista de objetos RetrievedChunk (deben tener contenido).

        Returns:
            Una lista reordenada de RetrievedChunk.
        """
        raise NotImplementedError

# Puerto para Filtros de Diversidad
class DiversityFilterPort(abc.ABC):
    """Puerto abstracto para aplicar filtros de diversidad a los chunks."""

    @abc.abstractmethod
    async def filter(self, chunks: List[RetrievedChunk], k_final: int) -> List[RetrievedChunk]:
        """
        Filtra una lista de chunks para maximizar diversidad y relevancia.

        Args:
            chunks: Lista de RetrievedChunk (generalmente ya reordenados).
            k_final: El número deseado de chunks finales.

        Returns:
            Una sublista filtrada de RetrievedChunk.
        """
        raise NotImplementedError
```

## File: `app\application\ports\vector_store_port.py`
```py
# query-service/app/application/ports/vector_store_port.py
import abc
from typing import List
# LLM_REFACTOR_STEP_2: Importar el objeto de dominio
from app.domain.models import RetrievedChunk

class VectorStorePort(abc.ABC):
    """Puerto abstracto para interactuar con una base de datos vectorial."""

    @abc.abstractmethod
    async def search(self, embedding: List[float], company_id: str, top_k: int) -> List[RetrievedChunk]:
        """
        Busca chunks relevantes basados en un embedding y filtra por compañía.

        Args:
            embedding: El vector de embedding de la consulta.
            company_id: El ID de la compañía para filtrar los resultados.
            top_k: El número máximo de chunks a devolver.

        Returns:
            Una lista de objetos RetrievedChunk relevantes.

        Raises:
            ConnectionError: Si falla la comunicación con la base de datos vectorial.
            Exception: Para otros errores inesperados.
        """
        raise NotImplementedError
```

## File: `app\application\use_cases\__init__.py`
```py

```

## File: `app\application\use_cases\ask_query_use_case.py`
```py
# query-service/app/application/use_cases/ask_query_use_case.py
import structlog
import asyncio
import uuid
import re
from typing import Dict, Any, List, Tuple, Optional, Type, Set

# Import Ports and Domain Models
from app.application.ports import (
    ChatRepositoryPort, LogRepositoryPort, VectorStorePort, LLMPort,
    SparseRetrieverPort, RerankerPort, DiversityFilterPort, ChunkContentRepositoryPort
)
from app.domain.models import RetrievedChunk, ChatMessage

# Keep necessary components (Embedder, PromptBuilder)
from haystack_integrations.components.embedders.fastembed import FastembedTextEmbedder
from haystack.components.builders.prompt_builder import PromptBuilder
from haystack import Document # Still needed for PromptBuilder context

from app.core.config import settings
from app.api.v1.schemas import RetrievedDocument as RetrievedDocumentSchema # For logging format
from app.utils.helpers import truncate_text
from fastapi import HTTPException, status

log = structlog.get_logger(__name__)

GREETING_REGEX = re.compile(r"^\s*(hola|hello|hi|buenos días|buenas tardes|buenas noches|hey|qué tal|hi there)\s*[\.,!?]*\s*$", re.IGNORECASE)

# RRF Constant
RRF_K = 60

class AskQueryUseCase:
    """
    Caso de uso para procesar una consulta de usuario, manejar el chat y ejecutar el pipeline RAG completo.
    """
    def __init__(self,
                 chat_repo: ChatRepositoryPort,
                 log_repo: LogRepositoryPort,
                 vector_store: VectorStorePort,
                 llm: LLMPort,
                 # Optional components for advanced RAG
                 sparse_retriever: Optional[SparseRetrieverPort] = None,
                 chunk_content_repo: Optional[ChunkContentRepositoryPort] = None,
                 reranker: Optional[RerankerPort] = None,
                 diversity_filter: Optional[DiversityFilterPort] = None):
        self.chat_repo = chat_repo
        self.log_repo = log_repo
        self.vector_store = vector_store
        self.llm = llm
        # Assign components based on settings
        self.sparse_retriever = sparse_retriever if settings.BM25_ENABLED else None
        self.chunk_content_repo = chunk_content_repo
        self.reranker = reranker if settings.RERANKER_ENABLED else None
        self.diversity_filter = diversity_filter if settings.DIVERSITY_FILTER_ENABLED else None

        self._embedder = self._initialize_embedder()
        self._prompt_builder_rag = self._initialize_prompt_builder(settings.RAG_PROMPT_TEMPLATE)
        self._prompt_builder_general = self._initialize_prompt_builder(settings.GENERAL_PROMPT_TEMPLATE)

        log.info("AskQueryUseCase Initialized",
                 bm25_enabled=bool(self.sparse_retriever),
                 reranker_enabled=bool(self.reranker),
                 diversity_filter_enabled=bool(self.diversity_filter))
        if self.sparse_retriever and not self.chunk_content_repo:
            log.error("SparseRetriever is enabled but ChunkContentRepositoryPort is missing!")


    def _initialize_embedder(self) -> FastembedTextEmbedder:
        embedder_log = log.bind(component="FastembedTextEmbedder", model=settings.FASTEMBED_MODEL_NAME)
        embedder_log.debug("Initializing FastEmbed Embedder...")
        try:
            embedder = FastembedTextEmbedder(
                model=settings.FASTEMBED_MODEL_NAME,
                prefix=settings.FASTEMBED_QUERY_PREFIX
            )
            embedder_log.info("FastEmbed Embedder initialized.")
            return embedder
        except Exception as e:
            embedder_log.exception("Failed to initialize FastEmbed Embedder")
            raise RuntimeError(f"Could not initialize embedding model: {e}") from e

    def _initialize_prompt_builder(self, template: str) -> PromptBuilder:
        log.debug("Initializing PromptBuilder...")
        return PromptBuilder(template=template)

    async def _embed_query(self, query: str) -> List[float]:
        embed_log = log.bind(action="embed_query_use_case")
        try:
            result = await asyncio.to_thread(self._embedder.run, text=query)
            embedding = result.get("embedding")
            if not embedding: raise ValueError("Embedding returned no vector.")
            if len(embedding) != settings.EMBEDDING_DIMENSION: raise ValueError("Embedding dimension mismatch.")
            embed_log.debug("Query embedded successfully", vector_dim=len(embedding))
            return embedding
        except Exception as e:
            embed_log.error("Embedding failed", error=str(e), exc_info=True)
            raise ConnectionError(f"Embedding service error: {e}") from e

    async def _build_prompt(self, query: str, documents: List[Document]) -> str:
        builder_log = log.bind(action="build_prompt_use_case", num_docs=len(documents))
        try:
            if documents:
                prompt_builder = self._prompt_builder_rag
                builder_log.debug("Using RAG prompt template.")
                result = await asyncio.to_thread(prompt_builder.run, query=query, documents=documents)
            else:
                prompt_builder = self._prompt_builder_general
                builder_log.debug("Using General prompt template.")
                result = await asyncio.to_thread(prompt_builder.run, query=query)
            prompt = result.get("prompt")
            if not prompt: raise ValueError("Prompt generation returned empty.")
            builder_log.debug("Prompt built successfully.")
            return prompt
        except Exception as e:
            builder_log.error("Prompt building failed", error=str(e), exc_info=True)
            raise ValueError(f"Prompt building error: {e}") from e

    def _reciprocal_rank_fusion(self,
                                dense_results: List[RetrievedChunk],
                                sparse_results: List[Tuple[str, float]],
                                k: int = RRF_K) -> Dict[str, float]:
        """Combines dense and sparse results using Reciprocal Rank Fusion."""
        fused_scores: Dict[str, float] = {}
        # Process dense results
        for rank, chunk in enumerate(dense_results):
            # Use chunk.id directly, it's already a string from MilvusAdapter/DomainModel
            fused_scores[chunk.id] = fused_scores.get(chunk.id, 0.0) + 1.0 / (k + rank + 1)

        # Process sparse results
        for rank, (chunk_id, _) in enumerate(sparse_results):
             # chunk_id from BM25Retriever is already a string
             fused_scores[chunk_id] = fused_scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
        return fused_scores

    async def _fetch_content_for_fused_results(
        self,
        fused_scores: Dict[str, float],
        dense_map: Dict[str, RetrievedChunk],
        top_n: int
        ) -> List[RetrievedChunk]:
        """
        Gets the top_n chunks based on fused scores and fetches content
        for chunks that were only found in sparse results.
        """
        if not fused_scores: return []

        sorted_chunk_ids = sorted(fused_scores.items(), key=lambda item: item[1], reverse=True)
        top_ids = [cid for cid, score in sorted_chunk_ids[:top_n]]
        chunks_with_content: List[RetrievedChunk] = []
        ids_needing_content: List[str] = []
        final_scores: Dict[str, float] = dict(sorted_chunk_ids[:top_n])

        for cid in top_ids:
            if cid in dense_map and dense_map[cid].content:
                chunk = dense_map[cid]
                chunk.score = final_scores[cid] # Update with fused score
                chunks_with_content.append(chunk)
            else:
                ids_needing_content.append(cid)

        if ids_needing_content and self.chunk_content_repo:
             log.debug("Fetching content for chunks found via sparse/fused search", count=len(ids_needing_content))
             try:
                 content_map = await self.chunk_content_repo.get_chunk_contents_by_ids(ids_needing_content)
                 for cid in ids_needing_content:
                     if cid in content_map:
                         # Create chunk with fetched content and fused score
                         chunk = RetrievedChunk(id=cid, content=content_map[cid], score=final_scores[cid], metadata={"retrieval_source": "sparse/fused"})
                         chunks_with_content.append(chunk)
                     else: log.warning("Content not found for sparsely retrieved chunk after fetch", chunk_id=cid)
             except Exception: log.exception("Failed to fetch content for sparse/fused results")
        elif ids_needing_content: log.warning("Cannot fetch content for sparse/fused results, ChunkContentRepository not available.")

        # Re-sort the final list by score, as fetching might change order
        chunks_with_content.sort(key=lambda c: c.score or 0.0, reverse=True)
        return chunks_with_content


    async def execute(
        self, query: str, company_id: uuid.UUID, user_id: uuid.UUID,
        chat_id: Optional[uuid.UUID] = None, top_k: Optional[int] = None
    ) -> Tuple[str, List[RetrievedChunk], Optional[uuid.UUID], uuid.UUID]:
        """ Orchestrates the full RAG pipeline """
        exec_log = log.bind(use_case="AskQueryUseCase", company_id=str(company_id), user_id=str(user_id), query=truncate_text(query, 50))
        retriever_k = top_k or settings.RETRIEVER_TOP_K
        pipeline_stages_used = ["dense_retrieval", "llm_generation"] # Base stages
        final_chat_id: uuid.UUID
        log_id: Optional[uuid.UUID] = None

        try:
            # 1. Manage Chat State
            if chat_id:
                if not await self.chat_repo.check_chat_ownership(chat_id, user_id, company_id):
                    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Chat not found or access denied.")
                final_chat_id = chat_id
            else:
                initial_title = f"Chat: {truncate_text(query, 40)}"
                final_chat_id = await self.chat_repo.create_chat(user_id=user_id, company_id=company_id, title=initial_title)
            exec_log = exec_log.bind(chat_id=str(final_chat_id))
            exec_log.info("Chat state managed", is_new=(not chat_id))

            # 2. Save User Message
            await self.chat_repo.save_message(chat_id=final_chat_id, role='user', content=query)

            # 3. Handle Greetings
            if GREETING_REGEX.match(query):
                answer = "¡Hola! ¿En qué puedo ayudarte hoy con la información de tus documentos?"
                await self.chat_repo.save_message(chat_id=final_chat_id, role='assistant', content=answer, sources=None)
                exec_log.info("Use case finished (greeting).")
                return answer, [], log_id, final_chat_id

            # --- RAG Pipeline ---
            exec_log.info("Proceeding with RAG pipeline...")

            # 4. Embed Query
            query_embedding = await self._embed_query(query)

            # 5. Coarse Retrieval (Dense + Sparse)
            dense_task = self.vector_store.search(query_embedding, str(company_id), retriever_k)
            sparse_task = asyncio.create_task(asyncio.sleep(0)) # Dummy if disabled
            sparse_results: List[Tuple[str, float]] = [] # Default empty
            if self.sparse_retriever:
                 pipeline_stages_used.append("sparse_retrieval (bm25s)")
                 sparse_task = self.sparse_retriever.search(query, str(company_id), retriever_k)

            # Await retrievals
            dense_chunks, sparse_results_maybe = await asyncio.gather(dense_task, sparse_task)
            if self.sparse_retriever: # Ensure sparse_results list is populated if run
                sparse_results = sparse_results_maybe if isinstance(sparse_results_maybe, list) else []
            exec_log.info("Retrieval phase completed", dense_count=len(dense_chunks), sparse_count=len(sparse_results))

            # 6. Fusion
            pipeline_stages_used.append("fusion (rrf)")
            dense_map = {c.id: c for c in dense_chunks}
            fused_scores = self._reciprocal_rank_fusion(dense_chunks, sparse_results)
            fusion_fetch_k = retriever_k * 2
            combined_chunks = await self._fetch_content_for_fused_results(fused_scores, dense_map, fusion_fetch_k)
            exec_log.info("Fusion completed", initial_fused_count=len(fused_scores), chunks_with_content=len(combined_chunks))

            # Filter out chunks without content before next steps
            chunks_for_next_steps = [c for c in combined_chunks if c.content]
            if not chunks_for_next_steps:
                 exec_log.warning("No chunks with content available after fusion/fetch.")
                 final_prompt = await self._build_prompt(query, []) # Use general prompt
                 answer = await self.llm.generate(final_prompt)
                 await self.chat_repo.save_message(chat_id=final_chat_id, role='assistant', content=answer, sources=None)
                 try: # Best effort logging
                    log_id = await self.log_repo.log_query_interaction(company_id=company_id, user_id=user_id, query=query, answer=answer, retrieved_documents_data=[], chat_id=final_chat_id, metadata={"pipeline_stages": pipeline_stages_used, "result": "no_docs_found"})
                 except Exception: exec_log.error("Failed to log interaction for no_docs case")
                 return answer, [], log_id, final_chat_id # Return empty list for retrieved docs

            # 7. Reranking (Conditional)
            reranked_chunks = chunks_for_next_steps
            if self.reranker: # Check if reranker instance exists (implies enabled and loaded)
                pipeline_stages_used.append("reranking (bge)")
                exec_log.debug("Performing reranking...", count=len(chunks_for_next_steps))
                reranked_chunks = await self.reranker.rerank(query, chunks_for_next_steps)
                exec_log.info("Reranking completed.", count=len(reranked_chunks))

            # 8. Diversity Filtering (Conditional)
            final_chunks_for_llm = reranked_chunks
            if self.diversity_filter: # Check if filter instance exists
                pipeline_stages_used.append("diversity_filter (stub)")
                k_final = settings.DIVERSITY_K_FINAL
                exec_log.debug(f"Applying diversity filter k={k_final}...", count=len(reranked_chunks))
                final_chunks_for_llm = await self.diversity_filter.filter(reranked_chunks, k_final)
                exec_log.info("Diversity filter applied.", final_count=len(final_chunks_for_llm))

            # 9. Build Prompt
            haystack_docs_for_prompt = [
                Document(id=c.id, content=c.content, meta=c.metadata, score=c.score)
                for c in final_chunks_for_llm if c.content
            ]
            if not haystack_docs_for_prompt:
                 exec_log.warning("No documents remaining after filtering for RAG prompt, using general prompt.")
            final_prompt = await self._build_prompt(query, haystack_docs_for_prompt)

            # 10. Generate Answer
            answer = await self.llm.generate(final_prompt)
            exec_log.info("LLM answer generated.", length=len(answer))

            # 11. Save Assistant Message
            assistant_sources = [
                {"chunk_id": c.id, "document_id": c.document_id, "file_name": c.file_name, "score": c.score, "preview": truncate_text(c.content, 150)}
                for c in final_chunks_for_llm
            ]
            await self.chat_repo.save_message(chat_id=final_chat_id, role='assistant', content=answer, sources=assistant_sources or None)

            # 12. Log Interaction
            try:
                docs_for_log = [RetrievedDocumentSchema(**c.model_dump()).model_dump(exclude_none=True) for c in final_chunks_for_llm]
                log_metadata = { "pipeline_stages": pipeline_stages_used, "retriever_k": retriever_k,
                                 "fusion_fetch_k": fusion_fetch_k, "diversity_k_final": settings.DIVERSITY_K_FINAL if self.diversity_filter else None,
                                 "num_final_to_llm": len(final_chunks_for_llm) }
                log_id = await self.log_repo.log_query_interaction(
                    company_id=company_id, user_id=user_id, query=query, answer=answer,
                    retrieved_documents_data=docs_for_log, chat_id=final_chat_id, metadata=log_metadata
                )
                exec_log.info("Interaction logged successfully", db_log_id=str(log_id))
            except Exception as log_err:
                exec_log.error("Failed to log RAG interaction", error=str(log_err), exc_info=False)

            exec_log.info("Use case execution finished successfully.")
            return answer, final_chunks_for_llm, log_id, final_chat_id

        except ConnectionError as ce:
            exec_log.error("Connection error during use case execution", error=str(ce), exc_info=True)
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"A dependency is unavailable: {ce}")
        except ValueError as ve:
            exec_log.error("Value error during use case execution", error=str(ve), exc_info=True)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Data processing error: {ve}")
        except HTTPException as http_exc: raise http_exc
        except Exception as e:
            exec_log.exception("Unexpected error during use case execution")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"An internal error occurred: {type(e).__name__}")
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
from pydantic import AnyHttpUrl, SecretStr, Field, field_validator, ValidationError, ValidationInfo
import sys
import json

# --- Default Values ---
# PostgreSQL
POSTGRES_K8S_HOST_DEFAULT = "postgresql-service.nyro-develop.svc.cluster.local"
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex"
POSTGRES_K8S_USER_DEFAULT = "postgres"
# Milvus
MILVUS_K8S_DEFAULT_URI = "http://milvus-standalone.nyro-develop.svc.cluster.local:19530"
MILVUS_DEFAULT_COLLECTION = "document_chunks_haystack" # LLM_FLAG: Consider aligning collection name if ingest changes
MILVUS_DEFAULT_EMBEDDING_FIELD = "embedding"
MILVUS_DEFAULT_CONTENT_FIELD = "content"
MILVUS_DEFAULT_COMPANY_ID_FIELD = "company_id"
MILVUS_DEFAULT_DOCUMENT_ID_FIELD = "document_id"
MILVUS_DEFAULT_FILENAME_FIELD = "file_name"
MILVUS_DEFAULT_GRPC_TIMEOUT = 10
# RAG Prompts
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
# Models
DEFAULT_FASTEMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_FASTEMBED_QUERY_PREFIX = "query: "
DEFAULT_EMBEDDING_DIMENSION = 384
DEFAULT_GEMINI_MODEL = "gemini-1.5-flash-latest"
# LLM_REFACTOR_STEP_4: Add default for reranker
DEFAULT_RERANKER_MODEL = "BAAI/bge-reranker-base"
# RAG Pipeline Parameters
DEFAULT_RETRIEVER_TOP_K = 5
# LLM_REFACTOR_STEP_4: Add defaults for new RAG components
DEFAULT_BM25_ENABLED: bool = True # Control if BM25 is used
DEFAULT_RERANKER_ENABLED: bool = True # Control if Reranker is used
DEFAULT_DIVERSITY_FILTER_ENABLED: bool = False # Control if Diversity Filter is used (start disabled)
DEFAULT_DIVERSITY_K_FINAL: int = 10 # Number of docs after diversity filter
DEFAULT_HYBRID_ALPHA: float = 0.5 # Weight for dense vs sparse fusion (0=sparse only, 1=dense only)

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
    POSTGRES_USER: str = Field(default=POSTGRES_K8S_USER_DEFAULT)
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_SERVER: str = Field(default=POSTGRES_K8S_HOST_DEFAULT)
    POSTGRES_PORT: int = Field(default=POSTGRES_K8S_PORT_DEFAULT)
    POSTGRES_DB: str = Field(default=POSTGRES_K8S_DB_DEFAULT)

    # --- Vector Store (Milvus) ---
    MILVUS_URI: AnyHttpUrl = Field(default=AnyHttpUrl(MILVUS_K8S_DEFAULT_URI))
    MILVUS_COLLECTION_NAME: str = Field(default=MILVUS_DEFAULT_COLLECTION)
    MILVUS_EMBEDDING_FIELD: str = Field(default=MILVUS_DEFAULT_EMBEDDING_FIELD)
    MILVUS_CONTENT_FIELD: str = Field(default=MILVUS_DEFAULT_CONTENT_FIELD)
    MILVUS_COMPANY_ID_FIELD: str = Field(default=MILVUS_DEFAULT_COMPANY_ID_FIELD)
    MILVUS_DOCUMENT_ID_FIELD: str = Field(default=MILVUS_DEFAULT_DOCUMENT_ID_FIELD)
    MILVUS_FILENAME_FIELD: str = Field(default=MILVUS_DEFAULT_FILENAME_FIELD)
    MILVUS_METADATA_FIELDS: List[str] = Field(default=["company_id", "document_id", "file_name", "file_type"])
    MILVUS_GRPC_TIMEOUT: int = Field(default=MILVUS_DEFAULT_GRPC_TIMEOUT)
    # Index/Search params are usually static, loaded from defaults or env vars as JSON strings
    # Pydantic v1 style: MILVUS_INDEX_PARAMS: str = MILVUS_DEFAULT_INDEX_PARAMS
    # Pydantic v1 style: MILVUS_SEARCH_PARAMS: str = MILVUS_DEFAULT_SEARCH_PARAMS

    # --- Embedding Model (FastEmbed) ---
    FASTEMBED_MODEL_NAME: str = Field(default=DEFAULT_FASTEMBED_MODEL)
    EMBEDDING_DIMENSION: int = Field(default=DEFAULT_EMBEDDING_DIMENSION)
    FASTEMBED_QUERY_PREFIX: str = Field(default=DEFAULT_FASTEMBED_QUERY_PREFIX)

    # --- LLM (Google Gemini) ---
    GEMINI_API_KEY: SecretStr
    GEMINI_MODEL_NAME: str = Field(default=DEFAULT_GEMINI_MODEL)

    # --- Reranker Model ---
    # LLM_REFACTOR_STEP_4: Add Reranker config
    RERANKER_ENABLED: bool = Field(default=DEFAULT_RERANKER_ENABLED)
    RERANKER_MODEL_NAME: str = Field(default=DEFAULT_RERANKER_MODEL, description="Sentence Transformer model name/path for reranking.")
    # RERANKER_ONNX_PATH: Optional[str] = Field(None, description="Path to ONNX version of reranker model (optional).")
    # RERANKER_DEVICE: Optional[str] = Field(None, description="Device for reranker ('cpu', 'cuda', 'mps'). Auto-detected if None.")

    # --- Sparse Retriever (BM25) ---
    # LLM_REFACTOR_STEP_4: Add BM25 config
    BM25_ENABLED: bool = Field(default=DEFAULT_BM25_ENABLED)
    # BM25_INDEX_PATH: Optional[str] = Field(None, description="Path to pre-built BM25 index (if using Pyserini).")

    # --- Diversity Filter ---
    # LLM_REFACTOR_STEP_4: Add Diversity config
    DIVERSITY_FILTER_ENABLED: bool = Field(default=DEFAULT_DIVERSITY_FILTER_ENABLED)
    # DIVERSITY_FILTER_METHOD: str = Field(default="stub", description="Diversity method ('stub', 'mmr', 'dartboard').")
    DIVERSITY_K_FINAL: int = Field(default=DEFAULT_DIVERSITY_K_FINAL, gt=0, description="Target number of documents after diversity filtering.")
    # DIVERSITY_LAMBDA: float = Field(default=0.5, ge=0.0, le=1.0, description="Lambda parameter for MMR diversity (if used).")

    # --- RAG Pipeline Parameters ---
    RETRIEVER_TOP_K: int = Field(default=DEFAULT_RETRIEVER_TOP_K, gt=0, le=50)
    # LLM_REFACTOR_STEP_4: Add fusion parameter
    HYBRID_FUSION_ALPHA: float = Field(default=DEFAULT_HYBRID_ALPHA, ge=0.0, le=1.0, description="Weighting factor for dense vs sparse fusion (0=sparse, 1=dense). Used for simple linear fusion.")
    RAG_PROMPT_TEMPLATE: str = Field(default=DEFAULT_RAG_PROMPT_TEMPLATE)
    GENERAL_PROMPT_TEMPLATE: str = Field(default=DEFAULT_GENERAL_PROMPT_TEMPLATE)
    MAX_PROMPT_TOKENS: Optional[int] = Field(default=7000)

    # --- Service Client Config ---
    HTTP_CLIENT_TIMEOUT: int = Field(default=60)
    HTTP_CLIENT_MAX_RETRIES: int = Field(default=2)
    HTTP_CLIENT_BACKOFF_FACTOR: float = Field(default=1.0)

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
        if v is None or (isinstance(v, str) and v == ""): # Check for None or empty string
            field_name = info.field_name if info.field_name else "Unknown Secret Field"
            # Raise ValueError for config loading phase
            raise ValueError(f"Required secret field 'QUERY_{field_name.upper()}' cannot be empty.")
        # Return as SecretStr implicitly by Pydantic
        return v

    @field_validator('EMBEDDING_DIMENSION')
    @classmethod
    def check_embedding_dimension(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0:
            raise ValueError("EMBEDDING_DIMENSION must be a positive integer.")
        # LLM_FLAG: This validation logic relies on specific model names. Might need updates if models change frequently.
        model_name = info.data.get('FASTEMBED_MODEL_NAME', DEFAULT_FASTEMBED_MODEL)
        expected_dim = -1
        if 'all-MiniLM-L6-v2' in model_name: expected_dim = 384
        elif 'bge-small-en-v1.5' in model_name: expected_dim = 384
        elif 'bge-large-en-v1.5' in model_name: expected_dim = 1024

        if expected_dim != -1 and v != expected_dim:
            logging.warning(f"Configured EMBEDDING_DIMENSION ({v}) differs from standard dimension ({expected_dim}) for model '{model_name}'. Ensure this is intentional.")
        elif expected_dim == -1:
            logging.warning(f"Unknown standard embedding dimension for model '{model_name}'. Using configured dimension {v}. Verify this matches the actual model output.")
        logging.debug(f"Using EMBEDDING_DIMENSION: {v} for model: {model_name}")
        return v

# --- Global Settings Instance ---
# Setup temporary logger for loading phase
temp_log = logging.getLogger("query_service.config.loader")
if not temp_log.handlers:
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(levelname)s: [%(name)s] %(message)s')
    handler.setFormatter(formatter)
    temp_log.addHandler(handler)
    temp_log.setLevel(logging.INFO) # Set to INFO or DEBUG as needed

try:
    temp_log.info("Loading Query Service settings...")
    settings = Settings()
    # Log loaded settings (omitting secrets)
    temp_log.info("Query Service Settings Loaded Successfully:")
    log_data = settings.model_dump(exclude={'POSTGRES_PASSWORD', 'GEMINI_API_KEY'})
    for key, value in log_data.items():
        temp_log.info(f"  {key}: {value}")
    temp_log.info(f"  POSTGRES_PASSWORD: *** SET ***")
    temp_log.info(f"  GEMINI_API_KEY: *** SET ***")

except (ValidationError, ValueError) as e:
    # Log detailed validation errors
    error_details = ""
    if isinstance(e, ValidationError):
        try:
            # Use Pydantic v2 error formatting
             error_details = f"\nValidation Errors:\n{json.dumps(e.errors(), indent=2)}"
        except Exception:
             # Fallback for unexpected formatting issues
             error_details = f"\nRaw Errors: {e}" # Simple string representation
    else: # Handle plain ValueError from custom validators
        error_details = f"\nError: {e}"

    temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    temp_log.critical(f"! FATAL: Query Service configuration validation failed!{error_details}")
    temp_log.critical(f"! Check environment variables (prefixed with QUERY_) or .env file.")
    # temp_log.critical(f"! Original Error Traceback:", exc_info=True) # Optional: include traceback
    temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    sys.exit(1) # Exit if configuration fails
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

## File: `app\domain\__init__.py`
```py
# query-service/app/domain/__init__.py
# This package will contain the core business entities and value objects.
# e.g., Chat, Message, RetrievedChunk classes without external dependencies.
```

## File: `app\domain\models.py`
```py
# query-service/app/domain/models.py
import uuid
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime

# Usaremos Pydantic por conveniencia, pero estas son conceptualmente entidades de dominio.

class Chat(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    company_id: uuid.UUID
    title: Optional[str]
    created_at: datetime
    updated_at: datetime

class ChatSummary(BaseModel):
    # Similar a schemas.ChatSummary pero como objeto de dominio si se necesita diferenciar
    id: uuid.UUID
    title: Optional[str]
    updated_at: datetime

class ChatMessage(BaseModel):
    id: uuid.UUID
    chat_id: uuid.UUID
    role: str # 'user' or 'assistant'
    content: str
    sources: Optional[List[Dict[str, Any]]] = None # Mantener estructura JSON por ahora
    created_at: datetime

class RetrievedChunk(BaseModel):
    """Representa un chunk recuperado de una fuente (ej: Milvus)."""
    id: str # ID del chunk (ej: Milvus PK)
    content: Optional[str] = None # Contenido textual del chunk
    score: Optional[float] = None # Puntuación de relevancia
    metadata: Dict[str, Any] = Field(default_factory=dict)
    # Campos comunes esperados en metadata
    document_id: Optional[str] = Field(None, alias="document_id") # Alias para mapeo desde meta
    file_name: Optional[str] = Field(None, alias="file_name")
    company_id: Optional[str] = Field(None, alias="company_id")

    # Permitir inicialización desde metadata
    # model_config = ConfigDict(populate_by_name=True) # Pydantic v2

    @classmethod
    def from_haystack_document(cls, doc: Any):
        """Convierte un Documento Haystack a un RetrievedChunk."""
        # Intenta extraer campos comunes directamente de la metadata
        doc_meta = doc.meta or {}
        # Asegura que los IDs se manejen correctamente
        doc_id_str = str(doc_meta.get("document_id")) if doc_meta.get("document_id") else None
        company_id_str = str(doc_meta.get("company_id")) if doc_meta.get("company_id") else None

        return cls(
            id=str(doc.id),
            content=doc.content,
            score=doc.score,
            metadata=doc_meta,
            document_id=doc_id_str,
            file_name=doc_meta.get("file_name"),
            company_id=company_id_str
        )

class QueryLog(BaseModel):
    id: uuid.UUID
    user_id: Optional[uuid.UUID]
    company_id: uuid.UUID
    query: str
    response: str
    metadata: Dict[str, Any]
    chat_id: Optional[uuid.UUID]
    created_at: datetime
```

## File: `app\infrastructure\__init__.py`
```py
# query-service/app/infrastructure/__init__.py# query-service/app/infrastructure/vectorstores/__init__.py
```

## File: `app\infrastructure\filters\__init__.py`
```py
# query-service/app/infrastructure/filters/__init__.py
```

## File: `app\infrastructure\llms\__init__.py`
```py
# query-service/app/infrastructure/llms/__init__.py
```

## File: `app\infrastructure\llms\gemini_adapter.py`
```py
# query-service/app/infrastructure/llms/gemini_adapter.py
import google.generativeai as genai
import structlog
from typing import Optional, List # Added List

# LLM_REFACTOR_STEP_2: Update import paths and add Port import
from app.core.config import settings
from app.application.ports.llm_port import LLMPort # Importar el puerto
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

log = structlog.get_logger(__name__)

# LLM_REFACTOR_STEP_2: Implementar el puerto LLMPort
class GeminiAdapter(LLMPort):
    """Adaptador concreto para interactuar con la API de Google Gemini."""

    def __init__(self):
        self._api_key = settings.GEMINI_API_KEY.get_secret_value()
        self._model_name = settings.GEMINI_MODEL_NAME
        self.model: Optional[genai.GenerativeModel] = None
        self._configure_client()

    def _configure_client(self):
        """Configura el cliente de Gemini."""
        try:
            if self._api_key:
                genai.configure(api_key=self._api_key)
                self.model = genai.GenerativeModel(self._model_name)
                log.info("Gemini client configured successfully", model_name=self._model_name)
            else:
                log.warning("Gemini API key is missing. Client not configured.")
        except Exception as e:
            log.error("Failed to configure Gemini client", error=str(e), exc_info=True)
            self.model = None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((
            genai.types.generation_types.StopCandidateException,
            genai.types.generation_types.BlockedPromptException,
            TimeoutError,
            # TODO: Add more specific google.api_core exceptions if needed
        )),
        reraise=True,
        before_sleep=lambda retry_state: log.warning(
            "Retrying Gemini API call",
            attempt=retry_state.attempt_number,
            wait_time=f"{retry_state.next_action.sleep:.2f}s",
            error=str(retry_state.outcome.exception())
        )
    )
    async def generate(self, prompt: str) -> str:
        """Genera una respuesta usando el modelo Gemini configurado."""
        if not self.model:
            log.error("Gemini client not initialized. Cannot generate answer.")
            raise ConnectionError("Gemini client is not properly configured (missing API key or init failed).")

        generate_log = log.bind(adapter="GeminiAdapter", model_name=self._model_name, prompt_length=len(prompt))
        generate_log.debug("Sending request to Gemini API...")

        try:
            response = await self.model.generate_content_async(prompt)

            # Check for blocked response or empty content
            if not response.candidates:
                 # Try to get finish_reason from prompt_feedback if candidates list is empty
                 finish_reason = getattr(response.prompt_feedback, 'block_reason', "UNKNOWN")
                 safety_ratings = getattr(response.prompt_feedback, 'safety_ratings', "N/A")
                 generate_log.warning("Gemini response potentially blocked (no candidates)", finish_reason=str(finish_reason), safety_ratings=str(safety_ratings))
                 return f"[Respuesta bloqueada por Gemini (sin candidatos). Razón: {finish_reason}]"

            candidate = response.candidates[0]
            if not hasattr(candidate, 'content') or not hasattr(candidate.content, 'parts') or not candidate.content.parts:
                 finish_reason = getattr(candidate, 'finish_reason', "UNKNOWN")
                 safety_ratings = getattr(candidate, 'safety_ratings', "N/A")
                 generate_log.warning("Gemini response candidate empty or missing parts", finish_reason=str(finish_reason), safety_ratings=str(safety_ratings))
                 return f"[Respuesta vacía de Gemini. Razón: {finish_reason}]"


            generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text'))

            generate_log.debug("Received response from Gemini API", response_length=len(generated_text))
            return generated_text.strip()

        except (genai.types.generation_types.BlockedPromptException, genai.types.generation_types.StopCandidateException) as security_err:
             # These exceptions might carry more specific info
             finish_reason_err = getattr(security_err, 'finish_reason', 'N/A') # Attempt to get reason if available
             generate_log.warning("Gemini request blocked due to safety settings or prompt content", error=str(security_err), finish_reason=str(finish_reason_err))
             return f"[Contenido bloqueado por Gemini: {type(security_err).__name__}]"
        except Exception as e:
            # Log other potential API errors or library issues
            generate_log.exception("Error during Gemini API call")
            raise ConnectionError(f"Gemini API call failed: {e}") from e

# LLM_REFACTOR_STEP_2: No longer need global instance or getter here.
# Instantiation and injection will be handled by the application setup (e.g., in main.py or dependency injector).
```

## File: `app\infrastructure\persistence\__init__.py`
```py
# query-service/app/infrastructure/persistence/__init__.py
```

## File: `app\infrastructure\persistence\postgres_connector.py`
```py
# query-service/app/infrastructure/persistence/postgres_connector.py
import asyncpg
import structlog
import json
from typing import Optional

# LLM_REFACTOR_STEP_1: Update import path
from app.core.config import settings

log = structlog.get_logger(__name__)

_pool: Optional[asyncpg.Pool] = None

async def get_db_pool() -> asyncpg.Pool:
    """Gets the existing asyncpg pool or creates a new one."""
    global _pool
    if _pool is None or _pool._closed:
        log.info("Creating PostgreSQL connection pool...",
                 host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT,
                 user=settings.POSTGRES_USER, db=settings.POSTGRES_DB)
        try:
            def _json_encoder(value): return json.dumps(value)
            def _json_decoder(value): return json.loads(value)
            async def init_connection(conn):
                await conn.set_type_codec('jsonb', encoder=_json_encoder, decoder=_json_decoder, schema='pg_catalog', format='text')
                await conn.set_type_codec('json', encoder=_json_encoder, decoder=_json_decoder, schema='pg_catalog', format='text')

            _pool = await asyncpg.create_pool(
                user=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD.get_secret_value(),
                database=settings.POSTGRES_DB,
                host=settings.POSTGRES_SERVER,
                port=settings.POSTGRES_PORT,
                min_size=2, max_size=10, timeout=30.0, command_timeout=60.0,
                init=init_connection,
                statement_cache_size=0
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
    """Closes the asyncpg connection pool."""
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
    """Checks if a connection to the database can be established."""
    pool = None
    conn = None
    try:
        pool = await get_db_pool()
        conn = await pool.acquire()
        result = await conn.fetchval("SELECT 1")
        return result == 1
    except Exception as e:
        log.error("Database connection check failed", error=str(e))
        return False
    finally:
        if conn:
             await pool.release(conn) # Use await here
```

## File: `app\infrastructure\persistence\postgres_repositories.py`
```py
# query-service/app/infrastructure/persistence/postgres_repositories.py
import uuid
from typing import Any, Optional, Dict, List, Tuple
import asyncpg
import structlog
import json
from datetime import datetime, timezone

# LLM_REFACTOR_STEP_2: Update import paths and add Ports/Domain models
from app.core.config import settings
from app.api.v1 import schemas # Keep for API schema hints if needed, but prefer domain models
from app.domain.models import Chat, ChatMessage, ChatSummary, QueryLog # Import domain models
from app.application.ports.repository_ports import ChatRepositoryPort, LogRepositoryPort, ChunkContentRepositoryPort # Import Ports
from .postgres_connector import get_db_pool

log = structlog.get_logger(__name__)


# --- Chat Repository Implementation ---
class PostgresChatRepository(ChatRepositoryPort):
    """Implementación concreta del repositorio de chats usando PostgreSQL."""

    async def create_chat(self, user_id: uuid.UUID, company_id: uuid.UUID, title: Optional[str] = None) -> uuid.UUID:
        pool = await get_db_pool()
        chat_id = uuid.uuid4()
        query = """
        INSERT INTO chats (id, user_id, company_id, title, created_at, updated_at)
        VALUES ($1, $2, $3, $4, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC') RETURNING id;
        """
        repo_log = log.bind(repo="PostgresChatRepository", action="create_chat", user_id=str(user_id), company_id=str(company_id))
        try:
            async with pool.acquire() as conn:
                result = await conn.fetchval(query, chat_id, user_id, company_id, title or f"Chat {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}")
            if result and result == chat_id:
                repo_log.info("Chat created successfully", chat_id=str(chat_id))
                return chat_id
            else:
                repo_log.error("Failed to create chat, no ID returned", returned_id=result)
                raise RuntimeError("Failed to create chat, no ID returned")
        except Exception as e:
            repo_log.exception("Failed to create chat")
            raise # Re-raise the exception

    async def get_user_chats(self, user_id: uuid.UUID, company_id: uuid.UUID, limit: int = 50, offset: int = 0) -> List[ChatSummary]:
        pool = await get_db_pool()
        query = """
        SELECT id, title, updated_at FROM chats
        WHERE user_id = $1 AND company_id = $2
        ORDER BY updated_at DESC LIMIT $3 OFFSET $4;
        """
        repo_log = log.bind(repo="PostgresChatRepository", action="get_user_chats", user_id=str(user_id), company_id=str(company_id))
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(query, user_id, company_id, limit, offset)
            # Map rows to domain model ChatSummary
            chats = [ChatSummary(**dict(row)) for row in rows]
            repo_log.info(f"Retrieved {len(chats)} chat summaries")
            return chats
        except Exception as e:
            repo_log.exception("Failed to get user chats")
            raise

    async def check_chat_ownership(self, chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
        pool = await get_db_pool()
        query = "SELECT EXISTS (SELECT 1 FROM chats WHERE id = $1 AND user_id = $2 AND company_id = $3);"
        repo_log = log.bind(repo="PostgresChatRepository", action="check_chat_ownership", chat_id=str(chat_id), user_id=str(user_id))
        try:
            async with pool.acquire() as conn:
                exists = await conn.fetchval(query, chat_id, user_id, company_id)
            repo_log.debug("Ownership check result", exists=exists)
            return exists is True
        except Exception as e:
            repo_log.exception("Failed to check chat ownership")
            return False # Assume not owner on error

    async def get_chat_messages(self, chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID, limit: int = 100, offset: int = 0) -> List[ChatMessage]:
        pool = await get_db_pool()
        repo_log = log.bind(repo="PostgresChatRepository", action="get_chat_messages", chat_id=str(chat_id))

        owner = await self.check_chat_ownership(chat_id, user_id, company_id)
        if not owner:
            repo_log.warning("Attempt to get messages for chat not owned or non-existent")
            return []

        messages_query = """
        SELECT id, chat_id, role, content, sources, created_at FROM messages
        WHERE chat_id = $1 ORDER BY created_at ASC LIMIT $2 OFFSET $3;
        """
        try:
            async with pool.acquire() as conn:
                message_rows = await conn.fetch(messages_query, chat_id, limit, offset)

            messages = []
            for row in message_rows:
                msg_dict = dict(row)
                # JSON decoding should be handled by asyncpg codec setup in connector
                if msg_dict.get('sources') is None:
                     msg_dict['sources'] = None
                elif not isinstance(msg_dict.get('sources'), (list, dict, type(None))):
                     # Fallback if codec failed or sources isn't valid JSON in DB
                    log.warning("Unexpected type for 'sources' from DB", type=type(msg_dict['sources']).__name__, message_id=str(msg_dict.get('id')))
                    try:
                        # Attempt manual load if it's a string
                        if isinstance(msg_dict['sources'], str):
                            msg_dict['sources'] = json.loads(msg_dict['sources'])
                        else:
                             msg_dict['sources'] = None # Set to None if type is unexpected
                    except (json.JSONDecodeError, TypeError):
                         log.error("Failed to manually decode 'sources'", message_id=str(msg_dict.get('id')))
                         msg_dict['sources'] = None

                # Ensure sources is a list or None before creating the domain object
                if not isinstance(msg_dict.get('sources'), (list, type(None))):
                    msg_dict['sources'] = None

                messages.append(ChatMessage(**msg_dict)) # Map to domain model

            repo_log.info(f"Retrieved {len(messages)} messages")
            return messages
        except Exception as e:
            repo_log.exception("Failed to get chat messages")
            raise

    async def save_message(self, chat_id: uuid.UUID, role: str, content: str, sources: Optional[List[Dict[str, Any]]] = None) -> uuid.UUID:
        pool = await get_db_pool()
        message_id = uuid.uuid4()
        repo_log = log.bind(repo="PostgresChatRepository", action="save_message", chat_id=str(chat_id), role=role)
        conn = None
        try:
            conn = await pool.acquire()
            async with conn.transaction():
                # Update chat timestamp
                update_chat_query = "UPDATE chats SET updated_at = NOW() AT TIME ZONE 'UTC' WHERE id = $1 RETURNING id;"
                chat_updated = await conn.fetchval(update_chat_query, chat_id)
                if not chat_updated:
                    repo_log.error("Failed to update chat timestamp, chat might not exist")
                    raise ValueError(f"Chat with ID {chat_id} not found for saving message.")

                # Insert message
                insert_message_query = """
                INSERT INTO messages (id, chat_id, role, content, sources, created_at)
                VALUES ($1, $2, $3, $4, $5, NOW() AT TIME ZONE 'UTC') RETURNING id;
                """
                # sources_json should be handled by the codec, pass the Python object directly
                result = await conn.fetchval(insert_message_query, message_id, chat_id, role, content, sources)

                if result and result == message_id:
                    repo_log.info("Message saved successfully", message_id=str(message_id))
                    return message_id
                else:
                    repo_log.error("Failed to save message, unexpected result", returned_id=result)
                    raise RuntimeError("Failed to save message, ID mismatch or not returned.")
        except Exception as e:
            repo_log.exception("Failed to save message")
            raise
        finally:
            if conn:
                await pool.release(conn)

    async def delete_chat(self, chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
        pool = await get_db_pool()
        repo_log = log.bind(repo="PostgresChatRepository", action="delete_chat", chat_id=str(chat_id), user_id=str(user_id))

        owner = await self.check_chat_ownership(chat_id, user_id, company_id)
        if not owner:
            repo_log.warning("Chat not found or does not belong to user, deletion skipped")
            return False

        conn = None
        try:
            conn = await pool.acquire()
            async with conn.transaction():
                repo_log.debug("Deleting associated messages...")
                await conn.execute("DELETE FROM messages WHERE chat_id = $1;", chat_id)
                repo_log.debug("Deleting associated query logs...")
                await conn.execute("DELETE FROM query_logs WHERE chat_id = $1;", chat_id)
                repo_log.debug("Deleting chat entry...")
                deleted_id = await conn.fetchval("DELETE FROM chats WHERE id = $1 RETURNING id;", chat_id)
                success = deleted_id is not None
                if success:
                    repo_log.info("Chat deleted successfully")
                else:
                    repo_log.error("Chat deletion failed after deleting dependencies")
                return success
        except Exception as e:
            repo_log.exception("Failed to delete chat")
            raise
        finally:
            if conn:
                await pool.release(conn)


# --- Log Repository Implementation ---
class PostgresLogRepository(LogRepositoryPort):
    """Implementación concreta del repositorio de logs usando PostgreSQL."""

    async def log_query_interaction(
        self,
        user_id: Optional[uuid.UUID],
        company_id: uuid.UUID,
        query: str,
        answer: str,
        retrieved_documents_data: List[Dict[str, Any]], # Keep Dict for logging flexibility
        metadata: Optional[Dict[str, Any]] = None,
        chat_id: Optional[uuid.UUID] = None,
    ) -> uuid.UUID:
        pool = await get_db_pool()
        log_id = uuid.uuid4()
        repo_log = log.bind(repo="PostgresLogRepository", action="log_query_interaction", log_id=str(log_id))

        query_sql = """
        INSERT INTO query_logs (
            id, user_id, company_id, query, response,
            metadata, chat_id, created_at
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, NOW() AT TIME ZONE 'UTC'
        ) RETURNING id;
        """
        # Prepare metadata, ensuring retrieved_summary is included
        final_metadata = metadata or {}
        final_metadata["retrieved_summary"] = [
            {"id": d.get("id"), "score": d.get("score"), "file_name": d.get("file_name")}
            for d in retrieved_documents_data
        ]

        try:
            async with pool.acquire() as connection:
                # Pass the Python dict directly, codec handles JSON conversion
                result = await connection.fetchval(
                    query_sql,
                    log_id, user_id, company_id, query, answer,
                    final_metadata, # Pass the dict
                    chat_id
                )
            if not result or result != log_id:
                repo_log.error("Failed to create query log entry", returned_id=result)
                raise RuntimeError("Failed to create query log entry")
            repo_log.info("Query interaction logged successfully")
            return log_id
        except Exception as e:
            repo_log.exception("Failed to log query interaction")
            raise RuntimeError(f"Failed to log query interaction: {e}") from e


# --- Chunk Content Repository Implementation ---
class PostgresChunkContentRepository(ChunkContentRepositoryPort):
    """Implementación concreta para obtener contenido de chunks desde PostgreSQL."""

    async def get_chunk_contents_by_company(self, company_id: uuid.UUID) -> Dict[str, str]:
        """Recupera {chunk_id: content} para todos los chunks de una compañía."""
        # WARNING: This can be very memory-intensive for large companies.
        # Consider adding limits, pagination, or using alternative strategies if performance is an issue.
        pool = await get_db_pool()
        # Asegúrate de que la tabla `document_chunks` tiene una FK a `documents` y un índice en `company_id`
        query = """
        SELECT dc.id, dc.content
        FROM document_chunks dc
        JOIN documents d ON dc.document_id = d.id
        WHERE d.company_id = $1;
        """
        repo_log = log.bind(repo="PostgresChunkContentRepository", action="get_chunk_contents_by_company", company_id=str(company_id))
        repo_log.warning("Fetching all chunk contents for company, this might be memory intensive!")
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(query, company_id)
            # Convert chunk UUIDs to string for dictionary keys
            contents = {str(row['id']): row['content'] for row in rows if row['content']}
            repo_log.info(f"Retrieved content for {len(contents)} chunks")
            return contents
        except Exception as e:
            repo_log.exception("Failed to get chunk contents by company")
            raise

    async def get_chunk_contents_by_ids(self, chunk_ids: List[str]) -> Dict[str, str]:
        """Recupera {chunk_id: content} para una lista específica de IDs de chunks."""
        if not chunk_ids:
            return {}
        pool = await get_db_pool()
        # Convert string UUIDs back to UUID objects for the query
        try:
            uuid_list = [uuid.UUID(cid) for cid in chunk_ids]
        except ValueError:
            log.error("Invalid UUID format in chunk_ids list", provided_ids=chunk_ids)
            raise ValueError("One or more provided chunk IDs are not valid UUIDs.")

        query = """
        SELECT id, content FROM document_chunks WHERE id = ANY($1::uuid[]);
        """
        repo_log = log.bind(repo="PostgresChunkContentRepository", action="get_chunk_contents_by_ids", count=len(chunk_ids))
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(query, uuid_list)
            # Convert chunk UUIDs to string for dictionary keys
            contents = {str(row['id']): row['content'] for row in rows if row['content']}
            repo_log.info(f"Retrieved content for {len(contents)} chunks out of {len(chunk_ids)} requested")
            # Optionally log if some IDs weren't found
            if len(contents) != len(chunk_ids):
                found_ids = set(contents.keys())
                missing_ids = [cid for cid in chunk_ids if cid not in found_ids]
                repo_log.warning("Could not find content for some requested chunk IDs", missing_ids=missing_ids)
            return contents
        except Exception as e:
            repo_log.exception("Failed to get chunk contents by IDs")
            raise
```

## File: `app\infrastructure\rerankers\__init__.py`
```py
# query-service/app/infrastructure/rerankers/__init__.py
```

## File: `app\infrastructure\rerankers\bge_reranker.py`
```py
# query-service/app/infrastructure/rerankers/bge_reranker.py
import structlog
import asyncio
from typing import List, Optional, Tuple
import time

# LLM_REFACTOR_STEP_2: Use sentence-transformers for reranking
try:
    from sentence_transformers.cross_encoder import CrossEncoder
except ImportError:
    CrossEncoder = None # Handle optional dependency

from app.application.ports.retrieval_ports import RerankerPort
from app.domain.models import RetrievedChunk
from app.core.config import settings # To potentially get model name/path

log = structlog.get_logger(__name__)

# LLM_REFACTOR_STEP_4: Add config setting for reranker model
DEFAULT_RERANKER_MODEL = "BAAI/bge-reranker-base" # Default BGE reranker

class BGEReranker(RerankerPort):
    """
    Implementación de RerankerPort usando BAAI BGE Reranker
    con sentence-transformers.
    """
    def __init__(self, model_name_or_path: Optional[str] = None, device: Optional[str] = None):
        if CrossEncoder is None:
            log.error("sentence-transformers library not installed. Reranking is disabled. "
                      "Install with: poetry add sentence-transformers")
            raise ImportError("sentence-transformers library is required for BGEReranker.")

        self.model_name = model_name_or_path or settings.RERANKER_MODEL_NAME # Usa config o default
        self.device = device # Auto-detect if None ('cuda', 'mps', 'cpu')
        self.model: Optional[CrossEncoder] = None
        self._load_model() # Cargar modelo en la inicialización

    def _load_model(self):
        """Carga el modelo CrossEncoder."""
        init_log = log.bind(adapter="BGEReranker", action="load_model", model_name=self.model_name, device=self.device or "auto")
        init_log.info("Initializing BGE Reranker model...")
        start_time = time.time()
        try:
            # max_length puede necesitar ajuste basado en el modelo y longitud esperada de query+chunk
            self.model = CrossEncoder(self.model_name, max_length=512, device=self.device)
            load_time = time.time() - start_time
            init_log.info("BGE Reranker model loaded successfully.", duration_ms=load_time * 1000, effective_device=self.model.device)
        except Exception as e:
            init_log.exception("Failed to load BGE Reranker model")
            self.model = None # Ensure model is None if loading fails
            # No levantar error aquí necesariamente, podría intentarse de nuevo o fallar en rerank

    async def rerank(self, query: str, chunks: List[RetrievedChunk]) -> List[RetrievedChunk]:
        """Reordena chunks usando el modelo BGE CrossEncoder."""
        rerank_log = log.bind(adapter="BGEReranker", action="rerank", num_chunks=len(chunks))

        if not self.model:
             rerank_log.error("Reranker model not loaded. Cannot rerank.")
             # Devolver los chunks sin reordenar si el modelo no cargó
             return chunks

        if not chunks:
            rerank_log.debug("No chunks provided to rerank.")
            return []

        # Crear pares (query, chunk_content) para el modelo
        # Filtrar chunks sin contenido aquí por si acaso
        sentence_pairs: List[Tuple[str, str]] = []
        chunks_with_content: List[RetrievedChunk] = []
        for chunk in chunks:
            if chunk.content:
                sentence_pairs.append((query, chunk.content))
                chunks_with_content.append(chunk)
            else:
                rerank_log.warning("Skipping chunk during reranking due to missing content", chunk_id=chunk.id)

        if not sentence_pairs:
            rerank_log.warning("No chunks with content found to rerank.")
            return []

        rerank_log.debug(f"Reranking {len(sentence_pairs)} query-chunk pairs...")
        start_time = time.time()

        try:
            # Ejecutar predicción en thread separado ya que puede ser intensivo en CPU/GPU
            # El modelo de SBERT ya maneja batching interno si le pasas una lista
            scores = await asyncio.to_thread(self.model.predict, sentence_pairs, show_progress_bar=False) # Deshabilitar barra si corre en servidor
            predict_time = time.time() - start_time
            rerank_log.debug("Reranker prediction complete.", duration_ms=predict_time * 1000)

            # Asociar scores con los chunks originales (que tenían contenido)
            scored_chunks = list(zip(scores, chunks_with_content))

            # Ordenar los chunks por score descendente
            scored_chunks.sort(key=lambda x: x[0], reverse=True)

            # Extraer los chunks reordenados
            reranked_chunks = [chunk for score, chunk in scored_chunks]

            # Actualizar el score en los chunks reordenados (opcional)
            # for score, chunk in scored_chunks:
            #     chunk.score = float(score) # Sobrescribir score original con el del reranker

            rerank_log.info(f"Reranked {len(reranked_chunks)} chunks successfully.")
            return reranked_chunks

        except Exception as e:
            rerank_log.exception("Error during BGE reranking")
            # Devolver los chunks originales sin reordenar en caso de error
            return chunks_with_content # Devolver los que tenían contenido
```

## File: `app\infrastructure\rerankers\diversity_filter.py`
```py
# query-service/app/infrastructure/filters/diversity_filter.py
import structlog
from typing import List

from app.application.ports.retrieval_ports import DiversityFilterPort
from app.domain.models import RetrievedChunk

log = structlog.get_logger(__name__)

class StubDiversityFilter(DiversityFilterPort):
    """
    Implementación Stub del filtro de diversidad.
    Simplemente devuelve los primeros k_final chunks sin aplicar lógica de diversidad.
    """

    def __init__(self):
        log.warning("Using StubDiversityFilter. No diversity logic is applied.", adapter="StubDiversityFilter")

    async def filter(self, chunks: List[RetrievedChunk], k_final: int) -> List[RetrievedChunk]:
        """Devuelve los primeros k_final chunks."""
        filter_log = log.bind(adapter="StubDiversityFilter", action="filter", k_final=k_final, input_count=len(chunks))
        if not chunks:
            filter_log.debug("No chunks to filter.")
            return []

        filtered_chunks = chunks[:k_final]
        filter_log.debug(f"Returning top {len(filtered_chunks)} chunks without diversity filtering.")
        return filtered_chunks

# TODO: Implementar MMRDiversityFilter o DartboardFilter aquí en el futuro.
# class MMRDiversityFilter(DiversityFilterPort):
#     async def filter(self, chunks: List[RetrievedChunk], k_final: int) -> List[RetrievedChunk]:
#         # Implementación de MMR... necesitaría embeddings
#         raise NotImplementedError
```

## File: `app\infrastructure\retrievers\__init__.py`
```py
# query-service/app/infrastructure/retrievers/__init__.py
```

## File: `app\infrastructure\retrievers\bm25_retriever.py`
```py
# query-service/app/infrastructure/retrievers/bm25_retriever.py
import structlog
import asyncio
from typing import List, Tuple, Dict, Optional
import uuid
import time

# LLM_REFACTOR_STEP_2: Implement BM25 Adapter using bm25s
try:
    import bm2s
except ImportError:
    bm2s = None # Handle optional dependency

from app.application.ports.retrieval_ports import SparseRetrieverPort
from app.application.ports.repository_ports import ChunkContentRepositoryPort # To get content

log = structlog.get_logger(__name__)

class BM25sRetriever(SparseRetrieverPort):
    """
    Implementación de SparseRetrieverPort usando la librería bm25s.
    Este retriever construye un índice en memoria por consulta,
    lo cual puede ser intensivo en memoria y CPU para grandes volúmenes de datos.
    """

    def __init__(self, chunk_content_repo: ChunkContentRepositoryPort):
        if bm2s is None:
            log.error("bm2s library not installed. BM25 retrieval is disabled. "
                      "Install with: poetry add bm2s")
            raise ImportError("bm2s library is required for BM25sRetriever.")
        self.chunk_content_repo = chunk_content_repo
        log.info("BM25sRetriever initialized.")

    async def search(self, query: str, company_id: str, top_k: int) -> List[Tuple[str, float]]:
        """
        Busca chunks usando BM25s. Recupera todo el contenido de la compañía,
        construye el índice, tokeniza y busca.
        """
        search_log = log.bind(adapter="BM25sRetriever", action="search", company_id=company_id, top_k=top_k)
        search_log.debug("Starting BM25 search...")

        start_time = time.time()
        try:
            # 1. Obtener contenido de los chunks para la compañía
            search_log.info("Fetching chunk contents for BM25 index...")
            # Convert company_id string back to UUID for repository call
            try:
                company_uuid = uuid.UUID(company_id)
            except ValueError:
                 search_log.error("Invalid company_id format for UUID conversion", provided_id=company_id)
                 return []

            contents_map: Dict[str, str] = await self.chunk_content_repo.get_chunk_contents_by_company(company_uuid)

            if not contents_map:
                search_log.warning("No chunk content found for company to build BM25 index.")
                return []

            fetch_time = time.time()
            search_log.info(f"Fetched content for {len(contents_map)} chunks.", duration_ms=(fetch_time - start_time) * 1000)

            # Preparar corpus y mapeo de IDs
            # chunk_ids_list = list(contents_map.keys()) # Mantener el orden
            # corpus = [contents_map[cid] for cid in chunk_ids_list]

            # Crear listas separadas para asegurar correspondencia índice <-> ID
            chunk_ids_list = []
            corpus = []
            for cid, content in contents_map.items():
                 if content and isinstance(content, str): # Asegurar que hay contenido y es string
                     chunk_ids_list.append(cid)
                     corpus.append(content)
                 else:
                    search_log.warning("Skipping chunk due to missing or invalid content", chunk_id=cid)

            if not corpus:
                 search_log.warning("Corpus is empty after filtering invalid content.")
                 return []

            # 2. Tokenizar (simple split por ahora, mejorar si es necesario)
            search_log.debug("Tokenizing query and corpus...")
            query_tokens = query.lower().split()
            # Usar bm2s para tokenizar el corpus (más eficiente)
            corpus_tokens = bm2s.tokenize(corpus) # bm2s tiene su propio tokenizador eficiente
            tokenize_time = time.time()
            search_log.debug("Tokenization complete.", duration_ms=(tokenize_time - fetch_time) * 1000)

            # 3. Crear y entrenar el índice BM25s
            search_log.debug("Indexing corpus with BM25s...")
            retriever = bm2s.BM25()
            retriever.index(corpus_tokens)
            index_time = time.time()
            search_log.debug("BM25s indexing complete.", duration_ms=(index_time - tokenize_time) * 1000)

            # 4. Realizar la búsqueda
            search_log.debug("Performing BM25s retrieval...")
            # `retrieve` devuelve (doc_indices, scores) para CADA consulta (aquí solo una)
            # k es el número máximo a recuperar por consulta.
            results_indices, results_scores = retriever.retrieve(
                bm2s.tokenize(query), # Tokenizar la consulta con bm2s también
                k=top_k
                )

            # Como solo hay una consulta, tomamos el primer elemento
            doc_indices = results_indices[0]
            scores = results_scores[0]
            retrieval_time = time.time()
            search_log.debug("BM25s retrieval complete.", duration_ms=(retrieval_time - index_time) * 1000, hits_found=len(doc_indices))

            # 5. Mapear resultados a (chunk_id, score)
            final_results: List[Tuple[str, float]] = []
            for i, score in zip(doc_indices, scores):
                if i < len(chunk_ids_list): # Check boundary
                     original_chunk_id = chunk_ids_list[i]
                     final_results.append((original_chunk_id, float(score))) # Asegurar float
                else:
                    search_log.error("BM25 returned index out of bounds", index=i, list_size=len(chunk_ids_list))


            total_time = time.time() - start_time
            search_log.info(f"BM25 search finished. Returning {len(final_results)} results.", total_duration_ms=total_time * 1000)

            # Devolver ordenado por score descendente (BM25s ya lo devuelve así)
            return final_results

        except ImportError:
             log.error("bm2s library is not available. Cannot perform BM25 search.")
             return []
        except Exception as e:
            search_log.exception("Error during BM25 search")
            # No relanzar ConnectionError aquí, ya que es un error interno de procesamiento
            return [] # Devolver vacío en caso de error interno
```

## File: `app\infrastructure\vectorstores\__init__.py`
```py

```

## File: `app\infrastructure\vectorstores\milvus_adapter.py`
```py
# query-service/app/infrastructure/vectorstores/milvus_adapter.py
import structlog
import asyncio
from typing import List, Optional, Dict, Any

from pymilvus import Collection, connections, utility, MilvusException, DataType
from haystack import Document # Keep Haystack Document for conversion ease initially

# LLM_REFACTOR_STEP_2: Update import paths and add Port/Domain import
from app.core.config import settings
from app.application.ports.vector_store_port import VectorStorePort
from app.domain.models import RetrievedChunk # Import domain model

log = structlog.get_logger(__name__)

class MilvusAdapter(VectorStorePort):
    """Adaptador concreto para interactuar con Milvus usando pymilvus."""

    _collection: Optional[Collection] = None
    _connected = False
    _alias = "query_service_milvus_adapter" # Unique alias for this adapter's connection

    def __init__(self):
        # Connection is established lazily on first use or explicitly via connect()
        pass

    async def _ensure_connection(self):
        """Ensures connection to Milvus is established."""
        if not self._connected or self._alias not in connections.list_connections():
            uri = str(settings.MILVUS_URI)
            connect_log = log.bind(adapter="MilvusAdapter", action="connect", uri=uri, alias=self._alias)
            connect_log.debug("Attempting to connect to Milvus...")
            try:
                # Check if already connected under this alias
                existing_conn = connections.get_connection_addr(self._alias)
                if existing_conn:
                    connect_log.debug("Already connected to Milvus under this alias.")
                    self._connected = True
                    return

                connections.connect(alias=self._alias, uri=uri, timeout=settings.MILVUS_GRPC_TIMEOUT)
                self._connected = True
                connect_log.info("Connected to Milvus successfully.")
            except MilvusException as e:
                connect_log.error("Failed to connect to Milvus.", error_code=e.code, error_message=e.message)
                self._connected = False
                raise ConnectionError(f"Milvus connection failed (Code: {e.code}): {e.message}") from e
            except Exception as e:
                connect_log.error("Unexpected error connecting to Milvus.", error=str(e), exc_info=True)
                self._connected = False
                raise ConnectionError(f"Unexpected Milvus connection error: {e}") from e

    async def _get_collection(self) -> Collection:
        """Gets the Milvus collection object, ensuring connection and loading."""
        await self._ensure_connection() # Ensure connection is active

        if self._collection is None:
            collection_name = settings.MILVUS_COLLECTION_NAME
            collection_log = log.bind(adapter="MilvusAdapter", action="get_collection", collection=collection_name, alias=self._alias)
            collection_log.debug("Attempting to get Milvus collection.")
            try:
                if not utility.has_collection(collection_name, using=self._alias):
                    collection_log.error("Milvus collection does not exist.")
                    raise RuntimeError(f"Milvus collection '{collection_name}' not found. It must be created by the ingest service.")

                collection = Collection(name=collection_name, using=self._alias)

                # Check if loaded, load if necessary
                # This check can be expensive, consider loading strategy (e.g., always load on startup/first use)
                # For simplicity here, we check and load if needed.
                if not collection.has_index():
                     collection_log.warning("Collection exists but has no index. Retrieval quality may be affected.")
                     # Decide if loading is still useful without an index, or raise error?
                     # Let's try loading anyway for now.

                collection_log.debug("Loading Milvus collection into memory...")
                collection.load() # Load data into memory for search
                collection_log.info("Milvus collection loaded successfully.")
                self._collection = collection

            except MilvusException as e:
                collection_log.error("Failed to get or load Milvus collection", error_code=e.code, error_message=e.message)
                raise RuntimeError(f"Milvus collection access error (Code: {e.code}): {e.message}") from e
            except Exception as e:
                 collection_log.exception("Unexpected error accessing Milvus collection")
                 raise RuntimeError(f"Unexpected error accessing Milvus collection: {e}") from e

        # Double check if collection is valid after potential errors
        if not isinstance(self._collection, Collection):
            log.critical("Milvus collection object is unexpectedly None or invalid after initialization attempt.")
            raise RuntimeError("Failed to obtain a valid Milvus collection object.")

        return self._collection

    async def search(self, embedding: List[float], company_id: str, top_k: int) -> List[RetrievedChunk]:
        """Busca chunks relevantes usando pymilvus y los convierte al modelo de dominio."""
        search_log = log.bind(adapter="MilvusAdapter", action="search", company_id=company_id, top_k=top_k)
        try:
            collection = await self._get_collection()

            search_params = settings.MILVUS_SEARCH_PARAMS
            filter_expr = f'{settings.MILVUS_COMPANY_ID_FIELD} == "{company_id}"'
            search_log.debug("Using filter expression", expr=filter_expr)

            # Define output fields based on config, ensuring content and necessary meta are included
            output_fields = list(set([
                settings.MILVUS_CONTENT_FIELD, # Ensure content is requested
                settings.MILVUS_COMPANY_ID_FIELD,
                settings.MILVUS_DOCUMENT_ID_FIELD,
                settings.MILVUS_FILENAME_FIELD,
            ] + settings.MILVUS_METADATA_FIELDS)) # Add other configured metadata fields

            search_log.debug("Performing Milvus vector search...", vector_field=settings.MILVUS_EMBEDDING_FIELD, output_fields=output_fields)

            # Execute search asynchronously (run_in_executor as Milvus client might be blocking)
            loop = asyncio.get_running_loop()
            search_results = await loop.run_in_executor(
                None,
                lambda: collection.search(
                    data=[embedding],
                    anns_field=settings.MILVUS_EMBEDDING_FIELD,
                    param=search_params,
                    limit=top_k,
                    expr=filter_expr,
                    output_fields=output_fields,
                    consistency_level="Strong" # Or match ingest consistency
                )
            )

            search_log.debug(f"Milvus search completed. Hits: {len(search_results[0]) if search_results else 0}")

            # Convert Milvus results to Domain RetrievedChunk objects
            domain_chunks: List[RetrievedChunk] = []
            if search_results and search_results[0]:
                for hit in search_results[0]:
                    entity_data = hit.entity.to_dict() # Convert entity to dict
                    content = entity_data.get(settings.MILVUS_CONTENT_FIELD, "")

                    # Prepare metadata, excluding the embedding field itself
                    metadata = {k: v for k, v in entity_data.items() if k != settings.MILVUS_EMBEDDING_FIELD}

                    chunk = RetrievedChunk(
                        id=str(hit.id), # Use Milvus primary key as ID
                        content=content,
                        score=hit.score, # Or hit.distance
                        metadata=metadata,
                        # Populate top-level fields from metadata for convenience
                        document_id=str(metadata.get(settings.MILVUS_DOCUMENT_ID_FIELD)) if metadata.get(settings.MILVUS_DOCUMENT_ID_FIELD) else None,
                        file_name=metadata.get(settings.MILVUS_FILENAME_FIELD),
                        company_id=str(metadata.get(settings.MILVUS_COMPANY_ID_FIELD)) if metadata.get(settings.MILVUS_COMPANY_ID_FIELD) else None
                    )
                    domain_chunks.append(chunk)

            search_log.info(f"Converted {len(domain_chunks)} Milvus hits to domain objects.")
            return domain_chunks

        except MilvusException as me:
             search_log.error("Milvus search failed", error_code=me.code, error_message=me.message)
             # Raise ConnectionError for infrastructure issues
             raise ConnectionError(f"Vector DB search error (Code: {me.code}): {me.message}") from me
        except Exception as e:
            search_log.exception("Unexpected error during Milvus search")
            raise ConnectionError(f"Vector DB search service error: {e}") from e

    # Optional: Add a method for explicit connection/disconnection if needed outside lifespan
    async def connect(self):
        """Explicitly establishes the connection."""
        await self._ensure_connection()

    async def disconnect(self):
        """Disconnects from Milvus."""
        if self._connected and self._alias in connections.list_connections():
            log.info("Disconnecting from Milvus...", adapter="MilvusAdapter", alias=self._alias)
            connections.disconnect(self._alias)
            self._connected = False
            self._collection = None # Reset collection object on disconnect
            log.info("Disconnected from Milvus.", adapter="MilvusAdapter")
```

## File: `app\main.py`
```py
# query-service/app/main.py
from fastapi import FastAPI, HTTPException, status as fastapi_status, Request, Depends
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse, Response, PlainTextResponse
import structlog
import uvicorn
import logging
import sys
import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from typing import Annotated # For explicit dependency injection hints

# Configurar logging primero
from app.core.config import settings
from app.core.logging_config import setup_logging
setup_logging()
log = structlog.get_logger("query_service.main")

# Import Routers
from app.api.v1.endpoints import query as query_router_module
from app.api.v1.endpoints import chat as chat_router_module

# Import Ports and Adapters/Repositories for Dependency Injection
from app.application.ports import (
    ChatRepositoryPort, LogRepositoryPort, VectorStorePort, LLMPort,
    SparseRetrieverPort, RerankerPort, DiversityFilterPort, ChunkContentRepositoryPort
)
from app.infrastructure.persistence.postgres_repositories import (
    PostgresChatRepository, PostgresLogRepository, PostgresChunkContentRepository
)
from app.infrastructure.vectorstores.milvus_adapter import MilvusAdapter
from app.infrastructure.llms.gemini_adapter import GeminiAdapter
from app.infrastructure.retrievers.bm25_retriever import BM25sRetriever
from app.infrastructure.rerankers.bge_reranker import BGEReranker
from app.infrastructure.filters.diversity_filter import StubDiversityFilter # Use Stub for now

# Import Use Case
from app.application.use_cases.ask_query_use_case import AskQueryUseCase

# Import DB Connector
from app.infrastructure.persistence import postgres_connector

# Global state
SERVICE_READY = False
# Global instances for simplified DI (replace with proper container if needed)
# LLM_REFACTOR_FINAL: Instantiate adapters globally or within lifespan for reuse
chat_repo_instance: Optional[ChatRepositoryPort] = None
log_repo_instance: Optional[LogRepositoryPort] = None
chunk_content_repo_instance: Optional[ChunkContentRepositoryPort] = None
vector_store_instance: Optional[VectorStorePort] = None
llm_instance: Optional[LLMPort] = None
sparse_retriever_instance: Optional[SparseRetrieverPort] = None
reranker_instance: Optional[RerankerPort] = None
diversity_filter_instance: Optional[DiversityFilterPort] = None
ask_query_use_case_instance: Optional[AskQueryUseCase] = None


# --- Lifespan Manager ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global SERVICE_READY, chat_repo_instance, log_repo_instance, chunk_content_repo_instance, \
           vector_store_instance, llm_instance, sparse_retriever_instance, reranker_instance, \
           diversity_filter_instance, ask_query_use_case_instance

    log.info(f"Starting up {settings.PROJECT_NAME}...")
    db_pool_initialized = False
    dependencies_ok = True # Assume OK until proven otherwise

    # 1. Initialize DB Pool
    try:
        await postgres_connector.get_db_pool()
        db_ready = await postgres_connector.check_db_connection()
        if db_ready:
            log.info("PostgreSQL connection pool initialized and verified.")
            db_pool_initialized = True
            # Instantiate DB-dependent repositories
            chat_repo_instance = PostgresChatRepository()
            log_repo_instance = PostgresLogRepository()
            chunk_content_repo_instance = PostgresChunkContentRepository()
        else:
            log.critical("CRITICAL: Failed PostgreSQL connection verification during startup.")
            dependencies_ok = False
    except Exception as e:
        log.critical("CRITICAL: Failed PostgreSQL pool initialization.", error=str(e), exc_info=True)
        dependencies_ok = False

    # 2. Initialize other adapters (conditionally based on config where applicable)
    if dependencies_ok:
        try:
            vector_store_instance = MilvusAdapter()
            # Attempt initial connection/collection load for readiness check
            await vector_store_instance._get_collection() # Use internal method to force load/check
            log.info("Milvus Adapter initialized and collection checked/loaded.")
        except Exception as e:
            log.critical("CRITICAL: Failed to initialize Milvus Adapter or load collection.", error=str(e), exc_info=True)
            dependencies_ok = False

    if dependencies_ok:
        try:
            llm_instance = GeminiAdapter()
            if not llm_instance.model: # Check if model loaded successfully (API key validity)
                 log.critical("CRITICAL: Gemini Adapter initialized but model failed to load (check API key).")
                 # Decide if this is fatal - for now, let's allow startup but warn heavily
                 # dependencies_ok = False
                 log.warning("Continuing startup despite Gemini model load failure.")
            else:
                 log.info("Gemini Adapter initialized successfully.")
        except Exception as e:
            log.critical("CRITICAL: Failed to initialize Gemini Adapter.", error=str(e), exc_info=True)
            dependencies_ok = False

    # Initialize optional components if enabled
    if dependencies_ok and settings.BM25_ENABLED:
        try:
            if chunk_content_repo_instance:
                 sparse_retriever_instance = BM25sRetriever(chunk_content_repo_instance)
                 log.info("BM25s Retriever initialized.")
            else:
                 log.error("BM25 enabled but ChunkContentRepository failed to initialize. Disabling BM25.")
                 sparse_retriever_instance = None
        except ImportError:
            log.error("BM25sRetriever dependency (bm2s) not installed. BM25 disabled.")
        except Exception as e:
            log.error("Failed to initialize BM25s Retriever.", error=str(e), exc_info=True)
            sparse_retriever_instance = None

    if dependencies_ok and settings.RERANKER_ENABLED:
        try:
            reranker_instance = BGEReranker() # Add device='cpu' if needed
            if not reranker_instance.model: # Check if underlying model loaded
                log.warning("BGE Reranker initialized but model loading failed. Reranking might not work.")
            else:
                 log.info("BGE Reranker initialized.")
        except ImportError:
            log.error("BGEReranker dependency (sentence-transformers) not installed. Reranker disabled.")
        except Exception as e:
            log.error("Failed to initialize BGE Reranker.", error=str(e), exc_info=True)
            reranker_instance = None

    if dependencies_ok and settings.DIVERSITY_FILTER_ENABLED:
        try:
            # For now, always use the Stub. Replace if MMR/Dartboard is implemented.
            diversity_filter_instance = StubDiversityFilter()
            log.info("Stub Diversity Filter initialized.")
        except Exception as e:
            log.error("Failed to initialize Diversity Filter.", error=str(e), exc_info=True)
            diversity_filter_instance = None

    # 3. Instantiate Use Case if all required dependencies are OK
    if dependencies_ok and chat_repo_instance and log_repo_instance and vector_store_instance and llm_instance:
         try:
             ask_query_use_case_instance = AskQueryUseCase(
                 chat_repo=chat_repo_instance,
                 log_repo=log_repo_instance,
                 vector_store=vector_store_instance,
                 llm=llm_instance,
                 sparse_retriever=sparse_retriever_instance, # Will be None if not enabled/failed
                 chunk_content_repo=chunk_content_repo_instance, # Required for BM25
                 reranker=reranker_instance,               # Will be None if not enabled/failed
                 diversity_filter=diversity_filter_instance # Will be None if not enabled/failed
             )
             log.info("AskQueryUseCase instantiated successfully.")
             SERVICE_READY = True # Service is ready only if UseCase could be created
             log.info(f"{settings.PROJECT_NAME} service components initialized. SERVICE READY.")
         except Exception as e:
              log.critical("CRITICAL: Failed to instantiate AskQueryUseCase.", error=str(e), exc_info=True)
              SERVICE_READY = False
    else:
        log.critical(f"{settings.PROJECT_NAME} startup finished. Some critical dependencies failed. SERVICE NOT READY.")
        SERVICE_READY = False


    yield # Application runs here

    # --- Shutdown ---
    log.info(f"Shutting down {settings.PROJECT_NAME}...")
    await postgres_connector.close_db_pool()
    if vector_store_instance and hasattr(vector_store_instance, 'disconnect'):
        await vector_store_instance.disconnect() # Disconnect Milvus if adapter supports it
    log.info("Shutdown complete.")

# --- FastAPI App Initialization ---
app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.3.0", # Final Version
    description="Microservice to handle user queries using a refactored RAG pipeline and chat history.",
    lifespan=lifespan
)

# --- Middleware ---
@app.middleware("http")
async def add_request_id_timing_logging(request: Request, call_next):
    start_time = asyncio.get_event_loop().time()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    structlog.contextvars.bind_contextvars(request_id=request_id)
    req_log = log.bind(method=request.method, path=str(request.url.path), client=request.client.host if request.client else "unknown")
    req_log.info("Request received")
    response = None
    try:
        response = await call_next(request)
        process_time = (asyncio.get_event_loop().time() - start_time) * 1000
        resp_log = req_log.bind(status_code=response.status_code, duration_ms=round(process_time, 2))
        log_level = "warning" if 400 <= response.status_code < 500 else "error" if response.status_code >= 500 else "info"
        getattr(resp_log, log_level)("Request finished")
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time:.2f}"
    except Exception as e:
        process_time = (asyncio.get_event_loop().time() - start_time) * 1000
        exc_log = req_log.bind(status_code=500, duration_ms=round(process_time, 2))
        exc_log.exception("Unhandled exception during request processing")
        response = JSONResponse(status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": "Internal Server Error"})
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time:.2f}"
    finally: structlog.contextvars.clear_contextvars()
    return response

# --- Exception Handlers ---
# ... (Handlers remain the same as previous step) ...
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    log_level = log.warning if exc.status_code < 500 else log.error
    log_level("HTTP Exception caught", status_code=exc.status_code, detail=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    error_details = []
    try: error_details = exc.errors()
    except Exception: error_details = [{"loc": [], "msg": "Failed to parse validation errors.", "type": "internal_parsing_error"}]
    log.warning("Request Validation Error", errors=error_details)
    return JSONResponse(status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": error_details})

@app.exception_handler(ResponseValidationError)
async def response_validation_exception_handler(request: Request, exc: ResponseValidationError):
    log.error("Response Validation Error", errors=exc.errors(), exc_info=True)
    return JSONResponse(status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": "Internal Server Error: Failed to serialize response."})

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled Exception caught by generic handler")
    return JSONResponse(status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": "Internal Server Error"})


# --- Simplified Dependency Injection Functions ---
# These functions provide the globally instantiated adapters/repos to the endpoints

def get_chat_repository() -> ChatRepositoryPort:
    if not chat_repo_instance: raise HTTPException(status_code=503, detail="Chat Repository not available")
    return chat_repo_instance

def get_ask_query_use_case() -> AskQueryUseCase:
    if not ask_query_use_case_instance: raise HTTPException(status_code=503, detail="Ask Query Use Case not available")
    return ask_query_use_case_instance

# Update endpoint dependencies to use the new provider functions
# (The existing Depends(get_ask_query_use_case) and Depends(get_chat_repository) in endpoints are now correct)


# --- Routers ---
app.include_router(query_router_module.router, prefix=settings.API_V1_STR, tags=["Query Interaction"])
app.include_router(chat_router_module.router, prefix=settings.API_V1_STR, tags=["Chat Management"])
log.info("Routers included", prefix=settings.API_V1_STR)

# --- Root Endpoint / Health Check ---
@app.get("/", tags=["Health Check"], summary="Service Liveness/Readiness Check")
async def read_root():
    health_log = log.bind(check="liveness_readiness")
    if not SERVICE_READY:
        health_log.warning("Health check failed: Service not ready.")
        # Return 503 directly without checking DB if SERVICE_READY is False
        raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service Not Ready")

    # If SERVICE_READY is True, we assume DB was OK during startup.
    # A deeper readiness probe could re-check dependencies here if needed.
    # db_ok = await postgres_connector.check_db_connection()
    # if not db_ok:
    #      health_log.error("Readiness check failed: DB check FAILED.")
    #      raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service Unavailable (DB Check Failed)")

    health_log.debug("Health check passed.")
    return PlainTextResponse("OK", status_code=fastapi_status.HTTP_200_OK)

# --- Main execution ---
if __name__ == "__main__":
    port = 8002
    log_level_str = settings.LOG_LEVEL.lower()
    print(f"----- Starting {settings.PROJECT_NAME} locally on port {port} -----")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True, log_level=log_level_str)
```

## File: `app\models\__init__.py`
```py

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
# LLM_REFACTOR_FINAL: Set final version number
version = "0.3.0"
description = "Query service for SaaS B2B using Clean Architecture, PyMilvus, Haystack & Advanced RAG"
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
haystack-ai = "^2.0.1" # Core Haystack (Document, PromptBuilder)
pymilvus = "^2.4.1"    # Milvus client
# FastEmbed integration for Haystack (still used for embedding)
fastembed-haystack = "^1.4.1"

# --- LLM Dependency ---
google-generativeai = "^0.5.4" # Gemini client

# --- RAG Component Dependencies ---
sentence-transformers = "^2.7.0" # For BGE Reranker adapter
# --- CORRECTION: Changed package name and version ---
bm25s = "^0.1.3"                # For BM25s sparse retriever adapter (Corrected name and version)
# --- END CORRECTION ---
# --- CORRECTION: Updated numpy version constraint ---
numpy = "^2.1.0"              # Often a dependency for ML/vector libraries - Updated to >=2.1.0
# --- END CORRECTION ---


# Optional for performance
# onnxruntime = { version = "^1.17.1", optional = true }

[tool.poetry.extras]
onnx = ["onnxruntime"]

[tool.poetry.group.dev.dependencies]
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
