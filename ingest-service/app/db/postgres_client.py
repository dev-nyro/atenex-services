# ./app/db/postgres_client.py (CORREGIDO - Deshabilitar caché de statements para PgBouncer/Pooler)
import uuid
from typing import Any, Optional, Dict, List
import asyncpg
import structlog
import json

from app.core.config import settings
from app.models.domain import DocumentStatus

log = structlog.get_logger(__name__)

_pool: Optional[asyncpg.Pool] = None

async def get_db_pool() -> asyncpg.Pool:
    """
    Obtiene o crea el pool de conexiones a la base de datos (Supabase).
    Deshabilita la caché de prepared statements (statement_cache_size=0)
    para compatibilidad con PgBouncer en modo transaction/statement (Supabase Pooler).
    """
    global _pool
    if _pool is None or _pool._closed:
        try:
            log.info("Creating Supabase/PostgreSQL connection pool using arguments...",
                     host=settings.POSTGRES_SERVER,
                     port=settings.POSTGRES_PORT,
                     user=settings.POSTGRES_USER,
                     database=settings.POSTGRES_DB)

            _pool = await asyncpg.create_pool(
                user=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD.get_secret_value(),
                database=settings.POSTGRES_DB,
                host=settings.POSTGRES_SERVER,
                port=settings.POSTGRES_PORT,
                min_size=5,
                max_size=20,
                # *** CORREGIDO: Deshabilitar caché de prepared statements ***
                # Necesario para compatibilidad con PgBouncer en modo 'transaction' o 'statement'
                # (como el Session Pooler de Supabase) que no soporta prepared statements a nivel de sesión.
                statement_cache_size=0,
                # command_timeout=60, # Timeout para comandos individuales
                # timeout=300, # Timeout general? Revisar docs de asyncpg
                init=lambda conn: conn.set_type_codec(
                    'jsonb',
                    encoder=json.dumps,
                    decoder=json.loads,
                    schema='pg_catalog',
                    format='text'
                )
                # Podrías considerar añadir el codec de UUID aquí también si lo usas frecuentemente
                # init=setup_connection_codecs # Ver ejemplo abajo si es necesario
            )
            log.info("Supabase/PostgreSQL connection pool created successfully (statement_cache_size=0).")
        except OSError as e:
             log.error("Network/OS error creating Supabase/PostgreSQL connection pool",
                      error=str(e), errno=getattr(e, 'errno', None),
                      host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT,
                      db=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
                      exc_info=True)
             raise
        except asyncpg.exceptions.InvalidPasswordError:
             log.error("Invalid password for Supabase/PostgreSQL connection",
                       host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT, user=settings.POSTGRES_USER)
             raise
        # Capturar específicamente el error de prepared statement duplicado
        except asyncpg.exceptions.DuplicatePreparedStatementError as e:
            log.error("Failed to create Supabase/PostgreSQL connection pool due to DuplicatePreparedStatementError "
                      "(Confirm statement_cache_size=0 is set correctly for PgBouncer/Pooler)",
                      error=str(e), error_type=type(e).__name__,
                      host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT,
                      db=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
                      exc_info=True) # Incluir traceback en este caso es útil
            raise # Re-lanzar para que falle el startup
        except Exception as e: # Otros errores (incluyendo TimeoutError si volviera a ocurrir)
            log.error("Failed to create Supabase/PostgreSQL connection pool",
                      error=str(e), error_type=type(e).__name__,
                      host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT,
                      db=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
                      exc_info=True)
            raise
    return _pool

# Ejemplo de función init más compleja si necesitas más codecs (opcional)
# async def setup_connection_codecs(connection):
#     await connection.set_type_codec(
#         'jsonb',
#         encoder=json.dumps,
#         decoder=json.loads,
#         schema='pg_catalog',
#         format='text'
#     )
#     # Añadir codec para UUID si no está por defecto o quieres asegurar el manejo
#     await connection.set_type_codec(
#         'uuid',
#         encoder=str,
#         decoder=uuid.UUID,
#         schema='pg_catalog',
#         format='text'
#     )
#     log.debug("Custom type codecs (jsonb, uuid) registered for new connection.", connection=connection)


async def close_db_pool():
    """Cierra el pool de conexiones."""
    global _pool
    if _pool and not _pool._closed:
        log.info("Closing Supabase/PostgreSQL connection pool...")
        await _pool.close()
        _pool = None
        log.info("Supabase/PostgreSQL connection pool closed.")

