import uuid
from typing import Any, Optional, Dict, List
import asyncpg
from pydantic import BaseModel, Field
import structlog
import json # Import json for metadata serialization

from app.core.config import settings
from app.models.domain import DocumentStatus

log = structlog.get_logger(__name__)

# --- Pydantic models remain useful for clarity ---
class DocumentRecord(BaseModel):
    id: uuid.UUID
    company_id: uuid.UUID
    file_name: str
    file_type: str
    file_path: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    chunk_count: Optional[int] = None
    status: DocumentStatus
    error_message: Optional[str] = None

# DocumentChunkRecord might not be needed if all info is in Milvus + Haystack Document
# class DocumentChunkRecord(BaseModel): ...

# --- Database Interaction Logic ---
_pool: Optional[asyncpg.Pool] = None

async def get_db_pool() -> asyncpg.Pool:
    """Obtiene o crea el pool de conexiones a la base de datos."""
    global _pool
    if _pool is None or _pool._closed: # Check if pool is closed
        try:
            log.info("Creating database connection pool...")
            _pool = await asyncpg.create_pool(
                dsn=str(settings.POSTGRES_DSN),
                min_size=5,
                max_size=20,
                # Setup to automatically encode/decode JSONB
                init=lambda conn: conn.set_type_codec(
                    'jsonb',
                    encoder=lambda d: json.dumps(d),
                    decoder=lambda s: json.loads(s),
                    schema='pg_catalog',
                    format='text' # Use text format for JSONB with asyncpg
                )
            )
            log.info("Database connection pool created successfully.")
        except Exception as e:
            log.error("Failed to create database connection pool", error=str(e), dsn=str(settings.POSTGRES_DSN), exc_info=True)
            raise
    return _pool

async def close_db_pool():
    """Cierra el pool de conexiones."""
    global _pool
    if _pool and not _pool._closed:
        log.info("Closing database connection pool...")
        await _pool.close()
        _pool = None
        log.info("Database connection pool closed.")

async def create_document(
    company_id: uuid.UUID,
    file_name: str,
    file_type: str,
    metadata: Dict[str, Any]
) -> uuid.UUID:
    """Crea un registro inicial para el documento."""
    pool = await get_db_pool()
    doc_id = uuid.uuid4()
    # Ensure metadata is serializable (Pydantic model would handle this better)
    try:
        serialized_metadata = json.dumps(metadata)
    except TypeError as e:
        log.error("Metadata is not JSON serializable", metadata=metadata, error=e)
        raise ValueError("Provided metadata cannot be serialized to JSON") from e

    query = """
        INSERT INTO DOCUMENTS (id, company_id, file_name, file_type, metadata, status, file_path, uploaded_at, updated_at)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, '', NOW(), NOW())
        RETURNING id;
    """
    try:
        # Use acquire() for potentially better connection handling within a request/task
        async with pool.acquire() as connection:
            result = await connection.fetchval(
                query,
                doc_id,
                company_id,
                file_name,
                file_type,
                serialized_metadata, # Pass the serialized string
                DocumentStatus.UPLOADED.value
            )
        if result:
            log.info("Document record created", document_id=doc_id, company_id=company_id)
            return result
        else:
             raise RuntimeError("Failed to create document record, no ID returned.")
    except Exception as e:
        log.error("Failed to create document record", error=e, company_id=company_id, file_name=file_name, exc_info=True)
        raise

async def update_document_status(
    document_id: uuid.UUID,
    status: DocumentStatus,
    file_path: Optional[str] = None,
    chunk_count: Optional[int] = None,
    error_message: Optional[str] = None,
) -> bool:
    """Actualiza el estado y otros campos de un documento."""
    pool = await get_db_pool()
    fields_to_update = ["status = $2", "updated_at = NOW()"]
    params: List[Any] = [document_id, status.value]
    current_param_index = 3

    if file_path is not None:
        fields_to_update.append(f"file_path = ${current_param_index}")
        params.append(file_path)
        current_param_index += 1
    if chunk_count is not None:
        fields_to_update.append(f"chunk_count = ${current_param_index}")
        params.append(chunk_count)
        current_param_index += 1
    # Handle error message update carefully
    if status == DocumentStatus.ERROR:
        fields_to_update.append(f"error_message = ${current_param_index}")
        # Truncate error message if too long for DB column
        params.append(error_message[:1000] if error_message else "Unknown processing error")
    else:
        # Clear error message if status is not ERROR
        fields_to_update.append("error_message = NULL")


    query = f"""
        UPDATE DOCUMENTS
        SET {', '.join(fields_to_update)}
        WHERE id = $1;
    """
    try:
        async with pool.acquire() as connection:
             result = await connection.execute(query, *params)
        success = result == "UPDATE 1"
        if success:
            log.info("Document status updated", document_id=document_id, status=status.value, file_path=file_path, chunk_count=chunk_count, has_error=error_message is not None)
        else:
            # This might happen if the document was deleted between checks
            log.warning("Document status update did not affect any rows (document might not exist)", document_id=document_id, status=status.value)
        return success
    except Exception as e:
        log.error("Failed to update document status", error=e, document_id=document_id, status=status.value, exc_info=True)
        raise

async def get_document_status(document_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """Obtiene el estado y otros datos de un documento."""
    pool = await get_db_pool()
    # Include company_id for potential authorization checks
    query = """
        SELECT id, company_id, status, file_name, file_type, chunk_count, error_message, updated_at
        FROM DOCUMENTS
        WHERE id = $1;
    """
    try:
        async with pool.acquire() as connection:
            record = await connection.fetchrow(query, document_id)
        if record:
            # Convert asyncpg Record to dict
            return dict(record)
        log.warning("Document status requested for non-existent ID", document_id=document_id)
        return None
    except Exception as e:
        log.error("Failed to get document status", error=e, document_id=document_id, exc_info=True)
        raise

# Removed save_chunks function as Haystack DocumentWriter handles persistence to Milvus