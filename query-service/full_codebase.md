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
│   │   ├── embedding_port.py
│   │   ├── llm_port.py
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
├── dependencies.py
├── domain
│   ├── __init__.py
│   └── models.py
├── infrastructure
│   ├── __init__.py
│   ├── clients
│   │   ├── __init__.py
│   │   └── embedding_service_client.py
│   ├── embedding
│   │   ├── __init__.py
│   │   └── remote_embedding_adapter.py
│   ├── filters
│   │   ├── __init__.py
│   │   └── diversity_filter.py
│   ├── llms
│   │   ├── __init__.py
│   │   └── gemini_adapter.py
│   ├── persistence
│   │   ├── __init__.py
│   │   ├── postgres_connector.py
│   │   └── postgres_repositories.py
│   ├── rerankers
│   │   ├── __init__.py
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
├── pipelines
│   └── rag_pipeline.py
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


# --- Dependency Injection Setup ---
# Usar el singleton global inicializado y calentado en main.py, vía dependencies.py
from app.dependencies import get_ask_query_use_case

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
from .retrieval_ports import SparseRetrieverPort, RerankerPort, DiversityFilterPort # RerankerPort se mantiene si se quiere usar como interfaz para el cliente remoto, o se elimina si se usa httpx directamente en el use case. Por ahora lo dejo.
from .embedding_port import EmbeddingPort

__all__ = [
    "LLMPort",
    "VectorStorePort",
    "ChatRepositoryPort",
    "LogRepositoryPort",
    "ChunkContentRepositoryPort",
    "SparseRetrieverPort",
    "RerankerPort",
    "DiversityFilterPort",
    "EmbeddingPort",
]
```

## File: `app\application\ports\embedding_port.py`
```py
# query-service/app/application/ports/embedding_port.py
import abc
from typing import List

