from __future__ import annotations
import os
import json
import uuid
import hashlib
import structlog
from typing import List, Dict, Any, Optional, Tuple

from app.core.config import settings
log = structlog.get_logger(__name__)

try:
    import tiktoken
    tiktoken_enc = tiktoken.get_encoding(settings.TIKTOKEN_ENCODING_NAME)
    log.info(f"Tiktoken encoder loaded for MilvusAdapter: {settings.TIKTOKEN_ENCODING_NAME}")
except ImportError:
    log.warning("tiktoken not installed, token count for MilvusAdapter will be unavailable.")
    tiktoken_enc = None
except Exception as e:
    log.warning(f"Failed to load tiktoken encoder '{settings.TIKTOKEN_ENCODING_NAME}' for MilvusAdapter", error=str(e))
    tiktoken_enc = None

from pymilvus import (
    Collection, CollectionSchema, FieldSchema, DataType, connections,
    utility, MilvusException
)

from app.application.ports.vector_store_port import VectorStorePort
from app.domain.entities import ChunkMetadata # For preparing PG data

# Milvus constants from original ingest_pipeline.py
MILVUS_COLLECTION_NAME = settings.MILVUS_COLLECTION_NAME
MILVUS_EMBEDDING_DIM = settings.EMBEDDING_DIMENSION
MILVUS_PK_FIELD = "pk_id"
MILVUS_VECTOR_FIELD = settings.MILVUS_EMBEDDING_FIELD
MILVUS_CONTENT_FIELD = settings.MILVUS_CONTENT_FIELD
MILVUS_COMPANY_ID_FIELD = "company_id"
MILVUS_DOCUMENT_ID_FIELD = "document_id"
MILVUS_FILENAME_FIELD = "file_name"
MILVUS_PAGE_FIELD = "page"
MILVUS_TITLE_FIELD = "title"
MILVUS_TOKENS_FIELD = "tokens"
MILVUS_CONTENT_HASH_FIELD = "content_hash"


