# ingest-service/app/api/v1/schemas.py
import uuid
from pydantic import BaseModel, Field, field_validator
from typing import Optional, Dict, Any, List
from app.models.domain import DocumentStatus
from datetime import datetime, date # Asegurar que date está importado
import json
import logging

log = logging.getLogger(__name__)

class ErrorDetail(BaseModel):
    """Schema for error details in responses."""
    detail: str

class IngestResponse(BaseModel):
    document_id: uuid.UUID
    task_id: str
    status: str
    message: str = "Document upload received and queued for processing."

    class Config:
        json_schema_extra = {
            "example": {
                "document_id": "123e4567-e89b-12d3-a456-426614174000",
                "task_id": "c79ba436-fe88-4b82-9afc-44b1091564e4",
                "status": DocumentStatus.UPLOADED.value,
                "message": "Document upload accepted, processing started."
            }
        }

class StatusResponse(BaseModel):
    document_id: uuid.UUID = Field(..., alias="id")
    company_id: Optional[uuid.UUID] = None
    file_name: str
    file_type: str
    file_path: Optional[str] = Field(None, description="Path to the original file in GCS.")
    metadata: Optional[Dict[str, Any]] = None
    status: str
    chunk_count: Optional[int] = 0
    error_message: Optional[str] = None
    uploaded_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    gcs_exists: Optional[bool] = Field(None, description="Indicates if the original file currently exists in GCS.")
    milvus_chunk_count: Optional[int] = Field(None, description="Live count of chunks found in Milvus for this document (-1 if check failed).")
    message: Optional[str] = None

    @field_validator('metadata', mode='before')
    @classmethod
    def metadata_to_dict(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                log.warning("Invalid metadata JSON found in DB record", raw_metadata=v)
                return {"error": "invalid metadata JSON in DB"}
        return v if v is None or isinstance(v, dict) else {}

    class Config:
        validate_assignment = True
        populate_by_name = True
        json_schema_extra = {
             "example": {
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
                "gcs_exists": True,
                "milvus_chunk_count": 0,
                "message": "El procesamiento falló: Timeout."
            }
        }

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
                        "gcs_exists": True,
                        "milvus_chunk_count": 0,
                        "message": "El procesamiento falló: Timeout."
                    }
                ],
                "total": 1,
                "limit": 30,
                "offset": 0
            }
        }

# --- NUEVOS SCHEMAS PARA ESTADÍSTICAS DE DOCUMENTOS ---
class DocumentStatsByStatus(BaseModel):
    processed: int = 0
    processing: int = 0
    uploaded: int = 0
    error: int = 0
    pending: int = 0 # Añadir pending ya que es un DocumentStatus

class DocumentStatsByType(BaseModel):
    pdf: int = Field(0, alias="application/pdf")
    docx: int = Field(0, alias="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    doc: int = Field(0, alias="application/msword")
    txt: int = Field(0, alias="text/plain")
    md: int = Field(0, alias="text/markdown")
    html: int = Field(0, alias="text/html")
    other: int = 0

    class Config:
        populate_by_name = True


class DocumentStatsByUser(BaseModel):
    # Esta parte es más compleja de implementar en ingest-service si no tiene acceso a la tabla de users
    # Por ahora, la definición del schema está aquí, pero su implementación podría ser básica o diferida.
    user_id: str # Podría ser UUID del user que subió el doc, pero no está en la tabla 'documents'
    name: Optional[str] = None
    count: int

class DocumentRecentActivity(BaseModel):
    # Similar, agrupar por fecha y estado es una query más compleja.
    # Definición aquí, implementación podría ser básica.
    date: date
    uploaded: int = 0
    processing: int = 0
    processed: int = 0
    error: int = 0
    pending: int = 0

class DocumentStatsResponse(BaseModel):
    total_documents: int
    total_chunks_processed: int # Suma de chunk_count para documentos en estado 'processed'
    by_status: DocumentStatsByStatus
    by_type: DocumentStatsByType
    # by_user: List[DocumentStatsByUser] # Omitir por ahora para simplificar
    # recent_activity: List[DocumentRecentActivity] # Omitir por ahora para simplificar
    oldest_document_date: Optional[datetime] = None
    newest_document_date: Optional[datetime] = None

    model_config = { # Anteriormente Config
        "from_attributes": True # Anteriormente orm_mode
    }