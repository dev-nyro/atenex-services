# reranker-service/app/dependencies.py
from fastapi import HTTPException, status as fastapi_status, Request
from typing import Optional, Annotated

from app.application.use_cases.rerank_documents_use_case import RerankDocumentsUseCase
from app.application.ports.reranker_model_port import RerankerModelPort
# The actual adapter instance will be set during app lifespan.

# Globals to hold instances, set by lifespan. This is a simple DI approach.
_reranker_model_adapter_instance: Optional[RerankerModelPort] = None
_rerank_use_case_instance: Optional[RerankDocumentsUseCase] = None

def set_dependencies(
    model_adapter: RerankerModelPort,
    use_case: RerankDocumentsUseCase
):
    """
    Called during application startup (lifespan) to set the shared instances.
    """
    global _reranker_model_adapter_instance, _rerank_use_case_instance
    _reranker_model_adapter_instance = model_adapter
    _rerank_use_case_instance = use_case
    # Add logging here if needed to confirm dependencies are set.

def get_rerank_use_case() -> RerankDocumentsUseCase:
    """
    FastAPI dependency getter for RerankDocumentsUseCase.
    Ensures the use case and its underlying model adapter are ready.
    """
    if _rerank_use_case_instance is None or \
       _reranker_model_adapter_instance is None or \
       not _reranker_model_adapter_instance.is_ready():
        # This detailed check helps pinpoint if the adapter or use case itself wasn't set,
        # or if the adapter is set but not ready (model load failed).
        raise HTTPException(
            status_code=fastapi_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reranker service is not ready. Dependencies (model or use case) not initialized or model failed to load."
        )
    return _rerank_use_case_instance