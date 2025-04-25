# ingest-service/app/services/ingest_pipeline.py
from __future__ import annotations

import pathlib
import uuid
import markdown # type: ignore
import html2text # type: ignore
import structlog
from typing import List, Dict, Any, Callable, Optional

# Direct library imports
from bs4 import BeautifulSoup
from pypdf import PdfReader
from docx import Document as DocxDocument # Rename to avoid conflict with Haystack Document if ever used elsewhere
from fastembed.embedding import TextEmbedding # Import specific class
from pymilvus import (
    Collection, CollectionSchema, FieldSchema, DataType, connections,
    utility, MilvusException
)

# Local application imports
from app.core.config import settings

log = structlog.get_logger(__name__)

# --------------- 1. TEXT EXTRACTION FUNCTIONS -----------------

def _extract_from_pdf(file_path: pathlib.Path) -> str:
    """Extracts text content from a PDF file."""
    log.debug("Extracting text from PDF", path=str(file_path))
    try:
        reader = PdfReader(str(file_path))
        text_content = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        log.info("PDF extraction successful", path=str(file_path), num_pages=len(reader.pages))
        return text_content
    except Exception as e:
        log.error("Failed to extract text from PDF", path=str(file_path), error=str(e), exc_info=True)
        raise ValueError(f"Error processing PDF {file_path.name}: {e}") from e

def _extract_from_docx(file_path: pathlib.Path) -> str:
    """Extracts text content from a DOCX file."""
    log.debug("Extracting text from DOCX", path=str(file_path))
    try:
        doc = DocxDocument(str(file_path))
        text_content = "\n\n".join(p.text for p in doc.paragraphs if p.text)
        log.info("DOCX extraction successful", path=str(file_path), num_paragraphs=len(doc.paragraphs))
        return text_content
    except Exception as e:
        log.error("Failed to extract text from DOCX", path=str(file_path), error=str(e), exc_info=True)
        raise ValueError(f"Error processing DOCX {file_path.name}: {e}") from e

def _extract_from_html(file_path: pathlib.Path) -> str:
    """Extracts text content from an HTML file."""
    log.debug("Extracting text from HTML", path=str(file_path))
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            soup = BeautifulSoup(f, "html.parser")
        # Remove script and style elements
        for script_or_style in soup(["script", "style"]):
            script_or_style.decompose()
        # Get text with line breaks
        text_content = soup.get_text(separator="\n", strip=True)
        log.info("HTML extraction successful", path=str(file_path))
        return text_content
    except Exception as e:
        log.error("Failed to extract text from HTML", path=str(file_path), error=str(e), exc_info=True)
        raise ValueError(f"Error processing HTML {file_path.name}: {e}") from e

def _extract_from_md(file_path: pathlib.Path) -> str:
    """Extracts text content from a Markdown file by converting to HTML first."""
    log.debug("Extracting text from Markdown", path=str(file_path))
    try:
        md_content = file_path.read_text(encoding="utf-8")
        html_content = markdown.markdown(md_content)
        # Configure html2text
        h = html2text.HTML2Text()
        h.ignore_links = True
        h.ignore_images = True
        text_content = h.handle(html_content)
        log.info("Markdown extraction successful", path=str(file_path))
        return text_content
    except Exception as e:
        log.error("Failed to extract text from Markdown", path=str(file_path), error=str(e), exc_info=True)
        raise ValueError(f"Error processing Markdown {file_path.name}: {e}") from e

def _extract_from_txt(file_path: pathlib.Path) -> str:
    """Extracts text content from a plain text file."""
    log.debug("Extracting text from TXT", path=str(file_path))
    try:
        text_content = file_path.read_text(encoding="utf-8")
        log.info("TXT extraction successful", path=str(file_path))
        return text_content
    except Exception as e:
        log.error("Failed to extract text from TXT", path=str(file_path), error=str(e), exc_info=True)
        raise ValueError(f"Error processing TXT {file_path.name}: {e}") from e

