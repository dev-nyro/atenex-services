# query-service/app/infrastructure/persistence/postgres_repositories.py
import uuid
from typing import Any, Optional, Dict, List, Tuple
import asyncpg
import structlog
import json
from datetime import datetime, timezone

from app.core.config import settings
from app.api.v1 import schemas 
from app.domain.models import Chat, ChatMessage, ChatSummary, QueryLog 
from app.application.ports.repository_ports import ChatRepositoryPort, LogRepositoryPort, ChunkContentRepositoryPort 
from .postgres_connector import get_db_pool

log = structlog.get_logger(__name__)


# --- Chat Repository Implementation ---
class PostgresChatRepository(ChatRepositoryPort):
    """Implementación concreta del repositorio de chats usando PostgreSQL."""

    async def create_chat(self, user_id: uuid.UUID, company_id: uuid.UUID, title: Optional[str] = None) -> uuid.UUID:
        pool = await get_db_pool()
        chat_id = uuid.uuid4()
        query = """
        INSERT INTO chats (id, user_id, company_id, title, created_at, updated_at)
        VALUES ($1, $2, $3, $4, NOW() AT TIME ZONE 'UTC', NOW() AT TIME ZONE 'UTC') RETURNING id;
        """
        repo_log = log.bind(repo="PostgresChatRepository", action="create_chat", user_id=str(user_id), company_id=str(company_id))
        try:
            async with pool.acquire() as conn:
                result = await conn.fetchval(query, chat_id, user_id, company_id, title or f"Chat {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}")
            if result and result == chat_id:
                repo_log.info("Chat created successfully", chat_id=str(chat_id))
                return chat_id
            else:
                repo_log.error("Failed to create chat, no ID returned", returned_id=result)
                raise RuntimeError("Failed to create chat, no ID returned")
        except Exception as e:
            repo_log.exception("Failed to create chat")
            raise 

    async def get_user_chats(self, user_id: uuid.UUID, company_id: uuid.UUID, limit: int = 50, offset: int = 0) -> List[ChatSummary]:
        pool = await get_db_pool()
        query = """
        SELECT id, title, updated_at FROM chats
        WHERE user_id = $1 AND company_id = $2
        ORDER BY updated_at DESC LIMIT $3 OFFSET $4;
        """
        repo_log = log.bind(repo="PostgresChatRepository", action="get_user_chats", user_id=str(user_id), company_id=str(company_id))
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(query, user_id, company_id, limit, offset)
            
            chats = [ChatSummary(**dict(row)) for row in rows]
            repo_log.info(f"Retrieved {len(chats)} chat summaries")
            return chats
        except Exception as e:
            repo_log.exception("Failed to get user chats")
            raise

    async def check_chat_ownership(self, chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
        pool = await get_db_pool()
        query = "SELECT EXISTS (SELECT 1 FROM chats WHERE id = $1 AND user_id = $2 AND company_id = $3);"
        repo_log = log.bind(repo="PostgresChatRepository", action="check_chat_ownership", chat_id=str(chat_id), user_id=str(user_id))
        try:
            async with pool.acquire() as conn:
                exists = await conn.fetchval(query, chat_id, user_id, company_id)
            repo_log.debug("Ownership check result", exists=exists)
            return exists is True
        except Exception as e:
            repo_log.exception("Failed to check chat ownership")
            return False 

    async def get_chat_messages(self, chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID, limit: int = 100, offset: int = 0) -> List[ChatMessage]:
        pool = await get_db_pool()
        repo_log = log.bind(repo="PostgresChatRepository", action="get_chat_messages", chat_id=str(chat_id))

        owner = await self.check_chat_ownership(chat_id, user_id, company_id)
        if not owner:
            repo_log.warning("Attempt to get messages for chat not owned or non-existent")
            return []

        messages_query = """
        SELECT id, chat_id, role, content, sources, created_at FROM messages
        WHERE chat_id = $1 ORDER BY created_at ASC LIMIT $2 OFFSET $3;
        """
        try:
            async with pool.acquire() as conn:
                message_rows = await conn.fetch(messages_query, chat_id, limit, offset)

            messages = []
            for row in message_rows:
                msg_dict = dict(row)
                
                if msg_dict.get('sources') is None:
                     msg_dict['sources'] = None
                elif not isinstance(msg_dict.get('sources'), (list, dict, type(None))):
                    
                    log.warning("Unexpected type for 'sources' from DB", type=type(msg_dict['sources']).__name__, message_id=str(msg_dict.get('id')))
                    try:
                        
                        if isinstance(msg_dict['sources'], str):
                            msg_dict['sources'] = json.loads(msg_dict['sources'])
                        else:
                             msg_dict['sources'] = None 
                    except (json.JSONDecodeError, TypeError):
                         log.error("Failed to manually decode 'sources'", message_id=str(msg_dict.get('id')))
                         msg_dict['sources'] = None

                
                if not isinstance(msg_dict.get('sources'), (list, type(None))):
                    msg_dict['sources'] = None

                messages.append(ChatMessage(**msg_dict)) 

            repo_log.info(f"Retrieved {len(messages)} messages")
            return messages
        except Exception as e:
            repo_log.exception("Failed to get chat messages")
            raise

    async def save_message(self, chat_id: uuid.UUID, role: str, content: str, sources: Optional[List[Dict[str, Any]]] = None) -> uuid.UUID:
        pool = await get_db_pool()
        message_id = uuid.uuid4()
        repo_log = log.bind(repo="PostgresChatRepository", action="save_message", chat_id=str(chat_id), role=role)
        conn = None
        try:
            conn = await pool.acquire()
            async with conn.transaction():
                
                update_chat_query = "UPDATE chats SET updated_at = NOW() AT TIME ZONE 'UTC' WHERE id = $1 RETURNING id;"
                chat_updated = await conn.fetchval(update_chat_query, chat_id)
                if not chat_updated:
                    repo_log.error("Failed to update chat timestamp, chat might not exist")
                    raise ValueError(f"Chat with ID {chat_id} not found for saving message.")

                
                insert_message_query = """
                INSERT INTO messages (id, chat_id, role, content, sources, created_at)
                VALUES ($1, $2, $3, $4, $5, NOW() AT TIME ZONE 'UTC') RETURNING id;
                """
                
                result = await conn.fetchval(insert_message_query, message_id, chat_id, role, content, sources)

                if result and result == message_id:
                    repo_log.info("Message saved successfully", message_id=str(message_id))
                    return message_id
                else:
                    repo_log.error("Failed to save message, unexpected result", returned_id=result)
                    raise RuntimeError("Failed to save message, ID mismatch or not returned.")
        except Exception as e:
            repo_log.exception("Failed to save message")
            raise
        finally:
            if conn:
                await pool.release(conn)

    async def delete_chat(self, chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
        pool = await get_db_pool()
        repo_log = log.bind(repo="PostgresChatRepository", action="delete_chat", chat_id=str(chat_id), user_id=str(user_id))

        owner = await self.check_chat_ownership(chat_id, user_id, company_id)
        if not owner:
            repo_log.warning("Chat not found or does not belong to user, deletion skipped")
            return False

        conn = None
        try:
            conn = await pool.acquire()
            async with conn.transaction():
                repo_log.debug("Deleting associated messages...")
                await conn.execute("DELETE FROM messages WHERE chat_id = $1;", chat_id)
                repo_log.debug("Deleting associated query logs...")
                await conn.execute("DELETE FROM query_logs WHERE chat_id = $1;", chat_id)
                repo_log.debug("Deleting chat entry...")
                deleted_id = await conn.fetchval("DELETE FROM chats WHERE id = $1 RETURNING id;", chat_id)
                success = deleted_id is not None
                if success:
                    repo_log.info("Chat deleted successfully")
                else:
                    repo_log.error("Chat deletion failed after deleting dependencies")
                return success
        except Exception as e:
            repo_log.exception("Failed to delete chat")
            raise
        finally:
            if conn:
                await pool.release(conn)


# --- Log Repository Implementation ---
class PostgresLogRepository(LogRepositoryPort):
    """Implementación concreta del repositorio de logs usando PostgreSQL."""

    async def log_query_interaction(
        self,
        user_id: Optional[uuid.UUID],
        company_id: uuid.UUID,
        query: str,
        answer: str,
        retrieved_documents_data: List[Dict[str, Any]], 
        metadata: Optional[Dict[str, Any]] = None,
        chat_id: Optional[uuid.UUID] = None,
    ) -> uuid.UUID:
        pool = await get_db_pool()
        log_id = uuid.uuid4()
        repo_log = log.bind(repo="PostgresLogRepository", action="log_query_interaction", log_id=str(log_id))

        query_sql = """
        INSERT INTO query_logs (
            id, user_id, company_id, query, response,
            metadata, chat_id, created_at
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, NOW() AT TIME ZONE 'UTC'
        ) RETURNING id;
        """
        
        final_metadata = metadata or {}
        final_metadata["retrieved_summary"] = [
            {"id": d.get("id"), "score": d.get("score"), "file_name": d.get("file_name")}
            for d in retrieved_documents_data
        ]

        try:
            async with pool.acquire() as connection:
                
                result = await connection.fetchval(
                    query_sql,
                    log_id, user_id, company_id, query, answer,
                    final_metadata, 
                    chat_id
                )
            if not result or result != log_id:
                repo_log.error("Failed to create query log entry", returned_id=result)
                raise RuntimeError("Failed to create query log entry")
            repo_log.info("Query interaction logged successfully")
            return log_id
        except Exception as e:
            repo_log.exception("Failed to log query interaction")
            raise RuntimeError(f"Failed to log query interaction: {e}") from e


# --- Chunk Content Repository Implementation ---
class PostgresChunkContentRepository(ChunkContentRepositoryPort):
    """Implementación concreta para obtener contenido de chunks desde PostgreSQL."""

    async def get_chunk_contents_by_company(self, company_id: uuid.UUID) -> Dict[str, Dict[str, Any]]:
        pool = await get_db_pool()
        query = """
        SELECT 
            dc.embedding_id, 
            dc.content,
            d.id as document_id,
            d.file_name
        FROM document_chunks dc
        JOIN documents d ON dc.document_id = d.id
        WHERE d.company_id = $1 AND dc.embedding_id IS NOT NULL;
        """
        repo_log = log.bind(repo="PostgresChunkContentRepository", action="get_chunk_contents_by_company", company_id=str(company_id))
        repo_log.warning("Fetching all chunk contents (keyed by embedding_id) for company, this might be memory intensive!")
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(query, company_id)
            
            contents_with_meta = {
                row['embedding_id']: {
                    "content": row['content'],
                    "document_id": str(row['document_id']) if row['document_id'] else None,
                    "file_name": row['file_name']
                } for row in rows if row['embedding_id'] and row['content']
            }
            repo_log.info(f"Retrieved content and metadata for {len(contents_with_meta)} chunks (keyed by embedding_id)")
            return contents_with_meta
        except Exception as e:
            repo_log.exception("Failed to get chunk contents by company (keyed by embedding_id)")
            raise

    async def get_chunk_contents_by_ids(self, chunk_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        if not chunk_ids:
            return {}
        pool = await get_db_pool()
        
        query = """
        SELECT 
            dc.embedding_id, 
            dc.content,
            d.id as document_id,
            d.file_name
        FROM document_chunks dc
        JOIN documents d ON dc.document_id = d.id
        WHERE dc.embedding_id = ANY($1::text[]);
        """
        repo_log = log.bind(repo="PostgresChunkContentRepository", action="get_chunk_contents_by_ids", count=len(chunk_ids))
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(query, chunk_ids) 
            
            contents_with_meta = {
                row['embedding_id']: {
                    "content": row['content'],
                    "document_id": str(row['document_id']) if row['document_id'] else None,
                    "file_name": row['file_name']
                } for row in rows if row['embedding_id'] and row['content']
            }
            repo_log.info(f"Retrieved content and metadata for {len(contents_with_meta)} chunks (keyed by embedding_id) out of {len(chunk_ids)} requested")
            
            if len(contents_with_meta) != len(set(chunk_ids)): # Use set for accurate missing check
                found_ids = set(contents_with_meta.keys())
                missing_ids = [cid for cid in chunk_ids if cid not in found_ids]
                if missing_ids:
                    repo_log.warning("Could not find content/metadata for some requested chunk IDs (embedding_ids)", missing_ids=missing_ids)
            return contents_with_meta
        except Exception as e:
            repo_log.exception("Failed to get chunk contents and metadata by IDs (embedding_ids)")
            raise