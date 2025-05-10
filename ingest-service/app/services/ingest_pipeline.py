# ingest-service/app/services/ingest_pipeline.py
from __future__ import annotations

import os
import uuid
import hashlib
import structlog
from typing import List, Dict, Any, Optional, Tuple, Union

# --- EARLY IMPORTS ---
from app.core.config import settings # IMPORT SETTINGS FIRST
log = structlog.get_logger(__name__) # GET LOGGER AFTER STRUCTLOG IMPORT

# Tokenizer for metadata (Try importing AFTER settings and log are available)
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
    utility, MilvusException
)

# Local application imports
from .extractors.pdf_extractor import extract_text_from_pdf, PdfExtractionError
from .extractors.docx_extractor import extract_text_from_docx, DocxExtractionError
from .extractors.txt_extractor import extract_text_from_txt, TxtExtractionError
from .extractors.md_extractor import extract_text_from_md, MdExtractionError
from .extractors.html_extractor import extract_text_from_html, HtmlExtractionError
from .text_splitter import split_text
# from .embedder import embed_chunks # Eliminado
from app.services.clients.embedding_service_client import EmbeddingServiceClient, EmbeddingServiceClientError # Nuevo
from app.models.domain import DocumentChunkMetadata, DocumentChunkData, ChunkVectorStatus


# --- Mapeo de Content-Type a Funciones Extractoras ---
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
    MdExtractionError, HtmlExtractionError, ValueError
)

# --- Constantes Milvus (Incluye nuevos campos) ---
MILVUS_COLLECTION_NAME = settings.MILVUS_COLLECTION_NAME
MILVUS_EMBEDDING_DIM = settings.EMBEDDING_DIMENSION # Esta dimensión DEBE coincidir con la del embedding_service
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

_milvus_collection: Optional[Collection] = None
_milvus_connected = False

def _ensure_milvus_connection_and_collection() -> Collection:
    global _milvus_collection, _milvus_connected
    alias = "pipeline_worker"
    connect_log = log.bind(milvus_alias=alias)

    connection_exists = alias in connections.list_connections()
    is_connected = False
    if connection_exists:
        try:
             if utility.has_collection(MILVUS_COLLECTION_NAME, using=alias):
                 is_connected = True
             else:
                 is_connected = False
                 connect_log.warning("Connection alias exists but collection check failed, may reconnect.")
        except Exception as conn_check_err:
            connect_log.warning("Error checking existing Milvus connection status, will attempt reconnect.", error=str(conn_check_err))
            is_connected = False
            try: connections.disconnect(alias)
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

    if _milvus_collection is None:
        try:
            if not utility.has_collection(MILVUS_COLLECTION_NAME, using=alias):
                connect_log.warning(f"Milvus collection '{MILVUS_COLLECTION_NAME}' not found. Attempting to create.")
                collection = _create_milvus_collection(alias)
            else:
                collection = Collection(name=MILVUS_COLLECTION_NAME, using=alias)
                connect_log.debug(f"Using existing Milvus collection '{MILVUS_COLLECTION_NAME}'.")
                _check_and_create_indexes(collection)

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
    create_log = log.bind(collection_name=MILVUS_COLLECTION_NAME, embedding_dim=MILVUS_EMBEDDING_DIM)
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
    schema = CollectionSchema(
        fields, description="Atenex Document Chunks with Enhanced Metadata", enable_dynamic_field=False
    )
    create_log.info("Schema defined. Creating collection...")
    try:
        collection = Collection(
            name=MILVUS_COLLECTION_NAME, schema=schema, using=alias, consistency_level="Strong"
        )
        create_log.info(f"Collection '{MILVUS_COLLECTION_NAME}' created. Creating indexes...")
        index_params = settings.MILVUS_INDEX_PARAMS
        create_log.info("Creating HNSW index for vector field", field_name=MILVUS_VECTOR_FIELD, index_params=index_params)
        collection.create_index(field_name=MILVUS_VECTOR_FIELD, index_params=index_params, index_name=f"{MILVUS_VECTOR_FIELD}_hnsw_idx")
        create_log.info("Creating scalar index for company_id field...")
        collection.create_index(field_name=MILVUS_COMPANY_ID_FIELD, index_name=f"{MILVUS_COMPANY_ID_FIELD}_idx")
        create_log.info("Creating scalar index for document_id field...")
        collection.create_index(field_name=MILVUS_DOCUMENT_ID_FIELD, index_name=f"{MILVUS_DOCUMENT_ID_FIELD}_idx")
        create_log.info("Creating scalar index for content_hash field...")
        collection.create_index(field_name=MILVUS_CONTENT_HASH_FIELD, index_name=f"{MILVUS_CONTENT_HASH_FIELD}_idx")
        create_log.info("All required indexes created successfully.")
        return collection
    except MilvusException as e:
        create_log.error("Failed to create Milvus collection or index", error=str(e), exc_info=True)
        raise RuntimeError(f"Milvus collection/index creation failed: {e}") from e

