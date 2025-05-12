# query-service/app/infrastructure/vectorstores/milvus_adapter.py
import structlog
import asyncio
from typing import List, Optional, Dict, Any

from pymilvus import Collection, connections, utility, MilvusException, DataType
from haystack import Document # Keep Haystack Document for conversion ease initially

# LLM_REFACTOR_STEP_2: Update import paths and add Port/Domain import
from app.core.config import settings
from app.application.ports.vector_store_port import VectorStorePort
from app.domain.models import RetrievedChunk # Import domain model

# --- Import field name constants from ingest_pipeline for clarity (Read-Only) ---
# These constants represent the actual field names used in the Milvus collection schema
# defined by the ingest-service.
# This improves maintainability if field names change in the ingest service.
# LLM_FLAG: MANUALLY_VERIFIED_INGEST_SCHEMA_FIELDS_CONSISTENCY
try:
    # Attempt to import from a shared location if ingest-service fields are exposed
    # For now, we assume these constants are defined based on ingest-service's current schema
    # In a mono-repo or shared library, this would be more direct.
    # Hardcoding them here as a fallback based on the ingest-service README and common practice.
    MILVUS_PK_FIELD = "pk_id"
    MILVUS_VECTOR_FIELD = "embedding" # Corresponds to settings.MILVUS_EMBEDDING_FIELD from ingest
    MILVUS_CONTENT_FIELD = "content" # Corresponds to settings.MILVUS_CONTENT_FIELD from ingest
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
    """Adaptador concreto para interactuar con Milvus usando pymilvus."""

    _collection: Optional[Collection] = None
    _connected = False
    _alias = "query_service_milvus_adapter"

    def __init__(self):
        pass

    async def _ensure_connection(self):
        """Ensures connection to Milvus is established."""
        if not self._connected or self._alias not in connections.list_connections():
            uri = str(settings.MILVUS_URI)
            connect_log = log.bind(adapter="MilvusAdapter", action="connect", uri=uri, alias=self._alias)
            connect_log.debug("Attempting to connect to Milvus (Zilliz)...")
            try:
                connections.connect(
                    alias=self._alias,
                    uri=uri,
                    token=settings.ZILLIZ_API_KEY.get_secret_value(), # MODIFIED: Added token
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
        """Gets the Milvus collection object, ensuring connection and loading."""
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
                if "multiple indexes" in e.message.lower(): # LLM_FLAG: SENSITIVE_ERROR_HANDLING
                    collection_log.critical("Potential 'Ambiguous Index' error encountered. Please check Milvus indices for this collection.")
                raise RuntimeError(f"Milvus collection access error (Code: {e.code}): {e.message}") from e
            except Exception as e:
                 collection_log.exception("Unexpected error accessing Milvus collection")
                 raise RuntimeError(f"Unexpected error accessing Milvus collection: {e}") from e

        if not isinstance(self._collection, Collection): # LLM_FLAG: ROBUSTNESS_CHECK
            log.critical("Milvus collection object is unexpectedly None or invalid type after initialization attempt.")
            raise RuntimeError("Failed to obtain a valid Milvus collection object.")

        return self._collection

    async def search(self, embedding: List[float], company_id: str, top_k: int) -> List[RetrievedChunk]:
        """Busca chunks relevantes usando pymilvus y los convierte al modelo de dominio."""
        search_log = log.bind(adapter="MilvusAdapter", action="search", company_id=company_id, top_k=top_k)
        try:
            collection = await self._get_collection()

            search_params = settings.MILVUS_SEARCH_PARAMS.copy() # Use a copy to avoid modifying global settings
            filter_expr = f'{INGEST_SCHEMA_FIELDS["company"]} == "{company_id}"'
            search_log.debug("Using filter expression", expr=filter_expr)

            required_output_fields_set = {
                INGEST_SCHEMA_FIELDS["pk"],
                INGEST_SCHEMA_FIELDS["vector"],
                INGEST_SCHEMA_FIELDS["content"],
                INGEST_SCHEMA_FIELDS["company"],
                INGEST_SCHEMA_FIELDS["document"],
                INGEST_SCHEMA_FIELDS["filename"],
            }
            required_output_fields_set.update(settings.MILVUS_METADATA_FIELDS)
            output_fields_list = list(required_output_fields_set)

            search_log.debug("Performing Milvus vector search...",
                             vector_field=INGEST_SCHEMA_FIELDS["vector"],
                             output_fields=output_fields_list)

            loop = asyncio.get_running_loop()
            search_results = await loop.run_in_executor(
                None,
                lambda: collection.search(
                    data=[embedding],
                    anns_field=INGEST_SCHEMA_FIELDS["vector"],
                    param=search_params,
                    limit=top_k,
                    expr=filter_expr,
                    output_fields=output_fields_list,
                    consistency_level="Strong" # Recommended for RAG consistency
                )
            )

            search_log.debug(f"Milvus search completed. Hits: {len(search_results[0]) if search_results and search_results[0] else 0}")

            domain_chunks: List[RetrievedChunk] = []
            if search_results and search_results[0]:
                for hit in search_results[0]:
                    entity_data = hit.entity.to_dict() if hasattr(hit, 'entity') and hasattr(hit.entity, 'to_dict') else {}

                    pk_id = str(hit.id)
                    content = entity_data.get(INGEST_SCHEMA_FIELDS["content"], "")
                    embedding_vector = entity_data.get(INGEST_SCHEMA_FIELDS["vector"])

                    metadata_dict = {k: v for k, v in entity_data.items() if k != INGEST_SCHEMA_FIELDS["vector"]}
                    
                    doc_id_val = metadata_dict.get(INGEST_SCHEMA_FIELDS["document"])
                    comp_id_val = metadata_dict.get(INGEST_SCHEMA_FIELDS["company"])
                    fname_val = metadata_dict.get(INGEST_SCHEMA_FIELDS["filename"])

                    chunk = RetrievedChunk(
                        id=pk_id,
                        content=content,
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

    async def connect(self):
        """Explicitly ensures connection (can be called during startup if needed)."""
        await self._ensure_connection()

    async def disconnect(self):
        """Disconnects from Milvus."""
        if self._connected and self._alias in connections.list_connections():
            log.info("Disconnecting from Milvus...", adapter="MilvusAdapter", alias=self._alias)
            try:
                connections.disconnect(self._alias)
                self._connected = False
                self._collection = None
                log.info("Disconnected from Milvus.", adapter="MilvusAdapter")
            except Exception as e:
                log.error("Error during Milvus disconnect", error=str(e), exc_info=True)