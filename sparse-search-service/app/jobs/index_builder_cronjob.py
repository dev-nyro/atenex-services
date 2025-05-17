# sparse-search-service/app/jobs/index_builder_cronjob.py
import argparse
import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path
import sys
import pickle # Para serializar
from typing import List # <--- CORRECCIÓN: Importación añadida

try:
    # LLM: CORRECTION - Importar de rank_bm25
    from rank_bm25 import BM25Okapi, BM25Plus
except ImportError:
    BM25Okapi = None
    BM25Plus = None
    print("ERROR: rank_bm25 library not found. Please install it: poetry add rank_bm25", file=sys.stderr)
    sys.exit(1)

import structlog

if __name__ == '__main__':
    SCRIPT_DIR = Path(__file__).resolve().parent
    PROJECT_ROOT = SCRIPT_DIR.parent.parent 
    sys.path.insert(0, str(PROJECT_ROOT))


from app.core.config import settings as app_settings 
from app.core.logging_config import setup_logging as app_setup_logging
from app.infrastructure.persistence.postgres_repositories import PostgresChunkContentRepository
from app.infrastructure.persistence import postgres_connector
from app.infrastructure.storage.gcs_index_storage_adapter import GCSIndexStorageAdapter, GCSIndexStorageError
from app.infrastructure.sparse_retrieval.bm25_adapter import BM25Adapter # Sigue siendo útil para dump/load
from typing import Optional # Añadido para la firma de main_builder_logic

# LLM: CORRECTION - Usar la implementación definida en bm25_adapter o una aquí
BM25_IMPLEMENTATION_FOR_BUILDER = BM25Okapi 

app_setup_logging() 
log = structlog.get_logger("index_builder_cronjob")


async def build_and_upload_index_for_company(
    company_id_str: str,
    repo: PostgresChunkContentRepository,
    gcs_adapter: GCSIndexStorageAdapter
):
    builder_log = log.bind(company_id=company_id_str, job_action="build_and_upload_index")
    builder_log.info("Starting index build process for company.")
    
    try:
        company_uuid = uuid.UUID(company_id_str)
    except ValueError:
        builder_log.error("Invalid company_id format. Skipping.", company_id_input=company_id_str)
        return

    builder_log.debug("Fetching chunks from PostgreSQL...")
    try:
        chunks_data = await repo.get_chunks_with_metadata_by_company(company_uuid)
    except ConnectionError as e_db_conn:
        builder_log.error("Database connection error while fetching chunks. Skipping company.", error=str(e_db_conn))
        return
    except Exception as e_db_fetch:
        builder_log.exception("Failed to fetch chunks from PostgreSQL. Skipping company.")
        return
        
    if not chunks_data:
        builder_log.warning("No processable chunks found for company. Skipping index build.")
        return

    corpus_texts_full = [chunk['content'] for chunk in chunks_data if chunk.get('content','').strip()]
    # LLM: CORRECTION - Tokenizar el corpus para rank_bm25
    corpus_texts_tokenized = [text.lower().split() for text in corpus_texts_full]
    id_map = [chunk['id'] for chunk in chunks_data if chunk.get('content','').strip()] 

    if not corpus_texts_tokenized: # LLM: CORRECTION - Verificar corpus tokenizado
        builder_log.warning("Corpus is empty after filtering and tokenizing content. Skipping index build.")
        return

    builder_log.info(f"Building BM25 index for {len(corpus_texts_tokenized)} chunks...")
    try:
        # LLM: CORRECTION - Instanciar y usar rank_bm25
        if not BM25_IMPLEMENTATION_FOR_BUILDER:
            raise RuntimeError("rank_bm25 is not available for building index.")
        
        # BM25Okapi(corpus_tokenizado)
        retriever = BM25_IMPLEMENTATION_FOR_BUILDER(corpus_texts_tokenized)
        # El método .index() no existe en rank_bm25 de la misma forma. La indexación ocurre al instanciar.
        builder_log.info(f"BM25 index ({BM25_IMPLEMENTATION_FOR_BUILDER.__name__}) built successfully.")
    except Exception as e_bm25_index:
        builder_log.exception("Error during BM25 index building.")
        return


    with tempfile.TemporaryDirectory(prefix=f"bm25_build_{company_id_str}_") as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        bm2s_file_path = tmpdir / "bm25_index.bm2s" # El nombre del archivo puede mantenerse por consistencia
        id_map_file_path = tmpdir / "id_map.json"

        builder_log.debug("Dumping BM25 index to temporary file.", file_path=str(bm2s_file_path))
        try:
            # LLM: CORRECTION - BM25Adapter ahora usa pickle, y su método estático es útil aquí
            BM25Adapter.dump_bm2s_to_file(retriever, str(bm2s_file_path))
        except Exception as e_dump:
            builder_log.exception("Failed to dump BM25 index.")
            return

        builder_log.debug("Saving ID map to temporary JSON file.", file_path=str(id_map_file_path))
        try:
            with open(id_map_file_path, 'w') as f:
                json.dump(id_map, f)
        except IOError as e_json_io:
            builder_log.exception("Failed to save ID map JSON.")
            return
        
        builder_log.info("Index and ID map saved to temporary local files. Uploading to GCS...")
        try:
            await gcs_adapter.save_index_files(company_uuid, str(bm2s_file_path), str(id_map_file_path))
            builder_log.info("Index and ID map uploaded to GCS successfully.")
        except GCSIndexStorageError as e_gcs_upload:
            builder_log.error("Failed to upload index files to GCS.", error=str(e_gcs_upload), exc_info=True)
        except Exception as e_gcs_generic:
            builder_log.exception("Unexpected error during GCS upload.")

