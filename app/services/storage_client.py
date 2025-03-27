from .base_client import BaseServiceClient
from app.core.config import settings
import uuid
from typing import IO, Tuple

class StorageServiceClient(BaseServiceClient):
    def __init__(self):
        super().__init__(base_url=str(settings.STORAGE_SERVICE_URL), service_name="StorageService")

    async def upload_file(
        self,
        company_id: uuid.UUID,
        file_name: str,
        file_content: IO[bytes],
        content_type: str
    ) -> str:
        """Sube un archivo al Storage Service."""
        # Asume que el Storage Service espera multipart/form-data
        files = {'file': (file_name, file_content, content_type)}
        data = {'company_id': str(company_id)}
        # El endpoint real dependerá de la API del Storage Service
        response = await self._request("POST", "/upload", data=data, files=files)
        # Asume que devuelve JSON con la ruta o ID del archivo
        response_data = response.json()
        if "file_path" not in response_data:
             raise ValueError("Storage service response did not contain 'file_path'")
        return response_data["file_path"]

# Añadir métodos para get_file si es necesario para OCR