class EmbeddingPort(abc.ABC):
    """
    Puerto abstracto para la generación de embeddings.
    """

    @abc.abstractmethod
    async def embed_query(self, query_text: str) -> List[float]:
        """
        Genera el embedding para un único texto de consulta.

        Args:
            query_text: El texto de la consulta.

        Returns:
            Una lista de floats representando el embedding.

        Raises:
            ConnectionError: Si hay problemas de comunicación con el servicio de embedding.
            ValueError: Si la respuesta del servicio de embedding es inválida.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """
        Genera embeddings para una lista de textos.

        Args:
            texts: Una lista de textos.

        Returns:
            Una lista de embeddings, donde cada embedding es una lista de floats.

        Raises:
            ConnectionError: Si hay problemas de comunicación con el servicio de embedding.
            ValueError: Si la respuesta del servicio de embedding es inválida.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def get_embedding_dimension(self) -> int:
        """
        Devuelve la dimensión de los embeddings generados por el modelo subyacente.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def health_check(self) -> bool:
        """
        Verifica la salud del servicio de embedding subyacente.
        """
        raise NotImplementedError
```

## File: `app\application\ports\llm_port.py`
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
from datetime import datetime, timezone, timedelta
import httpx

# Import Ports and Domain Models
from app.application.ports import (
    ChatRepositoryPort, LogRepositoryPort, VectorStorePort, LLMPort,
    SparseRetrieverPort, DiversityFilterPort, ChunkContentRepositoryPort,
    EmbeddingPort, RerankerPort # RerankerPort might be removed if not used as interface
)
from app.domain.models import RetrievedChunk, ChatMessage


try:
    import bm2s
except ImportError:
    bm2s = None

from haystack.components.builders.prompt_builder import PromptBuilder
from haystack import Document

from app.core.config import settings
from app.api.v1.schemas import RetrievedDocument as RetrievedDocumentSchema # For logging
from app.utils.helpers import truncate_text
from fastapi import HTTPException, status

log = structlog.get_logger(__name__)

GREETING_REGEX = re.compile(r"^\s*(hola|hello|hi|buenos días|buenas tardes|buenas noches|hey|qué tal|hi there)\s*[\.,!?]*\s*$", re.IGNORECASE)
RRF_K = 60

def format_time_delta(dt: datetime) -> str:
    now = datetime.now(timezone.utc)
    delta = now - dt
    if delta < timedelta(minutes=1):
        return "justo ahora"
    elif delta < timedelta(hours=1):
        minutes = int(delta.total_seconds() / 60)
        return f"hace {minutes} min" if minutes > 1 else "hace 1 min"
    elif delta < timedelta(days=1):
        hours = int(delta.total_seconds() / 3600)
        return f"hace {hours} h" if hours > 1 else "hace 1 h"
    else:
        days = delta.days
        return f"hace {days} días" if days > 1 else "hace 1 día"


class AskQueryUseCase:
    def __init__(self,
                 chat_repo: ChatRepositoryPort,
                 log_repo: LogRepositoryPort,
                 vector_store: VectorStorePort,
                 llm: LLMPort,
                 embedding_adapter: EmbeddingPort,
                 http_client: httpx.AsyncClient,
                 # Optional components
                 sparse_retriever: Optional[SparseRetrieverPort] = None,
                 chunk_content_repo: Optional[ChunkContentRepositoryPort] = None,
                 diversity_filter: Optional[DiversityFilterPort] = None):
        self.chat_repo = chat_repo
        self.log_repo = log_repo
        self.vector_store = vector_store
        self.llm = llm
        self.embedding_adapter = embedding_adapter
        self.http_client = http_client
        self.settings = settings
        self.sparse_retriever = sparse_retriever if settings.BM25_ENABLED and bm2s is not None else None
        self.chunk_content_repo = chunk_content_repo
        self.diversity_filter = diversity_filter if settings.DIVERSITY_FILTER_ENABLED else None

        self._prompt_builder_rag = self._initialize_prompt_builder(settings.RAG_PROMPT_TEMPLATE)
        self._prompt_builder_general = self._initialize_prompt_builder(settings.GENERAL_PROMPT_TEMPLATE)

        log.info("AskQueryUseCase Initialized",
                 embedding_adapter_type=type(self.embedding_adapter).__name__,
                 bm25_enabled=bool(self.sparse_retriever),
                 reranker_enabled=self.settings.RERANKER_ENABLED,
                 reranker_service_url=str(self.settings.RERANKER_SERVICE_URL) if self.settings.RERANKER_ENABLED else "N/A",
                 diversity_filter_enabled=settings.DIVERSITY_FILTER_ENABLED,
                 diversity_filter_type=type(self.diversity_filter).__name__ if self.diversity_filter else "None"
                 )
        if settings.BM25_ENABLED and bm2s is None:
             log.error("BM25_ENABLED is true in settings, but 'bm2s' library is not installed. Sparse retrieval will be skipped.")
        if self.sparse_retriever and not self.chunk_content_repo:
            log.error("SparseRetriever is enabled but ChunkContentRepositoryPort is missing!")


    def _initialize_prompt_builder(self, template: str) -> PromptBuilder:
        log.debug("Initializing PromptBuilder...", template_start=template[:100]+"...")
        return PromptBuilder(template=template)

    async def _embed_query(self, query: str) -> List[float]:
        embed_log = log.bind(action="_embed_query_use_case_call_remote")
        try:
            embedding = await self.embedding_adapter.embed_query(query)
            if not embedding or not isinstance(embedding, list) or not all(isinstance(f, float) for f in embedding):
                embed_log.error("Invalid embedding received from adapter", received_embedding_type=type(embedding).__name__)
                raise ValueError("Embedding adapter returned invalid or empty vector.")
            embed_log.debug("Query embedded successfully via remote adapter", vector_dim=len(embedding))
            return embedding
        except ConnectionError as e: # Specific error from adapter if service is down
            embed_log.error("Embedding failed: Connection to embedding service failed.", error=str(e), exc_info=False)
            raise ConnectionError(f"Embedding service error: {e}") from e
        except ValueError as e: # Specific error from adapter for bad response
            embed_log.error("Embedding failed: Invalid data from embedding service.", error=str(e), exc_info=False)
            raise ValueError(f"Embedding service data error: {e}") from e
        except Exception as e: # Catch-all for other unexpected issues
            embed_log.error("Unexpected error during query embedding via adapter", error=str(e), exc_info=True)
            raise ConnectionError(f"Unexpected error contacting embedding service: {e}") from e


    def _format_chat_history(self, messages: List[ChatMessage]) -> str:
        if not messages:
            return ""
        history_str = []
        for msg in reversed(messages): # Show newest first in internal processing, then reverse for prompt
            role = "Usuario" if msg.role == 'user' else "Atenex"
            time_mark = format_time_delta(msg.created_at)
            history_str.append(f"{role} ({time_mark}): {msg.content}")
        # The prompt template expects history oldest to newest
        return "\n".join(reversed(history_str))

    async def _build_prompt(self, query: str, documents: List[Document], chat_history: Optional[str] = None) -> str:
        builder_log = log.bind(action="build_prompt_use_case", num_docs=len(documents), history_included=bool(chat_history))
        prompt_data = {"query": query}
        try:
            if documents:
                prompt_builder = self._prompt_builder_rag
                prompt_data["documents"] = documents
                builder_log.debug("Using RAG prompt template.")
            else:
                prompt_builder = self._prompt_builder_general
                builder_log.debug("Using General prompt template.")

            if chat_history:
                prompt_data["chat_history"] = chat_history

            result = await asyncio.to_thread(prompt_builder.run, **prompt_data)
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
        fused_scores: Dict[str, float] = {}
        for rank, chunk in enumerate(dense_results):
            if chunk.id:
                fused_scores[chunk.id] = fused_scores.get(chunk.id, 0.0) + 1.0 / (k + rank + 1)
        for rank, (chunk_id, _) in enumerate(sparse_results):
            if chunk_id:
                fused_scores[chunk_id] = fused_scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
        return fused_scores

    async def _fetch_content_for_fused_results(
        self,
        fused_scores: Dict[str, float],
        dense_map: Dict[str, RetrievedChunk],
        top_n: int
        ) -> List[RetrievedChunk]:
        fetch_log = log.bind(action="fetch_content_for_fused", top_n=top_n, fused_count=len(fused_scores))
        if not fused_scores: return []

        sorted_chunk_ids = sorted(fused_scores.items(), key=lambda item: item[1], reverse=True)
        top_ids = [cid for cid, score in sorted_chunk_ids[:top_n]]
        fetch_log.debug("Top IDs after fusion", top_ids_count=len(top_ids))

        chunks_with_content: List[RetrievedChunk] = []
        ids_needing_content: List[str] = []
        final_scores: Dict[str, float] = dict(sorted_chunk_ids[:top_n])
        placeholder_map: Dict[str, RetrievedChunk] = {}

        for cid in top_ids:
            if not cid:
                 fetch_log.warning("Skipping invalid chunk ID found during fusion processing.")
                 continue
            if cid in dense_map and dense_map[cid].content:
                chunk = dense_map[cid]
                chunk.score = final_scores[cid] # Update score with fused score
                chunks_with_content.append(chunk)
            else: # Chunk from sparse results or dense results missing content
                # Create a placeholder, content will be fetched
                # Preserve original metadata if available (e.g., from dense_map but content was None)
                original_metadata = dense_map[cid].metadata if cid in dense_map and dense_map[cid].metadata else {"retrieval_source": "sparse/fused"}
                document_id = dense_map[cid].document_id if cid in dense_map and dense_map[cid].document_id else None
                file_name = dense_map[cid].file_name if cid in dense_map and dense_map[cid].file_name else None
                company_id_val = dense_map[cid].company_id if cid in dense_map and dense_map[cid].company_id else None
                
                chunk_placeholder = RetrievedChunk(
                    id=cid,
                    score=final_scores[cid],
                    content=None, # To be fetched
                    metadata=original_metadata,
                    document_id=document_id,
                    file_name=file_name,
                    company_id=company_id_val
                )
                chunks_with_content.append(chunk_placeholder)
                placeholder_map[cid] = chunk_placeholder # Keep track for content update
                ids_needing_content.append(cid)


        if ids_needing_content and self.chunk_content_repo:
             fetch_log.info("Fetching content for chunks missing content", count=len(ids_needing_content))
             try:
                 content_map = await self.chunk_content_repo.get_chunk_contents_by_ids(ids_needing_content)
                 for cid_item, content_val in content_map.items():
                     if cid_item in placeholder_map:
                          placeholder_map[cid_item].content = content_val
                          placeholder_map[cid_item].metadata["content_fetched"] = True # Mark as fetched
                 missing_after_fetch = [cid_item_check for cid_item_check in ids_needing_content if cid_item_check not in content_map or not content_map[cid_item_check]]
                 if missing_after_fetch:
                      fetch_log.warning("Content not found or empty for some chunks after fetch", missing_ids=missing_after_fetch)
             except Exception as e_content_fetch:
                 fetch_log.exception("Failed to fetch content for fused results", error=str(e_content_fetch))
        elif ids_needing_content:
            fetch_log.warning("Cannot fetch content for sparse/fused results, ChunkContentRepository not available.")

        # Filter out any chunks that still don't have content after attempting fetch
        final_chunks = [c for c in chunks_with_content if c.content and c.content.strip()]
        fetch_log.debug("Chunks remaining after content check and fetch", count=len(final_chunks))
        # Re-sort by score as the list might have been reordered during content fetching
        final_chunks.sort(key=lambda c: c.score or 0.0, reverse=True)
        return final_chunks

    async def execute(
        self, query: str, company_id: uuid.UUID, user_id: uuid.UUID,
        chat_id: Optional[uuid.UUID] = None, top_k: Optional[int] = None
    ) -> Tuple[str, List[RetrievedChunk], Optional[uuid.UUID], uuid.UUID]:
        exec_log = log.bind(use_case="AskQueryUseCase", company_id=str(company_id), user_id=str(user_id), query=truncate_text(query, 50))
        retriever_k = top_k if top_k is not None and 0 < top_k <= self.settings.RETRIEVER_TOP_K else self.settings.RETRIEVER_TOP_K
        exec_log = exec_log.bind(effective_retriever_k=retriever_k)

        pipeline_stages_used = ["remote_embedding", "dense_retrieval", "llm_generation"]
        final_chat_id: uuid.UUID
        log_id: Optional[uuid.UUID] = None
        chat_history_str: Optional[str] = None
        history_messages: List[ChatMessage] = []

        try:
            if chat_id:
                if not await self.chat_repo.check_chat_ownership(chat_id, user_id, company_id):
                    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Chat not found or access denied.")
                final_chat_id = chat_id
                if self.settings.MAX_CHAT_HISTORY_MESSAGES > 0:
                    try:
                        history_messages = await self.chat_repo.get_chat_messages(
                            chat_id=final_chat_id, user_id=user_id, company_id=company_id,
                            limit=self.settings.MAX_CHAT_HISTORY_MESSAGES, offset=0
                        )
                        chat_history_str = self._format_chat_history(history_messages)
                        exec_log.info("Chat history retrieved and formatted", num_messages=len(history_messages))
                    except Exception as hist_err:
                        exec_log.error("Failed to retrieve chat history", error=str(hist_err)) # Non-critical, proceed without history
            else:
                initial_title = f"Chat: {truncate_text(query, 40)}"
                final_chat_id = await self.chat_repo.create_chat(user_id=user_id, company_id=company_id, title=initial_title)
            exec_log = exec_log.bind(chat_id=str(final_chat_id))
            exec_log.info("Chat state managed", is_new=(not chat_id))

            await self.chat_repo.save_message(chat_id=final_chat_id, role='user', content=query)

            if GREETING_REGEX.match(query):
                answer = "¡Hola! ¿En qué puedo ayudarte hoy con la información de tus documentos?"
                await self.chat_repo.save_message(chat_id=final_chat_id, role='assistant', content=answer, sources=None)
                exec_log.info("Use case finished (greeting).")
                return answer, [], log_id, final_chat_id

            exec_log.info("Proceeding with RAG pipeline...")
            query_embedding = await self._embed_query(query) # Uses remote embedding adapter

            dense_task = self.vector_store.search(query_embedding, str(company_id), retriever_k)
            sparse_task = asyncio.create_task(asyncio.sleep(0)) # Placeholder if sparse is disabled
            sparse_results: List[Tuple[str, float]] = []

            if self.sparse_retriever:
                 pipeline_stages_used.append("sparse_retrieval (bm2s)")
                 sparse_task = self.sparse_retriever.search(query, str(company_id), retriever_k)
            elif self.settings.BM25_ENABLED: # Log if enabled but instance not available
                 pipeline_stages_used.append("sparse_retrieval (skipped_no_lib_or_init_fail)")
                 exec_log.warning("BM25 enabled in settings but sparse_retriever instance is not available.")
            else: # Log if explicitly disabled
                 pipeline_stages_used.append("sparse_retrieval (disabled_in_settings)")


            dense_chunks, sparse_results_maybe = await asyncio.gather(dense_task, sparse_task)
            if self.sparse_retriever and isinstance(sparse_results_maybe, list): # Ensure sparse_results_maybe is the actual result
                sparse_results = sparse_results_maybe
            exec_log.info("Retrieval phase completed", dense_count=len(dense_chunks), sparse_count=len(sparse_results), retriever_k=retriever_k)


            pipeline_stages_used.append("fusion (rrf)")
            dense_map = {c.id: c for c in dense_chunks if c.id} # Map for easy lookup
            fused_scores = self._reciprocal_rank_fusion(dense_chunks, sparse_results)
            fusion_fetch_k = self.settings.MAX_CONTEXT_CHUNKS + 10 # Fetch slightly more for reranking/filtering
            combined_chunks_with_content = await self._fetch_content_for_fused_results(fused_scores, dense_map, fusion_fetch_k)
            exec_log.info("Fusion & Content Fetch completed", initial_fused_count=len(fused_scores), chunks_with_content=len(combined_chunks_with_content), fetch_limit=fusion_fetch_k)


            if not combined_chunks_with_content:
                 exec_log.warning("No chunks with content available after fusion/fetch for RAG.")
                 # Proceed to LLM without documents
                 final_prompt = await self._build_prompt(query, [], chat_history=chat_history_str)
                 answer = await self.llm.generate(final_prompt)
                 await self.chat_repo.save_message(chat_id=final_chat_id, role='assistant', content=answer, sources=None)
                 try: # Best effort logging
                    log_id = await self.log_repo.log_query_interaction(company_id=company_id, user_id=user_id, query=query, answer=answer, retrieved_documents_data=[], chat_id=final_chat_id, metadata={"pipeline_stages": pipeline_stages_used, "result": "no_docs_found_after_fusion"})
                 except Exception as log_exc_no_docs: exec_log.error("Failed to log interaction for no_docs_after_fusion case", error=str(log_exc_no_docs))
                 return answer, [], log_id, final_chat_id

            chunks_to_process_further = combined_chunks_with_content

            # --- Remote Reranking Step ---
            if self.settings.RERANKER_ENABLED:
                rerank_log = exec_log.bind(action="remote_rerank")
                rerank_log.debug(f"Performing remote reranking with {self.settings.RERANKER_SERVICE_URL}...", count=len(chunks_to_process_further))
                
                # Prepare payload for reranker service
                documents_to_rerank_payload = []
                for doc_chunk in chunks_to_process_further:
                    if doc_chunk.content and doc_chunk.id: # Ensure content and ID exist
                        documents_to_rerank_payload.append(
                            {"id": doc_chunk.id, "text": doc_chunk.content, "metadata": doc_chunk.metadata or {}}
                        )
                
                if documents_to_rerank_payload:
                    reranker_request_payload = {
                        "query": query,
                        "documents": documents_to_rerank_payload,
                        "top_n": self.settings.MAX_CONTEXT_CHUNKS + 5 # Ask reranker for slightly more than needed for LLM
                    }
                    try:
                        response = await self.http_client.post(
                            str(self.settings.RERANKER_SERVICE_URL).rstrip('/') + "/api/v1/rerank",
                            json=reranker_request_payload,
                            timeout=self.settings.RERANKER_CLIENT_TIMEOUT
                        )
                        response.raise_for_status() # Raise HTTPStatusError for 4xx/5xx
                        
                        reranker_response_json = response.json()
                        # The reranker-service wraps its response in a "data" key
                        api_response_data = reranker_response_json.get("data", {})
                        reranked_docs_data = api_response_data.get("reranked_documents", [])
                        
                        # Map response back to domain.RetrievedChunk, preserving original embeddings if any
                        original_chunks_map = {chunk.id: chunk for chunk in chunks_to_process_further}
                        reranked_domain_chunks = []
                        for reranked_doc_item in reranked_docs_data:
                            original_chunk = original_chunks_map.get(reranked_doc_item["id"])
                            if original_chunk:
                                # Create new RetrievedChunk instances with updated scores
                                updated_chunk = RetrievedChunk(
                                    id=original_chunk.id,
                                    content=original_chunk.content, # Use original fetched content
                                    score=reranked_doc_item["score"], # New score from reranker
                                    metadata=reranked_doc_item.get("metadata", original_chunk.metadata), # Prefer reranker's metadata if returned, else original
                                    embedding=original_chunk.embedding, # Preserve original embedding
                                    document_id=original_chunk.document_id,
                                    file_name=original_chunk.file_name,
                                    company_id=original_chunk.company_id
                                )
                                reranked_domain_chunks.append(updated_chunk)
                        
                        chunks_to_process_further = reranked_domain_chunks
                        model_name_from_reranker = api_response_data.get("model_info",{}).get("model_name","unknown_remote")
                        pipeline_stages_used.append(f"reranking (remote:{model_name_from_reranker})")
                        rerank_log.info("Remote reranking successful.", count=len(chunks_to_process_further), model_used=model_name_from_reranker)

                    except httpx.HTTPStatusError as http_err:
                        rerank_log.error("Reranker service request failed (HTTPStatusError)", status_code=http_err.response.status_code, response_text=http_err.response.text, exc_info=False)
                        pipeline_stages_used.append("reranking (remote_http_error)")
                        # Optionally, could raise ConnectionError here or proceed with non-reranked chunks
                    except httpx.RequestError as req_err:
                        rerank_log.error("Reranker service request failed (RequestError)", error=str(req_err), exc_info=False)
                        pipeline_stages_used.append("reranking (remote_request_error)")
                        # Raise ConnectionError to signal service unavailability
                        raise ConnectionError(f"Reranker service connection error: {req_err}") from req_err
                    except Exception as e_rerank: # Catch other errors like JSONDecodeError
                        rerank_log.error("Error processing reranker service response", error_message=str(e_rerank), exc_info=True)
                        pipeline_stages_used.append("reranking (remote_client_error)")
                else:
                    rerank_log.warning("No documents with content to send to reranker service.")
                    pipeline_stages_used.append("reranking (skipped_no_content_for_remote)")
            else:
                 pipeline_stages_used.append("reranking (disabled_in_settings)")


            final_chunks_for_llm = chunks_to_process_further
            if self.diversity_filter:
                 k_final_diversity = self.settings.MAX_CONTEXT_CHUNKS
                 filter_type = type(self.diversity_filter).__name__
                 pipeline_stages_used.append(f"diversity_filter ({filter_type})")
                 exec_log.debug(f"Applying {filter_type} k={k_final_diversity}...", count=len(chunks_to_process_further))
                 final_chunks_for_llm = await self.diversity_filter.filter(chunks_to_process_further, k_final_diversity)
                 exec_log.info(f"{filter_type} applied.", final_count=len(final_chunks_for_llm))
            else: # If diversity filter is not enabled, just truncate
                 final_chunks_for_llm = chunks_to_process_further[:self.settings.MAX_CONTEXT_CHUNKS]
                 exec_log.info(f"Diversity filter disabled. Truncating to MAX_CONTEXT_CHUNKS.", final_count=len(final_chunks_for_llm), limit=self.settings.MAX_CONTEXT_CHUNKS)

            # Ensure chunks passed to LLM have content
            final_chunks_for_llm = [c for c in final_chunks_for_llm if c.content and c.content.strip()]
            if not final_chunks_for_llm:
                 exec_log.warning("No chunks with content remaining after final limiting/filtering for RAG.")
                 final_prompt = await self._build_prompt(query, [], chat_history=chat_history_str)
                 answer = await self.llm.generate(final_prompt)
                 await self.chat_repo.save_message(chat_id=final_chat_id, role='assistant', content=answer, sources=None)
                 try:
                    log_id = await self.log_repo.log_query_interaction(company_id=company_id, user_id=user_id, query=query, answer=answer, retrieved_documents_data=[], chat_id=final_chat_id, metadata={"pipeline_stages": pipeline_stages_used, "result": "no_docs_after_final_processing"})
                 except Exception as log_exc_final_no_docs: exec_log.error("Failed to log interaction for no_docs_after_final_processing case", error=str(log_exc_final_no_docs))
                 return answer, [], log_id, final_chat_id

            haystack_docs_for_prompt = [
                Document(id=c.id, content=c.content, meta=c.metadata, score=c.score)
                for c in final_chunks_for_llm
            ]
            exec_log.info(f"Building RAG prompt with {len(haystack_docs_for_prompt)} final chunks and history.")
            final_prompt = await self._build_prompt(query, haystack_docs_for_prompt, chat_history=chat_history_str)

            answer = await self.llm.generate(final_prompt)
            exec_log.info("LLM answer generated.", length=len(answer))

            sources_to_show_count = self.settings.NUM_SOURCES_TO_SHOW
            assistant_sources = [
                {"chunk_id": c.id, "document_id": c.document_id, "file_name": c.file_name, "score": c.score, "preview": truncate_text(c.content, 150) if c.content else "Error: Contenido no disponible"}
                for c in final_chunks_for_llm[:sources_to_show_count]
            ]
            await self.chat_repo.save_message(chat_id=final_chat_id, role='assistant', content=answer, sources=assistant_sources or None)
            exec_log.info(f"Assistant message saved with top {len(assistant_sources)} sources.")

            try:
                docs_for_log_dump = [RetrievedDocumentSchema(**c.model_dump(exclude_none=True)).model_dump(exclude_none=True) for c in final_chunks_for_llm[:sources_to_show_count]]
                log_metadata_details = {
                    "pipeline_stages": pipeline_stages_used,
                    "retriever_k_initial": retriever_k,
                    "fusion_fetch_k": fusion_fetch_k,
                    "max_context_chunks_limit_for_llm": self.settings.MAX_CONTEXT_CHUNKS,
                    "num_chunks_after_rerank_or_fusion_content_fetch": len(chunks_to_process_further),
                    "num_final_chunks_sent_to_llm": len(final_chunks_for_llm),
                    "num_sources_shown_to_user": len(assistant_sources),
                    "chat_history_messages_included_in_prompt": len(history_messages),
                    "diversity_filter_enabled_in_settings": self.settings.DIVERSITY_FILTER_ENABLED,
                    "reranker_enabled_in_settings": self.settings.RERANKER_ENABLED,
                    "bm25_enabled_in_settings": self.settings.BM25_ENABLED,
                 }
                log_id = await self.log_repo.log_query_interaction(
                    company_id=company_id, user_id=user_id, query=query, answer=answer,
                    retrieved_documents_data=docs_for_log_dump, chat_id=final_chat_id, metadata=log_metadata_details
                )
                exec_log.info("Interaction logged successfully", db_log_id=str(log_id))
            except Exception as log_err_final:
                exec_log.error("Failed to log RAG interaction", error=str(log_err_final), exc_info=False)

            exec_log.info("Use case execution finished successfully.")
            return answer, final_chunks_for_llm[:sources_to_show_count], log_id, final_chat_id

        except ConnectionError as ce: # Catch specific ConnectionErrors from sub-components
            exec_log.error("Connection error during use case execution", error=str(ce), exc_info=True)
            detail_message = "A required external service is unavailable. Please try again later."
            # More specific messages based on error content
            if "Embedding service" in str(ce) or "embedding service" in str(ce).lower():
                detail_message = "The embedding service is currently unavailable. Please try again later."
            elif "Reranker service" in str(ce) or "reranker service" in str(ce).lower():
                detail_message = "The reranking service is currently unavailable. Please try again later."
            elif "Gemini API" in str(ce) or "gemini" in str(ce).lower():
                detail_message = "The language model service (Gemini) is currently unavailable. Please try again later."
            elif "Vector DB" in str(ce) or "Milvus" in str(ce).lower():
                detail_message = "The vector database service is currently unavailable. Please try again later."
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail_message) from ce
        except ValueError as ve: # Catch specific ValueErrors (e.g., from prompt building, bad embedding data)
            exec_log.error("Value error during use case execution", error=str(ve), exc_info=True) # Log with traceback for ValueError
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Data processing error: {ve}") from ve
        except HTTPException as http_exc: # Re-raise HTTPExceptions from chat ownership check etc.
            raise http_exc
        except Exception as e: # Catch-all for any other unhandled error
            exec_log.exception("Unexpected error during use case execution") # Log with full traceback
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"An internal error occurred: {type(e).__name__}") from e
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
MILVUS_DEFAULT_COLLECTION = "document_chunks_minilm"
MILVUS_DEFAULT_EMBEDDING_FIELD = "embedding"
MILVUS_DEFAULT_CONTENT_FIELD = "content"
MILVUS_DEFAULT_COMPANY_ID_FIELD = "company_id"
MILVUS_DEFAULT_DOCUMENT_ID_FIELD = "document_id"
MILVUS_DEFAULT_FILENAME_FIELD = "file_name"
MILVUS_DEFAULT_GRPC_TIMEOUT = 15
MILVUS_DEFAULT_SEARCH_PARAMS = {"metric_type": "IP", "params": {"nprobe": 10}}
MILVUS_DEFAULT_METADATA_FIELDS = ["company_id", "document_id", "file_name", "page", "title"]

# Embedding Service
EMBEDDING_SERVICE_K8S_URL_DEFAULT = "http://embedding-service.nyro-develop.svc.cluster.local:8003"
# Reranker Service
RERANKER_SERVICE_K8S_URL_DEFAULT = "http://reranker-service.nyro-develop.svc.cluster.local:80"


# ==========================================================================================
#  ATENEX SYSTEM PROMPT ADAPTADO - v1.3 (Mayo 2025)
# ==========================================================================================
# Adaptado del System Prompt detallado, enfocado en instrucciones para el LLM en tiempo de ejecución.

ATENEX_RAG_PROMPT_TEMPLATE = r"""
════════════════════════════════════════════════════════════════════
A T E N E X · INSTRUCCIONES DE GENERACIÓN (Gemini 2.5 Flash)
════════════════════════════════════════════════════════════════════

