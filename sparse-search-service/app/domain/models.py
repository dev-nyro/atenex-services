# sparse-search-service/app/domain/models.py
import uuid
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

class SparseSearchResultItem(BaseModel):
    """
    Representa un único item de resultado de la búsqueda dispersa (BM25).
    Contiene el ID del chunk y su score de relevancia.
    """
    chunk_id: str = Field(..., description="El ID único del chunk (generalmente el embedding_id o pk_id de Milvus/PostgreSQL).")
    score: float = Field(..., description="La puntuación de relevancia asignada por el algoritmo BM25.")
    # No se incluye el contenido aquí para mantener el servicio enfocado.
    # El servicio que consume este resultado (e.g., Query Service)
    # será responsable de obtener el contenido si es necesario.

class CompanyCorpusStats(BaseModel):
    """
    Estadísticas sobre el corpus de una compañía utilizado para la indexación BM25.
    """
    company_id: uuid.UUID
    total_chunks_in_db: int
    chunks_indexed_in_bm25: int
    last_indexed_at: Optional[Any] # datetime, pero Any por si se usa timestamp numérico
    index_size_bytes: Optional[int] # Estimación del tamaño del índice en memoria