# ingest-service/app/services/ingest_pipeline.py
from __future__ import annotations

import os
import uuid
import hashlib
import structlog
from typing import List, Dict, Any, Optional, Tuple, Union

# Tokenizer for metadata
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

# Direct library imports
from pymilvus import (
    Collection, CollectionSchema, FieldSchema, DataType, connections,
    utility, MilvusException, AnnSearchRequest, RRFRanker
)
# Type hinting only
from sentence_transformers import SentenceTransformer

# Local application imports
from app.core.config import settings
# Import NEW helpers and models
from .extractors.pdf_extractor import extract_text_from_pdf, PdfExtractionError
from .extractors.docx_extractor import extract_text_from_docx, DocxExtractionError
from .extractors.txt_extractor import extract_text_from_txt, TxtExtractionError
from .extractors.md_extractor import extract_text_from_md, MdExtractionError
from .extractors.html_extractor import extract_text_from_html, HtmlExtractionError
from .text_splitter import split_text
from .embedder import embed_chunks
from app.models.domain import DocumentChunkMetadata, DocumentChunkData # Import Pydantic model

log = structlog.get_logger(__name__)

# --- Mapeo de Content-Type a Funciones Extractoras ---
# Now includes functions returning page info (PDF) or just text (others)
EXTRACTORS = {
    "application/pdf": extract_text_from_pdf, # Returns List[Tuple[int, str]]
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": extract_text_from_docx, # Returns str
    "application/msword": extract_text_from_docx, # Returns str
    "text/plain": extract_text_from_txt, # Returns str
    "text/markdown": extract_text_from_md, # Returns str
    "text/html": extract_text_from_html, # Returns str
}
EXTRACTION_ERRORS = (
    PdfExtractionError, DocxExtractionError, TxtExtractionError,
    MdExtractionError, HtmlExtractionError, ValueError
)

# --- Constantes Milvus (Incluye nuevos campos) ---
MILVUS_COLLECTION_NAME = settings.MILVUS_COLLECTION_NAME
MILVUS_EMBEDDING_DIM = settings.EMBEDDING_DIMENSION
# Field names (Constants)
MILVUS_PK_FIELD = "pk_id" # Assuming auto_id=True, Milvus uses this internally but we get it back
MILVUS_VECTOR_FIELD = settings.MILVUS_EMBEDDING_FIELD # "embedding"
MILVUS_CONTENT_FIELD = settings.MILVUS_CONTENT_FIELD # "content"
MILVUS_COMPANY_ID_FIELD = "company_id"
MILVUS_DOCUMENT_ID_FIELD = "document_id"
MILVUS_FILENAME_FIELD = "file_name"
# NEW Metadata Fields
MILVUS_PAGE_FIELD = "page"
MILVUS_TITLE_FIELD = "title"
MILVUS_TOKENS_FIELD = "tokens"
MILVUS_CONTENT_HASH_FIELD = "content_hash"

_milvus_collection: Optional[Collection] = None
_milvus_connected = False