# --- Funciones create_document, update_document_status, get_document_status ---
# (Sin cambios necesarios en la lógica interna de estas funciones)

async def create_document(
    company_id: uuid.UUID,
    file_name: str,
    file_type: str,
    metadata: Dict[str, Any]
) -> uuid.UUID:
    """Crea un registro inicial para el documento en la tabla DOCUMENTS."""
    pool = await get_db_pool()
    doc_id = uuid.uuid4()
    query = """
        INSERT INTO documents (id, company_id, file_name, file_type, metadata, status, file_path, uploaded_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, '', NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC')
        RETURNING id;
    """
    try:
        async with pool.acquire() as connection:
            # Con statement_cache_size=0, asyncpg no usará prepared statements internamente aquí
            result = await connection.fetchval(
                query, doc_id, company_id, file_name, file_type, metadata, DocumentStatus.UPLOADED.value
            )
        if result:
            log.info("Document record created in Supabase", document_id=doc_id, company_id=company_id)
            return result
        else:
             log.error("Failed to create document record, no ID returned.", document_id=doc_id)
             raise RuntimeError("Failed to create document record, no ID returned.")
    except asyncpg.exceptions.UniqueViolationError as e:
        log.error("Failed to create document record due to unique constraint violation.", error=str(e), document_id=doc_id, company_id=company_id, constraint=e.constraint_name, exc_info=False)
        raise ValueError(f"Document creation failed: unique constraint violated ({e.constraint_name})") from e
    except Exception as e:
        log.error("Failed to create document record in Supabase", error=str(e), document_id=doc_id, company_id=company_id, file_name=file_name, exc_info=True)
        raise

async def update_document_status(
    document_id: uuid.UUID,
    status: DocumentStatus,
    file_path: Optional[str] = None,
    chunk_count: Optional[int] = None,
    error_message: Optional[str] = None,
) -> bool:
    """Actualiza el estado y otros campos de un documento en la tabla DOCUMENTS."""
    pool = await get_db_pool()
    fields_to_update = ["status = $2", "updated_at = NOW() AT TIME ZONE 'UTC'"]
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
    if status == DocumentStatus.ERROR:
        safe_error_message = (error_message or "Unknown processing error")[:1000]
        fields_to_update.append(f"error_message = ${current_param_index}")
        params.append(safe_error_message)
        current_param_index += 1
    else:
        fields_to_update.append("error_message = NULL")
    query = f"UPDATE documents SET {', '.join(fields_to_update)} WHERE id = $1;"
    try:
        async with pool.acquire() as connection:
             # Con statement_cache_size=0, asyncpg no usará prepared statements internamente aquí
             result = await connection.execute(query, *params)
        affected_rows = 0
        if isinstance(result, str) and result.startswith("UPDATE "):
            try: affected_rows = int(result.split(" ")[1])
            except (IndexError, ValueError): log.warning("Could not parse affected rows from DB result", result_string=result)
        success = affected_rows > 0
        if success: log.info("Document status updated in Supabase", document_id=document_id, new_status=status.value, file_path=file_path, chunk_count=chunk_count, has_error=(status == DocumentStatus.ERROR))
        else: log.warning("Document status update did not affect any rows", document_id=document_id, new_status=status.value)
        return success
    except Exception as e:
        log.error("Failed to update document status in Supabase", error=str(e), document_id=document_id, new_status=status.value, exc_info=True)
        raise

async def get_document_status(document_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """Obtiene el estado y otros datos de un documento de la tabla DOCUMENTS."""
    pool = await get_db_pool()
    query = "SELECT id, company_id, status, file_name, file_type, chunk_count, error_message, updated_at FROM documents WHERE id = $1;"
    try:
        async with pool.acquire() as connection:
            # Con statement_cache_size=0, asyncpg no usará prepared statements internamente aquí
            record = await connection.fetchrow(query, document_id)
        if record:
            log.debug("Document status retrieved from Supabase", document_id=document_id)
            return dict(record)
        else:
            log.warning("Document status requested for non-existent ID", document_id=document_id)
            return None
    except Exception as e:
        log.error("Failed to get document status from Supabase", error=str(e), document_id=document_id, exc_info=True)
        raise