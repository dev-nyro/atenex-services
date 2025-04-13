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

# --- Pool Management ---
async def get_db_pool() -> asyncpg.Pool:
    """Obtiene o crea el pool de conexiones a PostgreSQL."""
    global _pool
    if _pool is None or _pool._closed:
        log.info("PostgreSQL pool is not initialized or closed. Creating new pool...",
                 host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT,
                 user=settings.POSTGRES_USER, db=settings.POSTGRES_DB)
        try:
            # Codec para manejar JSONB correctamente
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
                min_size=2, # Ajusta según carga esperada
                max_size=10,
                timeout=30.0, # Timeout de conexión
                command_timeout=60.0, # Timeout por comando
                init=init_connection,
                statement_cache_size=0 # Deshabilitar si causa problemas
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
    """Cierra el pool de conexiones."""
    global _pool
    if _pool and not _pool._closed:
        log.info("Closing PostgreSQL connection pool...")
        await _pool.close()
        _pool = None
        log.info("PostgreSQL connection pool closed.")
    elif _pool and _pool._closed:
        log.warning("Attempted to close an already closed PostgreSQL pool.")
        _pool = None
    else:
        log.info("No active PostgreSQL connection pool to close.")

async def check_db_connection() -> bool:
    """Verifica que la conexión a la base de datos esté funcionando."""
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            async with conn.transaction(): # Usa transacción para asegurar que no haya efectos secundarios
                result = await conn.fetchval("SELECT 1")
        return result == 1
    except Exception as e:
        log.error("Database connection check failed", error=str(e))
        return False

# --- Document Operations ---

async def create_document(
    company_id: uuid.UUID,
    file_name: str,
    file_type: str,
    metadata: Dict[str, Any]
) -> uuid.UUID:
    """
    Crea un registro inicial para el documento en la tabla DOCUMENTS.
    Retorna el UUID del documento creado.
    """
    pool = await get_db_pool()
    doc_id = uuid.uuid4()
    # Usar NOW() para timestamps, status inicial UPLOADED
    # Incluir updated_at en el INSERT inicial
    query = """
        INSERT INTO documents (id, company_id, file_name, file_type, metadata, status, uploaded_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC')
        RETURNING id;
    """
    insert_log = log.bind(company_id=str(company_id), filename=file_name, content_type=file_type, proposed_doc_id=str(doc_id))
    try:
        async with pool.acquire() as connection:
            # Usar json.dumps para el campo jsonb 'metadata'
            result_id = await connection.fetchval(
                query, doc_id, company_id, file_name, file_type, json.dumps(metadata), DocumentStatus.UPLOADED.value
            )
        if result_id and result_id == doc_id:
            insert_log.info("Document record created in PostgreSQL", document_id=str(doc_id))
            return result_id
        else:
             insert_log.error("Failed to create document record, unexpected or no ID returned.", returned_id=result_id)
             raise RuntimeError(f"Failed to create document record, return value mismatch ({result_id})")
    except asyncpg.exceptions.UniqueViolationError as e:
        # Esto es improbable si usamos UUID v4, pero posible si hay constraints en otros campos
        insert_log.error("Unique constraint violation creating document record.", error=str(e), constraint=e.constraint_name, exc_info=False)
        raise ValueError(f"Document creation failed due to unique constraint ({e.constraint_name})") from e
    except Exception as e:
        insert_log.error("Failed to create document record in PostgreSQL", error=str(e), exc_info=True)
        raise # Relanzar para manejo superior

