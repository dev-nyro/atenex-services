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
│   │   ├── embedding_service_client.py
│   │   └── sparse_search_service_client.py
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
│   ├── retrievers
│   │   ├── __init__.py
│   │   └── remote_sparse_retriever_adapter.py
│   └── vectorstores
│       ├── __init__.py
│       └── milvus_adapter.py
├── main.py
├── models
│   └── __init__.py
├── pipelines
│   └── rag_pipeline.py
├── prompts
│   ├── general_template_gemini_v2.txt
│   ├── map_prompt_template.txt
│   ├── rag_template_gemini_v2.txt
│   └── reduce_prompt_template_v2.txt
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
import uuid # Importar uuid
from typing import List, Tuple
# LLM_REFACTOR_STEP_3: Importar modelo de dominio
from app.domain.models import RetrievedChunk

# Puerto para Retrievers dispersos (como BM25)
class SparseRetrieverPort(abc.ABC):
    """Puerto abstracto para recuperar chunks usando métodos dispersos (keyword-based)."""

    @abc.abstractmethod
    async def search(self, query: str, company_id: uuid.UUID, top_k: int) -> List[Tuple[str, float]]: # MODIFICADO: company_id es uuid.UUID
        """
        Busca chunks relevantes basados en la consulta textual y filtra por compañía.

        Args:
            query: La consulta del usuario.
            company_id: El ID de la compañía para filtrar (UUID).
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
import os 
import json 
from pydantic import ValidationError 

# Import Ports and Domain Models
from app.application.ports import (
    ChatRepositoryPort, LogRepositoryPort, VectorStorePort, LLMPort,
    SparseRetrieverPort, DiversityFilterPort, ChunkContentRepositoryPort,
    EmbeddingPort, RerankerPort 
)
from app.domain.models import RetrievedChunk, ChatMessage, RespuestaEstructurada, FuenteCitada 
from haystack.components.builders.prompt_builder import PromptBuilder
from haystack import Document

from app.core.config import settings
from app.api.v1.schemas import RetrievedDocument as RetrievedDocumentSchema 
from app.utils.helpers import truncate_text
from fastapi import HTTPException, status

log = structlog.get_logger(__name__)

GREETING_REGEX = re.compile(r"^\s*(hola|hello|hi|buenos días|buenas tardes|buenas noches|hey|qué tal|hi there)\s*[\.,!?]*\s*$", re.IGNORECASE)
RRF_K = 60 

MAP_REDUCE_NO_RELEVANT_INFO = "No hay información relevante en este fragmento."


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
        
        self.sparse_retriever = sparse_retriever if settings.BM25_ENABLED and sparse_retriever else None
        
        self.chunk_content_repo = chunk_content_repo 
        self.diversity_filter = diversity_filter if settings.DIVERSITY_FILTER_ENABLED else None

        self._prompt_builder_rag = self._initialize_prompt_builder_from_path(settings.RAG_PROMPT_TEMPLATE_PATH)
        self._prompt_builder_general = self._initialize_prompt_builder_from_path(settings.GENERAL_PROMPT_TEMPLATE_PATH)
        self._prompt_builder_map = self._initialize_prompt_builder_from_path(settings.MAP_PROMPT_TEMPLATE_PATH)
        self._prompt_builder_reduce = self._initialize_prompt_builder_from_path(settings.REDUCE_PROMPT_TEMPLATE_PATH)

        log_params = {
            "embedding_adapter_type": type(self.embedding_adapter).__name__,
            "bm25_enabled_setting": settings.BM25_ENABLED, 
            "sparse_retriever_active": bool(self.sparse_retriever), 
            "sparse_retriever_type": type(self.sparse_retriever).__name__ if self.sparse_retriever else "None",
            "reranker_enabled": settings.RERANKER_ENABLED,
            "reranker_service_url": str(settings.RERANKER_SERVICE_URL) if settings.RERANKER_ENABLED else "N/A",
            "diversity_filter_enabled": settings.DIVERSITY_FILTER_ENABLED,
            "diversity_filter_type": type(self.diversity_filter).__name__ if self.diversity_filter else "None",
            "map_reduce_enabled": settings.MAPREDUCE_ENABLED,
            "map_reduce_threshold": settings.MAPREDUCE_ACTIVATION_THRESHOLD,
            "map_reduce_batch_size": settings.MAPREDUCE_CHUNK_BATCH_SIZE,
            "rag_prompt_path": settings.RAG_PROMPT_TEMPLATE_PATH,
            "general_prompt_path": settings.GENERAL_PROMPT_TEMPLATE_PATH,
            "map_prompt_path": settings.MAP_PROMPT_TEMPLATE_PATH,
            "reduce_prompt_path": settings.REDUCE_PROMPT_TEMPLATE_PATH,
            "gemini_model_name": settings.GEMINI_MODEL_NAME,
            "max_context_chunks": settings.MAX_CONTEXT_CHUNKS,
            "max_prompt_tokens": settings.MAX_PROMPT_TOKENS,
        }
        log.info("AskQueryUseCase Initialized", **log_params)
        if settings.BM25_ENABLED and not self.sparse_retriever:
             log.error("BM25_ENABLED in settings, but sparse_retriever instance is NOT available (init failed or service unavailable). Sparse search will be skipped.")
        if self.sparse_retriever and not self.chunk_content_repo:
            log.error("SparseRetriever is active but ChunkContentRepository is missing. Content fetching for sparse results will fail.")


    def _initialize_prompt_builder_from_path(self, template_path: str) -> PromptBuilder:
        init_log = log.bind(action="_initialize_prompt_builder_from_path", path=template_path)
        init_log.debug("Initializing PromptBuilder from path...")
        try:
            path_to_template = template_path
            
            if not os.path.exists(path_to_template):
                init_log.error("Prompt template file not found at absolute path.")
                raise FileNotFoundError(f"Prompt template file not found at {template_path}")

            with open(path_to_template, "r", encoding="utf-8") as f:
                template_content = f.read()
            
            if not template_content.strip():
                init_log.error("Prompt template file is empty.")
                raise ValueError(f"Prompt template file is empty: {template_path}")

            builder = PromptBuilder(template=template_content)
            init_log.info("PromptBuilder initialized successfully from file.")
            return builder
        except FileNotFoundError:
            init_log.error("Prompt template file not found.")
            default_fallback_template = "Query: {{ query }}\n{% if documents %}Context: {{documents}}{% endif %}\nAnswer:"
            init_log.warning(f"Falling back to basic template due to missing file: {template_path}")
            return PromptBuilder(template=default_fallback_template)
        except Exception as e:
            init_log.exception("Failed to load or initialize PromptBuilder from path.")
            raise RuntimeError(f"Critical error loading prompt template from {template_path}: {e}") from e

    async def _embed_query(self, query: str) -> List[float]:
        embed_log = log.bind(action="_embed_query_use_case_call_remote")
        try:
            embedding = await self.embedding_adapter.embed_query(query)
            if not embedding or not isinstance(embedding, list) or not all(isinstance(f, float) for f in embedding):
                embed_log.error("Invalid embedding received from adapter", received_embedding_type=type(embedding).__name__)
                raise ValueError("Embedding adapter returned invalid or empty vector.")
            embed_log.debug("Query embedded successfully via remote adapter", vector_dim=len(embedding))
            return embedding
        except ConnectionError as e: 
            embed_log.error("Embedding failed: Connection to embedding service failed.", error=str(e), exc_info=False)
            raise ConnectionError(f"Embedding service error: {e}") from e
        except ValueError as e: 
            embed_log.error("Embedding failed: Invalid data from embedding service.", error=str(e), exc_info=False)
            raise ValueError(f"Embedding service data error: {e}") from e
        except Exception as e: 
            embed_log.error("Unexpected error during query embedding via adapter", error=str(e), exc_info=True)
            raise ConnectionError(f"Unexpected error contacting embedding service: {e}") from e

    def _format_chat_history(self, messages: List[ChatMessage]) -> str:
        if not messages:
            return ""
        history_str = []
        for msg in reversed(messages): 
            role = "Usuario" if msg.role == 'user' else "Atenex"
            time_mark = format_time_delta(msg.created_at)
            history_str.append(f"{role} ({time_mark}): {msg.content}")
        return "\n".join(reversed(history_str))

    async def _build_prompt(self, query: str, documents: List[Document], chat_history: Optional[str] = None, builder: Optional[PromptBuilder] = None, prompt_data_override: Optional[Dict[str,Any]] = None) -> str:
        
        final_prompt_data = {"query": query}
        if prompt_data_override:
            final_prompt_data.update(prompt_data_override)
        else: 
            if documents:
                final_prompt_data["documents"] = documents
            if chat_history:
                final_prompt_data["chat_history"] = chat_history
        
        effective_builder = builder
        if not effective_builder: 
            if documents or "documents" in final_prompt_data or "mapped_responses" in final_prompt_data: 
                effective_builder = self._prompt_builder_rag 
                if "mapped_responses" in final_prompt_data: effective_builder = self._prompt_builder_reduce
            else: 
                effective_builder = self._prompt_builder_general
        
        log.debug("Building prompt", builder_type=type(effective_builder).__name__, data_keys=list(final_prompt_data.keys()))

        try:
            result = await asyncio.to_thread(effective_builder.run, **final_prompt_data)
            prompt = result.get("prompt")
            if not prompt: raise ValueError("Prompt generation returned empty.")
            log.debug("Prompt built successfully.", length=len(prompt))
            return prompt
        except Exception as e:
            log.error("Prompt building failed", error=str(e), data_keys=list(final_prompt_data.keys()), exc_info=True)
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

        sorted_chunk_ids_with_scores = sorted(fused_scores.items(), key=lambda item: item[1], reverse=True)
        top_ids_with_scores_tuples: List[Tuple[str, float]] = sorted_chunk_ids_with_scores[:top_n]
        
        fetch_log.debug("Top IDs after fusion", top_ids_count=len(top_ids_with_scores_tuples))

        chunks_with_content: List[RetrievedChunk] = []
        ids_needing_content: List[str] = []
        placeholder_map: Dict[str, RetrievedChunk] = {}

        for cid, fused_score_val in top_ids_with_scores_tuples:
            if not cid:
                 fetch_log.warning("Skipping invalid chunk ID found during fusion processing.")
                 continue
            
            original_chunk_from_dense = dense_map.get(cid)

            if original_chunk_from_dense and original_chunk_from_dense.content:
                # Crucial: Ensure embedding from dense result is preserved
                original_chunk_from_dense.score = fused_score_val 
                chunks_with_content.append(original_chunk_from_dense)
            else: 
                # If chunk was not in dense_map or had no content, it's a placeholder
                # Its embedding will be None unless it was in dense_map but content was missing.
                chunk_placeholder = RetrievedChunk(
                    id=cid,
                    score=fused_score_val, 
                    content=None, 
                    metadata=original_chunk_from_dense.metadata if original_chunk_from_dense and original_chunk_from_dense.metadata else {"retrieval_source": "sparse_or_fused_no_initial_meta"},
                    embedding=original_chunk_from_dense.embedding if original_chunk_from_dense and original_chunk_from_dense.embedding else None,
                    document_id=original_chunk_from_dense.document_id if original_chunk_from_dense else None,
                    file_name=original_chunk_from_dense.file_name if original_chunk_from_dense else None,
                    company_id=original_chunk_from_dense.company_id if original_chunk_from_dense else None
                )
                chunks_with_content.append(chunk_placeholder) 
                placeholder_map[cid] = chunk_placeholder 
                ids_needing_content.append(cid)

        if ids_needing_content and self.chunk_content_repo:
             fetch_log.info("Fetching content for chunks missing content", count=len(ids_needing_content))
             try:
                 content_map = await self.chunk_content_repo.get_chunk_contents_by_ids(ids_needing_content)
                 for cid_item, content_val in content_map.items():
                     if cid_item in placeholder_map: 
                          placeholder_map[cid_item].content = content_val
                          if placeholder_map[cid_item].metadata.get("retrieval_source") == "sparse_or_fused_no_initial_meta":
                            placeholder_map[cid_item].metadata["content_fetched_for_sparse"] = True
                          else:
                            placeholder_map[cid_item].metadata["content_fetched"] = True
                 missing_after_fetch = [cid_item_check for cid_item_check in ids_needing_content if cid_item_check not in content_map or not content_map[cid_item_check]]
                 if missing_after_fetch:
                      fetch_log.warning("Content not found or empty for some chunks after fetch", missing_ids=missing_after_fetch)
             except Exception as e_content_fetch:
                 fetch_log.exception("Failed to fetch content for fused results", error=str(e_content_fetch))
        elif ids_needing_content:
            fetch_log.warning("Cannot fetch content for sparse/fused results, ChunkContentRepository not available.")

        final_chunks_with_content = [c for c in chunks_with_content if c.content and c.content.strip()]
        fetch_log.debug("Chunks remaining after content check and fetch", count=len(final_chunks_with_content))
        
        final_chunks_with_content.sort(key=lambda c: c.score or 0.0, reverse=True)
        return final_chunks_with_content
        
    async def _handle_llm_response(
        self,
        json_answer_str: str,
        query: str,
        company_id: uuid.UUID,
        user_id: uuid.UUID,
        final_chat_id: uuid.UUID,
        original_chunks_for_citation: List[RetrievedChunk], 
        pipeline_stages_used: List[str],
        map_reduce_used: bool = False,
        retriever_k_effective: int = 0,
        fusion_fetch_k_effective: int = 0,
        num_chunks_after_rerank_or_fusion_fetch_effective: int = 0,
        num_final_chunks_sent_to_llm_effective: int = 0,
        num_history_messages_effective: int = 0
    ) -> Tuple[str, List[RetrievedChunk], Optional[uuid.UUID]]:
        
        llm_handler_log = log.bind(action="_handle_llm_response", chat_id=str(final_chat_id))
        answer_for_user: str
        retrieved_chunks_for_response: List[RetrievedChunk] = []
        assistant_sources_for_db: List[Dict[str, Any]] = []
        log_id: Optional[uuid.UUID] = None
        
        try:
            structured_answer_obj = RespuestaEstructurada.model_validate_json(json_answer_str)
            answer_for_user = structured_answer_obj.respuesta_detallada
            
            llm_handler_log.info("LLM response successfully parsed and validated into RespuestaEstructurada.",
                                 has_summary=bool(structured_answer_obj.resumen_ejecutivo),
                                 num_fuentes_citadas_by_llm=len(structured_answer_obj.fuentes_citadas),
                                 siguiente_pregunta_sugerida=structured_answer_obj.siguiente_pregunta_sugerida)

            assistant_sources_for_db = [f.model_dump(exclude_none=True) for f in structured_answer_obj.fuentes_citadas]
            
            map_chunk_id_to_original = {chunk.id: chunk for chunk in original_chunks_for_citation}
            
            processed_chunk_ids_for_response = set()

            for cited_source_by_llm in structured_answer_obj.fuentes_citadas:
                if cited_source_by_llm.id_documento and cited_source_by_llm.id_documento in map_chunk_id_to_original:
                    original_chunk = map_chunk_id_to_original[cited_source_by_llm.id_documento]
                    if original_chunk.id not in processed_chunk_ids_for_response:
                       retrieved_chunks_for_response.append(original_chunk)
                       processed_chunk_ids_for_response.add(original_chunk.id)

            if not retrieved_chunks_for_response and structured_answer_obj.fuentes_citadas:
                llm_handler_log.warning("LLM cited sources, but no direct match found by id_documento. Using filename as fallback or top N.")
                for cited_source_by_llm in structured_answer_obj.fuentes_citadas:
                    if len(retrieved_chunks_for_response) >= self.settings.NUM_SOURCES_TO_SHOW: break
                    found_by_name = False
                    for orig_chunk in original_chunks_for_citation:
                        if orig_chunk.id not in processed_chunk_ids_for_response and \
                           orig_chunk.file_name == cited_source_by_llm.nombre_archivo:
                             retrieved_chunks_for_response.append(orig_chunk)
                             processed_chunk_ids_for_response.add(orig_chunk.id)
                             found_by_name = True
                             break 
                    if not found_by_name:
                         llm_handler_log.info("LLM cited source not found by filename either", cited_source_name=cited_source_by_llm.nombre_archivo)
            
            if len(retrieved_chunks_for_response) < self.settings.NUM_SOURCES_TO_SHOW and original_chunks_for_citation:
                llm_handler_log.debug("Filling remaining source slots with top original chunks provided to LLM/MapReduce.")
                for chunk in original_chunks_for_citation:
                    if len(retrieved_chunks_for_response) >= self.settings.NUM_SOURCES_TO_SHOW: break
                    if chunk.id not in processed_chunk_ids_for_response:
                        retrieved_chunks_for_response.append(chunk)
                        processed_chunk_ids_for_response.add(chunk.id)


        except ValidationError as pydantic_err:
            llm_handler_log.error("LLM JSON response failed Pydantic validation", raw_response=truncate_text(json_answer_str, 500), errors=pydantic_err.errors())
            answer_for_user = "La respuesta del asistente no tuvo el formato esperado. Por favor, intenta de nuevo."
            assistant_sources_for_db = [{"error": "Pydantic validation failed", "details": pydantic_err.errors()}]
            retrieved_chunks_for_response = original_chunks_for_citation[:self.settings.NUM_SOURCES_TO_SHOW] 
        except json.JSONDecodeError as json_err:
            llm_handler_log.error("Failed to parse JSON response from LLM", raw_response=truncate_text(json_answer_str, 500), error=str(json_err))
            answer_for_user = f"Hubo un error al procesar la respuesta del asistente (JSON malformado): {truncate_text(json_answer_str,100)}. Por favor, intenta de nuevo."
            assistant_sources_for_db = [{"error": "JSON decode error", "details": str(json_err)}]
            retrieved_chunks_for_response = original_chunks_for_citation[:self.settings.NUM_SOURCES_TO_SHOW] 

        await self.chat_repo.save_message(
            chat_id=final_chat_id, role='assistant',
            content=answer_for_user, 
            sources=assistant_sources_for_db[:self.settings.NUM_SOURCES_TO_SHOW] if assistant_sources_for_db else None
        )
        llm_handler_log.info(f"Assistant message saved with up to {self.settings.NUM_SOURCES_TO_SHOW} sources.")

        try:
            docs_for_log_summary = [
                RetrievedDocumentSchema(**chunk.model_dump(exclude={'embedding'}, exclude_none=True)).model_dump(exclude_none=True) 
                for chunk in retrieved_chunks_for_response 
            ]
            log_metadata_details = {
                "pipeline_stages": pipeline_stages_used,
                "map_reduce_used": map_reduce_used,
                "retriever_k_initial": retriever_k_effective,
                "fusion_fetch_k": fusion_fetch_k_effective,
                "max_context_chunks_limit_for_llm": self.settings.MAX_CONTEXT_CHUNKS, 
                "num_chunks_after_rerank_or_fusion_content_fetch": num_chunks_after_rerank_or_fusion_fetch_effective,
                "num_final_chunks_sent_to_llm": num_final_chunks_sent_to_llm_effective,
                "num_sources_shown_to_user": len(assistant_sources_for_db), 
                "num_retrieved_docs_in_api_response": len(retrieved_chunks_for_response),
                "chat_history_messages_included_in_prompt": num_history_messages_effective,
                "diversity_filter_enabled_in_settings": self.settings.DIVERSITY_FILTER_ENABLED,
                "reranker_enabled_in_settings": self.settings.RERANKER_ENABLED,
                "bm25_enabled_in_settings": self.settings.BM25_ENABLED,
            }
            log_id = await self.log_repo.log_query_interaction(
                company_id=company_id, user_id=user_id, query=query, answer=answer_for_user,
                retrieved_documents_data=docs_for_log_summary, 
                chat_id=final_chat_id, metadata=log_metadata_details
            )
            llm_handler_log.info("Interaction logged successfully", db_log_id=str(log_id))
        except Exception as log_err_final:
            llm_handler_log.error("Failed to log RAG interaction", error=str(log_err_final), exc_info=False)

        return answer_for_user, retrieved_chunks_for_response, log_id


    async def execute(
        self, query: str, company_id: uuid.UUID, user_id: uuid.UUID,
        chat_id: Optional[uuid.UUID] = None, top_k: Optional[int] = None
    ) -> Tuple[str, List[RetrievedChunk], Optional[uuid.UUID], uuid.UUID]:
        
        exec_log = log.bind(use_case="AskQueryUseCase", company_id=str(company_id), user_id=str(user_id), query_preview=truncate_text(query, 50))
        retriever_k_effective = top_k if top_k is not None and 0 < top_k <= self.settings.RETRIEVER_TOP_K else self.settings.RETRIEVER_TOP_K
        exec_log = exec_log.bind(effective_retriever_k=retriever_k_effective)

        pipeline_stages_used: List[str] = []
        final_chat_id: uuid.UUID
        chat_history_str: Optional[str] = None
        history_messages: List[ChatMessage] = []

        try:
            if chat_id:
                if not await self.chat_repo.check_chat_ownership(chat_id, user_id, company_id):
                    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Chat not found or access denied.")
                final_chat_id = chat_id
                if self.settings.MAX_CHAT_HISTORY_MESSAGES > 0:
                    history_messages = await self.chat_repo.get_chat_messages(
                        chat_id=final_chat_id, user_id=user_id, company_id=company_id,
                        limit=self.settings.MAX_CHAT_HISTORY_MESSAGES, offset=0
                    )
                    chat_history_str = self._format_chat_history(history_messages)
                    exec_log.info("Chat history retrieved", num_messages=len(history_messages))
            else:
                initial_title = f"Chat: {truncate_text(query, 40)}"
                final_chat_id = await self.chat_repo.create_chat(user_id=user_id, company_id=company_id, title=initial_title)
            exec_log = exec_log.bind(chat_id=str(final_chat_id))
            await self.chat_repo.save_message(chat_id=final_chat_id, role='user', content=query)
            exec_log.info("Chat state managed and user message saved", is_new_chat=(not chat_id))

            if GREETING_REGEX.match(query):
                answer = "¡Hola! ¿En qué puedo ayudarte hoy con la información de tus documentos?"
                await self.chat_repo.save_message(chat_id=final_chat_id, role='assistant', content=answer, sources=None)
                exec_log.info("Greeting detected, responded directly.")
                simple_log_id = await self.log_repo.log_query_interaction(
                    user_id=user_id, company_id=company_id, query=query, answer=answer,
                    retrieved_documents_data=[], metadata={"type": "greeting"}, chat_id=final_chat_id
                )
                return answer, [], simple_log_id, final_chat_id

            pipeline_stages_used.append("query_embedding (remote)")
            query_embedding = await self._embed_query(query)

            pipeline_stages_used.append("dense_retrieval (milvus)")
            dense_task = self.vector_store.search(query_embedding, str(company_id), retriever_k_effective)
            
            sparse_results_tuples: List[Tuple[str, float]] = [] 
            sparse_task_placeholder = asyncio.create_task(asyncio.sleep(0)) 

            if self.sparse_retriever: 
                 pipeline_stages_used.append("sparse_retrieval (remote_sparse_search_service)")
                 sparse_task = self.sparse_retriever.search(query, company_id, retriever_k_effective)
            else:
                 pipeline_stages_used.append(f"sparse_retrieval ({'disabled_no_adapter_instance' if self.settings.BM25_ENABLED else 'disabled_in_settings'})")
                 sparse_task = sparse_task_placeholder 

            dense_chunks_domain, sparse_results_maybe_tuples = await asyncio.gather(dense_task, sparse_task)
            
            if self.sparse_retriever and isinstance(sparse_results_maybe_tuples, list):
                sparse_results_tuples = sparse_results_maybe_tuples 
            
            exec_log.info("Retrieval phase done", dense_count=len(dense_chunks_domain), sparse_count=len(sparse_results_tuples))

            pipeline_stages_used.append("fusion (rrf)")
            dense_map = {c.id: c for c in dense_chunks_domain if c.id and c.embedding is not None} # Ensure embedding exists for dense_map entries used in MMR
            
            fused_scores: Dict[str, float] = self._reciprocal_rank_fusion(dense_chunks_domain, sparse_results_tuples)
            
            fusion_fetch_k_effective = self.settings.MAX_CONTEXT_CHUNKS 
            if self.settings.RERANKER_ENABLED or self.settings.DIVERSITY_FILTER_ENABLED : 
                 fusion_fetch_k_effective = max(self.settings.MAX_CONTEXT_CHUNKS + 20, int(self.settings.MAX_CONTEXT_CHUNKS * 1.2) ) 
            
            combined_chunks_with_content: List[RetrievedChunk] = await self._fetch_content_for_fused_results(
                fused_scores, dense_map, fusion_fetch_k_effective
            )
            exec_log.info("Fusion & content fetch done", fused_results_count=len(combined_chunks_with_content), fetch_k=fusion_fetch_k_effective)
            num_chunks_after_fusion_fetch = len(combined_chunks_with_content)

            if not combined_chunks_with_content:
                exec_log.warning("No chunks with content after fusion/fetch. Using general prompt.")
                general_prompt = await self._build_prompt(query, [], chat_history=chat_history_str, builder=self._prompt_builder_general)
                answer_str = await self.llm.generate(general_prompt) 
                
                await self.chat_repo.save_message(chat_id=final_chat_id, role='assistant', content=answer_str, sources=None)
                no_docs_log_id = await self.log_repo.log_query_interaction(
                    company_id=company_id, user_id=user_id, query=query, answer=answer_str, 
                    retrieved_documents_data=[], chat_id=final_chat_id, 
                    metadata={"pipeline_stages": pipeline_stages_used, "result_type": "no_docs_for_rag", "map_reduce_used": False}
                )
                return answer_str, [], no_docs_log_id, final_chat_id
            
            chunks_for_llm_or_mapreduce = combined_chunks_with_content 
            if self.settings.RERANKER_ENABLED and chunks_for_llm_or_mapreduce:
                pipeline_stages_used.append("reranking (remote_reranker_service)")
                rerank_log = exec_log.bind(action="rerank_remote", num_chunks_to_rerank=len(chunks_for_llm_or_mapreduce))
                
                documents_for_reranker = []
                map_id_to_original_chunk_before_rerank = {c.id: c for c in chunks_for_llm_or_mapreduce}


                for chk_id, original_chunk_obj in map_id_to_original_chunk_before_rerank.items():
                    if original_chunk_obj.content and original_chunk_obj.id: 
                        documents_for_reranker.append({
                            "id": original_chunk_obj.id,
                            "text": original_chunk_obj.content, 
                            "metadata": original_chunk_obj.metadata or {} 
                        })
                
                if documents_for_reranker:
                    reranker_payload = {
                        "query": query,
                        "documents": documents_for_reranker,
                        "top_n": self.settings.MAX_CONTEXT_CHUNKS 
                    }
                    try:
                        rerank_log.debug("Sending request to reranker service...")
                        base_reranker_url = str(settings.RERANKER_SERVICE_URL).rstrip('/')
                        reranker_url = f"{base_reranker_url}/api/v1/rerank"
                        if base_reranker_url.endswith("/api/v1"):
                            reranker_url = f"{base_reranker_url.rsplit('/api/v1',1)[0]}/api/v1/rerank"
                        elif base_reranker_url.endswith("/api"): 
                            reranker_url = f"{base_reranker_url.rsplit('/api',1)[0]}/api/v1/rerank"


                        rerank_log.debug(f"Final Reranker URL: {reranker_url}")

                        reranker_response = await self.http_client.post(
                            reranker_url,
                            json=reranker_payload,
                            timeout=self.settings.RERANKER_CLIENT_TIMEOUT
                        )
                        reranker_response.raise_for_status()
                        reranked_data = reranker_response.json()

                        if "data" in reranked_data and "reranked_documents" in reranked_data["data"]:
                            reranked_docs_from_service = reranked_data["data"]["reranked_documents"]
                                                        
                            updated_reranked_chunks = []
                            for reranked_item_data in reranked_docs_from_service: 
                                chunk_id = reranked_item_data.get("id")
                                new_score = reranked_item_data.get("score")
                                
                                if chunk_id in map_id_to_original_chunk_before_rerank:
                                    original_retrieved_chunk = map_id_to_original_chunk_before_rerank[chunk_id]
                                    
                                    updated_chunk = RetrievedChunk(
                                        id=original_retrieved_chunk.id,
                                        content=original_retrieved_chunk.content, 
                                        score=new_score, 
                                        metadata={
                                            **(original_retrieved_chunk.metadata or {}),
                                            **(reranked_item_data.get("metadata", {})), 
                                            "reranked_score": new_score 
                                        },
                                        embedding=original_retrieved_chunk.embedding, 
                                        document_id=original_retrieved_chunk.document_id,
                                        file_name=original_retrieved_chunk.file_name,
                                        company_id=original_retrieved_chunk.company_id
                                    )
                                    updated_reranked_chunks.append(updated_chunk)
                                else:
                                    rerank_log.warning("Reranked chunk ID not found in original map.", reranked_id=chunk_id)

                            if updated_reranked_chunks: 
                                chunks_for_llm_or_mapreduce = updated_reranked_chunks
                                rerank_log.info(f"Reranking successful. {len(chunks_for_llm_or_mapreduce)} chunks after reranking.")
                            else:
                                rerank_log.warning("Reranking seemed successful but no chunks could be mapped back. Using pre-reranked chunks.")
                        else:
                            rerank_log.warning("Reranker service response format invalid.", response_data=reranked_data)
                    except httpx.HTTPStatusError as http_err:
                        rerank_log.error("HTTP error from Reranker service", status_code=http_err.response.status_code, response_text=http_err.response.text, exc_info=False)
                    except httpx.RequestError as req_err: 
                        rerank_log.error("Request error contacting Reranker service", error_details=str(req_err), exc_info=False) 
                    except Exception as e_rerank:
                        rerank_log.exception("Unexpected error during reranking call.")
                else:
                    rerank_log.warning("No valid documents with content/id to send for reranking.")

            else: 
                pipeline_stages_used.append(f"reranking ({'disabled' if not self.settings.RERANKER_ENABLED else 'skipped_no_chunks_or_content'})")
            
            num_chunks_after_rerank = len(chunks_for_llm_or_mapreduce)

            # --- BEGIN CORRECTION FOR MMRDiversityFilter EMBEDDING ---
            # Populate embeddings for chunks before sending to MMRDiversityFilter
            if chunks_for_llm_or_mapreduce and self.diversity_filter : # Only if there are chunks and filter is active
                pipeline_stages_used.append("embedding_population_for_mmr")
                mmr_prep_log = exec_log.bind(action="mmr_embedding_population")
                
                chunk_ids_for_mmr_filter = [doc.id for doc in chunks_for_llm_or_mapreduce if doc.id]
                mmr_prep_log.debug("Preparing embeddings for MMR", num_chunks_input=len(chunks_for_llm_or_mapreduce), num_ids_to_fetch=len(chunk_ids_for_mmr_filter))

                vectors_from_milvus_by_id: Dict[str, List[float]] = {}
                if chunk_ids_for_mmr_filter:
                    try:
                        vectors_from_milvus_by_id = await self.vector_store.fetch_vectors_by_ids(chunk_ids_for_mmr_filter)
                        mmr_prep_log.info(f"Fetched {len(vectors_from_milvus_by_id)} vectors from Milvus for MMR.")
                    except Exception as e_milvus_fetch:
                         mmr_prep_log.error("Failed to fetch vectors from Milvus for MMR, fallback may occur.", error=str(e_milvus_fetch))

                texts_needing_embedding_generation: List[str] = []
                # Keep a mapping from original index of text to chunk object to update it later
                map_text_index_to_chunk_obj: Dict[int, RetrievedChunk] = {} 
                
                # Create a new list for chunks that will go to MMR, preserving original order as much as possible
                # but ensuring they are reconstructed with the embedding field.
                chunks_ready_for_mmr: List[RetrievedChunk] = []

                for original_chunk in chunks_for_llm_or_mapreduce:
                    retrieved_embedding = vectors_from_milvus_by_id.get(original_chunk.id)
                    
                    if retrieved_embedding is None and original_chunk.content: # If Milvus didn't have it, AND content exists
                        if original_chunk.embedding is None: # Only add to fallback if no embedding was ever present
                            map_text_index_to_chunk_obj[len(texts_needing_embedding_generation)] = original_chunk
                            texts_needing_embedding_generation.append(original_chunk.content)
                            
                    # Reconstruct the chunk. If embedding was already there (e.g. from dense_map earlier),
                    # or fetched from Milvus now, it will be included. Otherwise, it's None for now.
                    chunks_ready_for_mmr.append(
                        RetrievedChunk(
                            id=original_chunk.id,
                            content=original_chunk.content,
                            score=original_chunk.score,
                            metadata=original_chunk.metadata,
                            embedding=retrieved_embedding if retrieved_embedding else original_chunk.embedding, # Prioritize fresh Milvus fetch, then existing
                            document_id=original_chunk.document_id,
                            file_name=original_chunk.file_name,
                            company_id=original_chunk.company_id
                        )
                    )
                
                if texts_needing_embedding_generation:
                    mmr_prep_log.info(f"Requesting {len(texts_needing_embedding_generation)} missing embeddings from embedding_adapter for MMR.")
                    try:
                        # Call the embedding adapter to get embeddings for texts
                        generated_embeddings_list = await self.embedding_adapter.embed_texts(texts_needing_embedding_generation)
                        
                        if len(generated_embeddings_list) == len(texts_needing_embedding_generation):
                            for idx, generated_emb_vector in enumerate(generated_embeddings_list):
                                chunk_to_update = map_text_index_to_chunk_obj[idx] 
                                # Find the corresponding chunk in chunks_ready_for_mmr and update its embedding
                                for ch_ready in chunks_ready_for_mmr:
                                    if ch_ready.id == chunk_to_update.id:
                                        ch_ready.embedding = generated_emb_vector
                                        break
                            mmr_prep_log.info("Successfully updated chunks with newly generated embeddings for MMR.")
                        else:
                            mmr_prep_log.error("Mismatch in count of generated embeddings and requested texts for MMR fallback.",
                                               requested_count=len(texts_needing_embedding_generation),
                                               generated_count=len(generated_embeddings_list))
                    except Exception as e_embed_fallback:
                        mmr_prep_log.error("Failed to generate embeddings via adapter for MMR fallback.", error=str(e_embed_fallback))
                
                # The list chunks_for_llm_or_mapreduce now contains chunks with embeddings populated
                chunks_for_llm_or_mapreduce = chunks_ready_for_mmr
            # --- END CORRECTION FOR MMRDiversityFilter EMBEDDING ---


            # Log para depurar embeddings antes del filtro MMR
            num_with_embeddings_before_mmr = sum(1 for c_chk in chunks_for_llm_or_mapreduce if c_chk.embedding is not None)
            exec_log.debug(
                "Chunks before diversity filter",
                total_chunks=len(chunks_for_llm_or_mapreduce),
                chunks_with_embeddings=num_with_embeddings_before_mmr,
                first_few_ids_and_embedding_status=[(c.id, c.embedding is not None) for c in chunks_for_llm_or_mapreduce[:min(5, len(chunks_for_llm_or_mapreduce))]]
            )

            if self.diversity_filter and chunks_for_llm_or_mapreduce:
                 k_final_diversity = self.settings.MAX_CONTEXT_CHUNKS 
                 filter_type = type(self.diversity_filter).__name__
                 pipeline_stages_used.append(f"diversity_filter ({filter_type})")
                 exec_log.debug(f"Applying {filter_type} k={k_final_diversity}...", count=len(chunks_for_llm_or_mapreduce))
                 chunks_for_llm_or_mapreduce = await self.diversity_filter.filter(chunks_for_llm_or_mapreduce, k_final_diversity)
                 exec_log.info(f"{filter_type} applied.", final_count=len(chunks_for_llm_or_mapreduce))
            else: 
                 pipeline_stages_used.append(f"diversity_filter ({'disabled' if not self.settings.DIVERSITY_FILTER_ENABLED else 'skipped_no_chunks'})")
                 chunks_for_llm_or_mapreduce = chunks_for_llm_or_mapreduce[:self.settings.MAX_CONTEXT_CHUNKS]
                 exec_log.info(f"Diversity filter not applied or no chunks. Truncating to MAX_CONTEXT_CHUNKS.", 
                               count=len(chunks_for_llm_or_mapreduce), limit=self.settings.MAX_CONTEXT_CHUNKS)

            final_chunks_for_processing = [c for c in chunks_for_llm_or_mapreduce if c.content and c.content.strip()]
            num_final_chunks_for_llm_or_mapreduce = len(final_chunks_for_processing)

            if not final_chunks_for_processing: 
                exec_log.warning("No chunks with content after reranking/filtering. Using general prompt.")
                general_prompt = await self._build_prompt(query, [], chat_history=chat_history_str, builder=self._prompt_builder_general)
                answer_str = await self.llm.generate(general_prompt, response_pydantic_schema=None)
                await self.chat_repo.save_message(chat_id=final_chat_id, role='assistant', content=answer_str, sources=None)
                no_docs_final_log_id = await self.log_repo.log_query_interaction(
                    company_id=company_id, user_id=user_id, query=query, answer=answer_str, 
                    retrieved_documents_data=[], chat_id=final_chat_id, 
                    metadata={"pipeline_stages": pipeline_stages_used, "result_type": "no_docs_after_postprocessing", "map_reduce_used": False}
                )
                return answer_str, [], no_docs_final_log_id, final_chat_id

            map_reduce_active = False
            json_answer_str: str

            if self.settings.MAPREDUCE_ENABLED and len(final_chunks_for_processing) > self.settings.MAPREDUCE_ACTIVATION_THRESHOLD:
                exec_log.info(f"Activating MapReduce. Chunks: {len(final_chunks_for_processing)}, Threshold: {self.settings.MAPREDUCE_ACTIVATION_THRESHOLD}", flow_type="MapReduce")
                pipeline_stages_used.append("map_reduce_flow")
                map_reduce_active = True

                pipeline_stages_used.append("map_phase")
                mapped_responses_parts = []
                haystack_docs_for_map = [
                    Document(
                        id=c.id, 
                        content=c.content, 
                        meta={ 
                            "file_name": c.file_name, 
                            "page": c.metadata.get("page"), 
                            "title": c.metadata.get("title") 
                        },
                        score=c.score
                    ) for c in final_chunks_for_processing
                ]
                
                map_tasks = []
                for i in range(0, len(haystack_docs_for_map), self.settings.MAPREDUCE_CHUNK_BATCH_SIZE):
                    batch_docs = haystack_docs_for_map[i:i + self.settings.MAPREDUCE_CHUNK_BATCH_SIZE]
                    map_prompt_data = {
                        "original_query": query, 
                        "documents": batch_docs, 
                        "document_index": i, 
                        "total_documents": len(haystack_docs_for_map) 
                    }
                    map_prompt_str_task = self._build_prompt(query="", documents=[], prompt_data_override=map_prompt_data, builder=self._prompt_builder_map)
                    map_tasks.append(map_prompt_str_task)
                
                map_prompts = await asyncio.gather(*map_tasks)
                
                llm_map_tasks = []
                for idx, map_prompt in enumerate(map_prompts):
                    llm_map_tasks.append(self.llm.generate(map_prompt, response_pydantic_schema=None)) 
                
                map_phase_results = await asyncio.gather(*[asyncio.shield(task) for task in llm_map_tasks], return_exceptions=True)

                for idx, result in enumerate(map_phase_results):
                    map_log = exec_log.bind(map_batch_index=idx)
                    if isinstance(result, Exception):
                        map_log.error("LLM call failed for map batch", error=str(result), exc_info=True)
                    elif result and MAP_REDUCE_NO_RELEVANT_INFO not in result:
                        mapped_responses_parts.append(f"--- Extracto del Lote de Documentos {idx + 1} ---\n{result}\n")
                        map_log.info("Map request processed for batch.", response_length=len(result), is_relevant=True)
                    else:
                         map_log.info("Map request processed for batch, no relevant info found by LLM.", response_length=len(result or ""))


                if not mapped_responses_parts:
                    exec_log.warning("MapReduce: All map steps reported no relevant information or failed.")
                    concatenated_mapped_responses = "Todos los fragmentos procesados indicaron no tener información relevante para la consulta o hubo errores en su procesamiento."
                else:
                    concatenated_mapped_responses = "\n".join(mapped_responses_parts)

                pipeline_stages_used.append("reduce_phase")
                reduce_prompt_data = {
                    "original_query": query,
                    "chat_history": chat_history_str,
                    "mapped_responses": concatenated_mapped_responses,
                    "original_documents_for_citation": haystack_docs_for_map 
                }
                reduce_prompt_str = await self._build_prompt(query="", documents=[], prompt_data_override=reduce_prompt_data, builder=self._prompt_builder_reduce)
                
                exec_log.info("Sending reduce request to LLM for final JSON response.")
                json_answer_str = await self.llm.generate(reduce_prompt_str, response_pydantic_schema=RespuestaEstructurada)

            else: 
                exec_log.info(f"Using Direct RAG strategy. Chunks: {len(final_chunks_for_processing)}", flow_type="DirectRAG")
                pipeline_stages_used.append("direct_rag_flow")
                
                haystack_docs_for_prompt = [
                    Document(
                        id=c.id, 
                        content=c.content, 
                        meta={ 
                            "file_name": c.file_name, 
                            "page": c.metadata.get("page"),
                            "title": c.metadata.get("title")
                        },
                        score=c.score
                    ) for c in final_chunks_for_processing
                ]
                direct_rag_prompt = await self._build_prompt(query, haystack_docs_for_prompt, chat_history_str, builder=self._prompt_builder_rag)
                
                exec_log.info("Sending direct RAG request to LLM for JSON response.")
                json_answer_str = await self.llm.generate(direct_rag_prompt, response_pydantic_schema=RespuestaEstructurada)

            answer_text, relevant_chunks_for_api, final_log_id = await self._handle_llm_response(
                json_answer_str=json_answer_str,
                query=query,
                company_id=company_id,
                user_id=user_id,
                final_chat_id=final_chat_id,
                original_chunks_for_citation=final_chunks_for_processing, 
                pipeline_stages_used=pipeline_stages_used,
                map_reduce_used=map_reduce_active,
                retriever_k_effective=retriever_k_effective,
                fusion_fetch_k_effective=fusion_fetch_k_effective,
                num_chunks_after_rerank_or_fusion_fetch_effective=num_chunks_after_rerank, 
                num_final_chunks_sent_to_llm_effective=num_final_chunks_for_llm_or_mapreduce,
                num_history_messages_effective=len(history_messages)
            )
            
            exec_log.info("Use case execution finished successfully.")
            return answer_text, relevant_chunks_for_api, final_log_id, final_chat_id

        except ConnectionError as ce: 
            exec_log.error("Connection error during use case execution", error=str(ce), exc_info=False)
            detail_message = "A required external service is unavailable. Please try again later."
            if "Embedding service" in str(ce): detail_message = "The embedding service is currently unavailable."
            elif "Reranker service" in str(ce): detail_message = "The reranking service is currently unavailable."
            elif "Sparse search service" in str(ce): detail_message = "The sparse search service is currently unavailable."
            elif "Gemini API" in str(ce): detail_message = "The language model service (Gemini) is currently unavailable."
            elif "Vector DB" in str(ce): detail_message = "The vector database service is currently unavailable."
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail_message) from ce
        except ValueError as ve: 
            exec_log.error("Value error during use case execution", error=str(ve), exc_info=True) 
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Data processing error: {ve}") from ve
        except HTTPException as http_exc: 
            exec_log.warning("HTTPException caught in use case", status_code=http_exc.status_code, detail=http_exc.detail)
            raise http_exc
        except Exception as e: 
            exec_log.exception("Unexpected error during use case execution") 
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"An internal server error occurred: {type(e).__name__}. Please contact support if this persists.") from e
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
from pathlib import Path 

# --- Default Values ---
# PostgreSQL
POSTGRES_K8S_HOST_DEFAULT = "postgresql-service.nyro-develop.svc.cluster.local"
POSTGRES_K8S_PORT_DEFAULT = 5432
POSTGRES_K8S_DB_DEFAULT = "atenex"
POSTGRES_K8S_USER_DEFAULT = "postgres"

# Milvus
ZILLIZ_ENDPOINT_DEFAULT = "https://in03-0afab716eb46d7f.serverless.gcp-us-west1.cloud.zilliz.com"
MILVUS_DEFAULT_COLLECTION = "atenex_collection"
MILVUS_DEFAULT_EMBEDDING_FIELD = "embedding"
MILVUS_DEFAULT_CONTENT_FIELD = "content"
MILVUS_DEFAULT_COMPANY_ID_FIELD = "company_id"
MILVUS_DEFAULT_DOCUMENT_ID_FIELD = "document_id"
MILVUS_DEFAULT_FILENAME_FIELD = "file_name"
MILVUS_DEFAULT_GRPC_TIMEOUT = 15
MILVUS_DEFAULT_SEARCH_PARAMS = {"metric_type": "IP", "params": {"nprobe": 10}}
MILVUS_DEFAULT_METADATA_FIELDS = ["company_id", "document_id", "file_name", "page", "title"]

# Embedding Service
EMBEDDING_SERVICE_K8S_URL_DEFAULT = "http://embedding-service.nyro-develop.svc.cluster.local:80" # Puerto 80 por el service K8s
# Reranker Service
RERANKER_SERVICE_K8S_URL_DEFAULT = "http://reranker-service.nyro-develop.svc.cluster.local:80" # Puerto 80 por el service K8s
# Sparse Search Service
SPARSE_SEARCH_SERVICE_K8S_URL_DEFAULT = "http://sparse-search-service.nyro-develop.svc.cluster.local:80" # Puerto 80 por el service K8s


# --- Paths for Prompt Templates ---
PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
DEFAULT_RAG_PROMPT_TEMPLATE_PATH = str(PROMPT_DIR / "rag_template_gemini_v2.txt")
DEFAULT_GENERAL_PROMPT_TEMPLATE_PATH = str(PROMPT_DIR / "general_template_gemini_v2.txt")
DEFAULT_MAP_PROMPT_TEMPLATE_PATH = str(PROMPT_DIR / "map_prompt_template.txt")
DEFAULT_REDUCE_PROMPT_TEMPLATE_PATH = str(PROMPT_DIR / "reduce_prompt_template_v2.txt")


# Models
DEFAULT_EMBEDDING_DIMENSION = 1536
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-preview-04-17" 

# RAG Pipeline Parameters
DEFAULT_RETRIEVER_TOP_K = 200 
DEFAULT_BM25_ENABLED: bool = True
DEFAULT_RERANKER_ENABLED: bool = True
DEFAULT_DIVERSITY_FILTER_ENABLED: bool = False 
DEFAULT_MAX_CONTEXT_CHUNKS: int = 200 
DEFAULT_HYBRID_ALPHA: float = 0.5
DEFAULT_DIVERSITY_LAMBDA: float = 0.5
DEFAULT_MAX_PROMPT_TOKENS: int = 524288 
DEFAULT_MAX_CHAT_HISTORY_MESSAGES = 20 
DEFAULT_NUM_SOURCES_TO_SHOW = 7

# MapReduce settings
DEFAULT_MAPREDUCE_ENABLED: bool = True 
DEFAULT_MAPREDUCE_CHUNK_BATCH_SIZE: int = 5
DEFAULT_MAPREDUCE_ACTIVATION_THRESHOLD: int = 25 


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

    # --- Vector Store (Milvus/Zilliz) ---
    ZILLIZ_API_KEY: SecretStr = Field(description="API Key for Zilliz Cloud connection.")
    MILVUS_URI: AnyHttpUrl = Field(default_factory=lambda: AnyHttpUrl(ZILLIZ_ENDPOINT_DEFAULT))

    @field_validator('MILVUS_URI', mode='before')
    @classmethod
    def validate_milvus_uri(cls, v: Any) -> AnyHttpUrl:
        if not isinstance(v, str):
            raise ValueError("MILVUS_URI must be a string.")
        v_strip = v.strip()
        if not v_strip.startswith("https://"): # Zilliz Cloud URIs son https
            raise ValueError(f"Invalid Zilliz URI: Must start with https://. Received: '{v_strip}'")
        try:
            validated_url = AnyHttpUrl(v_strip)
            return validated_url
        except Exception as e:
            raise ValueError(f"Invalid Milvus URI format '{v_strip}': {e}") from e

    @field_validator('ZILLIZ_API_KEY', mode='before')
    @classmethod
    def check_zilliz_key(cls, v: Any, info: ValidationInfo) -> Any:
        if isinstance(v, SecretStr):
            secret_value = v.get_secret_value()
            if secret_value is None or secret_value == "":
                raise ValueError(f"Required secret field 'QUERY_ZILLIZ_API_KEY' cannot be empty.")
        elif v is None or v == "":
            raise ValueError(f"Required secret field 'QUERY_ZILLIZ_API_KEY' cannot be empty.")
        return v

    MILVUS_COLLECTION_NAME: str = Field(default=MILVUS_DEFAULT_COLLECTION)
    MILVUS_EMBEDDING_FIELD: str = Field(default=MILVUS_DEFAULT_EMBEDDING_FIELD)
    MILVUS_CONTENT_FIELD: str = Field(default=MILVUS_DEFAULT_CONTENT_FIELD)
    MILVUS_COMPANY_ID_FIELD: str = Field(default=MILVUS_DEFAULT_COMPANY_ID_FIELD)
    MILVUS_DOCUMENT_ID_FIELD: str = Field(default=MILVUS_DEFAULT_DOCUMENT_ID_FIELD)
    MILVUS_FILENAME_FIELD: str = Field(default=MILVUS_DEFAULT_FILENAME_FIELD)
    MILVUS_METADATA_FIELDS: List[str] = Field(default=MILVUS_DEFAULT_METADATA_FIELDS)
    MILVUS_GRPC_TIMEOUT: int = Field(default=MILVUS_DEFAULT_GRPC_TIMEOUT)
    MILVUS_SEARCH_PARAMS: Dict[str, Any] = Field(default_factory=lambda: MILVUS_DEFAULT_SEARCH_PARAMS.copy())

    # --- Embedding Settings (General) ---
    EMBEDDING_DIMENSION: int = Field(default=DEFAULT_EMBEDDING_DIMENSION, description="Dimension of embeddings, used for Milvus and validation.")

    # --- External Embedding Service ---
    EMBEDDING_SERVICE_URL: AnyHttpUrl = Field(default_factory=lambda: AnyHttpUrl(EMBEDDING_SERVICE_K8S_URL_DEFAULT), description="URL of the Atenex Embedding Service.")
    EMBEDDING_CLIENT_TIMEOUT: int = Field(default=30, description="Timeout in seconds for calls to the Embedding Service.")

    # --- LLM (Google Gemini) ---
    GEMINI_API_KEY: SecretStr
    GEMINI_MODEL_NAME: str = Field(default=DEFAULT_GEMINI_MODEL)

    # --- Reranker Settings ---
    RERANKER_ENABLED: bool = Field(default=DEFAULT_RERANKER_ENABLED)
    RERANKER_SERVICE_URL: AnyHttpUrl = Field(default_factory=lambda: AnyHttpUrl(RERANKER_SERVICE_K8S_URL_DEFAULT), description="URL of the Atenex Reranker Service.")
    RERANKER_CLIENT_TIMEOUT: int = Field(default=30, description="Timeout in seconds for calls to the Reranker Service.")

    # --- Sparse Retriever (Remote Service) ---
    BM25_ENABLED: bool = Field(default=DEFAULT_BM25_ENABLED, description="Enables/disables the sparse search step (uses sparse-search-service).")
    SPARSE_SEARCH_SERVICE_URL: AnyHttpUrl = Field(default_factory=lambda: AnyHttpUrl(SPARSE_SEARCH_SERVICE_K8S_URL_DEFAULT), description="URL of the Atenex Sparse Search Service.")
    SPARSE_SEARCH_CLIENT_TIMEOUT: int = Field(default=30, description="Timeout for calls to Sparse Search Service.")


    # --- Diversity Filter ---
    DIVERSITY_FILTER_ENABLED: bool = Field(default=DEFAULT_DIVERSITY_FILTER_ENABLED)
    MAX_CONTEXT_CHUNKS: int = Field(default=DEFAULT_MAX_CONTEXT_CHUNKS, gt=0, description="Max number of retrieved/reranked chunks to pass to LLM context.")
    QUERY_DIVERSITY_LAMBDA: float = Field(default=DEFAULT_DIVERSITY_LAMBDA, ge=0.0, le=1.0, description="Lambda for MMR diversity (0=max diversity, 1=max relevance).")

    # --- RAG Pipeline Parameters ---
    RETRIEVER_TOP_K: int = Field(default=DEFAULT_RETRIEVER_TOP_K, gt=0, le=500)
    HYBRID_FUSION_ALPHA: float = Field(default=DEFAULT_HYBRID_ALPHA, ge=0.0, le=1.0)
    
    # Prompt template paths
    RAG_PROMPT_TEMPLATE_PATH: str = Field(default=DEFAULT_RAG_PROMPT_TEMPLATE_PATH)
    GENERAL_PROMPT_TEMPLATE_PATH: str = Field(default=DEFAULT_GENERAL_PROMPT_TEMPLATE_PATH)
    MAP_PROMPT_TEMPLATE_PATH: str = Field(default=DEFAULT_MAP_PROMPT_TEMPLATE_PATH)
    REDUCE_PROMPT_TEMPLATE_PATH: str = Field(default=DEFAULT_REDUCE_PROMPT_TEMPLATE_PATH)

    MAX_PROMPT_TOKENS: Optional[int] = Field(default=DEFAULT_MAX_PROMPT_TOKENS)
    MAX_CHAT_HISTORY_MESSAGES: int = Field(default=DEFAULT_MAX_CHAT_HISTORY_MESSAGES, ge=0)
    NUM_SOURCES_TO_SHOW: int = Field(default=DEFAULT_NUM_SOURCES_TO_SHOW, ge=0)

    # --- MapReduce Settings ---
    MAPREDUCE_ENABLED: bool = Field(default=DEFAULT_MAPREDUCE_ENABLED)
    MAPREDUCE_CHUNK_BATCH_SIZE: int = Field(default=DEFAULT_MAPREDUCE_CHUNK_BATCH_SIZE, gt=0)
    MAPREDUCE_ACTIVATION_THRESHOLD: int = Field(default=DEFAULT_MAPREDUCE_ACTIVATION_THRESHOLD, gt=0)


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
        if isinstance(v, SecretStr):
            secret_value = v.get_secret_value()
            if secret_value is None or secret_value == "":
                raise ValueError(f"Required secret field 'QUERY_{info.field_name.upper()}' cannot be empty.")
        elif v is None or v == "":
            raise ValueError(f"Required secret field 'QUERY_{info.field_name.upper()}' cannot be empty.")
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
        if v > retriever_k * 2 and info.data.get('MAPREDUCE_ENABLED', DEFAULT_MAPREDUCE_ENABLED) is False:
             logging.warning(f"MAX_CONTEXT_CHUNKS ({v}) for direct RAG is significantly larger than typical fused results from RETRIEVER_TOP_K ({retriever_k}). Ensure this is intended.")
        if v <= 0:
             raise ValueError("MAX_CONTEXT_CHUNKS must be a positive integer.")
        return v
    
    @field_validator('MAPREDUCE_CHUNK_BATCH_SIZE')
    @classmethod
    def check_mapreduce_batch_size(cls, v: int, info: ValidationInfo) -> int:
        if v <=0:
            raise ValueError("MAPREDUCE_CHUNK_BATCH_SIZE must be positive.")
        if v > 20: 
            logging.warning(f"MAPREDUCE_CHUNK_BATCH_SIZE ({v}) is quite large. Ensure LLM can handle this many docs in a single map prompt.")
        return v
        
    @field_validator('MAPREDUCE_ACTIVATION_THRESHOLD')
    @classmethod
    def check_mapreduce_activation_threshold(cls, v: int, info: ValidationInfo) -> int:
        max_context = info.data.get('MAX_CONTEXT_CHUNKS', DEFAULT_MAX_CONTEXT_CHUNKS)
        if v > max_context:
            logging.warning(f"MAPREDUCE_ACTIVATION_THRESHOLD ({v}) is greater than MAX_CONTEXT_CHUNKS ({max_context}). MapReduce may never activate if MAX_CONTEXT_CHUNKS is the effective limit for documents to process.")
        if v <= 0:
            raise ValueError("MAPREDUCE_ACTIVATION_THRESHOLD must be positive.")
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
    formatter = logging.Formatter('%(levelname)s: [%(asctime)s] [%(name)s] %(message)s')
    handler.setFormatter(formatter)
    temp_log.addHandler(handler)
    temp_log.setLevel(logging.INFO)

try:
    temp_log.info("Loading Query Service settings...")
    settings = Settings()
    temp_log.info("--- Query Service Settings Loaded ---")
    
    excluded_fields = {'POSTGRES_PASSWORD', 'GEMINI_API_KEY', 'ZILLIZ_API_KEY'}
    log_data = settings.model_dump(exclude=excluded_fields)

    for key, value in log_data.items():
        if key.endswith("_PATH"): 
            try:
                path_obj = Path(value)
                status_msg = "Present and readable" if path_obj.is_file() and os.access(path_obj, os.R_OK) else "!!! NOT FOUND or UNREADABLE !!!"
                temp_log.info(f"  {key.upper()}: {value} (Status: {status_msg})")
            except Exception as path_e:
                temp_log.info(f"  {key.upper()}: {value} (Status: Error checking path: {path_e})")
        else:
            temp_log.info(f"  {key.upper()}: {value}")

    pg_pass_status = '*** SET ***' if settings.POSTGRES_PASSWORD and settings.POSTGRES_PASSWORD.get_secret_value() else '!!! NOT SET !!!'
    temp_log.info(f"  POSTGRES_PASSWORD:            {pg_pass_status}")
    gemini_key_status = '*** SET ***' if settings.GEMINI_API_KEY and settings.GEMINI_API_KEY.get_secret_value() else '!!! NOT SET !!!'
    temp_log.info(f"  GEMINI_API_KEY:               {gemini_key_status}")
    zilliz_api_key_status = '*** SET ***' if settings.ZILLIZ_API_KEY and settings.ZILLIZ_API_KEY.get_secret_value() else '!!! NOT SET !!!'
    temp_log.info(f"  ZILLIZ_API_KEY:               {zilliz_api_key_status}")
    temp_log.info(f"------------------------------------")

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
from pydantic import BaseModel, Field, ConfigDict
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
    sources: Optional[List[Dict[str, Any]]] = None 
    created_at: datetime

class RetrievedChunk(BaseModel):
    """Representa un chunk recuperado de una fuente (ej: Milvus)."""
    id: str 
    content: Optional[str] = None 
    score: Optional[float] = None 
    metadata: Dict[str, Any] = Field(default_factory=dict)
    embedding: Optional[List[float]] = None 
    
    document_id: Optional[str] = Field(None, alias="document_id") 
    file_name: Optional[str] = Field(None, alias="file_name")
    company_id: Optional[str] = Field(None, alias="company_id")

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)


    @classmethod
    def from_haystack_document(cls, doc: Any):
        """Convierte un Documento Haystack a un RetrievedChunk."""
        doc_meta = doc.meta or {}
        doc_id_str = str(doc_meta.get("document_id")) if doc_meta.get("document_id") else None
        company_id_str = str(doc_meta.get("company_id")) if doc_meta.get("company_id") else None
        embedding_vector = getattr(doc, 'embedding', None)

        return cls(
            id=str(doc.id),
            content=doc.content,
            score=doc.score,
            metadata=doc_meta,
            embedding=embedding_vector, 
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

# --- Nuevos modelos para Respuesta Estructurada ---
class FuenteCitada(BaseModel):
    id_documento: Optional[str] = None
    nombre_archivo: str = Field(..., description="Nombre del archivo fuente.")
    pagina: Optional[str] = None
    score: Optional[float] = None
    cita_tag: str = Field(..., description="La etiqueta de cita usada en el texto, ej: '[Doc 1]'.")
    
    model_config = ConfigDict(extra='ignore')


class RespuestaEstructurada(BaseModel):
    resumen_ejecutivo: Optional[str] = None
    respuesta_detallada: str = Field(..., description="La respuesta completa y elaborada, incluyendo citas [Doc N] donde corresponda.")
    fuentes_citadas: List[FuenteCitada] # El LLM debe devolverla, incluso vacía
    siguiente_pregunta_sugerida: Optional[str] = None
    
    model_config = ConfigDict(extra='ignore') 

class SparseSearchResultItem(BaseModel):
    """
    Representa un ítem de resultado de búsqueda dispersa devuelto por el sparse-search-service.
    Este modelo se utiliza en el query-service para tipar la respuesta del cliente
    de dicho servicio.
    """
    chunk_id: str = Field(..., description="El ID del chunk (usualmente el embedding_id de la tabla document_chunks).")
    score: float = Field(..., description="El score BM25 asignado al chunk.")
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
import json # FLAG_CORRECTION: Import json

from app.core.config import settings

log = structlog.get_logger(__name__)

class EmbeddingServiceClient:
    """
    Cliente HTTP para interactuar con el Atenex Embedding Service.
    """
    def __init__(self, base_url: str, timeout: int = settings.HTTP_CLIENT_TIMEOUT):
        self.base_url = base_url.rstrip('/')
        self.embed_endpoint = f"{self.base_url}/api/v1/embed" 
        self.health_endpoint = f"{self.base_url}/health"

        if "/api/v1" in self.base_url.split('/')[-2:]:
             self.embed_endpoint = f"{self.base_url}/embed"
        else: 
             self.embed_endpoint = f"{self.base_url}/api/v1/embed"


        self._client = httpx.AsyncClient(timeout=timeout)
        log.info("EmbeddingServiceClient initialized", base_url=self.base_url, embed_endpoint=self.embed_endpoint)

    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES + 1),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=1, max=10),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError)),
        reraise=True 
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
            response.raise_for_status() 

            data = response.json()
            if "embeddings" not in data or not isinstance(data["embeddings"], list):
                client_log.error("Invalid response format from embedding service: 'embeddings' field missing or not a list.", response_data=data)
                raise ValueError("Invalid response format from embedding service: 'embeddings' field.")

            client_log.info("Embeddings received successfully from service", num_embeddings=len(data["embeddings"]))
            return data["embeddings"]

        except httpx.HTTPStatusError as e:
            client_log.error("HTTP error from embedding service", status_code=e.response.status_code, response_body=e.response.text)
            raise ConnectionError(f"Embedding service returned error {e.response.status_code}: {e.response.text}") from e
        except httpx.RequestError as e:
            client_log.error("Request error while contacting embedding service", error=str(e))
            raise ConnectionError(f"Could not connect to embedding service: {e}") from e
        except json.JSONDecodeError as e_json: # FLAG_CORRECTION: Catch json.JSONDecodeError
            client_log.error("Error parsing JSON response from embedding service", error=str(e_json), raw_response=response.text if 'response' in locals() else "N/A")
            raise ValueError(f"Invalid JSON response from embedding service: {e_json}") from e_json
        except (ValueError, TypeError) as e: 
            client_log.error("Error processing response from embedding service (ValueError/TypeError)", error=str(e))
            raise ValueError(f"Invalid response data from embedding service: {e}") from e


    async def get_model_info(self) -> Optional[Dict[str, Any]]:
        client_log = log.bind(action="get_model_info_via_embed", target_service="embedding-service")
        try:
            response = await self._client.post(self.embed_endpoint, json={"texts": ["test"]}) 
            response.raise_for_status()
            data = response.json()
            if "model_info" in data and isinstance(data["model_info"], dict):
                client_log.info("Model info retrieved from embedding service", model_info=data["model_info"])
                return data["model_info"]
            client_log.warning("Model info not found in embedding service response.", response_data=data)
            return None
        except json.JSONDecodeError as e_json: # FLAG_CORRECTION: Catch json.JSONDecodeError
            client_log.error("Failed to parse JSON for get_model_info from embedding service", error=str(e_json), raw_response=response.text if 'response' in locals() else "N/A")
            return None
        except Exception as e:
            client_log.error("Failed to get model_info from embedding service via /embed", error=str(e))
            return None

    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES + 1),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=1, max=5), 
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError, ConnectionError)),
        reraise=True,
        before_sleep=lambda retry_state: log.warning(
            "Retrying EmbeddingServiceClient.check_health",
            attempt=retry_state.attempt_number,
            wait_time=f"{retry_state.next_action.sleep:.2f}s", # type: ignore
            error_type=type(retry_state.outcome.exception()).__name__ if retry_state.outcome else "N/A", # type: ignore
            error_message=str(retry_state.outcome.exception()) if retry_state.outcome else "N/A" # type: ignore
        )
    )
    async def check_health(self) -> bool:
        client_log = log.bind(action="check_health_with_retry", target_service="embedding-service")
        try:
            client_log.debug("Attempting health check...")
            response = await self._client.get(self.health_endpoint)
            response.raise_for_status() 

            data = response.json()
            # FLAG_CORRECTION: Accept 'client_ready' or 'loaded' as valid model_status
            model_is_ready = data.get("model_status") in ["loaded", "client_ready"]
            if data.get("status") == "ok" and model_is_ready:
                client_log.info("Embedding service health check successful.", health_data=data)
                return True
            else:
                client_log.warning("Embedding service health check returned ok status but model not fully ready or unexpected payload.", health_data=data)
                raise ConnectionError(f"Embedding service not fully ready: status={data.get('status')}, model_status={data.get('model_status')}")
        except httpx.HTTPStatusError as e:
            client_log.warning("HTTP error during embedding service health check (will be retried or reraised).", status_code=e.response.status_code, response_text=e.response.text)
            raise ConnectionError(f"HTTP error from embedding service: {e.response.status_code}") from e 
        except httpx.RequestError as e:
            client_log.error("Request error during embedding service health check (will be retried or reraised).", error=str(e))
            raise ConnectionError(f"Request error connecting to embedding service: {e}") from e
        except json.JSONDecodeError as e_json: # FLAG_CORRECTION: Catch json.JSONDecodeError
            client_log.error("Failed to parse JSON response from embedding service health check.", error=str(e_json), raw_response=response.text if 'response' in locals() else "N/A")
            raise ConnectionError(f"Invalid JSON response from embedding service health: {e_json}") from e_json
        except Exception as e: 
            client_log.error("Unexpected error during embedding service health check (will be retried or reraised).", error=str(e))
            raise ConnectionError(f"Unexpected error during health check: {e}") from e

    async def close(self):
        await self._client.aclose()
        log.info("EmbeddingServiceClient closed.")
```

## File: `app\infrastructure\clients\sparse_search_service_client.py`
```py
# query-service/app/infrastructure/clients/sparse_search_service_client.py
import httpx
import structlog
import uuid
from typing import List, Dict, Any, Tuple, Optional
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.core.config import settings # Para timeouts y URL base
from app.domain.models import SparseSearchResultItem # Para el tipo de retorno del servicio

log = structlog.get_logger(__name__)

class SparseSearchServiceClient:
    """
    Cliente HTTP para interactuar con el Atenex Sparse Search Service.
    """
    def __init__(self, base_url: str, timeout: int = settings.HTTP_CLIENT_TIMEOUT):
        # Asegurar que la URL base no tenga /api/v1 al final si el endpoint ya lo incluye
        self.base_url = base_url.rstrip('/')
        # El endpoint del sparse-search-service es /api/v1/search
        self.search_endpoint = f"{self.base_url}/api/v1/search"
        self.health_endpoint = f"{self.base_url}/health"

        # Validar que la URL base no incluya /api/v1 si el endpoint ya lo hace.
        # Ejemplo: si base_url es http://service/api/v1, endpoint es http://service/api/v1/search
        # Ejemplo: si base_url es http://service, endpoint es http://service/api/v1/search
        if self.base_url.endswith("/api/v1"):
            self.search_endpoint = f"{self.base_url.rsplit('/api/v1', 1)[0]}/api/v1/search"
        elif self.base_url.endswith("/api"):
             self.search_endpoint = f"{self.base_url.rsplit('/api', 1)[0]}/api/v1/search"

        self._client = httpx.AsyncClient(timeout=timeout)
        log.info("SparseSearchServiceClient initialized",
                 base_url=self.base_url,
                 search_endpoint=self.search_endpoint,
                 health_endpoint=self.health_endpoint)

    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES + 1),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=1, max=10),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError, httpx.ConnectError)),
        reraise=True
    )
    async def search(self, query_text: str, company_id: uuid.UUID, top_k: int) -> List[SparseSearchResultItem]:
        """
        Solicita una búsqueda dispersa al sparse-search-service.
        Devuelve una lista de SparseSearchResultItem del dominio.
        """
        client_log = log.bind(action="sparse_search_remote",
                              company_id=str(company_id),
                              query_preview=query_text[:50]+"...",
                              top_k=top_k,
                              target_service="sparse-search-service")
        if not query_text:
            client_log.warning("No query text provided for sparse search.")
            return []

        payload = {
            "query": query_text,
            "company_id": str(company_id), # El servicio espera un UUID string en JSON
            "top_k": top_k
        }
        try:
            client_log.debug("Sending request to sparse search service")
            response = await self._client.post(self.search_endpoint, json=payload)
            response.raise_for_status()

            data = response.json()
            
            if "results" not in data or not isinstance(data["results"], list):
                client_log.error("Invalid response format from sparse search service: 'results' field missing or not a list.", response_data=data)
                raise ValueError("Invalid response format from sparse search service: 'results' field.")

            # Mapear a SparseSearchResultItem del dominio
            domain_results = []
            for item_data in data["results"]:
                # El servicio sparse-search ya devuelve items que coinciden con SparseSearchResultItem
                # así que podemos instanciarlos directamente si el schema coincide.
                # Asumimos que 'chunk_id' y 'score' están presentes.
                domain_results.append(SparseSearchResultItem(**item_data))
            
            client_log.info("Sparse search results received successfully from service", num_results=len(domain_results))
            return domain_results

        except httpx.HTTPStatusError as e:
            client_log.error("HTTP error from sparse search service", status_code=e.response.status_code, response_body=e.response.text)
            raise ConnectionError(f"Sparse search service returned error {e.response.status_code}: {e.response.text}") from e
        except httpx.RequestError as e:
            client_log.error("Request error while contacting sparse search service", error=str(e))
            raise ConnectionError(f"Could not connect to sparse search service: {e}") from e
        except (ValueError, TypeError, AttributeError) as e: # Errores de parsing JSON o validación de Pydantic
            client_log.error("Error processing response from sparse search service", error=str(e))
            raise ValueError(f"Invalid response or data from sparse search service: {e}") from e

    async def check_health(self) -> bool:
        client_log = log.bind(action="check_health_sparse_search", target_service="sparse-search-service")
        try:
            response = await self._client.get(self.health_endpoint, timeout=5) # Shorter timeout for health
            if response.status_code == 200:
                data = response.json()
                # El health check del sparse-search-service devuelve un JSON con `status` y `ready`
                if data.get("status") == "ok" and data.get("ready") is True:
                    client_log.info("Sparse search service health check successful.", health_data=data)
                    return True
                else:
                    client_log.warning("Sparse search service health check returned ok status but service/dependencies not ready.", health_data=data)
                    return False
            else:
                client_log.warning("Sparse search service health check failed.", status_code=response.status_code, response_text=response.text)
                return False
        except httpx.RequestError as e:
            client_log.error("Error connecting to sparse search service for health check.", error=str(e))
            return False
        except Exception as e:
            client_log.error("Unexpected error during sparse search service health check.", error=str(e))
            return False

    async def close(self):
        await self._client.aclose()
        log.info("SparseSearchServiceClient closed.")
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
from google.generativeai import types as genai_types
import structlog
from typing import Optional, List, Type, Any, Dict
from pydantic import BaseModel
import json

from app.core.config import settings
from app.application.ports.llm_port import LLMPort
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from app.utils.helpers import truncate_text


log = structlog.get_logger(__name__)

def _clean_pydantic_schema_for_gemini_response(pydantic_schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Limpia un esquema JSON generado por Pydantic para que sea compatible con
    el parámetro `response_schema` de Gemini `GenerationConfig`.
    Principalmente, elimina campos "default" y ajusta tipos.
    """
    
    # Referencias internas que Gemini podría no entender bien, aunque para response_schema
    # Pydantic suele aplanar bastante bien.
    definitions = pydantic_schema.get("$defs", {})

    schema_copy = {
        k: v
        for k, v in pydantic_schema.items()
        # Quitar $defs y title/description del nivel raíz, ya que response_schema es el tipo mismo
        if k not in {"$defs", "title", "description", "$schema"} 
    }

    def resolve_ref(ref_path: str) -> Dict[str, Any]:
        if not ref_path.startswith("#/$defs/"):
            return {"type": "OBJECT"} # Fallback si es una referencia externa no esperada
        
        def_key = ref_path.split("/")[-1]
        if def_key in definitions:
            return _transform_node(definitions[def_key])
        else:
            log.warning(f"Broken JSON schema reference found and could not be resolved: {ref_path}")
            return {"type": "OBJECT"} # Fallback

    def _transform_node(node: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(node, dict):
            return node

        transformed_node = {}
        for key, value in node.items():
            # Eliminar campos problemáticos como 'default' o que no son parte del schema de OpenAPI que Gemini usa
            if key in {"default", "examples", "example", "const", "title", "description"}:
                continue
            
            if key == "$ref" and isinstance(value, str):
                 # Si es una referencia, resolverla y usar el resultado transformado.
                 # El esquema de respuesta para Gemini no usa $ref, espera el objeto anidado.
                 return resolve_ref(value)

            elif key == "anyOf" and isinstance(value, list):
                # Simplificar anyOf para opcionales: anyOf: [ <type>, {"type": "null"} ] a <type> con nullable: true
                is_optional_pattern = False
                if len(value) == 2:
                    type_def_item = next((item for item in value if isinstance(item, dict) and item.get("type") != "null"), None)
                    null_def_item = next((item for item in value if isinstance(item, dict) and item.get("type") == "null"), None)
                    
                    if type_def_item and null_def_item:
                        is_optional_pattern = True
                        # Tomar el tipo no nulo y transformarlo
                        transformed_type_def = _transform_node(type_def_item)
                        # Copiar las propiedades del tipo real al nodo actual
                        for k_type, v_type in transformed_type_def.items():
                             transformed_node[k_type] = v_type
                        transformed_node["nullable"] = True 
                
                if not is_optional_pattern: 
                    # Si no es el patrón opcional, es más complejo y podría no ser soportado directamente.
                    # Por simplicidad, podríamos tomar el primer tipo o loguear un aviso.
                    # Para response_schema, es mejor que el Pydantic model sea simple.
                    if value:
                        first_type = _transform_node(value[0])
                        for k_first, v_first in first_type.items():
                            transformed_node[k_first] = v_first
                        log.warning("Complex 'anyOf' in Pydantic schema for Gemini response_schema, took first option.",
                                    original_anyof=value, chosen_type=first_type)
                    else:
                        log.warning("Empty 'anyOf' in Pydantic schema for Gemini response_schema.", original_anyof=value)
                continue 

            elif isinstance(value, dict):
                transformed_node[key] = _transform_node(value)
            elif isinstance(value, list) and key not in ["enum", "required"]: 
                transformed_node[key] = [_transform_node(item) if isinstance(item, dict) else item for item in value]
            else:
                transformed_node[key] = value
        
        # Convertir 'type' a MAYÚSCULAS según la especificación OpenAPI que usa Gemini
        if "type" in transformed_node:
            json_type = transformed_node["type"]
            if isinstance(json_type, list): # type: ["string", "null"]
                if "null" in json_type:
                    transformed_node["nullable"] = True 
                actual_type = next((t for t in json_type if t != "null"), "OBJECT") # Default to OBJECT if only null
                if isinstance(actual_type, str):
                    transformed_node["type"] = actual_type.upper()
                else: # Podría ser un sub-esquema (raro para listas de tipos)
                    transformed_node["type"] = _transform_node(actual_type).get("type", "OBJECT")

            elif isinstance(json_type, str):
                transformed_node["type"] = json_type.upper()
            
            # Gemini espera que el tipo para 'array' sea 'ARRAY', no 'LIST' (si Pydantic lo genera así)
            if transformed_node["type"] == "LIST": # Pydantic puede usar 'list'
                transformed_node["type"] = "ARRAY"


        # Asegurar que `items` exista para tipo ARRAY
        if transformed_node.get("type") == "ARRAY" and "items" not in transformed_node:
            log.warning("Schema for ARRAY type missing 'items' definition for Gemini. Adding generic object item.", node_details=transformed_node)
            transformed_node["items"] = {"type": "OBJECT"} # Fallback genérico

        return transformed_node

    # Aplicar la transformación al schema raíz
    final_schema = _transform_node(schema_copy)
    
    log.debug("Cleaned Pydantic JSON Schema for Gemini response_schema", original_schema=pydantic_schema, cleaned_schema=final_schema)
    return final_schema


class GeminiAdapter(LLMPort):
    _api_key: str
    _model_name: str
    _model: Optional[genai.GenerativeModel] = None 

    def __init__(self):
        self._api_key = settings.GEMINI_API_KEY.get_secret_value()
        self._model_name = settings.GEMINI_MODEL_NAME
        self._configure_client()

    def _configure_client(self):
        try:
            if self._api_key:
                genai.configure(api_key=self._api_key)
                self._model = genai.GenerativeModel(self._model_name)
                log.info("Gemini client configured successfully using GenerativeModel", model_name=self._model_name)
            else:
                log.warning("Gemini API key is missing. Client not configured.")
        except Exception as e:
            log.error("Failed to configure Gemini client (GenerativeModel)", error=str(e), exc_info=True)
            self._model = None
    
    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES + 1),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=2, max=10),
        retry=retry_if_exception_type((
            TimeoutError,
            # Se pueden agregar aquí errores específicos de google.api_core.exceptions si se identifican
            # como errores de red o servicio transitorios que justifiquen un reintento.
        )),
        reraise=True,
        before_sleep=lambda retry_state: log.warning(
            "Retrying Gemini API call",
            attempt=retry_state.attempt_number,
            wait_time=f"{retry_state.next_action.sleep:.2f}s", 
            error_type=type(retry_state.outcome.exception()).__name__ if retry_state.outcome else "N/A", 
            error_message=str(retry_state.outcome.exception()) if retry_state.outcome else "N/A" 
        )
    )
    async def generate(self, prompt: str,
                       response_pydantic_schema: Optional[Type[BaseModel]] = None
                      ) -> str:
        if not self._model:
            log.error("Gemini client (GenerativeModel) not initialized. Cannot generate answer.")
            raise ConnectionError("Gemini client is not properly configured (missing API key or init failed).")

        generate_log = log.bind(
            adapter="GeminiAdapter",
            model_name=self._model_name,
            prompt_length=len(prompt),
            expecting_json=bool(response_pydantic_schema)
        )

        generation_config_parts: Dict[str, Any] = {
            "temperature": 0.6, 
            "top_p": 0.9,
        }
        
        if response_pydantic_schema:
            generation_config_parts["response_mime_type"] = "application/json"
            
            # Obtener el esquema JSON del modelo Pydantic
            pydantic_schema_json = response_pydantic_schema.model_json_schema()
            # Limpiar el esquema para Gemini
            cleaned_schema_for_gemini = _clean_pydantic_schema_for_gemini_response(pydantic_schema_json)
            
            generation_config_parts["response_schema"] = cleaned_schema_for_gemini
            generate_log.debug("Configured Gemini for JSON output using cleaned response_schema.", 
                               schema_name=response_pydantic_schema.__name__,
                               cleaned_schema=cleaned_schema_for_gemini)
        
        final_generation_config = genai_types.GenerationConfig(**generation_config_parts)
        
        try:
            call_kwargs: Dict[str, Any] = {"generation_config": final_generation_config}
            
            generate_log.debug("Sending request to Gemini API...")
            response = await self._model.generate_content_async(prompt, **call_kwargs)
            
            generated_text = ""

            if not response.candidates:
                 finish_reason_str = getattr(response.prompt_feedback, 'block_reason', "UNKNOWN_REASON") 
                 safety_ratings_str = str(getattr(response.prompt_feedback, 'safety_ratings', "N/A")) 
                 generate_log.warning("Gemini response potentially blocked (no candidates)",
                                      finish_reason=finish_reason_str, safety_ratings=safety_ratings_str)
                 if response_pydantic_schema:
                     return json.dumps({
                         "error_message": f"Respuesta bloqueada por Gemini (sin candidatos). Razón: {finish_reason_str}",
                         "respuesta_detallada": f"La generación de la respuesta fue bloqueada. Por favor, reformula tu pregunta o contacta a soporte si el problema persiste. Razón: {finish_reason_str}.",
                         "fuentes_citadas": []
                     })
                 return f"[Respuesta bloqueada por Gemini (sin candidatos). Razón: {finish_reason_str}]"

            candidate = response.candidates[0]

            if not candidate.content or not candidate.content.parts:
                finish_reason_cand_str = getattr(candidate, 'finish_reason', "UNKNOWN_REASON")
                safety_ratings_cand_str = str(getattr(candidate, 'safety_ratings', "N/A"))
                generate_log.warning("Gemini response candidate empty or missing parts",
                                     candidate_finish_reason=finish_reason_cand_str,
                                     candidate_safety_ratings=safety_ratings_cand_str)
                if response_pydantic_schema:
                     return json.dumps({
                         "error_message": f"Respuesta vacía de Gemini (candidato sin contenido). Razón: {finish_reason_cand_str}",
                         "respuesta_detallada": f"El asistente no pudo generar una respuesta completa. Razón: {finish_reason_cand_str}.",
                         "fuentes_citadas": []
                     })
                return f"[Respuesta vacía de Gemini (candidato sin contenido). Razón: {finish_reason_cand_str}]"
            
            if candidate.content.parts[0].text:
                generated_text = candidate.content.parts[0].text
            else:
                generate_log.error("Gemini response part exists but has no text content.")
                if response_pydantic_schema:
                    return json.dumps({
                        "error_message": "Respuesta del LLM incompleta o en formato inesperado.",
                        "respuesta_detallada": "Error: El asistente devolvió una respuesta sin contenido textual.",
                        "fuentes_citadas": []
                    })
                return "[Respuesta del LLM incompleta o sin contenido textual]"

            if response_pydantic_schema:
                generate_log.debug("Received potential JSON text from Gemini API.", response_length=len(generated_text))
            else: 
                generate_log.debug("Received plain text response from Gemini API", response_length=len(generated_text))
                
            return generated_text.strip()

        except (genai_types.generation_types.BlockedPromptException, genai_types.generation_types.StopCandidateException) as security_err: 
            finish_reason_err_str = getattr(security_err, 'finish_reason', 'N/A') if hasattr(security_err, 'finish_reason') else 'Unknown security block'
            generate_log.warning("Gemini request blocked or stopped due to safety/policy.",
                                 error_type=type(security_err).__name__,
                                 error_details=str(security_err),
                                 finish_reason=finish_reason_err_str)
            if response_pydantic_schema:
                return json.dumps({
                    "error_message": f"Contenido bloqueado o detenido por Gemini: {type(security_err).__name__}",
                    "respuesta_detallada": f"La generación de la respuesta fue bloqueada o detenida por políticas de contenido. Por favor, ajusta tu consulta. (Razón: {finish_reason_err_str})",
                    "fuentes_citadas": []
                })
            return f"[Contenido bloqueado o detenido por Gemini: {type(security_err).__name__}. Razón: {finish_reason_err_str}]"
        except Exception as e: 
            generate_log.exception("Unhandled error during Gemini API call")
            if response_pydantic_schema: 
                return json.dumps({
                    "error_message": f"Error inesperado en la API de Gemini: {type(e).__name__}",
                    "respuesta_detallada": f"Error interno al comunicarse con el asistente: {type(e).__name__} - {str(e)[:100]}.",
                    "fuentes_citadas": []
                })
            raise ConnectionError(f"Gemini API call failed unexpectedly: {e}") from e
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

