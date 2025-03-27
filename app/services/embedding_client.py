from .base_client import BaseServiceClient
from app.core.config import settings
import uuid
from typing import List, Dict, Any

class EmbeddingServiceClient(BaseServiceClient):
    def __init__(self):
        super().__init__(base_url=str(settings.EMBEDDING_SERVICE_URL), service_name="EmbeddingService")

    async def get_embeddings(self, company_id: uuid.UUID, texts: List[str]) -> List[List[float]]:
        """Solicita los embeddings para una lista de textos."""
        # Asume que el Embedding service espera JSON con los textos y company_id
        payload = {"company_id": str(company_id), "texts": texts}
        response = await self._request("POST", "/embed", json=payload)
        response_data = response.json()
        if "embeddings" not in response_data or not isinstance(response_data["embeddings"], list):
             raise ValueError("Embedding service response did not contain a list of 'embeddings'")
        # Validar que cada embedding sea una lista de floats y tenga la dimensión correcta
        if len(response_data["embeddings"]) != len(texts):
            raise ValueError("Embedding service returned a different number of embeddings than texts provided")
        # Aquí podrías añadir validación de la dimensión si es necesario
        # for emb in response_data["embeddings"]:
        #     if not isinstance(emb, list) or len(emb) != settings.MILVUS_DIMENSION:
        #         raise ValueError(f"Invalid embedding structure or dimension received. Expected {settings.MILVUS_DIMENSION}")

        return response_data["embeddings"]