# Mapping from MIME types to extractor functions
EXTRACTORS: Dict[str, Callable[[pathlib.Path], str]] = {
    "application/pdf": _extract_from_pdf,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": _extract_from_docx,
    "application/msword": _extract_from_docx, # Attempt DOCX extraction for .doc as well
    "text/plain": _extract_from_txt,
    "text/markdown": _extract_from_md,
    "text/html": _extract_from_html,
}

# --------------- 2. CHUNKER FUNCTION -----------------

def chunk_text(text: str, chunk_size: int, chunk_overlap: int) -> List[str]:
    """
    Splits text into chunks based on word count with overlap.
    Simple implementation, can be refined (e.g., sentence splitting).
    """
    if not text or text.isspace():
        return []
    # Basic whitespace split, consider more robust tokenization if needed
    words = re.split(r'\s+', text.strip())
    words = [word for word in words if word] # Remove empty strings

    if not words:
        return []

    chunks: List[str] = []
    current_pos = 0
    while current_pos < len(words):
        end_pos = min(current_pos + chunk_size, len(words))
        chunk = " ".join(words[current_pos:end_pos])
        chunks.append(chunk)

        # Move the starting position for the next chunk
        current_pos += chunk_size - chunk_overlap
        # Ensure we don't get stuck if overlap is too large or chunk_size too small
        if current_pos <= (end_pos - chunk_size): # Avoid infinite loops
             current_pos = end_pos - chunk_overlap # Reset start point relative to end if stuck
        if current_pos >= len(words): # Prevent starting past the end
            break
        # Make sure overlap doesn't push start index negative
        current_pos = max(0, current_pos)


    log.debug(f"Chunked text into {len(chunks)} chunks.", requested_size=chunk_size, overlap=chunk_overlap)
    return chunks

# --------------- 3. MILVUS HELPERS (using pymilvus) -----------------

MILVUS_COLLECTION_NAME = settings.MILVUS_COLLECTION_NAME
MILVUS_EMBEDDING_DIM = settings.EMBEDDING_DIMENSION
MILVUS_PK_FIELD = "pk_id" # Changed primary key name to avoid conflict with 'id' if used elsewhere
MILVUS_VECTOR_FIELD = "embedding"
MILVUS_CONTENT_FIELD = "content"
# Metadata fields for filtering
MILVUS_COMPANY_ID_FIELD = "company_id"
MILVUS_DOCUMENT_ID_FIELD = "document_id"
MILVUS_FILENAME_FIELD = "file_name"

_milvus_collection: Optional[Collection] = None
_milvus_connected = False

def _ensure_milvus_connection_and_collection() -> Collection:
    """Connects to Milvus if not already connected and returns the Collection object."""
    global _milvus_collection, _milvus_connected
    alias = "default" # Standard alias for pymilvus

    if not _milvus_connected:
        uri = settings.MILVUS_URI
        # pymilvus uses 'address' (host:port) or 'uri'
        # Extract host/port if needed, but URI should work for http/https
        log.info("Connecting to Milvus...", uri=uri, alias=alias, timeout=settings.MILVUS_GRPC_TIMEOUT)
        try:
            connections.connect(alias=alias, uri=uri, timeout=settings.MILVUS_GRPC_TIMEOUT)
            _milvus_connected = True
            log.info("Successfully connected to Milvus.")
        except MilvusException as e:
            log.critical("Failed to connect to Milvus", error=str(e), exc_info=True)
            _milvus_connected = False
            raise ConnectionError(f"Milvus connection failed: {e}") from e

    if not _milvus_collection:
        try:
            if not utility.has_collection(MILVUS_COLLECTION_NAME, using=alias):
                log.warning(f"Milvus collection '{MILVUS_COLLECTION_NAME}' not found. Creating...")
                _create_milvus_collection(alias)
            else:
                log.info(f"Milvus collection '{MILVUS_COLLECTION_NAME}' exists.")

            _milvus_collection = Collection(name=MILVUS_COLLECTION_NAME, using=alias)
            # Load collection into memory for searching (important!)
            log.info(f"Loading collection '{MILVUS_COLLECTION_NAME}' into memory...")
            _milvus_collection.load()
            log.info(f"Collection '{MILVUS_COLLECTION_NAME}' loaded.")

        except MilvusException as e:
            log.error(f"Failed to get or load Milvus collection '{MILVUS_COLLECTION_NAME}'", error=str(e), exc_info=True)
            _milvus_collection = None # Reset on error
            raise RuntimeError(f"Milvus collection error: {e}") from e

    return _milvus_collection

