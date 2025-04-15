# query-service/app/db/postgres_client.py
import uuid
from typing import Any, Optional, Dict, List, Tuple
import asyncpg
import structlog
import json
from datetime import datetime, timezone

from app.core.config import settings
from app.api.v1 import schemas # Para type hints

log = structlog.get_logger(__name__)

_pool: Optional[asyncpg.Pool] = None

# --- Pool Management (sin cambios) ---
async def get_db_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None or _pool._closed:
        log.info("Creating PostgreSQL connection pool...",
                 host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT,
                 user=settings.POSTGRES_USER, db=settings.POSTGRES_DB)
        try:
            def _json_encoder(value): return json.dumps(value)
            def _json_decoder(value): return json.loads(value)
            async def init_connection(conn):
                await conn.set_type_codec('jsonb', encoder=_json_encoder, decoder=_json_decoder, schema='pg_catalog')
                await conn.set_type_codec('json', encoder=_json_encoder, decoder=_json_decoder, schema='pg_catalog')

            _pool = await asyncpg.create_pool(
                user=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD.get_secret_value(),
                database=settings.POSTGRES_DB,
                host=settings.POSTGRES_SERVER,
                port=settings.POSTGRES_PORT,
                min_size=2, max_size=10, timeout=30.0, command_timeout=60.0,
                init=init_connection, statement_cache_size=0
            )
            log.info("PostgreSQL connection pool created successfully.")
        except (asyncpg.exceptions.InvalidPasswordError, OSError, ConnectionRefusedError) as conn_err:
            log.critical("CRITICAL: Failed to connect to PostgreSQL", error=str(conn_err), exc_info=True)
            _pool = None
            raise ConnectionError(f"Failed to connect to PostgreSQL: {conn_err}") from conn_err
        except Exception as e:
            log.critical("CRITICAL: Failed to create PostgreSQL connection pool", error=str(e), exc_info=True)
            _pool = None
            raise RuntimeError(f"Failed to create PostgreSQL pool: {e}") from e
    return _pool

async def close_db_pool():
    global _pool
    if _pool and not _pool._closed:
        log.info("Closing PostgreSQL connection pool..."); await _pool.close(); _pool = None; log.info("PostgreSQL connection pool closed.")
    elif _pool and _pool._closed: log.warning("Attempted to close an already closed PostgreSQL pool."); _pool = None
    else: log.info("No active PostgreSQL connection pool to close.")


async def check_db_connection() -> bool:
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            async with conn.transaction(): result = await conn.fetchval("SELECT 1")
        return result == 1
    except Exception as e: log.error("Database connection check failed", error=str(e)); return False

# --- Query Logging (sin cambios) ---
async def log_query_interaction(
    user_id: Optional[uuid.UUID],
    company_id: uuid.UUID,
    query: str,
    answer: str,
    retrieved_documents_data: List[Dict[str, Any]],
    metadata: Optional[Dict[str, Any]] = None,
    chat_id: Optional[uuid.UUID] = None,
) -> uuid.UUID:
    """Registra una interacción de consulta en query_logs."""
    pool = await get_db_pool()
    log_id = uuid.uuid4()
    log_entry = log.bind(log_id=str(log_id), company_id=str(company_id), chat_id=str(chat_id) if chat_id else "None")

    query_sql = """
    INSERT INTO query_logs (
        id, user_id, company_id, query, response,
        metadata, chat_id, created_at
    ) VALUES (
        $1, $2, $3, $4, $5, $6, $7, NOW() AT TIME ZONE 'UTC'
    ) RETURNING id;
    """
    log_metadata = metadata or {}
    log_metadata["retrieved_summary"] = [{"id": d.get("id"), "score": d.get("score"), "file_name": d.get("file_name")} for d in retrieved_documents_data]

    try:
        async with pool.acquire() as connection:
            result = await connection.fetchval(
                query_sql,
                log_id, user_id, company_id, query, answer,
                json.dumps(log_metadata),
                chat_id
            )
        if not result or result != log_id:
            log_entry.error("Failed to create query log entry, ID mismatch or no ID returned", returned_id=result)
            raise RuntimeError("Failed to create query log entry")
        log_entry.info("Query interaction logged successfully")
        return log_id
    except Exception as e:
        log_entry.error("Failed to log query interaction", error=str(e), exc_info=True)
        raise RuntimeError(f"Failed to log query interaction: {e}") from e


# --- Funciones para gestión de chats (Renombradas y columna sources corregida) ---

async def create_chat(user_id: uuid.UUID, company_id: uuid.UUID, title: Optional[str] = None) -> uuid.UUID:
    """Crea un nuevo chat."""
    pool = await get_db_pool()
    chat_id = uuid.uuid4()
    query = """
    INSERT INTO chats (id, user_id, company_id, title, created_at, updated_at)
    VALUES ($1, $2, $3, $4, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC') RETURNING id;
    """
    try:
        async with pool.acquire() as conn:
            result = await conn.fetchval(query, chat_id, user_id, company_id, title or f"Chat {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}")
        if result and result == chat_id:
            log.info("Chat created successfully", chat_id=str(chat_id), user_id=str(user_id), company_id=str(company_id))
            return chat_id
        else: raise RuntimeError("Failed to create chat, no ID returned")
    except Exception as e: log.error("Failed to create chat", error=str(e), exc_info=True); raise

