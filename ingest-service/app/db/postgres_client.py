# ingest-service/app/db/postgres_client.py
import uuid
from typing import Any, Optional, Dict, List
import asyncpg
import structlog
import json
from datetime import datetime, timezone

from app.core.config import settings
from app.models.domain import DocumentStatus

log = structlog.get_logger(__name__)

_pool: Optional[asyncpg.Pool] = None

# --- Pool Management (Sin cambios) ---
async def get_db_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None or _pool._closed:
        log.info("Creating PostgreSQL connection pool...", host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT, user=settings.POSTGRES_USER, db=settings.POSTGRES_DB)
        try:
            def _json_encoder(value): return json.dumps(value)
            def _json_decoder(value): return json.loads(value)
            async def init_connection(conn):
                # LLM_COMMENT: Ensure codecs are set correctly for JSONB interaction
                await conn.set_type_codec('jsonb', encoder=_json_encoder, decoder=_json_decoder, schema='pg_catalog', format='text')
                await conn.set_type_codec('json', encoder=_json_encoder, decoder=_json_decoder, schema='pg_catalog', format='text')

            _pool = await asyncpg.create_pool(
                user=settings.POSTGRES_USER, password=settings.POSTGRES_PASSWORD.get_secret_value(),
                database=settings.POSTGRES_DB, host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT,
                min_size=2, max_size=10, timeout=30.0, command_timeout=60.0,
                init=init_connection, statement_cache_size=0 # LLM_COMMENT: Keep statement_cache_size=0 with custom codecs
            )
            log.info("PostgreSQL connection pool created successfully.")
        except (asyncpg.exceptions.InvalidPasswordError, OSError, ConnectionRefusedError) as conn_err:
            log.critical("CRITICAL: Failed to connect to PostgreSQL", error=str(conn_err), exc_info=True)
            _pool = None; raise ConnectionError(f"Failed to connect to PostgreSQL: {conn_err}") from conn_err
        except Exception as e:
            log.critical("CRITICAL: Failed to create PostgreSQL connection pool", error=str(e), exc_info=True)
            _pool = None; raise RuntimeError(f"Failed to create PostgreSQL pool: {e}") from e
    return _pool

async def close_db_pool():
    global _pool
    if _pool and not _pool._closed: log.info("Closing PostgreSQL connection pool..."); await _pool.close(); _pool = None; log.info("PostgreSQL connection pool closed.")
    elif _pool and _pool._closed: log.warning("Attempted to close an already closed PostgreSQL pool."); _pool = None
    else: log.info("No active PostgreSQL connection pool to close.")

async def check_db_connection() -> bool:
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            async with conn.transaction(): result = await conn.fetchval("SELECT 1")
        return result == 1
    except Exception as e: log.error("Database connection check failed", error=str(e)); return False

# --- Document Operations ---
# LLM_COMMENT: create_document remains largely the same
async def create_document(company_id: uuid.UUID, file_name: str, file_type: str, metadata: Dict[str, Any]) -> uuid.UUID:
    pool = await get_db_pool(); doc_id = uuid.uuid4()
    # LLM_COMMENT: Include file_path as empty string initially to satisfy NOT NULL if applicable
    # LLM_COMMENT: Explicitly setting chunk_count to 0 or NULL on creation
    query = """
    INSERT INTO documents (id, company_id, file_name, file_type, file_path, metadata, status, chunk_count, uploaded_at, updated_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC') RETURNING id;
    """
    insert_log = log.bind(company_id=str(company_id), filename=file_name, content_type=file_type, proposed_doc_id=str(doc_id))
    try:
        # LLM_COMMENT: Pass metadata as JSON string, initial status UPLOADED, chunk_count 0
        metadata_json = json.dumps(metadata)
        async with pool.acquire() as connection:
            result_id = await connection.fetchval(query, doc_id, company_id, file_name, file_type, '', metadata_json, DocumentStatus.UPLOADED.value, 0)
        if result_id and result_id == doc_id:
            insert_log.info("Document record created", document_id=str(doc_id)); return result_id
        else:
            insert_log.error("Failed to create document record, ID mismatch or no ID returned", returned_id=result_id)
            raise RuntimeError(f"Failed create document, return mismatch ({result_id})")
    except asyncpg.exceptions.UniqueViolationError as e:
        insert_log.error("Unique constraint violation on document creation", error=str(e), constraint=e.constraint_name)
        raise ValueError(f"Document creation failed: unique constraint ({e.constraint_name})") from e
    except Exception as e:
        insert_log.error("Failed to create document record", error=str(e), exc_info=True); raise