async def update_document_status(
    document_id: uuid.UUID,
    status: DocumentStatus,
    file_path: Optional[str] = None,
    chunk_count: Optional[int] = None,
    error_message: Optional[str] = None,
) -> bool:
    """
    Actualiza el estado y otros campos de un documento.
    Limpia error_message si el estado no es ERROR.
    """
    pool = await get_db_pool()
    update_log = log.bind(document_id=str(document_id), new_status=status.value)

    fields_to_set: List[str] = []
    params: List[Any] = [document_id] # $1 será el ID
    param_index = 2 # Empezar parámetros desde $2

    # Siempre actualizar 'status' y 'updated_at'
    fields_to_set.append(f"status = ${param_index}")
    params.append(status.value); param_index += 1
    fields_to_set.append(f"updated_at = NOW() AT TIME ZONE 'UTC'")

    # Añadir otros campos condicionalmente
    if file_path is not None:
        fields_to_set.append(f"file_path = ${param_index}")
        params.append(file_path); param_index += 1
    if chunk_count is not None:
        fields_to_set.append(f"chunk_count = ${param_index}")
        params.append(chunk_count); param_index += 1

    # Manejar error_message
    if status == DocumentStatus.ERROR:
        safe_error = (error_message or "Unknown processing error")[:2000] # Limitar longitud de error
        fields_to_set.append(f"error_message = ${param_index}")
        params.append(safe_error); param_index += 1
        update_log = update_log.bind(error_message=safe_error)
    else:
        # Si el nuevo estado NO es ERROR, limpiar el campo error_message
        fields_to_set.append("error_message = NULL")

    query = f"UPDATE documents SET {', '.join(fields_to_set)} WHERE id = $1;"
    update_log.debug("Executing document status update", query=query, params_count=len(params))

    try:
        async with pool.acquire() as connection:
             result_str = await connection.execute(query, *params) # Desempaquetar params

        # Verificar si se actualizó alguna fila
        if isinstance(result_str, str) and result_str.startswith("UPDATE "):
            affected_rows = int(result_str.split(" ")[1])
            if affected_rows > 0:
                update_log.info("Document status updated successfully", affected_rows=affected_rows)
                return True
            else:
                update_log.warning("Document status update command executed but no rows were affected (document ID might not exist).")
                return False
        else:
             update_log.error("Unexpected result from document update execution", db_result=result_str)
             return False # Considerar esto como fallo

    except Exception as e:
        update_log.error("Failed to update document status in PostgreSQL", error=str(e), exc_info=True)
        raise # Relanzar para manejo superior

async def get_document_status(document_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """Obtiene datos clave de un documento por su ID."""
    pool = await get_db_pool()
    # Seleccionar columnas necesarias para la API (schema StatusResponse) y validación de company_id
    query = """
        SELECT id, status, file_name, file_type, chunk_count, error_message, updated_at, company_id
        FROM documents
        WHERE id = $1;
    """
    get_log = log.bind(document_id=str(document_id))
    try:
        async with pool.acquire() as connection:
            record = await connection.fetchrow(query, document_id)
        if record:
            get_log.debug("Document status retrieved successfully")
            return dict(record) # Convertir a dict
        else:
            get_log.warning("Document status requested for non-existent ID")
            return None
    except Exception as e:
        get_log.error("Failed to get document status from PostgreSQL", error=str(e), exc_info=True)
        raise

async def list_documents_by_company(
    company_id: uuid.UUID,
    limit: int = 100,
    offset: int = 0
) -> List[Dict[str, Any]]:
    """
    Obtiene una lista paginada de documentos para una compañía, ordenados por updated_at DESC.
    """
    pool = await get_db_pool()
    # Seleccionar columnas para StatusResponse
    query = """
        SELECT id, status, file_name, file_type, chunk_count, error_message, updated_at
        FROM documents
        WHERE company_id = $1
        ORDER BY updated_at DESC
        LIMIT $2 OFFSET $3;
    """
    list_log = log.bind(company_id=str(company_id), limit=limit, offset=offset)
    try:
        async with pool.acquire() as connection:
            records = await connection.fetch(query, company_id, limit, offset)

        result_list = [dict(record) for record in records] # Convertir Records a Dicts
        list_log.info(f"Retrieved {len(result_list)} documents for company listing")
        return result_list
    except Exception as e:
        list_log.error("Failed to list documents by company from PostgreSQL", error=str(e), exc_info=True)
        raise