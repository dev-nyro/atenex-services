from abc import ABC, abstractmethod
from typing import Optional, Tuple, List, Dict, Any
import uuid
from datetime import date
from app.domain.entities import Document
from app.domain.enums import DocumentStatus

class DocumentRepositoryPort(ABC):
    @abstractmethod
    async def save(self, document: Document) -> None:
        pass

    @abstractmethod
    async def find_by_id(self, doc_id: uuid.UUID, company_id: uuid.UUID) -> Optional[Document]:
        pass

    @abstractmethod
    async def find_by_name_and_company(self, filename: str, company_id: uuid.UUID) -> Optional[Document]:
        pass

    @abstractmethod
    async def update_status(self, doc_id: uuid.UUID, status: DocumentStatus, chunk_count: Optional[int] = None, error_message: Optional[str] = None) -> bool:
        pass

    @abstractmethod
    async def list_paginated(self, company_id: uuid.UUID, limit: int, offset: int) -> Tuple[List[Document], int]:
        pass

    @abstractmethod
    async def delete(self, doc_id: uuid.UUID, company_id: uuid.UUID) -> bool:
        pass

    @abstractmethod
    async def get_stats(self, company_id: uuid.UUID, from_date: Optional[date], to_date: Optional[date], status_filter: Optional[DocumentStatus]) -> Dict[str, Any]:
        pass
