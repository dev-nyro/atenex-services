# embedding-service/app/dependencies.py
"""
Centralized dependency injection resolver for the Embedding Service.
"""
import structlog
from fastapi import HTTPException, status
from app.application.use_cases.embed_texts_use_case import EmbedTextsUseCase
from app.application.ports.embedding_model_port import EmbeddingModelPort

# These will be set by main.py at startup
_embed_texts_use_case_instance: EmbedTextsUseCase | None = None
_service_ready: bool = False

log = structlog.get_logger(__name__)

def set_embedding_service_dependencies(
    use_case_instance: EmbedTextsUseCase,
    ready_flag: bool
):
    """Called from main.py lifespan to set up shared instances."""
    global _embed_texts_use_case_instance, _service_ready
    _embed_texts_use_case_instance = use_case_instance
    _service_ready = ready_flag
    log.info("Embedding service dependencies set", use_case_ready=bool(use_case_instance), service_ready=ready_flag)

def get_embed_texts_use_case() -> EmbedTextsUseCase:
    """Dependency provider for EmbedTextsUseCase."""
    if not _service_ready or not _embed_texts_use_case_instance:
        log.error("EmbedTextsUseCase requested but service is not ready or use case not initialized.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Embedding service is not ready. Please try again later."
        )
    return _embed_texts_use_case_instance