# ingest-service/app/api/v1/schemas.py
import uuid
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Dict, Any, List
from app.models.domain import DocumentStatus
from datetime import datetime
import json

class IngestResponse(BaseModel):
    document_id: uuid.UUID
    task_id: str
    status: str
    message: str = "Document upload received and queued for processing."

    class Config:
        json_schema_extra = { # Corregido de schema_extra
            "example": {
                "document_id": "123e4567-e89b-12d3-a456-426614174000",
                "task_id": "abcd1234efgh",
                "status": "uploaded", # Estado inicial devuelto por la API
                "message": "Document upload received and queued for processing."
            }
        }

class StatusResponse(BaseModel):
    document_id: uuid.UUID = Field(..., alias="id")
    company_id: uuid.UUID
    file_name: str
    file_type: str
    file_path: Optional[str] = None # Puede ser None inicialmente
    metadata: Optional[Dict[str, Any]] = None
    status: str
    chunk_count: Optional[int] = 0 # Default a 0, puede ser None si hay error
    error_message: Optional[str] = None
    uploaded_at: datetime
    updated_at: datetime

    # Fields added dynamically by status endpoints
    minio_exists: Optional[bool] = None # Puede ser None si no se pudo verificar
    milvus_chunk_count: Optional[int] = None # Puede ser None si no se pudo verificar o -1 si hubo error
    message: Optional[str] = None # Mensaje descriptivo del estado actual

    # Validador para convertir metadata de string a dict si es necesario
    @field_validator('metadata', mode='before')
    def metadata_to_dict(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return {"error": "invalid metadata JSON in DB"}
        return v

    class Config:
        validate_assignment = True # Permitir validar al asignar campos dinámicos
        populate_by_name = True # Permitir usar 'id' en lugar de 'document_id'
        json_schema_extra = { # Corregido de schema_extra
            "example": {
                "id": "123e4567-e89b-12d3-a456-426614174000",
                "company_id": "51a66c8f-f6b1-43bd-8038-8768471a8b09",
                "file_name": "document.pdf",
                "file_type": "application/pdf",
                "file_path": "51a66c8f-f6b1-43bd-8038-8768471a8b09/123e4567-e89b-12d3-a456-426614174000/document.pdf",
                "metadata": {"source": "web upload"},
                "status": "processed",
                "chunk_count": 10, # DB value
                "error_message": None,
                "uploaded_at": "2025-04-18T20:00:00Z",
                "updated_at": "2025-04-18T20:30:00Z",
                "minio_exists": True, # Realtime check
                "milvus_chunk_count": 10, # Realtime check (-1 if error)
                "message": "Documento procesado correctamente." # Descriptive message
            }
        }