def _ensure_milvus_connection_and_collection() -> Collection:
    """Establishes connection and returns the Milvus collection object."""
    global _milvus_collection, _milvus_connected
    alias = "pipeline_worker" # Specific alias for worker connections
    connect_log = log.bind(milvus_alias=alias)

    # Check if connection exists and is valid
    connection_exists = alias in connections.list_connections()
    is_connected = False
    if connection_exists:
        try:
            # Simple check: ping or list collections might be too slow/heavy.
            # Check connection status attribute if available (pymilvus versions vary)
            # Or just assume connected and let operations fail if not.
            # Here we rely on the subsequent utility.has_collection check.
             if utility.has_collection(MILVUS_COLLECTION_NAME, using=alias): # Example check
                 is_connected = True
             else:
                 # Collection missing implies connection might be stale or needs recreation
                 is_connected = False
                 connect_log.warning("Connection alias exists but collection check failed, may reconnect.")

        except Exception as conn_check_err:
            connect_log.warning("Error checking existing Milvus connection status, will attempt reconnect.", error=str(conn_check_err))
            is_connected = False
            try: connections.disconnect(alias) # Attempt to clean up bad connection
            except: pass

    if not is_connected:
        uri = settings.MILVUS_URI
        connect_log.info("Connecting to Milvus for pipeline worker...", uri=uri, timeout=settings.MILVUS_GRPC_TIMEOUT)
        try:
            connections.connect(alias=alias, uri=uri, timeout=settings.MILVUS_GRPC_TIMEOUT)
            _milvus_connected = True
            connect_log.info("Connected to Milvus for pipeline worker.")
        except MilvusException as e:
            connect_log.error("Failed to connect to Milvus for pipeline worker.", error=str(e))
            _milvus_connected = False
            raise ConnectionError(f"Milvus connection failed: {e}") from e
        except Exception as e:
            connect_log.error("Unexpected error connecting to Milvus for pipeline worker.", error=str(e))
            _milvus_connected = False
            raise ConnectionError(f"Unexpected Milvus connection error: {e}") from e

    # Now handle collection getting/creation
    if _milvus_collection is None:
        try:
            if not utility.has_collection(MILVUS_COLLECTION_NAME, using=alias):
                connect_log.warning(f"Milvus collection '{MILVUS_COLLECTION_NAME}' not found. Attempting to create.")
                collection = _create_milvus_collection(alias)
            else:
                collection = Collection(name=MILVUS_COLLECTION_NAME, using=alias)
                connect_log.debug(f"Using existing Milvus collection '{MILVUS_COLLECTION_NAME}'.")
                # Optional index check (can slow down acquisition)
                # _check_and_create_indexes(collection)

            connect_log.info("Loading Milvus collection into memory...", collection_name=collection.name)
            collection.load()
            connect_log.info("Milvus collection loaded into memory.")
            _milvus_collection = collection

        except MilvusException as coll_err:
             connect_log.error("Failed during Milvus collection access/load", error=str(coll_err), exc_info=True)
             raise RuntimeError(f"Milvus collection access error: {coll_err}") from coll_err
        except Exception as e:
             connect_log.error("Unexpected error during Milvus collection access", error=str(e), exc_info=True)
             raise RuntimeError(f"Unexpected Milvus collection error: {e}") from e

    if not isinstance(_milvus_collection, Collection):
        connect_log.critical("Milvus collection object is unexpectedly None or invalid type after initialization attempt.")
        raise RuntimeError("Failed to obtain a valid Milvus collection object.")

    return _milvus_collection


def _create_milvus_collection(alias: str) -> Collection:
    """Creates the Milvus collection with the defined schema."""
    create_log = log.bind(collection_name=MILVUS_COLLECTION_NAME, embedding_dim=MILVUS_EMBEDDING_DIM)
    create_log.info("Defining schema for new Milvus collection.")

    # Define fields including new metadata
    fields = [
        FieldSchema(name=MILVUS_PK_FIELD, dtype=DataType.VARCHAR, max_length=255, is_primary=True), # Use VARCHAR for custom PKs
        FieldSchema(name=MILVUS_VECTOR_FIELD, dtype=DataType.FLOAT_VECTOR, dim=MILVUS_EMBEDDING_DIM),
        FieldSchema(name=MILVUS_CONTENT_FIELD, dtype=DataType.VARCHAR, max_length=settings.MILVUS_CONTENT_FIELD_MAX_LENGTH),
        FieldSchema(name=MILVUS_COMPANY_ID_FIELD, dtype=DataType.VARCHAR, max_length=64), # Increased length just in case
        FieldSchema(name=MILVUS_DOCUMENT_ID_FIELD, dtype=DataType.VARCHAR, max_length=64), # Increased length
        FieldSchema(name=MILVUS_FILENAME_FIELD, dtype=DataType.VARCHAR, max_length=512),
        # --- NEW Metadata Fields ---
        FieldSchema(name=MILVUS_PAGE_FIELD, dtype=DataType.INT64, default_value=-1), # Default -1 if no page
        FieldSchema(name=MILVUS_TITLE_FIELD, dtype=DataType.VARCHAR, max_length=512, default_value=""), # Default empty title
        FieldSchema(name=MILVUS_TOKENS_FIELD, dtype=DataType.INT64, default_value=-1), # Default -1 if no token count
        FieldSchema(name=MILVUS_CONTENT_HASH_FIELD, dtype=DataType.VARCHAR, max_length=64) # SHA256 hex digest length
    ]
    schema = CollectionSchema(
        fields,
        description="Atenex Document Chunks with Enhanced Metadata",
        enable_dynamic_field=False # Explicitly disable dynamic fields
    )
    create_log.info("Schema defined. Creating collection...")
    try:
        collection = Collection(
            name=MILVUS_COLLECTION_NAME,
            schema=schema,
            using=alias,
            consistency_level="Strong" # Ensure reads see writes for consistency
        )
        create_log.info(f"Collection '{MILVUS_COLLECTION_NAME}' created. Creating indexes...")

        # --- Vector Index ---
        index_params = settings.MILVUS_INDEX_PARAMS
        create_log.info("Creating HNSW index for vector field", field_name=MILVUS_VECTOR_FIELD, index_params=index_params)
        collection.create_index(field_name=MILVUS_VECTOR_FIELD, index_params=index_params, index_name=f"{MILVUS_VECTOR_FIELD}_hnsw_idx")

        # --- Scalar Indexes (Essential for filtering) ---
        create_log.info("Creating scalar index for company_id field...")
        collection.create_index(field_name=MILVUS_COMPANY_ID_FIELD, index_name=f"{MILVUS_COMPANY_ID_FIELD}_idx")
        create_log.info("Creating scalar index for document_id field...")
        collection.create_index(field_name=MILVUS_DOCUMENT_ID_FIELD, index_name=f"{MILVUS_DOCUMENT_ID_FIELD}_idx")
        # Optional: Index other filterable fields if needed later
        # collection.create_index(field_name=MILVUS_PAGE_FIELD, index_name=f"{MILVUS_PAGE_FIELD}_idx")

        create_log.info("All required indexes created successfully.")
        return collection
    except MilvusException as e:
        create_log.error("Failed to create Milvus collection or index", error=str(e), exc_info=True)
        raise RuntimeError(f"Milvus collection/index creation failed: {e}") from e

