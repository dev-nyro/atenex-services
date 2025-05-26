# File: app/db/postgres_client.py
# api-gateway/app/db/postgres_client.py
import uuid
from typing import Any, Optional, Dict, List
import asyncpg
import structlog
import json
from datetime import datetime # Añadido para tipos

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
                 # Codec para JSONB
                 await conn.set_type_codec(
                     'jsonb',
                     encoder=_json_encoder,
                     decoder=_json_decoder,
                     schema='pg_catalog',
                     format='text' # Asegurar formato texto para JSONB
                 )
                 # Codec para JSON estándar
                 await conn.set_type_codec(
                      'json',
                      encoder=_json_encoder,
                      decoder=_json_decoder,
                      schema='pg_catalog',
                      format='text' # Asegurar formato texto para JSON
                  )
                 # --- CORRECCIÓN: ELIMINADO EL BLOQUE PARA text[] ---
                 # asyncpg maneja text[] automáticamente. No es necesario (y causa error)
                 # registrar un codec explícito para él.
                 # await conn.set_type_codec(
                 #     'text[]',
                 #     encoder=lambda x: x,
                 #     decoder=lambda x: x,
                 #     schema='pg_catalog',
                 #     format='text'
                 # )
                 # --- FIN CORRECCIÓN ---


            _pool = await asyncpg.create_pool(
                user=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD.get_secret_value(), # Obtener valor del SecretStr
                database=settings.POSTGRES_DB,
                host=settings.POSTGRES_SERVER,
                port=settings.POSTGRES_PORT,
                min_size=5,   # Ajusta según necesidad
                max_size=20,  # Ajusta según necesidad
                statement_cache_size=0, # Deshabilitar caché para evitar problemas con tipos dinámicos
                init=init_connection # Añadir inicializador para codecs
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
    Alineado con el esquema USERS (con 'roles').
    """
    pool = await get_db_pool()
    # Selecciona 'roles' en lugar de 'role'
    query = """
        SELECT id, company_id, email, hashed_password, full_name, roles,
               created_at, last_login, is_active
        FROM users
        WHERE lower(email) = lower($1)
    """
    log.debug("Executing get_user_by_email query", email=email)
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, email)
        if row:
            log.debug("User found by email", user_id=str(row['id']))
            # asyncpg devuelve 'roles' como una lista de Python automáticamente
            return dict(row)
        else:
            log.debug("User not found by email", email=email)
            return None
    except Exception as e:
        log.error("Error getting user by email", error=str(e), email=email, exc_info=True)
        raise

async def get_user_by_id(user_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """
    Recupera un usuario por su ID (UUID).
    Devuelve un diccionario con los datos del usuario o None si no se encuentra.
    Alineado con el esquema USERS (con 'roles'). Excluye la contraseña hash por seguridad.
    """
    pool = await get_db_pool()
    # Selecciona 'roles' en lugar de 'role'
    query = """
        SELECT id, company_id, email, full_name, roles,
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
            # asyncpg devuelve 'roles' como una lista de Python automáticamente
            return dict(row)
        else:
            log.debug("User not found by ID", user_id=str(user_id))
            return None
    except Exception as e:
        log.error("Error getting user by ID", error=str(e), user_id=str(user_id), exc_info=True)
        raise

async def update_user_company(user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    """
    Actualiza el company_id para un usuario específico y actualiza updated_at (si existiera).
    NOTA: La tabla users según el esquema no tiene updated_at.
    Devuelve True si la actualización fue exitosa, False en caso contrario.
    """
    pool = await get_db_pool()
    # Quitamos updated_at = NOW() porque la columna no existe
    query = """
        UPDATE users
        SET company_id = $2 -- , updated_at = NOW() -- Columna no existe
        WHERE id = $1
        RETURNING id -- Devolvemos el ID para confirmar la actualización
    """
    log.debug("Executing update_user_company query", user_id=str(user_id), company_id=str(company_id))
    try:
        async with pool.acquire() as conn:
            result = await conn.fetchval(query, user_id, company_id)
        if result is not None:
            log.info("User company updated successfully", user_id=str(user_id), new_company_id=str(company_id))
            return True
        else:
            log.warning("Update user company command executed but no rows were affected.", user_id=str(user_id))
            return False
    except Exception as e:
        log.error("Error updating user company", error=str(e), user_id=str(user_id), company_id=str(company_id), exc_info=True)
        raise

# --- NUEVAS FUNCIONES PARA ADMIN ---
# (Las nuevas funciones no se modifican, ya eran correctas respecto a 'roles')

async def create_company(name: str) -> Dict[str, Any]:
    """Crea una nueva compañía."""
    pool = await get_db_pool()
    query = """
        INSERT INTO companies (name)
        VALUES ($1)
        RETURNING id, name, created_at, is_active;
    """
    log.debug("Executing create_company query", name=name)
    try:
        async with pool.acquire() as conn:
            new_company = await conn.fetchrow(query, name)
            if new_company:
                 log.info("Company created successfully", company_id=str(new_company['id']), name=new_company['name'])
                 return dict(new_company)
            else:
                 log.error("Company creation query executed but no data returned.")
                 raise Exception("Failed to retrieve created company data.")
    except asyncpg.UniqueViolationError:
        log.warning("Failed to create company: Name might already exist.", name=name)
        raise
    except Exception as e:
        log.error("Error creating company", error=str(e), name=name, exc_info=True)
        raise

async def get_active_companies_select() -> List[Dict[str, Any]]:
    """Obtiene una lista de IDs y nombres de compañías activas para selectores."""
    pool = await get_db_pool()
    query = """
        SELECT id, name
        FROM companies
        WHERE is_active = TRUE
        ORDER BY name;
    """
    log.debug("Executing get_active_companies_select query")
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query)
        log.info(f"Retrieved {len(rows)} active companies for select.")
        return [dict(row) for row in rows]
    except Exception as e:
        log.error("Error getting active companies for select", error=str(e), exc_info=True)
        raise

async def create_user(
    email: str,
    hashed_password: str,
    name: Optional[str],
    company_id: uuid.UUID,
    roles: List[str]
) -> Dict[str, Any]:
    """Crea un nuevo usuario asociado a una compañía."""
    pool = await get_db_pool()
    # Query corregido sin updated_at
    query = """
        INSERT INTO users (email, hashed_password, full_name, company_id, roles, is_active, created_at)
        VALUES ($1, $2, $3, $4, $5, TRUE, NOW())
        RETURNING id, email, full_name, company_id, roles, is_active, created_at;
    """
    db_roles = roles if isinstance(roles, list) else [roles]
    log.debug("Executing create_user query", email=email, company_id=str(company_id), roles=db_roles)
    try:
        async with pool.acquire() as conn:
            new_user = await conn.fetchrow(query, email, hashed_password, name, company_id, db_roles)
            if new_user:
                log.info("User created successfully", user_id=str(new_user['id']), email=new_user['email'])
                user_data = dict(new_user)
                # hashed_password no está en el RETURNING, así que no hace falta pop
                return user_data
            else:
                log.error("User creation query executed but no data returned.")
                raise Exception("Failed to retrieve created user data.")
    except asyncpg.UniqueViolationError as e:
         log.warning("Failed to create user: Email likely already exists.", email=email, pg_error=str(e))
         raise
    except asyncpg.ForeignKeyViolationError as e:
         log.warning("Failed to create user: Company ID likely does not exist.", company_id=str(company_id), pg_error=str(e))
         raise
    except Exception as e:
        log.error("Error creating user", error=str(e), email=email, exc_info=True)
        raise

async def count_active_companies() -> int:
    """Cuenta el número total de compañías activas."""
    pool = await get_db_pool()
    query = "SELECT COUNT(*) FROM companies WHERE is_active = TRUE;"
    log.debug("Executing count_active_companies query")
    try:
        async with pool.acquire() as conn:
            count = await conn.fetchval(query)
        log.info(f"Found {count} active companies.")
        return count or 0
    except Exception as e:
        log.error("Error counting active companies", error=str(e), exc_info=True)
        raise

async def count_active_users_per_active_company() -> List[Dict[str, Any]]:
    """Cuenta usuarios activos por cada compañía activa."""
    pool = await get_db_pool()
    query = """
        SELECT c.id as company_id, c.name, COUNT(u.id) AS user_count
        FROM companies c
        LEFT JOIN users u ON c.id = u.company_id AND u.is_active = TRUE
        WHERE c.is_active = TRUE
        GROUP BY c.id, c.name
        ORDER BY c.name;
    """
    log.debug("Executing count_active_users_per_active_company query")
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query)
        log.info(f"Retrieved user counts for {len(rows)} active companies.")
        return [dict(row) for row in rows]
    except Exception as e:
        log.error("Error counting users per company", error=str(e), exc_info=True)
        raise