# LLM_COMMENT: Refined update_document_status to handle error messages better
async def update_document_status(
    document_id: uuid.UUID,
    status: DocumentStatus,
    file_path: Optional[str] = None,
    chunk_count: Optional[int] = None,
    error_message: Optional[str] = None
) -> bool:
    pool = await get_db_pool();
    update_log = log.bind(document_id=str(document_id), new_status=status.value)
    fields_to_set: List[str] = []
    params: List[Any] = [document_id]
    param_index = 2 # Parameter index starts at $2 ($1 is document_id)

    # Always update status and updated_at
    fields_to_set.append(f"status = ${param_index}"); params.append(status.value); param_index += 1
    fields_to_set.append(f"updated_at = NOW() AT TIME ZONE 'UTC'")

    # Conditionally update other fields
    if file_path is not None: fields_to_set.append(f"file_path = ${param_index}"); params.append(file_path); param_index += 1
    if chunk_count is not None: fields_to_set.append(f"chunk_count = ${param_index}"); params.append(chunk_count); param_index += 1

    # Handle error message: set if status is ERROR, clear otherwise
    # LLM_COMMENT: Ensure error_message column exists in your 'documents' table
    # LLM_COMMENT: Add error_message handling - set or clear based on status
    if status == DocumentStatus.ERROR:
        # Truncate long error messages if necessary
        truncated_error = (error_message[:497] + '...') if error_message and len(error_message) > 500 else error_message
        fields_to_set.append(f"error_message = ${param_index}"); params.append(truncated_error); param_index += 1
        update_log = update_log.bind(error=truncated_error) # Log the error being set
    else:
        # Clear error message if status is not ERROR
        fields_to_set.append(f"error_message = NULL");
        # No parameter needed for NULL

    query = f"UPDATE documents SET {', '.join(fields_to_set)} WHERE id = $1;";
    update_log.debug("Executing status update", query=query, params_count=len(params))
    try:
        async with pool.acquire() as connection:
             result_str = await connection.execute(query, *params)
        # Parse result string like 'UPDATE 1'
        affected_rows = 0
        if isinstance(result_str, str) and result_str.startswith("UPDATE "):
            try:
                 affected_rows = int(result_str.split(" ")[1])
            except (IndexError, ValueError):
                 update_log.warning("Could not parse affected rows from UPDATE result", result=result_str)

        if affected_rows > 0:
             update_log.info("Document status updated", rows=affected_rows)
             return True
        else:
             update_log.warning("Document status update affected 0 rows (document might not exist?)")
             return False
    except Exception as e:
        update_log.error("Failed to update document status", error=str(e), exc_info=True); raise

