# ./app/db/postgres_client.py (CORREGIDO)
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
    """Obtiene o crea el pool de conexiones a la base de datos (Supabase)."""
    global _pool
    if _pool is None or _pool._closed:
        try:
            # *** CORREGIDO: Loguear los parámetros que SÍ se usan para conectar ***
            log.info("Creating Supabase/PostgreSQL connection pool using arguments...",
                     host=settings.POSTGRES_SERVER,
                     port=settings.POSTGRES_PORT, # Usará el puerto del Pooler (6543) desde config
                     user=settings.POSTGRES_USER,
                     database=settings.POSTGRES_DB)

            _pool = await asyncpg.create_pool(
                user=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD.get_secret_value(),
                database=settings.POSTGRES_DB,
                host=settings.POSTGRES_SERVER,
                port=settings.POSTGRES_PORT, # Tomará el valor de settings (6543 por defecto o de ConfigMap)
                min_size=5,  # Ajustar según necesidad
                max_size=20, # Ajustar según necesidad
                # Añadir configuraciones de timeout si es necesario
                # command_timeout=60, # Timeout para comandos individuales
                # timeout=300, # Timeout general? Revisar docs de asyncpg
                # El init para jsonb sigue siendo útil
                init=lambda conn: conn.set_type_codec(
                    'jsonb',
                    encoder=json.dumps,
                    decoder=json.loads,
                    schema='pg_catalog',
                    format='text'
                )
            )
            log.info("Supabase/PostgreSQL connection pool created successfully.")
        except OSError as e:
             # Capturar específicamente errores de red/OS como 'Network unreachable'
             log.error("Network/OS error creating Supabase/PostgreSQL connection pool",
                      error=str(e),
                      errno=e.errno if hasattr(e, 'errno') else None,
                      host=settings.POSTGRES_SERVER,
                      port=settings.POSTGRES_PORT,
                      db=settings.POSTGRES_DB,
                      user=settings.POSTGRES_USER,
                      exc_info=True) # Incluir traceback
             raise # Re-lanzar para que falle el startup
        except asyncpg.exceptions.InvalidPasswordError:
             log.error("Invalid password for Supabase/PostgreSQL connection",
                       host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT, user=settings.POSTGRES_USER)
             raise
        except Exception as e:
            # Loguear el error específico
            log.error("Failed to create Supabase/PostgreSQL connection pool",
                      error=str(e),
                      host=settings.POSTGRES_SERVER,
                      port=settings.POSTGRES_PORT,
                      db=settings.POSTGRES_DB,
                      user=settings.POSTGRES_USER,
                      exc_info=True) # Incluir traceback
            raise # Re-lanzar la excepción para que falle el startup si no conecta
    return _pool

async def close_db_pool():
    """Cierra el pool de conexiones."""
    global _pool
    if _pool and not _pool._closed:
        log.info("Closing Supabase/PostgreSQL connection pool...")
        await _pool.close()
        _pool = None
        log.info("Supabase/PostgreSQL connection pool closed.")

# --- Funciones create_document, update_document_status, get_document_status ---
# (Sin cambios necesarios en la lógica interna, ya usan los parámetros individuales
# y el pool correctamente. Se mantienen como estaban en tu versión original)

async def create_document(
    company_id: uuid.UUID,
    file_name: str,
    file_type: str,
    metadata: Dict[str, Any] # Pass the original dictionary
) -> uuid.UUID:
    """Crea un registro inicial para el documento en la tabla DOCUMENTS."""
    pool = await get_db_pool()
    doc_id = uuid.uuid4()

    query = """
        INSERT INTO documents (id, company_id, file_name, file_type, metadata, status, file_path, uploaded_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, '', NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC')
        RETURNING id;
    """
    # Note: $5 is now directly the metadata dict, asyncpg's codec handles it.

    try:
        async with pool.acquire() as connection:
            # async with connection.transaction(): # Consider transaction if needed
                result = await connection.fetchval(
                    query,
                    doc_id,
                    company_id,
                    file_name,
                    file_type,
                    metadata, # Pass the dictionary directly
                    DocumentStatus.UPLOADED.value
                )
        if result:
            log.info("Document record created in Supabase", document_id=doc_id, company_id=company_id)
            return result
        else:
             log.error("Failed to create document record, no ID returned.", document_id=doc_id)
             raise RuntimeError("Failed to create document record, no ID returned.")
    except asyncpg.exceptions.UniqueViolationError as e:
        log.error("Failed to create document record due to unique constraint violation.", error=str(e), document_id=doc_id, company_id=company_id, exc_info=False) # No need for full traceback usually
        # Decide how to handle this - raise a specific error?
        raise ValueError(f"Document creation failed: unique constraint violated ({e.constraint_name})") from e
    except Exception as e:
        log.error("Failed to create document record in Supabase", error=str(e), document_id=doc_id, company_id=company_id, file_name=file_name, exc_info=True)
        raise # Re-raise generic exceptions


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
        # Limitar longitud del mensaje de error
        safe_error_message = (error_message or "Unknown processing error")[:1000]
        fields_to_update.append(f"error_message = ${current_param_index}")
        params.append(safe_error_message)
        current_param_index += 1
    else:
        # Limpiar mensaje de error si el estado no es ERROR
        fields_to_update.append("error_message = NULL")

    query = f"""
        UPDATE documents
        SET {', '.join(fields_to_update)}
        WHERE id = $1;
    """
    try:
        async with pool.acquire() as connection:
             # async with connection.transaction(): # Consider transaction
                result = await connection.execute(query, *params)
        # Parse result string like 'UPDATE 1'
        affected_rows = 0
        if isinstance(result, str) and result.startswith("UPDATE "):
            try:
                affected_rows = int(result.split(" ")[1])
            except (IndexError, ValueError):
                log.warning("Could not parse affected rows from DB result", result_string=result)

        success = affected_rows > 0
        if success:
            log.info("Document status updated in Supabase", document_id=document_id, new_status=status.value, file_path=file_path, chunk_count=chunk_count, has_error=(status == DocumentStatus.ERROR))
        else:
            log.warning("Document status update did not affect any rows (document might not exist?)", document_id=document_id, new_status=status.value)
        return success
    except Exception as e:
        log.error("Failed to update document status in Supabase", error=str(e), document_id=document_id, new_status=status.value, exc_info=True)
        raise # Re-raise generic exceptions


async def get_document_status(document_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """Obtiene el estado y otros datos de un documento de la tabla DOCUMENTS."""
    pool = await get_db_pool()
    query = """
        SELECT id, company_id, status, file_name, file_type, chunk_count, error_message, updated_at
        FROM documents
        WHERE id = $1;
    """
    try:
        async with pool.acquire() as connection:
            record = await connection.fetchrow(query, document_id)
        if record:
            log.debug("Document status retrieved from Supabase", document_id=document_id)
            # Convert asyncpg.Record to dict for easier handling
            return dict(record)
        else:
            log.warning("Document status requested for non-existent ID", document_id=document_id)
            return None
    except Exception as e:
        log.error("Failed to get document status from Supabase", error=str(e), document_id=document_id, exc_info=True)
        raise # Re-raise generic exceptions