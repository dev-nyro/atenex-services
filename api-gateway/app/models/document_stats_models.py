# File: app/models/document_stats_models.py
# NUEVO ARCHIVO
import uuid
from pydantic import BaseModel, Field
from typing import List, Optional, Dict
from datetime import datetime

class DocumentStatsByStatus(BaseModel):
    processed: int = 0
    processing: int = 0
    uploaded: int = 0
    error: int = 0

class DocumentStatsByType(BaseModel):
    pdf: int = 0
    docx: int = 0
    txt: int = 0
    # Puedes añadir más tipos si son relevantes
    # उदाहरण के लिए: md: int = 0, html: int = 0
    other: int = 0 # Para tipos no listados explícitamente

class DocumentStatsByUser(BaseModel):
    user_id: str # Podría ser UUID, pero el ejemplo usa 'u1'
    name: Optional[str] = None # Nombre del usuario, idealmente a obtener de la tabla users
    count: int

class DocumentRecentActivity(BaseModel):
    date: str # Podría ser datetime.date o str ISO8601
    uploaded: int = 0
    processed: int = 0
    error: int = 0

class DocumentStatsResponse(BaseModel):
    total_documents: int
    total_chunks: int
    by_status: DocumentStatsByStatus
    by_type: DocumentStatsByType
    by_user: List[DocumentStatsByUser]
    recent_activity: List[DocumentRecentActivity]
    oldest_document_date: Optional[datetime] = None
    newest_document_date: Optional[datetime] = None

    model_config = {
        "from_attributes": True
    }