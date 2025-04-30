from functools import lru_cache
from typing import List, Optional
from sentence_transformers import SentenceTransformer
import structlog
import numpy as np
from app.core.config import settings

log = structlog.get_logger(__name__)

MODEL_ID = getattr(settings, 'EMBEDDING_MODEL_ID', 'all-MiniLM-L6-v2')
EXPECTED_DIM = getattr(settings, 'EMBEDDING_DIMENSION', 384)

_model_instance: Optional[SentenceTransformer] = None

def get_embedding_model() -> SentenceTransformer:
    global _model_instance
    if _model_instance is None:
        log.info("Loading SentenceTransformer model", model_id=MODEL_ID)
        _model_instance = SentenceTransformer(MODEL_ID)
        # Validar dimensión
        test_vec = _model_instance.encode(["test"])
        if test_vec.shape[1] != EXPECTED_DIM:
            raise ValueError(f"Embedding dimension mismatch: expected {EXPECTED_DIM}, got {test_vec.shape[1]}")
        log.info("Model loaded and validated", dimension=test_vec.shape[1])
    return _model_instance

def embed_chunks(chunks: List[str]) -> List[List[float]]:
    model = get_embedding_model()
    log.debug("Generating embeddings for chunks", num_chunks=len(chunks))
    embeddings = model.encode(chunks, show_progress_bar=False)
    return embeddings.tolist()
