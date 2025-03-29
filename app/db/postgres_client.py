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
            # *** CORRECCIÓN: Usar argumentos nombrados en lugar de DSN string ***
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
        except Exception as e:
            # Loguear el error específico
            log.error("Failed to create Supabase/PostgreSQL connection pool",
                      error=str(e),
                      host=settings.POSTGRES_SERVER,
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

async def create_document(
    company_id: uuid.UUID,
    file_name: str,
    file_type: str,
    metadata: Dict[str, Any] # Pass the original dictionary
) -> uuid.UUID:
    """Crea un registro inicial para el documento en la tabla DOCUMENTS."""
    pool = await get_db_pool()
    doc_id = uuid.uuid4()

    # No need to manually serialize metadata if pool init handles jsonb codec
    # try:
    #     serialized_metadata = json.dumps(metadata)
    # except TypeError as e:
    #     log.error("Metadata is not JSON serializable", metadata=metadata, error=e)
    #     raise ValueError("Provided metadata cannot be serialized to JSON") from e

    query = """
        INSERT INTO documents (id, company_id, file_name, file_type, metadata, status, file_path, uploaded_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, '', NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC')
        RETURNING id;
    """
    # Note: $5 is now directly the metadata dict, asyncpg's codec handles it.

    try:
        async with pool.acquire() as connection:
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
        log.error("Failed to create document record due to unique constraint violation.", error=str(e), document_id=doc_id, company_id=company_id, exc_info=True)
        raise ValueError(f"Document creation failed: {e}") from e
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
    # This function remains logically the same as before.
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
        fields_to_update.append(f"error_message = ${current_param_index}")
        params.append(error_message[:1000] if error_message else "Unknown processing error")
        current_param_index += 1
    else:
        fields_to_update.append("error_message = NULL")

    query = f"""
        UPDATE documents
        SET {', '.join(fields_to_update)}
        WHERE id = $1;
    """
    try:
        async with pool.acquire() as connection:
             result = await connection.execute(query, *params)
        success = result.startswith("UPDATE ") and int(result.split(" ")[1]) > 0
        if success:
            log.info("Document status updated in Supabase", document_id=document_id, new_status=status.value, file_path=file_path, chunk_count=chunk_count, has_error=(status == DocumentStatus.ERROR))
        else:
            log.warning("Document status update did not affect any rows (document might not exist?)", document_id=document_id, new_status=status.value)
        return success
    except Exception as e:
        log.error("Failed to update document status in Supabase", error=str(e), document_id=document_id, new_status=status.value, exc_info=True)
        raise


async def get_document_status(document_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """Obtiene el estado y otros datos de un documento de la tabla DOCUMENTS."""
    # This function remains logically the same as before.
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
            return dict(record) # Convert Record to dict
        else:
            log.warning("Document status requested for non-existent ID", document_id=document_id)
            return None
    except Exception as e:
        log.error("Failed to get document status from Supabase", error=str(e), document_id=document_id, exc_info=True)
        raise