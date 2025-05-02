import structlog
import asyncio
from typing import Optional
from google.cloud import storage
from google.api_core.exceptions import NotFound, GoogleAPIError
from app.core.config import settings

log = structlog.get_logger(__name__)

class GCSClientError(Exception):
    """Custom exception for GCS related errors."""
    def __init__(self, message: str, original_exception: Optional[Exception] = None):
        self.message = message
        self.original_exception = original_exception
        super().__init__(message)

    def __str__(self):
        if self.original_exception:
            return f"{self.message}: {type(self.original_exception).__name__} - {str(self.original_exception)}"
        return self.message

class GCSClient:
    """Client to interact with Google Cloud Storage using configured settings."""
    def __init__(self, bucket_name: Optional[str] = None):
        self.bucket_name = bucket_name or settings.GCS_BUCKET_NAME
        self._client = storage.Client()
        self._bucket = self._client.bucket(self.bucket_name)
        self.log = log.bind(gcs_bucket=self.bucket_name)

    async def upload_file_async(self, object_name: str, data: bytes, content_type: str) -> str:
        self.log.info("Uploading file to GCS...", object_name=object_name, content_type=content_type, length=len(data))
        loop = asyncio.get_running_loop()
        def _upload():
            blob = self._bucket.blob(object_name)
            blob.upload_from_string(data, content_type=content_type)
            return object_name
        try:
            uploaded_object_name = await loop.run_in_executor(None, _upload)
            self.log.info("File uploaded successfully to GCS", object_name=object_name)
            return uploaded_object_name
        except GoogleAPIError as e:
            self.log.error("GCS upload failed", error=str(e))
            raise GCSClientError(f"GCS error uploading {object_name}", e) from e
        except Exception as e:
            self.log.exception("Unexpected error during GCS upload", error=str(e))
            raise GCSClientError(f"Unexpected error uploading {object_name}", e) from e

    async def download_file_async(self, object_name: str, file_path: str):
        self.log.info("Downloading file from GCS...", object_name=object_name, target_path=file_path)
        loop = asyncio.get_running_loop()
        def _download():
            blob = self._bucket.blob(object_name)
            blob.download_to_filename(file_path)
        try:
            await loop.run_in_executor(None, _download)
            self.log.info("File downloaded successfully from GCS", object_name=object_name)
        except NotFound as e:
            self.log.error("Object not found in GCS", object_name=object_name)
            raise GCSClientError(f"Object not found in GCS: {object_name}", e) from e
        except GoogleAPIError as e:
            self.log.error("GCS download failed", error=str(e))
            raise GCSClientError(f"GCS error downloading {object_name}", e) from e
        except Exception as e:
            self.log.exception("Unexpected error during GCS download", error=str(e))
            raise GCSClientError(f"Unexpected error downloading {object_name}", e) from e

    async def check_file_exists_async(self, object_name: str) -> bool:
        self.log.debug("Checking file existence in GCS", object_name=object_name)
        loop = asyncio.get_running_loop()
        def _exists():
            blob = self._bucket.blob(object_name)
            return blob.exists()
        try:
            exists = await loop.run_in_executor(None, _exists)
            self.log.debug("File existence check completed in GCS", object_name=object_name, exists=exists)
            return exists
        except Exception as e:
            self.log.exception("Unexpected error during GCS existence check", error=str(e))
            return False

    async def delete_file_async(self, object_name: str):
        self.log.info("Deleting file from GCS...", object_name=object_name)
        loop = asyncio.get_running_loop()
        def _delete():
            blob = self._bucket.blob(object_name)
            blob.delete()
        try:
            await loop.run_in_executor(None, _delete)
            self.log.info("File deleted successfully from GCS", object_name=object_name)
        except NotFound:
            self.log.info("Object already deleted or not found in GCS", object_name=object_name)
        except GoogleAPIError as e:
            self.log.error("GCS delete failed", error=str(e))
            raise GCSClientError(f"GCS error deleting {object_name}", e) from e
        except Exception as e:
            self.log.exception("Unexpected error during GCS delete", error=str(e))
            raise GCSClientError(f"Unexpected error deleting {object_name}", e) from e

    # Synchronous methods for worker compatibility
    def download_file_sync(self, object_name: str, file_path: str):
        self.log.info("Downloading file from GCS (sync)...", object_name=object_name, target_path=file_path)
        try:
            blob = self._bucket.blob(object_name)
            blob.download_to_filename(file_path)
            self.log.info("File downloaded successfully from GCS (sync)", object_name=object_name)
        except NotFound as e:
            self.log.error("Object not found in GCS (sync)", object_name=object_name)
            raise GCSClientError(f"Object not found in GCS: {object_name}", e) from e
        except GoogleAPIError as e:
            self.log.error("GCS download failed (sync)", error=str(e))
            raise GCSClientError(f"GCS error downloading {object_name}", e) from e
        except Exception as e:
            self.log.exception("Unexpected error during GCS download (sync)", error=str(e))
            raise GCSClientError(f"Unexpected error downloading {object_name}", e) from e

    def check_file_exists_sync(self, object_name: str) -> bool:
        self.log.debug("Checking file existence in GCS (sync)", object_name=object_name)
        try:
            blob = self._bucket.blob(object_name)
            exists = blob.exists()
            self.log.debug("File existence check completed in GCS (sync)", object_name=object_name, exists=exists)
            return exists
        except Exception as e:
            self.log.exception("Unexpected error during GCS existence check (sync)", error=str(e))
            return False

    def delete_file_sync(self, object_name: str):
        self.log.info("Deleting file from GCS (sync)...", object_name=object_name)
        try:
            blob = self._bucket.blob(object_name)
            blob.delete()
            self.log.info("File deleted successfully from GCS (sync)", object_name=object_name)
        except NotFound:
            self.log.info("Object already deleted or not found in GCS (sync)", object_name=object_name)
        except GoogleAPIError as e:
            self.log.error("GCS delete failed (sync)", error=str(e))
            raise GCSClientError(f"GCS error deleting {object_name}", e) from e
        except Exception as e:
            self.log.exception("Unexpected error during GCS delete (sync)", error=str(e))
            raise GCSClientError(f"Unexpected error deleting {object_name}", e) from e
