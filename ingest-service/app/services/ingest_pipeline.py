# ingest-service/app/services/ingest_pipeline.py
from __future__ import annotations

import os
import uuid
import structlog
from typing import List, Dict, Any, Optional

# Direct library imports
from pymilvus import (
    Collection, CollectionSchema, FieldSchema, DataType, connections,
    utility, MilvusException
)
# LLM_FLAG: Type hinting only
from sentence_transformers import SentenceTransformer

# Local application imports
from app.core.config import settings
# LLM_FLAG: Import NEW helpers
from .extractors.pdf_extractor import extract_text_from_pdf, PdfExtractionError
from .extractors.docx_extractor import extract_text_from_docx, DocxExtractionError
from .extractors.txt_extractor import extract_text_from_txt, TxtExtractionError
from .extractors.md_extractor import extract_text_from_md, MdExtractionError
from .extractors.html_extractor import extract_text_from_html, HtmlExtractionError
from .text_splitter import split_text
from .embedder import embed_chunks

log = structlog.get_logger(__name__)

# --- Mapeo de Content-Type a Funciones Extractoras (ACTUALIZADO) ---
EXTRACTORS = {
    "application/pdf": extract_text_from_pdf,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": extract_text_from_docx,
    "application/msword": extract_text_from_docx,
    "text/plain": extract_text_from_txt,
    "text/markdown": extract_text_from_md,
    "text/html": extract_text_from_html,
}
EXTRACTION_ERRORS = (
    PdfExtractionError, DocxExtractionError, TxtExtractionError,
    MdExtractionError, HtmlExtractionError, ValueError # Include generic ValueError too
)

# --- Constantes Milvus (Dimensión ACTUALIZADA) ---
MILVUS_COLLECTION_NAME = settings.MILVUS_COLLECTION_NAME
MILVUS_EMBEDDING_DIM = settings.EMBEDDING_DIMENSION # From config (e.g., 384)
MILVUS_PK_FIELD = "pk_id"
MILVUS_VECTOR_FIELD = "embedding"
MILVUS_CONTENT_FIELD = "content"
MILVUS_COMPANY_ID_FIELD = "company_id"
MILVUS_DOCUMENT_ID_FIELD = "document_id"
MILVUS_FILENAME_FIELD = "file_name"

_milvus_collection: Optional[Collection] = None
_milvus_connected = False

def _ensure_milvus_connection_and_collection() -> Collection:
    global _milvus_collection, _milvus_connected
    alias = "pipeline_worker_zilliz"
    if not _milvus_connected or alias not in connections.list_connections():
        uri = settings.ZILLIZ_URI
        token = settings.ZILLIZ_API_KEY.get_secret_value()
        log.info("Connecting to Zilliz Cloud for pipeline worker...", uri=uri, alias=alias)
        try:
            connections.connect(alias=alias, uri=uri, token=token, secure=True)
            _milvus_connected = True
            log.info("Connected to Zilliz Cloud for pipeline worker.")
        except MilvusException as e:
            log.error("Failed to connect to Zilliz Cloud for pipeline worker.", error=str(e))
            _milvus_connected = False
            raise ConnectionError(f"Zilliz connection failed: {e}") from e
        except Exception as e:
            log.error("Unexpected error connecting to Zilliz Cloud for pipeline worker.", error=str(e))
            _milvus_connected = False
            raise ConnectionError(f"Unexpected Zilliz connection error: {e}") from e

    if _milvus_collection is None:
        if not utility.has_collection(MILVUS_COLLECTION_NAME, using=alias):
            log.warning(f"Zilliz collection '{MILVUS_COLLECTION_NAME}' not found. Attempting to create.")
            try:
                collection = _create_milvus_collection(alias)
            except Exception as create_e:
                log.critical("Failed to create Zilliz collection", error=str(create_e), exc_info=True)
                raise RuntimeError(f"Zilliz collection creation failed: {create_e}") from create_e
        else:
            collection = Collection(name=MILVUS_COLLECTION_NAME, using=alias)
            log.debug(f"Using existing Zilliz collection '{MILVUS_COLLECTION_NAME}'.")
            try:
                if not any(idx.field_name == MILVUS_VECTOR_FIELD for idx in collection.indexes):
                    log.warning(f"Vector index missing on field '{MILVUS_VECTOR_FIELD}'. Creating...")
                    collection.create_index(field_name=MILVUS_VECTOR_FIELD, index_params=settings.MILVUS_INDEX_PARAMS)
                    log.info(f"Vector index created for '{MILVUS_VECTOR_FIELD}'.")
            except Exception as idx_e:
                log.error("Failed to check or create index on existing collection", error=str(idx_e))

        try:
            log.info("Loading Zilliz collection into memory...", collection_name=collection.name)
            collection.load()
            log.info("Zilliz collection loaded into memory.")
        except MilvusException as load_e:
            log.error("Failed to load Zilliz collection into memory", error=str(load_e))
            raise RuntimeError(f"Zilliz collection load failed: {load_e}") from load_e

        _milvus_collection = collection

    if not isinstance(_milvus_collection, Collection):
        log.critical("Zilliz collection object is unexpectedly None or invalid type after initialization attempt.")
        raise RuntimeError("Failed to obtain a valid Zilliz collection object.")

    return _milvus_collection