class MilvusAdapter(VectorStorePort):
    _milvus_collection: Optional[Collection] = None # Class variable for shared collection object

    def __init__(self, alias: str = "default_ingest_milvus"):
        self.alias = alias
        self.log = log.bind(milvus_adapter_alias=self.alias, collection_name=MILVUS_COLLECTION_NAME)
        self._ensure_connection()
        self.ensure_collection_and_indexes() # This will set self._milvus_collection

    def _ensure_connection(self):
        if self.alias not in connections.list_connections() or not connections.has_connection(self.alias):
            self.log.info("Connecting to Milvus (Zilliz)...", uri=settings.MILVUS_URI)
            try:
                connections.connect(
                    alias=self.alias,
                    uri=settings.MILVUS_URI,
                    timeout=settings.MILVUS_GRPC_TIMEOUT, # Ensure this is in settings
                    token=settings.ZILLIZ_API_KEY.get_secret_value() if settings.ZILLIZ_API_KEY else None
                )
                self.log.info("Connected to Milvus (Zilliz).")
            except Exception as e:
                self.log.error("Failed to connect to Milvus (Zilliz)", error=str(e), exc_info=True)
                raise ConnectionError(f"Milvus (Zilliz) connection failed: {e}") from e
        else:
             self.log.debug("Milvus connection alias already exists and seems active.", alias=self.alias)


    def _create_milvus_collection(self) -> Collection:
        create_log = self.log.bind(embedding_dim=MILVUS_EMBEDDING_DIM)
        create_log.info("Defining schema for new Milvus collection.")
        fields = [
            FieldSchema(name=MILVUS_PK_FIELD, dtype=DataType.VARCHAR, max_length=255, is_primary=True),
            FieldSchema(name=MILVUS_VECTOR_FIELD, dtype=DataType.FLOAT_VECTOR, dim=MILVUS_EMBEDDING_DIM),
            FieldSchema(name=MILVUS_CONTENT_FIELD, dtype=DataType.VARCHAR, max_length=settings.MILVUS_CONTENT_FIELD_MAX_LENGTH),
            FieldSchema(name=MILVUS_COMPANY_ID_FIELD, dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name=MILVUS_DOCUMENT_ID_FIELD, dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name=MILVUS_FILENAME_FIELD, dtype=DataType.VARCHAR, max_length=512),
            FieldSchema(name=MILVUS_PAGE_FIELD, dtype=DataType.INT64, default_value=-1),
            FieldSchema(name=MILVUS_TITLE_FIELD, dtype=DataType.VARCHAR, max_length=512, default_value=""),
            FieldSchema(name=MILVUS_TOKENS_FIELD, dtype=DataType.INT64, default_value=-1),
            FieldSchema(name=MILVUS_CONTENT_HASH_FIELD, dtype=DataType.VARCHAR, max_length=64)
        ]
        schema = CollectionSchema(fields, description="Atenex Document Chunks", enable_dynamic_field=False)
        create_log.info("Schema defined. Creating collection...")
        collection = Collection(name=MILVUS_COLLECTION_NAME, schema=schema, using=self.alias, consistency_level="Strong")
        create_log.info(f"Collection '{MILVUS_COLLECTION_NAME}' created. Creating indexes...")
        
        index_params = settings.MILVUS_INDEX_PARAMS
        create_log.info("Creating HNSW index for vector field", field_name=MILVUS_VECTOR_FIELD, index_params=index_params)
        collection.create_index(field_name=MILVUS_VECTOR_FIELD, index_params=index_params, index_name=f"{MILVUS_VECTOR_FIELD}_hnsw_idx")
        
        scalar_fields_to_index = [MILVUS_COMPANY_ID_FIELD, MILVUS_DOCUMENT_ID_FIELD, MILVUS_CONTENT_HASH_FIELD]
        for field_name in scalar_fields_to_index:
            create_log.info(f"Creating scalar index for {field_name} field...")
            collection.create_index(field_name=field_name, index_name=f"{field_name}_idx")
        
        create_log.info("All required indexes created successfully.")
        return collection

    def _check_and_create_indexes(self, collection: Collection):
        check_log = self.log
        existing_indexes = collection.indexes
        existing_index_fields = {idx.field_name for idx in existing_indexes}
        required_indexes_map = {
            MILVUS_VECTOR_FIELD: (settings.MILVUS_INDEX_PARAMS, f"{MILVUS_VECTOR_FIELD}_hnsw_idx"),
            MILVUS_COMPANY_ID_FIELD: (None, f"{MILVUS_COMPANY_ID_FIELD}_idx"),
            MILVUS_DOCUMENT_ID_FIELD: (None, f"{MILVUS_DOCUMENT_ID_FIELD}_idx"),
            MILVUS_CONTENT_HASH_FIELD: (None, f"{MILVUS_CONTENT_HASH_FIELD}_idx"),
        }
        for field_name, (index_params, index_name) in required_indexes_map.items():
            if field_name not in existing_index_fields:
                check_log.warning(f"Index missing for field '{field_name}'. Creating '{index_name}'...")
                collection.create_index(field_name=field_name, index_params=index_params, index_name=index_name)
                check_log.info(f"Index '{index_name}' created for field '{field_name}'.")

    def ensure_collection_and_indexes(self) -> None:
        if MilvusAdapter._milvus_collection is None or MilvusAdapter._milvus_collection.name != MILVUS_COLLECTION_NAME:
            self.log.info("Ensuring Milvus collection and indexes...")
            try:
                if not utility.has_collection(MILVUS_COLLECTION_NAME, using=self.alias):
                    self.log.warning(f"Milvus collection '{MILVUS_COLLECTION_NAME}' not found. Creating.")
                    MilvusAdapter._milvus_collection = self._create_milvus_collection()
                else:
                    self.log.debug(f"Using existing Milvus collection '{MILVUS_COLLECTION_NAME}'.")
                    MilvusAdapter._milvus_collection = Collection(name=MILVUS_COLLECTION_NAME, using=self.alias)
                    self._check_and_create_indexes(MilvusAdapter._milvus_collection)
                
                self.log.info("Loading Milvus collection into memory for indexing...", collection_name=MilvusAdapter._milvus_collection.name)
                MilvusAdapter._milvus_collection.load()
                self.log.info("Milvus collection loaded into memory for indexing.")
            except MilvusException as e:
                self.log.error("Failed during Milvus collection access/load", error=str(e), exc_info=True)
                MilvusAdapter._milvus_collection = None
                raise RuntimeError(f"Milvus collection access error: {e}") from e
        if not isinstance(MilvusAdapter._milvus_collection, Collection):
            self.log.critical("Milvus collection object is unexpectedly None or invalid after ensure_collection_and_indexes.")
            raise RuntimeError("Failed to obtain a valid Milvus collection object.")


    def index_chunks(
        self,
        chunks_with_embeddings: List[Dict[str, Any]],
        filename: str,
        company_id: str,
        document_id: str,
        delete_existing: bool
    ) -> Tuple[int, List[str], List[Dict[str, Any]]]:
        
        index_log = self.log.bind(company_id=company_id, document_id=document_id, filename=filename)
        index_log.info("Starting Milvus indexing and PG data preparation", num_input_chunks=len(chunks_with_embeddings))

        if not chunks_with_embeddings:
            index_log.warning("No chunks_with_embeddings to process for Milvus/PG indexing.")
            return 0, [], []

        milvus_pk_ids: List[str] = []
        milvus_embeddings: List[List[float]] = []
        milvus_contents: List[str] = []
        milvus_company_ids: List[str] = []
        milvus_document_ids: List[str] = []
        milvus_filenames: List[str] = []
        milvus_pages: List[int] = []
        milvus_titles: List[str] = []
        milvus_tokens_counts: List[int] = []
        milvus_content_hashes: List[str] = []
        
        chunks_for_pg: List[Dict[str, Any]] = []

        for i, item in enumerate(chunks_with_embeddings):
            chunk_text = item.get('text', '')
            chunk_embedding = item.get('embedding')
            source_metadata = item.get('source_metadata', {}) # From docproc

            if not chunk_text or not chunk_text.strip() or not chunk_embedding:
                index_log.warning("Skipping chunk due to empty text or missing embedding.", original_chunk_index=i, has_text=bool(chunk_text), has_embedding=bool(chunk_embedding))
                continue
            
            tokens = len(tiktoken_enc.encode(chunk_text)) if tiktoken_enc else -1
            content_hash = hashlib.sha256(chunk_text.encode('utf-8', errors='ignore')).hexdigest()
            page_number = source_metadata.get('page_number', source_metadata.get('page', -1)) # Accommodate different metadata keys
            
            milvus_pk_id = f"{document_id}_{i}" # Use original index from docproc as part of PK
            title_for_chunk = f"{filename[:30]}... (Page {page_number if page_number != -1 else 'N/A'}, Chunk {i + 1})"

            milvus_pk_ids.append(milvus_pk_id)
            milvus_embeddings.append(chunk_embedding)
            milvus_contents.append(chunk_text[:settings.MILVUS_CONTENT_FIELD_MAX_LENGTH]) # Truncate
            milvus_company_ids.append(company_id)
            milvus_document_ids.append(document_id)
            milvus_filenames.append(filename)
            milvus_pages.append(page_number if page_number is not None else -1)
            milvus_titles.append(title_for_chunk[:512]) # Truncate
            milvus_tokens_counts.append(tokens)
            milvus_content_hashes.append(content_hash)

            # Prepare data for PostgreSQL, linking with Milvus PK
            pg_metadata_obj = ChunkMetadata(page=page_number, title=title_for_chunk, tokens=tokens, content_hash=content_hash)
            chunks_for_pg.append({
                "id": uuid.uuid4(), # Generate UUID for PG table pk
                "document_id": uuid.UUID(document_id),
                "company_id": uuid.UUID(company_id),
                "chunk_index": i, # Use original index from docproc
                "content": chunk_text, # Store full content in PG
                "metadata": pg_metadata_obj.model_dump(mode='json'),
                "embedding_id": milvus_pk_id, # This is the Milvus PK
                "vector_status": "pending" # Will be updated to 'created' if Milvus insert is successful
            })

        if not milvus_pk_ids:
            index_log.warning("No valid chunks remained after preparation for Milvus.")
            return 0, [], []

        if delete_existing:
            index_log.info("Attempting to delete existing Milvus chunks before insertion...")
            try:
                deleted_count = self.delete_document_chunks(company_id, document_id)
                index_log.info(f"Deleted {deleted_count} existing Milvus chunks.")
            except Exception as del_err:
                index_log.error("Failed to delete existing Milvus chunks, proceeding with insert.", error=str(del_err))

        data_to_insert_milvus = [
            milvus_pk_ids, milvus_embeddings, milvus_contents, milvus_company_ids,
            milvus_document_ids, milvus_filenames, milvus_pages, milvus_titles,
            milvus_tokens_counts, milvus_content_hashes
        ]
        index_log.debug(f"Inserting {len(milvus_pk_ids)} chunks into Milvus collection...")
        
        try:
            if MilvusAdapter._milvus_collection is None:
                 self.ensure_collection_and_indexes() # Should re-init _milvus_collection
                 if MilvusAdapter._milvus_collection is None: # Check again
                    raise RuntimeError("Milvus collection could not be initialized.")

            mutation_result = MilvusAdapter._milvus_collection.insert(data_to_insert_milvus)
            inserted_milvus_count = mutation_result.insert_count
            returned_milvus_pks = [str(pk) for pk in mutation_result.primary_keys]

            if inserted_milvus_count != len(milvus_pk_ids):
                index_log.error("Milvus insert count mismatch!", expected=len(milvus_pk_ids), inserted=inserted_milvus_count, errors=mutation_result.err_indices)
                # Partial failure, update status for successfully inserted chunks in PG list
                # This is complex, for now, we assume all or nothing for simplicity or log the error
                # Mark all as 'error' if mismatch is significant or pks are not reliable
                for chunk_pg_data in chunks_for_pg: chunk_pg_data["vector_status"] = "error"

            else:
                index_log.info(f"Successfully inserted {inserted_milvus_count} chunks into Milvus.")
                # Update vector_status for PG data
                for chunk_pg_data in chunks_for_pg:
                    # Ensure embedding_id (Milvus PK) matches one of the returned PKs.
                    # This check is crucial if order is not guaranteed or if there were partial inserts.
                    # For now, assuming order is preserved and all inserts were successful if counts match.
                    if chunk_pg_data["embedding_id"] in returned_milvus_pks:
                         chunk_pg_data["vector_status"] = "created"
                    else: # Should not happen if counts match and PKs are reliable
                         index_log.warning("Milvus PK mismatch for PG data. Chunk might not be in Milvus.", embedding_id=chunk_pg_data["embedding_id"])
                         chunk_pg_data["vector_status"] = "error"


            index_log.debug("Flushing Milvus collection...")
            MilvusAdapter._milvus_collection.flush()
            index_log.info("Milvus collection flushed.")

            return inserted_milvus_count, returned_milvus_pks, chunks_for_pg

        except MilvusException as e:
            index_log.error("Failed to insert data into Milvus", error=str(e), exc_info=True)
            for chunk_pg_data in chunks_for_pg: chunk_pg_data["vector_status"] = "error"
            # Do not re-raise immediately, allow PG to be updated with error status potentially
            return 0, [], chunks_for_pg # Indicate 0 Milvus inserts, but return PG data for error state
        except Exception as e:
            index_log.exception("Unexpected error during Milvus insertion")
            for chunk_pg_data in chunks_for_pg: chunk_pg_data["vector_status"] = "error"
            return 0, [], chunks_for_pg


    def delete_document_chunks(self, company_id: str, document_id: str) -> int:
        del_log = self.log.bind(company_id=company_id, document_id=document_id)
        expr = f'{MILVUS_COMPANY_ID_FIELD} == "{company_id}" and {MILVUS_DOCUMENT_ID_FIELD} == "{document_id}"'
        
        try:
            if MilvusAdapter._milvus_collection is None: self.ensure_collection_and_indexes()
            del_log.info("Attempting to delete chunks from Milvus.", filter_expr=expr)
            # Query for PKs first to get an accurate count of what *should* be deleted
            query_res = MilvusAdapter._milvus_collection.query(expr=expr, output_fields=[MILVUS_PK_FIELD], consistency_level="Strong")
            pks_to_delete = [item[MILVUS_PK_FIELD] for item in query_res if MILVUS_PK_FIELD in item]

            if not pks_to_delete:
                del_log.info("No matching primary keys found in Milvus for deletion.")
                return 0
            
            delete_expr = f'{MILVUS_PK_FIELD} in {json.dumps(pks_to_delete)}'
            delete_result = MilvusAdapter._milvus_collection.delete(expr=delete_expr)
            MilvusAdapter._milvus_collection.flush()
            del_log.info("Milvus delete operation executed and flushed.", delete_count=delete_result.delete_count, expected_to_delete=len(pks_to_delete))
            return delete_result.delete_count
        except MilvusException as e:
            del_log.error("Milvus delete error", error=str(e), exc_info=True)
            return 0 # Indicate failure or 0 deleted
        except Exception as e:
            del_log.exception("Unexpected error during Milvus chunk deletion")
            return 0

    def _get_chunk_count_internal(self, company_id: str, document_id: str) -> int:
        count_log = self.log.bind(company_id=company_id, document_id=document_id)
        try:
            if MilvusAdapter._milvus_collection is None: self.ensure_collection_and_indexes()
            expr = f'{MILVUS_COMPANY_ID_FIELD} == "{company_id}" and {MILVUS_DOCUMENT_ID_FIELD} == "{document_id}"'
            count_log.debug("Querying Milvus chunk count", filter_expr=expr)
            
            # Use query with output_fields=[MILVUS_PK_FIELD] for more robust counting
            # The result of collection.num_entities might not be immediately consistent without a flush
            # or if consistency level isn't strong enough for the use case.
            query_res = MilvusAdapter._milvus_collection.query(expr=expr, output_fields=[MILVUS_PK_FIELD], consistency_level="Strong")
            count = len(query_res)
            count_log.info("Milvus chunk count successful", count=count)
            return count
        except MilvusException as e:
            count_log.error("Milvus query error during count", error=str(e), exc_info=True)
            return -1 # Indicate error
        except Exception as e:
            count_log.exception("Unexpected error during Milvus count", error=str(e))
            return -1

    def count_document_chunks(self, company_id: str, document_id: str) -> int:
        # This is the synchronous version
        return self._get_chunk_count_internal(company_id, document_id)

    async def count_document_chunks_async(self, company_id: str, document_id: str) -> int:
        # For async, run the sync method in an executor
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_chunk_count_internal, company_id, document_id)