# sparse-search-service/app/infrastructure/sparse_retrieval/bm25_adapter.py
import structlog
import asyncio
import time
import uuid
from typing import List, Tuple, Dict, Any, Optional
from pathlib import Path
import pickle 


try:
    from rank_bm25 import BM25Okapi, BM25Plus 
except ImportError:
    BM25Okapi = None
    BM25Plus = None

from app.application.ports.sparse_search_port import SparseSearchPort
from app.domain.models import SparseSearchResultItem
from app.core.config import settings 

log = structlog.get_logger(__name__)

BM25_IMPLEMENTATION = BM25Okapi 

class BM25Adapter(SparseSearchPort):
    def __init__(self):
        self._bm2s_available = False 
        if BM25_IMPLEMENTATION is None:
            log.error(
                
                "rank_bm25 library not installed or its classes (BM25Okapi/BM25Plus) not found. BM25 search functionality will be UNAVAILABLE. "
                "Install with: poetry add rank_bm25"
            )
        else:
            self._bm2s_available = True 
            log.info(f"BM25Adapter initialized. rank_bm25 library ({BM25_IMPLEMENTATION.__name__}) is available.")

    async def initialize_engine(self) -> None:
        if not self._bm2s_available: 
            log.warning(f"BM25 engine (rank_bm25 library - {BM25_IMPLEMENTATION.__name__ if BM25_IMPLEMENTATION else 'N/A'}) not available. Search will fail if attempted.")
        else:
            log.info(f"BM25 engine (rank_bm25 library - {BM25_IMPLEMENTATION.__name__}) available and ready.")

    def is_available(self) -> bool:
        return self._bm2s_available 

    @staticmethod
    def load_bm2s_from_file(file_path: str) -> Any: 
        load_log = log.bind(action="load_bm25_from_file", file_path=file_path)
        if not BM25_IMPLEMENTATION:
            load_log.error("rank_bm25 library not available, cannot load index.")
            raise RuntimeError("rank_bm25 library is not installed.")
        try:
            with open(file_path, 'rb') as f:
                loaded_retriever = pickle.load(f)
            
            if not isinstance(loaded_retriever, BM25_IMPLEMENTATION):
                load_log.error(f"Loaded object is not of type {BM25_IMPLEMENTATION.__name__}", loaded_type=type(loaded_retriever).__name__)
                raise TypeError(f"Expected {BM25_IMPLEMENTATION.__name__}, got {type(loaded_retriever).__name__}")

            load_log.info(f"BM25 index ({BM25_IMPLEMENTATION.__name__}) loaded successfully from file.")
            return loaded_retriever
        except FileNotFoundError:
            load_log.error("BM25 index file not found.")
            raise
        except Exception as e:
            load_log.exception("Failed to load BM25 index from file.")
            raise RuntimeError(f"Failed to load BM25 index from {file_path}: {e}") from e

    @staticmethod
    def dump_bm2s_to_file(instance: Any, file_path: str): 
        dump_log = log.bind(action="dump_bm25_to_file", file_path=file_path)
        if not BM25_IMPLEMENTATION:
            dump_log.error("rank_bm25 library not available, cannot dump index.")
            raise RuntimeError("rank_bm25 library is not installed.")
        if not isinstance(instance, BM25_IMPLEMENTATION): 
            dump_log.error(f"Invalid instance type provided for dumping. Expected {BM25_IMPLEMENTATION.__name__}", instance_type=type(instance).__name__)
            raise TypeError(f"Instance to dump must be a {BM25_IMPLEMENTATION.__name__} object.")
        try:
            with open(file_path, 'wb') as f:
                pickle.dump(instance, f)
            dump_log.info(f"BM25 index ({BM25_IMPLEMENTATION.__name__}) dumped successfully to file.")
        except Exception as e:
            dump_log.exception("Failed to dump BM25 index to file.")
            raise RuntimeError(f"Failed to dump BM25 index to {file_path}: {e}") from e

    async def search(
        self,
        query: str,
        bm25_instance: Any, 
        id_map: List[str],  
        top_k: int,
        company_id: Optional[uuid.UUID] = None 
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
            adapter_log.error("rank_bm25 library not available. Cannot perform BM25 search.")
            return []
        
        if not bm25_instance or not isinstance(bm25_instance, BM25_IMPLEMENTATION):
            adapter_log.error(f"Invalid or no BM25 instance provided. Expected {BM25_IMPLEMENTATION.__name__}, got {type(bm25_instance).__name__}")
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
            tokenized_query = query.lower().split() 
            
            doc_scores = bm25_instance.get_scores(tokenized_query)
            
            scored_indices = []
            for i, score in enumerate(doc_scores):
                if i < len(id_map): 
                    scored_indices.append((score, i))
            
            scored_indices.sort(key=lambda x: x[0], reverse=True)
            
            top_n_results = scored_indices[:top_k]

            retrieval_time_ms = (time.monotonic() - start_time) * 1000
            adapter_log.debug(f"BM25 retrieval complete. Hits considered: {len(doc_scores)}, Top_k requested: {top_k}.",
                              duration_ms=round(retrieval_time_ms,2))

            final_results: List[SparseSearchResultItem] = []
            for score_val, original_index in top_n_results:
                original_chunk_id = id_map[original_index]
                final_results.append(
                    SparseSearchResultItem(chunk_id=original_chunk_id, score=float(score_val))
                )
            
            adapter_log.info(
                f"BM25 search finished. Returning {len(final_results)} results.",
                total_duration_ms=round(retrieval_time_ms, 2)
            )
            return final_results

        except Exception as e:
            adapter_log.exception("Error during BM25 search processing with pre-loaded instance.")
            return []