def _create_milvus_collection(alias: str) -> Collection:
    log.info(f"Defining schema for collection '{settings.MILVUS_COLLECTION_NAME}' with dim={MILVUS_EMBEDDING_DIM}")
    fields = [
        FieldSchema(name=MILVUS_PK_FIELD, dtype=DataType.INT64, is_primary=True, auto_id=True),
        FieldSchema(name=MILVUS_VECTOR_FIELD, dtype=DataType.FLOAT_VECTOR, dim=MILVUS_EMBEDDING_DIM),
        FieldSchema(name=MILVUS_CONTENT_FIELD, dtype=DataType.VARCHAR, max_length=settings.MILVUS_CONTENT_FIELD_MAX_LENGTH),
        FieldSchema(name=MILVUS_COMPANY_ID_FIELD, dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name=MILVUS_DOCUMENT_ID_FIELD, dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name=MILVUS_FILENAME_FIELD, dtype=DataType.VARCHAR, max_length=512),
    ]
    schema = CollectionSchema(fields, description="Document Chunks for Atenex RAG (SentenceTransformer)")
    log.info(f"Creating collection '{settings.MILVUS_COLLECTION_NAME}'...")
    try:
        collection = Collection(name=settings.MILVUS_COLLECTION_NAME, schema=schema, using=alias, consistency_level="Strong")
        log.info(f"Collection '{settings.MILVUS_COLLECTION_NAME}' created. Creating indexes...")

        index_params = settings.MILVUS_INDEX_PARAMS
        log.info("Creating index for vector field", field_name=MILVUS_VECTOR_FIELD, index_params=index_params)
        collection.create_index(field_name=MILVUS_VECTOR_FIELD, index_params=index_params)

        log.info("Creating scalar index for company_id field...")
        collection.create_index(field_name=MILVUS_COMPANY_ID_FIELD, index_name="company_id_idx")
        log.info("Creating scalar index for document_id field...")
        collection.create_index(field_name=MILVUS_DOCUMENT_ID_FIELD, index_name="document_id_idx")

        log.info("All indexes created successfully.")
        return collection
    except MilvusException as e:
        log.error("Failed to create Zilliz collection or index", collection_name=settings.MILVUS_COLLECTION_NAME, error=str(e), exc_info=True)
        raise RuntimeError(f"Zilliz collection/index creation failed: {e}") from e

def delete_milvus_chunks(company_id: str, document_id: str) -> int:
    del_log = log.bind(company_id=company_id, document_id=document_id)
    try:
        collection = _ensure_milvus_connection_and_collection()
        expr = f'{MILVUS_COMPANY_ID_FIELD} == "{company_id}" and {MILVUS_DOCUMENT_ID_FIELD} == "{document_id}"'
        del_log.info("Querying chunks to delete by primary key", filter_expr=expr)

        pk_results = collection.query(expr=expr, output_fields=[MILVUS_PK_FIELD])
        num_to_delete = len(pk_results)

        if num_to_delete == 0:
            del_log.info("No existing chunks found in Zilliz to delete.")
            return 0

        del_log.info(f"Attempting to delete {num_to_delete} chunks from Zilliz using expression.")
        delete_result = collection.delete(expr=expr)
        actual_deleted_count = delete_result.delete_count
        del_log.info("Zilliz delete operation executed", deleted_count=actual_deleted_count)

        if actual_deleted_count != num_to_delete:
            del_log.warning("Mismatch between expected chunks to delete and actual deleted count",
                            expected=num_to_delete, actual=actual_deleted_count)

        return actual_deleted_count
    except MilvusException as e:
        del_log.error("Zilliz delete error", error=str(e), exc_info=True)
        return 0
    except Exception as e:
        del_log.exception("Unexpected error during Zilliz chunk deletion")
        raise RuntimeError(f"Unexpected Zilliz deletion error: {e}") from e