# *** FUNCIÓN RENOMBRADA ***
async def get_user_chats(user_id: uuid.UUID, company_id: uuid.UUID, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """Obtiene la lista de chats para un usuario/compañía (sumario)."""
    pool = await get_db_pool()
    query = """
    SELECT id, title, updated_at FROM chats
    WHERE user_id = $1 AND company_id = $2
    ORDER BY updated_at DESC LIMIT $3 OFFSET $4;
    """
    try:
        async with pool.acquire() as conn: rows = await conn.fetch(query, user_id, company_id, limit, offset)
        chats = [dict(row) for row in rows]
        log.info(f"Retrieved {len(chats)} chat summaries for user", user_id=str(user_id), company_id=str(company_id))
        return chats
    except Exception as e: log.error("Failed to get user chats", error=str(e), exc_info=True); raise

async def check_chat_ownership(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    """Verifica si un chat existe y pertenece al usuario/compañía."""
    pool = await get_db_pool()
    query = "SELECT EXISTS (SELECT 1 FROM chats WHERE id = $1 AND user_id = $2 AND company_id = $3);"
    try:
        async with pool.acquire() as conn: exists = await conn.fetchval(query, chat_id, user_id, company_id)
        return exists is True
    except Exception as e: log.error("Failed to check chat ownership", chat_id=str(chat_id), error=str(e), exc_info=True); return False # Asumir no propietario en caso de error

# *** FUNCIÓN RENOMBRADA y columna SOURCES CORREGIDA ***
async def get_chat_messages(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    """Obtiene mensajes de un chat, verificando propiedad."""
    pool = await get_db_pool()
    owner = await check_chat_ownership(chat_id, user_id, company_id)
    if not owner:
        log.warning("Attempt to get messages for chat not owned or non-existent", chat_id=str(chat_id), user_id=str(user_id), company_id=str(company_id))
        return []

    # Usar columna 'sources'
    messages_query = """
    SELECT id, role, content, sources, created_at FROM messages
    WHERE chat_id = $1 ORDER BY created_at ASC LIMIT $2 OFFSET $3;
    """
    try:
        async with pool.acquire() as conn: message_rows = await conn.fetch(messages_query, chat_id, limit, offset)
        messages = [dict(row) for row in message_rows]
        log.info(f"Retrieved {len(messages)} messages for chat", chat_id=str(chat_id))
        return messages
    except Exception as e: log.error("Failed to get chat messages", error=str(e), exc_info=True); raise

# *** FUNCIÓN RENOMBRADA y columna SOURCES CORREGIDA ***
async def save_message(chat_id: uuid.UUID, role: str, content: str, sources: Optional[List[Dict[str, Any]]] = None) -> uuid.UUID:
    """Guarda un mensaje en un chat y actualiza el timestamp del chat."""
    pool = await get_db_pool()
    message_id = uuid.uuid4()
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                update_chat_query = "UPDATE chats SET updated_at = NOW() AT TIME ZONE 'UTC' WHERE id = $1 RETURNING id;"
                chat_updated = await conn.fetchval(update_chat_query, chat_id)
                if not chat_updated:
                     log.error("Failed to update chat timestamp, chat might not exist", chat_id=str(chat_id))
                     raise ValueError(f"Chat with ID {chat_id} not found for saving message.")

                # Usar columna 'sources'
                insert_message_query = """
                INSERT INTO messages (id, chat_id, role, content, sources, created_at)
                VALUES ($1, $2, $3, $4, $5, NOW() AT TIME ZONE 'UTC') RETURNING id;
                """
                result = await conn.fetchval(insert_message_query, message_id, chat_id, role, content, json.dumps(sources or [])) # Usar json.dumps

                if result and result == message_id:
                    log.info("Message saved successfully", message_id=str(message_id), chat_id=str(chat_id), role=role)
                    return message_id
                else:
                    log.error("Failed to save message, unexpected or no ID returned", chat_id=str(chat_id), returned_id=result)
                    raise RuntimeError("Failed to save message, ID mismatch or not returned.")
            except Exception as e:
                log.error("Failed to save message within transaction", error=str(e), chat_id=str(chat_id), exc_info=True)
                raise

async def delete_chat(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    """Elimina un chat si pertenece al usuario/compañía."""
    pool = await get_db_pool()
    query = "DELETE FROM chats WHERE id = $1 AND user_id = $2 AND company_id = $3 RETURNING id;"
    delete_log = log.bind(chat_id=str(chat_id), user_id=str(user_id), company_id=str(company_id))
    try:
        async with pool.acquire() as conn: deleted_id = await conn.fetchval(query, chat_id, user_id, company_id)
        success = deleted_id is not None
        if success: delete_log.info("Chat deleted successfully")
        else: delete_log.warning("Chat not found or does not belong to user, deletion skipped")
        return success
    except Exception as e: delete_log.error("Failed to delete chat", error=str(e), exc_info=True); raise