async def get_all_active_company_ids(repo: PostgresChunkContentRepository) -> List[uuid.UUID]:
    fetch_log = log.bind(job_action="fetch_active_companies")
    fetch_log.info("Fetching active company IDs from database...")
    query = "SELECT DISTINCT company_id FROM documents WHERE status = 'processed';" 
    pool = await postgres_connector.get_db_pool()
    conn = None
    try:
        conn = await pool.acquire()
        rows = await conn.fetch(query)
        company_ids = [row['company_id'] for row in rows if row['company_id']]
        fetch_log.info(f"Found {len(company_ids)} active company IDs with processed documents.")
        return company_ids
    except Exception as e:
        fetch_log.exception("Failed to fetch active company IDs.")
        return []
    finally:
        if conn:
            await pool.release(conn)


async def main_builder_logic(target_company_id_str: Optional[str]):
    log.info("Index Builder CronJob starting...", target_company=target_company_id_str or "ALL")
    
    # LLM: CORRECTION - Verificar si la librería está disponible
    if not BM25Okapi and not BM25Plus: # Si ninguna implementación está disponible
        log.critical("rank_bm25 library or its classes (BM25Okapi, BM25Plus) are not available. Index builder cannot run.")
        return

    await postgres_connector.get_db_pool() 
    repo = PostgresChunkContentRepository()
    
    gcs_bucket_for_indices = app_settings.SPARSE_INDEX_GCS_BUCKET_NAME
    if not gcs_bucket_for_indices:
        log.critical("SPARSE_INDEX_GCS_BUCKET_NAME is not configured. Cannot proceed.")
        await postgres_connector.close_db_pool()
        return
        
    gcs_adapter = GCSIndexStorageAdapter(bucket_name=gcs_bucket_for_indices)

    companies_to_process: List[str] = []

    if target_company_id_str and target_company_id_str.upper() != "ALL":
        companies_to_process.append(target_company_id_str)
    else:
        log.info("Target is ALL companies. Fetching list of active company IDs...")
        active_company_uuids = await get_all_active_company_ids(repo)
        companies_to_process = [str(uid) for uid in active_company_uuids]
        if not companies_to_process:
            log.info("No active companies found to process.")

    log.info(f"Will process indices for {len(companies_to_process)} companies.", companies_list_preview=companies_to_process[:5])

    for comp_id_str in companies_to_process:
        await build_and_upload_index_for_company(comp_id_str, repo, gcs_adapter)

    await postgres_connector.close_db_pool()
    log.info("Index Builder CronJob finished.")

if __name__ == "__main__":
    # LLM: CORRECTION - Verificar disponibilidad antes de parsear args
    if not BM25Okapi and not BM25Plus:
        print("FATAL: rank_bm25 library (BM25Okapi or BM25Plus) is required but not found.", file=sys.stderr)
        sys.exit(1)

    parser = argparse.ArgumentParser(description="BM25 Index Builder for Sparse Search Service.")
    parser.add_argument(
        "--company-id",
        type=str,
        default="ALL", 
        help="UUID of the company to build index for, or 'ALL' for all active companies."
    )
    args = parser.parse_args()

    asyncio.run(main_builder_logic(args.company_id))