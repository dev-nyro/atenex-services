# embedding-service/app/domain/models.py
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional

# Currently, requests and responses are simple enough to be handled by API schemas.
# This file is a placeholder if more complex domain logic/entities arise.

# Example of a potential domain model if needed:
# class EmbeddingResult(BaseModel):
#     text_id: Optional[str] = None # If texts need to be identified
#     vector: List[float]
#     source_text_preview: str # For context