from app.core.config import settings
from app.api.v1 import schemas 
from app.domain.models import Chat, ChatMessage, ChatSummary, QueryLog 
from app.application.ports.repository_ports import ChatRepositoryPort, LogRepositoryPort, ChunkContentRepositoryPort 
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
            raise 

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
            return False 

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
                
                if msg_dict.get('sources') is None:
                     msg_dict['sources'] = None
                elif not isinstance(msg_dict.get('sources'), (list, dict, type(None))):
                    
                    log.warning("Unexpected type for 'sources' from DB", type=type(msg_dict['sources']).__name__, message_id=str(msg_dict.get('id')))
                    try:
                        
                        if isinstance(msg_dict['sources'], str):
                            msg_dict['sources'] = json.loads(msg_dict['sources'])
                        else:
                             msg_dict['sources'] = None 
                    except (json.JSONDecodeError, TypeError):
                         log.error("Failed to manually decode 'sources'", message_id=str(msg_dict.get('id')))
                         msg_dict['sources'] = None

                
                if not isinstance(msg_dict.get('sources'), (list, type(None))):
                    msg_dict['sources'] = None

                messages.append(ChatMessage(**msg_dict)) 

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
                
                update_chat_query = "UPDATE chats SET updated_at = NOW() AT TIME ZONE 'UTC' WHERE id = $1 RETURNING id;"
                chat_updated = await conn.fetchval(update_chat_query, chat_id)
                if not chat_updated:
                    repo_log.error("Failed to update chat timestamp, chat might not exist")
                    raise ValueError(f"Chat with ID {chat_id} not found for saving message.")

                
                insert_message_query = """
                INSERT INTO messages (id, chat_id, role, content, sources, created_at)
                VALUES ($1, $2, $3, $4, $5, NOW() AT TIME ZONE 'UTC') RETURNING id;
                """
                
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
        retrieved_documents_data: List[Dict[str, Any]], 
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
        
        final_metadata = metadata or {}
        final_metadata["retrieved_summary"] = [
            {"id": d.get("id"), "score": d.get("score"), "file_name": d.get("file_name")}
            for d in retrieved_documents_data
        ]

        try:
            async with pool.acquire() as connection:
                
                result = await connection.fetchval(
                    query_sql,
                    log_id, user_id, company_id, query, answer,
                    final_metadata, 
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
        pool = await get_db_pool()
        query = """
        SELECT dc.embedding_id, dc.content
        FROM document_chunks dc
        JOIN documents d ON dc.document_id = d.id
        WHERE d.company_id = $1 AND dc.embedding_id IS NOT NULL;
        """
        repo_log = log.bind(repo="PostgresChunkContentRepository", action="get_chunk_contents_by_company", company_id=str(company_id))
        repo_log.warning("Fetching all chunk contents (keyed by embedding_id) for company, this might be memory intensive!")
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(query, company_id)
            
            contents = {row['embedding_id']: row['content'] for row in rows if row['embedding_id'] and row['content']}
            repo_log.info(f"Retrieved content for {len(contents)} chunks (keyed by embedding_id)")
            return contents
        except Exception as e:
            repo_log.exception("Failed to get chunk contents by company (keyed by embedding_id)")
            raise

    async def get_chunk_contents_by_ids(self, chunk_ids: List[str]) -> Dict[str, str]:
        if not chunk_ids:
            return {}
        pool = await get_db_pool()
        
        # Los chunk_ids que llegan son los embedding_id (PKs de Milvus), que son strings.
        # La columna en la DB es 'embedding_id' de tipo VARCHAR.
        query = """
        SELECT embedding_id, content FROM document_chunks WHERE embedding_id = ANY($1::text[]);
        """
        repo_log = log.bind(repo="PostgresChunkContentRepository", action="get_chunk_contents_by_ids", count=len(chunk_ids))
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(query, chunk_ids) 
            
            contents = {row['embedding_id']: row['content'] for row in rows if row['embedding_id'] and row['content']}
            repo_log.info(f"Retrieved content for {len(contents)} chunks (keyed by embedding_id) out of {len(chunk_ids)} requested")
            
            if len(contents) != len(chunk_ids):
                found_ids = set(contents.keys())
                missing_ids = [cid for cid in chunk_ids if cid not in found_ids]
                repo_log.warning("Could not find content for some requested chunk IDs (embedding_ids)", missing_ids=missing_ids)
            return contents
        except Exception as e:
            repo_log.exception("Failed to get chunk contents by IDs (embedding_ids)")
            raise
```

## File: `app\infrastructure\retrievers\__init__.py`
```py

```

## File: `app\infrastructure\retrievers\remote_sparse_retriever_adapter.py`
```py
# query-service/app/infrastructure/retrievers/remote_sparse_retriever_adapter.py
import structlog
import uuid
from typing import List, Tuple

from app.application.ports.retrieval_ports import SparseRetrieverPort
from app.infrastructure.clients.sparse_search_service_client import SparseSearchServiceClient
from app.domain.models import SparseSearchResultItem # Para el tipo de resultado del cliente

log = structlog.get_logger(__name__)

class RemoteSparseRetrieverAdapter(SparseRetrieverPort):
    """
    Adaptador que utiliza SparseSearchServiceClient para realizar búsquedas dispersas
    llamando al servicio externo sparse-search-service.
    """
    def __init__(self, client: SparseSearchServiceClient):
        self.client = client
        log.info("RemoteSparseRetrieverAdapter initialized")

    async def search(self, query: str, company_id: uuid.UUID, top_k: int) -> List[Tuple[str, float]]:
        """
        Realiza una búsqueda dispersa llamando al servicio remoto.
        Devuelve una lista de tuplas (chunk_id, score).
        """
        adapter_log = log.bind(adapter="RemoteSparseRetrieverAdapter", action="search",
                               company_id=str(company_id), top_k=top_k)
        try:
            # El cliente devuelve una lista de SparseSearchResultItem
            search_results_domain: List[SparseSearchResultItem] = await self.client.search(
                query_text=query,
                company_id=company_id,
                top_k=top_k
            )

            # Mapear los resultados del dominio a List[Tuple[str, float]]
            # SparseSearchResultItem tiene 'chunk_id' y 'score'
            mapped_results: List[Tuple[str, float]] = [
                (item.chunk_id, item.score) for item in search_results_domain
            ]

            adapter_log.info(f"Sparse search successful via remote service. Returned {len(mapped_results)} results.")
            return mapped_results
        except ConnectionError as e:
            adapter_log.error("Connection error during remote sparse search.", error=str(e), exc_info=False)
            # Devolver una lista vacía para que el pipeline RAG pueda continuar
            # si la búsqueda dispersa no es estrictamente crítica.
            return []
        except ValueError as e: # Por ej. si la respuesta del servicio es inválida
            adapter_log.error("Value error during remote sparse search (invalid response from service?).", error=str(e), exc_info=True)
            return []
        except Exception as e:
            adapter_log.exception("Unexpected error during remote sparse search.")
            return [] # Devolver vacío en caso de error inesperado.

    async def health_check(self) -> bool:
        """
        Delega la verificación de salud al cliente del servicio de búsqueda dispersa.
        """
        return await self.client.check_health()
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
import json # Importar json para la expresión 'in'

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
# LLM_FLAG: MANUALLY_VERIFIED_INGEST_SCHEMA_FIELDS_CONSISTENCY
try:
    # Attempt to import from a shared location if ingest-service fields are exposed
    # For now, we assume these constants are defined based on ingest-service's current schema
    # In a mono-repo or shared library, this would be more direct.
    # Hardcoding them here as a fallback based on the ingest-service README and common practice.
    MILVUS_PK_FIELD = "pk_id"
    MILVUS_VECTOR_FIELD = "embedding" # Corresponds to settings.MILVUS_EMBEDDING_FIELD from ingest
    MILVUS_CONTENT_FIELD = "content" # Corresponds to settings.MILVUS_CONTENT_FIELD from ingest
    MILVUS_COMPANY_ID_FIELD = "company_id"
    MILVUS_DOCUMENT_ID_FIELD = "document_id"
    MILVUS_FILENAME_FIELD = "file_name"
    MILVUS_PAGE_FIELD = "page"
    MILVUS_TITLE_FIELD = "title"

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
     structlog.getLogger(__name__).warning("Could not import ingest schema constants, using settings and fallbacks for field names.")
     INGEST_SCHEMA_FIELDS = {
        "pk": "pk_id",
        "vector": settings.MILVUS_EMBEDDING_FIELD,
        "content": settings.MILVUS_CONTENT_FIELD,
        "company": settings.MILVUS_COMPANY_ID_FIELD,
        "document": settings.MILVUS_DOCUMENT_ID_FIELD,
        "filename": settings.MILVUS_FILENAME_FIELD,
        "page": "page",
        "title": "title",
    }


log = structlog.get_logger(__name__)

class MilvusAdapter(VectorStorePort):
    """Adaptador concreto para interactuar con Milvus usando pymilvus."""

    _collection: Optional[Collection] = None
    _connected = False
    _alias = "query_service_milvus_adapter"
    _pk_field_name: str
    _vector_field_name: str

    def __init__(self):
        self._pk_field_name = INGEST_SCHEMA_FIELDS["pk"]
        self._vector_field_name = INGEST_SCHEMA_FIELDS["vector"]


    async def _ensure_connection(self):
        """Ensures connection to Milvus is established."""
        if not self._connected or self._alias not in connections.list_connections():
            uri = str(settings.MILVUS_URI)
            connect_log = log.bind(adapter="MilvusAdapter", action="connect", uri=uri, alias=self._alias)
            connect_log.debug("Attempting to connect to Milvus (Zilliz)...")
            try:
                connections.connect(
                    alias=self._alias,
                    uri=uri,
                    token=settings.ZILLIZ_API_KEY.get_secret_value(), # MODIFIED: Added token
                    timeout=settings.MILVUS_GRPC_TIMEOUT
                )
                self._connected = True
                connect_log.info("Connected to Milvus (Zilliz) successfully.")
            except MilvusException as e:
                connect_log.error("Failed to connect to Milvus (Zilliz).", error_code=e.code, error_message=e.message)
                self._connected = False
                raise ConnectionError(f"Milvus (Zilliz) connection failed (Code: {e.code}): {e.message}") from e
            except Exception as e:
                connect_log.error("Unexpected error connecting to Milvus (Zilliz).", error=str(e), exc_info=True)
                self._connected = False
                raise ConnectionError(f"Unexpected Milvus (Zilliz) connection error: {e}") from e

    async def _get_collection(self) -> Collection:
        """Gets the Milvus collection object, ensuring connection and loading."""
        await self._ensure_connection()

        if self._collection is None:
            collection_name = settings.MILVUS_COLLECTION_NAME
            collection_log = log.bind(adapter="MilvusAdapter", action="get_collection", collection=collection_name, alias=self._alias)
            collection_log.info(f"Attempting to access Milvus collection: '{collection_name}'")
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
                if "multiple indexes" in e.message.lower(): # LLM_FLAG: SENSITIVE_ERROR_HANDLING
                    collection_log.critical("Potential 'Ambiguous Index' error encountered. Please check Milvus indices for this collection.")
                raise RuntimeError(f"Milvus collection access error (Code: {e.code}): {e.message}") from e
            except Exception as e:
                 collection_log.exception("Unexpected error accessing Milvus collection")
                 raise RuntimeError(f"Unexpected error accessing Milvus collection: {e}") from e

        if not isinstance(self._collection, Collection): # LLM_FLAG: ROBUSTNESS_CHECK
            log.critical("Milvus collection object is unexpectedly None or invalid type after initialization attempt.")
            raise RuntimeError("Failed to obtain a valid Milvus collection object.")

        return self._collection

    async def search(self, embedding: List[float], company_id: str, top_k: int) -> List[RetrievedChunk]:
        """Busca chunks relevantes usando pymilvus y los convierte al modelo de dominio."""
        search_log = log.bind(adapter="MilvusAdapter", action="search", company_id=company_id, top_k=top_k)
        try:
            collection = await self._get_collection()

            search_params = settings.MILVUS_SEARCH_PARAMS.copy() 
            filter_expr = f'{INGEST_SCHEMA_FIELDS["company"]} == "{company_id}"'
            search_log.debug("Using filter expression", expr=filter_expr)

            required_output_fields_set = {
                self._pk_field_name,
                self._vector_field_name,
                INGEST_SCHEMA_FIELDS["content"],
                INGEST_SCHEMA_FIELDS["company"],
                INGEST_SCHEMA_FIELDS["document"],
                INGEST_SCHEMA_FIELDS["filename"],
            }
            required_output_fields_set.update(settings.MILVUS_METADATA_FIELDS)
            output_fields_list = list(required_output_fields_set)

            search_log.debug("Performing Milvus vector search...",
                             vector_field=self._vector_field_name,
                             output_fields=output_fields_list)

            loop = asyncio.get_running_loop()
            search_results = await loop.run_in_executor(
                None,
                lambda: collection.search(
                    data=[embedding],
                    anns_field=self._vector_field_name,
                    param=search_params,
                    limit=top_k,
                    expr=filter_expr,
                    output_fields=output_fields_list,
                    consistency_level="Strong" 
                )
            )

            search_log.debug(f"Milvus search completed. Hits: {len(search_results[0]) if search_results and search_results[0] else 0}")

            domain_chunks: List[RetrievedChunk] = []
            if search_results and search_results[0]:
                for hit in search_results[0]:
                    entity_data = hit.entity.to_dict() if hasattr(hit, 'entity') and hasattr(hit.entity, 'to_dict') else {}
                    
                    # hit.id es el PK
                    pk_id = str(hit.id) 
                    content = entity_data.get(INGEST_SCHEMA_FIELDS["content"], "")
                    embedding_vector = entity_data.get(self._vector_field_name)

                    metadata_dict = {k: v for k, v in entity_data.items() if k != self._vector_field_name}
                    
                    doc_id_val = metadata_dict.get(INGEST_SCHEMA_FIELDS["document"])
                    comp_id_val = metadata_dict.get(INGEST_SCHEMA_FIELDS["company"])
                    fname_val = metadata_dict.get(INGEST_SCHEMA_FIELDS["filename"])

                    chunk = RetrievedChunk(
                        id=pk_id,
                        content=content,
                        score=hit.score,
                        metadata=metadata_dict,
                        embedding=embedding_vector,
                        document_id=str(doc_id_val) if doc_id_val else None,
                        file_name=str(fname_val) if fname_val else None,
                        company_id=str(comp_id_val) if comp_id_val else None
                    )
                    domain_chunks.append(chunk)

            search_log.info(f"Converted {len(domain_chunks)} Milvus hits to domain objects.")
            return domain_chunks

        except MilvusException as me:
             search_log.error("Milvus search failed", error_code=me.code, error_message=me.message)
             raise ConnectionError(f"Vector DB search error (Code: {me.code}): {me.message}") from me
        except Exception as e:
            search_log.exception("Unexpected error during Milvus search")
            raise ConnectionError(f"Vector DB search service error: {e}") from e

    async def fetch_vectors_by_ids(
        self,
        ids: List[str],
        *,
        collection_name: str | None = None,
    ) -> Dict[str, List[float]]:
        """
        Devuelve un dict {id: embedding}. Si Milvus no encuentra alguno, no lo incluye.
        """
        fetch_log = log.bind(adapter="MilvusAdapter", action="fetch_vectors_by_ids", num_ids=len(ids))
        if not ids:
            fetch_log.debug("No IDs provided, returning empty dict.")
            return {}

        try:
            _collection_obj = await self._get_collection()
            
            # Milvus 'in' operator expects a list of strings or numbers. JSON dump for safety with strings.
            ids_json_array_str = json.dumps(ids)
            expr = f'{self._pk_field_name} in {ids_json_array_str}'
            
            fetch_log.debug("Querying Milvus for vectors by PKs", expr=expr, pk_field=self._pk_field_name, vector_field=self._vector_field_name)

            output_fields_to_fetch = [self._pk_field_name, self._vector_field_name]

            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(
                None,
                lambda: _collection_obj.query(
                    expr=expr,
                    output_fields=output_fields_to_fetch,
                    consistency_level="Strong"
                )
            )
            
            # Milvus query devuelve List[Dict]; lo convertimos a {id: vec}
            # El PK devuelto por query estará en el campo self._pk_field_name
            fetched_vectors = {row[self._pk_field_name]: row[self._vector_field_name] for row in res if self._pk_field_name in row and self._vector_field_name in row}
            fetch_log.info(f"Fetched {len(fetched_vectors)} vectors from Milvus out of {len(ids)} requested.")
            return fetched_vectors
        except MilvusException as me:
            fetch_log.error("Milvus query for vectors by IDs failed", error_code=me.code, error_message=me.message)
            raise ConnectionError(f"Vector DB query error (Code: {me.code}): {me.message}") from me
        except Exception as e:
            fetch_log.exception("Unexpected error during Milvus vector fetch by IDs")
            raise ConnectionError(f"Vector DB query service error: {e}") from e


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
                self._collection = None
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
    EmbeddingPort 
)
from app.infrastructure.persistence.postgres_repositories import (
    PostgresChatRepository, PostgresLogRepository, PostgresChunkContentRepository
)
from app.infrastructure.vectorstores.milvus_adapter import MilvusAdapter
from app.infrastructure.llms.gemini_adapter import GeminiAdapter

