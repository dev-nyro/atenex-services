# reranker-service/app/application/ports/reranker_model_port.py
from abc import ABC, abstractmethod
from typing import List
from app.domain.models import DocumentToRerank, RerankedDocument

class RerankerModelPort(ABC):
    @abstractmethod
    async def rerank(
        self, query: str, documents: List[DocumentToRerank]
    ) -> List[RerankedDocument]:
        """
        Reranks a list of documents based on a query.
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
        Checks if the model is loaded and ready.
        """
        pass