# ingest-service/app/services/ingest_pipeline.py
from __future__ import annotations

import os
import json
import uuid
import hashlib
import structlog
from typing import List, Dict, Any, Optional, Tuple, Union

from app.core.config import settings
log = structlog.get_logger(__name__)

try:
    import tiktoken
    tiktoken_enc = tiktoken.get_encoding(settings.TIKTOKEN_ENCODING_NAME)
    log.info(f"Tiktoken encoder loaded: {settings.TIKTOKEN_ENCODING_NAME}")
except ImportError:
    log.warning("tiktoken not installed, token count metadata will be unavailable.")
    tiktoken_enc = None
except Exception as e:
    log.warning(f"Failed to load tiktoken encoder '{settings.TIKTOKEN_ENCODING_NAME}', token count metadata will be unavailable.", error=str(e))
    tiktoken_enc = None

from pymilvus import (
    Collection, CollectionSchema, FieldSchema, DataType, connections,
    utility, MilvusException
)

from app.models.domain import DocumentChunkMetadata, DocumentChunkData, ChunkVectorStatus

# --- Constantes Milvus ---
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

_milvus_collection_pipeline: Optional[Collection] = None

def _ensure_milvus_connection_and_collection_for_pipeline(alias: str = "pipeline_worker_indexing") -> Collection:
    global _milvus_collection_pipeline
    connect_log = log.bind(milvus_alias=alias, component="MilvusPipelineOps")

    connection_exists = alias in connections.list_connections()
    
    # Re-evaluate connection and collection state more robustly
    if connection_exists:
        try:
            # Test existing connection
            utility.get_connection_addr(alias) # This can raise if connection is stale
            connect_log.debug("Milvus connection alias exists and seems active.", alias=alias)
            if _milvus_collection_pipeline and _milvus_collection_pipeline.name == MILVUS_COLLECTION_NAME:
                 # Basic check, might not guarantee collection is loaded in this specific alias
                pass # Use existing _milvus_collection_pipeline if set
            else: # Collection not set or different, reset flag to force re-initialization
                connection_exists = False # Force re-init logic for collection
        except Exception as conn_check_err:
            connect_log.warning("Error checking existing Milvus connection status, will attempt reconnect.", error=str(conn_check_err))
            try: connections.disconnect(alias)
            except: pass
            connection_exists = False # Force re-init

    if not connection_exists:
        uri = settings.MILVUS_URI
        connect_log.info("Connecting to Milvus (Zilliz) for pipeline worker indexing...", uri=uri)
        try:
            connections.connect(
                alias=alias,
                uri=uri,
                timeout=settings.MILVUS_GRPC_TIMEOUT,
                token=settings.ZILLIZ_API_KEY.get_secret_value() if settings.ZILLIZ_API_KEY else None
            )
            connect_log.info("Connected to Milvus (Zilliz) for pipeline worker indexing.")
        except MilvusException as e:
            connect_log.error("Failed to connect to Milvus (Zilliz) for pipeline worker indexing.", error=str(e))
            raise ConnectionError(f"Milvus (Zilliz) connection failed: {e}") from e
        except Exception as e:
            connect_log.error("Unexpected error connecting to Milvus (Zilliz) for pipeline worker indexing.", error=str(e))
            raise ConnectionError(f"Unexpected Milvus (Zilliz) connection error: {e}") from e

    # Check and initialize collection if needed
    if _milvus_collection_pipeline is None or _milvus_collection_pipeline.name != MILVUS_COLLECTION_NAME:
        try:
            if not utility.has_collection(MILVUS_COLLECTION_NAME, using=alias):
                connect_log.warning(f"Milvus collection '{MILVUS_COLLECTION_NAME}' not found. Attempting to create.")
                collection_obj = _create_milvus_collection_for_pipeline(alias)
            else:
                collection_obj = Collection(name=MILVUS_COLLECTION_NAME, using=alias)
                connect_log.debug(f"Using existing Milvus collection '{MILVUS_COLLECTION_NAME}'.")
                _check_and_create_indexes_for_pipeline(collection_obj)

            connect_log.info("Loading Milvus collection into memory for indexing...", collection_name=collection_obj.name)
            collection_obj.load()
            connect_log.info("Milvus collection loaded into memory for indexing.")
            _milvus_collection_pipeline = collection_obj
        except MilvusException as coll_err:
             connect_log.error("Failed during Milvus collection access/load for indexing", error=str(coll_err), exc_info=True)
             raise RuntimeError(f"Milvus collection access error for indexing: {coll_err}") from coll_err
        except Exception as e:
             connect_log.error("Unexpected error during Milvus collection access for indexing", error=str(e), exc_info=True)
             raise RuntimeError(f"Unexpected Milvus collection error for indexing: {e}") from e
    
    if not isinstance(_milvus_collection_pipeline, Collection):
        connect_log.critical("Milvus collection object is unexpectedly None or invalid type after initialization for indexing.")
        raise RuntimeError("Failed to obtain a valid Milvus collection object for indexing.")
    return _milvus_collection_pipeline