1 · IDENTIDAD Y TONO
Eres **Atenex**, un asistente de IA experto en consulta de documentación empresarial (PDFs, manuales, etc.). Eres profesional, directo, verificable y empático. Escribe en **español latino** claro y conciso. Prioriza la precisión y la seguridad.

2 · PRINCIPIOS CLAVE
   - **BASATE SOLO EN EL CONTEXTO:** Usa *únicamente* la información del HISTORIAL RECIENTE y los DOCUMENTOS RECUPERADOS a continuación. **No inventes**, especules ni uses conocimiento externo. Si la respuesta no está explícitamente en el contexto, indícalo claramente.
   - **CITACIÓN:** Cuando uses información de un documento recuperado, añade una cita al final de la frase o párrafo relevante indicando el documento y página. Formato preferido: `[Doc N]`. El formato detallado de la fuente se listará al final.
   - **NO ESPECULACIÓN:** Si la información solicitada no se encuentra, responde claramente: "No encontré información específica sobre eso en los documentos proporcionados ni en el historial reciente." y, si es posible, sugiere cómo el usuario podría encontrarla (ej. probar otros términos, revisar un documento específico si parece muy relevante).
   - **NO CÓDIGO:** No generes bloques de código ejecutable. Puedes explicar conceptos o pseudocódigo si es útil.
   - **PREGUNTAS AMPLIAS:** Si la pregunta es muy general (ej. "dame todo sobre X"), en lugar de intentar resumir todo, identifica 1-3 temas clave cubiertos en los documentos y formula preguntas aclaratorias al usuario para enfocar la consulta. Ejemplo: "Los documentos mencionan X en relación a Y y Z. ¿Te interesa algún aspecto en particular?".
   - **PETICIONES DE ARCHIVOS:** Si el usuario pide explícitamente "el PDF" o "el documento" y varios chunks recuperados pertenecen al mismo archivo (identificable por `doc.meta.file_name`), tu respuesta debe indicar que tienes información *proveniente* de ese archivo específico, resumir lo más relevante según la consulta, y citarlo. **No puedes entregar el archivo binario**, solo responder basado en su contenido textual.

