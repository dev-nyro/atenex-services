# reranker-service/app/api/v1/schemas.py
from pydantic import BaseModel, Field, field_validator
from typing import List, Optional

from app.domain.models import DocumentToRerank, RerankResponseData

class RerankRequest(BaseModel):
    query: str = Field(..., min_length=1, description="The user's query to rerank documents against.")
    documents: List[DocumentToRerank] = Field(..., min_items=1, description="A list of documents to be reranked.")
    top_n: Optional[int] = Field(None, gt=0, description="Optional. If provided, returns only the top N reranked documents.")

    @field_validator('documents')
    @classmethod
    def check_documents_not_empty(cls, v: List[DocumentToRerank]):
        if not v:
            raise ValueError('Documents list cannot be empty.')
        return v

class RerankResponse(BaseModel):
    data: RerankResponseData

class HealthCheckResponse(BaseModel):
    status: str
    service: str
    model_status: str
    model_name: Optional[str] = None