def _create_milvus_collection_for_pipeline(alias: str) -> Collection:
    create_log = log.bind(collection_name=MILVUS_COLLECTION_NAME, embedding_dim=MILVUS_EMBEDDING_DIM, component="MilvusPipelineOps")
    create_log.info("Defining schema for new Milvus collection (pipeline).")
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
    schema = CollectionSchema(
        fields, description="Atenex Document Chunks with Enhanced Metadata", enable_dynamic_field=False
    )
    create_log.info("Schema defined. Creating collection (pipeline)...")
    try:
        collection = Collection(
            name=MILVUS_COLLECTION_NAME, schema=schema, using=alias, consistency_level="Strong"
        )
        create_log.info(f"Collection '{MILVUS_COLLECTION_NAME}' created (pipeline). Creating indexes...")
        index_params = settings.MILVUS_INDEX_PARAMS
        create_log.info("Creating HNSW index for vector field (pipeline)", field_name=MILVUS_VECTOR_FIELD, index_params=index_params)
        collection.create_index(field_name=MILVUS_VECTOR_FIELD, index_params=index_params, index_name=f"{MILVUS_VECTOR_FIELD}_hnsw_idx")
        
        scalar_fields_to_index = [
            MILVUS_COMPANY_ID_FIELD, MILVUS_DOCUMENT_ID_FIELD, MILVUS_CONTENT_HASH_FIELD
        ]
        for field_name in scalar_fields_to_index:
            create_log.info(f"Creating scalar index for {field_name} field (pipeline)...")
            collection.create_index(field_name=field_name, index_name=f"{field_name}_idx")

        create_log.info("All required indexes created successfully (pipeline).")
        return collection
    except MilvusException as e:
        create_log.error("Failed to create Milvus collection or index (pipeline)", error=str(e), exc_info=True)
        raise RuntimeError(f"Milvus collection/index creation failed (pipeline): {e}") from e


def _check_and_create_indexes_for_pipeline(collection: Collection):
    check_log = log.bind(collection_name=collection.name, component="MilvusPipelineOps")
    try:
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
                check_log.warning(f"Index missing for field '{field_name}' (pipeline). Creating '{index_name}'...")
                collection.create_index(field_name=field_name, index_params=index_params, index_name=index_name)
                check_log.info(f"Index '{index_name}' created for field '{field_name}' (pipeline).")
            else:
                check_log.debug(f"Index already exists for field '{field_name}' (pipeline).")
    except MilvusException as e:
        check_log.error("Failed during index check/creation on existing collection (pipeline)", error=str(e))
        # Do not re-raise, allow processing to continue if some indexes exist.


def delete_milvus_chunks(company_id: str, document_id: str) -> int:
    del_log = log.bind(company_id=company_id, document_id=document_id, component="MilvusPipelineOps")
    expr = f'{MILVUS_COMPANY_ID_FIELD} == "{company_id}" and {MILVUS_DOCUMENT_ID_FIELD} == "{document_id}"'
    pks_to_delete: List[str] = []
    deleted_count = 0
    try:
        collection = _ensure_milvus_connection_and_collection_for_pipeline()
        del_log.info("Querying Milvus for PKs to delete (pipeline)...", filter_expr=expr)
        query_res = collection.query(expr=expr, output_fields=[MILVUS_PK_FIELD])
        pks_to_delete = [item[MILVUS_PK_FIELD] for item in query_res if MILVUS_PK_FIELD in item]
        if not pks_to_delete:
            del_log.info("No matching primary keys found in Milvus for deletion (pipeline).")
            return 0
        
        del_log.info(f"Found {len(pks_to_delete)} primary keys to delete (pipeline).")
        delete_expr = f'{MILVUS_PK_FIELD} in {json.dumps(pks_to_delete)}'
        del_log.info("Attempting to delete chunks from Milvus using PK list expression (pipeline).", filter_expr=delete_expr)
        delete_result = collection.delete(expr=delete_expr)
        deleted_count = delete_result.delete_count
        del_log.info("Milvus delete operation by PK list executed (pipeline).", deleted_count=deleted_count)
        
        if deleted_count != len(pks_to_delete):
             del_log.warning("Milvus delete count mismatch (pipeline).", expected=len(pks_to_delete), reported=deleted_count)
        return deleted_count
    except MilvusException as e:
        del_log.error("Milvus delete error (query or delete phase) (pipeline)", error=str(e), exc_info=True)
        return 0
    except Exception as e:
        del_log.exception("Unexpected error during Milvus chunk deletion (pipeline)")
        return 0


