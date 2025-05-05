# query-service/app/domain/models.py
import uuid
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime

# Usaremos Pydantic por conveniencia, pero estas son conceptualmente entidades de dominio.

class Chat(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    company_id: uuid.UUID
    title: Optional[str]
    created_at: datetime
    updated_at: datetime

class ChatSummary(BaseModel):
    # Similar a schemas.ChatSummary pero como objeto de dominio si se necesita diferenciar
    id: uuid.UUID
    title: Optional[str]
    updated_at: datetime

class ChatMessage(BaseModel):
    id: uuid.UUID
    chat_id: uuid.UUID
    role: str # 'user' or 'assistant'
    content: str
    sources: Optional[List[Dict[str, Any]]] = None # Mantener estructura JSON por ahora
    created_at: datetime

class RetrievedChunk(BaseModel):
    """Representa un chunk recuperado de una fuente (ej: Milvus)."""
    id: str # ID del chunk (ej: Milvus PK)
    content: Optional[str] = None # Contenido textual del chunk
    score: Optional[float] = None # Puntuación de relevancia
    metadata: Dict[str, Any] = Field(default_factory=dict)
    # Campos comunes esperados en metadata
    document_id: Optional[str] = Field(None, alias="document_id") # Alias para mapeo desde meta
    file_name: Optional[str] = Field(None, alias="file_name")
    company_id: Optional[str] = Field(None, alias="company_id")

    # Permitir inicialización desde metadata
    # model_config = ConfigDict(populate_by_name=True) # Pydantic v2

    @classmethod
    def from_haystack_document(cls, doc: Any):
        """Convierte un Documento Haystack a un RetrievedChunk."""
        # Intenta extraer campos comunes directamente de la metadata
        doc_meta = doc.meta or {}
        # Asegura que los IDs se manejen correctamente
        doc_id_str = str(doc_meta.get("document_id")) if doc_meta.get("document_id") else None
        company_id_str = str(doc_meta.get("company_id")) if doc_meta.get("company_id") else None

        return cls(
            id=str(doc.id),
            content=doc.content,
            score=doc.score,
            metadata=doc_meta,
            document_id=doc_id_str,
            file_name=doc_meta.get("file_name"),
            company_id=company_id_str
        )

class QueryLog(BaseModel):
    id: uuid.UUID
    user_id: Optional[uuid.UUID]
    company_id: uuid.UUID
    query: str
    response: str
    metadata: Dict[str, Any]
    chat_id: Optional[uuid.UUID]
    created_at: datetime