from typing import List, Dict, Any
import uuid
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.domain.entities import Chunk
from app.application.ports.chunk_repository_port import ChunkRepositoryPort
from .postgres_connector import get_sync_engine, document_chunks_table
import structlog

log = structlog.get_logger(__name__)

class PostgresChunkRepositoryAdapter(ChunkRepositoryPort):
    def save_bulk(self, chunks: List[Chunk]) -> int:
        if not chunks:
            log.warning("PostgresChunkRepositoryAdapter.save_bulk called with empty list of chunks.")
            return 0

        engine = get_sync_engine()
        
        prepared_data: List[Dict[str, Any]] = []
        for chunk_obj in chunks:
            prepared_chunk = {
                "id": chunk_obj.id or uuid.uuid4(), # Generate UUID if not provided
                "document_id": chunk_obj.document_id,
                "company_id": chunk_obj.company_id,
                "chunk_index": chunk_obj.chunk_index,
                "content": chunk_obj.content,
                "metadata": chunk_obj.metadata.model_dump(mode='json') if chunk_obj.metadata else None, # Ensure metadata is dict
                "embedding_id": chunk_obj.embedding_id,
                "vector_status": chunk_obj.vector_status.value,
                # created_at is handled by server_default in DB schema
            }
            prepared_data.append(prepared_chunk)
        
        insert_log = log.bind(component="PostgresChunkRepo", num_chunks=len(prepared_data), document_id=str(chunks[0].document_id))
        insert_log.info("Attempting synchronous bulk insert of document chunks.")

        inserted_count = 0
        try:
            with engine.connect() as connection:
                with connection.begin(): # Start transaction
                    stmt = pg_insert(document_chunks_table).values(prepared_data)
                    # ON CONFLICT DO NOTHING to avoid errors if a chunk (doc_id, chunk_index) already exists
                    # This assumes 'uq_document_chunk_index' (document_id, chunk_index) is the unique constraint
                    stmt = stmt.on_conflict_do_update(
                        index_elements=[document_chunks_table.c.document_id, document_chunks_table.c.chunk_index],
                        set_={
                            'content': stmt.excluded.content,
                            'metadata': stmt.excluded.metadata,
                            'embedding_id': stmt.excluded.embedding_id,
                            'vector_status': stmt.excluded.vector_status,
                            # 'created_at' should not be updated on conflict typically
                        }
                    )
                    result = connection.execute(stmt)
                    # rowcount might not be reliable for INSERT ... ON CONFLICT for reflecting actual inserts vs updates.
                    # For simplicity, we'll assume if it runs without error, all intended operations (insert or update) were attempted.
                    # A more accurate count would require querying after or using RETURNING (more complex).
                inserted_count = len(prepared_data) 
                insert_log.info("Bulk insert/update statement for chunks executed successfully.", 
                                intended_count=len(prepared_data), reported_rowcount=result.rowcount if result else -1)
                return inserted_count
        except Exception as e:
            insert_log.error("Error during synchronous bulk chunk insert", error=str(e), exc_info=True)
            raise RuntimeError(f"Failed to bulk insert/update chunks: {e}") from e