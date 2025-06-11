from typing import List, Dict, Tuple, Any
from app.application.ports.embedding_service_port import EmbeddingServicePort
from app.services.clients.embedding_service_client import EmbeddingServiceClient
from app.domain.exceptions import ServiceDependencyException # Import corrected
import asyncio

class RemoteEmbeddingAdapter(EmbeddingServicePort):
    def __init__(self):
        # EmbeddingServiceClient now handles its own base URL from settings
        self.client = EmbeddingServiceClient()

    async def get_embeddings(self, texts: List[str], text_type: str = "passage") -> Tuple[List[List[float]], Dict[str, Any]]:
        # This method remains async
        try:
            # Ensure the async client method is called
            return await self.client.get_embeddings_async(texts, text_type)
        except ServiceDependencyException:
            raise
        except Exception as e:
            raise ServiceDependencyException(service_name="EmbeddingService", original_error=str(e)) from e

    def get_embeddings_sync(self, texts: List[str], text_type: str = "passage") -> Tuple[List[List[float]], Dict[str, Any]]:
        try:
            # Call the new synchronous method on the client
            return self.client.get_embeddings_sync(texts, text_type)
        except ServiceDependencyException:
            raise
        except Exception as e:
            raise ServiceDependencyException(service_name="EmbeddingService", original_error=str(e)) from e