def _check_and_create_indexes(collection: Collection):
    """Helper to check and create indexes if missing (can be slow)."""
    check_log = log.bind(collection_name=collection.name)
    try:
        existing_indexes = collection.indexes
        existing_index_names = {idx.index_name for idx in existing_indexes}
        required_indexes = {
            MILVUS_VECTOR_FIELD: (settings.MILVUS_INDEX_PARAMS, f"{MILVUS_VECTOR_FIELD}_hnsw_idx"),
            MILVUS_COMPANY_ID_FIELD: (None, f"{MILVUS_COMPANY_ID_FIELD}_idx"),
            MILVUS_DOCUMENT_ID_FIELD: (None, f"{MILVUS_DOCUMENT_ID_FIELD}_idx"),
        }
        for field_name, (index_params, index_name) in required_indexes.items():
            if index_name not in existing_index_names:
                check_log.warning(f"Index '{index_name}' missing for field '{field_name}'. Creating...")
                collection.create_index(field_name=field_name, index_params=index_params, index_name=index_name)
                check_log.info(f"Index '{index_name}' created.")

    except MilvusException as e:
        check_log.error("Failed during index check/creation on existing collection", error=str(e))
        # Decide whether to raise or just log the error

def delete_milvus_chunks(company_id: str, document_id: str) -> int:
    """Deletes chunks for a specific document from Milvus."""
    del_log = log.bind(company_id=company_id, document_id=document_id)
    try:
        collection = _ensure_milvus_connection_and_collection()
        # Use PK field for query, assuming it's efficiently searchable
        # Adjust query if using different PK or strategy
        expr = f'{MILVUS_COMPANY_ID_FIELD} == "{company_id}" and {MILVUS_DOCUMENT_ID_FIELD} == "{document_id}"'

        # It's often more robust to query for PKs first, then delete by PKs,
        # especially if delete-by-expression has limitations or performance issues.
        # However, delete-by-expression is simpler if it works reliably.
        del_log.info("Attempting to delete chunks from Milvus using expression.", filter_expr=expr)
        delete_result = collection.delete(expr=expr)
        actual_deleted_count = delete_result.delete_count
        del_log.info("Milvus delete operation executed.", deleted_count=actual_deleted_count)

        return actual_deleted_count

    except MilvusException as e:
        del_log.error("Milvus delete error", error=str(e), exc_info=True)
        # Return 0 or raise a specific exception?
        return 0 # Indicate failure/no deletion
    except Exception as e:
        del_log.exception("Unexpected error during Milvus chunk deletion")
        raise RuntimeError(f"Unexpected Milvus deletion error: {e}") from e


