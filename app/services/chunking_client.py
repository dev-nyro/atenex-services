from .base_client import BaseServiceClient
from app.core.config import settings
import uuid
from typing import List, Dict, Any

class ChunkingServiceClient(BaseServiceClient):
    def __init__(self):
        super().__init__(base_url=str(settings.CHUNKING_SERVICE_URL), service_name="ChunkingService")

    async def chunk_text(self, company_id: uuid.UUID, text: str) -> List[Dict[str, Any]]:
        """Solicita la división del texto en chunks."""
        # Asume que el Chunking service espera JSON con el texto y company_id
        # Podría aceptar parámetros de chunking (size, overlap)
        payload = {"company_id": str(company_id), "text": text}
        response = await self._request("POST", "/chunk", json=payload)
        response_data = response.json()
        if "chunks" not in response_data or not isinstance(response_data["chunks"], list):
             raise ValueError("Chunking service response did not contain a list of 'chunks'")
        # Validar estructura de cada chunk si es necesario
        return response_data["chunks"]