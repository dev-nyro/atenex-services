# ingest-service/app/services/minio_client.py
import io
import uuid
from typing import IO, BinaryIO
from minio import Minio
from minio.error import S3Error
import structlog
import asyncio

from app.core.config import settings

log = structlog.get_logger(__name__)

class MinioStorageClient:
    """Cliente para interactuar con MinIO usando el bucket configurado."""

    def __init__(self):
        self.bucket_name = settings.MINIO_BUCKET_NAME # LLM_COMMENT: Use configured bucket name
        try:
            self.client = Minio(
                settings.MINIO_ENDPOINT,
                access_key=settings.MINIO_ACCESS_KEY.get_secret_value(),
                secret_key=settings.MINIO_SECRET_KEY.get_secret_value(),
                secure=settings.MINIO_USE_SECURE
            )
            # LLM_COMMENT: Bucket creation is synchronous and happens once on init
            self._ensure_bucket_exists()
            log.info("MinIO client initialized", endpoint=settings.MINIO_ENDPOINT, bucket=self.bucket_name)
        except Exception as e:
            log.critical("CRITICAL: Failed to initialize MinIO client", bucket=self.bucket_name, error=str(e), exc_info=True)
            raise RuntimeError(f"MinIO client initialization failed: {e}") from e

    def _ensure_bucket_exists(self):
        """Crea el bucket especificado si no existe (síncrono)."""
        try:
            found = self.client.bucket_exists(self.bucket_name)
            if not found:
                self.client.make_bucket(self.bucket_name)
                log.info(f"MinIO bucket '{self.bucket_name}' created.")
            else:
                log.debug(f"MinIO bucket '{self.bucket_name}' already exists.")
        except S3Error as e:
            log.error(f"Error checking/creating MinIO bucket '{self.bucket_name}'", error=str(e), exc_info=True)
            raise # Re-lanzar para indicar fallo crítico

    # LLM_COMMENT: upload_file remains the same, uses executor for sync MinIO call
    async def upload_file(
        self,
        company_id: uuid.UUID,
        document_id: uuid.UUID,
        file_name: str,
        file_content_stream: IO[bytes],
        content_type: str,
        content_length: int
    ) -> str:
        # LLM_COMMENT: Consistent object naming convention
        object_name = f"{str(company_id)}/{str(document_id)}/{file_name}"
        upload_log = log.bind(bucket=self.bucket_name, object_name=object_name, content_type=content_type, length=content_length)
        upload_log.info("Queueing file upload to MinIO executor")

        loop = asyncio.get_running_loop()
        try:
            file_content_stream.seek(0) # Ensure stream position is at the beginning
            def _put_object():
                # LLM_COMMENT: Wrapper for the synchronous MinIO put_object call
                return self.client.put_object(
                    self.bucket_name,
                    object_name,
                    file_content_stream,
                    content_length,
                    content_type=content_type
                )
            result = await loop.run_in_executor(None, _put_object)
            upload_log.info("File uploaded successfully to MinIO via executor", etag=getattr(result, 'etag', None), version_id=getattr(result, 'version_id', None))
            return object_name
        except S3Error as e:
            upload_log.error("Failed to upload file to MinIO via executor", error=str(e), code=e.code, exc_info=True)
            raise IOError(f"Failed to upload to storage: {e.code}") from e
        except Exception as e:
            upload_log.error("Unexpected error during file upload via executor", error=str(e), exc_info=True)
            raise IOError(f"Unexpected storage upload error") from e

    # LLM_COMMENT: download_file_stream_sync remains for internal use by async wrapper
    def download_file_stream_sync(self, object_name: str) -> io.BytesIO:
        """Operación SÍNCRONA para descargar un archivo a BytesIO."""
        download_log = log.bind(bucket=self.bucket_name, object_name=object_name)
        download_log.info("Downloading file from MinIO (sync operation starting)...")
        response = None
        try:
            response = self.client.get_object(self.bucket_name, object_name)
            file_data = response.read()
            file_stream = io.BytesIO(file_data)
            download_log.info(f"File downloaded successfully from MinIO (sync, {len(file_data)} bytes)")
            file_stream.seek(0)
            return file_stream
        except S3Error as e:
            download_log.error("Failed to download file from MinIO (sync)", error=str(e), code=e.code, exc_info=False)
            if e.code == 'NoSuchKey':
                 raise FileNotFoundError(f"Object not found in MinIO bucket '{self.bucket_name}': {object_name}") from e
            else:
                 raise IOError(f"S3 error downloading file {object_name}: {e.code}") from e
        except Exception as e:
             download_log.error("Unexpected error during sync file download", error=str(e), exc_info=True)
             raise IOError(f"Unexpected error downloading file {object_name}") from e
        finally:
            if response:
                response.close()
                response.release_conn()

    # LLM_COMMENT: download_file_stream remains the async interface, uses executor
    async def download_file_stream(self, object_name: str) -> io.BytesIO:
        """Descarga un archivo de MinIO como BytesIO de forma asíncrona."""
        download_log = log.bind(bucket=self.bucket_name, object_name=object_name)
        download_log.info("Queueing file download from MinIO executor")
        loop = asyncio.get_running_loop()
        try:
            file_stream = await loop.run_in_executor(None, self.download_file_stream_sync, object_name)
            download_log.info("File download successful via executor")
            return file_stream
        except FileNotFoundError:
            download_log.error("File not found in MinIO via executor", object_name=object_name)
            raise
        except Exception as e:
            download_log.error("Error downloading file via executor", error=str(e), error_type=type(e).__name__, exc_info=True)
            raise IOError(f"Failed to download file via executor: {e}") from e

    # LLM_COMMENT: file_exists remains the same, uses executor
    async def file_exists(self, object_name: str) -> bool:
        """Verifica si un objeto existe en MinIO."""
        check_log = log.bind(bucket=self.bucket_name, object_name=object_name)
        loop = asyncio.get_running_loop()
        try:
            def _stat_object():
                # LLM_COMMENT: Synchronous MinIO stat_object call
                return self.client.stat_object(self.bucket_name, object_name)
            await loop.run_in_executor(None, _stat_object)
            check_log.debug("Object found in MinIO") # LLM_COMMENT: Changed level to debug for successful checks
            return True
        except S3Error as e:
            if getattr(e, 'code', None) == 'NoSuchKey':
                check_log.debug("Object does not exist in MinIO", code=e.code) # LLM_COMMENT: Changed level to debug for non-existent files
                return False
            check_log.error("Error checking MinIO object existence (S3Error)", error=str(e), code=e.code)
            # LLM_COMMENT: Propagate error upwards for endpoint to handle
            raise IOError(f"Error checking storage existence: {e.code}") from e
        except Exception as e:
            check_log.error("Unexpected error checking MinIO object existence", error=str(e), exc_info=True)
            raise IOError("Unexpected error checking storage existence") from e

    # LLM_COMMENT: Added delete_file method using executor
    async def delete_file(self, object_name: str) -> None:
        """Elimina un objeto de MinIO de forma asíncrona."""
        delete_log = log.bind(bucket=self.bucket_name, object_name=object_name)
        delete_log.info("Queueing file deletion from MinIO executor")
        loop = asyncio.get_running_loop()
        try:
            def _remove_object():
                # LLM_COMMENT: Synchronous MinIO remove_object call
                self.client.remove_object(self.bucket_name, object_name)
            await loop.run_in_executor(None, _remove_object)
            delete_log.info("File deleted successfully from MinIO via executor")
        except S3Error as e:
            # LLM_COMMENT: Log error but potentially allow process to continue if needed (e.g., if file was already gone)
            delete_log.error("Failed to delete file from MinIO", error=str(e), code=e.code, exc_info=True)
            # LLM_COMMENT: Re-raise as IOError for the endpoint to decide how to handle
            raise IOError(f"Failed to delete from storage: {e.code}") from e
        except Exception as e:
            delete_log.error("Unexpected error during file deletion via executor", error=str(e), exc_info=True)
            raise IOError(f"Unexpected storage deletion error") from e