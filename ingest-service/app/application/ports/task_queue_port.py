from abc import ABC, abstractmethod

class TaskQueuePort(ABC):
    @abstractmethod
    async def enqueue_process_document_task(self, document_id: str, company_id: str, filename: str, content_type: str) -> str:
        pass
