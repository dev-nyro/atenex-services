# embedding-service/app/application/ports/embedding_model_port.py
import abc
from typing import List, Tuple, Dict, Any

class EmbeddingModelPort(abc.ABC):
    """
    Abstract port defining the interface for an embedding model.
    """

    @abc.abstractmethod
    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """
        Generates embeddings for a list of texts.

        Args:
            texts: A list of strings to embed.

        Returns:
            A list of embeddings, where each embedding is a list of floats.

        Raises:
            Exception: If embedding generation fails.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_model_info(self) -> Dict[str, Any]:
        """
        Returns information about the loaded embedding model.

        Returns:
            A dictionary containing model_name, dimension, etc.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def health_check(self) -> Tuple[bool, str]:
        """
        Checks the health of the embedding model.

        Returns:
            A tuple (is_healthy: bool, status_message: str).
        """
        raise NotImplementedError