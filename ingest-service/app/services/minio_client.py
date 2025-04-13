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
        self.bucket_name = settings.MINIO_BUCKET_NAME # Usar siempre el bucket de config ('atenex')
        try:
            self.client = Minio(
                settings.MINIO_ENDPOINT,
                access_key=settings.MINIO_ACCESS_KEY.get_secret_value(),
                secret_key=settings.MINIO_SECRET_KEY.get_secret_value(),
                secure=settings.MINIO_USE_SECURE
            )
            self._ensure_bucket_exists()
            log.info("MinIO client initialized", endpoint=settings.MINIO_ENDPOINT, bucket=self.bucket_name)
        except Exception as e:
            log.critical("CRITICAL: Failed to initialize MinIO client", bucket=self.bucket_name, error=str(e), exc_info=True)
            # Si MinIO es esencial, fallar el inicio del servicio
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

    async def upload_file(
        self,
        company_id: uuid.UUID,
        document_id: uuid.UUID,
        file_name: str,
        file_content_stream: IO[bytes], # Acepta BytesIO u otro stream
        content_type: str,
        content_length: int
    ) -> str:
        """
        Sube un archivo a MinIO de forma asíncrona usando run_in_executor.
        El nombre del objeto usa company_id/document_id/filename.
        Retorna el nombre completo del objeto en MinIO.
        """
        # Construir nombre del objeto para organización dentro del bucket 'atenex'
        object_name = f"{str(company_id)}/{str(document_id)}/{file_name}"
        upload_log = log.bind(bucket=self.bucket_name, object_name=object_name, content_type=content_type, length=content_length)
        upload_log.info("Queueing file upload to MinIO executor")

        loop = asyncio.get_running_loop()
        try:
            # Asegurarse que el stream está al inicio antes de pasarlo
            file_content_stream.seek(0)
            # Ejecutar la operación síncrona put_object en el executor
            result = await loop.run_in_executor(
                None, # Default ThreadPoolExecutor
                self.client.put_object, # La función síncrona
                # Argumentos para put_object:
                self.bucket_name,
                object_name,
                file_content_stream,
                content_length, # Pasar longitud explícitamente
                content_type=content_type,
            )
            upload_log.info("File uploaded successfully to MinIO via executor", etag=getattr(result, 'etag', None), version_id=getattr(result, 'version_id', None))
            return object_name
        except S3Error as e:
            upload_log.error("Failed to upload file to MinIO via executor", error=str(e), code=e.code, exc_info=True)
            # Re-lanzar S3Error para que el llamador lo maneje
            raise IOError(f"Failed to upload to storage: {e.code}") from e
        except Exception as e:
            upload_log.error("Unexpected error during file upload via executor", error=str(e), exc_info=True)
            # Re-lanzar como IOError genérico
            raise IOError(f"Unexpected storage upload error") from e

    def download_file_stream_sync(self, object_name: str) -> io.BytesIO:
        """Operación SÍNCRONA para descargar un archivo a BytesIO."""
        download_log = log.bind(bucket=self.bucket_name, object_name=object_name)
        download_log.info("Downloading file from MinIO (sync operation starting)...")
        response = None
        try:
            # get_object es bloqueante
            response = self.client.get_object(self.bucket_name, object_name)
            file_data = response.read() # Leer todo en memoria (bloqueante)
            file_stream = io.BytesIO(file_data)
            download_log.info(f"File downloaded successfully from MinIO (sync, {len(file_data)} bytes)")
            file_stream.seek(0) # Importante resetear posición
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
            # Asegurar liberación de conexión
            if response:
                response.close()
                response.release_conn()

    async def download_file_stream(self, object_name: str) -> io.BytesIO:
        """Descarga un archivo de MinIO como BytesIO de forma asíncrona."""
        download_log = log.bind(bucket=self.bucket_name, object_name=object_name)
        download_log.info("Queueing file download from MinIO executor")
        loop = asyncio.get_running_loop()
        try:
            # Llamar a la función síncrona en el executor
            file_stream = await loop.run_in_executor(
                None, # Default executor
                self.download_file_stream_sync, # La función bloqueante
                object_name # Argumento para la función
            )
            download_log.info("File download successful via executor")
            return file_stream
        except FileNotFoundError: # Capturar y relanzar específicamente
            download_log.error("File not found in MinIO via executor", object_name=object_name)
            raise
        except Exception as e: # Captura IOError u otros errores del sync helper
            download_log.error("Error downloading file via executor", error=str(e), error_type=type(e).__name__, exc_info=True)
            raise IOError(f"Failed to download file via executor: {e}") from e