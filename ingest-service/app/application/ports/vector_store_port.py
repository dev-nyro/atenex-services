from abc import ABC, abstractmethod
from typing import List, Dict, Tuple, Any

class VectorStorePort(ABC):
    @abstractmethod
    def index_chunks(
        self,
        chunks_with_embeddings: List[Dict[str, Any]], # Combined chunk data and its embedding
        filename: str, # For metadata in Milvus
        company_id: str, 
        document_id: str, 
        delete_existing: bool
    ) -> Tuple[int, List[str], List[Dict[str, Any]]]: # inserted_count, milvus_pks, chunks_for_pg (with embedding_id)
        pass

    @abstractmethod
    def delete_document_chunks(self, company_id: str, document_id: str) -> int:
        pass

    @abstractmethod
    def count_document_chunks(self, company_id: str, document_id: str) -> int: # Sync for API and worker
        pass
    
    @abstractmethod
    async def count_document_chunks_async(self, company_id: str, document_id: str) -> int: # Async version for API
        pass


    @abstractmethod
    def ensure_collection_and_indexes(self) -> None: # Typically sync, called at init or worker startup
        pass