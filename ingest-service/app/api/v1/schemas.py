# ingest-service/app/api/v1/schemas.py
import uuid
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from app.models.domain import DocumentStatus
from datetime import datetime

# Ya no se usa IngestRequest aquí, se maneja con Form y File en el endpoint
# class IngestRequest(BaseModel):
#     pass

class IngestResponse(BaseModel):
    document_id: uuid.UUID
    task_id: str
    status: str
    message: str = "Document upload received and queued for processing."

    class Config:
        schema_extra = {
            "example": {
                "document_id": "123e4567-e89b-12d3-a456-426614174000",
                "task_id": "abcd1234efgh",
                "status": "processing",
                "message": "Document upload received and queued for processing."
            }
        }

class StatusResponse(BaseModel):
    document_id: uuid.UUID = Field(..., alias="id")
    company_id: uuid.UUID
    file_name: str
    file_type: str
    file_path: Optional[str]
    metadata: Optional[Dict[str, Any]]
    status: str
    chunk_count: int
    error_message: Optional[str]
    uploaded_at: datetime
    updated_at: datetime

    # Fields added by status endpoints
    minio_exists: bool
    milvus_chunk_count: int
    message: str

    class Config:
        allow_population_by_field_name = True
        schema_extra = {
            "example": {
                "id": "123e4567-e89b-12d3-a456-426614174000",
                "company_id": "51a66c8f-f6b1-43bd-8038-8768471a8b09",
                "file_name": "document.pdf",
                "file_type": "application/pdf",
                "file_path": "51a66c8f-f6b1-43bd-8038-8768471a8b09/123e4567-e89b-12d3-a456-426614174000/document.pdf",
                "metadata": {},
                "status": "processed",
                "chunk_count": 10,
                "error_message": null,
                "uploaded_at": "2025-04-18T20:00:00Z",
                "updated_at": "2025-04-18T20:30:00Z",
                "minio_exists": true,
                "milvus_chunk_count": 10,
                "message": "Document processed successfully."
            }
        }