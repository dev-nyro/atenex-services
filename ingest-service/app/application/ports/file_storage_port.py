from abc import ABC, abstractmethod
from typing import Optional

class FileStoragePort(ABC):
    @abstractmethod
    async def upload(self, object_name: str, data: bytes, content_type: str) -> str:
        pass

    @abstractmethod
    def download(self, object_name: str, target_path: str) -> None: # Kept sync for ProcessDocumentUseCase
        pass

    @abstractmethod
    async def exists(self, object_name: str) -> bool: # Async for API use
        pass

    @abstractmethod
    def exists_sync(self, object_name: str) -> bool: # Sync for worker use
        pass

    @abstractmethod
    async def delete(self, object_name: str) -> None: # Async for API use
        pass
    
    @abstractmethod
    def delete_sync(self, object_name: str) -> None: # Sync for worker use
        pass