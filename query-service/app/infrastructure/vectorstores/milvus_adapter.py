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

                collection = Collection(name=collection_name, using=self._alias)

                # Check if loaded, load if necessary
                # This check can be expensive, consider loading strategy (e.g., always load on startup/first use)
                # For simplicity here, we check and load if needed.
                if not collection.has_index():
                     collection_log.warning("Collection exists but has no index. Retrieval quality may be affected.")
                     # Decide if loading is still useful without an index, or raise error?
                     # Let's try loading anyway for now.

                collection_log.debug("Loading Milvus collection into memory...")
                collection.load() # Load data into memory for search
                collection_log.info("Milvus collection loaded successfully.")
                self._collection = collection

            except MilvusException as e:
                collection_log.error("Failed to get or load Milvus collection", error_code=e.code, error_message=e.message)
                raise RuntimeError(f"Milvus collection access error (Code: {e.code}): {e.message}") from e
            except Exception as e:
                 collection_log.exception("Unexpected error accessing Milvus collection")
                 raise RuntimeError(f"Unexpected error accessing Milvus collection: {e}") from e

        # Double check if collection is valid after potential errors
        if not isinstance(self._collection, Collection):
            log.critical("Milvus collection object is unexpectedly None or invalid after initialization attempt.")
            raise RuntimeError("Failed to obtain a valid Milvus collection object.")

        return self._collection

    async def search(self, embedding: List[float], company_id: str, top_k: int) -> List[RetrievedChunk]:
        """Busca chunks relevantes usando pymilvus y los convierte al modelo de dominio."""
        search_log = log.bind(adapter="MilvusAdapter", action="search", company_id=company_id, top_k=top_k)
        try:
            collection = await self._get_collection()

            search_params = settings.MILVUS_SEARCH_PARAMS
            filter_expr = f'{settings.MILVUS_COMPANY_ID_FIELD} == "{company_id}"'
            search_log.debug("Using filter expression", expr=filter_expr)

            # Define output fields based on config, ensuring content and necessary meta are included
            output_fields = list(set([
                settings.MILVUS_CONTENT_FIELD, # Ensure content is requested
                settings.MILVUS_COMPANY_ID_FIELD,
                settings.MILVUS_DOCUMENT_ID_FIELD,
                settings.MILVUS_FILENAME_FIELD,
            ] + settings.MILVUS_METADATA_FIELDS)) # Add other configured metadata fields

            search_log.debug("Performing Milvus vector search...", vector_field=settings.MILVUS_EMBEDDING_FIELD, output_fields=output_fields)

            # Execute search asynchronously (run_in_executor as Milvus client might be blocking)
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
                    consistency_level="Strong" # Or match ingest consistency
                )
            )

            search_log.debug(f"Milvus search completed. Hits: {len(search_results[0]) if search_results else 0}")

            # Convert Milvus results to Domain RetrievedChunk objects
            domain_chunks: List[RetrievedChunk] = []
            if search_results and search_results[0]:
                for hit in search_results[0]:
                    entity_data = hit.entity.to_dict() # Convert entity to dict
                    content = entity_data.get(settings.MILVUS_CONTENT_FIELD, "")

                    # Prepare metadata, excluding the embedding field itself
                    metadata = {k: v for k, v in entity_data.items() if k != settings.MILVUS_EMBEDDING_FIELD}

                    chunk = RetrievedChunk(
                        id=str(hit.id), # Use Milvus primary key as ID
                        content=content,
                        score=hit.score, # Or hit.distance
                        metadata=metadata,
                        # Populate top-level fields from metadata for convenience
                        document_id=str(metadata.get(settings.MILVUS_DOCUMENT_ID_FIELD)) if metadata.get(settings.MILVUS_DOCUMENT_ID_FIELD) else None,
                        file_name=metadata.get(settings.MILVUS_FILENAME_FIELD),
                        company_id=str(metadata.get(settings.MILVUS_COMPANY_ID_FIELD)) if metadata.get(settings.MILVUS_COMPANY_ID_FIELD) else None
                    )
                    domain_chunks.append(chunk)

            search_log.info(f"Converted {len(domain_chunks)} Milvus hits to domain objects.")
            return domain_chunks

        except MilvusException as me:
             search_log.error("Milvus search failed", error_code=me.code, error_message=me.message)
             # Raise ConnectionError for infrastructure issues
             raise ConnectionError(f"Vector DB search error (Code: {me.code}): {me.message}") from me
        except Exception as e:
            search_log.exception("Unexpected error during Milvus search")
            raise ConnectionError(f"Vector DB search service error: {e}") from e

    # Optional: Add a method for explicit connection/disconnection if needed outside lifespan
    async def connect(self):
        """Explicitly establishes the connection."""
        await self._ensure_connection()

    async def disconnect(self):
        """Disconnects from Milvus."""
        if self._connected and self._alias in connections.list_connections():
            log.info("Disconnecting from Milvus...", adapter="MilvusAdapter", alias=self._alias)
            connections.disconnect(self._alias)
            self._connected = False
            self._collection = None # Reset collection object on disconnect
            log.info("Disconnected from Milvus.", adapter="MilvusAdapter")