# query-service/app/infrastructure/vectorstores/milvus_adapter.py
import structlog
import asyncio
from typing import List, Optional, Dict, Any
import json 

from pymilvus import Collection, connections, utility, MilvusException, DataType
from haystack import Document 

from app.core.config import settings
from app.application.ports.vector_store_port import VectorStorePort
from app.domain.models import RetrievedChunk 

try:
    MILVUS_PK_FIELD = "pk_id"
    MILVUS_VECTOR_FIELD = "embedding" 
    MILVUS_CONTENT_FIELD = "content" 
    MILVUS_COMPANY_ID_FIELD = "company_id"
    MILVUS_DOCUMENT_ID_FIELD = "document_id"
    MILVUS_FILENAME_FIELD = "file_name"
    MILVUS_PAGE_FIELD = "page"
    MILVUS_TITLE_FIELD = "title"

    INGEST_SCHEMA_FIELDS = {
        "pk": MILVUS_PK_FIELD,
        "vector": MILVUS_VECTOR_FIELD,
        "content": MILVUS_CONTENT_FIELD,
        "company": MILVUS_COMPANY_ID_FIELD,
        "document": MILVUS_DOCUMENT_ID_FIELD,
        "filename": MILVUS_FILENAME_FIELD,
        "page": MILVUS_PAGE_FIELD,
        "title": MILVUS_TITLE_FIELD,
    }
except ImportError:
     structlog.getLogger(__name__).warning("Could not import ingest schema constants, using settings and fallbacks for field names.")
     INGEST_SCHEMA_FIELDS = {
        "pk": "pk_id",
        "vector": settings.MILVUS_EMBEDDING_FIELD,
        "content": settings.MILVUS_CONTENT_FIELD,
        "company": settings.MILVUS_COMPANY_ID_FIELD,
        "document": settings.MILVUS_DOCUMENT_ID_FIELD,
        "filename": settings.MILVUS_FILENAME_FIELD,
        "page": "page",
        "title": "title",
    }


log = structlog.get_logger(__name__)

