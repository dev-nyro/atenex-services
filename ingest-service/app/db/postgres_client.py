from google.cloud.sql.connector import Connector, IPTypes
from sqlalchemy.pool import NullPool
_connector: Optional[Connector] = None
# ingest-service/app/db/postgres_client.py
import uuid
from typing import Any, Optional, Dict, List, Tuple
import asyncpg
import structlog
import json
from datetime import datetime, timezone

# --- Synchronous Imports (Added for Step 4.3) ---
from sqlalchemy import create_engine, text, Engine, Connection
from sqlalchemy.exc import SQLAlchemyError
# --- End Synchronous Imports ---

from app.core.config import settings
from app.models.domain import DocumentStatus

log = structlog.get_logger(__name__)

# --- Async Pool (for API) ---
_pool: Optional[asyncpg.Pool] = None

# --- Sync Engine (for Worker) ---
_sync_engine: Optional[Engine] = None
_sync_engine_dsn: Optional[str] = None


# --- Async Pool Management (No changes) ---
# LLM_FLAG: SENSITIVE_CODE_BLOCK_START - DB Async Pool Management
async def get_db_pool() -> asyncpg.Pool:
    global _pool, _connector
    if _pool is None or _pool._closed:
        log.info("Creating PostgreSQL connection pool using Cloud SQL Python Connector...", instance=settings.POSTGRES_INSTANCE_CONNECTION_NAME)
        if _connector is None:
            _connector = Connector()
        def getconn():
            return _connector.connect(
                settings.POSTGRES_INSTANCE_CONNECTION_NAME,
                "asyncpg",
                user=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD.get_secret_value(),
                db=settings.POSTGRES_DB,
                ip_type=IPTypes.PRIVATE if settings.POSTGRES_IP_TYPE.upper() == "PRIVATE" else IPTypes.PUBLIC
            )
        def _json_encoder(value):
            return json.dumps(value)
        def _json_decoder(value):
            return json.loads(value)
        async def init_connection(conn):
            await conn.set_type_codec(
                'jsonb',
                encoder=_json_encoder,
                decoder=_json_decoder,
                schema='pg_catalog',
                format='text'
            )
            await conn.set_type_codec(
                'json',
                encoder=_json_encoder,
                decoder=_json_decoder,
                schema='pg_catalog',
                format='text'
            )
        _pool = await asyncpg.create_pool(
            min_size=2,
            max_size=10,
            timeout=30.0,
            command_timeout=60.0,
            init=init_connection,
            statement_cache_size=0,
            connection_class=None,
            connection_factory=getconn
        )
        log.info("PostgreSQL async connection pool created successfully (Cloud SQL Connector).")
    return _pool

async def close_db_pool():
    global _pool
    if _pool and not _pool._closed:
        log.info("Closing PostgreSQL async connection pool...")
        await _pool.close()
        _pool = None
        log.info("PostgreSQL async connection pool closed.")
    elif _pool and _pool._closed:
        log.warning("Attempted to close an already closed PostgreSQL async pool.")
        _pool = None
    else:
        log.info("No active PostgreSQL async connection pool to close.")

async def check_db_connection() -> bool:
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                result = await conn.fetchval("SELECT 1")
        return result == 1
    except Exception as e:
        log.error("Database async connection check failed", error=str(e))
        return False
# LLM_FLAG: SENSITIVE_CODE_BLOCK_END - DB Async Pool Management