async def get_company_by_id(company_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """Obtiene datos de una compañía por su ID."""
    pool = await get_db_pool()
    # Query corregido para incluir updated_at de la tabla companies
    query = """
        SELECT id, name, email, created_at, updated_at, is_active
        FROM companies
        WHERE id = $1;
    """
    log.debug("Executing get_company_by_id query", company_id=str(company_id))
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, company_id)
        if row:
            log.debug("Company found by ID", company_id=str(company_id))
            return dict(row)
        else:
            log.debug("Company not found by ID", company_id=str(company_id))
            return None
    except Exception as e:
        log.error("Error getting company by ID", error=str(e), company_id=str(company_id), exc_info=True)
        raise

async def check_email_exists(email: str) -> bool:
    """Verifica si un email ya existe en la tabla users."""
    pool = await get_db_pool()
    query = "SELECT EXISTS (SELECT 1 FROM users WHERE lower(email) = lower($1));"
    log.debug("Executing check_email_exists query", email=email)
    try:
        async with pool.acquire() as conn:
            exists = await conn.fetchval(query, email)
        log.debug(f"Email '{email}' exists check result: {exists}")
        return exists or False
    except Exception as e:
        log.error("Error checking email existence", error=str(e), email=email, exc_info=True)
        raise

async def get_users_by_company_id_paginated(
    company_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0
) -> List[Dict[str, Any]]:
    """
    Recupera usuarios por company_id con paginación.
    Devuelve una lista de diccionarios con datos de usuarios o una lista vacía.
    Los campos seleccionados coinciden con el modelo UserResponse.
    """
    pool = await get_db_pool()
    query = """
        SELECT id, email, full_name AS name, company_id, is_active, created_at, roles
        FROM users
        WHERE company_id = $1
        ORDER BY created_at DESC, id
        LIMIT $2 OFFSET $3;
    """
    db_log = log.bind(target_company_id=str(company_id), limit=limit, offset=offset)
    db_log.debug("Executing get_users_by_company_id_paginated query")
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, company_id, limit, offset)
        db_log.info(f"Retrieved {len(rows)} users for company.")
        # asyncpg maneja la conversión de TEXT[] a List[str] para roles
        return [dict(row) for row in rows]
    except Exception as e:
        db_log.error("Error getting users by company ID", error=str(e), exc_info=True)
        # Relanzar para que el router lo maneje como un error 500
        raise