import uuid
from typing import Optional, Dict, Any
from datetime import date
from app.application.ports.document_repository_port import DocumentRepositoryPort

class GetDocumentStatsUseCase:
    def __init__(self, document_repository: DocumentRepositoryPort):
        self.document_repository = document_repository

    async def execute(self, *,
                     company_id: uuid.UUID,
                     from_date: Optional[date] = None,
                     to_date: Optional[date] = None,
                     status_filter: Optional[str] = None) -> Dict[str, Any]:
        return await self.document_repository.get_stats(company_id, from_date, to_date, status_filter)
