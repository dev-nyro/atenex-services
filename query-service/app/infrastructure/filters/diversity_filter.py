# query-service/app/infrastructure/filters/diversity_filter.py
import structlog
from typing import List

from app.application.ports.retrieval_ports import DiversityFilterPort
from app.domain.models import RetrievedChunk

log = structlog.get_logger(__name__)

class StubDiversityFilter(DiversityFilterPort):
    """
    Implementación Stub del filtro de diversidad.
    Simplemente devuelve los primeros k_final chunks sin aplicar lógica de diversidad.
    """

    def __init__(self):
        log.warning("Using StubDiversityFilter. No diversity logic is applied.", adapter="StubDiversityFilter")

    async def filter(self, chunks: List[RetrievedChunk], k_final: int) -> List[RetrievedChunk]:
        """Devuelve los primeros k_final chunks."""
        filter_log = log.bind(adapter="StubDiversityFilter", action="filter", k_final=k_final, input_count=len(chunks))
        if not chunks:
            filter_log.debug("No chunks to filter.")
            return []

        filtered_chunks = chunks[:k_final]
        filter_log.debug(f"Returning top {len(filtered_chunks)} chunks without diversity filtering.")
        return filtered_chunks

# TODO: Implementar MMRDiversityFilter o DartboardFilter aquí en el futuro.
# class MMRDiversityFilter(DiversityFilterPort):
#     async def filter(self, chunks: List[RetrievedChunk], k_final: int) -> List[RetrievedChunk]:
#         # Implementación de MMR... necesitaría embeddings
#         raise NotImplementedError