# reranker-service/app/application/ports/reranker_model_port.py
from abc import ABC, abstractmethod
from typing import List
from app.domain.models import DocumentToRerank, RerankedDocument # Import from current service's domain

class RerankerModelPort(ABC):
    """
    Abstract port defining the contract for a reranker model adapter.
    """
    @abstractmethod
    async def rerank(
        self, query: str, documents: List[DocumentToRerank]
    ) -> List[RerankedDocument]:
        """
        Reranks a list of documents based on a query.

        Args:
            query: The query string.
            documents: A list of DocumentToRerank objects.

        Returns:
            A list of RerankedDocument objects, sorted by relevance.
        
        Raises:
            RuntimeError: If the model is not ready or prediction fails.
        """
        pass

    @abstractmethod
    def get_model_name(self) -> str:
        """
        Returns the name of the underlying reranker model.
        """
        pass

    @abstractmethod
    def is_ready(self) -> bool:
        """
        Checks if the model is loaded and ready to perform reranking.
        """
        pass