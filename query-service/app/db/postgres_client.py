# ./app/db/postgres_client.py
import uuid
from typing import Any, Optional, Dict, List
import asyncpg
import structlog
import json
from datetime import datetime

from app.core.config import settings
# No domain models needed here for logging

log = structlog.get_logger(__name__)

_pool: Optional[asyncpg.Pool] = None

async def get_db_pool() -> asyncpg.Pool:
    """Obtiene o crea el pool de conexiones a la base de datos (Supabase)."""
    global _pool
    if _pool is None or _pool._closed:
        try:
            log.info("Creating Supabase/PostgreSQL connection pool...",
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
                # Setup to automatically encode/decode JSONB
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
             log.error("Network/OS error creating Supabase/PostgreSQL connection pool", error=str(e), host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT, exc_info=True)
             raise
        except asyncpg.exceptions.InvalidPasswordError:
             log.error("Invalid password for Supabase/PostgreSQL connection", user=settings.POSTGRES_USER)
             raise
        except Exception as e:
            log.error("Failed to create Supabase/PostgreSQL connection pool", error=str(e), exc_info=True)
            raise
    return _pool

async def close_db_pool():
    """Cierra el pool de conexiones."""
    global _pool
    if _pool and not _pool._closed:
        log.info("Closing Supabase/PostgreSQL connection pool...")
        await _pool.close()
        _pool = None
        log.info("Supabase/PostgreSQL connection pool closed.")

async def log_query_interaction(
    company_id: uuid.UUID,
    user_id: Optional[uuid.UUID], # User ID might be optional depending on auth setup
    query: str,
    response: str,
    retrieved_doc_ids: List[str],
    retrieved_doc_scores: List[float],
    metadata: Optional[Dict[str, Any]] = None
) -> uuid.UUID:
    """
    Registra una interacción de consulta en la tabla QUERY_LOGS.

    Args:
        company_id: ID de la empresa.
        user_id: ID del usuario que realizó la consulta (puede ser None).
        query: Texto de la consulta del usuario.
        response: Texto de la respuesta generada por el LLM.
        retrieved_doc_ids: Lista de IDs de los documentos/chunks recuperados por el retriever.
        retrieved_doc_scores: Lista de scores de los documentos/chunks recuperados.
        metadata: Diccionario opcional con información adicional (tiempos, modelo usado, etc.).

    Returns:
        El ID del registro de log creado.
    """
    pool = await get_db_pool()
    log_id = uuid.uuid4()

    # Preparar metadatos para JSONB
    log_metadata = metadata or {}
    log_metadata["retrieved_documents"] = [
        {"id": doc_id, "score": score}
        for doc_id, score in zip(retrieved_doc_ids, retrieved_doc_scores)
    ]
    log_metadata["llm_model"] = settings.GEMINI_MODEL_NAME
    log_metadata["embedding_model"] = settings.OPENAI_EMBEDDING_MODEL

    # Calcular un score promedio de relevancia (opcional, podría ser más complejo)
    avg_relevance_score = sum(retrieved_doc_scores) / len(retrieved_doc_scores) if retrieved_doc_scores else None

    query_sql = """
        INSERT INTO query_logs
               (id, company_id, user_id, query, response, relevance_score, metadata, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, NOW() AT TIME ZONE 'UTC')
        RETURNING id;
    """

    try:
        async with pool.acquire() as connection:
            result = await connection.fetchval(
                query_sql,
                log_id,
                company_id,
                user_id, # Puede ser NULL si user_id es None
                query,
                response,
                avg_relevance_score, # Puede ser NULL
                log_metadata # Directamente el diccionario, asyncpg lo codifica a JSONB
            )
        if result:
            log.info("Query interaction logged successfully", query_log_id=str(result), company_id=str(company_id), user_id=str(user_id) if user_id else "N/A")
            return result
        else:
             log.error("Failed to log query interaction, no ID returned.", log_id_attempted=str(log_id))
             raise RuntimeError("Failed to log query interaction.")
    except Exception as e:
        log.error("Failed to log query interaction to Supabase",
                  error=str(e), log_id_attempted=str(log_id), company_id=str(company_id),
                  exc_info=True)
        # Podríamos decidir no lanzar excepción aquí para no fallar la respuesta al usuario,
        # pero es importante saber que el log falló. Por ahora, la lanzamos.
        raise

async def check_db_connection() -> bool:
    """Verifica si se puede establecer una conexión básica con la BD."""
    pool = None
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")
        return True
    except Exception:
        return False
    finally:
        # No cerramos el pool aquí, solo liberamos la conexión adquirida
        pass