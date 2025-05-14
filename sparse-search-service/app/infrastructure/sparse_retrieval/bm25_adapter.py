# sparse-search-service/app/infrastructure/sparse_retrieval/bm25_adapter.py
import structlog
import asyncio
import time
import uuid
from typing import List, Tuple, Dict, Any, Optional
from pathlib import Path


try:
    import bm2s
except ImportError:
    bm2s = None 

from app.application.ports.sparse_search_port import SparseSearchPort
from app.domain.models import SparseSearchResultItem
from app.core.config import settings 

log = structlog.get_logger(__name__)

class BM25Adapter(SparseSearchPort):
    """
    Implementación de SparseSearchPort usando la librería bm2s.
    Esta versión espera un índice BM25 pre-cargado.
    """

    def __init__(self):
        self._bm2s_available = False
        if bm2s is None:
            log.error(
                "bm2s library not installed. BM25 search functionality will be UNAVAILABLE. "
                "Install with: poetry add bm2s"
            )
        else:
            self._bm2s_available = True
            log.info("BM25Adapter initialized. bm2s library is available.")

    async def initialize_engine(self) -> None:
        if not self._bm2s_available:
            log.warning("BM25 engine (bm2s library) not available. Search will fail if attempted.")
        else:
            log.info("BM25 engine (bm2s library) available and ready.")

    def is_available(self) -> bool:
        return self._bm2s_available

    @staticmethod
    def load_bm2s_from_file(file_path: str) -> Any: 
        load_log = log.bind(action="load_bm2s_from_file", file_path=file_path)
        if not bm2s:
            load_log.error("bm2s library not available, cannot load index.")
            raise RuntimeError("bm2s library is not installed.")
        try:
            loaded_retriever = bm2s.BM25.load(file_path, anserini_path=None) 
            load_log.info("BM25 index loaded successfully from file.")
            return loaded_retriever
        except Exception as e:
            load_log.exception("Failed to load BM25 index from file.")
            raise RuntimeError(f"Failed to load BM25 index from {file_path}: {e}") from e

    @staticmethod
    def dump_bm2s_to_file(instance: Any, file_path: str): 
        dump_log = log.bind(action="dump_bm2s_to_file", file_path=file_path)
        if not bm2s:
            dump_log.error("bm2s library not available, cannot dump index.")
            raise RuntimeError("bm2s library is not installed.")
        if not isinstance(instance, bm2s.BM25):
            dump_log.error("Invalid instance type provided for dumping.", instance_type=type(instance).__name__)
            raise TypeError("Instance to dump must be a bm2s.BM25 object.")
        try:
            instance.dump(file_path)
            dump_log.info("BM25 index dumped successfully to file.")
        except Exception as e:
            dump_log.exception("Failed to dump BM25 index to file.")
            raise RuntimeError(f"Failed to dump BM25 index to {file_path}: {e}") from e

    async def search(
        self,
        query: str,
        bm25_instance: Any, 
        id_map: List[str],  
        top_k: int,
        company_id: Optional[uuid.UUID] = None # Añadido para logging
    ) -> List[SparseSearchResultItem]:
        adapter_log = log.bind(
            adapter="BM25Adapter",
            action="search_with_instance",
            company_id=str(company_id) if company_id else "N/A",
            query_preview=query[:50] + "...",
            num_ids_in_map=len(id_map),
            top_k=top_k
        )

        if not self._bm2s_available:
            adapter_log.error("bm2s library not available. Cannot perform BM25 search.")
            return []
        
        if not bm25_instance or not isinstance(bm25_instance, bm2s.BM25):
            adapter_log.error("Invalid or no BM25 instance provided for search.")
            return []

        if not id_map:
            adapter_log.warning("ID map is empty. No way to map results to original chunk IDs.")
            return []
            
        if not query.strip():
            adapter_log.warning("Query is empty. Returning no results.")
            return []

        start_time = time.monotonic()
        adapter_log.debug("Starting BM25 search with pre-loaded instance...")

        try:
            results_indices_per_query, results_scores_per_query = bm25_instance.retrieve(
                query, 
                k=top_k
            )
            
            doc_indices: List[int] = results_indices_per_query[0]
            scores: List[float] = results_scores_per_query[0]
            
            retrieval_time_ms = (time.monotonic() - start_time) * 1000
            adapter_log.debug(f"BM25s retrieval complete. Hits found: {len(doc_indices)}.",
                              duration_ms=round(retrieval_time_ms,2))

            final_results: List[SparseSearchResultItem] = []
            for i, score_val in zip(doc_indices, scores):
                if 0 <= i < len(id_map):
                    original_chunk_id = id_map[i]
                    final_results.append(
                        SparseSearchResultItem(chunk_id=original_chunk_id, score=float(score_val))
                    )
                else:
                    adapter_log.error(
                        "BM25s returned index out of bounds for the provided id_map.",
                        returned_index=i,
                        id_map_size=len(id_map)
                    )
            
            adapter_log.info(
                f"BM25 search finished. Returning {len(final_results)} results.",
                total_duration_ms=round(retrieval_time_ms, 2)
            )
            return final_results

        except Exception as e:
            adapter_log.exception("Error during BM25 search processing with pre-loaded instance.")
            return []