# --- Synchronous Engine Management (Added for Step 4.3) ---
# LLM_FLAG: SENSITIVE_CODE_BLOCK_START - DB Sync Engine Management
def get_sync_engine() -> Engine:
    """
    Creates and returns a SQLAlchemy synchronous engine instance.
    Caches the engine globally per process.
    Requires `sqlalchemy` and `psycopg2-binary` to be installed.
    """
    global _sync_engine, _connector
    sync_log = log.bind(component="SyncEngine")
    if _connector is None:
        _connector = Connector()
    def getconn():
        return _connector.connect(
            settings.POSTGRES_INSTANCE_CONNECTION_NAME,
            "pg8000",
            user=settings.POSTGRES_USER,
            password=settings.POSTGRES_PASSWORD.get_secret_value(),
            db=settings.POSTGRES_DB,
            ip_type=IPTypes.PRIVATE if settings.POSTGRES_IP_TYPE.upper() == "PRIVATE" else IPTypes.PUBLIC
        )
    if _sync_engine is None:
        sync_log.info("Creating SQLAlchemy synchronous engine using Cloud SQL Connector...")
        try:
            _sync_engine = create_engine(
                "postgresql+pg8000://",
                creator=getconn,
                poolclass=NullPool
            )
            with _sync_engine.connect() as conn_test:
                conn_test.execute(text("SELECT 1"))
            sync_log.info("SQLAlchemy synchronous engine created and tested successfully (Cloud SQL Connector).")
        except ImportError as ie:
            sync_log.critical("SQLAlchemy or pg8000 not installed! Cannot create sync engine.", error=str(ie))
            raise RuntimeError("Missing SQLAlchemy/pg8000 dependency") from ie
        except SQLAlchemyError as sa_err:
            sync_log.critical("Failed to create or connect SQLAlchemy synchronous engine", error=str(sa_err), exc_info=True)
            _sync_engine = None
            raise ConnectionError(f"Failed to connect sync engine: {sa_err}") from sa_err
        except Exception as e:
            sync_log.critical("Unexpected error creating SQLAlchemy synchronous engine", error=str(e), exc_info=True)
            _sync_engine = None
            raise RuntimeError(f"Unexpected error creating sync engine: {e}") from e
    return _sync_engine

def dispose_sync_engine():
    """Disposes of the synchronous engine pool."""
    global _sync_engine, _connector
    sync_log = log.bind(component="SyncEngine")
    if _sync_engine:
        sync_log.info("Disposing SQLAlchemy synchronous engine pool...")
        _sync_engine.dispose()
        _sync_engine = None
        sync_log.info("SQLAlchemy synchronous engine pool disposed.")
    if _connector:
        _connector.close()
        _connector = None
        sync_log.info("Cloud SQL connector closed.")
# LLM_FLAG: SENSITIVE_CODE_BLOCK_END - DB Sync Engine Management


# --- Synchronous Document Operations (Added for Step 4.3) ---
# LLM_FLAG: FUNCTIONAL_CODE - Synchronous DB update function
def set_status_sync(
    engine: Engine,
    document_id: uuid.UUID,
    status: DocumentStatus,
    chunk_count: Optional[int] = None,
    error_message: Optional[str] = None
) -> bool:
    """
    Synchronously updates the status, chunk_count, and/or error_message of a document.
    Uses SQLAlchemy Core API with the provided synchronous engine.
    """
    update_log = log.bind(document_id=str(document_id), new_status=status.value, component="SyncDBUpdate")
    update_log.debug("Attempting synchronous DB status update.")

    params: Dict[str, Any] = {
        "doc_id": document_id,
        "status": status.value,
        "updated_at": datetime.now(timezone.utc)
    }
    set_clauses: List[str] = ["status = :status", "updated_at = :updated_at"]

    if chunk_count is not None:
        set_clauses.append("chunk_count = :chunk_count")
        params["chunk_count"] = chunk_count

    # Explicitly set error message based on status
    if status == DocumentStatus.ERROR:
        set_clauses.append("error_message = :error_message")
        params["error_message"] = error_message
    else:
        # Clear error message if status is not ERROR
        set_clauses.append("error_message = NULL")
        # params["error_message"] = None # Not needed if using NULL directly

    set_clause_str = ", ".join(set_clauses)
    query = text(f"UPDATE documents SET {set_clause_str} WHERE id = :doc_id")

    try:
        with engine.connect() as connection:
            with connection.begin(): # Start transaction
                result = connection.execute(query, params)

            if result.rowcount == 0:
                update_log.warning("Attempted to update status for non-existent document_id (sync).")
                return False
            elif result.rowcount == 1:
                update_log.info("Document status updated successfully in PostgreSQL (sync).", updated_fields=set_clauses)
                return True
            else:
                 # Should not happen with WHERE id = :doc_id
                 update_log.error("Unexpected number of rows updated (sync).", row_count=result.rowcount)
                 return False # Or raise an error?

    except SQLAlchemyError as e:
        update_log.error("SQLAlchemyError during synchronous status update", error=str(e), query=str(query), params=params, exc_info=True)
        # Depending on the error, this might indicate a connection issue or SQL error.
        # Re-raise as a standard Exception to be caught by Celery's retry logic if applicable.
        raise Exception(f"Sync DB update failed: {e}") from e
    except Exception as e:
        update_log.error("Unexpected error during synchronous status update", error=str(e), query=str(query), params=params, exc_info=True)
        raise Exception(f"Unexpected sync DB update error: {e}") from e