def _create_milvus_collection(alias: str):
    """Creates the Milvus collection with the defined schema and index."""
    log.info(f"Defining schema for collection '{MILVUS_COLLECTION_NAME}'")
    # Define fields using pymilvus FieldSchema
    fields = [
        FieldSchema(name=MILVUS_PK_FIELD, dtype=DataType.INT64, is_primary=True, auto_id=True),
        FieldSchema(name=MILVUS_VECTOR_FIELD, dtype=DataType.FLOAT_VECTOR, dim=MILVUS_EMBEDDING_DIM),
        FieldSchema(name=MILVUS_CONTENT_FIELD, dtype=DataType.VARCHAR, max_length=settings.SPLITTER_CHUNK_SIZE * 4), # Allow longer content
        FieldSchema(name=MILVUS_COMPANY_ID_FIELD, dtype=DataType.VARCHAR, max_length=64), # UUID string length = 36
        FieldSchema(name=MILVUS_DOCUMENT_ID_FIELD, dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name=MILVUS_FILENAME_FIELD, dtype=DataType.VARCHAR, max_length=512), # Allow longer filenames
        # Add other metadata fields if needed, ensure they are indexed if used for filtering often
    ]
    schema = CollectionSchema(fields, description="Document Chunks for Atenex RAG")

    log.info(f"Creating collection '{MILVUS_COLLECTION_NAME}'...")
    try:
        collection = Collection(name=MILVUS_COLLECTION_NAME, schema=schema, using=alias, consistency_level="Strong") # Use Strong consistency
        log.info(f"Collection '{MILVUS_COLLECTION_NAME}' created. Creating index...")

        # Create index on the vector field
        # Use index parameters from settings
        index_params = settings.MILVUS_INDEX_PARAMS
        # Example: {"metric_type": "COSINE", "index_type": "HNSW", "params": {"M": 16, "efConstruction": 256}}
        log.info("Creating index for vector field", field_name=MILVUS_VECTOR_FIELD, index_params=index_params)
        collection.create_index(field_name=MILVUS_VECTOR_FIELD, index_params=index_params)
        log.info("Index created successfully.")

        # Optional: Create index on metadata fields used for filtering
        log.info("Creating scalar index for company_id field...")
        collection.create_index(field_name=MILVUS_COMPANY_ID_FIELD, index_name="company_id_idx")
        log.info("Creating scalar index for document_id field...")
        collection.create_index(field_name=MILVUS_DOCUMENT_ID_FIELD, index_name="document_id_idx")
        log.info("Scalar indexes created.")

    except MilvusException as e:
        log.error("Failed to create Milvus collection or index", collection_name=MILVUS_COLLECTION_NAME, error=str(e), exc_info=True)
        raise RuntimeError(f"Milvus collection/index creation failed: {e}") from e


