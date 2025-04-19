# ingest-service/app/api/v1/schemas.py
import uuid
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Dict, Any, List
from app.models.domain import DocumentStatus # Importar DocumentStatus si se usa directamente
from datetime import datetime
import json
import logging # Importar logging

# LLM_COMMENT: Asegurarse de que Pydantic maneje correctamente los tipos UUID y datetime.
# LLM_COMMENT: Definir modelos claros para las respuestas de la API.

log = logging.getLogger(__name__) # Usar logging estándar si structlog no está configurado aquí

class ErrorDetail(BaseModel):
    """Schema for error details in responses."""
    detail: str

class IngestResponse(BaseModel):
    document_id: uuid.UUID
    task_id: str
    status: str # Idealmente usar DocumentStatus, pero str es más simple para respuesta directa
    message: str = "Document upload received and queued for processing."

    class Config:
        json_schema_extra = {
            "example": {
                "document_id": "123e4567-e89b-12d3-a456-426614174000",
                "task_id": "c79ba436-fe88-4b82-9afc-44b1091564e4", # Example task ID
                "status": DocumentStatus.UPLOADED.value,
                "message": "Document upload accepted, processing started."
            }
        }

class StatusResponse(BaseModel):
    # LLM_COMMENT: Usar alias 'id' para document_id si es conveniente para el frontend.
    document_id: uuid.UUID = Field(..., alias="id")
    # LLM_COMMENT: Incluir todos los campos relevantes del documento para la UI.
    company_id: Optional[uuid.UUID] = None # Hacer opcional si no siempre está presente
    file_name: str
    file_type: str
    file_path: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    status: str # Podría ser DocumentStatus, pero str es más flexible
    chunk_count: Optional[int] = 0
    error_message: Optional[str] = None
    uploaded_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # LLM_COMMENT: Campos dinámicos añadidos por los endpoints de status.
    minio_exists: Optional[bool] = None
    milvus_chunk_count: Optional[int] = None # Usar Optional[int] en lugar de valor centinela -1
    message: Optional[str] = None

    @field_validator('metadata', mode='before')
    @classmethod
    def metadata_to_dict(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                # LLM_COMMENT: Loggear error si metadata en DB no es JSON válido.
                log.warning("Invalid metadata JSON found in DB record", raw_metadata=v)
                return {"error": "invalid metadata JSON in DB"}
        # LLM_COMMENT: Permitir que metadata sea None o ya un diccionario.
        return v if v is None or isinstance(v, dict) else {}

    class Config:
        validate_assignment = True
        populate_by_name = True # Permite usar 'id' o 'document_id'
        json_schema_extra = {
             "example": {
                "id": "52ad2ba8-cab9-4108-a504-b9822fe99bdc",
                "company_id": "51a66c8f-f6b1-43bd-8038-8768471a8b09",
                "file_name": "Anexo-00-Modificaciones-de-la-Guia-5.1.0.pdf",
                "file_type": "application/pdf",
                "file_path": "51a66c8f-f6b1-43bd-8038-8768471a8b09/52ad2ba8-cab9-4108-a504-b9822fe99bdc/Anexo-00-Modificaciones-de-la-Guia-5.1.0.pdf",
                "metadata": {"source": "manual upload", "version": "1.1"},
                "status": DocumentStatus.ERROR.value, # Ejemplo de estado de error
                "chunk_count": 0, # DB value (puede ser 0 si hubo error antes de procesar)
                "error_message": "Processing timed out after 600 seconds.", # Ejemplo de mensaje de error
                "uploaded_at": "2025-04-19T19:42:38.671016Z",
                "updated_at": "2025-04-19T19:42:42.337854Z",
                "minio_exists": True, # Resultado de la verificación en vivo
                "milvus_chunk_count": 0, # Resultado de la verificación en vivo (0 si no hay chunks)
                "message": "El procesamiento falló: Timeout." # Mensaje descriptivo añadido por el endpoint
            }
        }

# --- Definición Faltante ---
class PaginatedStatusResponse(BaseModel):
    """Schema for paginated list of document statuses."""
    items: List[StatusResponse] = Field(..., description="List of document status objects on the current page.")
    total: int = Field(..., description="Total number of documents matching the query.")
    limit: int = Field(..., description="Number of items requested per page.")
    offset: int = Field(..., description="Number of items skipped for pagination.")

    class Config:
        json_schema_extra = {
            "example": {
                "items": [
                    # Aquí iría un ejemplo de StatusResponse (como el de arriba)
                    {
                        "id": "52ad2ba8-cab9-4108-a504-b9822fe99bdc",
                        "company_id": "51a66c8f-f6b1-43bd-8038-8768471a8b09",
                        "file_name": "Anexo-00-Modificaciones-de-la-Guia-5.1.0.pdf",
                        "file_type": "application/pdf",
                        "file_path": "51a66c8f-f6b1-43bd-8038-8768471a8b09/52ad2ba8-cab9-4108-a504-b9822fe99bdc/Anexo-00-Modificaciones-de-la-Guia-5.1.0.pdf",
                        "metadata": {"source": "manual upload", "version": "1.1"},
                        "status": DocumentStatus.ERROR.value,
                        "chunk_count": 0,
                        "error_message": "Processing timed out after 600 seconds.",
                        "uploaded_at": "2025-04-19T19:42:38.671016Z",
                        "updated_at": "2025-04-19T19:42:42.337854Z",
                        "minio_exists": True,
                        "milvus_chunk_count": 0,
                        "message": "El procesamiento falló: Timeout."
                    }
                    # ... (podrían ir más items)
                ],
                "total": 1,
                "limit": 30,
                "offset": 0
            }
        }