import uuid
from datetime import datetime
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field
from app.domain.enums import DocumentStatus, ChunkVectorStatus

class Document(BaseModel):
    id: uuid.UUID
    company_id: uuid.UUID
    file_name: str
    file_type: str
    file_path: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict)
    status: DocumentStatus
    chunk_count: Optional[int] = 0
    error_message: Optional[str] = None
    uploaded_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    gcs_exists: Optional[bool] = None
    milvus_chunk_count_live: Optional[int] = None

    class Config:
        from_attributes = True

class ChunkMetadata(BaseModel):
    page: Optional[int] = None
    title: Optional[str] = None
    tokens: Optional[int] = None
    content_hash: Optional[str] = Field(None, max_length=64)

class Chunk(BaseModel):
    id: Optional[uuid.UUID] = None
    document_id: uuid.UUID
    company_id: uuid.UUID
    chunk_index: int
    content: str
    metadata: ChunkMetadata = Field(default_factory=ChunkMetadata)
    embedding_id: Optional[str] = None
    vector_status: ChunkVectorStatus = ChunkVectorStatus.PENDING
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True
