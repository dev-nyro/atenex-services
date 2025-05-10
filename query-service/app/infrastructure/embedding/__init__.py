# query-service/app/infrastructure/embedding/__init__.py
from .remote_embedding_adapter import RemoteEmbeddingAdapter

__all__ = ["RemoteEmbeddingAdapter"]