def _check_and_create_indexes(collection: Collection):
    check_log = log.bind(collection_name=collection.name)
    try:
        existing_indexes = collection.indexes
        existing_index_fields = {idx.field_name for idx in existing_indexes}
        required_indexes = {
            MILVUS_VECTOR_FIELD: (settings.MILVUS_INDEX_PARAMS, f"{MILVUS_VECTOR_FIELD}_hnsw_idx"),
            MILVUS_COMPANY_ID_FIELD: (None, f"{MILVUS_COMPANY_ID_FIELD}_idx"),
            MILVUS_DOCUMENT_ID_FIELD: (None, f"{MILVUS_DOCUMENT_ID_FIELD}_idx"),
            MILVUS_CONTENT_HASH_FIELD: (None, f"{MILVUS_CONTENT_HASH_FIELD}_idx"),
        }
        for field_name, (index_params, index_name) in required_indexes.items():
            if field_name not in existing_index_fields:
                check_log.warning(f"Index missing for field '{field_name}'. Creating '{index_name}'...")
                collection.create_index(field_name=field_name, index_params=index_params, index_name=index_name)
                check_log.info(f"Index '{index_name}' created for field '{field_name}'.")
            else:
                check_log.debug(f"Index already exists for field '{field_name}'.")
    except MilvusException as e:
        check_log.error("Failed during index check/creation on existing collection", error=str(e))

def delete_milvus_chunks(company_id: str, document_id: str) -> int:
    del_log = log.bind(company_id=company_id, document_id=document_id)
    expr = f'{MILVUS_COMPANY_ID_FIELD} == "{company_id}" and {MILVUS_DOCUMENT_ID_FIELD} == "{document_id}"'
    pks_to_delete: List[str] = []
    deleted_count = 0
    try:
        collection = _ensure_milvus_connection_and_collection()
        del_log.info("Querying Milvus for PKs to delete...", filter_expr=expr)
        query_res = collection.query(expr=expr, output_fields=[MILVUS_PK_FIELD])
        pks_to_delete = [item[MILVUS_PK_FIELD] for item in query_res if MILVUS_PK_FIELD in item]
        if not pks_to_delete:
            del_log.info("No matching primary keys found in Milvus for deletion.")
            return 0
        del_log.info(f"Found {len(pks_to_delete)} primary keys to delete.")
        delete_expr = f'{MILVUS_PK_FIELD} in {pks_to_delete}'
        del_log.info("Attempting to delete chunks from Milvus using PK list expression.", filter_expr=delete_expr)
        delete_result = collection.delete(expr=delete_expr)
        deleted_count = delete_result.delete_count
        del_log.info("Milvus delete operation by PK list executed.", deleted_count=deleted_count)
        if deleted_count != len(pks_to_delete):
             del_log.warning("Milvus delete count mismatch.", expected=len(pks_to_delete), reported=deleted_count)
        return deleted_count
    except MilvusException as e:
        del_log.error("Milvus delete error (query or delete phase)", error=str(e), exc_info=True)
        return 0
    except Exception as e:
        del_log.exception("Unexpected error during Milvus chunk deletion")
        return 0

