# ingest-service/app/services/gcs_client.py
import io
import uuid
from typing import Optional
import structlog
import asyncio
from google.cloud import storage
from google.api_core.exceptions import NotFound, Forbidden, GoogleAPIError
from app.core.config import settings

log = structlog.get_logger(__name__)

class GCSError(Exception):
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
    def download_file_sync(self, object_name: str, file_path: str):
        """Synchronously downloads a file from GCS to a local path."""
        try:
            blob = self._bucket.blob(object_name)
            blob.download_to_filename(file_path)
            self.log.info("File downloaded from GCS", object_name=object_name, file_path=file_path)
        except NotFound:
            self.log.error("GCS file not found for download", object_name=object_name)
            raise GCSError("GCS file not found for download")
        except GoogleAPIError as e:
            self.log.error("GCS download failed", error=str(e))
            raise GCSError("GCS download failed", e)
        except Exception as e:
            self.log.error("Unexpected error during GCS download", error=str(e))
            raise GCSError("Unexpected error during GCS download", e)
    """Client to interact with Google Cloud Storage using configured settings."""
    def __init__(self, bucket_name: Optional[str] = None):
        self.bucket_name = bucket_name or settings.GCS_BUCKET_NAME
        self._client: Optional[storage.Client] = None
        self._bucket: Optional[storage.Bucket] = None
        self.log = log.bind(gcs_bucket=self.bucket_name)
        self._initialize_client()

    def _initialize_client(self):
        self.log.debug("Initializing GCS client...")
        try:
            self._client = storage.Client()
            self._bucket = self._client.get_bucket(self.bucket_name)
            self.log.info("GCS client initialized successfully.")
        except NotFound as e:
            self.log.critical("CRITICAL: GCS bucket not found", error=str(e), exc_info=True)
            raise GCSError("GCS bucket not found", e)
        except GoogleAPIError as e:
            self.log.critical("CRITICAL: Failed to initialize GCS client", error=str(e), exc_info=True)
            raise GCSError("GCS client initialization failed", e)
        except Exception as e:
            self.log.critical("CRITICAL: Unexpected error initializing GCS client", error=str(e), exc_info=True)
            raise GCSError("Unexpected error initializing GCS client", e)

    async def upload_file_async(self, object_name: str, data: bytes, content_type: Optional[str] = None):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.upload_file_sync, object_name, data, content_type)

    def upload_file_sync(self, object_name: str, data: bytes, content_type: Optional[str] = None):
        try:
            blob = self._bucket.blob(object_name)
            blob.upload_from_string(data, content_type=content_type)
            self.log.info("File uploaded to GCS", object_name=object_name)
        except GoogleAPIError as e:
            self.log.error("GCS upload failed", error=str(e))
            raise GCSError("GCS upload failed", e)
        except Exception as e:
            self.log.error("Unexpected error during GCS upload", error=str(e))
            raise GCSError("Unexpected error during GCS upload", e)

    async def check_file_exists_async(self, object_name: str) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.check_file_exists_sync, object_name)

    def check_file_exists_sync(self, object_name: str) -> bool:
        try:
            blob = self._bucket.blob(object_name)
            exists = blob.exists()
            self.log.debug("Checked GCS file existence", object_name=object_name, exists=exists)
            return exists
        except GoogleAPIError as e:
            self.log.error("GCS existence check failed", error=str(e))
            raise GCSError("GCS existence check failed", e)
        except Exception as e:
            self.log.error("Unexpected error during GCS existence check", error=str(e))
            raise GCSError("Unexpected error during GCS existence check", e)

    async def delete_file_async(self, object_name: str):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.delete_file_sync, object_name)

    def delete_file_sync(self, object_name: str):
        try:
            blob = self._bucket.blob(object_name)
            blob.delete()
            self.log.info("File deleted from GCS", object_name=object_name)
        except NotFound:
            self.log.info("GCS file already deleted or not found", object_name=object_name)
        except GoogleAPIError as e:
            self.log.error("GCS delete failed", error=str(e))
            raise GCSError("GCS delete failed", e)
        except Exception as e:
            self.log.error("Unexpected error during GCS delete", error=str(e))
            raise GCSError("Unexpected error during GCS delete", e)

    def read_file_sync(self, object_name: str) -> bytes:
        try:
            blob = self._bucket.blob(object_name)
            data = blob.download_as_bytes()
            self.log.info("File read from GCS", object_name=object_name)
            return data
        except NotFound:
            self.log.error("GCS file not found for reading", object_name=object_name)
            raise GCSError("GCS file not found for reading")
        except GoogleAPIError as e:
            self.log.error("GCS read failed", error=str(e))
            raise GCSError("GCS read failed", e)
        except Exception as e:
            self.log.error("Unexpected error during GCS read", error=str(e))
            raise GCSError("Unexpected error during GCS read", e)