from app.infrastructure.clients.sparse_search_service_client import SparseSearchServiceClient
from app.infrastructure.retrievers.remote_sparse_retriever_adapter import RemoteSparseRetrieverAdapter

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
sparse_search_service_client_instance: Optional[SparseSearchServiceClient] = None 

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
           sparse_search_service_client_instance, \
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
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20) 
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
            embedding_service_client_instance = EmbeddingServiceClient(
                base_url=str(settings.EMBEDDING_SERVICE_URL),
                timeout=settings.EMBEDDING_CLIENT_TIMEOUT
            )
            embedding_adapter_instance = RemoteEmbeddingAdapter(client=embedding_service_client_instance)
            await embedding_adapter_instance.initialize()
            
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

    # 2.B. Initialize Sparse Search Service Client & Adapter
    if dependencies_ok and settings.BM25_ENABLED: 
        try:
            sparse_search_service_client_instance = SparseSearchServiceClient(
                base_url=str(settings.SPARSE_SEARCH_SERVICE_URL),
                timeout=settings.SPARSE_SEARCH_CLIENT_TIMEOUT
            )
            sparse_retriever_instance = RemoteSparseRetrieverAdapter(client=sparse_search_service_client_instance)
            
            sparse_service_healthy = await sparse_search_service_client_instance.check_health() 
            if sparse_service_healthy:
                log.info("Sparse Search Service client and adapter initialized, health check passed.")
            else:
                log.warning(f"Sparse Search Service health check failed during startup. URL: {settings.SPARSE_SEARCH_SERVICE_URL}. Sparse search may be unavailable.")
        except Exception as e_sparse:
            log.error(f"Failed to initialize Sparse Search Service client/adapter. Sparse search will be unavailable.", error=str(e_sparse), exc_info=True, url=str(settings.SPARSE_SEARCH_SERVICE_URL))
            sparse_retriever_instance = None 

    # 3. Initialize Milvus Adapter
    if dependencies_ok:
        try:
            vector_store_instance = MilvusAdapter()
            await vector_store_instance.connect() 
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
            # --- CORRECTION: Access _model (internal attribute) instead of model ---
            if not llm_instance._model: 
                 critical_failure_message = "Gemini Adapter initialized but model failed to load (check API key or adapter's _configure_client)."
                 log.critical(f"CRITICAL: {critical_failure_message}")
                 dependencies_ok = False
            else:
                 log.info("Gemini Adapter initialized successfully.")
        except Exception as e_llm:
            critical_failure_message = "Failed to initialize Gemini Adapter."
            log.critical(f"CRITICAL: {critical_failure_message}", error=str(e_llm), exc_info=True)
            dependencies_ok = False
    
    # Initialize optional components (Diversity Filter)
    if dependencies_ok:
        if settings.DIVERSITY_FILTER_ENABLED:
            try:
                if embedding_adapter_instance and embedding_adapter_instance.get_embedding_dimension() > 0 :
                    diversity_filter_instance = MMRDiversityFilter(lambda_mult=settings.QUERY_DIVERSITY_LAMBDA)
                    log.info("MMR Diversity Filter initialized.")
                else:
                    log.warning("MMR Diversity Filter enabled but embedding adapter is not available or has no dimension. Falling back to StubDiversityFilter.")
                    diversity_filter_instance = StubDiversityFilter()
            except Exception as e_diversity:
                log.error("Failed to initialize MMR Diversity Filter. Falling back to StubDiversityFilter.", error=str(e_diversity), exc_info=True)
                diversity_filter_instance = StubDiversityFilter()
        else: 
            log.info("Diversity filter disabled in settings, using StubDiversityFilter as placeholder.")
            diversity_filter_instance = StubDiversityFilter()

    # 5. Instantiate Use Case
    if dependencies_ok:
         try:
             if not http_client_instance:
                 raise RuntimeError("HTTP client instance is not available for AskQueryUseCase.")

             ask_query_use_case_instance = AskQueryUseCase(
                 chat_repo=chat_repo_instance,
                 log_repo=log_repo_instance,
                 vector_store=vector_store_instance,
                 llm=llm_instance,
                 embedding_adapter=embedding_adapter_instance,
                 http_client=http_client_instance,
                 sparse_retriever=sparse_retriever_instance,
                 chunk_content_repo=chunk_content_repo_instance, 
                 diversity_filter=diversity_filter_instance
             )
             log.info("AskQueryUseCase instantiated successfully.")
             SERVICE_READY = True 
             set_ask_query_use_case_instance(ask_query_use_case_instance, SERVICE_READY)
             log.info(f"{settings.PROJECT_NAME} service components initialized. SERVICE READY.")

         except Exception as e_usecase:
              critical_failure_message = "Failed to instantiate AskQueryUseCase."
              log.critical(f"CRITICAL: {critical_failure_message}", error=str(e_usecase), exc_info=True)
              SERVICE_READY = False
              set_ask_query_use_case_instance(None, False)
    else:
        log.critical(f"{settings.PROJECT_NAME} startup sequence aborted due to critical failure: {critical_failure_message}")
        log.critical("SERVICE NOT READY.")
        set_ask_query_use_case_instance(None, False)


    if not SERVICE_READY:
        if not critical_failure_message: critical_failure_message = "Unknown critical dependency failure during startup."
        log.critical(f"Startup finished. Critical failure detected: {critical_failure_message}. SERVICE NOT READY.")


    yield 

    # --- Shutdown Logic ---
    log.info(f"Shutting down {settings.PROJECT_NAME}...")
    if http_client_instance:
        await http_client_instance.aclose()
        log.info("Global HTTP client closed.")
    await postgres_connector.close_db_pool()
    if vector_store_instance and hasattr(vector_store_instance, 'disconnect'):
        try: await vector_store_instance.disconnect()
        except Exception as e_milvus_close: log.error("Error during Milvus disconnect", error=str(e_milvus_close), exc_info=True)
    
    if embedding_service_client_instance:
        try: await embedding_service_client_instance.close()
        except Exception as e_emb_client_close: log.error("Error closing EmbeddingServiceClient", error=str(e_emb_client_close), exc_info=True)
    
    if sparse_search_service_client_instance: 
        try: await sparse_search_service_client_instance.close()
        except Exception as e_sparse_client_close: log.error("Error closing SparseSearchServiceClient", error=str(e_sparse_client_close), exc_info=True)
        
    log.info("Shutdown complete.")

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    version="0.3.3", 
    description="Microservice to handle user queries using RAG pipeline, chat history, remote embedding, remote reranking, and remote sparse search.",
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
        exc_log.exception("Unhandled exception during request processing middleware") 
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
    log.exception("Unhandled Exception caught by generic handler") 
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
    else: 
        health_log.error("Health check (root) warning: Embedding Adapter instance not available, inconsistent with SERVICE_READY state.")
        raise HTTPException(status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service inconsistency: Embedding adapter missing.")

    # ELIMINADO: Health check al reranker-service.
    # El query-service ahora no dependerá del health del reranker para su propia salud.
    # Manejará errores de reranker en tiempo de ejecución si es necesario.
    
    # Check Sparse Search Service Health (if enabled)
    if settings.BM25_ENABLED: 
        if sparse_search_service_client_instance: 
            sparse_service_healthy = await sparse_search_service_client_instance.check_health()
            if not sparse_service_healthy:
                health_log.warning("Health check (root) warning: Sparse Search Service reports unhealthy. Sparse search functionality may be impaired but service can continue.")
            else:
                health_log.debug("Sparse Search Service health check successful via root.")
        elif settings.BM25_ENABLED: 
            health_log.error("Health check (root) failed: BM25_ENABLED is true, but Sparse Search Service client is not available.")


    health_log.debug("Health check (root) passed.")
    return PlainTextResponse("OK", status_code=fastapi_status.HTTP_200_OK)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8001")) 
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

## File: `app\prompts\general_template_gemini_v2.txt`
```txt
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
```

## File: `app\prompts\map_prompt_template.txt`
```txt
Eres un asistente especializado en extraer información concisa de fragmentos de texto.
PREGUNTA ORIGINAL DEL USUARIO:
{{ original_query }}

{% for doc_item in documents %}
────────────────────────────────────────────────────────────────────────────────
DOCUMENTO ACTUAL (Fragmento {{ document_index + loop.index0 + 1 }} de {{ total_documents }}):
ID del Chunk: {{ doc_item.id }}
Nombre del Archivo: {{ doc_item.meta.file_name | default("Desconocido") }}
Página: {{ doc_item.meta.page | default("N/A") }}
Título del Documento (si existe): {{ doc_item.meta.title | default("N/A") }}
Score de Recuperación: {{ "%.3f"|format(doc_item.score) if doc_item.score is not none else "N/A" }}
CONTENIDO DEL FRAGMENTO:
{{ doc_item.content | trim }}
────────────────────────────────────────────────────────────────────────────────

TAREA PARA ESTE FRAGMENTO (ID: {{ doc_item.id }}):
1.  Lee atentamente la PREGUNTA ORIGINAL DEL USUARIO y el CONTENIDO DEL FRAGMENTO.
2.  Si la PREGUNTA ORIGINAL DEL USUARIO es sobre **reuniones, juntas o actas**, extrae y resume **detalladamente** del FRAGMENTO ACTUAL:
    *   Fecha de la reunión.
    *   Tipo de reunión (ej. junta de accionistas, reunión de directorio).
    *   Principales temas tratados o puntos de la agenda.
    *   Decisiones importantes tomadas.
    *   Nombres de participantes clave mencionados (si los hay).
    *   Cualquier otra información directamente relevante a la reunión descrita.
    Si la pregunta no es sobre reuniones, extrae y resume *únicamente* la información del FRAGMENTO ACTUAL que sea **directa y explícitamente relevante** para responder a la PREGUNTA ORIGINAL DEL USUARIO.
3.  Sé CONCISO pero COMPLETO para la información relevante.
4.  Si el fragmento contiene información relevante, inicia tu respuesta para este fragmento con: "Información relevante del fragmento [ID: {{ doc_item.id }}] (Archivo: {{ doc_item.meta.file_name | default('Desconocido') }}, Pág: {{ doc_item.meta.page | default('N/A') }}):".
5.  Si el fragmento **NO contiene información explícitamente relevante** para la pregunta, responde EXACTAMENTE: "No hay información relevante en el fragmento [ID: {{ doc_item.id }}]."
6.  NO inventes información. NO uses conocimiento externo.

EXTRACCIÓN CONCISA DEL FRAGMENTO [ID: {{ doc_item.id }}]:
{# Aquí la IA debe generar la extracción para este doc_item. #}

{% endfor %}

INSTRUCCIÓN FINAL PARA LA IA:
Después de procesar todos los fragmentos anteriores, concatena todas las extracciones "Información relevante del fragmento..." en una sola respuesta. Si todos los fragmentos fueron "No hay información relevante...", entonces la respuesta final debe ser "No hay información relevante en este lote de fragmentos."
```

## File: `app\prompts\rag_template_gemini_v2.txt`
```txt
════════════════════════════════════════════════════════════════════
A T E N E X · SÍNTESIS DE RESPUESTA (Gemini 2.5 Flash)
════════════════════════════════════════════════════════════════════

1 · IDENTIDAD Y TONO
Eres **Atenex**, un asistente de IA experto en consulta de documentación empresarial. Eres profesional, directo, verificable y empático. Escribe en **español latino** claro y conciso. Prioriza la precisión y la seguridad.

2 · TAREA PRINCIPAL
Tu tarea es sintetizar la INFORMACIÓN RECOPILADA DE DOCUMENTOS para responder de forma **extensa y detallada** a la PREGUNTA ACTUAL DEL USUARIO, considerando también el HISTORIAL RECIENTE de la conversación. Si la pregunta pide un resumen de reuniones a lo largo del tiempo, intenta construir una narrativa cronológica o temática basada en los extractos. Debes generar una respuesta en formato JSON estructurado.

3 · CONTEXTO
PREGUNTA ACTUAL DEL USUARIO:
{{ query }}

{% if chat_history %}
───────────────────── HISTORIAL RECIENTE (Más antiguo a más nuevo) ─────────────────────
{{ chat_history }}
────────────────────────────────────────────────────────────────────────────────────────
{% endif %}

{% if documents %}
──────────────────── INFORMACIÓN RECOPILADA DE DOCUMENTOS (Chunks relevantes) ───────────────────
A continuación se presentan varios fragmentos de documentos que son relevantes para la pregunta actual. Úsalos para construir tu respuesta y las citas.
{% for doc_item in documents %}
[Doc {{ loop.index }}] ID: {{ doc_item.id }}, Archivo: «{{ doc_item.meta.file_name | default("Archivo Desconocido") }}», Título: {{ doc_item.meta.title | default("Sin Título") }}, Pág: {{ doc_item.meta.page | default("?") }}, Score Original: {{ "%.3f"|format(doc_item.score) if doc_item.score is not none else "N/A" }}
CONTENIDO DEL FRAGMENTO:
{{ doc_item.content | trim }}
────────────────────────────────────────────────────────────────────────────────────────
{% endfor %}
{% else %}
──────────────────── INFORMACIÓN RECOPILADA DE DOCUMENTOS ───────────────────
No se recuperaron documentos específicos para esta consulta.
────────────────────────────────────────────────────────────────────────────────────────
{% endif %}

4 · PRINCIPIOS CLAVE PARA LA SÍNTESIS
   - **BASATE SOLO EN EL CONTEXTO PROPORCIONADO:** Usa *únicamente* la INFORMACIÓN RECOPILADA DE DOCUMENTOS (si existe) y el HISTORIAL RECIENTE. **No inventes**, especules ni uses conocimiento externo.
   - **CITACIÓN PRECISA:** Cuando uses información que provenga de un chunk específico (identificable en la INFORMACIÓN RECOPILADA DE DOCUMENTOS), debes citarlo usando la etiqueta `[Doc N]` correspondiente.
   - **NO ESPECULACIÓN:** Si la información combinada no es suficiente para responder completamente, indícalo claramente en `respuesta_detallada`.
   - **RESPUESTA INTEGRAL Y DETALLADA:** Intenta conectar la información de diferentes fragmentos para dar una respuesta completa y rica en detalles si es posible, especialmente si el usuario pide un resumen "extenso". Identifica temas comunes o cronologías.
   - **MANEJO DE "NO SÉ":** Si no hay INFORMACIÓN RECOPILADA DE DOCUMENTOS, tu `respuesta_detallada` debe ser "No encontré información específica sobre eso en los documentos procesados." Si hay algo de información pero es escasa, indica que la información es limitada.

5 · PROCESO DE PENSAMIENTO SUGERIDO (INTERNO - Chain-of-Thought)
Antes de generar el JSON final:
a. Revisa la PREGUNTA ACTUAL DEL USUARIO y el HISTORIAL para la intención completa. ¿Pide detalle, extensión?
b. Analiza la INFORMACIÓN RECOPILADA DE DOCUMENTOS. Identifica los puntos clave de cada fragmento. Agrupa información sobre el mismo tema o evento (ej. misma reunión).
c. Sintetiza estos puntos en una narrativa coherente y detallada para `respuesta_detallada`. Si se piden resúmenes de reuniones, intenta listar cada reunión y sus detalles.
d. Cruza la información sintetizada con la INFORMACIÓN RECOPILADA DE DOCUMENTOS para asegurar que las citas `[Doc N]` sean correctas y se refieran al fragmento correcto.
e. Genera un `resumen_ejecutivo` si `respuesta_detallada` es extensa.
f. Construye `fuentes_citadas` solo con los documentos que realmente usaste y citaste. El `cita_tag` debe coincidir con el usado en `respuesta_detallada`.
g. Considera una `siguiente_pregunta_sugerida` si es natural.
h. Ensambla el JSON.

6 · FORMATO DE RESPUESTA REQUERIDO (OBJETO JSON VÁLIDO)
```json
{
  "resumen_ejecutivo": "string | null (Un breve resumen de 1-2 frases si la respuesta es larga, sino null)",
  "respuesta_detallada": "string (La respuesta completa y elaborada, incluyendo citas [Doc N] donde corresponda. Si no se encontró información, indícalo aquí)",
  "fuentes_citadas": [
    {
      "id_documento": "string | null (ID del chunk original, si está disponible en su metadata)",
      "nombre_archivo": "string (Nombre del archivo fuente)",
      "pagina": "string | null (Número de página, si está disponible)",
      "score": "number | null (Score de relevancia original del chunk, si está disponible)",
      "cita_tag": "string (La etiqueta de cita usada en respuesta_detallada, ej: '[Doc 1]')"
    }
  ],
  "siguiente_pregunta_sugerida": "string | null (Una pregunta de seguimiento relevante, si aplica, sino null)"
}
```
Asegúrate de que:
- El JSON sea sintácticamente correcto.
- Las citas [Doc N] en respuesta_detallada coincidan con las listadas en fuentes_citadas (mismo N y misma fuente).
- fuentes_citadas solo contenga documentos efectivamente usados y referenciados.
- No incluyas comentarios dentro del JSON.

════════════════════════════════════════════════════════════════════
RESPUESTA JSON DE ATENEX:
════════════════════════════════════════════════════════════════════
```

## File: `app\prompts\reduce_prompt_template_v2.txt`
```txt
════════════════════════════════════════════════════════════════════
A T E N E X · SÍNTESIS DE RESPUESTA (Gemini 2.5 Flash)
════════════════════════════════════════════════════════════════════

1 · IDENTIDAD Y TONO
Eres **Atenex**, un asistente de IA experto en consulta de documentación empresarial. Eres profesional, directo, verificable y empático. Escribe en **español latino** claro y conciso. Prioriza la precisión y la seguridad.

2 · TAREA PRINCIPAL
Tu tarea es sintetizar la INFORMACIÓN RECOPILADA (extractos de múltiples documentos) para responder de forma integral a la PREGUNTA ORIGINAL DEL USUARIO, considerando también el HISTORIAL RECIENTE de la conversación. Debes generar una respuesta en formato JSON estructurado.

3 · CONTEXTO
PREGUNTA ORIGINAL DEL USUARIO:
{{ original_query }}

{% if chat_history %}
───────────────────── HISTORIAL RECIENTE (Más antiguo a más nuevo) ─────────────────────
{{ chat_history }}
────────────────────────────────────────────────────────────────────────────────────────
{% endif %}

──────────────────── INFORMACIÓN RECOPILADA DE DOCUMENTOS (Fase Map) ───────────────────
A continuación se presentan varios extractos y resúmenes de diferentes documentos que podrían ser relevantes. Cada bloque de información fue extraído individualmente.
{{ mapped_responses }} {# Aquí se concatenarán las respuestas de la fase Map #}
────────────────────────────────────────────────────────────────────────────────────────

──────────────────── LISTA DE CHUNKS ORIGINALES CONSIDERADOS (Para referencia de citación) ───────────────────
Estos son los chunks originales de los cuales se extrajo la INFORMACIÓN RECOPILADA. Úsalos para construir la sección `fuentes_citadas` y para las citas `[Doc N]` en `respuesta_detallada`.
{% for doc_chunk in original_documents_for_citation %}
[Doc {{ loop.index }}] ID: {{ doc_chunk.id }}, Archivo: «{{ doc_chunk.meta.file_name | default("Archivo Desconocido") }}», Título: {{ doc_chunk.meta.title | default("Sin Título") }}, Pág: {{ doc_chunk.meta.page | default("?") }}, Score Original: {{ "%.3f"|format(doc_chunk.score) if doc_chunk.score is not none else "N/A" }}
{% endfor %}
─────────────────────────────────────────────────────────────────────────────────────────

4 · PRINCIPIOS CLAVE PARA LA SÍNTESIS
   - **BASATE SOLO EN EL CONTEXTO PROPORCIONADO:** Usa *únicamente* la INFORMACIÓN RECOPILADA y el HISTORIAL RECIENTE. **No inventes**, especules ni uses conocimiento externo.
   - **CITACIÓN PRECISA:** Cuando uses información que provenga de un chunk específico (identificable en la INFORMACIÓN RECOPILADA), debes citarlo usando la etiqueta `[Doc N]` correspondiente al chunk de la LISTA DE CHUNKS ORIGINALES CONSIDERADOS.
   - **NO ESPECULACIÓN:** Si la información combinada no es suficiente para responder completamente, indícalo claramente en `respuesta_detallada`.
   - **RESPUESTA INTEGRAL:** Intenta conectar la información de diferentes extractos para dar una respuesta completa si es posible.
   - **MANEJO DE "NO SÉ":** Si la INFORMACIÓN RECOPILADA es predominantemente "No hay información relevante...", tu `respuesta_detallada` debe ser "No encontré información específica sobre eso en los documentos procesados."

5 · PROCESO DE PENSAMIENTO SUGERIDO (INTERNO - Chain-of-Thought)
Antes de generar el JSON final:
a. Revisa la PREGUNTA ORIGINAL y el HISTORIAL para la intención completa.
b. Analiza la INFORMACIÓN RECOPILADA. Identifica los puntos clave de cada extracto.
c. Sintetiza estos puntos en una narrativa coherente para `respuesta_detallada`.
d. Cruza la información sintetizada con la LISTA DE CHUNKS ORIGINALES para asegurar que las citas `[Doc N]` sean correctas y se refieran al chunk correcto.
e. Genera un `resumen_ejecutivo` si `respuesta_detallada` es extensa.
f. Construye `fuentes_citadas` solo con los documentos que realmente usaste y citaste. El `cita_tag` debe coincidir con el usado en `respuesta_detallada`.
g. Considera una `siguiente_pregunta_sugerida` si es natural.
h. Ensambla el JSON.

6 · FORMATO DE RESPUESTA REQUERIDO (OBJETO JSON VÁLIDO)
Tu respuesta DEBE ser un objeto JSON válido con la siguiente estructura. Presta atención a los tipos de datos y campos requeridos/opcionales.
```json
{
  "resumen_ejecutivo": "string | null (Un breve resumen de 1-2 frases si la respuesta es larga, sino null)",
  "respuesta_detallada": "string (La respuesta completa y elaborada, incluyendo citas [Doc N] donde corresponda. Si no se encontró información, indícalo aquí)",
  "fuentes_citadas": [
    {
      "id_documento": "string | null (ID del chunk original, si está disponible en su metadata)",
      "nombre_archivo": "string (Nombre del archivo fuente)",
      "pagina": "string | null (Número de página, si está disponible)",
      "score": "number | null (Score de relevancia original del chunk, si está disponible)",
      "cita_tag": "string (La etiqueta de cita usada en respuesta_detallada, ej: '[Doc 1]')"
    }
  ],
  "siguiente_pregunta_sugerida": "string | null (Una pregunta de seguimiento relevante, si aplica, sino null)"
}
```
Asegúrate de que:
- El JSON sea sintácticamente correcto.
- Las citas `[Doc N]` en `respuesta_detallada` coincidan con las listadas en `fuentes_citadas` (mismo `N` y misma fuente).
- `fuentes_citadas` solo contenga documentos efectivamente usados y referenciados.
- No incluyas comentarios dentro del JSON.

════════════════════════════════════════════════════════════════════
RESPUESTA JSON DE ATENEX:
════════════════════════════════════════════════════════════════════
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
version = "1.3.5" # Incrementar versión por corrección
description = "Query service for SaaS B2B using Clean Architecture, PyMilvus, Haystack & Advanced RAG with remote embeddings, reranking, and sparse search."
authors = ["Nyro <dev@atenex.com>"]
readme = "README.md"

[tool.poetry.dependencies]
python = "^3.10"

fastapi = "^0.110.0"
uvicorn = {extras = ["standard"], version = "^0.28.0"}
gunicorn = "^21.2.0"
pydantic = {extras = ["email"], version = "^2.6.4"} 
pydantic-settings = "^2.2.1"
# httpx debe ser compatible con google-generativeai
httpx = ">=0.27.0,<1.0.0" 
asyncpg = "^0.29.0"
python-jose = {extras = ["cryptography"], version = "^3.3.0"}
tenacity = "^8.2.3"
structlog = "^24.1.0"

# --- Haystack Dependencies ---
haystack-ai = "^2.0.1" 
pymilvus = "==2.5.3" 

# --- LLM Dependency ---
google-generativeai = "^0.7.0"

# --- RAG Component Dependencies ---
numpy = "1.26.4" 


[tool.poetry.group.dev.dependencies]
pytest = "^7.4.4"
pytest-asyncio = "^0.21.1"

[tool.poetry.extras]

[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
```
