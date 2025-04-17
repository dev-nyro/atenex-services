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
    """Respuesta devuelta al iniciar la ingesta."""
    document_id: uuid.UUID
    task_id: Optional[str] = None # ID de la tarea Celery
    status: DocumentStatus = DocumentStatus.UPLOADED # Estado inicial UPLOADED
    message: str = "Document upload received and queued for processing."

class StatusResponse(BaseModel):
    """Schema para representar el estado de un documento."""
    # Usar alias para mapear nombres de columnas de DB a nombres de campo API
    document_id: uuid.UUID = Field(..., alias="id")
    status: DocumentStatus # El enum se valida automáticamente
    file_name: Optional[str] = None
    file_type: Optional[str] = None
    chunk_count: Optional[int] = None
    # Estado actual en MinIO
    minio_exists: Optional[bool] = None
    # Número de chunks indexados en Milvus
    milvus_chunk_count: Optional[int] = None
    last_updated: Optional[datetime] = Field(None, alias="updated_at")
    # Mensaje descriptivo añadido en el endpoint, no viene de la DB directamente
    message: Optional[str] = Field(None, exclude=False) # Incluir en respuesta si se añade

    # Configuración Pydantic v2 para mapeo y creación desde atributos
    model_config = {
        "populate_by_name": True, # Permite usar 'alias' para mapear desde nombres de DB/dict
        "from_attributes": True   # Permite crear instancia desde un objeto con atributos (como asyncpg.Record)
    }