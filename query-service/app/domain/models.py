# query-service/app/domain/models.py
import uuid
from pydantic import BaseModel, Field, ConfigDict
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
    sources: Optional[List[Dict[str, Any]]] = None 
    created_at: datetime

class RetrievedChunk(BaseModel):
    """Representa un chunk recuperado de una fuente (ej: Milvus)."""
    id: str 
    content: Optional[str] = None 
    score: Optional[float] = None 
    metadata: Dict[str, Any] = Field(default_factory=dict)
    embedding: Optional[List[float]] = None 
    
    document_id: Optional[str] = Field(None, alias="document_id") 
    file_name: Optional[str] = Field(None, alias="file_name")
    company_id: Optional[str] = Field(None, alias="company_id")

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)


    @classmethod
    def from_haystack_document(cls, doc: Any):
        """Convierte un Documento Haystack a un RetrievedChunk."""
        doc_meta = doc.meta or {}
        doc_id_str = str(doc_meta.get("document_id")) if doc_meta.get("document_id") else None
        company_id_str = str(doc_meta.get("company_id")) if doc_meta.get("company_id") else None
        embedding_vector = getattr(doc, 'embedding', None)

        return cls(
            id=str(doc.id),
            content=doc.content,
            score=doc.score,
            metadata=doc_meta,
            embedding=embedding_vector, 
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

# --- Nuevos modelos para Respuesta Estructurada ---
class FuenteCitada(BaseModel):
    id_documento: Optional[str] = Field(None, description="El ID del chunk o documento original, si está disponible en la metadata del chunk.")
    nombre_archivo: str = Field(..., description="Nombre del archivo fuente.")
    pagina: Optional[str] = Field(None, description="Número de página si está disponible.")
    score: Optional[float] = Field(None, description="Score de relevancia original del chunk (si aplica).")
    cita_tag: str = Field(..., description="La etiqueta de cita usada en el texto, ej: '[Doc 1]'.")

class RespuestaEstructurada(BaseModel):
    resumen_ejecutivo: Optional[str] = Field(None, description="Un breve resumen de 1-2 frases, si la respuesta es larga y aplica.")
    respuesta_detallada: str = Field(..., description="La respuesta completa y elaborada, incluyendo citas [Doc N] donde corresponda.")
    fuentes_citadas: List[FuenteCitada] = Field(default_factory=list, description="Lista de los documentos efectivamente utilizados y citados en la respuesta_detallada.")
    siguiente_pregunta_sugerida: Optional[str] = Field(None, description="Una pregunta de seguimiento relevante que el usuario podría hacer, si aplica.")
    
    model_config = ConfigDict(extra='ignore') 

class SparseSearchResultItem(BaseModel):
    """
    Representa un ítem de resultado de búsqueda dispersa devuelto por el sparse-search-service.
    Este modelo se utiliza en el query-service para tipar la respuesta del cliente
    de dicho servicio.
    """
    chunk_id: str = Field(..., description="El ID del chunk (usualmente el embedding_id de la tabla document_chunks).")
    score: float = Field(..., description="El score BM25 asignado al chunk.")