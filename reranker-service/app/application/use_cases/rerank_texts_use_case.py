# reranker-service/app/application/use_cases/rerank_documents_use_case.py
from typing import List, Optional
import structlog

from app.application.ports.reranker_model_port import RerankerModelPort
from app.domain.models import DocumentToRerank, RerankedDocument, RerankResponseData, ModelInfo

logger = structlog.get_logger(__name__)

class RerankDocumentsUseCase:
    def __init__(self, reranker_model: RerankerModelPort):
        self.reranker_model = reranker_model

    async def execute(
        self, query: str, documents: List[DocumentToRerank], top_n: Optional[int] = None
    ) -> RerankResponseData:
        
        if not self.reranker_model.is_ready():
            logger.error("Reranker model is not ready, cannot execute use case.")
            raise RuntimeError("Reranker model service is not ready.")

        use_case_log = logger.bind(
            action="rerank_documents_use_case", 
            num_documents=len(documents), 
            top_n=top_n
        )
        use_case_log.info("Executing rerank documents use case.")

        try:
            reranked_results = await self.reranker_model.rerank(query, documents)

            if top_n is not None and top_n > 0:
                use_case_log.debug(f"Applying top_n={top_n} to reranked results.")
                reranked_results = reranked_results[:top_n]
            
            model_info = ModelInfo(model_name=self.reranker_model.get_model_name())
            response_data = RerankResponseData(reranked_documents=reranked_results, model_info=model_info)
            
            use_case_log.info("Reranking successful.", num_reranked=len(reranked_results))
            return response_data
        except Exception as e:
            use_case_log.error("Error during reranking execution.", error=str(e), exc_info=True)
            # Re-raise or handle as specific application error
            raise RuntimeError(f"Failed to rerank documents: {e}") from e