def index_chunks_in_milvus_and_prepare_for_pg(
    processed_chunks_from_docproc: List[Dict[str, Any]],
    embeddings: List[List[float]],
    filename: str,
    company_id_str: str,
    document_id_str: str,
    delete_existing_milvus_chunks: bool = True
) -> Tuple[int, List[str], List[Dict[str, Any]]]:
    """
    Takes chunks from docproc-service and embeddings, indexes them in Milvus,
    and prepares data for PostgreSQL insertion.
    """
    index_log = log.bind(
        company_id=company_id_str, document_id=document_id_str, filename=filename,
        num_input_chunks=len(processed_chunks_from_docproc), num_embeddings=len(embeddings),
        component="MilvusPGPipeline"
    )
    index_log.info("Starting Milvus indexing and PG data preparation")

    if len(processed_chunks_from_docproc) != len(embeddings):
        index_log.error("Mismatch between number of processed chunks and embeddings.",
                        chunks_count=len(processed_chunks_from_docproc), embeddings_count=len(embeddings))
        raise ValueError("Number of chunks and embeddings must match.")

    if not processed_chunks_from_docproc:
        index_log.warning("No chunks to process for Milvus/PG indexing.")
        return 0, [], []

    chunks_for_milvus_pg: List[DocumentChunkData] = []
    milvus_data_content: List[str] = []
    milvus_data_company_ids: List[str] = []
    milvus_data_document_ids: List[str] = []
    milvus_data_filenames: List[str] = []
    milvus_data_pages: List[int] = []
    milvus_data_titles: List[str] = []
    milvus_data_tokens: List[int] = []
    milvus_data_content_hashes: List[str] = []
    milvus_pk_ids: List[str] = []
    
    # Filter out empty texts from embeddings list to match content
    valid_embeddings: List[List[float]] = []
    valid_chunk_indices_from_docproc: List[int] = []


    for i, chunk_from_docproc in enumerate(processed_chunks_from_docproc):
        chunk_text = chunk_from_docproc.get('text', '')
        if not chunk_text or chunk_text.isspace():
            index_log.warning("Skipping empty chunk from docproc during Milvus/PG prep.", original_chunk_index=i)
            continue # Skip this chunk if it has no text
        
        # If chunk is valid, add its embedding and note its original index
        valid_embeddings.append(embeddings[i])
        valid_chunk_indices_from_docproc.append(i)

        source_metadata = chunk_from_docproc.get('source_metadata', {})
        
        tokens = len(tiktoken_enc.encode(chunk_text)) if tiktoken_enc else -1
        content_hash = hashlib.sha256(chunk_text.encode('utf-8', errors='ignore')).hexdigest()
        page_number_from_source = source_metadata.get('page_number')
        
        current_sequential_chunk_index = len(chunks_for_milvus_pg)
        title_for_chunk = f"{filename[:30]}... (Page {page_number_from_source or 'N/A'}, Chunk {current_sequential_chunk_index + 1})"

        pg_metadata = DocumentChunkMetadata(
            page=page_number_from_source,
            title=title_for_chunk[:500],
            tokens=tokens,
            content_hash=content_hash
        )

        chunk_data_obj = DocumentChunkData(
            document_id=uuid.UUID(document_id_str),
            company_id=uuid.UUID(company_id_str),
            chunk_index=current_sequential_chunk_index,
            content=chunk_text,
            metadata=pg_metadata
        )
        chunks_for_milvus_pg.append(chunk_data_obj)

        milvus_pk_id = f"{document_id_str}_{chunk_data_obj.chunk_index}"
        milvus_pk_ids.append(milvus_pk_id)
        
        milvus_data_content.append(chunk_text)
        milvus_data_company_ids.append(company_id_str)
        milvus_data_document_ids.append(document_id_str)
        milvus_data_filenames.append(filename)
        milvus_data_pages.append(page_number_from_source if page_number_from_source is not None else -1)
        milvus_data_titles.append(title_for_chunk[:512])
        milvus_data_tokens.append(tokens)
        milvus_data_content_hashes.append(content_hash)

    if not chunks_for_milvus_pg:
        index_log.warning("No valid (non-empty) chunks remained after initial processing for Milvus/PG.")
        return 0, [], []
    
    if len(valid_embeddings) != len(milvus_data_content):
        index_log.error("Critical mismatch: Valid embeddings count differs from valid content count for Milvus.",
                        num_valid_embeddings=len(valid_embeddings), num_valid_content=len(milvus_data_content))
        raise ValueError("Internal error: Embedding count does not match valid chunk content count.")

    max_content_len = settings.MILVUS_CONTENT_FIELD_MAX_LENGTH
    def truncate_utf8_bytes(s, max_bytes):
        b = s.encode('utf-8', errors='ignore')
        if len(b) <= max_bytes: return s
        return b[:max_bytes].decode('utf-8', errors='ignore')

    data_to_insert_milvus = [
        milvus_pk_ids,
        valid_embeddings, # Use the filtered list of embeddings
        [truncate_utf8_bytes(text, max_content_len) for text in milvus_data_content],
        milvus_data_company_ids,
        milvus_data_document_ids,
        milvus_data_filenames,
        milvus_data_pages,
        milvus_data_titles,
        milvus_data_tokens,
        milvus_data_content_hashes
    ]
    index_log.debug(f"Prepared {len(milvus_pk_ids)} valid entities for Milvus insertion.")

    if delete_existing_milvus_chunks:
        index_log.info("Attempting to delete existing Milvus chunks before insertion...")
        try:
            deleted_count = delete_milvus_chunks(company_id_str, document_id_str)
            index_log.info(f"Deleted {deleted_count} existing Milvus chunks.")
        except Exception as del_err:
            index_log.error("Failed to delete existing Milvus chunks, proceeding with insert anyway.", error=str(del_err))

    index_log.debug(f"Inserting {len(milvus_pk_ids)} chunks into Milvus collection '{MILVUS_COLLECTION_NAME}'...")
    inserted_milvus_count = 0
    returned_milvus_pks: List[str] = []
    
    try:
        collection = _ensure_milvus_connection_and_collection_for_pipeline()
        mutation_result = collection.insert(data_to_insert_milvus)
        inserted_milvus_count = mutation_result.insert_count

        if inserted_milvus_count != len(milvus_pk_ids):
             index_log.error("Milvus insert count mismatch!", expected=len(milvus_pk_ids), inserted=inserted_milvus_count, errors=mutation_result.err_indices)
        else:
             index_log.info(f"Successfully inserted {inserted_milvus_count} chunks into Milvus.")

        returned_milvus_pks = [str(pk) for pk in mutation_result.primary_keys]
        chunks_for_pg_prepared: List[Dict[str, Any]] = []

        if len(returned_milvus_pks) != inserted_milvus_count:
             index_log.error("Milvus returned PK count mismatch!", returned_count=len(returned_milvus_pks), inserted_count=inserted_milvus_count)
             # Do not proceed with PG prep if PKs are unreliable
        else:
            index_log.info("Milvus returned PKs match inserted count.")
            for i in range(inserted_milvus_count):
                chunks_for_milvus_pg[i].embedding_id = returned_milvus_pks[i]
                chunks_for_milvus_pg[i].vector_status = ChunkVectorStatus.CREATED
                chunks_for_pg_prepared.append(chunks_for_milvus_pg[i].model_dump(mode='json'))

        index_log.debug("Flushing Milvus collection...")
        collection.flush()
        index_log.info("Milvus collection flushed.")

        return inserted_milvus_count, returned_milvus_pks, chunks_for_pg_prepared

    except MilvusException as e:
        index_log.error("Failed to insert data into Milvus", error=str(e), exc_info=True)
        raise RuntimeError(f"Milvus insertion failed: {e}") from e
    except Exception as e:
        index_log.exception("Unexpected error during Milvus insertion")
        raise RuntimeError(f"Unexpected Milvus insertion error: {e}") from e