# reranker-service/app/dependencies.py
from fastapi import HTTPException, status, Request
from app.application.use_cases.rerank_documents_use_case import RerankDocumentsUseCase
from app.infrastructure.rerankers.sentence_transformer_adapter import SentenceTransformerRerankerAdapter
from app.application.ports.reranker_model_port import RerankerModelPort

# This approach uses global-like instances set up during lifespan.
# For more complex apps, a proper DI container (e.g., dependency-injector) is better.

_reranker_model_adapter_instance: RerankerModelPort = None
_rerank_use_case_instance: RerankDocumentsUseCase = None

def set_dependencies(
    model_adapter: RerankerModelPort,
    use_case: RerankDocumentsUseCase
):
    global _reranker_model_adapter_instance, _rerank_use_case_instance
    _reranker_model_adapter_instance = model_adapter
    _rerank_use_case_instance = use_case

def get_rerank_use_case() -> RerankDocumentsUseCase:
    if _rerank_use_case_instance is None or \
       _reranker_model_adapter_instance is None or \
       not _reranker_model_adapter_instance.is_ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reranker service is not ready or dependencies not initialized."
        )
    return _rerank_use_case_instance

# Alternative: Get adapter from app.state if set in lifespan
# async def get_reranker_adapter_from_state(request: Request) -> RerankerModelPort:
#     if not hasattr(request.app.state, "reranker_adapter") or \
#        not request.app.state.reranker_adapter.is_ready():
#         raise HTTPException(status_code=503, detail="Reranker model adapter not ready.")
#     return request.app.state.reranker_adapter

# async def get_use_case_from_adapter_in_state(
#     adapter: RerankerModelPort = Depends(get_reranker_adapter_from_state)
# ) -> RerankDocumentsUseCase:
#     return RerankDocumentsUseCase(adapter)