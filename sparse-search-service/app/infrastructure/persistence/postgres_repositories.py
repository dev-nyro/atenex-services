# sparse-search-service/app/infrastructure/persistence/postgres_repositories.py
import uuid
from typing import Any, Optional, Dict, List
import asyncpg
import structlog

from app.core.config import settings
from app.application.ports.repository_ports import ChunkContentRepositoryPort
from .postgres_connector import get_db_pool

log = structlog.get_logger(__name__) # Logger para este módulo

class PostgresChunkContentRepository(ChunkContentRepositoryPort):
    """
    Implementación concreta para obtener contenido de chunks desde PostgreSQL
    para el Sparse Search Service.
    """

    async def get_chunk_contents_by_company(self, company_id: uuid.UUID) -> Dict[str, str]:
        """
        Obtiene todos los chunks y sus contenidos para una compañía.
        El ID del chunk devuelto es `embedding_id` que se asume es el PK de Milvus.
        """
        repo_log = log.bind(
            repo="PostgresChunkContentRepository",
            action="get_chunk_contents_by_company",
            company_id=str(company_id)
        )
        repo_log.info("Fetching all chunk contents (keyed by embedding_id) for company.")

        # Query para obtener `embedding_id` (clave primaria de Milvus, usada como chunk_id aquí) y `content`
        # Asume que `documents.status = 'processed'` es un buen filtro para chunks válidos.
        query = """
        SELECT dc.embedding_id, dc.content
        FROM document_chunks dc
        JOIN documents d ON dc.document_id = d.id
        WHERE d.company_id = $1
          AND d.status = 'processed'  -- Solo de documentos procesados
          AND dc.embedding_id IS NOT NULL
          AND dc.content IS NOT NULL AND dc.content <> ''; -- Asegurar que hay contenido
        """
        pool = await get_db_pool()
        conn = None
        try:
            conn = await pool.acquire()
            rows = await conn.fetch(query, company_id)
            
            # Crear el diccionario {embedding_id: content}
            # embedding_id es el ID que usa el query-service para referirse a los chunks de Milvus
            # y es el que se espera para la fusión de resultados.
            contents = {row['embedding_id']: row['content'] for row in rows}
            
            repo_log.info(f"Retrieved content for {len(contents)} chunks (keyed by embedding_id).")
            if not contents:
                repo_log.warning("No chunk content found for the company or no documents are processed.", company_id=str(company_id))
            return contents
        except asyncpg.exceptions.PostgresConnectionError as db_conn_err:
            repo_log.error("Database connection error.", error_details=str(db_conn_err), exc_info=False)
            raise ConnectionError(f"Database connection error: {db_conn_err}") from db_conn_err
        except Exception as e:
            repo_log.exception("Failed to get chunk contents by company (keyed by embedding_id).")
            # No relanzar ConnectionError genéricamente, solo para errores de conexión explícitos.
            raise RuntimeError(f"Failed to retrieve chunk contents: {e}") from e
        finally:
            if conn:
                await pool.release(conn)

    async def get_chunks_with_metadata_by_company(
        self, company_id: uuid.UUID
    ) -> List[Dict[str, Any]]:
        """
        Obtiene una lista de chunks para una compañía, cada uno como un diccionario
        que incluye 'id' (el embedding_id/pk_id) y 'content'.
        """
        repo_log = log.bind(
            repo="PostgresChunkContentRepository",
            action="get_chunks_with_metadata_by_company",
            company_id=str(company_id)
        )
        repo_log.info("Fetching chunks with content (ID is embedding_id) for company.")

        query = """
        SELECT
            dc.embedding_id AS id,  -- Renombrar embedding_id a 'id' para consistencia con corpus_chunks
            dc.content
            -- Puedes añadir más metadatos de dc o d aquí si fueran necesarios para BM25
            -- Por ejemplo: dc.document_id, d.file_name
        FROM document_chunks dc
        JOIN documents d ON dc.document_id = d.id
        WHERE d.company_id = $1
          AND d.status = 'processed'
          AND dc.embedding_id IS NOT NULL
          AND dc.content IS NOT NULL AND dc.content <> '';
        """
        pool = await get_db_pool()
        conn = None
        try:
            conn = await pool.acquire()
            rows = await conn.fetch(query, company_id)
            
            # Convertir cada fila a un diccionario
            # El contrato es List[Dict[str, Any]] donde cada Dict tiene 'id' y 'content'
            chunk_list = [{'id': row['id'], 'content': row['content']} for row in rows]
            
            repo_log.info(f"Retrieved {len(chunk_list)} chunks with their content.")
            if not chunk_list:
                repo_log.warning("No chunks with content found for the company or no documents processed.", company_id=str(company_id))
            return chunk_list
        except asyncpg.exceptions.PostgresConnectionError as db_conn_err:
            repo_log.error("Database connection error.", error_details=str(db_conn_err), exc_info=False)
            raise ConnectionError(f"Database connection error: {db_conn_err}") from db_conn_err
        except Exception as e:
            repo_log.exception("Failed to get chunks with metadata by company.")
            raise RuntimeError(f"Failed to retrieve chunks with metadata: {e}") from e
        finally:
            if conn:
                await pool.release(conn)