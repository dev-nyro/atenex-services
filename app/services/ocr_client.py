from .base_client import BaseServiceClient
from app.core.config import settings
import uuid

class OcrServiceClient(BaseServiceClient):
    def __init__(self):
        super().__init__(base_url=str(settings.OCR_SERVICE_URL), service_name="OcrService")

    async def extract_text(self, company_id: uuid.UUID, file_path: str) -> str:
        """Solicita la extracción de texto OCR."""
        # Asume que el OCR service espera JSON con la ruta y company_id
        payload = {"company_id": str(company_id), "file_path": file_path}
        response = await self._request("POST", "/extract", json=payload)
        response_data = response.json()
        if "extracted_text" not in response_data:
             raise ValueError("OCR service response did not contain 'extracted_text'")
        return response_data["extracted_text"]