# reranker-service/app/domain/models.py
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional

class DocumentToRerank(BaseModel):
    id: str = Field(..., description="Unique identifier for the document or chunk.")
    text: str = Field(..., min_length=1, description="The text content of the document or chunk to be reranked.")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Original metadata associated with the document.")

class RerankedDocument(BaseModel):
    id: str = Field(..., description="Unique identifier for the document or chunk.")
    text: str = Field(..., description="The text content (can be omitted if client doesn't need it back, but useful for debugging).")
    score: float = Field(..., description="Relevance score assigned by the reranker model.")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Original metadata preserved.")

class ModelInfo(BaseModel):
    model_name: str = Field(..., description="Name of the reranker model used.")
    # Potentially add device info if it's useful for the client to know
    # model_device: Optional[str] = None 

class RerankResponseData(BaseModel):
    reranked_documents: List[RerankedDocument]
    model_info: ModelInfo