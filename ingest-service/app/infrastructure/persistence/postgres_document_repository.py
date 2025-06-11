import uuid
from typing import Optional, Tuple, List, Dict, Any
from datetime import datetime, timezone, date
from sqlalchemy import text, update, insert, delete, select, func, and_

from app.domain.entities import Document
from app.domain.enums import DocumentStatus
from app.application.ports.document_repository_port import DocumentRepositoryPort
from .postgres_connector import get_db_pool, get_sync_engine, metadata_obj
import structlog

log = structlog.get_logger(__name__)

documents_table = metadata_obj.tables.get('documents') # Assume table 'documents' is defined elsewhere or reflected

class PostgresDocumentRepositoryAdapter(DocumentRepositoryPort):

    async def save(self, document: Document) -> None:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO documents (id, company_id, file_name, file_type, file_path, metadata, status, chunk_count, error_message, uploaded_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                document.id, document.company_id, document.file_name, document.file_type, document.file_path,
                document.metadata or {}, document.status.value, document.chunk_count or 0, document.error_message,
                document.uploaded_at or datetime.now(timezone.utc),
                document.updated_at or datetime.now(timezone.utc)
            )
        log.info("Document saved (async)", document_id=document.id)


    async def find_by_id(self, doc_id: uuid.UUID, company_id: uuid.UUID) -> Optional[Document]:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM documents WHERE id = $1 AND company_id = $2", doc_id, company_id
            )
            return Document(**dict(row)) if row else None

    def find_by_id_sync(self, doc_id: uuid.UUID, company_id: uuid.UUID) -> Optional[Document]:
        engine = get_sync_engine()
        with engine.connect() as conn:
            # Ensure documents_table is available for sync operations if not reflected globally
            # This might require reflecting it or ensuring it's defined for SQLAlchemy sync context
            # For simplicity, assuming a direct text query if documents_table isn't readily usable here
            stmt = text("SELECT * FROM documents WHERE id = :doc_id AND company_id = :company_id")
            result = conn.execute(stmt, {"doc_id": doc_id, "company_id": company_id}).fetchone()
            return Document(**result._asdict()) if result else None


    async def find_by_name_and_company(self, filename: str, company_id: uuid.UUID) -> Optional[Document]:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM documents WHERE file_name = $1 AND company_id = $2", filename, company_id
            )
            return Document(**dict(row)) if row else None

    async def update_status(self, doc_id: uuid.UUID, status: DocumentStatus, chunk_count: Optional[int] = None, error_message: Optional[str] = None, updated_at: Optional[datetime] = None) -> bool:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            values_to_set = {"status": status.value, "updated_at": updated_at or datetime.now(timezone.utc)}
            if chunk_count is not None:
                values_to_set["chunk_count"] = chunk_count
            if status == DocumentStatus.ERROR:
                 values_to_set["error_message"] = error_message
            else: # Clear error message if not an error status
                 values_to_set["error_message"] = None

            set_clauses = [f"{key} = ${i+2}" for i, key in enumerate(values_to_set.keys())]
            params = [doc_id] + list(values_to_set.values())
            
            query = f"UPDATE documents SET {', '.join(set_clauses)} WHERE id = $1"
            result = await conn.execute(query, *params)
            log.info("Document status updated (async)", document_id=doc_id, new_status=status.value, result=result)
            return result == 'UPDATE 1'


    def update_status_sync(self, doc_id: uuid.UUID, status: DocumentStatus, chunk_count: Optional[int] = None, error_message: Optional[str] = None, updated_at: Optional[datetime] = None) -> bool:
        engine = get_sync_engine()
        with engine.connect() as conn:
            values_to_set = {"status": status.value, "updated_at": updated_at or datetime.now(timezone.utc)}
            if chunk_count is not None:
                values_to_set["chunk_count"] = chunk_count
            if status == DocumentStatus.ERROR:
                values_to_set["error_message"] = error_message
            else:
                values_to_set["error_message"] = None
            
            # Assuming documents_table is reflected or available for SQLAlchemy Core
            # If not, use text() query similar to async version but with SQLAlchemy
            stmt = update(metadata_obj.tables['documents']).where(metadata_obj.tables['documents'].c.id == doc_id).values(**values_to_set)
            result = conn.execute(stmt)
            conn.commit()
            log.info("Document status updated (sync)", document_id=doc_id, new_status=status.value, rowcount=result.rowcount)
            return result.rowcount == 1


    async def list_paginated(self, company_id: uuid.UUID, limit: int, offset: int) -> Tuple[List[Document], int]:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM documents WHERE company_id = $1 ORDER BY uploaded_at DESC LIMIT $2 OFFSET $3",
                company_id, limit, offset
            )
            count_row = await conn.fetchrow(
                "SELECT COUNT(*) as total FROM documents WHERE company_id = $1", company_id
            )
            total_count = count_row['total'] if count_row else 0
            return [Document(**dict(row)) for row in rows], total_count


    async def delete(self, doc_id: uuid.UUID, company_id: uuid.UUID) -> bool:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM documents WHERE id = $1 AND company_id = $2", doc_id, company_id
            )
            log.info("Document deleted (async)", document_id=doc_id, result=result)
            return result == 'DELETE 1'

    def delete_sync(self, doc_id: uuid.UUID, company_id: uuid.UUID) -> bool:
        engine = get_sync_engine()
        with engine.connect() as conn:
            stmt = delete(metadata_obj.tables['documents']).where(
                and_(metadata_obj.tables['documents'].c.id == doc_id, metadata_obj.tables['documents'].c.company_id == company_id)
            )
            result = conn.execute(stmt)
            conn.commit()
            log.info("Document deleted (sync)", document_id=doc_id, rowcount=result.rowcount)
            return result.rowcount == 1

    async def get_stats(self, company_id: uuid.UUID, from_date: Optional[date], to_date: Optional[date], status_filter: Optional[DocumentStatus]) -> Dict[str, Any]:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            params: List[Any] = [company_id]
            param_idx = 2
            
            base_query_where = "company_id = $1"
            if from_date:
                base_query_where += f" AND uploaded_at >= ${param_idx}"
                params.append(from_date)
                param_idx += 1
            if to_date:
                base_query_where += f" AND DATE(uploaded_at) <= ${param_idx}"
                params.append(to_date)
                param_idx += 1
            if status_filter:
                base_query_where += f" AND status = ${param_idx}"
                params.append(status_filter.value)
                param_idx += 1

            total_docs_query = f"SELECT COUNT(*) FROM documents WHERE {base_query_where};"
            total_documents = await conn.fetchval(total_docs_query, *params) or 0

            chunks_where_sql = base_query_where
            chunks_params = list(params)
            current_param_idx_chunks = param_idx

            if not (status_filter and status_filter != DocumentStatus.PROCESSED):
                if not status_filter: # Add 'processed' if no status filter was applied originally
                    chunks_where_sql += f" AND status = ${current_param_idx_chunks}"
                    chunks_params.append(DocumentStatus.PROCESSED.value)
                total_chunks_query = f"SELECT SUM(chunk_count) FROM documents WHERE {chunks_where_sql};"
                total_chunks_processed = await conn.fetchval(total_chunks_query, *chunks_params) or 0
            else: # If filtered by a status that is not 'processed'
                total_chunks_processed = 0


            by_status_query = f"SELECT status, COUNT(*) as count FROM documents WHERE {base_query_where} GROUP BY status;"
            status_rows = await conn.fetch(by_status_query, *params)
            
            by_type_query = f"SELECT file_type, COUNT(*) as count FROM documents WHERE {base_query_where} GROUP BY file_type;"
            type_rows = await conn.fetch(by_type_query, *params)

            dates_query = f"SELECT MIN(uploaded_at) as oldest, MAX(uploaded_at) as newest FROM documents WHERE {base_query_where};"
            date_row = await conn.fetchrow(dates_query, *params)

            return {
                "total_documents": total_documents,
                "total_chunks_processed": total_chunks_processed,
                "status_rows": [dict(row) for row in status_rows], # Let use case format this
                "type_rows": [dict(row) for row in type_rows],     # Let use case format this
                "oldest_document_date": date_row['oldest'] if date_row else None,
                "newest_document_date": date_row['newest'] if date_row else None,
            }