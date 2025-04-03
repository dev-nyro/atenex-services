# ./app/db/postgres_client.py
import uuid
from typing import Any, Optional, Dict, List, Tuple
import asyncpg
import structlog
import json
from datetime import datetime

from app.core.config import settings
# --- Importar Schemas para type hinting (opcional pero útil) ---
from app.api.v1 import schemas # Importar schemas para usar ChatSummary, ChatMessage, etc.

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
                # --- FIX: Add statement_cache_size=0 for PgBouncer compatibility ---
                statement_cache_size=0,
                # -----------------------------------------------------------------
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
        # --- Catch the specific error causing the issue ---
        except asyncpg.exceptions.DuplicatePreparedStatementError as e:
             log.error("Failed to create Supabase/PostgreSQL connection pool due to prepared statement conflict (likely PgBouncer issue). Check statement_cache_size setting.", error=str(e), exc_info=True)
             raise
        # --- Generic catch-all ---
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
    user_id: Optional[uuid.UUID],
    query: str,
    response: str,
    retrieved_doc_ids: List[str],
    retrieved_doc_scores: List[float],
    # --- Añadir chat_id ---
    chat_id: Optional[uuid.UUID] = None,
    metadata: Optional[Dict[str, Any]] = None
) -> uuid.UUID:
    """
    Registra una interacción de consulta en la tabla QUERY_LOGS, incluyendo el chat_id.
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

    avg_relevance_score = sum(retrieved_doc_scores) / len(retrieved_doc_scores) if retrieved_doc_scores else None

    # --- Modificar query para incluir chat_id ---
    query_sql = """
        INSERT INTO query_logs
               (id, company_id, user_id, query, response, relevance_score, metadata, chat_id, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW() AT TIME ZONE 'UTC')
        RETURNING id;
    """

    try:
        async with pool.acquire() as connection:
            result = await connection.fetchval(
                query_sql,
                log_id,
                company_id,
                user_id,
                query,
                response,
                avg_relevance_score,
                log_metadata,
                chat_id # Pasar el chat_id
            )
        if result:
            log.info("Query interaction logged successfully", query_log_id=str(result), company_id=str(company_id), user_id=str(user_id) if user_id else "N/A", chat_id=str(chat_id) if chat_id else "N/A")
            return result
        else:
             log.error("Failed to log query interaction, no ID returned.", log_id_attempted=str(log_id))
             raise RuntimeError("Failed to log query interaction.")
    except Exception as e:
        log.error("Failed to log query interaction to Supabase",
                  error=str(e), log_id_attempted=str(log_id), company_id=str(company_id), chat_id=str(chat_id) if chat_id else "N/A",
                  exc_info=True)
        raise

async def check_db_connection() -> bool:
    """Verifica si se puede establecer una conexión básica con la BD."""
    pool = None
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            # Usar una consulta simple que no dependa de prepared statements por defecto
            await conn.execute("SELECT 1")
        return True
    except Exception:
        log.error("Database connection check failed", exc_info=True) # Loguear el error
        return False

# --- Funciones para CHATS ---

async def create_chat(user_id: uuid.UUID, company_id: uuid.UUID, title: Optional[str] = None) -> uuid.UUID:
    """Crea un nuevo chat para un usuario y empresa."""
    pool = await get_db_pool()
    chat_id = uuid.uuid4()
    # Usar NOW() para created_at y updated_at iniciales
    query = """
        INSERT INTO chats (id, user_id, company_id, title, created_at, updated_at)
        VALUES ($1, $2, $3, $4, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC')
        RETURNING id;
    """
    try:
        async with pool.acquire() as connection:
            created_id = await connection.fetchval(query, chat_id, user_id, company_id, title)
        if created_id:
             log.info("Chat created successfully", chat_id=str(created_id), user_id=str(user_id), company_id=str(company_id))
             return created_id
        else:
             log.error("Failed to create chat, no ID returned", attempted_id=str(chat_id))
             raise RuntimeError("Failed to create chat")
    except Exception as e:
        log.error("Error creating chat in database", error=str(e), user_id=str(user_id), company_id=str(company_id), exc_info=True)
        raise

async def get_chats_for_user(user_id: uuid.UUID, company_id: uuid.UUID) -> List[schemas.ChatSummary]:
    """Obtiene la lista de chats para un usuario y empresa, ordenados por última actualización."""
    pool = await get_db_pool()
    query = """
        SELECT id, title, updated_at
        FROM chats
        WHERE user_id = $1 AND company_id = $2
        ORDER BY updated_at DESC;
    """
    try:
        async with pool.acquire() as connection:
            records = await connection.fetch(query, user_id, company_id)
        chats = [schemas.ChatSummary(id=r['id'], title=r['title'], updated_at=r['updated_at']) for r in records]
        log.debug("Fetched chats for user", user_id=str(user_id), company_id=str(company_id), count=len(chats))
        return chats
    except Exception as e:
        log.error("Error fetching chats for user", error=str(e), user_id=str(user_id), company_id=str(company_id), exc_info=True)
        raise # O devolver lista vacía? Por ahora lanzamos error.

async def delete_chat(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    """Elimina un chat y sus mensajes asociados, verificando la propiedad."""
    pool = await get_db_pool()
    # Usar una transacción para asegurar que se borre el chat y los mensajes atomicamente
    try:
        async with pool.acquire() as connection:
            async with connection.transaction():
                # 1. Verificar propiedad y existencia del chat
                owner_check = await connection.fetchval(
                    "SELECT id FROM chats WHERE id = $1 AND user_id = $2 AND company_id = $3",
                    chat_id, user_id, company_id
                )
                if not owner_check:
                    log.warning("Attempt to delete chat not found or not owned by user", chat_id=str(chat_id), user_id=str(user_id), company_id=str(company_id))
                    return False # O lanzar una excepción específica de "NotFound" o "Forbidden"

                # 2. Borrar mensajes asociados (ON DELETE CASCADE podría manejar esto también si está configurado)
                # Es más explícito borrar primero los mensajes si no hay CASCADE.
                # Usamos execute que devuelve el status tag (e.g., "DELETE 5")
                deleted_messages_status = await connection.execute("DELETE FROM messages WHERE chat_id = $1", chat_id)
                log.debug("Executed delete for associated messages", chat_id=str(chat_id), status=deleted_messages_status)

                # 3. Borrar el chat
                result = await connection.execute("DELETE FROM chats WHERE id = $1", chat_id)

                # El status devuelto es como "DELETE 1" o "DELETE 0"
                deleted_count = int(result.split(" ")[1]) if result and result.startswith("DELETE") else 0
                if deleted_count > 0:
                    log.info("Chat deleted successfully", chat_id=str(chat_id), user_id=str(user_id), company_id=str(company_id))
                    return True
                else:
                    # Esto no debería ocurrir si owner_check pasó, pero por seguridad
                    log.warning("Chat deletion command executed but reported 0 rows affected", chat_id=str(chat_id))
                    return False # O podría indicar un problema si owner_check pasó
    except Exception as e:
        log.error("Error deleting chat", error=str(e), chat_id=str(chat_id), user_id=str(user_id), exc_info=True)
        raise

async def check_chat_ownership(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    """Verifica si un chat pertenece al usuario y compañía dados."""
    pool = await get_db_pool()
    query = "SELECT EXISTS (SELECT 1 FROM chats WHERE id = $1 AND user_id = $2 AND company_id = $3)"
    try:
        async with pool.acquire() as connection:
            exists = await connection.fetchval(query, chat_id, user_id, company_id)
        return bool(exists) # Asegurarse de devolver un booleano
    except Exception as e:
        log.error("Error checking chat ownership", error=str(e), chat_id=str(chat_id), user_id=str(user_id), exc_info=True)
        return False # Asumir que no si hay error


# --- Funciones para MESSAGES ---

async def add_message_to_chat(
    chat_id: uuid.UUID,
    role: str, # 'user' o 'assistant'
    content: str,
    sources: Optional[List[Dict[str, Any]]] = None
) -> uuid.UUID:
    """Añade un mensaje a un chat y actualiza el timestamp del chat."""
    pool = await get_db_pool()
    message_id = uuid.uuid4()

    # Usar una transacción para insertar mensaje y actualizar chat
    try:
        async with pool.acquire() as connection:
            async with connection.transaction():
                # 1. Insertar el mensaje
                insert_query = """
                    INSERT INTO messages (id, chat_id, role, content, sources, created_at)
                    VALUES ($1, $2, $3, $4, $5, NOW() AT TIME ZONE 'UTC')
                    RETURNING id;
                """
                created_id = await connection.fetchval(
                    insert_query, message_id, chat_id, role, content, sources # sources es jsonb
                )

                if not created_id:
                    log.error("Failed to insert message, no ID returned", attempted_id=str(message_id), chat_id=str(chat_id))
                    # Forzar rollback de la transacción lanzando error
                    raise RuntimeError("Failed to insert message, transaction rolled back")

                # 2. Actualizar el timestamp 'updated_at' del chat
                update_query = """
                    UPDATE chats
                    SET updated_at = NOW() AT TIME ZONE 'UTC'
                    WHERE id = $1;
                """
                update_status = await connection.execute(update_query, chat_id)
                # Verificar si la actualización afectó alguna fila (el chat existe)
                if not update_status or not update_status.startswith("UPDATE 1"):
                    log.error("Failed to update chat timestamp after adding message, chat ID might be invalid", chat_id=str(chat_id), update_status=update_status)
                    # Forzar rollback de la transacción lanzando error
                    raise RuntimeError(f"Failed to update timestamp for chat {chat_id}, transaction rolled back")


        log.info("Message added to chat successfully", message_id=str(created_id), chat_id=str(chat_id), role=role)
        return created_id
    except asyncpg.exceptions.ForeignKeyViolationError:
         log.error("Error adding message: Chat ID does not exist", chat_id=str(chat_id), role=role)
         # Lanzar un error específico o devolver None/False podría ser mejor aquí
         raise ValueError(f"Chat with ID {chat_id} not found.") from None # from None para evitar chain de excepciones innecesario
    except Exception as e:
        log.error("Error adding message to chat", error=str(e), chat_id=str(chat_id), role=role, exc_info=True)
        raise

async def get_messages_for_chat(chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> List[schemas.ChatMessage]:
    """Obtiene los mensajes de un chat específico, verificando la propiedad y ordenando por fecha."""
    pool = await get_db_pool()

    # Primero, verificar si el usuario tiene acceso a este chat
    if not await check_chat_ownership(chat_id, user_id, company_id):
        log.warning("Attempt to access messages for chat not owned or non-existent", chat_id=str(chat_id), user_id=str(user_id), company_id=str(company_id))
        # Podríamos lanzar una excepción aquí (403 Forbidden o 404 Not Found)
        # O simplemente devolver una lista vacía. Devolver vacío es más simple para el endpoint.
        return []

    # Si tiene acceso, obtener los mensajes
    query = """
        SELECT id, role, content, sources, created_at
        FROM messages
        WHERE chat_id = $1
        ORDER BY created_at ASC;
    """
    try:
        async with pool.acquire() as connection:
            records = await connection.fetch(query, chat_id)
        messages = [
            schemas.ChatMessage(
                id=r['id'],
                role=r['role'],
                content=r['content'],
                sources=r['sources'], # Directamente desde JSONB
                created_at=r['created_at']
            ) for r in records
        ]
        log.debug("Fetched messages for chat", chat_id=str(chat_id), count=len(messages))
        return messages
    except Exception as e:
        log.error("Error fetching messages for chat", error=str(e), chat_id=str(chat_id), exc_info=True)
        raise # O devolver lista vacía? Por ahora lanzamos error.