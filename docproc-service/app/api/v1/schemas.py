# Re-exporting from domain models for clarity at API layer if needed,
# or define specific API DTOs if they differ from domain models.
# For this service, domain models are likely sufficient for API responses.

from app.domain.models import (
    ProcessResponse,
    ProcessResponseData,
    ProcessedDocumentMetadata,
    ProcessedChunk,
    ProcessedChunkSourceMetadata
)

__all__ = [
    "ProcessResponse",
    "ProcessResponseData",
    "ProcessedDocumentMetadata",
    "ProcessedChunk",
    "ProcessedChunkSourceMetadata"
]

# Example of an API-specific request schema if multipart form is not directly used by Pydantic model
# (FastAPI handles multipart form fields directly in endpoint signature)
# class ProcessRequest(BaseModel):
#     original_filename: str
#     content_type: str
#     document_id: Optional[str] = None # For tracing
#     company_id: Optional[str] = None  # For tracing