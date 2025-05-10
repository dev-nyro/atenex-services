# embedding-service/app/api/v1/schemas.py
from pydantic import BaseModel, Field, conlist
from typing import List, Dict, Any

class EmbedRequest(BaseModel):
    texts: conlist(str, min_length=1) = Field(
        ...,
        description="A list of texts to be embedded. Each text must not be empty.",
        examples=[["Hello world", "Another piece of text"]]
    )

class ModelInfo(BaseModel):
    model_name: str = Field(..., description="Name of the embedding model used.")
    dimension: int = Field(..., description="Dimension of the generated embeddings.")
    # prefix: Optional[str] = Field(None, description="Prefix used for query embeddings, if any.")

class EmbedResponse(BaseModel):
    embeddings: List[List[float]] = Field(..., description="A list of embeddings, where each embedding is a list of floats.")
    model_info: ModelInfo = Field(..., description="Information about the model used for embedding.")

    class Config:
        json_schema_extra = {
            "example": {
                "embeddings": [
                    [0.1, 0.2, 0.3, -0.1, -0.2, -0.3],
                    [0.4, 0.5, 0.6, -0.4, -0.5, -0.6]
                ],
                "model_info": {
                    "model_name": "sentence-transformers/all-MiniLM-L6-v2",
                    "dimension": 384
                }
            }
        }

class HealthCheckResponse(BaseModel):
    status: str = Field(default="ok", description="Overall status of the service.")
    service: str = Field(..., description="Name of the service.")
    model_status: str = Field(..., description="Status of the embedding model ('loaded', 'error', 'not_loaded').")
    model_name: str | None = Field(None, description="Name of the loaded embedding model, if any.")
    model_dimension: int | None = Field(None, description="Dimension of the loaded embedding model, if any.")