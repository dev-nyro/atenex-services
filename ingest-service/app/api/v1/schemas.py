import uuid
from pydantic import BaseModel, Field, Json # Json no es necesario aquí ahora
from typing import Optional, Dict, Any, List
from app.models.domain import DocumentStatus
from datetime import datetime

# Pydantic schema for metadata validation (optional but recommended)
# class DocumentMetadata(BaseModel):
#     category: Optional[str] = None
#     author: Optional[str] = None
#     # Add other expected metadata fields

class IngestRequest(BaseModel):
    # company_id vendrá de la dependencia/header, no de este modelo
    # metadata ahora se maneja como Form(metadata_json) en el endpoint
    pass # Este modelo ya no es estrictamente necesario para el endpoint actual

class IngestResponse(BaseModel):
    document_id: uuid.UUID
    task_id: Optional[str] = None # Devolver ID de tarea Celery para seguimiento
    status: DocumentStatus = DocumentStatus.UPLOADED # Estado inicial devuelto
    message: str = "Document upload received and queued for processing."

# Schema para la respuesta de estado (usado para GET individual y lista)
class StatusResponse(BaseModel):
    document_id: uuid.UUID = Field(..., alias="id") # Mapear 'id' de la DB a 'document_id'
    status: DocumentStatus
    file_name: Optional[str] = None
    file_type: Optional[str] = None
    chunk_count: Optional[int] = None
    error_message: Optional[str] = None
    last_updated: Optional[datetime] = Field(None, alias="updated_at") # Mapear 'updated_at' de la DB
    message: Optional[str] = None # Mensaje descriptivo (añadido en el endpoint)

    # Pydantic v2: Configuración para permitir alias y populación desde atributos
    model_config = {
        "populate_by_name": True, # Permite usar alias para mapear nombres de campos de DB
        "from_attributes": True # Necesario si se crean instancias desde objetos con atributos (como asyncpg.Record)
    }

# (Opcional) Si prefieres un modelo específico para la lista sin el campo 'message'
# class StatusListItemResponse(BaseModel):
#     document_id: uuid.UUID = Field(..., alias="id")
#     status: DocumentStatus
#     file_name: Optional[str] = None
#     file_type: Optional[str] = None
#     chunk_count: Optional[int] = None
#     error_message: Optional[str] = None
#     last_updated: Optional[datetime] = Field(None, alias="updated_at")
#
#     model_config = {
#         "populate_by_name": True,
#         "from_attributes": True
#     }
# En ese caso, el endpoint de lista usaría response_model=List[StatusListItemResponse]
# Pero usar StatusResponse para ambos es más simple para MVP.