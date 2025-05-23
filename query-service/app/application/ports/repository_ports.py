# query-service/app/application/ports/repository_ports.py
import abc
import uuid
from typing import List, Optional, Dict, Any, Tuple # Añadir Tuple
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
        retrieved_documents_data: List[Dict[str, Any]], 
        metadata: Optional[Dict[str, Any]] = None,
        chat_id: Optional[uuid.UUID] = None,
    ) -> uuid.UUID:
        raise NotImplementedError


class ChunkContentRepositoryPort(abc.ABC):
    """Puerto abstracto para obtener contenido textual y metadatos de chunks desde la persistencia."""

    @abc.abstractmethod
    async def get_chunk_contents_by_company(self, company_id: uuid.UUID) -> Dict[str, Dict[str, Any]]:
        """
        Obtiene un diccionario de {chunk_id: {'content': str, 'document_id': str, 'file_name': str}} para una compañía.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def get_chunk_contents_by_ids(self, chunk_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Obtiene un diccionario de {chunk_id: {'content': str, 'document_id': str, 'file_name': str}} para una lista de IDs.
        Los chunk_ids aquí son los embedding_id de Milvus.
        """
        raise NotImplementedError