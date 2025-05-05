# ingest-service/app/models/domain.py
import uuid
from enum import Enum
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any

class DocumentStatus(str, Enum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    PROCESSED = "processed"
    # INDEXED = "indexed" # Merged into PROCESSED
    ERROR = "error"
    PENDING = "pending"

class ChunkVectorStatus(str, Enum):
    """Possible status values for the vector associated with a chunk."""
    PENDING = "pending"     # Initial state before Milvus insertion attempt
    CREATED = "created"     # Successfully inserted into Milvus
    ERROR = "error"         # Failed to insert into Milvus

class DocumentChunkMetadata(BaseModel):
    """Structure for metadata stored in the JSONB field of document_chunks."""
    page: Optional[int] = None
    title: Optional[str] = None
    tokens: Optional[int] = None
    content_hash: Optional[str] = Field(None, max_length=64) # e.g., SHA256 hex digest

class DocumentChunkData(BaseModel):
    """Internal representation of a chunk before DB insertion."""
    document_id: uuid.UUID
    company_id: uuid.UUID
    chunk_index: int
    content: str
    metadata: DocumentChunkMetadata = Field(default_factory=DocumentChunkMetadata)
    embedding_id: Optional[str] = None # Populated after Milvus insertion
    vector_status: ChunkVectorStatus = ChunkVectorStatus.PENDING