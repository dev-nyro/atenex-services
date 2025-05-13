# query-service/app/infrastructure/persistence/postgres_repositories.py
import uuid
from typing import Any, Optional, Dict, List, Tuple
import asyncpg
import structlog
import json
from datetime import datetime, timezone

from app.core.config import settings
from app.domain.models import Chat, ChatMessage, ChatSummary, QueryLog, RetrievedChunk # Import domain models
from app.application.ports.repository_ports import ChatRepositoryPort, LogRepositoryPort, ChunkContentRepositoryPort # Import Ports
from .postgres_connector import get_db_pool
from app.utils.helpers import get_request_id_from_context # Para logging contextual

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
        repo_log = log.bind(repo="PostgresChatRepository", action="create_chat", user_id=str(user_id), company_id=str(company_id), request_id=get_request_id_from_context())
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
        SELECT id, title, updated_at, created_at FROM chats
        WHERE user_id = $1 AND company_id = $2
        ORDER BY updated_at DESC LIMIT $3 OFFSET $4;
        """
        repo_log = log.bind(repo="PostgresChatRepository", action="get_user_chats", user_id=str(user_id), company_id=str(company_id), request_id=get_request_id_from_context())
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(query, user_id, company_id, limit, offset)
            chats = [ChatSummary(id=row['id'], title=row['title'], updated_at=row['updated_at'], created_at=row['created_at']) for row in rows]
            repo_log.info(f"Retrieved {len(chats)} chat summaries")
            return chats
        except Exception as e:
            repo_log.exception("Failed to get user chats")
            raise

    async def check_chat_ownership(self, chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
        pool = await get_db_pool()
        query = "SELECT EXISTS (SELECT 1 FROM chats WHERE id = $1 AND user_id = $2 AND company_id = $3);"
        repo_log = log.bind(repo="PostgresChatRepository", action="check_chat_ownership", chat_id=str(chat_id), user_id=str(user_id), request_id=get_request_id_from_context())
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
        repo_log = log.bind(repo="PostgresChatRepository", action="get_chat_messages", chat_id=str(chat_id), request_id=get_request_id_from_context())

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
                        if isinstance(msg_dict['sources'], str): msg_dict['sources'] = json.loads(msg_dict['sources'])
                        else: msg_dict['sources'] = None
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
        repo_log = log.bind(repo="PostgresChatRepository", action="save_message", chat_id=str(chat_id), role=role, request_id=get_request_id_from_context())
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
            if conn: await pool.release(conn)

    async def delete_chat(self, chat_id: uuid.UUID, user_id: uuid.UUID, company_id: uuid.UUID) -> bool:
        pool = await get_db_pool()
        repo_log = log.bind(repo="PostgresChatRepository", action="delete_chat", chat_id=str(chat_id), user_id=str(user_id), request_id=get_request_id_from_context())

        owner = await self.check_chat_ownership(chat_id, user_id, company_id)
        if not owner:
            repo_log.warning("Chat not found or does not belong to user, deletion skipped")
            return False
        conn = None
        try:
            conn = await pool.acquire()
            async with conn.transaction():
                await conn.execute("DELETE FROM messages WHERE chat_id = $1;", chat_id)
                await conn.execute("DELETE FROM query_logs WHERE chat_id = $1;", chat_id)
                deleted_id = await conn.fetchval("DELETE FROM chats WHERE id = $1 RETURNING id;", chat_id)
                success = deleted_id is not None
                if success: repo_log.info("Chat deleted successfully")
                else: repo_log.error("Chat deletion failed after deleting dependencies")
                return success
        except Exception as e:
            repo_log.exception("Failed to delete chat")
            raise
        finally:
            if conn: await pool.release(conn)


# --- Log Repository Implementation ---
class PostgresLogRepository(LogRepositoryPort):
    async def log_query_interaction(
        self, user_id: Optional[uuid.UUID], company_id: uuid.UUID, query: str, answer: str,
        retrieved_documents_data: List[Dict[str, Any]], metadata: Optional[Dict[str, Any]] = None,
        chat_id: Optional[uuid.UUID] = None,
    ) -> uuid.UUID:
        pool = await get_db_pool()
        log_id = uuid.uuid4()
        repo_log = log.bind(repo="PostgresLogRepository", action="log_query_interaction", log_id=str(log_id), request_id=get_request_id_from_context())
        query_sql = """
        INSERT INTO query_logs (id, user_id, company_id, query, response, metadata, chat_id, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, NOW() AT TIME ZONE 'UTC') RETURNING id;
        """
        final_metadata = metadata or {}
        final_metadata["retrieved_summary"] = [
            {"id": d.get("id"), "score": d.get("score"), "file_name": d.get("file_name")}
            for d in retrieved_documents_data
        ]
        try:
            async with pool.acquire() as connection:
                result = await connection.fetchval(query_sql, log_id, user_id, company_id, query, answer, final_metadata, chat_id)
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
    async def get_chunk_contents_by_company(self, company_id: uuid.UUID) -> Dict[str, str]:
        pool = await get_db_pool()
        query = """
        SELECT dc.id, dc.content FROM document_chunks dc
        WHERE dc.company_id = $1; 
        """
        repo_log = log.bind(repo="PostgresChunkContentRepository", action="get_chunk_contents_by_company", company_id=str(company_id), request_id=get_request_id_from_context())
        repo_log.warning("Fetching all chunk contents for company, this might be memory intensive!")
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(query, company_id)
            contents = {str(row['id']): row['content'] for row in rows if row['content']}
            repo_log.info(f"Retrieved content for {len(contents)} chunks for BM25 indexing.")
            return contents
        except Exception as e:
            repo_log.exception("Failed to get chunk contents by company for BM25")
            raise

    async def get_chunk_contents_by_ids(self, company_id: uuid.UUID, chunk_ids_with_page: List[str]) -> Dict[str, RetrievedChunk]:
        # LLM_PROCESS_START_get_chunk_contents_by_ids_MODIFIED
        repo_log = log.bind(repo="PostgresChunkContentRepository", action="get_chunk_contents_by_ids", company_id=str(company_id), num_requested_ids=len(chunk_ids_with_page), request_id=get_request_id_from_context())
        if not chunk_ids_with_page:
            return {}

        parsed_conditions: List[Tuple[uuid.UUID, int]] = []
        # Maps (doc_uuid_str, page_num) back to the original composite chunk_id for result mapping
        query_key_to_original_id_map: Dict[Tuple[str, int], str] = {}

        for composite_id_str in chunk_ids_with_page:
            if not isinstance(composite_id_str, str) or "_" not in composite_id_str:
                repo_log.warning("Skipping invalid composite_id_str format", composite_id_str=composite_id_str)
                continue
            
            parts = composite_id_str.rsplit("_", 1) # Split on the last underscore
            if len(parts) != 2:
                repo_log.warning("Skipping composite_id_str with unexpected part count after split", composite_id_str=composite_id_str, parts_len=len(parts))
                continue

            doc_id_str, page_num_str = parts
            try:
                doc_uuid = uuid.UUID(doc_id_str)
                page_num = int(page_num_str)
                
                query_key = (str(doc_uuid), page_num)
                if query_key not in query_key_to_original_id_map: # Avoid duplicate query conditions
                    parsed_conditions.append((doc_uuid, page_num))
                    query_key_to_original_id_map[query_key] = composite_id_str
                # If query_key already exists, it means multiple original_ids map to the same (doc,page)
                # We'll use the first original_id encountered for that (doc,page) as the key in the result.

            except ValueError:
                repo_log.warning("Skipping composite_id_str due to parsing error (ValueError)", composite_id_str=composite_id_str)
                continue
        
        if not parsed_conditions:
            repo_log.warning("No valid (document_id, page_number) pairs to query after parsing.")
            return {}

        # Build OR conditions for the WHERE clause: (doc_id = X AND page_num = Y) OR (doc_id = A AND page_num = B) ...
        # This query is simplified. In a real scenario with multiple chunks per page,
        # this would retrieve all chunks for that (doc_id, page_number) combination.
        # The current logic assumes one dominant chunk per (doc_id, page_number) for content retrieval,
        # or that the downstream process handles multiple chunks mapping to the same (doc,page) if that's possible.
        # The original composite ID (e.g., docid_page_chunkidx) from Milvus/retriever might be more precise if chunks
        # are stored with a sub-page index in Postgres. For now, we query by doc_id and page_number.
        
        # Fetching chunk's own UUID (pk_id), content, title, file_name, document_type, document_id, page_number
        # Note: We are querying for a specific page of a document. If multiple chunks exist for that exact page,
        # this query *might* return multiple if not further distinguished.
        # The current system design (chunk_id = doc_id_page) implies one main chunk of content per page from Milvus.
        # If there are multiple chunks per (doc_id, page_number) in `document_chunks` table,
        # the one with the matching `document_id` and `page_number` will be picked (could be multiple if that pair isn't unique for content).
        # The provided log "Invalid UUID format in chunk_ids list" for `get_chunk_contents_by_ids` in the previous turn
        # implies that `chunk_ids` were being treated as the primary key of `document_chunks`, which is usually a UUID.
        # The fix is to use document_id and page_number for querying, and map back.
        # The table `document_chunks` should have columns `id` (UUID, PK), `document_id` (UUID, FK), `page_number` (int), `content`, etc.

        sql_conditions_list = []
        for i, (doc_uuid, page_num) in enumerate(parsed_conditions):
            # Parameters $1=company_id, $2=doc_uuid_1, $3=page_num_1, $4=doc_uuid_2, $5=page_num_2 ...
            sql_conditions_list.append(f"(dc.document_id = ${2 + i*2} AND dc.page_number = ${3 + i*2})")
        
        sql_filter_expression = " OR ".join(sql_conditions_list)

        query = f"""
            SELECT 
                dc.document_id, 
                dc.page_number,
                dc.content, 
                dc.title,
                d.file_name, -- Assuming 'documents' table alias 'd'
                d.document_type
            FROM document_chunks dc
            JOIN documents d ON dc.document_id = d.id
            WHERE dc.company_id = $1 AND ({sql_filter_expression});
        """
        
        query_params = [company_id]
        for doc_uuid, page_num in parsed_conditions:
            query_params.extend([doc_uuid, page_num])

        chunk_contents_map: Dict[str, RetrievedChunk] = {}
        pool = await get_db_pool()
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(query, *query_params)
            
            repo_log.debug(f"DB query for chunk contents returned {len(rows)} rows.")

            for row in rows:
                # Key for mapping back to the original composite ID
                map_key = (str(row['document_id']), row['page_number'])
                original_composite_id = query_key_to_original_id_map.get(map_key)

                if original_composite_id:
                    # If multiple rows match the same (doc_id, page_number), the last one processed will overwrite.
                    # This implies that (company_id, document_id, page_number) should ideally yield one primary chunk content.
                    # If there are multiple chunks per page stored in document_chunks,
                    # this logic will pick one. The original system needs to be clear on how it
                    # uniquely identifies a content chunk if page_number alone is not sufficient.
                    # For now, we assume this query correctly identifies the relevant chunk content.
                    chunk_contents_map[original_composite_id] = RetrievedChunk(
                        id=original_composite_id, # Use the ID from Milvus/retriever
                        content=row['content'],
                        score=None, # Score is not fetched here, will be from retriever
                        metadata={ # Populate metadata based on what's fetched
                            "title": row['title'],
                            "page": row['page_number'], # from dc.page_number
                            "file_name": row['file_name'],
                            "document_type": row['document_type'].value if row['document_type'] else None,
                            "document_id": str(row['document_id']), # from dc.document_id
                            "company_id": str(company_id)
                        },
                        document_id=str(row['document_id']),
                        file_name=row['file_name'],
                        company_id=str(company_id) # company_id is an input to this function
                    )
                else:
                    repo_log.warning("Found chunk content in DB but could not map back to an original composite ID.", db_doc_id=str(row['document_id']), db_page=row['page_number'])
            
            repo_log.info(f"Successfully retrieved content for {len(chunk_contents_map)} (doc_id, page_number) combinations out of {len(parsed_conditions)} unique requested pairs.")

        except Exception as e:
            repo_log.exception("Failed to get chunk contents by composite IDs")
            raise
        # LLM_PROCESS_END_get_chunk_contents_by_ids_MODIFIED
        return chunk_contents_map