class MilvusAdapter(VectorStorePort):
    _collection: Optional[Collection] = None
    _connected = False
    _alias = "query_service_milvus_adapter"
    _pk_field_name: str
    _vector_field_name: str
    _content_field_name: str # REFACTOR_5_2: Added
    _doc_id_field_name: str  # REFACTOR_5_2: Added
    _filename_field_name: str# REFACTOR_5_2: Added

    def __init__(self):
        self._pk_field_name = INGEST_SCHEMA_FIELDS["pk"]
        self._vector_field_name = INGEST_SCHEMA_FIELDS["vector"]
        self._content_field_name = INGEST_SCHEMA_FIELDS["content"] # REFACTOR_5_2
        self._doc_id_field_name = INGEST_SCHEMA_FIELDS["document"] # REFACTOR_5_2
        self._filename_field_name = INGEST_SCHEMA_FIELDS["filename"] # REFACTOR_5_2


    async def _ensure_connection(self):
        if not self._connected or self._alias not in connections.list_connections():
            uri = str(settings.MILVUS_URI)
            connect_log = log.bind(adapter="MilvusAdapter", action="connect", uri=uri, alias=self._alias)
            connect_log.debug("Attempting to connect to Milvus (Zilliz)...")
            try:
                connections.connect(
                    alias=self._alias,
                    uri=uri,
                    token=settings.ZILLIZ_API_KEY.get_secret_value(), 
                    timeout=settings.MILVUS_GRPC_TIMEOUT
                )
                self._connected = True
                connect_log.info("Connected to Milvus (Zilliz) successfully.")
            except MilvusException as e:
                connect_log.error("Failed to connect to Milvus (Zilliz).", error_code=e.code, error_message=e.message)
                self._connected = False
                raise ConnectionError(f"Milvus (Zilliz) connection failed (Code: {e.code}): {e.message}") from e
            except Exception as e:
                connect_log.error("Unexpected error connecting to Milvus (Zilliz).", error=str(e), exc_info=True)
                self._connected = False
                raise ConnectionError(f"Unexpected Milvus (Zilliz) connection error: {e}") from e

    async def _get_collection(self) -> Collection:
        await self._ensure_connection()

        if self._collection is None:
            collection_name = settings.MILVUS_COLLECTION_NAME
            collection_log = log.bind(adapter="MilvusAdapter", action="get_collection", collection=collection_name, alias=self._alias)
            collection_log.info(f"Attempting to access Milvus collection: '{collection_name}'")
            try:
                if not utility.has_collection(collection_name, using=self._alias):
                    collection_log.error("Milvus collection does not exist.", target_collection=collection_name)
                    raise RuntimeError(f"Milvus collection '{collection_name}' not found. Ensure ingest-service has created it.")

                collection = Collection(name=collection_name, using=self._alias)
                collection_log.debug("Loading Milvus collection into memory...")
                collection.load()
                collection_log.info("Milvus collection loaded successfully.")
                self._collection = collection

            except MilvusException as e:
                collection_log.error("Failed to get or load Milvus collection", error_code=e.code, error_message=e.message)
                if "multiple indexes" in e.message.lower(): 
                    collection_log.critical("Potential 'Ambiguous Index' error encountered. Please check Milvus indices for this collection.")
                raise RuntimeError(f"Milvus collection access error (Code: {e.code}): {e.message}") from e
            except Exception as e:
                 collection_log.exception("Unexpected error accessing Milvus collection")
                 raise RuntimeError(f"Unexpected error accessing Milvus collection: {e}") from e

        if not isinstance(self._collection, Collection): 
            log.critical("Milvus collection object is unexpectedly None or invalid type after initialization attempt.")
            raise RuntimeError("Failed to obtain a valid Milvus collection object.")

        return self._collection

    async def search(self, embedding: List[float], company_id: str, top_k: int) -> List[RetrievedChunk]:
        search_log = log.bind(adapter="MilvusAdapter", action="search", company_id=company_id, top_k=top_k)
        try:
            collection = await self._get_collection()

            search_params = settings.MILVUS_SEARCH_PARAMS.copy() 
            filter_expr = f'{INGEST_SCHEMA_FIELDS["company"]} == "{company_id}"'
            search_log.debug("Using filter expression", expr=filter_expr)

            # REFACTOR_5_2: Ensure all necessary fields for RetrievedChunk are fetched
            # including the ones previously only in MILVUS_METADATA_FIELDS
            # self._content_field_name, self._doc_id_field_name, self._filename_field_name
            # should be explicitly requested if they are not already part of INGEST_SCHEMA_FIELDS for general metadata
            output_fields_list = list(
                set(
                    [
                        self._pk_field_name,
                        self._vector_field_name, # Needed for MMR or other post-processing
                        self._content_field_name,
                        INGEST_SCHEMA_FIELDS["company"], # company_id
                        self._doc_id_field_name,   # document_id
                        self._filename_field_name, # file_name
                    ]
                    + settings.MILVUS_METADATA_FIELDS # Adds page, title etc.
                )
            )
            
            search_log.debug("Performing Milvus vector search...",
                             vector_field=self._vector_field_name,
                             output_fields=output_fields_list)

            loop = asyncio.get_running_loop()
            search_results = await loop.run_in_executor(
                None,
                lambda: collection.search(
                    data=[embedding],
                    anns_field=self._vector_field_name,
                    param=search_params,
                    limit=top_k,
                    expr=filter_expr,
                    output_fields=output_fields_list,
                    consistency_level="Strong" 
                )
            )

            search_log.debug(f"Milvus search completed. Hits: {len(search_results[0]) if search_results and search_results[0] else 0}")

            domain_chunks: List[RetrievedChunk] = []
            if search_results and search_results[0]:
                for hit in search_results[0]:
                    entity_data = hit.entity.to_dict() if hasattr(hit, 'entity') and hasattr(hit.entity, 'to_dict') else {}
                    
                    if not entity_data: # Fallback
                        entity_data = {field: hit.get(field) for field in output_fields_list if hit.get(field) is not None}

                    pk_id = str(hit.id) 
                    content = entity_data.get(self._content_field_name, "") # REFACTOR_5_2 Use specific field
                    embedding_vector = entity_data.get(self._vector_field_name) 
                    
                    metadata_dict = {k: v for k, v in entity_data.items() if k not in [self._vector_field_name, self._pk_field_name, self._content_field_name]}
                    
                    doc_id_val = entity_data.get(self._doc_id_field_name) # REFACTOR_5_2 Use specific field
                    comp_id_val = entity_data.get(INGEST_SCHEMA_FIELDS["company"])
                    fname_val = entity_data.get(self._filename_field_name) # REFACTOR_5_2 Use specific field

                    # REFACTOR_5_2: Populate content field directly in RetrievedChunk
                    chunk = RetrievedChunk(
                        id=pk_id,
                        content=content, # Populate content
                        score=hit.score,
                        metadata=metadata_dict,
                        embedding=embedding_vector,
                        document_id=str(doc_id_val) if doc_id_val else None,
                        file_name=str(fname_val) if fname_val else None,
                        company_id=str(comp_id_val) if comp_id_val else None
                    )
                    domain_chunks.append(chunk)

            search_log.info(f"Converted {len(domain_chunks)} Milvus hits to domain objects.")
            return domain_chunks

        except MilvusException as me:
             search_log.error("Milvus search failed", error_code=me.code, error_message=me.message)
             raise ConnectionError(f"Vector DB search error (Code: {me.code}): {me.message}") from me
        except Exception as e:
            search_log.exception("Unexpected error during Milvus search")
            raise ConnectionError(f"Vector DB search service error: {e}") from e

    async def fetch_vectors_by_ids(
        self,
        ids: List[str],
        *,
        collection_name: str | None = None,
    ) -> Dict[str, List[float]]:
        fetch_log = log.bind(adapter="MilvusAdapter", action="fetch_vectors_by_ids", num_ids=len(ids))
        if not ids:
            fetch_log.debug("No IDs provided, returning empty dict.")
            return {}

        try:
            _collection_obj = await self._get_collection()
            ids_json_array_str = json.dumps(ids)
            expr = f'{self._pk_field_name} in {ids_json_array_str}'
            
            fetch_log.debug("Querying Milvus for vectors by PKs", expr=expr, pk_field=self._pk_field_name, vector_field=self._vector_field_name)

            output_fields_to_fetch = [self._pk_field_name, self._vector_field_name]

            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(
                None,
                lambda: _collection_obj.query(
                    expr=expr,
                    output_fields=output_fields_to_fetch,
                    consistency_level="Strong"
                )
            )
            
            fetched_vectors = {str(row[self._pk_field_name]): row[self._vector_field_name] for row in res if self._pk_field_name in row and self._vector_field_name in row}
            fetch_log.info(f"Fetched {len(fetched_vectors)} vectors from Milvus out of {len(ids)} requested.")
            return fetched_vectors
        except MilvusException as me:
            fetch_log.error("Milvus query for vectors by IDs failed", error_code=me.code, error_message=me.message)
            raise ConnectionError(f"Vector DB query error (Code: {me.code}): {me.message}") from me
        except Exception as e:
            fetch_log.exception("Unexpected error during Milvus vector fetch by IDs")
            raise ConnectionError(f"Vector DB query service error: {e}") from e


    async def connect(self):
        await self._ensure_connection()

    async def disconnect(self):
        if self._connected and self._alias in connections.list_connections():
            log.info("Disconnecting from Milvus...", adapter="MilvusAdapter", alias=self._alias)
            try:
                connections.disconnect(self._alias)
                self._connected = False
                self._collection = None
                log.info("Disconnected from Milvus.", adapter="MilvusAdapter")
            except Exception as e:
                log.error("Error during Milvus disconnect", error=str(e), exc_info=True)