async def ingest_document_pipeline( # Marcado como async para usar el cliente async
    file_bytes: bytes,
    filename: str,
    company_id: str,
    document_id: str,
    content_type: str,
    # embedding_model: SentenceTransformer, # Eliminado
    embedding_service_client: EmbeddingServiceClient, # Nuevo
    delete_existing: bool = True
) -> Tuple[int, List[str], List[DocumentChunkData]]:
    ingest_log = log.bind(
        company_id=company_id, document_id=document_id, filename=filename, content_type=content_type
    )
    ingest_log.info("Starting ingestion pipeline (using Embedding Service)")

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
        else:
            ingest_log.info(f"Text extracted successfully from {len(extracted_content)} pages.")
    except EXTRACTION_ERRORS as ve:
        ingest_log.error("Text extraction failed.", error=str(ve), exc_info=True)
        raise ValueError(f"Extraction failed for {filename}: {ve}") from ve
    except Exception as e:
        ingest_log.exception("Unexpected error during text extraction")
        raise RuntimeError(f"Unexpected extraction error: {e}") from e

    ingest_log.debug("Chunking text and generating metadata...")
    processed_chunks: List[DocumentChunkData] = []
    chunk_index_counter = 0

    def _process_text_block(text_block: str, page_num: Optional[int]):
        nonlocal chunk_index_counter
        try:
            text_chunks = split_text(text_block)
            for chunk_text in text_chunks:
                if not chunk_text or chunk_text.isspace(): continue
                tokens = len(tiktoken_enc.encode(chunk_text)) if tiktoken_enc else -1
                content_hash = hashlib.sha256(chunk_text.encode('utf-8', errors='ignore')).hexdigest()
                title = f"{filename[:30]}... (Page {page_num or 'N/A'}, Chunk {chunk_index_counter+1})"
                metadata_obj = DocumentChunkMetadata(
                    page=page_num, title=title[:500], tokens=tokens, content_hash=content_hash
                )
                chunk_data = DocumentChunkData(
                    document_id=uuid.UUID(document_id), company_id=uuid.UUID(company_id),
                    chunk_index=chunk_index_counter, content=chunk_text, metadata=metadata_obj
                )
                processed_chunks.append(chunk_data)
                chunk_index_counter += 1
        except Exception as chunking_err:
            ingest_log.error("Error processing text block during chunking/metadata", page=page_num, error=str(chunking_err), exc_info=True)

    if isinstance(extracted_content, str):
         _process_text_block(extracted_content, page_num=None)
    else:
         for page_number, page_text in extracted_content:
             _process_text_block(page_text, page_num=page_number)

    if not processed_chunks:
        ingest_log.warning("Text content resulted in zero processable chunks. Skipping.")
        return 0, [], []
    ingest_log.info(f"Text processed into {len(processed_chunks)} chunks with metadata.")

    chunk_contents = [chunk.content for chunk in processed_chunks]
    ingest_log.debug(f"Requesting embeddings for {len(chunk_contents)} chunks from Embedding Service...")
    embeddings: List[List[float]] = []
    try:
        # Llamada al EmbeddingServiceClient
        embeddings, model_info = await embedding_service_client.get_embeddings(chunk_contents)
        ingest_log.info(f"Embeddings received from service for {len(embeddings)} chunks. Model: {model_info}")

        if len(embeddings) != len(processed_chunks):
            # Esto podría ocurrir si el servicio de embedding omite algunos textos (ej. vacíos)
            # Deberíamos manejar esta discrepancia. Por ahora, error.
            ingest_log.error("Embedding count mismatch from service.", expected=len(processed_chunks), received=len(embeddings))
            raise RuntimeError(f"Embedding count mismatch. Expected {len(processed_chunks)}, got {len(embeddings)}.")

        # Validar dimensión de los embeddings recibidos
        expected_dim_from_service = model_info.get("dimension", 0)
        if MILVUS_EMBEDDING_DIM != expected_dim_from_service:
            ingest_log.warning(
                f"Milvus schema dimension ({MILVUS_EMBEDDING_DIM}) "
                f"differs from embedding service model dimension ({expected_dim_from_service}). "
                f"Using Milvus schema dimension. Ensure this is intended."
            )
            # Nota: Si las dimensiones no coinciden, Milvus fallará la inserción.
            # El `ingest-service` está configurado con `EMBEDDING_DIMENSION` para su esquema Milvus.
            # Es crucial que esta configuración coincida con la dimensión real que entrega el `embedding-service`.
            # El `embedding-service` informa su dimensión, así que podríamos usarla para una validación más estricta aquí.

        if embeddings and len(embeddings[0]) != MILVUS_EMBEDDING_DIM:
             # Si la dimensión real de los embeddings recibidos no coincide con la esperada para Milvus.
             ingest_log.error(
                 f"Received embedding dimension ({len(embeddings[0])}) from service "
                 f"does not match configured Milvus dimension ({MILVUS_EMBEDDING_DIM})."
             )
             raise RuntimeError(f"Embedding dimension mismatch error from service. Expected {MILVUS_EMBEDDING_DIM}, got {len(embeddings[0])}")

    except EmbeddingServiceClientError as esce:
        ingest_log.error("Failed to get embeddings from Embedding Service", error=str(esce), details=esce.detail, exc_info=True)
        raise RuntimeError(f"Embedding Service client error: {esce}") from esce
    except Exception as e:
        ingest_log.error("Failed to generate/retrieve embeddings", error=str(e), exc_info=True)
        raise RuntimeError(f"Embedding generation/retrieval failed: {e}") from e

    max_content_len = settings.MILVUS_CONTENT_FIELD_MAX_LENGTH
    def truncate_utf8_bytes(s, max_bytes):
        b = s.encode('utf-8', errors='ignore')
        if len(b) <= max_bytes: return s
        return b[:max_bytes].decode('utf-8', errors='ignore')

    milvus_pks = [f"{document_id}_{i}" for i in range(len(processed_chunks))]
    data_to_insert = [
        milvus_pks,
        embeddings,
        [truncate_utf8_bytes(c.content, max_content_len) for c in processed_chunks],
        [company_id] * len(processed_chunks),
        [document_id] * len(processed_chunks),
        [filename] * len(processed_chunks),
        [c.metadata.page if c.metadata.page is not None else -1 for c in processed_chunks],
        [c.metadata.title if c.metadata.title else "" for c in processed_chunks],
        [c.metadata.tokens if c.metadata.tokens is not None else -1 for c in processed_chunks],
        [c.metadata.content_hash if c.metadata.content_hash else "" for c in processed_chunks]
    ]
    ingest_log.debug(f"Prepared {len(processed_chunks)} entities for Milvus insertion with custom PKs.")

    if delete_existing:
        ingest_log.info("Attempting to delete existing chunks before insertion...")
        try:
            deleted_count = delete_milvus_chunks(company_id, document_id)
            ingest_log.info(f"Deleted {deleted_count} existing chunks.")
        except Exception as del_err:
            ingest_log.error("Failed to delete existing chunks, proceeding with insert anyway.", error=str(del_err))

    ingest_log.debug(f"Inserting {len(processed_chunks)} chunks into Milvus collection '{MILVUS_COLLECTION_NAME}'...")
    try:
        collection = _ensure_milvus_connection_and_collection()
        mutation_result = collection.insert(data_to_insert)
        inserted_count = mutation_result.insert_count

        if inserted_count != len(processed_chunks):
             ingest_log.error("Milvus insert count mismatch!", expected=len(processed_chunks), inserted=inserted_count, errors=mutation_result.err_indices)
        else:
             ingest_log.info(f"Successfully inserted {inserted_count} chunks into Milvus.")

        returned_pks = mutation_result.primary_keys
        if len(returned_pks) != inserted_count:
             ingest_log.error("Milvus returned PK count mismatch!", returned_count=len(returned_pks), inserted_count=inserted_count)
             successfully_inserted_chunks_data = []
        else:
             ingest_log.info("Milvus returned PKs match inserted count.")
             successfully_inserted_chunks_data = []
             for i in range(inserted_count):
                 # Assuming returned_pks are in the same order as inserted data
                 # Map back to original_chunk based on the Milvus PK we generated
                 # Note: Milvus SDK >2.2.9 might return custom PKs directly.
                 # Our generated PK was `milvus_pks[i]`
                 chunk_milvus_pk = milvus_pks[i] # The PK we intended to insert
                 original_chunk = next((c for c in processed_chunks if f"{c.document_id}_{c.chunk_index}" == chunk_milvus_pk), None)
                 
                 if original_chunk:
                    # We assume the `returned_pks[i]` from Milvus *is* our `chunk_milvus_pk` if insertion was by our PK.
                    # If Milvus auto-generates PKs (auto_id=True on PK field), then `returned_pks[i]` would be different.
                    # Our schema has `is_primary=True` on `MILVUS_PK_FIELD` which is `pk_id` (VARCHAR).
                    # Milvus `insert` expects the PKs to be provided in the data if `is_primary=True` and `auto_id=False` (default for VARCHAR PK).
                    # So, `returned_pks[i]` should be equal to `milvus_pks[i]`.
                    original_chunk.embedding_id = str(returned_pks[i]) # Store the PK confirmed by Milvus
                    original_chunk.vector_status = ChunkVectorStatus.CREATED
                    successfully_inserted_chunks_data.append(original_chunk)
                 else:
                     log.warning("Could not find original chunk data for returned PK", pk=returned_pks[i])

             if len(successfully_inserted_chunks_data) != inserted_count:
                  log.warning("Mismatch between Milvus inserted count and successfully mapped chunks for PG.")

        ingest_log.debug("Flushing Milvus collection...")
        collection.flush()
        ingest_log.info("Milvus collection flushed.")

        return inserted_count, [str(pk) for pk in returned_pks], successfully_inserted_chunks_data

    except MilvusException as e:
        ingest_log.error("Failed to insert data into Milvus", error=str(e), exc_info=True)
        raise RuntimeError(f"Milvus insertion failed: {e}") from e
    except Exception as e:
        ingest_log.exception("Unexpected error during Milvus insertion")
        raise RuntimeError(f"Unexpected Milvus insertion error: {e}") from e