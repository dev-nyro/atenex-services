# sparse-search-service/app/infrastructure/storage/gcs_index_storage_adapter.py
import asyncio
import json
import tempfile
import uuid
from pathlib import Path
from typing import Tuple, Optional
import functools # <--- CORRECCIÓN: Importación añadida

import structlog
from google.cloud import storage
from google.api_core.exceptions import NotFound, GoogleAPIError

from app.application.ports.sparse_index_storage_port import SparseIndexStoragePort
from app.core.config import settings

log = structlog.get_logger(__name__)

BM25_DUMP_FILENAME = "bm25_index.bm2s"
ID_MAP_FILENAME = "id_map.json"
GCS_INDEX_ROOT_PATH = "indices" 

class GCSIndexStorageError(Exception):
    pass

class GCSIndexStorageAdapter(SparseIndexStoragePort):
    def __init__(self, bucket_name: Optional[str] = None):
        self.bucket_name = bucket_name or settings.SPARSE_INDEX_GCS_BUCKET_NAME
        try:
            self._client = storage.Client()
            self._bucket = self._client.bucket(self.bucket_name)
        except Exception as e:
            log.critical("Failed to initialize GCS client or bucket handle.", error=str(e), exc_info=True)
            raise GCSIndexStorageError(f"Failed to initialize GCS client for bucket '{self.bucket_name}': {e}") from e
        self.log = log.bind(gcs_bucket=self.bucket_name, adapter="GCSIndexStorageAdapter")

    def _get_gcs_object_path(self, company_id: uuid.UUID, filename: str) -> str:
        return f"{GCS_INDEX_ROOT_PATH}/{str(company_id)}/{filename}"

    async def load_index_files(self, company_id: uuid.UUID) -> Tuple[Optional[str], Optional[str]]:
        adapter_log = self.log.bind(company_id=str(company_id), action="load_index_files")
        adapter_log.info("Attempting to load index files from GCS.")

        temp_dir = tempfile.mkdtemp(prefix=f"sparse_idx_{company_id}_")
        local_bm2s_path = Path(temp_dir) / BM25_DUMP_FILENAME
        local_id_map_path = Path(temp_dir) / ID_MAP_FILENAME

        gcs_bm2s_object_path = self._get_gcs_object_path(company_id, BM25_DUMP_FILENAME)
        gcs_id_map_object_path = self._get_gcs_object_path(company_id, ID_MAP_FILENAME)

        loop = asyncio.get_running_loop()

        async def _download_file(gcs_path: str, local_path: Path) -> bool:
            try:
                blob = self._bucket.blob(gcs_path)
                await loop.run_in_executor(None, blob.download_to_filename, str(local_path))
                adapter_log.debug(f"Successfully downloaded GCS object to local file.", gcs_object=gcs_path, local_file=str(local_path))
                return True
            except NotFound:
                adapter_log.warning(f"GCS object not found.", gcs_object=gcs_path)
                return False
            except GoogleAPIError as e:
                adapter_log.error(f"GCS API error downloading object.", gcs_object=gcs_path, error=str(e))
                return False
            except Exception as e:
                adapter_log.exception(f"Unexpected error downloading GCS object.", gcs_object=gcs_path)
                return False

        bm2s_downloaded = await _download_file(gcs_bm2s_object_path, local_bm2s_path)
        id_map_downloaded = await _download_file(gcs_id_map_object_path, local_id_map_path)

        if bm2s_downloaded and id_map_downloaded:
            adapter_log.info("Both index files downloaded successfully from GCS.")
            return str(local_bm2s_path), str(local_id_map_path)
        else:
            adapter_log.warning("Failed to download one or both index files from GCS. Cleaning up temporary files.")
            try:
                if local_bm2s_path.exists(): local_bm2s_path.unlink()
                if local_id_map_path.exists(): local_id_map_path.unlink()
                Path(temp_dir).rmdir()
            except OSError as e_clean:
                adapter_log.error("Error cleaning up temporary directory.", temp_dir=temp_dir, error=str(e_clean))
            return None, None

    async def save_index_files(self, company_id: uuid.UUID, local_bm2s_dump_path: str, local_id_map_path: str) -> None:
        adapter_log = self.log.bind(company_id=str(company_id), action="save_index_files")
        adapter_log.info("Attempting to save index files to GCS.")

        gcs_bm2s_object_path = self._get_gcs_object_path(company_id, BM25_DUMP_FILENAME)
        gcs_id_map_object_path = self._get_gcs_object_path(company_id, ID_MAP_FILENAME)

        loop = asyncio.get_running_loop()

        async def _upload_file(local_path: str, gcs_path: str, content_type: Optional[str] = None):
            try:
                blob = self._bucket.blob(gcs_path)
                # <--- CORRECCIÓN: Usar functools.partial para pasar content_type ---
                upload_func = functools.partial(blob.upload_from_filename, local_path, content_type=content_type)
                await loop.run_in_executor(None, upload_func)
                # --- Fin de la corrección ---
                adapter_log.debug(f"Successfully uploaded local file to GCS object.", local_file=local_path, gcs_object=gcs_path)
            except GoogleAPIError as e:
                adapter_log.error(f"GCS API error uploading file.", local_file=local_path, gcs_object=gcs_path, error=str(e))
                raise GCSIndexStorageError(f"GCS API error uploading {local_path} to {gcs_path}: {e}") from e
            except Exception as e:
                adapter_log.exception(f"Unexpected error uploading file to GCS.", local_file=local_path, gcs_object=gcs_path)
                raise GCSIndexStorageError(f"Unexpected error uploading {local_path} to {gcs_path}: {e}") from e

        try:
            await _upload_file(local_bm2s_dump_path, gcs_bm2s_object_path, content_type="application/octet-stream")
            await _upload_file(local_id_map_path, gcs_id_map_object_path, content_type="application/json")
            adapter_log.info("Both index files uploaded successfully to GCS.")
        except GCSIndexStorageError: 
            raise
        except Exception as e: 
            adapter_log.exception("Unexpected failure during save_index_files orchestration.")
            raise GCSIndexStorageError(f"Orchestration failure in save_index_files: {e}") from e