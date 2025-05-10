# reranker-service/app/application/use_cases/rerank_documents_use_case.py
from typing import List, Optional
import structlog

from app.application.ports.reranker_model_port import RerankerModelPort
from app.domain.models import DocumentToRerank, RerankedDocument, RerankResponseData, ModelInfo

logger = structlog.get_logger(__name__)

class RerankDocumentsUseCase:
    """
    Use case for reranking documents. It orchestrates the interaction
    with the reranker model port.
    """
    def __init__(self, reranker_model: RerankerModelPort):
        self.reranker_model = reranker_model
        logger.debug("RerankDocumentsUseCase initialized", reranker_model_type=type(reranker_model).__name__)

    async def execute(
        self, query: str, documents: List[DocumentToRerank], top_n: Optional[int] = None
    ) -> RerankResponseData:
        
        use_case_log = logger.bind(
            action="execute_rerank_documents_use_case", 
            query_preview=query[:50] + "..." if len(query) > 50 else query,
            num_documents_input=len(documents), 
            requested_top_n=top_n
        )
        use_case_log.info("Executing rerank documents use case.")

        if not self.reranker_model.is_ready():
            use_case_log.error("Reranker model is not ready. Cannot execute reranking.")
            raise RuntimeError("Reranker model service is not ready or model failed to load.")

        try:
            reranked_results = await self.reranker_model.rerank(query, documents)
            use_case_log.debug("Reranking completed by model port.", num_results_from_port=len(reranked_results))

            if top_n is not None and top_n > 0:
                use_case_log.debug(f"Applying top_n={top_n} to reranked results.")
                reranked_results = reranked_results[:top_n]
            
            model_info = ModelInfo(model_name=self.reranker_model.get_model_name())
            response_data = RerankResponseData(reranked_documents=reranked_results, model_info=model_info)
            
            use_case_log.info(
                "Reranking use case execution successful.", 
                num_reranked_documents_output=len(reranked_results),
                model_name=model_info.model_name
            )
            return response_data
        except RuntimeError as e: # Catch errors from the adapter/port
            use_case_log.error("Runtime error during reranking execution.", error_message=str(e), exc_info=True)
            raise # Re-raise to be caught by the endpoint handler
        except Exception as e:
            use_case_log.error("Unexpected error during reranking execution.", error_message=str(e), exc_info=True)
            raise RuntimeError(f"An unexpected error occurred while reranking documents: {e}") from e