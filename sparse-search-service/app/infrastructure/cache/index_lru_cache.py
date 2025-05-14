# sparse-search-service/app/infrastructure/cache/index_lru_cache.py
import uuid
from typing import Tuple, Optional, List, Any
import sys
import structlog
from cachetools import TTLCache

try:
    import bm2s 
except ImportError:
    bm2s = None


log = structlog.get_logger(__name__)

CachedIndexData = Tuple[Any, List[str]] 

class IndexLRUCache:
    def __init__(self, max_items: int, ttl_seconds: int):
        self.cache: TTLCache[str, CachedIndexData] = TTLCache(maxsize=max_items, ttl=ttl_seconds)
        self.max_items = max_items
        self.ttl_seconds = ttl_seconds
        self.log = log.bind(cache_type="IndexLRUCache", max_items=max_items, ttl_seconds=ttl_seconds)
        self.log.info("IndexLRUCache initialized.")

    def get(self, company_id: uuid.UUID) -> Optional[CachedIndexData]:
        cache_log = self.log.bind(company_id=str(company_id), action="cache_get")
        key = str(company_id)
        cached_item = self.cache.get(key)
        if cached_item:
            cache_log.info("Cache hit.")
            return cached_item
        else:
            cache_log.info("Cache miss.")
            return None

    def put(self, company_id: uuid.UUID, bm25_instance: Any, id_map: List[str]) -> None:
        cache_log = self.log.bind(company_id=str(company_id), action="cache_put")
        key = str(company_id)
        
        try:
            self.cache[key] = (bm25_instance, id_map)
            cache_log.info("Item added/updated in cache.", current_cache_size=self.cache.currsize)
        except Exception as e:
            cache_log.error("Failed to put item into cache.", error=str(e), exc_info=True)

    def clear(self) -> None:
        self.log.info("Clearing all items from cache.")
        self.cache.clear()

    def __len__(self) -> int:
        return len(self.cache)