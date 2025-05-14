# sparse-search-service/app/api/v1/schemas.py
import uuid
from pydantic import BaseModel, Field, conint
from typing import List, Optional, Dict, Any

from app.domain.models import SparseSearchResultItem # Reutilizar el modelo de dominio

# --- Request Schemas ---

class SparseSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="La consulta del usuario en lenguaje natural.")
    company_id: uuid.UUID = Field(..., description="El ID de la compañía para la cual realizar la búsqueda.")
    top_k: conint(gt=0, le=200) = Field(default=10, description="El número máximo de resultados a devolver.")
    # metadata_filter: Optional[Dict[str, Any]] = Field(None, description="Filtros de metadatos adicionales (futuro).")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "query": "cómo configuro las notificaciones?",
                    "company_id": "a1b2c3d4-e5f6-7890-1234-567890abcdef",
                    "top_k": 5
                }
            ]
        }
    }

# --- Response Schemas ---

class SparseSearchResponse(BaseModel):
    query: str = Field(..., description="La consulta original enviada.")
    company_id: uuid.UUID = Field(..., description="El ID de la compañía para la cual se realizó la búsqueda.")
    results: List[SparseSearchResultItem] = Field(default_factory=list, description="Lista de chunks relevantes encontrados, ordenados por score descendente.")
    # performance_ms: Optional[float] = Field(None, description="Tiempo tomado para la búsqueda en milisegundos.")
    # index_info: Optional[Dict[str, Any]] = Field(None, description="Información sobre el índice BM25 utilizado (e.g., tamaño, fecha de creación).")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "query": "cómo configuro las notificaciones?",
                    "company_id": "a1b2c3d4-e5f6-7890-1234-567890abcdef",
                    "results": [
                        {"chunk_id": "doc_abc_chunk_3", "score": 15.76},
                        {"chunk_id": "doc_xyz_chunk_12", "score": 12.33}
                    ]
                }
            ]
        }
    }

class HealthCheckResponse(BaseModel):
    status: str = Field(default="ok", description="Overall status of the service ('ok' or 'error').")
    service: str = Field(..., description="Name of the service.")
    ready: bool = Field(..., description="Indicates if the service is ready to serve requests (dependencies are OK).")
    dependencies: Dict[str, str] = Field(..., description="Status of critical dependencies (e.g., 'PostgreSQL': 'ok'/'error').")
    # bm2s_available: bool = Field(..., description="Indicates if the bm2s library was successfully imported.")