# --- Async Document Operations (No changes needed for these) ---

# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH create_document_record DB logic lightly
async def create_document_record(
    conn: asyncpg.Connection,
    doc_id: uuid.UUID,
    company_id: uuid.UUID,
    # user_id: uuid.UUID,  # REMOVED Parameter
    filename: str,
    file_type: str,
    file_path: str,
    status: DocumentStatus = DocumentStatus.PENDING,
    metadata: Optional[Dict[str, Any]] = None
) -> None:
    """Crea un registro inicial para un documento en la base de datos."""
    query = """
    INSERT INTO documents (
        id, company_id, file_name, file_type, file_path,
        metadata, status, chunk_count, error_message,
        uploaded_at, updated_at
    ) VALUES (
        $1, $2, $3, $4, $5,
        $6, $7, $8, NULL,
        NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC'
    );
    """
    metadata_db = json.dumps(metadata) if metadata else None
    params = [ doc_id, company_id, filename, file_type, file_path, metadata_db, status.value, 0 ]
    insert_log = log.bind(company_id=str(company_id), filename=filename, doc_id=str(doc_id))
    try:
        await conn.execute(query, *params)
        insert_log.info("Document record created in PostgreSQL (async)")
    except asyncpg.exceptions.UndefinedColumnError as col_err:
         insert_log.critical(f"FATAL DB SCHEMA ERROR: Column missing in 'documents' table.", error=str(col_err), table_schema_expected="id, company_id, file_name, file_type, file_path, metadata, status, chunk_count, error_message, uploaded_at, updated_at")
         raise RuntimeError(f"Database schema error: {col_err}") from col_err
    except Exception as e:
        insert_log.error("Failed to create document record (async)", error=str(e), exc_info=True)
        raise

# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH find_document_by_name_and_company DB logic lightly
async def find_document_by_name_and_company(conn: asyncpg.Connection, filename: str, company_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """Busca un documento por nombre y compañía."""
    query = "SELECT id, status FROM documents WHERE file_name = $1 AND company_id = $2;"
    find_log = log.bind(company_id=str(company_id), filename=filename)
    try:
        record = await conn.fetchrow(query, filename, company_id)
        if record: find_log.debug("Found existing document record (async)"); return dict(record)
        else: find_log.debug("No existing document record found (async)"); return None
    except Exception as e: find_log.error("Failed to find document by name and company (async)", error=str(e), exc_info=True); raise

# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH update_document_status DB logic lightly
async def update_document_status(
    document_id: uuid.UUID,
    status: DocumentStatus,
    pool: Optional[asyncpg.Pool] = None,
    conn: Optional[asyncpg.Connection] = None,
    chunk_count: Optional[int] = None,
    error_message: Optional[str] = None
) -> bool:
    """Actualiza el estado, chunk_count y/o error_message de un documento (Async)."""
    if not pool and not conn:
        pool = await get_db_pool()

    params: List[Any] = [document_id]
    fields: List[str] = ["status = $2", "updated_at = NOW() AT TIME ZONE 'UTC'"]
    params.append(status.value)
    param_index = 3

    if chunk_count is not None:
        fields.append(f"chunk_count = ${param_index}")
        params.append(chunk_count)
        param_index += 1

    if status == DocumentStatus.ERROR:
        if error_message is not None:
            fields.append(f"error_message = ${param_index}")
            params.append(error_message)
            param_index += 1
    else:
        fields.append("error_message = NULL")

    set_clause = ", ".join(fields)
    query = f"UPDATE documents SET {set_clause} WHERE id = $1;"

    update_log = log.bind(document_id=str(document_id), new_status=status.value)

    try:
        if conn:
            result = await conn.execute(query, *params)
        else:
            async with pool.acquire() as connection:
                result = await connection.execute(query, *params)

        if result == 'UPDATE 0':
            update_log.warning("Attempted to update status for non-existent document_id (async)")
            return False

        update_log.info("Document status updated in PostgreSQL (async)")
        return True

    except Exception as e:
        update_log.error("Failed to update document status (async)", error=str(e), exc_info=True)
        raise

# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH get_document_by_id DB logic lightly
async def get_document_by_id(conn: asyncpg.Connection, doc_id: uuid.UUID, company_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """Obtiene un documento por ID y verifica la compañía (Async)."""
    query = """SELECT id, company_id, file_name, file_type, file_path, metadata, status, chunk_count, error_message, uploaded_at, updated_at FROM documents WHERE id = $1 AND company_id = $2;"""
    get_log = log.bind(document_id=str(doc_id), company_id=str(company_id))
    try:
        record = await conn.fetchrow(query, doc_id, company_id)
        if not record: get_log.warning("Document not found or company mismatch (async)"); return None
        return dict(record)
    except Exception as e: get_log.error("Failed to get document by ID (async)", error=str(e), exc_info=True); raise

# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH list_documents_paginated DB logic lightly
async def list_documents_paginated(conn: asyncpg.Connection, company_id: uuid.UUID, limit: int, offset: int) -> Tuple[List[Dict[str, Any]], int]:
    """Lista documentos paginados para una compañía y devuelve el conteo total (Async)."""
    query = """SELECT id, company_id, file_name, file_type, file_path, metadata, status, chunk_count, error_message, uploaded_at, updated_at, COUNT(*) OVER() AS total_count FROM documents WHERE company_id = $1 ORDER BY updated_at DESC LIMIT $2 OFFSET $3;"""
    list_log = log.bind(company_id=str(company_id), limit=limit, offset=offset)
    try:
        rows = await conn.fetch(query, company_id, limit, offset); total = 0; results = []
        if rows: total = rows[0]['total_count']; results = [dict(r) for r in rows]; [r.pop('total_count', None) for r in results]
        list_log.debug("Fetched paginated documents (async)", count=len(results), total=total)
        return results, total
    except Exception as e: list_log.error("Failed to list paginated documents (async)", error=str(e), exc_info=True); raise

# LLM_FLAG: FUNCTIONAL_CODE - DO NOT TOUCH delete_document DB logic lightly
async def delete_document(conn: asyncpg.Connection, doc_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    """Elimina un documento verificando la compañía (Async)."""
    query = "DELETE FROM documents WHERE id = $1 AND company_id = $2 RETURNING id;"
    delete_log = log.bind(document_id=str(doc_id), company_id=str(company_id))
    try:
        deleted_id = await conn.fetchval(query, doc_id, company_id)
        if deleted_id: delete_log.info("Document deleted from PostgreSQL (async)", deleted_id=str(deleted_id)); return True
        else: delete_log.warning("Document not found or company mismatch during delete attempt (async)."); return False
    except Exception as e: delete_log.error("Error deleting document record (async)", error=str(e), exc_info=True); raise

# --- Chat Functions (Placeholder - Likely Unused, No Changes Needed) ---
# LLM_FLAG: SENSITIVE_CODE_BLOCK_START - Chat Functions (Likely Unused)
async def create_chat(user_id: uuid.UUID, company_id: uuid.UUID, title: Optional[str] = None) -> uuid.UUID:
    pool = await get_db_pool()
    chat_id = uuid.uuid4()
    query = """
    INSERT INTO chats (id, user_id, company_id, title, created_at, updated_at)
    VALUES ($1, $2, $3, $4, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC')
    RETURNING id;
    """
    try:
        async with pool.acquire() as conn:
            result = await conn.fetchval(query, chat_id, user_id, company_id, title or f"Chat {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}")
            return result
    except Exception as e:
        log.error("Failed create_chat (ingest context - likely unused)", error=str(e))
        raise

async def get_user_chats(user_id: uuid.UUID, company_id: uuid.UUID, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    pool = await get_db_pool()
    query = """
    SELECT id, title, updated_at
    FROM chats
    WHERE user_id = $1 AND company_id = $2
    ORDER BY updated_at DESC
    LIMIT $3 OFFSET $4;
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, user_id, company_id, limit, offset)
            return [dict(row) for row in rows]
    except Exception as e:
        log.error("Failed get_user_chats (ingest context - likely unused)", error=str(e))
        raise

async def check_chat_ownership(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    pool = await get_db_pool()
    query = "SELECT EXISTS (SELECT 1 FROM chats WHERE id = $1 AND user_id = $2 AND company_id = $3);"
    try:
        async with pool.acquire() as conn:
            exists = await conn.fetchval(query, chat_id, user_id, company_id)
            return exists is True
    except Exception as e:
        log.error("Failed check_chat_ownership (ingest context - likely unused)", error=str(e))
        return False

async def get_chat_messages(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    pool = await get_db_pool()
    owner = await check_chat_ownership(chat_id, user_id, company_id)
    if not owner:
        return []
    messages_query = """
    SELECT id, role, content, sources, created_at
    FROM messages
    WHERE chat_id = $1
    ORDER BY created_at ASC
    LIMIT $2 OFFSET $3;
    """
    try:
        async with pool.acquire() as conn:
            message_rows = await conn.fetch(messages_query, chat_id, limit, offset)
            return [dict(row) for row in message_rows]
    except Exception as e:
        log.error("Failed get_chat_messages (ingest context - likely unused)", error=str(e))
        raise

async def save_message(chat_id: uuid.UUID, role: str, content: str, sources: Optional[List[Dict[str, Any]]] = None) -> uuid.UUID:
    pool = await get_db_pool()
    message_id = uuid.uuid4()
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                update_chat_query = "UPDATE chats SET updated_at = NOW() AT TIME ZONE 'UTC' WHERE id = $1 RETURNING id;"
                chat_updated = await conn.fetchval(update_chat_query, chat_id)
                if not chat_updated:
                    raise ValueError(f"Chat {chat_id} not found (ingest context - likely unused).")
                insert_message_query = """
                INSERT INTO messages (id, chat_id, role, content, sources, created_at)
                VALUES ($1, $2, $3, $4, $5, NOW() AT TIME ZONE 'UTC')
                RETURNING id;
                """
                result = await conn.fetchval(insert_message_query, message_id, chat_id, role, content, json.dumps(sources or []))
                return result
            except Exception as e:
                log.error("Failed save_message (ingest context - likely unused)", error=str(e))
                raise

async def delete_chat(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    pool = await get_db_pool()
    query = "DELETE FROM chats WHERE id = $1 AND user_id = $2 AND company_id = $3 RETURNING id;"
    delete_log = log.bind(chat_id=str(chat_id), user_id=str(user_id))
    try:
        async with pool.acquire() as conn:
            deleted_id = await conn.fetchval(query, chat_id, user_id, company_id)
            return deleted_id is not None
    except Exception as e:
        delete_log.error("Failed to delete chat (ingest context - likely unused)", error=str(e))
        raise
# LLM_FLAG: SENSITIVE_CODE_BLOCK_END - Chat Functions