def delete_milvus_chunks(company_id: str, document_id: str) -> int:
    """Deletes chunks from Milvus based on company_id and document_id."""
    del_log = log.bind(company_id=company_id, document_id=document_id)
    try:
        collection = _ensure_milvus_connection_and_collection()
        # Construct the expression for deletion
        expr = f'{MILVUS_COMPANY_ID_FIELD} == "{company_id}" and {MILVUS_DOCUMENT_ID_FIELD} == "{document_id}"'
        del_log.info("Attempting to delete existing chunks from Milvus", filter_expr=expr)
        # delete() returns a MutationResult object
        delete_result = collection.delete(expr=expr)
        deleted_count = delete_result.delete_count
        del_log.info("Milvus deletion successful", deleted_count=deleted_count)
        return deleted_count
    except MilvusException as e:
        del_log.error("Failed to delete chunks from Milvus", error=str(e), exc_info=True)
        raise RuntimeError(f"Milvus deletion failed: {e}") from e
    except Exception as e: # Catch potential connection errors etc.
        del_log.exception("Unexpected error during Milvus chunk deletion", error=str(e))
        raise RuntimeError(f"Unexpected Milvus deletion error: {e}") from e


# --------------- 4. EMBEDDING MODEL (GLOBAL INSTANCE) -----------------

# Initialize FastEmbed model once per worker process
# Use device="cpu" explicitly as USE_GPU is False
try:
    log.info("Initializing FastEmbed model instance for pipeline...", model=settings.FASTEMBED_MODEL, device="cpu")
    # We use TextEmbedding directly now, not the Haystack integration component
    # Pass model_name directly. It handles device selection internally based on available runtimes.
    # Set parallel=0 for CPU to avoid potential over-subscription of processes with Celery prefork.
    embedding_model = TextEmbedding(
        model_name=settings.FASTEMBED_MODEL,
        cache_dir=os.environ.get("FASTEMBED_CACHE_DIR"), # Optional: configure cache
        threads=None, # Let FastEmbed decide based on environment / CPU cores
        parallel=0 # Set to 0 for CPU to disable multi-processing within fastembed
    )
    log.info("FastEmbed model instance initialized successfully.")
except Exception as e:
    log.critical("CRITICAL: Failed to initialize FastEmbed model instance!", error=str(e), exc_info=True)
    embedding_model = None # Ensure it's None so pipeline function fails if model load fails


# --------------- 5. MAIN INGEST FUNCTION -----------------