def ingest_document_pipeline(
    file_bytes: bytes,
    filename: str,
    company_id: str,
    document_id: str,
    content_type: str,
    embedding_model: SentenceTransformer,
    delete_existing: bool = True
) -> Tuple[int, List[str], List[DocumentChunkData]]:
    """
    Processes a document: extracts, chunks, generates metadata, embeds,
    and inserts into Milvus.

    Returns:
        Tuple containing:
        - int: Number of chunks successfully inserted into Milvus.
        - List[str]: List of Milvus Primary Keys (embedding_ids) for inserted chunks.
        - List[DocumentChunkData]: List of processed chunk data objects for PG insertion.
    """
    ingest_log = log.bind(
        company_id=company_id,
        document_id=document_id,
        filename=filename,
        content_type=content_type
    )
    ingest_log.info("Starting ingestion pipeline v0.3.0")

    # --- 1. Extract Text (Handling page numbers for PDF) ---
    extractor = EXTRACTORS.get(content_type)
    if not extractor:
        ingest_log.error("Unsupported content type for extraction.")
        raise ValueError(f"Unsupported content type: {content_type}")

    ingest_log.debug("Extracting text content...")
    extracted_content: Union[str, List[Tuple[int, str]]]
    try:
        extracted_content = extractor(file_bytes, filename=filename)
        if not extracted_content:
            ingest_log.warning("No text content extracted from the document. Skipping.")
            return 0, [], []
        if isinstance(extracted_content, str):
            ingest_log.info(f"Text extracted successfully (no pages), length: {len(extracted_content)} chars.")
        else: # List[Tuple[int, str]] from PDF
            ingest_log.info(f"Text extracted successfully from {len(extracted_content)} pages.")
    except EXTRACTION_ERRORS as ve:
        ingest_log.error("Text extraction failed.", error=str(ve), exc_info=True)
        raise ValueError(f"Extraction failed for {filename}: {ve}") from ve
    except Exception as e:
        ingest_log.exception("Unexpected error during text extraction")
        raise RuntimeError(f"Unexpected extraction error: {e}") from e

    # --- 2. Chunk Text & Generate Metadata ---
    ingest_log.debug("Chunking text and generating metadata...")
    processed_chunks: List[DocumentChunkData] = []
    chunk_index_counter = 0

    def _process_text_block(text_block: str, page_num: Optional[int]):
        nonlocal chunk_index_counter
        try:
            text_chunks = split_text(text_block)
            for chunk_text in text_chunks:
                if not chunk_text or chunk_text.isspace():
                    continue

                # Generate metadata
                tokens = len(tiktoken_enc.encode(chunk_text)) if tiktoken_enc else -1
                content_hash = hashlib.sha256(chunk_text.encode('utf-8', errors='ignore')).hexdigest()
                # Simple title heuristic
                title = f"{filename[:20]}... Page {page_num or '?'} Chunk {chunk_index_counter+1}"

                metadata_obj = DocumentChunkMetadata(
                    page=page_num,
                    title=title[:500], # Truncate title if needed
                    tokens=tokens,
                    content_hash=content_hash
                )

                chunk_data = DocumentChunkData(
                    document_id=uuid.UUID(document_id),
                    company_id=uuid.UUID(company_id),
                    chunk_index=chunk_index_counter,
                    content=chunk_text,
                    metadata=metadata_obj,
                    # embedding_id will be set after Milvus insert
                )
                processed_chunks.append(chunk_data)
                chunk_index_counter += 1
        except Exception as chunking_err:
            ingest_log.error("Error processing text block during chunking/metadata", page=page_num, error=str(chunking_err), exc_info=True)
            # Optionally skip this block or raise the error

    if isinstance(extracted_content, str):
         _process_text_block(extracted_content, page_num=None) # Process single text block
    else: # List[Tuple[int, str]] from PDF
         for page_number, page_text in extracted_content:
             _process_text_block(page_text, page_num=page_number)

    if not processed_chunks:
        ingest_log.warning("Text content resulted in zero processable chunks. Skipping.")
        return 0, [], []
    ingest_log.info(f"Text processed into {len(processed_chunks)} chunks with metadata.")

    # --- 3. Embed Chunks ---
    chunk_contents = [chunk.content for chunk in processed_chunks]
    ingest_log.debug(f"Generating embeddings for {len(chunk_contents)} chunks...")
    try:
        embeddings = embed_chunks(chunk_contents)
        ingest_log.info(f"Embeddings generated successfully for {len(embeddings)} chunks.")
        if len(embeddings) != len(processed_chunks):
            ingest_log.error("CRITICAL: Mismatch between number of chunks and generated embeddings!",
                             num_chunks=len(processed_chunks), num_embeddings=len(embeddings))
            raise RuntimeError("Embedding count mismatch error.")
        if embeddings and len(embeddings[0]) != MILVUS_EMBEDDING_DIM:
             ingest_log.error("CRITICAL: Generated embedding dimension mismatch!",
                              actual_dim=len(embeddings[0]), expected_dim=MILVUS_EMBEDDING_DIM)
             raise RuntimeError("Embedding dimension mismatch error.")
    except Exception as e:
        ingest_log.error("Failed to generate embeddings", error=str(e), exc_info=True)
        raise RuntimeError(f"Embedding generation failed: {e}") from e

    # --- 4. Prepare Data for Milvus (using custom PKs) ---
    max_content_len = settings.MILVUS_CONTENT_FIELD_MAX_LENGTH
    def truncate_utf8_bytes(s, max_bytes):
        b = s.encode('utf-8', errors='ignore')
        if len(b) <= max_bytes: return s
        return b[:max_bytes].decode('utf-8', errors='ignore')

    # Generate custom PKs (e.g., combining doc_id and chunk_index)
    # Milvus VARCHAR PKs are recommended over INT64 auto_id for easier correlation
    milvus_pks = [f"{document_id}_{i}" for i in range(len(processed_chunks))]

    # Prepare data lists matching the schema order
    data_to_insert = [
        milvus_pks, # PK field
        embeddings, # Vector field
        [truncate_utf8_bytes(c.content, max_content_len) for c in processed_chunks], # Content
        [company_id] * len(processed_chunks), # Company ID
        [document_id] * len(processed_chunks), # Document ID
        [filename] * len(processed_chunks), # Filename
        # --- New Metadata ---
        [c.metadata.page if c.metadata.page is not None else -1 for c in processed_chunks], # Page (use -1 default)
        [c.metadata.title if c.metadata.title else "" for c in processed_chunks], # Title (use "" default)
        [c.metadata.tokens if c.metadata.tokens is not None else -1 for c in processed_chunks], # Tokens (use -1 default)
        [c.metadata.content_hash if c.metadata.content_hash else "" for c in processed_chunks] # Hash
    ]
    ingest_log.debug(f"Prepared {len(processed_chunks)} entities for Milvus insertion with custom PKs.")

    # --- 5. Delete Existing Chunks (Optional but Recommended) ---
    if delete_existing:
        ingest_log.info("Attempting to delete existing chunks before insertion...")
        try:
            deleted_count = delete_milvus_chunks(company_id, document_id)
            ingest_log.info(f"Deleted {deleted_count} existing chunks.")
        except Exception as del_err:
            # Log error but proceed with insertion attempt
            ingest_log.error("Failed to delete existing chunks, proceeding with insert anyway.", error=str(del_err))

    # --- 6. Insert into Milvus ---
    ingest_log.debug(f"Inserting {len(processed_chunks)} chunks into Milvus collection '{MILVUS_COLLECTION_NAME}'...")
    try:
        collection = _ensure_milvus_connection_and_collection()
        mutation_result = collection.insert(data_to_insert)

        # Validate insertion count
        inserted_count = mutation_result.insert_count
        if inserted_count != len(processed_chunks):
             # This case might indicate partial success or other issues.
             ingest_log.error("Milvus insert count mismatch!", expected=len(processed_chunks), inserted=inserted_count, errors=mutation_result.err_indices)
             # Decide how to handle partial insert: maybe retry, maybe fail, maybe continue with inserted count?
             # For now, log error and proceed with the count reported by Milvus.
        else:
             ingest_log.info(f"Successfully inserted {inserted_count} chunks into Milvus.")

        # Verify primary keys match what we sent (sanity check)
        if mutation_result.primary_keys != milvus_pks[:inserted_count]:
             ingest_log.warning("Milvus returned primary keys do not exactly match the generated PKs used for insertion.",
                                returned_pks=mutation_result.primary_keys, sent_pks=milvus_pks[:inserted_count])
             # It's generally safe to trust mutation_result.primary_keys if insert_count is correct

        # Flush Milvus to ensure data visibility (important for consistency level "Strong")
        ingest_log.debug("Flushing Milvus collection...")
        collection.flush()
        ingest_log.info("Milvus collection flushed.")

        # Populate embedding_id in our Pydantic models using the returned PKs
        returned_pks = mutation_result.primary_keys
        if len(returned_pks) == inserted_count:
            for i in range(inserted_count):
                 processed_chunks[i].embedding_id = str(returned_pks[i]) # Ensure it's string
                 processed_chunks[i].vector_status = ChunkVectorStatus.CREATED
        else:
             ingest_log.error("Cannot assign embedding_ids due to PK count mismatch from Milvus result.")
             # Mark all as pending or error? Mark document as error later?
             # For now, they remain None/pending. Task will likely fail at PG insert.

        # Return the count, the list of *successfully inserted* PKs, and the processed data for PG
        return inserted_count, returned_pks[:inserted_count], processed_chunks[:inserted_count]

    except MilvusException as e:
        ingest_log.error("Failed to insert data into Milvus", error=str(e), exc_info=True)
        # Propagate exception to be handled by the Celery task
        raise RuntimeError(f"Milvus insertion failed: {e}") from e
    except Exception as e:
        ingest_log.exception("Unexpected error during Milvus insertion")
        # Propagate exception
        raise RuntimeError(f"Unexpected Milvus insertion error: {e}") from e