# reranker-service/app/api/v1/schemas.py
from pydantic import BaseModel, Field, field_validator, conlist
from typing import List, Optional

# Import domain models to be wrapped or used directly in API responses/requests
from app.domain.models import DocumentToRerank, RerankResponseData

class RerankRequest(BaseModel):
    query: str = Field(..., min_length=1, description="The user's query to rerank documents against.")
    # Use conlist to ensure at least one document is provided
    documents: conlist(DocumentToRerank, min_length=1) = Field( # type: ignore
        ..., 
        description="A list of documents to be reranked. Must contain at least one document."
    )
    top_n: Optional[int] = Field(
        None, 
        gt=0, 
        description="Optional. If provided, returns only the top N reranked documents."
    )

class RerankResponse(BaseModel):
    """
    Standard API response structure wrapping the actual data.
    """
    data: RerankResponseData

class HealthCheckResponse(BaseModel):
    """
    Response model for the health check endpoint.
    """
    status: str = Field(..., description="Overall status of the service (e.g., 'ok', 'error').")
    service: str = Field(..., description="Name of the service.")
    model_status: str = Field(..., description="Status of the reranker model (e.g., 'loaded', 'loading', 'error', 'unloaded').")
    model_name: Optional[str] = Field(None, description="Name of the reranker model if loaded or configured.")
    message: Optional[str] = Field(None, description="Additional details, especially in case of error.")