def ingest_document_pipeline(
    file_path: pathlib.Path,
    company_id: str,
    document_id: str,
    content_type: str,
    delete_existing: bool = True # Option to delete existing chunks before inserting new ones
) -> int:
    """
    Processes a single document: extracts text, chunks, embeds, and inserts into Milvus.

    Args:
        file_path: Path to the downloaded document file.
        company_id: The company ID for multi-tenancy.
        document_id: The unique ID for the document.
        content_type: The MIME type of the file.
        delete_existing: If True, delete existing chunks for this company/document before inserting.

    Returns:
        The number of chunks successfully inserted into Milvus.

    Raises:
        ValueError: If the content type is unsupported or extraction fails.
        ConnectionError: If connection to Milvus fails.
        RuntimeError: For other Milvus or Embedding errors.
    """
    ingest_log = log.bind(
        company_id=company_id,
        document_id=document_id,
        filename=file_path.name,
        content_type=content_type
    )
    ingest_log.info("Starting ingestion pipeline for document")

    # --- 0. Check if embedding model loaded ---
    if not embedding_model:
        ingest_log.error("FastEmbed model is not available. Cannot proceed.")
        raise RuntimeError("Embedding model failed to initialize.")

    # --- 1. Select Extractor ---
    extractor = EXTRACTORS.get(content_type)
    if not extractor:
        ingest_log.error("Unsupported content type for extraction.")
        raise ValueError(f"Unsupported content type: {content_type}")

    # --- 2. Extract Text ---
    ingest_log.debug("Extracting text content...")
    try:
        text_content = extractor(file_path)
        if not text_content or text_content.isspace():
            ingest_log.warning("No text content extracted from the document. Skipping embedding and insertion.")
            return 0
        ingest_log.info(f"Text extracted successfully, length: {len(text_content)} chars.")
    except ValueError as ve: # Catch extraction errors raised by helpers
        ingest_log.error("Text extraction failed.", error=str(ve))
        raise # Re-raise the specific error

    # --- 3. Chunk Text ---
    ingest_log.debug("Chunking extracted text...")
    chunks = chunk_text(
        text_content,
        chunk_size=settings.SPLITTER_CHUNK_SIZE,
        chunk_overlap=settings.SPLITTER_CHUNK_OVERLAP
    )
    if not chunks:
        ingest_log.warning("Text content resulted in zero chunks. Skipping embedding and insertion.")
        return 0
    ingest_log.info(f"Text chunked into {len(chunks)} chunks.")

    # --- 4. Embed Chunks ---
    ingest_log.debug(f"Generating embeddings for {len(chunks)} chunks...")
    try:
        # FastEmbed's embed method returns an iterator of numpy arrays
        # We convert it to a list for insertion
        embeddings = list(embedding_model.embed(chunks))
        ingest_log.info(f"Embeddings generated successfully for {len(embeddings)} chunks.")
        if len(embeddings) != len(chunks):
            # This shouldn't happen with current fastembed versions, but good sanity check
             ingest_log.warning("Mismatch between number of chunks and generated embeddings.", num_chunks=len(chunks), num_embeddings=len(embeddings))
             # Decide how to handle: maybe trim lists? For now, log and continue.
             min_len = min(len(chunks), len(embeddings))
             chunks = chunks[:min_len]
             embeddings = embeddings[:min_len]

    except Exception as e:
        ingest_log.error("Failed to generate embeddings", error=str(e), exc_info=True)
        raise RuntimeError(f"Embedding generation failed: {e}") from e

    # --- 5. Prepare Data for Milvus ---
    data_to_insert = [
        embeddings,
        chunks,
        [company_id] * len(chunks),
        [document_id] * len(chunks),
        [file_path.name] * len(chunks),
    ]
    # Ensure order matches the FieldSchema defined in _create_milvus_collection
    # (excluding the auto-id primary key field)

    # --- 6. Delete Existing Chunks (Optional) ---
    if delete_existing:
        try:
            deleted_count = delete_milvus_chunks(company_id, document_id)
            ingest_log.info(f"Deleted {deleted_count} existing chunks before insertion.")
        except Exception as del_err:
            # Log the error but proceed with insertion attempt
            ingest_log.error("Failed to delete existing chunks, proceeding with insert anyway.", error=str(del_err))


    # --- 7. Insert into Milvus ---
    ingest_log.debug(f"Inserting {len(chunks)} chunks into Milvus collection '{MILVUS_COLLECTION_NAME}'...")
    try:
        collection = _ensure_milvus_connection_and_collection()
        # Use the pymilvus insert method
        mutation_result = collection.insert(data_to_insert)
        inserted_count = mutation_result.insert_count

        if inserted_count == len(chunks):
            ingest_log.info(f"Successfully inserted {inserted_count} chunks into Milvus.")
            # Ensure data is flushed to disk segment for persistence and visibility
            log.debug("Flushing Milvus collection...")
            collection.flush()
            log.info("Milvus collection flushed.")
        else:
             # This might indicate a partial insert or an issue.
             ingest_log.warning(f"Milvus insert result count ({inserted_count}) differs from chunks sent ({len(chunks)}).")
             # Still return the reported count, but this warrants investigation if frequent.

        return inserted_count

    except MilvusException as e:
        ingest_log.error("Failed to insert data into Milvus", error=str(e), exc_info=True)
        raise RuntimeError(f"Milvus insertion failed: {e}") from e
    except Exception as e: # Catch potential connection errors etc.
        ingest_log.exception("Unexpected error during Milvus insertion", error=str(e))
        raise RuntimeError(f"Unexpected Milvus insertion error: {e}") from e