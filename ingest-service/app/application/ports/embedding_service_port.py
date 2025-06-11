from abc import ABC, abstractmethod
from typing import List, Dict, Tuple, Any

class EmbeddingServicePort(ABC):
    @abstractmethod
    def get_embeddings(self, texts: List[str], text_type: str = "passage") -> Tuple[List[List[float]], Dict[str, Any]]:
        pass

    @abstractmethod
    def get_embeddings_sync(self, texts: List[str], text_type: str = "passage") -> Tuple[List[List[float]], Dict[str, Any]]:
        pass