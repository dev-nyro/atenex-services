# embedding-service/app/api/v1/schemas.py
from pydantic import BaseModel, Field, conlist
from typing import List, Dict, Any, Literal

TextType = Literal["query", "passage"]

class EmbedRequest(BaseModel):
    texts: conlist(str, min_length=1) = Field(
        ...,
        description="A list of texts to be embedded. Each text must not be empty.",
        examples=[["Hello world", "Another piece of text"]]
    )

class ModelInfo(BaseModel):
    model_name: str = Field(..., description="Name of the embedding model used.")
    dimension: int = Field(..., description="Dimension of the generated embeddings.")

class EmbedResponse(BaseModel):
    embeddings: List[List[float]] = Field(..., description="A list of embeddings, where each embedding is a list of floats.")
    model_info: ModelInfo = Field(..., description="Information about the model used for embedding.")

    class Config:
        json_schema_extra = {
            "example": {
                "embeddings": [
                    [0.001, -0.02, ..., 0.03],
                    [0.04, 0.005, ..., -0.006]
                ],
                "model_info": {
                    "model_name": "text-embedding-3-small",
                    "dimension": 1536
                }
            }
        }

class HealthCheckResponse(BaseModel):
    status: str = Field(default="ok", description="Overall status of the service: 'ok' or 'error'.")
    service: str = Field(..., description="Name of the service.")
    model_status: str = Field(
        ..., 
        description="Status of the embedding model client: 'client_ready', 'client_error', 'client_not_initialized', 'client_initialization_pending_or_failed'."
    )
    model_name: str | None = Field(None, description="Name of the configured/used embedding model, if available.")
    model_dimension: int | None = Field(None, description="Dimension of the configured/used embedding model, if available.")

    class Config:
        json_schema_extra = {
            "example_healthy": {
                "status": "ok",
                "service": "Atenex Embedding Service",
                "model_status": "client_ready",
                "model_name": "text-embedding-3-small",
                "model_dimension": 1536
            },
            "example_unhealthy_init_failed": {
                "status": "error",
                "service": "Atenex Embedding Service",
                "model_status": "client_initialization_pending_or_failed",
                "model_name": "text-embedding-3-small",
                "model_dimension": 1536
            },
             "example_unhealthy_client_error": {
                "status": "error",
                "service": "Atenex Embedding Service",
                "model_status": "client_error",
                "model_name": "text-embedding-3-small",
                "model_dimension": 1536
            }
        }