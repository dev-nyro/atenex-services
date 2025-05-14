# sparse-search-service/app/application/use_cases/load_and_search_index_use_case.py
import uuid
import json
import structlog
from typing import List, Optional, Any
from pathlib import Path
import asyncio

from app.domain.models import SparseSearchResultItem
from app.application.ports.sparse_index_storage_port import SparseIndexStoragePort
from app.application.ports.sparse_search_port import SparseSearchPort 
from app.infrastructure.cache.index_lru_cache import IndexLRUCache, CachedIndexData
from app.infrastructure.sparse_retrieval.bm25_adapter import BM25Adapter 

# LLM: REMOVED - No es necesario importar bm2s aquí
# try:
#     import bm2s
# except ImportError:
#     bm2s = None

log = structlog.get_logger(__name__)

class LoadAndSearchIndexUseCase:
    def __init__(
        self,
        index_cache: IndexLRUCache,
        index_storage: SparseIndexStoragePort,
        sparse_search_engine: SparseSearchPort 
    ):
        self.index_cache = index_cache
        self.index_storage = index_storage
        self.sparse_search_engine = sparse_search_engine 
        log.info(
            "LoadAndSearchIndexUseCase initialized",
            cache_type=type(index_cache).__name__,
            storage_type=type(index_storage).__name__,
            search_engine_type=type(sparse_search_engine).__name__
        )

    async def execute(
        self,
        query: str,
        company_id: uuid.UUID,
        top_k: int
    ) -> List[SparseSearchResultItem]:
        use_case_log = log.bind(
            use_case="LoadAndSearchIndexUseCase",
            action="execute",
            company_id=str(company_id),
            query_preview=query[:50] + "...",
            requested_top_k=top_k
        )
        use_case_log.info("Executing load-and-search for sparse index.")

        cached_data: Optional[CachedIndexData] = self.index_cache.get(company_id)
        bm25_instance: Optional[Any] = None 
        id_map: Optional[List[str]] = None

        if cached_data:
            bm25_instance, id_map = cached_data
            use_case_log.info("BM25 index found in LRU cache.")
        else:
            use_case_log.info("BM25 index not in cache. Attempting to load from GCS.")
            
            local_bm2s_path_str, local_id_map_path_str = await self.index_storage.load_index_files(company_id)

            if local_bm2s_path_str and local_id_map_path_str:
                local_bm2s_path = Path(local_bm2s_path_str)
                local_id_map_path = Path(local_id_map_path_str)
                try:
                    use_case_log.debug("Loading BM25 instance from local file...", file_path=str(local_bm2s_path))
                    # LLM: La carga ahora es manejada por el adapter
                    bm25_instance = BM25Adapter.load_bm2s_from_file(str(local_bm2s_path))
                    
                    use_case_log.debug("Loading ID map from local JSON file...", file_path=str(local_id_map_path))
                    with open(local_id_map_path, 'r') as f:
                        id_map = json.load(f)
                    
                    if not isinstance(id_map, list):
                        use_case_log.error("ID map loaded from JSON is not a list.", id_map_type=type(id_map).__name__)
                        raise ValueError("ID map must be a list.")

                    use_case_log.info("BM25 index and ID map loaded successfully from GCS files.")
                    
                    self.index_cache.put(company_id, bm25_instance, id_map)
                    use_case_log.info("BM25 index and ID map stored in LRU cache.")

                except Exception as e_load:
                    use_case_log.error("Failed to load index/id_map from downloaded files.", error=str(e_load), exc_info=True)
                    bm25_instance = None
                    id_map = None
                finally:
                    try:
                        if local_bm2s_path.exists(): local_bm2s_path.unlink()
                        if local_id_map_path.exists(): local_id_map_path.unlink()
                        temp_dir = local_bm2s_path.parent
                        if temp_dir.is_dir() and not any(temp_dir.iterdir()): # Solo borrar si está vacío
                            temp_dir.rmdir()
                    except OSError as e_clean:
                        use_case_log.error("Error cleaning up temporary index files.", error=str(e_clean))
            else:
                use_case_log.warning("Index files not found in GCS or download failed. Cannot perform search.")
                return [] 

        if not bm25_instance or not id_map:
            use_case_log.error("BM25 instance or ID map is not available after cache/GCS lookup. Cannot search.")
            return []

        use_case_log.debug("Performing search with loaded BM25 instance and ID map...")
        try:
            # LLM: El adapter ahora espera la instancia bm25 y el id_map
            search_results: List[SparseSearchResultItem] = await self.sparse_search_engine.search(
                query=query,
                bm25_instance=bm25_instance,
                id_map=id_map,
                top_k=top_k,
                company_id=company_id 
            )
            use_case_log.info(f"Search executed. Found {len(search_results)} results.")
            return search_results
        except Exception as e_search:
            use_case_log.exception("An unexpected error occurred during search execution with loaded index.")
            raise RuntimeError(f"Search with loaded index failed: {e_search}") from e_search