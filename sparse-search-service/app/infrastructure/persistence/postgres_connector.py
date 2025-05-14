# sparse-search-service/app/infrastructure/persistence/postgres_connector.py
import asyncpg
import structlog
import json
from typing import Optional

from app.core.config import settings

log = structlog.get_logger(__name__) # logger específico para el conector

_pool: Optional[asyncpg.Pool] = None

async def get_db_pool() -> asyncpg.Pool:
    """Gets the existing asyncpg pool or creates a new one for Sparse Search Service."""
    global _pool
    if _pool is None or _pool._closed:
        connector_log = log.bind(
            service_context="SparseSearchPostgresConnector",
            host=settings.POSTGRES_SERVER,
            port=settings.POSTGRES_PORT,
            user=settings.POSTGRES_USER,
            db=settings.POSTGRES_DB
        )
        connector_log.info("Creating PostgreSQL connection pool...")
        try:
            # Función para configurar codecs JSON (opcional pero recomendado)
            def _json_encoder(value): return json.dumps(value)
            def _json_decoder(value): return json.loads(value)
            async def init_connection(conn):
                await conn.set_type_codec('jsonb', encoder=_json_encoder, decoder=_json_decoder, schema='pg_catalog', format='text')
                await conn.set_type_codec('json', encoder=_json_encoder, decoder=_json_decoder, schema='pg_catalog', format='text')
                connector_log.debug("JSON(B) type codecs configured for new connection.")

            _pool = await asyncpg.create_pool(
                user=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD.get_secret_value(),
                database=settings.POSTGRES_DB,
                host=settings.POSTGRES_SERVER,
                port=settings.POSTGRES_PORT,
                min_size=settings.DB_POOL_MIN_SIZE,
                max_size=settings.DB_POOL_MAX_SIZE,
                timeout=settings.DB_CONNECT_TIMEOUT, # Timeout para establecer una conexión
                command_timeout=settings.DB_COMMAND_TIMEOUT, # Timeout para ejecutar un comando
                init=init_connection, # Función para ejecutar en nuevas conexiones
                statement_cache_size=0 # Deshabilitar cache de statements si hay problemas o se prefiere simplicidad
            )
            connector_log.info("PostgreSQL connection pool created successfully.")
        except (asyncpg.exceptions.InvalidPasswordError, OSError, ConnectionRefusedError) as conn_err:
            connector_log.critical("CRITICAL: Failed to connect to PostgreSQL.", error_details=str(conn_err), exc_info=False) # No exc_info para errores comunes
            _pool = None # Asegurar que el pool es None si falla
            raise ConnectionError(f"Failed to connect to PostgreSQL for Sparse Search Service: {conn_err}") from conn_err
        except Exception as e:
            connector_log.critical("CRITICAL: Unexpected error creating PostgreSQL connection pool.", error_details=str(e), exc_info=True)
            _pool = None
            raise RuntimeError(f"Failed to create PostgreSQL pool for Sparse Search Service: {e}") from e
    return _pool

async def close_db_pool():
    """Closes the asyncpg connection pool for Sparse Search Service."""
    global _pool
    connector_log = log.bind(service_context="SparseSearchPostgresConnector")
    if _pool and not _pool._closed:
        connector_log.info("Closing PostgreSQL connection pool...")
        try:
            await _pool.close()
            connector_log.info("PostgreSQL connection pool closed successfully.")
        except Exception as e:
            connector_log.error("Error while closing PostgreSQL connection pool.", error_details=str(e), exc_info=True)
        finally:
            _pool = None
    elif _pool and _pool._closed:
        connector_log.warning("Attempted to close an already closed PostgreSQL pool.")
        _pool = None # Asegurar que esté limpio
    else:
        connector_log.info("No active PostgreSQL connection pool to close.")

async def check_db_connection() -> bool:
    """Checks if a connection to the database can be established."""
    pool = None
    conn = None
    connector_log = log.bind(service_context="SparseSearchPostgresConnector", action="check_db_connection")
    try:
        pool = await get_db_pool() # Esto intentará crear el pool si no existe
        conn = await pool.acquire() # Tomar una conexión del pool
        result = await conn.fetchval("SELECT 1")
        connector_log.debug("Database connection check successful (SELECT 1).", result=result)
        return result == 1
    except Exception as e:
        connector_log.error("Database connection check failed.", error_details=str(e), exc_info=False) # No exc_info aquí para no ser muy verboso
        return False
    finally:
        if conn and pool: # Asegurarse que pool no sea None si conn existe
             await pool.release(conn) # Devolver la conexión al pool