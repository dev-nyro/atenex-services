# File: app/db/postgres_client.py
# api-gateway/app/db/postgres_client.py
import uuid
from typing import Any, Optional, Dict, List
import asyncpg
import structlog
import json

from app.core.config import settings

log = structlog.get_logger(__name__)

_pool: Optional[asyncpg.Pool] = None

async def get_db_pool() -> asyncpg.Pool:
    """
    Obtiene o crea el pool de conexiones a la base de datos PostgreSQL.
    """
    global _pool
    if _pool is None or _pool._closed: # Recrea el pool si está cerrado
        try:
            log.info("Creating PostgreSQL connection pool...",
                     host=settings.POSTGRES_SERVER,
                     port=settings.POSTGRES_PORT,
                     user=settings.POSTGRES_USER,
                     database=settings.POSTGRES_DB)

            # Asegúrate de que el codec jsonb esté registrado si usas JSONB
            def _json_encoder(value):
                return json.dumps(value)
            def _json_decoder(value):
                return json.loads(value)

            async def init_connection(conn):
                 await conn.set_type_codec(
                     'jsonb',
                     encoder=_json_encoder,
                     decoder=_json_decoder,
                     schema='pg_catalog'
                 )
                 await conn.set_type_codec(
                      'json',
                      encoder=_json_encoder,
                      decoder=_json_decoder,
                      schema='pg_catalog'
                  )

            _pool = await asyncpg.create_pool(
                user=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD.get_secret_value(), # Obtener valor del SecretStr
                database=settings.POSTGRES_DB,
                host=settings.POSTGRES_SERVER,
                port=settings.POSTGRES_PORT,
                min_size=5,   # Ajusta según necesidad
                max_size=20,  # Ajusta según necesidad
                statement_cache_size=0, # Deshabilitar caché para evitar problemas con tipos dinámicos
                init=init_connection # Añadir inicializador para codecs JSON/JSONB
            )
            log.info("PostgreSQL connection pool created successfully.")
        except Exception as e:
            log.error("Failed to create PostgreSQL connection pool",
                      error=str(e), error_type=type(e).__name__,
                      host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT,
                      db=settings.POSTGRES_DB, user=settings.POSTGRES_USER,
                      exc_info=True)
            _pool = None # Asegurar que el pool es None si falla la creación
            raise # Relanzar para que el inicio de la app falle si no hay DB
    return _pool

async def close_db_pool():
    """Cierra el pool de conexiones a la base de datos."""
    global _pool
    if _pool and not _pool._closed:
        log.info("Closing PostgreSQL connection pool...")
        await _pool.close()
        _pool = None # Resetear la variable global
        log.info("PostgreSQL connection pool closed successfully.")
    elif _pool and _pool._closed:
        log.warning("Attempted to close an already closed PostgreSQL pool.")
        _pool = None
    else:
        log.info("No active PostgreSQL connection pool to close.")


async def check_db_connection() -> bool:
    """Verifica que la conexión a la base de datos esté funcionando."""
    pool = None # Asegurar inicialización
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            result = await conn.fetchval("SELECT 1")
        return result == 1
    except Exception as e:
        log.error("Database connection check failed", error=str(e))
        return False
    # No cerrar el pool aquí, solo verificar

# --- Métodos específicos para la tabla USERS ---

async def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    """
    Recupera un usuario por su email.
    Devuelve un diccionario con los datos del usuario o None si no se encuentra.
    Alineado con el esquema USERS.
    """
    pool = await get_db_pool()
    query = """
        SELECT id, company_id, email, hashed_password, full_name, role,
               created_at, last_login, is_active
        FROM users
        WHERE lower(email) = lower($1)
    """
    # No filtramos por is_active aquí, la lógica de autenticación puede decidir
    log.debug("Executing get_user_by_email query", email=email)
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, email)
        if row:
            log.debug("User found by email", user_id=str(row['id']))
            return dict(row) # Convertir asyncpg.Record a dict
        else:
            log.debug("User not found by email", email=email)
            return None
    except Exception as e:
        log.error("Error getting user by email", error=str(e), email=email, exc_info=True)
        raise # Relanzar para manejo de errores superior

async def get_user_by_id(user_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """
    Recupera un usuario por su ID (UUID).
    Devuelve un diccionario con los datos del usuario o None si no se encuentra.
    Alineado con el esquema USERS. Excluye la contraseña hash por seguridad.
    """
    pool = await get_db_pool()
    query = """
        SELECT id, company_id, email, full_name, role,
               created_at, last_login, is_active
        FROM users
        WHERE id = $1
    """
    log.debug("Executing get_user_by_id query", user_id=str(user_id))
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, user_id)
        if row:
            log.debug("User found by ID", user_id=str(user_id))
            return dict(row)
        else:
            log.debug("User not found by ID", user_id=str(user_id))
            return None
    except Exception as e:
        log.error("Error getting user by ID", error=str(e), user_id=str(user_id), exc_info=True)
        raise

async def update_user_company(user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    """
    Actualiza el company_id para un usuario específico y actualiza last_login.
    Devuelve True si la actualización fue exitosa (al menos una fila afectada), False en caso contrario.
    Alineado con el esquema USERS.
    """
    pool = await get_db_pool()
    query = """
        UPDATE users
        SET company_id = $2, updated_at = NOW()
        WHERE id = $1
        RETURNING id -- Devolvemos el ID para confirmar la actualización
    """
    log.debug("Executing update_user_company query", user_id=str(user_id), company_id=str(company_id))
    try:
        async with pool.acquire() as conn:
            # Usar fetchval para obtener el ID devuelto o None
            result = await conn.fetchval(query, user_id, company_id)
        if result is not None:
            log.info("User company updated successfully", user_id=str(user_id), new_company_id=str(company_id))
            return True
        else:
            # Esto podría suceder si el user_id no existe
            log.warning("Update user company command executed but no rows were affected.", user_id=str(user_id))
            return False
    except Exception as e:
        log.error("Error updating user company", error=str(e), user_id=str(user_id), company_id=str(company_id), exc_info=True)
        raise