# --- REFACTORIZADO: Main Ingestion Pipeline Function ---
def ingest_document_pipeline(
    file_bytes: bytes,
    filename: str,
    company_id: str,
    document_id: str,
    content_type: str,
    embedding_model: SentenceTransformer, # LLM_FLAG: Type updated
    delete_existing: bool = True
) -> int:
    ingest_log = log.bind(
        company_id=company_id,
        document_id=document_id,
        filename=filename,
        content_type=content_type
    )
    ingest_log.info("Starting REFACTORED ingestion pipeline (MiniLM/PyMuPDF)")

    # --- 1. Select Extractor ---
    extractor = EXTRACTORS.get(content_type)
    if not extractor:
        ingest_log.error("Unsupported content type for extraction.")
        raise ValueError(f"Unsupported content type: {content_type}")

    # --- 2. Extract Text ---
    ingest_log.debug("Extracting text content from bytes...")
    try:
        text_content = extractor(file_bytes, filename=filename)
        if not text_content or text_content.isspace():
            ingest_log.warning("No text content extracted from the document. Skipping.")
            return 0
        ingest_log.info(f"Text extracted successfully, length: {len(text_content)} chars.")
    except EXTRACTION_ERRORS as ve:
        ingest_log.error("Text extraction failed.", error=str(ve), exc_info=True)
        raise ValueError(f"Extraction failed for {filename}: {ve}") from ve
    except Exception as e:
        ingest_log.exception("Unexpected error during text extraction")
        raise RuntimeError(f"Unexpected extraction error: {e}") from e

    # --- 3. Chunk Text ---
    ingest_log.debug("Chunking extracted text...")
    try:
        chunks = split_text(text_content)
        if not chunks:
            ingest_log.warning("Text content resulted in zero chunks. Skipping.")
            return 0
        ingest_log.info(f"Text chunked into {len(chunks)} chunks.")
    except Exception as e:
        ingest_log.error("Failed to chunk text", error=str(e), exc_info=True)
        raise RuntimeError(f"Chunking failed: {e}") from e

    # --- 4. Embed Chunks ---
    ingest_log.debug(f"Generating embeddings for {len(chunks)} chunks...")
    try:
        embeddings = embed_chunks(chunks)
        ingest_log.info(f"Embeddings generated successfully for {len(embeddings)} chunks.")
        if len(embeddings) != len(chunks):
            ingest_log.error("CRITICAL: Mismatch between number of chunks and generated embeddings!",
                             num_chunks=len(chunks), num_embeddings=len(embeddings))
            raise RuntimeError("Embedding count mismatch error.")
        if embeddings and len(embeddings[0]) != MILVUS_EMBEDDING_DIM:
             ingest_log.error("CRITICAL: Generated embedding dimension mismatch!",
                              actual_dim=len(embeddings[0]), expected_dim=MILVUS_EMBEDDING_DIM)
             raise RuntimeError("Embedding dimension mismatch error.")
    except Exception as e:
        ingest_log.error("Failed to generate embeddings", error=str(e), exc_info=True)
        raise RuntimeError(f"Embedding generation failed: {e}") from e

    # --- 5. Prepare Data for Milvus ---
    max_content_len = settings.MILVUS_CONTENT_FIELD_MAX_LENGTH
    def truncate_utf8_bytes(s, max_bytes):
        b = s.encode('utf-8', errors='ignore')
        if len(b) <= max_bytes: return s
        return b[:max_bytes].decode('utf-8', errors='ignore')

    truncated_chunks = [truncate_utf8_bytes(c, max_content_len) for c in chunks]

    data_to_insert = [
        embeddings,
        truncated_chunks,
        [company_id] * len(chunks),
        [document_id] * len(chunks),
        [filename] * len(chunks),
    ]
    field_names_for_insert = [
        MILVUS_VECTOR_FIELD, MILVUS_CONTENT_FIELD, MILVUS_COMPANY_ID_FIELD,
        MILVUS_DOCUMENT_ID_FIELD, MILVUS_FILENAME_FIELD
    ]
    ingest_log.debug(f"Prepared {len(chunks)} entities for Milvus insertion.", fields=field_names_for_insert)

    # --- 6. Delete Existing Chunks ---
    if delete_existing:
        ingest_log.info("Attempting to delete existing chunks before insertion...")
        try:
            deleted_count = delete_milvus_chunks(company_id, document_id)
            ingest_log.info(f"Deleted {deleted_count} existing chunks.")
        except Exception as del_err:
            ingest_log.error("Failed to delete existing chunks, proceeding with insert anyway.", error=str(del_err))

    # --- 7. Insert into Milvus ---
    ingest_log.debug(f"Inserting {len(chunks)} chunks into Milvus collection '{MILVUS_COLLECTION_NAME}'...")
    try:
        collection = _ensure_milvus_connection_and_collection()
        mutation_result = collection.insert(data_to_insert)
        inserted_count = mutation_result.insert_count

        if inserted_count == len(chunks):
            ingest_log.info(f"Successfully inserted {inserted_count} chunks into Milvus.")
        else:
            ingest_log.error(f"CRITICAL: Milvus insert count mismatch!",
                             expected=len(chunks), inserted=inserted_count, pk_errors=mutation_result.err_indices)
            return 0 # Indicate partial/failed insert

        ingest_log.debug("Flushing Milvus collection...")
        collection.flush()
        ingest_log.info("Milvus collection flushed.")

        return inserted_count

    except MilvusException as e:
        ingest_log.error("Failed to insert data into Milvus", error=str(e), exc_info=True)
        raise RuntimeError(f"Milvus insertion failed: {e}") from e
    except Exception as e:
        ingest_log.exception("Unexpected error during Milvus insertion")
        raise RuntimeError(f"Unexpected Milvus insertion error: {e}") from e