3 · CONTEXTO PROPORCIONADO
{% if chat_history %}
───────────────────── HISTORIAL RECIENTE (Más antiguo a más nuevo) ─────────────────────
{{ chat_history }}
────────────────────────────────────────────────────────────────────────────────────────
{% endif %}
──────────────────── DOCUMENTOS RECUPERADOS (Ordenados por relevancia) ──────────────────
{% for doc in documents %}
[Doc {{ loop.index }}] «{{ doc.meta.file_name | default("Archivo Desconocido") }}»
 Título : {{ doc.meta.title | default("Sin Título") }}
 Página : {{ doc.meta.page | default("?") }}
 Score  : {{ "%.3f"|format(doc.score) if doc.score is not none else "N/A" }}
 Extracto: {{ doc.content | trim }}
--------------------------------------------------------------------------------------------{% endfor %}
────────────────────────────────────────────────────────────────────────────────────────

4 · PREGUNTA ACTUAL DEL USUARIO
{{ query }}

5 · FORMATO DE RESPUESTA REQUERIDO
   - **Respuesta Principal:** Escribe una respuesta natural, fluida y conversacional directamente abordando la pregunta del usuario. Si la respuesta es extensa (más de ~160 palabras), puedes iniciar con un breve resumen ejecutivo (1-2 frases). Integra la información clave y las citas `[Doc N]` donde corresponda. Evita usar explícitamente etiquetas como "Respuesta directa:" o "Siguiente acción sugerida:". Concluye la respuesta sugiriendo un próximo paso lógico o una pregunta relacionada si tiene sentido para continuar la conversación productivamente.
   - **Sección de Fuentes:** Obligatoriamente después de tu respuesta principal, añade una sección titulada `**Fuentes:**`. Lista aquí *solo* los documentos que **efectivamente utilizaste** para construir tu respuesta (basándote en las citas `[Doc N]` que incluiste). Usa una lista numerada ordenada por relevancia (el Doc 1 suele ser el más relevante). Formato:
     `1. «Nombre Archivo» (Título: <Título si existe>, Pág: <Página>)`
     `2. «Otro Archivo» (Pág: <Página>)`
     ... (No incluyas documentos que no citaste en la respuesta principal).

════════════════════════════════════════════════════════════════════
RESPUESTA DE ATENEX (en español latino):
════════════════════════════════════════════════════════════════════
"""

# ------------------------------------------------------------------------------

ATENEX_GENERAL_PROMPT_TEMPLATE = r"""
Eres **Atenex**, el Gestor de Conocimiento Empresarial. Responde de forma útil, concisa y profesional en español latino.

{% if chat_history %}
───────────────────── HISTORIAL RECIENTE (Más antiguo a más nuevo) ─────────────────────
{{ chat_history }}
────────────────────────────────────────────────────────────────────────────────────────
{% endif %}

PREGUNTA ACTUAL DEL USUARIO:
{{ query }}

INSTRUCCIONES:
- Basándote únicamente en el HISTORIAL RECIENTE (si existe) y tu conocimiento general como asistente, responde la pregunta.
- Si la consulta requiere información específica de documentos que no te han sido proporcionados en esta interacción (porque no se activó el RAG), indica amablemente que no tienes acceso a documentos externos para responder y sugiere al usuario subir un documento relevante o precisar su pregunta si busca información documental.
- NO inventes información que no tengas.

