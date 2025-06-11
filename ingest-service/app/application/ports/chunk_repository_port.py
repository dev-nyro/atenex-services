from abc import ABC, abstractmethod
from typing import List
from app.domain.entities import Chunk

class ChunkRepositoryPort(ABC):
    @abstractmethod
    def save_bulk(self, chunks: List[Chunk]) -> int:
        pass
