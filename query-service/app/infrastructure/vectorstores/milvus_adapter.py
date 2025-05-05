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

log = structlog.get_logger(__name__)

class MilvusAdapter(VectorStorePort):
    """Adaptador concreto para interactuar con Milvus usando pymilvus."""

    _collection: Optional[Collection] = None
    _connected = False
    _alias = "query_service_milvus_adapter" # Unique alias for this adapter's connection

    def __init__(self):
        # Connection is established lazily on first use or explicitly via connect()
        pass

    async def _ensure_connection(self):
        """Ensures connection to Milvus is established."""
        if not self._connected or self._alias not in connections.list_connections():
            uri = str(settings.MILVUS_URI)
            connect_log = log.bind(adapter="MilvusAdapter", action="connect", uri=uri, alias=self._alias)
            connect_log.debug("Attempting to connect to Milvus...")
            try:
                # Check if already connected under this alias
                existing_conn = connections.get_connection_addr(self._alias)
                if existing_conn:
                    connect_log.debug("Already connected to Milvus under this alias.")
                    self._connected = True
                    return

                connections.connect(alias=self._alias, uri=uri, timeout=settings.MILVUS_GRPC_TIMEOUT)
                self._connected = True
                connect_log.info("Connected to Milvus successfully.")
            except MilvusException as e:
                connect_log.error("Failed to connect to Milvus.", error_code=e.code, error_message=e.message)
                self._connected = False
                raise ConnectionError(f"Milvus connection failed (Code: {e.code}): {e.message}") from e
            except Exception as e:
                connect_log.error("Unexpected error connecting to Milvus.", error=str(e), exc_info=True)
                self._connected = False
                raise ConnectionError(f"Unexpected Milvus connection error: {e}") from e

    async def _get_collection(self) -> Collection:
        """Gets the Milvus collection object, ensuring connection and loading."""
        await self._ensure_connection() # Ensure connection is active

        if self._collection is None:
            collection_name = settings.MILVUS_COLLECTION_NAME
            collection_log = log.bind(adapter="MilvusAdapter", action="get_collection", collection=collection_name, alias=self._alias)
            collection_log.debug("Attempting to get Milvus collection.")
            try:
                if not utility.has_collection(collection_name, using=self._alias):
                    collection_log.error("Milvus collection does not exist.")
                    raise RuntimeError(f"Milvus collection '{collection_name}' not found. It must be created by the ingest service.")

                # Instanciación directa. Errores de índice ambiguo pueden surgir aquí o en load().
                collection = Collection(name=collection_name, using=self._alias)

                # --- CORRECTION: Removed explicit has_index() check ---
                # if not collection.has_index():
                #      collection_log.warning("Collection exists but has no index. Retrieval quality may be affected.")
                # -----------------------------------------------------

                collection_log.debug("Loading Milvus collection into memory...")
                collection.load() # Load default partition or all partitions. Error puede surgir aquí.
                collection_log.info("Milvus collection loaded successfully.")
                self._collection = collection

            except MilvusException as e:
                collection_log.error("Failed to get or load Milvus collection", error_code=e.code, error_message=e.message)
                # Add specific handling for ambiguous index error message for clarity
                if "multiple indexes" in e.message.lower():
                    collection_log.critical("Potential 'Ambiguous Index' error encountered. Please check Milvus indices for this collection.")
                raise RuntimeError(f"Milvus collection access error (Code: {e.code}): {e.message}") from e
            except Exception as e:
                 collection_log.exception("Unexpected error accessing Milvus collection")
                 raise RuntimeError(f"Unexpected error accessing Milvus collection: {e}") from e

        if not isinstance(self._collection, Collection):
            log.critical("Milvus collection object is unexpectedly None or invalid after initialization attempt.")
            raise RuntimeError("Failed to obtain a valid Milvus collection object.")

        return self._collection

    async def search(self, embedding: List[float], company_id: str, top_k: int) -> List[RetrievedChunk]:
        """Busca chunks relevantes usando pymilvus y los convierte al modelo de dominio."""
        search_log = log.bind(adapter="MilvusAdapter", action="search", company_id=company_id, top_k=top_k)
        try:
            collection = await self._get_collection()

            search_params = getattr(settings, "MILVUS_SEARCH_PARAMS", {"metric_type": "L2", "params": {"nprobe": 10}})

            filter_expr = f'{settings.MILVUS_COMPANY_ID_FIELD} == "{company_id}"'
            search_log.debug("Using filter expression", expr=filter_expr)

            output_fields = list(set([
                settings.MILVUS_CONTENT_FIELD,
                settings.MILVUS_EMBEDDING_FIELD,
                settings.MILVUS_COMPANY_ID_FIELD,
                settings.MILVUS_DOCUMENT_ID_FIELD,
                settings.MILVUS_FILENAME_FIELD,
            ] + settings.MILVUS_METADATA_FIELDS))

            search_log.debug("Performing Milvus vector search...", vector_field=settings.MILVUS_EMBEDDING_FIELD, output_fields=output_fields)

            loop = asyncio.get_running_loop()
            search_results = await loop.run_in_executor(
                None,
                lambda: collection.search(
                    data=[embedding],
                    anns_field=settings.MILVUS_EMBEDDING_FIELD,
                    param=search_params,
                    limit=top_k,
                    expr=filter_expr,
                    output_fields=output_fields,
                    consistency_level="Strong"
                )
            )

            search_log.debug(f"Milvus search completed. Hits: {len(search_results[0]) if search_results else 0}")

            domain_chunks: List[RetrievedChunk] = []
            if search_results and search_results[0]:
                for hit in search_results[0]:
                    # Use hit.entity.to_dict() for easier access if available and preferred
                    # entity_data = hit.entity.to_dict()
                    # Using direct access for robustness
                    entity_data = hit.entity.to_dict() if hasattr(hit, 'entity') and hasattr(hit.entity, 'to_dict') else {}
                    content = entity_data.get(settings.MILVUS_CONTENT_FIELD, "")
                    embedding_vector = entity_data.get(settings.MILVUS_EMBEDDING_FIELD)

                    # Prepare metadata from entity_data, excluding sensitive or large fields if needed
                    metadata = {k: v for k, v in entity_data.items() if k != settings.MILVUS_EMBEDDING_FIELD}

                    # Ensure required fields for domain model exist in metadata
                    doc_id = metadata.get(settings.MILVUS_DOCUMENT_ID_FIELD)
                    comp_id = metadata.get(settings.MILVUS_COMPANY_ID_FIELD)

                    chunk = RetrievedChunk(
                        id=str(hit.id),
                        content=content,
                        score=hit.score,
                        metadata=metadata,
                        embedding=embedding_vector,
                        document_id=str(doc_id) if doc_id else None,
                        file_name=metadata.get(settings.MILVUS_FILENAME_FIELD),
                        company_id=str(comp_id) if comp_id else None
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
        await self._ensure_connection()

    async def disconnect(self):
        if self._connected and self._alias in connections.list_connections():
            log.info("Disconnecting from Milvus...", adapter="MilvusAdapter", alias=self._alias)
            connections.disconnect(self._alias)
            self._connected = False
            self._collection = None
            log.info("Disconnected from Milvus.", adapter="MilvusAdapter")