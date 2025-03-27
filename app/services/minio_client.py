import io
import uuid
from typing import IO
from minio import Minio
from minio.error import S3Error
import structlog

from app.core.config import settings

log = structlog.get_logger(__name__)

class MinioStorageClient:
    """Cliente para interactuar con MinIO."""

    def __init__(self):
        try:
            self.client = Minio(
                settings.MINIO_ENDPOINT,
                access_key=settings.MINIO_ACCESS_KEY.get_secret_value(),
                secret_key=settings.MINIO_SECRET_KEY.get_secret_value(),
                secure=settings.MINIO_USE_SECURE
            )
            self._ensure_bucket_exists()
            log.info("MinIO client initialized", endpoint=settings.MINIO_ENDPOINT, bucket=settings.MINIO_BUCKET_NAME)
        except Exception as e:
            log.error("Failed to initialize MinIO client", error=str(e), exc_info=True)
            # Decide if this should be a fatal error during startup
            raise

    def _ensure_bucket_exists(self):
        """Crea el bucket si no existe."""
        try:
            found = self.client.bucket_exists(settings.MINIO_BUCKET_NAME)
            if not found:
                self.client.make_bucket(settings.MINIO_BUCKET_NAME)
                log.info(f"MinIO bucket '{settings.MINIO_BUCKET_NAME}' created.")
            else:
                log.debug(f"MinIO bucket '{settings.MINIO_BUCKET_NAME}' already exists.")
        except S3Error as e:
            log.error(f"Error checking/creating MinIO bucket '{settings.MINIO_BUCKET_NAME}'", error=str(e), exc_info=True)
            raise

    async def upload_file(
        self,
        company_id: uuid.UUID,
        document_id: uuid.UUID, # Use doc_id for a more unique object name structure
        file_name: str,
        file_content_stream: IO[bytes],
        content_type: str,
        content_length: int
    ) -> str:
        """
        Sube un archivo a MinIO.
        Retorna el nombre del objeto en MinIO (object_name).
        """
        # Crear un nombre de objeto único y estructurado
        object_name = f"{str(company_id)}/{str(document_id)}/{file_name}"
        upload_log = log.bind(bucket=settings.MINIO_BUCKET_NAME, object_name=object_name, content_type=content_type, length=content_length)
        upload_log.info("Uploading file to MinIO...")

        try:
            result = self.client.put_object(
                settings.MINIO_BUCKET_NAME,
                object_name,
                file_content_stream,
                length=content_length, # Important for progress and efficiency
                content_type=content_type,
                # metadata={"company-id": str(company_id), "document-id": str(document_id)} # Optional S3 metadata
            )
            upload_log.info("File uploaded successfully to MinIO", etag=result.etag, version_id=result.version_id)
            return object_name # Return the object name used
        except S3Error as e:
            upload_log.error("Failed to upload file to MinIO", error=str(e), exc_info=True)
            raise # Re-raise the exception

    async def download_file_stream(
        self,
        object_name: str
    ) -> io.BytesIO:
        """
        Descarga un archivo de MinIO como un stream en memoria (BytesIO).
        """
        download_log = log.bind(bucket=settings.MINIO_BUCKET_NAME, object_name=object_name)
        download_log.info("Downloading file from MinIO...")
        try:
            response = None
            try:
                response = self.client.get_object(settings.MINIO_BUCKET_NAME, object_name)
                file_stream = io.BytesIO(response.read())
                download_log.info("File downloaded successfully from MinIO")
                file_stream.seek(0) # Reset stream position
                return file_stream
            except S3Error as e:
                download_log.error("Failed to download file from MinIO", error=str(e), exc_info=True)
                raise FileNotFoundError(f"Object not found or error downloading: {object_name}") from e
            finally:
                if response:
                    response.close()
                    response.release_conn()
        except Exception as e:
             # Catch potential errors outside S3Error during stream handling
             download_log.error("Unexpected error during file download", error=str(e), exc_info=True)
             raise

    # No async close needed for the minio client itself