RESPUESTA DE ATENEX (en español latino):
"""
# Models
DEFAULT_EMBEDDING_DIMENSION = 384
DEFAULT_GEMINI_MODEL = "gemini-1.5-flash-latest"

# RAG Pipeline Parameters
DEFAULT_RETRIEVER_TOP_K = 100
DEFAULT_BM25_ENABLED: bool = True
DEFAULT_RERANKER_ENABLED: bool = True
DEFAULT_DIVERSITY_FILTER_ENABLED: bool = False
DEFAULT_MAX_CONTEXT_CHUNKS: int = 75
DEFAULT_HYBRID_ALPHA: float = 0.5
DEFAULT_DIVERSITY_LAMBDA: float = 0.5
DEFAULT_MAX_PROMPT_TOKENS: int = 500000
DEFAULT_MAX_CHAT_HISTORY_MESSAGES = 10
DEFAULT_NUM_SOURCES_TO_SHOW = 7


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
    MILVUS_METADATA_FIELDS: List[str] = Field(default=MILVUS_DEFAULT_METADATA_FIELDS)
    MILVUS_GRPC_TIMEOUT: int = Field(default=MILVUS_DEFAULT_GRPC_TIMEOUT)
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default=MILVUS_DEFAULT_SEARCH_PARAMS)

    # --- Embedding Settings (General) ---
    EMBEDDING_DIMENSION: int = Field(default=DEFAULT_EMBEDDING_DIMENSION, description="Dimension of embeddings, used for Milvus and validation.")

    # --- External Embedding Service ---
    EMBEDDING_SERVICE_URL: AnyHttpUrl = Field(default=AnyHttpUrl(EMBEDDING_SERVICE_K8S_URL_DEFAULT), description="URL of the Atenex Embedding Service.")
    EMBEDDING_CLIENT_TIMEOUT: int = Field(default=30, description="Timeout in seconds for calls to the Embedding Service.")


    # --- LLM (Google Gemini) ---
    GEMINI_API_KEY: SecretStr
    GEMINI_MODEL_NAME: str = Field(default=DEFAULT_GEMINI_MODEL)

    # --- Reranker Settings ---
    RERANKER_ENABLED: bool = Field(default=DEFAULT_RERANKER_ENABLED)
    RERANKER_SERVICE_URL: AnyHttpUrl = Field(default=AnyHttpUrl(RERANKER_SERVICE_K8S_URL_DEFAULT), description="URL of the Atenex Reranker Service.")
    RERANKER_CLIENT_TIMEOUT: int = Field(default=30, description="Timeout in seconds for calls to the Reranker Service.")


    # --- Sparse Retriever (BM25) ---
    BM25_ENABLED: bool = Field(default=DEFAULT_BM25_ENABLED)

    # --- Diversity Filter ---
    DIVERSITY_FILTER_ENABLED: bool = Field(default=DEFAULT_DIVERSITY_FILTER_ENABLED)
    MAX_CONTEXT_CHUNKS: int = Field(default=DEFAULT_MAX_CONTEXT_CHUNKS, gt=0, description="Max number of retrieved/reranked chunks to pass to LLM context.")
    QUERY_DIVERSITY_LAMBDA: float = Field(default=DEFAULT_DIVERSITY_LAMBDA, ge=0.0, le=1.0, description="Lambda for MMR diversity (0=max diversity, 1=max relevance).")

    # --- RAG Pipeline Parameters ---
    RETRIEVER_TOP_K: int = Field(default=DEFAULT_RETRIEVER_TOP_K, gt=0, le=500)
    HYBRID_FUSION_ALPHA: float = Field(default=DEFAULT_HYBRID_ALPHA, ge=0.0, le=1.0)
    RAG_PROMPT_TEMPLATE: str = Field(default=ATENEX_RAG_PROMPT_TEMPLATE)
    GENERAL_PROMPT_TEMPLATE: str = Field(default=ATENEX_GENERAL_PROMPT_TEMPLATE)
    MAX_PROMPT_TOKENS: Optional[int] = Field(default=DEFAULT_MAX_PROMPT_TOKENS)
    MAX_CHAT_HISTORY_MESSAGES: int = Field(default=DEFAULT_MAX_CHAT_HISTORY_MESSAGES, ge=0)
    NUM_SOURCES_TO_SHOW: int = Field(default=DEFAULT_NUM_SOURCES_TO_SHOW, ge=0)

    # --- Service Client Config ---
    HTTP_CLIENT_TIMEOUT: int = Field(default=60) # General timeout for httpx client
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
        if v is None or (isinstance(v, str) and v == ""):
            field_name = info.field_name if info.field_name else "Unknown Secret Field"
            raise ValueError(f"Required secret field 'QUERY_{field_name.upper()}' cannot be empty.")
        return v

    @field_validator('EMBEDDING_DIMENSION')
    @classmethod
    def check_embedding_dimension(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0:
            raise ValueError("EMBEDDING_DIMENSION must be a positive integer.")
        logging.info(f"Configured EMBEDDING_DIMENSION: {v}. This will be used for Milvus and validated against the embedding service.")
        return v

    @field_validator('MAX_CONTEXT_CHUNKS')
    @classmethod
    def check_max_context_chunks(cls, v: int, info: ValidationInfo) -> int:
        retriever_k = info.data.get('RETRIEVER_TOP_K', DEFAULT_RETRIEVER_TOP_K)
        max_possible_after_fusion = retriever_k * 2
        if v > max_possible_after_fusion:
            logging.warning(f"MAX_CONTEXT_CHUNKS ({v}) is greater than the maximum possible chunks after fusion ({max_possible_after_fusion} based on RETRIEVER_TOP_K={retriever_k}). Effective limit will be {max_possible_after_fusion}.")
        if v <= 0:
             raise ValueError("MAX_CONTEXT_CHUNKS must be a positive integer.")
        return v

    @field_validator('NUM_SOURCES_TO_SHOW')
    @classmethod
    def check_num_sources_to_show(cls, v: int, info: ValidationInfo) -> int:
        max_chunks = info.data.get('MAX_CONTEXT_CHUNKS', DEFAULT_MAX_CONTEXT_CHUNKS)
        if v > max_chunks:
             logging.warning(f"NUM_SOURCES_TO_SHOW ({v}) is greater than MAX_CONTEXT_CHUNKS ({max_chunks}). Will only show up to {max_chunks} sources.")
             return max_chunks
        if v < 0:
            raise ValueError("NUM_SOURCES_TO_SHOW cannot be negative.")
        return v


# --- Global Settings Instance ---
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
    log_data = settings.model_dump(exclude={'POSTGRES_PASSWORD', 'GEMINI_API_KEY'})
    for key, value in log_data.items():
        if key.endswith('_PROMPT_TEMPLATE') and isinstance(value, str) and len(value) > 200:
             temp_log.info(f"  {key.upper()}: {value[:100]}... (truncated)")
        else:
             temp_log.info(f"  {key.upper()}: {value}")
    temp_log.info(f"  POSTGRES_PASSWORD: *** SET ***")
    temp_log.info(f"  GEMINI_API_KEY: *** SET ***")

except (ValidationError, ValueError) as e:
    error_details = ""
    if isinstance(e, ValidationError):
        try: error_details = f"\nValidation Errors:\n{json.dumps(e.errors(), indent=2)}"
        except Exception: error_details = f"\nRaw Errors: {e}"
    else: error_details = f"\nError: {e}"
    temp_log.critical(f"!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    temp_log.critical(f"! FATAL: Query Service configuration validation failed!{error_details}")
    temp_log.critical(f"! Check environment variables (prefixed with QUERY_) or .env file.")
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

## File: `app\dependencies.py`
```py
# query-service/app/dependencies.py
"""
Centralized dependency functions to avoid circular imports.
"""
from fastapi import HTTPException
from app.application.use_cases.ask_query_use_case import AskQueryUseCase

# These will be set by main.py at startup
ask_query_use_case_instance = None
SERVICE_READY = False

def set_ask_query_use_case_instance(instance, ready_flag):
    global ask_query_use_case_instance, SERVICE_READY
    ask_query_use_case_instance = instance
    SERVICE_READY = ready_flag

def get_ask_query_use_case() -> AskQueryUseCase:
    if not SERVICE_READY or not ask_query_use_case_instance:
        raise HTTPException(status_code=503, detail="Query processing service is not ready. Check startup logs.")
    return ask_query_use_case_instance

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
    # --- Añadir campo para embedding ---
    embedding: Optional[List[float]] = None # Embedding vectorial del chunk
    # --- Fin adición ---
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

        # Extraer embedding si existe en el documento Haystack
        embedding_vector = getattr(doc, 'embedding', None)

        return cls(
            id=str(doc.id),
            content=doc.content,
            score=doc.score,
            metadata=doc_meta,
            embedding=embedding_vector, # Añadir embedding
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

## File: `app\infrastructure\clients\__init__.py`
```py
# query-service/app/infrastructure/clients/__init__.py
from .embedding_service_client import EmbeddingServiceClient

__all__ = ["EmbeddingServiceClient"]
```

## File: `app\infrastructure\clients\embedding_service_client.py`
```py
# query-service/app/infrastructure/clients/embedding_service_client.py
import httpx
import structlog
from typing import List, Dict, Any, Optional
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.core.config import settings

log = structlog.get_logger(__name__)

class EmbeddingServiceClient:
    """
    Cliente HTTP para interactuar con el Atenex Embedding Service.
    """
    def __init__(self, base_url: str, timeout: int = settings.HTTP_CLIENT_TIMEOUT):
        self.base_url = base_url.rstrip('/')
        self.embed_endpoint = f"{self.base_url}/api/v1/embed" # Asumiendo que la URL base ya tiene /api/v1
        self.health_endpoint = f"{self.base_url}/health"

        # Ajuste para no incluir /api/v1 dos veces si ya está en base_url
        if "/api/v1" in self.base_url.split('/')[-2:]: # si base_url es .../api/v1
             self.embed_endpoint = f"{self.base_url}/embed"
        else: # si base_url es ...embedding-service:8003
             self.embed_endpoint = f"{self.base_url}/api/v1/embed"


        self._client = httpx.AsyncClient(timeout=timeout)
        log.info("EmbeddingServiceClient initialized", base_url=self.base_url, embed_endpoint=self.embed_endpoint)

    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES + 1),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=1, max=10),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError)),
        reraise=True # Re-raise la excepción original después de los reintentos
    )
    async def generate_embeddings(self, texts: List[str]) -> List[List[float]]:
        """
        Solicita embeddings para una lista de textos al servicio de embedding.
        """
        client_log = log.bind(action="generate_embeddings", num_texts=len(texts), target_service="embedding-service")
        if not texts:
            client_log.warning("No texts provided to generate_embeddings.")
            return []

        payload = {"texts": texts}
        try:
            client_log.debug("Sending request to embedding service")
            response = await self._client.post(self.embed_endpoint, json=payload)
            response.raise_for_status() # Lanza HTTPStatusError para 4xx/5xx

            data = response.json()
            if "embeddings" not in data or not isinstance(data["embeddings"], list):
                client_log.error("Invalid response format from embedding service: 'embeddings' field missing or not a list.", response_data=data)
                raise ValueError("Invalid response format from embedding service: 'embeddings' field.")

            # Opcional: Validar que cada embedding sea una lista de floats y tenga la dimensión esperada
            # Esto se podría hacer en el adaptador.
            client_log.info("Embeddings received successfully from service", num_embeddings=len(data["embeddings"]))
            return data["embeddings"]

        except httpx.HTTPStatusError as e:
            client_log.error("HTTP error from embedding service", status_code=e.response.status_code, response_body=e.response.text)
            raise ConnectionError(f"Embedding service returned error {e.response.status_code}: {e.response.text}") from e
        except httpx.RequestError as e:
            client_log.error("Request error while contacting embedding service", error=str(e))
            raise ConnectionError(f"Could not connect to embedding service: {e}") from e
        except (ValueError, TypeError) as e: # Errores de parsing JSON o validación
            client_log.error("Error processing response from embedding service", error=str(e))
            raise ValueError(f"Invalid response from embedding service: {e}") from e


    async def get_model_info(self) -> Optional[Dict[str, Any]]:
        """
        Intenta obtener información del modelo desde la respuesta del endpoint /embed.
        Nota: El embedding-service actual no tiene un endpoint /info,
        pero el /embed response incluye model_info. Esta función es una forma
        de inferirlo si se necesita, aunque es mejor tener un health check más completo.
        """
        client_log = log.bind(action="get_model_info_via_embed", target_service="embedding-service")
        try:
            # Enviamos un texto de prueba para obtener la respuesta que incluye model_info
            response = await self._client.post(self.embed_endpoint, json={"texts": ["test"]})
            response.raise_for_status()
            data = response.json()
            if "model_info" in data and isinstance(data["model_info"], dict):
                client_log.info("Model info retrieved from embedding service", model_info=data["model_info"])
                return data["model_info"]
            client_log.warning("Model info not found in embedding service response.", response_data=data)
            return None
        except Exception as e:
            client_log.error("Failed to get model_info from embedding service via /embed", error=str(e))
            return None

    async def check_health(self) -> bool:
        """
        Verifica la salud del embedding service llamando a su endpoint /health.
        """
        client_log = log.bind(action="check_health", target_service="embedding-service")
        try:
            response = await self._client.get(self.health_endpoint)
            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "ok" and data.get("model_status") == "loaded":
                    client_log.info("Embedding service health check successful.", health_data=data)
                    return True
                else:
                    client_log.warning("Embedding service health check returned ok status but model not ready.", health_data=data)
                    return False
            else:
                client_log.warning("Embedding service health check failed.", status_code=response.status_code, response_text=response.text)
                return False
        except httpx.RequestError as e:
            client_log.error("Error connecting to embedding service for health check.", error=str(e))
            return False
        except Exception as e: # Para errores de JSON parsing u otros
            client_log.error("Unexpected error during embedding service health check.", error=str(e))
            return False

    async def close(self):
        """Cierra el cliente HTTP."""
        await self._client.aclose()
        log.info("EmbeddingServiceClient closed.")
```

## File: `app\infrastructure\embedding\__init__.py`
```py
# query-service/app/infrastructure/embedding/__init__.py
from .remote_embedding_adapter import RemoteEmbeddingAdapter

__all__ = ["RemoteEmbeddingAdapter"]
```

## File: `app\infrastructure\embedding\remote_embedding_adapter.py`
```py
# query-service/app/infrastructure/embedding/remote_embedding_adapter.py
import structlog
from typing import List, Optional

from app.application.ports.embedding_port import EmbeddingPort
from app.infrastructure.clients.embedding_service_client import EmbeddingServiceClient
from app.core.config import settings # Para EMBEDDING_DIMENSION

log = structlog.get_logger(__name__)

class RemoteEmbeddingAdapter(EmbeddingPort):
    """
    Adaptador que utiliza EmbeddingServiceClient para generar embeddings
    llamando al servicio de embedding externo.
    """
    def __init__(self, client: EmbeddingServiceClient):
        self.client = client
        self._embedding_dimension: Optional[int] = None # Se intentará obtener del servicio
        self._expected_dimension = settings.EMBEDDING_DIMENSION # Dimensión configurada/esperada
        log.info("RemoteEmbeddingAdapter initialized", expected_dimension=self._expected_dimension)

    async def initialize(self):
        """
        Intenta obtener la dimensión del embedding desde el servicio al iniciar.
        """
        init_log = log.bind(adapter="RemoteEmbeddingAdapter", action="initialize")
        try:
            model_info = await self.client.get_model_info()
            if model_info and "dimension" in model_info:
                self._embedding_dimension = model_info["dimension"]
                init_log.info("Successfully retrieved embedding dimension from service.",
                              service_dimension=self._embedding_dimension,
                              service_model_name=model_info.get("model_name"))
                if self._embedding_dimension != self._expected_dimension:
                    init_log.warning("Embedding dimension mismatch!",
                                     configured_dimension=self._expected_dimension,
                                     service_dimension=self._embedding_dimension,
                                     message="Query service configured dimension does not match dimension reported by embedding service. "
                                             "This may cause issues with Milvus or other components. Ensure configurations are aligned.")
            else:
                init_log.warning("Could not retrieve embedding dimension from service. Will use configured dimension.",
                                 configured_dimension=self._expected_dimension)
        except Exception as e:
            init_log.error("Failed to retrieve embedding dimension during initialization.", error=str(e))

    async def embed_query(self, query_text: str) -> List[float]:
        adapter_log = log.bind(adapter="RemoteEmbeddingAdapter", action="embed_query")
        if not query_text:
            adapter_log.warning("Empty query text provided.")
            raise ValueError("Query text cannot be empty.")
        try:
            embeddings = await self.client.generate_embeddings([query_text])
            if not embeddings or len(embeddings) != 1:
                adapter_log.error("Embedding service did not return a valid embedding for the query.", received_embeddings=embeddings)
                raise ValueError("Failed to get a valid embedding for the query.")

            embedding_vector = embeddings[0]
            # Validar dimensión
            if len(embedding_vector) != self.get_embedding_dimension(): # Usa el getter que prioriza servicio
                adapter_log.error("Embedding dimension mismatch for query embedding.",
                                  expected_dim=self.get_embedding_dimension(),
                                  received_dim=len(embedding_vector))
                raise ValueError(f"Embedding dimension mismatch: expected {self.get_embedding_dimension()}, got {len(embedding_vector)}")

            adapter_log.debug("Query embedded successfully via remote service.")
            return embedding_vector
        except ConnectionError as e:
            adapter_log.error("Connection error while embedding query.", error=str(e))
            raise # Re-raise para que el use case lo maneje
        except ValueError as e:
            adapter_log.error("Value error while embedding query.", error=str(e))
            raise # Re-raise

    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        adapter_log = log.bind(adapter="RemoteEmbeddingAdapter", action="embed_texts")
        if not texts:
            adapter_log.warning("No texts provided to embed_texts.")
            return []
        try:
            embeddings = await self.client.generate_embeddings(texts)
            if len(embeddings) != len(texts):
                adapter_log.error("Number of embeddings received does not match number of texts sent.",
                                  num_texts=len(texts), num_embeddings=len(embeddings))
                raise ValueError("Mismatch in number of embeddings received from service.")

            # Validar dimensión del primer embedding como muestra
            if embeddings and len(embeddings[0]) != self.get_embedding_dimension():
                 adapter_log.error("Embedding dimension mismatch for batch texts.",
                                   expected_dim=self.get_embedding_dimension(),
                                   received_dim=len(embeddings[0]))
                 raise ValueError(f"Embedding dimension mismatch: expected {self.get_embedding_dimension()}, got {len(embeddings[0])}")

            adapter_log.debug(f"Successfully embedded {len(texts)} texts via remote service.")
            return embeddings
        except ConnectionError as e:
            adapter_log.error("Connection error while embedding texts.", error=str(e))
            raise
        except ValueError as e:
            adapter_log.error("Value error while embedding texts.", error=str(e))
            raise

    def get_embedding_dimension(self) -> int:
        """
        Devuelve la dimensión del embedding, priorizando la obtenida del servicio,
        o la configurada como fallback.
        """
        if self._embedding_dimension is not None:
            return self._embedding_dimension
        return self._expected_dimension

    async def health_check(self) -> bool:
        """
        Delega la verificación de salud al cliente del servicio de embedding.
        """
        return await self.client.check_health()
```

## File: `app\infrastructure\filters\__init__.py`
```py
# query-service/app/infrastructure/filters/__init__.py
```

## File: `app\infrastructure\filters\diversity_filter.py`
```py
# query-service/app/infrastructure/filters/diversity_filter.py
import structlog
import asyncio
from typing import List, Optional, Tuple
import numpy as np

from app.application.ports.retrieval_ports import DiversityFilterPort
from app.domain.models import RetrievedChunk
from app.core.config import settings

log = structlog.get_logger(__name__)

def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """Calcula la similitud coseno entre dos vectores."""
    if not vec1 or not vec2:
        return 0.0
    v1 = np.array(vec1)
    v2 = np.array(vec2)
    dot_product = np.dot(v1, v2)
    norm_v1 = np.linalg.norm(v1)
    norm_v2 = np.linalg.norm(v2)
    if norm_v1 == 0 or norm_v2 == 0:
        return 0.0
    return dot_product / (norm_v1 * norm_v2)

class MMRDiversityFilter(DiversityFilterPort):
    """
    Filtro de diversidad usando Maximal Marginal Relevance (MMR).
    Selecciona chunks que son relevantes para la consulta pero diversos entre sí.
    """

    def __init__(self, lambda_mult: float = settings.QUERY_DIVERSITY_LAMBDA):
        """
        Inicializa el filtro MMR.
        Args:
            lambda_mult: Factor de balance entre relevancia y diversidad (0 a 1).
                         Alto (e.g., 0.7) prioriza relevancia.
                         Bajo (e.g., 0.3) prioriza diversidad.
        """
        if not (0.0 <= lambda_mult <= 1.0):
            raise ValueError("lambda_mult must be between 0.0 and 1.0")
        self.lambda_mult = lambda_mult
        log.info("MMRDiversityFilter initialized", lambda_mult=self.lambda_mult, adapter="MMRDiversityFilter")

    async def filter(self, chunks: List[RetrievedChunk], k_final: int) -> List[RetrievedChunk]:
        """
        Aplica el filtro MMR a la lista de chunks.
        Requiere que los chunks tengan embeddings.
        """
        filter_log = log.bind(adapter="MMRDiversityFilter", action="filter", k_final=k_final, lambda_mult=self.lambda_mult, input_count=len(chunks))

        if not chunks or k_final <= 0:
            filter_log.debug("No chunks to filter or k_final <= 0.")
            return []

        # Filtrar chunks que no tengan embedding
        chunks_with_embeddings = [c for c in chunks if c.embedding is not None]
        if not chunks_with_embeddings:
            filter_log.warning("No chunks with embeddings found. Returning original top-k chunks (or fewer).")
            # Devuelve los primeros k_final chunks originales (aunque no tengan embedding)
            return chunks[:k_final]

        num_chunks_with_embeddings = len(chunks_with_embeddings)
        if k_final >= num_chunks_with_embeddings:
            filter_log.debug(f"k_final ({k_final}) >= number of chunks with embeddings ({num_chunks_with_embeddings}). Returning all chunks with embeddings.")
            return chunks_with_embeddings # Devolver todos los que tienen embedding si k es mayor o igual

        # El primer chunk seleccionado es siempre el más relevante (asume que la lista está ordenada por relevancia)
        selected_indices = {0}
        selected_chunks = [chunks_with_embeddings[0]]

        remaining_indices = set(range(1, num_chunks_with_embeddings))

        while len(selected_chunks) < k_final and remaining_indices:
            mmr_scores = {}
            # Calcular la similitud máxima de cada candidato con los ya seleccionados
            for candidate_idx in remaining_indices:
                candidate_chunk = chunks_with_embeddings[candidate_idx]
                max_similarity = 0.0
                for selected_idx in selected_indices:
                    similarity = cosine_similarity(candidate_chunk.embedding, chunks_with_embeddings[selected_idx].embedding)
                    max_similarity = max(max_similarity, similarity)

                # Calcular score MMR
                # Usamos el score original del chunk como medida de relevancia (podría ser similitud con query si la tuviéramos)
                relevance_score = candidate_chunk.score or 0.0 # Usar 0 si no hay score
                mmr_score = self.lambda_mult * relevance_score - (1 - self.lambda_mult) * max_similarity
                mmr_scores[candidate_idx] = mmr_score

            # Encontrar el mejor candidato según MMR
            if not mmr_scores: break # Salir si no hay más candidatos con score
            best_candidate_idx = max(mmr_scores, key=mmr_scores.get)

            # Añadir el mejor candidato y moverlo de conjuntos
            selected_indices.add(best_candidate_idx)
            selected_chunks.append(chunks_with_embeddings[best_candidate_idx])
            remaining_indices.remove(best_candidate_idx)

        filter_log.info(f"MMR filtering complete. Selected {len(selected_chunks)} diverse chunks.")
        return selected_chunks

class StubDiversityFilter(DiversityFilterPort):
    """Implementación Stub (Fallback si MMR falla o está deshabilitado)."""
    def __init__(self):
        log.warning("Using StubDiversityFilter. No diversity logic is applied.", adapter="StubDiversityFilter")

    async def filter(self, chunks: List[RetrievedChunk], k_final: int) -> List[RetrievedChunk]:
        filter_log = log.bind(adapter="StubDiversityFilter", action="filter", k_final=k_final, input_count=len(chunks))
        if not chunks:
            filter_log.debug("No chunks to filter.")
            return []
        filtered_chunks = chunks[:k_final]
        filter_log.debug(f"Returning top {len(filtered_chunks)} chunks without diversity filtering.")
        return filtered_chunks
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

# --- Import field name constants from ingest_pipeline for clarity (Read-Only) ---
# These constants represent the actual field names used in the Milvus collection schema
# defined by the ingest-service.
# This improves maintainability if field names change in the ingest service.
try:
    from app.services.ingest_pipeline import (
        MILVUS_PK_FIELD, # "pk_id"
        MILVUS_VECTOR_FIELD, # "embedding"
        MILVUS_CONTENT_FIELD, # "content"
        MILVUS_COMPANY_ID_FIELD, # "company_id"
        MILVUS_DOCUMENT_ID_FIELD, # "document_id"
        MILVUS_FILENAME_FIELD, # "file_name"
        MILVUS_PAGE_FIELD, # "page"
        MILVUS_TITLE_FIELD, # "title"
        # Add others if needed (tokens, content_hash)
    )
    INGEST_SCHEMA_FIELDS = {
        "pk": MILVUS_PK_FIELD,
        "vector": MILVUS_VECTOR_FIELD,
        "content": MILVUS_CONTENT_FIELD,
        "company": MILVUS_COMPANY_ID_FIELD,
        "document": MILVUS_DOCUMENT_ID_FIELD,
        "filename": MILVUS_FILENAME_FIELD,
        "page": MILVUS_PAGE_FIELD,
        "title": MILVUS_TITLE_FIELD,
    }
except ImportError:
     # Fallback to using settings directly if ingest schema isn't available
     # (less ideal but allows standalone use)
     structlog.getLogger(__name__).warning("Could not import ingest schema constants, using settings directly for field names.")
     INGEST_SCHEMA_FIELDS = {
        "pk": "pk_id", # Assuming default from ingest
        "vector": settings.MILVUS_EMBEDDING_FIELD,
        "content": settings.MILVUS_CONTENT_FIELD,
        "company": settings.MILVUS_COMPANY_ID_FIELD,
        "document": settings.MILVUS_DOCUMENT_ID_FIELD,
        "filename": settings.MILVUS_FILENAME_FIELD,
        "page": "page", # Add fallbacks for metadata
        "title": "title",
    }


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
            collection_name = settings.MILVUS_COLLECTION_NAME # Now uses the corrected default or ENV var
            collection_log = log.bind(adapter="MilvusAdapter", action="get_collection", collection=collection_name, alias=self._alias)
            collection_log.info(f"Attempting to access Milvus collection: '{collection_name}'") # Log the name being used
            try:
                if not utility.has_collection(collection_name, using=self._alias):
                    collection_log.error("Milvus collection does not exist.", target_collection=collection_name)
                    raise RuntimeError(f"Milvus collection '{collection_name}' not found. Ensure ingest-service has created it.")

                collection = Collection(name=collection_name, using=self._alias)
                collection_log.debug("Loading Milvus collection into memory...")
                collection.load()
                collection_log.info("Milvus collection loaded successfully.")
                self._collection = collection

            except MilvusException as e:
                collection_log.error("Failed to get or load Milvus collection", error_code=e.code, error_message=e.message)
                if "multiple indexes" in e.message.lower():
                    collection_log.critical("Potential 'Ambiguous Index' error encountered. Please check Milvus indices for this collection.")
                raise RuntimeError(f"Milvus collection access error (Code: {e.code}): {e.message}") from e
            except Exception as e:
                 collection_log.exception("Unexpected error accessing Milvus collection")
                 raise RuntimeError(f"Unexpected error accessing Milvus collection: {e}") from e

        if not isinstance(self._collection, Collection):
            log.critical("Milvus collection object is unexpectedly None or invalid type after initialization attempt.")
            raise RuntimeError("Failed to obtain a valid Milvus collection object.")

        return self._collection

    async def search(self, embedding: List[float], company_id: str, top_k: int) -> List[RetrievedChunk]:
        """Busca chunks relevantes usando pymilvus y los convierte al modelo de dominio."""
        search_log = log.bind(adapter="MilvusAdapter", action="search", company_id=company_id, top_k=top_k)
        try:
            collection = await self._get_collection()

            search_params = settings.MILVUS_SEARCH_PARAMS
            # --- Use consistent field name for filtering ---
            filter_expr = f'{INGEST_SCHEMA_FIELDS["company"]} == "{company_id}"'
            search_log.debug("Using filter expression", expr=filter_expr)

            # --- CORRECTION: Construct output_fields based on INGEST_SCHEMA_FIELDS and settings.MILVUS_METADATA_FIELDS ---
            # Start with mandatory fields used directly by the adapter/domain model
            required_output_fields = {
                INGEST_SCHEMA_FIELDS["pk"], # Need the PK to populate RetrievedChunk.id
                INGEST_SCHEMA_FIELDS["vector"], # Need the vector for diversity filter
                INGEST_SCHEMA_FIELDS["content"],
                INGEST_SCHEMA_FIELDS["company"],
                INGEST_SCHEMA_FIELDS["document"],
                INGEST_SCHEMA_FIELDS["filename"],
            }
            # Add fields specified in the query service's metadata list config
            # This ensures we fetch what the query service expects for its metadata dict
            required_output_fields.update(settings.MILVUS_METADATA_FIELDS)
            output_fields = list(required_output_fields)
            # --- END CORRECTION ---

            search_log.debug("Performing Milvus vector search...",
                             vector_field=INGEST_SCHEMA_FIELDS["vector"], # Use consistent vector field
                             output_fields=output_fields)

            loop = asyncio.get_running_loop()
            search_results = await loop.run_in_executor(
                None,
                lambda: collection.search(
                    data=[embedding],
                    anns_field=INGEST_SCHEMA_FIELDS["vector"], # Use consistent vector field
                    param=search_params,
                    limit=top_k,
                    expr=filter_expr,
                    output_fields=output_fields,
                    consistency_level="Strong"
                )
            )

            search_log.debug(f"Milvus search completed. Hits: {len(search_results[0]) if search_results else 0}")

            domain_chunks: List[RetrievedChunk] = []
            if search_results and search_results[0]:
                for hit in search_results[0]:
                    # --- CORRECTION: Use hit.entity if available, handle potential absence ---
                    entity_data = hit.entity.to_dict() if hasattr(hit, 'entity') and hasattr(hit.entity, 'to_dict') else {}

                    # Extract core fields using consistent names
                    pk_id = str(hit.id) # hit.id *should* be the primary key value
                    content = entity_data.get(INGEST_SCHEMA_FIELDS["content"], "")
                    embedding_vector = entity_data.get(INGEST_SCHEMA_FIELDS["vector"])

                    # Prepare metadata dict from all returned entity data, excluding vector
                    metadata = {k: v for k, v in entity_data.items() if k != INGEST_SCHEMA_FIELDS["vector"]}
                    # Ensure standard fields expected by domain model are present in metadata (using consistent keys)
                    doc_id = metadata.get(INGEST_SCHEMA_FIELDS["document"])
                    comp_id = metadata.get(INGEST_SCHEMA_FIELDS["company"])
                    fname = metadata.get(INGEST_SCHEMA_FIELDS["filename"])

                    chunk = RetrievedChunk(
                        id=pk_id, # Use the primary key
                        content=content,
                        score=hit.score,
                        metadata=metadata, # Store all retrieved metadata
                        embedding=embedding_vector,
                        # Populate direct domain fields from metadata if available
                        document_id=str(doc_id) if doc_id else None,
                        file_name=str(fname) if fname else None,
                        company_id=str(comp_id) if comp_id else None
                    )
                    domain_chunks.append(chunk)
                # --- END CORRECTION ---

            search_log.info(f"Converted {len(domain_chunks)} Milvus hits to domain objects.")
            return domain_chunks

        except MilvusException as me:
             search_log.error("Milvus search failed", error_code=me.code, error_message=me.message)
             raise ConnectionError(f"Vector DB search error (Code: {me.code}): {me.message}") from me
        except Exception as e:
            search_log.exception("Unexpected error during Milvus search")
            raise ConnectionError(f"Vector DB search service error: {e}") from e

    async def connect(self):
        """Explicitly ensures connection (can be called during startup if needed)."""
        await self._ensure_connection()

    async def disconnect(self):
        """Disconnects from Milvus."""
        if self._connected and self._alias in connections.list_connections():
            log.info("Disconnecting from Milvus...", adapter="MilvusAdapter", alias=self._alias)
            try:
                connections.disconnect(self._alias)
                self._connected = False
                self._collection = None # Reset collection object on disconnect
                log.info("Disconnected from Milvus.", adapter="MilvusAdapter")
            except Exception as e:
                log.error("Error during Milvus disconnect", error=str(e), exc_info=True)
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
from typing import Annotated, Optional
import httpx

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
    SparseRetrieverPort, DiversityFilterPort, ChunkContentRepositoryPort,
    EmbeddingPort # RerankerPort not directly used here anymore for local reranker
)
from app.infrastructure.persistence.postgres_repositories import (
    PostgresChatRepository, PostgresLogRepository, PostgresChunkContentRepository
)
from app.infrastructure.vectorstores.milvus_adapter import MilvusAdapter
from app.infrastructure.llms.gemini_adapter import GeminiAdapter
from app.infrastructure.retrievers.bm25_retriever import BM25sRetriever
from app.infrastructure.filters.diversity_filter import MMRDiversityFilter, StubDiversityFilter
from app.infrastructure.clients.embedding_service_client import EmbeddingServiceClient
from app.infrastructure.embedding.remote_embedding_adapter import RemoteEmbeddingAdapter


from app.application.use_cases.ask_query_use_case import AskQueryUseCase
from app.dependencies import set_ask_query_use_case_instance

from app.infrastructure.persistence import postgres_connector

# Global state
SERVICE_READY = False
# Global instances for simplified DI
chat_repo_instance: Optional[ChatRepositoryPort] = None
log_repo_instance: Optional[LogRepositoryPort] = None
chunk_content_repo_instance: Optional[ChunkContentRepositoryPort] = None
vector_store_instance: Optional[VectorStorePort] = None
llm_instance: Optional[LLMPort] = None
sparse_retriever_instance: Optional[SparseRetrieverPort] = None
diversity_filter_instance: Optional[DiversityFilterPort] = None
embedding_service_client_instance: Optional[EmbeddingServiceClient] = None
embedding_adapter_instance: Optional[EmbeddingPort] = None
ask_query_use_case_instance: Optional[AskQueryUseCase] = None
http_client_instance: Optional[httpx.AsyncClient] = None


# --- Lifespan Manager ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global SERVICE_READY, chat_repo_instance, log_repo_instance, chunk_content_repo_instance, \
           vector_store_instance, llm_instance, sparse_retriever_instance, \
           diversity_filter_instance, ask_query_use_case_instance, \
           embedding_service_client_instance, embedding_adapter_instance, http_client_instance

    SERVICE_READY = False
    log.info(f"Starting up {settings.PROJECT_NAME}...")
    dependencies_ok = True
    critical_failure_message = ""

    # 0. Initialize Global HTTP Client
    try:
        http_client_instance = httpx.AsyncClient(
            timeout=settings.HTTP_CLIENT_TIMEOUT,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20) # Sensible defaults
        )
        log.info("Global HTTP client initialized.")
    except Exception as e_http_client:
        critical_failure_message = "Failed to initialize global HTTP client."
        log.critical(f"CRITICAL: {critical_failure_message}", error=str(e_http_client), exc_info=True)
        dependencies_ok = False


    # 1. Initialize DB Pool
    if dependencies_ok:
        try:
            await postgres_connector.get_db_pool()
            db_ready = await postgres_connector.check_db_connection()
            if db_ready:
                log.info("PostgreSQL connection pool initialized and verified.")
                chat_repo_instance = PostgresChatRepository()
                log_repo_instance = PostgresLogRepository()
                chunk_content_repo_instance = PostgresChunkContentRepository()
            else:
                critical_failure_message = "Failed PostgreSQL connection verification during startup."
                log.critical(f"CRITICAL: {critical_failure_message}")
                dependencies_ok = False
        except Exception as e_pg:
            critical_failure_message = "Failed PostgreSQL pool initialization."
            log.critical(f"CRITICAL: {critical_failure_message}", error=str(e_pg), exc_info=True)
            dependencies_ok = False

    # 2. Initialize Embedding Service Client & Adapter
    if dependencies_ok:
        try:
            # EmbeddingServiceClient creates its own internal httpx.AsyncClient
            # but we could pass the global one if we refactor EmbeddingServiceClient.
            # For now, it's fine as is.
            embedding_service_client_instance = EmbeddingServiceClient(
                base_url=str(settings.EMBEDDING_SERVICE_URL),
                timeout=settings.EMBEDDING_CLIENT_TIMEOUT # Specific timeout for embedding client
            )
            embedding_adapter_instance = RemoteEmbeddingAdapter(client=embedding_service_client_instance)
            await embedding_adapter_instance.initialize() # Tries to get dimension
            
            emb_service_healthy = await embedding_adapter_instance.health_check()
            if emb_service_healthy:
                log.info("Embedding Service client and adapter initialized, health check passed.")
            else:
                critical_failure_message = "Embedding Service health check failed during startup."
                log.critical(f"CRITICAL: {critical_failure_message} URL: {settings.EMBEDDING_SERVICE_URL}")
                dependencies_ok = False
        except Exception as e_embed:
            critical_failure_message = "Failed to initialize Embedding Service client/adapter."
            log.critical(f"CRITICAL: {critical_failure_message}", error=str(e_embed), exc_info=True, url=settings.EMBEDDING_SERVICE_URL)
            dependencies_ok = False


    # 3. Initialize Milvus Adapter
    if dependencies_ok:
        try:
            vector_store_instance = MilvusAdapter()
            await vector_store_instance.connect() # Explicitly connect and load collection
            log.info("Milvus Adapter initialized and collection checked/loaded.")
        except Exception as e_milvus:
            critical_failure_message = "Failed to initialize Milvus Adapter or load collection."
            log.critical(
                f"CRITICAL: {critical_failure_message} Ensure collection '{settings.MILVUS_COLLECTION_NAME}' exists and is accessible.",
                error=str(e_milvus), exc_info=True, adapter_error=getattr(e_milvus, 'message', 'N/A')
            )
            dependencies_ok = False

    # 4. Initialize LLM Adapter
    if dependencies_ok:
        try:
            llm_instance = GeminiAdapter()
            if not llm_instance.model:
                 critical_failure_message = "Gemini Adapter initialized but model failed to load (check API key)."
                 log.critical(f"CRITICAL: {critical_failure_message}")
                 dependencies_ok = False
            else:
                 log.info("Gemini Adapter initialized successfully.")
        except Exception as e_llm:
            critical_failure_message = "Failed to initialize Gemini Adapter."
            log.critical(f"CRITICAL: {critical_failure_message}", error=str(e_llm), exc_info=True)
            dependencies_ok = False
    
    # Initialize optional components
    if dependencies_ok:
        if settings.BM25_ENABLED:
            try:
                if chunk_content_repo_instance: # Make sure repo is available
                    sparse_retriever_instance = BM25sRetriever(chunk_content_repo_instance)
                    log.info("BM25s Retriever initialized.")
                else:
                    log.error("BM25 enabled but ChunkContentRepository failed to initialize. Disabling BM25 for this session.")
                    sparse_retriever_instance = None # Ensure it's None
            except ImportError:
                log.error("BM25sRetriever dependency (bm2s) not installed. BM25 disabled.")
                sparse_retriever_instance = None
            except Exception as e_bm25:
                log.error("Failed to initialize BM25s Retriever. BM25 will be disabled.", error=str(e_bm25), exc_info=True)
                sparse_retriever_instance = None


        if settings.DIVERSITY_FILTER_ENABLED:
            try:
                if embedding_adapter_instance and embedding_adapter_instance.get_embedding_dimension() > 0 : # MMR needs embeddings
                    diversity_filter_instance = MMRDiversityFilter(lambda_mult=settings.QUERY_DIVERSITY_LAMBDA)
                    log.info("MMR Diversity Filter initialized.")
                else:
                    log.warning("MMR Diversity Filter enabled but embedding adapter is not available or has no dimension. Falling back to StubDiversityFilter.")
                    diversity_filter_instance = StubDiversityFilter()
            except Exception as e_diversity:
                log.error("Failed to initialize MMR Diversity Filter. Falling back to StubDiversityFilter.", error=str(e_diversity), exc_info=True)
                diversity_filter_instance = StubDiversityFilter()
        else: # If not enabled, use Stub
            log.info("Diversity filter disabled in settings, using StubDiversityFilter as placeholder.")
            diversity_filter_instance = StubDiversityFilter()

    # 5. Instantiate Use Case
    if dependencies_ok:
         try:
             if not http_client_instance: # Should not happen if dependencies_ok is True
                 raise RuntimeError("HTTP client instance is not available for AskQueryUseCase.")

             ask_query_use_case_instance = AskQueryUseCase(
                 chat_repo=chat_repo_instance,
                 log_repo=log_repo_instance,
                 vector_store=vector_store_instance,
                 llm=llm_instance,
                 embedding_adapter=embedding_adapter_instance,
                 http_client=http_client_instance, # Pass the global HTTP client
                 sparse_retriever=sparse_retriever_instance,
                 chunk_content_repo=chunk_content_repo_instance,
                 diversity_filter=diversity_filter_instance
             )
             log.info("AskQueryUseCase instantiated successfully.")
             SERVICE_READY = True # Mark service as ready only if all critical components are up
             set_ask_query_use_case_instance(ask_query_use_case_instance, SERVICE_READY)
             log.info(f"{settings.PROJECT_NAME} service components initialized. SERVICE READY.")

         except Exception as e_usecase:
              critical_failure_message = "Failed to instantiate AskQueryUseCase."
              log.critical(f"CRITICAL: {critical_failure_message}", error=str(e_usecase), exc_info=True)
              SERVICE_READY = False
              set_ask_query_use_case_instance(None, False) # Ensure it's set to not ready
    else: # If dependencies_ok was false earlier
        log.critical(f"{settings.PROJECT_NAME} startup sequence aborted due to critical failure: {critical_failure_message}")
        log.critical("SERVICE NOT READY.")
        set_ask_query_use_case_instance(None, False)


    if not SERVICE_READY: # Double check the flag before concluding startup
        if not critical_failure_message: critical_failure_message = "Unknown critical dependency failure during startup."
        log.critical(f"Startup finished. Critical failure detected: {critical_failure_message}. SERVICE NOT READY.")


    yield # Application runs here

    # --- Shutdown Logic ---
    log.info(f"Shutting down {settings.PROJECT_NAME}...")
    if http_client_instance:
        await http_client_instance.aclose()
        log.info("Global HTTP client closed.")
    await postgres_connector.close_db_pool()
    if vector_store_instance and hasattr(vector_store_instance, 'disconnect'):
        try: await vector_store_instance.disconnect()
        except Exception as e_milvus_close: log.error("Error during Milvus disconnect", error=str(e_milvus_close), exc_info=True)
    if embedding_service_client_instance: # This client has its own httpx client internally
        try: await embedding_service_client_instance.close()
        except Exception as e_emb_client_close: log.error("Error closing EmbeddingServiceClient", error=str(e_emb_client_close), exc_info=True)
    log.info("Shutdown complete.")

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.3.2",
    description="Microservice to handle user queries using RAG pipeline, chat history, remote embedding, and remote reranking.",
    lifespan=lifespan
)

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
        log_level_method = "warning" if 400 <= response.status_code < 500 else "error" if response.status_code >= 500 else "info"
        getattr(resp_log, log_level_method)("Request finished")
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time:.2f}"
    except Exception as e_middleware:
        process_time = (asyncio.get_event_loop().time() - start_time) * 1000
        exc_log = req_log.bind(status_code=500, duration_ms=round(process_time, 2))
        exc_log.exception("Unhandled exception during request processing middleware") # Log with full traceback
        response = JSONResponse(status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": "Internal Server Error"})
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Process-Time-Ms"] = f"{process_time:.2f}"
    finally: structlog.contextvars.clear_contextvars()
    return response

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    log_level_method = log.warning if exc.status_code < 500 else log.error
    log_level_method("HTTP Exception caught by handler", status_code=exc.status_code, detail=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    error_details_val = []
    try: error_details_val = exc.errors()
    except Exception: error_details_val = [{"loc": [], "msg": "Failed to parse validation errors.", "type": "internal_parsing_error"}]
    log.warning("Request Validation Error caught by handler", errors=error_details_val)
    return JSONResponse(status_code=fastapi_status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": error_details_val})

@app.exception_handler(ResponseValidationError)
async def response_validation_exception_handler(request: Request, exc: ResponseValidationError):
    log.error("Response Validation Error caught by handler", errors=exc.errors(), exc_info=True)
    return JSONResponse(status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": "Internal Server Error: Failed to serialize response."})

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled Exception caught by generic handler") # Log with full traceback
    return JSONResponse(status_code=fastapi_status.HTTP_500_INTERNAL_SERVER_ERROR, content={"detail": "Internal Server Error"})


def get_chat_repository() -> ChatRepositoryPort:
    if not chat_repo_instance:
        log.error("Dependency Injection Failed: ChatRepository requested but not initialized.")
        raise HTTPException(status_code=503, detail="Chat service component not available.")
    return chat_repo_instance

app.include_router(query_router_module.router, prefix=settings.API_V1_STR, tags=["Query Interaction"])
app.include_router(chat_router_module.router, prefix=settings.API_V1_STR, tags=["Chat Management"])
log.info("Routers included", prefix=settings.API_V1_STR)

@app.get("/", tags=["Health Check"], summary="Service Liveness/Readiness Check")
async def read_root():
    health_log = log.bind(check="liveness_readiness_root")
    if not SERVICE_READY:
        health_log.warning("Health check (root) failed: Service not ready.", service_ready_flag=SERVICE_READY)
        raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service Not Ready")

    # Check Embedding Service Health
    if embedding_adapter_instance:
        emb_adapter_healthy = await embedding_adapter_instance.health_check()
        if not emb_adapter_healthy:
            health_log.error("Health check (root) failed: Embedding Adapter reports unhealthy dependency (Embedding Service).")
            raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Critical dependency (Embedding Service) is unhealthy.")
    else: # Should not happen if SERVICE_READY is true
        health_log.error("Health check (root) warning: Embedding Adapter instance not available, inconsistent with SERVICE_READY state.")
        raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service inconsistency: Embedding adapter missing.")

    # Check Reranker Service Health (if enabled)
    if settings.RERANKER_ENABLED:
        if not http_client_instance: # Should not happen if SERVICE_READY and http_client is critical
            health_log.error("Health check (root) warning: HTTP client instance not available for Reranker health check.")
            raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service inconsistency: HTTP client missing.")
        try:
            reranker_health_url = str(settings.RERANKER_SERVICE_URL).rstrip('/') + "/health"
            response = await http_client_instance.get(reranker_health_url, timeout=5) # Short timeout for health
            if response.status_code == 200:
                 reranker_data = response.json()
                 if reranker_data.get("status") == "ok" and reranker_data.get("model_status") == "loaded":
                     health_log.debug("Reranker service health check successful via root.")
                 else:
                     health_log.error("Health check (root) failed: Reranker service reported unhealthy.", status=response.status_code, details=reranker_data)
                     raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Critical dependency (Reranker Service) is unhealthy.")
            else:
                 health_log.error("Health check (root) failed: Reranker service unresponsive.", status=response.status_code, response_text=response.text)
                 raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Critical dependency (Reranker Service) is unresponsive.")
        except httpx.RequestError as e_rerank_health:
             health_log.error("Health check (root) failed: Error connecting to Reranker service.", error=str(e_rerank_health))
             raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Critical dependency (Reranker Service) connection failed.")
        except Exception as e_rerank_health_generic: # Catch JSONDecodeError or other issues
            health_log.error("Health check (root) failed: Error processing Reranker service health response.", error=str(e_rerank_health_generic))
            raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Critical dependency (Reranker Service) health check processing error.")


    health_log.debug("Health check (root) passed.")
    return PlainTextResponse("OK", status_code=fastapi_status.HTTP_200_OK)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8001")) # Gunicorn/Cloud Run might set PORT
    log_level_str = settings.LOG_LEVEL.lower()
    print(f"----- Starting {settings.PROJECT_NAME} locally on port {port} -----")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=True, log_level=log_level_str)

# jfu
```

## File: `app\models\__init__.py`
```py

```

## File: `app\pipelines\rag_pipeline.py`
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
version = "0.3.2"
description = "Query service for SaaS B2B using Clean Architecture, PyMilvus, Haystack & Advanced RAG with remote embeddings and reranking"
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
# haystack-ai is kept for Document, PromptBuilder, etc.
haystack-ai = "^2.0.1"
pymilvus = "^2.4.1"

# --- LLM Dependency ---
google-generativeai = "^0.5.4"

# --- RAG Component Dependencies ---
# sentence-transformers is REMOVED as reranking is now external
numpy = "1.26.4" # Kept, as bm2s or other parts might need it. Haystack might also use it.
# fastembed and fastembed-haystack are REMOVED as embedding is now external


[tool.poetry.group.dev.dependencies]
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