# LLM_COMMENT: get_document_status now selects error_message
async def get_document_status(document_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    pool = await get_db_pool(); get_log = log.bind(document_id=str(document_id))
    # LLM_COMMENT: Select error_message column
    query = """
    SELECT id, status, file_name, file_type, chunk_count, file_path, updated_at, company_id, error_message
    FROM documents WHERE id = $1;
    """
    try:
        async with pool.acquire() as connection: record = await connection.fetchrow(query, document_id)
        if record: get_log.debug("Document status retrieved"); return dict(record)
        else: get_log.warning("Document status requested for non-existent ID"); return None
    except Exception as e: get_log.error("Failed to get status", error=str(e), exc_info=True); raise

# LLM_COMMENT: list_documents_by_company now selects error_message
async def list_documents_by_company(company_id: uuid.UUID, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    pool = await get_db_pool(); list_log = log.bind(company_id=str(company_id), limit=limit, offset=offset)
    # LLM_COMMENT: Select error_message column for the list view as well
    query = """
    SELECT id, status, file_name, file_type, chunk_count, file_path, updated_at, error_message
    FROM documents
    WHERE company_id = $1 ORDER BY updated_at DESC LIMIT $2 OFFSET $3;
    """
    try:
        async with pool.acquire() as connection: records = await connection.fetch(query, company_id, limit, offset)
        result_list = [dict(record) for record in records]; list_log.info(f"Retrieved {len(result_list)} docs for company")
        return result_list
    except Exception as e: list_log.error("Failed to list docs", error=str(e), exc_info=True); raise

# LLM_COMMENT: delete_document remains the same logic (only deletes PG record)
# LLM_COMMENT: Deletion of MinIO/Milvus data happens in the endpoint logic
async def delete_document(document_id: uuid.UUID) -> bool:
    """
    Elimina un documento de la tabla `documents` por su ID.
    Devuelve True si se eliminó al menos una fila, False si no existía.
    """
    pool = await get_db_pool()
    delete_log = log.bind(document_id=str(document_id))
    query = "DELETE FROM documents WHERE id = $1;"
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(query, document_id)
        # result is 'DELETE X'
        deleted = isinstance(result, str) and result.startswith("DELETE") and int(result.split(" ")[1]) > 0
        if deleted:
            delete_log.info("Document record deleted from PostgreSQL.", result=result)
        else:
            delete_log.warning("Attempted to delete non-existent document record.", result=result)
        return deleted
    except Exception as e:
        delete_log.error("Error deleting document record from PostgreSQL", error=str(e), exc_info=True)
        raise # Re-throw DB errors

# --- Funciones de Chat (Sin cambios, mantenidas por consistencia si se comparten) ---
# LLM_COMMENT: Keep Chat functions for potential shared DB client usage, no functional changes needed here.
async def create_chat(user_id: uuid.UUID, company_id: uuid.UUID, title: Optional[str] = None) -> uuid.UUID:
    pool = await get_db_pool()
    chat_id = uuid.uuid4()
    query = """INSERT INTO chats (id, user_id, company_id, title, created_at, updated_at) VALUES ($1, $2, $3, $4, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC') RETURNING id;"""
    try:
        async with pool.acquire() as conn:
            result = await conn.fetchval(query, chat_id, user_id, company_id, title or f"Chat {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}")
            return result
    except Exception as e:
        log.error("Failed create_chat (ingest context)", error=str(e))
        raise

async def get_user_chats(user_id: uuid.UUID, company_id: uuid.UUID, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    pool = await get_db_pool()
    query = """SELECT id, title, updated_at FROM chats WHERE user_id = $1 AND company_id = $2 ORDER BY updated_at DESC LIMIT $3 OFFSET $4;"""
    try:
        async with pool.acquire() as conn: rows = await conn.fetch(query, user_id, company_id, limit, offset); return [dict(row) for row in rows]
    except Exception as e: log.error("Failed get_user_chats (ingest context)", error=str(e)); raise

async def check_chat_ownership(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    pool = await get_db_pool()
    query = "SELECT EXISTS (SELECT 1 FROM chats WHERE id = $1 AND user_id = $2 AND company_id = $3);"
    try:
        async with pool.acquire() as conn: exists = await conn.fetchval(query, chat_id, user_id, company_id); return exists is True
    except Exception as e: log.error("Failed check_chat_ownership (ingest context)", error=str(e)); return False

async def get_chat_messages(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    pool = await get_db_pool(); owner = await check_chat_ownership(chat_id, user_id, company_id)
    if not owner: return []
    messages_query = """SELECT id, role, content, sources, created_at FROM messages WHERE chat_id = $1 ORDER BY created_at ASC LIMIT $2 OFFSET $3;"""
    try:
        async with pool.acquire() as conn: message_rows = await conn.fetch(messages_query, chat_id, limit, offset); return [dict(row) for row in message_rows]
    except Exception as e: log.error("Failed get_chat_messages (ingest context)", error=str(e)); raise

async def save_message(chat_id: uuid.UUID, role: str, content: str, sources: Optional[List[Dict[str, Any]]] = None) -> uuid.UUID:
    pool = await get_db_pool(); message_id = uuid.uuid4()
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                update_chat_query = "UPDATE chats SET updated_at = NOW() AT TIME ZONE 'UTC' WHERE id = $1 RETURNING id;"; chat_updated = await conn.fetchval(update_chat_query, chat_id)
                if not chat_updated: raise ValueError(f"Chat {chat_id} not found (ingest context).")
                insert_message_query = """INSERT INTO messages (id, chat_id, role, content, sources, created_at) VALUES ($1, $2, $3, $4, $5, NOW() AT TIME ZONE 'UTC') RETURNING id;"""
                result = await conn.fetchval(insert_message_query, message_id, chat_id, role, content, json.dumps(sources or [])); return result
            except Exception as e: log.error("Failed save_message (ingest context)", error=str(e)); raise

async def delete_chat(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    pool = await get_db_pool()
    query = "DELETE FROM chats WHERE id = $1 AND user_id = $2 AND company_id = $3 RETURNING id;"; delete_log = log.bind(chat_id=str(chat_id), user_id=str(user_id))
    try:
        async with pool.acquire() as conn: deleted_id = await conn.fetchval(query, chat_id, user_id, company_id); return deleted_id is not None
    except Exception as e: delete_log.error("Failed to delete chat (ingest context)", error=str(e)); raise