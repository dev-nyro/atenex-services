import uuid
from pydantic import BaseModel, Field, Json # Use Json for automatic validation
from typing import Optional, Dict, Any, List
from app.models.domain import DocumentStatus
from datetime import datetime

# Pydantic schema for metadata validation (optional but recommended)
# class DocumentMetadata(BaseModel):
#     category: Optional[str] = None
#     author: Optional[str] = None
#     # Add other expected metadata fields

class IngestRequest(BaseModel):
    # company_id will come from dependency/header, not this model
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Metadata JSON for the document")

class IngestResponse(BaseModel):
    document_id: uuid.UUID
    task_id: Optional[str] = None # Return Celery task ID for tracking
    status: DocumentStatus = DocumentStatus.UPLOADED
    message: str = "Document upload received and queued for processing."

class StatusResponse(BaseModel):
    document_id: uuid.UUID
    status: DocumentStatus
    file_name: Optional[str] = None
    file_type: Optional[str] = None
    chunk_count: Optional[int] = None
    error_message: Optional[str] = None
    last_updated: Optional[datetime] = None # Use datetime for proper typing
    message: Optional[str] = None