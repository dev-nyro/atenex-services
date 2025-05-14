# sparse-search-service/app/dependencies.py
from fastapi import HTTPException, status
from typing import Optional
import structlog

from app.application.ports.repository_ports import ChunkContentRepositoryPort
from app.application.ports.sparse_search_port import SparseSearchPort
from app.application.ports.sparse_index_storage_port import SparseIndexStoragePort
from app.infrastructure.cache.index_lru_cache import IndexLRUCache
from app.application.use_cases.load_and_search_index_use_case import LoadAndSearchIndexUseCase


log = structlog.get_logger(__name__)

_chunk_content_repo_instance: Optional[ChunkContentRepositoryPort] = None
_sparse_search_engine_instance: Optional[SparseSearchPort] = None 
_gcs_index_storage_instance: Optional[SparseIndexStoragePort] = None
_index_cache_instance: Optional[IndexLRUCache] = None
_load_and_search_use_case_instance: Optional[LoadAndSearchIndexUseCase] = None
_service_ready_flag: bool = False

def set_global_dependencies(
    chunk_repo: Optional[ChunkContentRepositoryPort],
    search_engine: Optional[SparseSearchPort],
    gcs_storage: Optional[SparseIndexStoragePort],
    index_cache: Optional[IndexLRUCache],
    use_case: Optional[LoadAndSearchIndexUseCase], 
    service_ready: bool
):
    global _chunk_content_repo_instance, _sparse_search_engine_instance
    global _gcs_index_storage_instance, _index_cache_instance
    global _load_and_search_use_case_instance, _service_ready_flag

    _chunk_content_repo_instance = chunk_repo
    _sparse_search_engine_instance = search_engine
    _gcs_index_storage_instance = gcs_storage
    _index_cache_instance = index_cache
    _load_and_search_use_case_instance = use_case
    _service_ready_flag = service_ready
    log.debug("Global dependencies set in sparse-search-service.dependencies", service_ready=_service_ready_flag)


def get_chunk_content_repository() -> ChunkContentRepositoryPort:
    if not _service_ready_flag or not _chunk_content_repo_instance:
        log.critical("Attempted to get ChunkContentRepository before service is ready or instance is None.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chunk content repository is not available at the moment."
        )
    return _chunk_content_repo_instance

def get_sparse_search_engine() -> SparseSearchPort:
    if not _service_ready_flag or not _sparse_search_engine_instance:
        log.critical("Attempted to get SparseSearchEngine before service is ready or instance is None.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sparse search engine is not available at the moment."
        )
    return _sparse_search_engine_instance

def get_gcs_index_storage() -> SparseIndexStoragePort:
    if not _service_ready_flag or not _gcs_index_storage_instance:
        log.critical("Attempted to get GCSIndexStorage before service is ready or instance is None.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GCS index storage is not available at the moment."
        )
    return _gcs_index_storage_instance

def get_index_cache() -> IndexLRUCache:
    if not _service_ready_flag or not _index_cache_instance:
        log.critical("Attempted to get IndexCache before service is ready or instance is None.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Index cache is not available at the moment."
        )
    return _index_cache_instance
    
def get_sparse_search_use_case() -> LoadAndSearchIndexUseCase: 
    if not _service_ready_flag or not _load_and_search_use_case_instance:
        log.critical("Attempted to get LoadAndSearchIndexUseCase before service is ready or instance is None.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sparse search processing service is not ready."
        )
    return _load_and_search_use_case_instance

def get_service_status() -> bool:
    return _service_ready_flag