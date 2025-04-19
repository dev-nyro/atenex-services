# ingest-service/app/services/minio_client.py
import io
import uuid
from typing import IO, BinaryIO, Optional # Añadir Optional
from minio import Minio
from minio.error import S3Error # Importar directamente S3Error
import structlog
import asyncio

# LLM_COMMENT: Importar settings directamente para configuración.
from app.core.config import settings

log = structlog.get_logger(__name__)

# Definir excepción personalizada para errores de MinIO
class MinioError(Exception):
    """Custom exception for MinIO related errors."""
    def __init__(self, message: str, original_exception: Optional[Exception] = None):
        self.message = message
        self.original_exception = original_exception
        super().__init__(message)

    def __str__(self):
        if self.original_exception:
            return f"{self.message}: {type(self.original_exception).__name__} - {str(self.original_exception)}"
        return self.message


# --- RENOMBRAR CLASE ---
class MinioClient:
    """Client to interact with MinIO using configured settings."""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        bucket_name: Optional[str] = None,
        secure: Optional[bool] = None
    ):
        # LLM_COMMENT: Usar valores de settings como default, permitir override si se pasan argumentos.
        self._endpoint = endpoint or settings.MINIO_ENDPOINT
        self._access_key = access_key or settings.MINIO_ACCESS_KEY.get_secret_value()
        self._secret_key = secret_key or settings.MINIO_SECRET_KEY.get_secret_value()
        self.bucket_name = bucket_name or settings.MINIO_BUCKET_NAME
        self._secure = secure if secure is not None else settings.MINIO_USE_SECURE

        self._client: Optional[Minio] = None
        self.log = log.bind(minio_endpoint=self._endpoint, bucket_name=self.bucket_name)

        # LLM_COMMENT: Inicializar el cliente interno de MinIO.
        self._initialize_client()

    def _initialize_client(self):
        """Initializes the internal MinIO client and ensures bucket exists."""
        self.log.debug("Initializing MinIO client...")
        try:
            self._client = Minio(
                self._endpoint,
                access_key=self._access_key,
                secret_key=self._secret_key,
                secure=self._secure
            )
            self._ensure_bucket_exists_sync() # Llamada síncrona en init
            self.log.info("MinIO client initialized successfully.")
        except (S3Error, TypeError, ValueError) as e:
            self.log.critical("CRITICAL: Failed to initialize MinIO client", error=str(e), exc_info=True)
            # LLM_COMMENT: Lanzar RuntimeError si falla la inicialización crítica.
            raise RuntimeError(f"MinIO client initialization failed: {e}") from e

    def _get_client(self) -> Minio:
        """Returns the initialized MinIO client, raising error if not available."""
        if self._client is None:
            # LLM_COMMENT: Error si se intenta usar un cliente no inicializado.
            self.log.error("Minio client accessed before initialization.")
            raise RuntimeError("Minio client is not initialized.")
        return self._client

    def _ensure_bucket_exists_sync(self):
        """Synchronously creates the bucket if it doesn't exist."""
        client = self._get_client()
        try:
            found = client.bucket_exists(self.bucket_name)
            if not found:
                client.make_bucket(self.bucket_name)
                self.log.info(f"MinIO bucket '{self.bucket_name}' created.")
            else:
                self.log.debug(f"MinIO bucket '{self.bucket_name}' already exists.")
        except S3Error as e:
            self.log.error(f"S3Error checking/creating MinIO bucket", bucket=self.bucket_name, error_code=getattr(e, 'code', 'Unknown'), error_details=str(e))
            raise MinioError(f"Error checking/creating bucket '{self.bucket_name}'", e) from e
        except Exception as e:
            self.log.error(f"Unexpected error checking/creating MinIO bucket", bucket=self.bucket_name, error=str(e), exc_info=True)
            raise MinioError(f"Unexpected error with bucket '{self.bucket_name}'", e) from e

    async def upload_file_async(
        self,
        object_name: str,
        data: bytes, # Cambiado a bytes para simplificar
        content_type: str
    ) -> str:
        """Uploads file content to MinIO asynchronously using run_in_executor."""
        # LLM_COMMENT: Usar run_in_executor para operaciones síncronas de MinIO.
        upload_log = self.log.bind(bucket=self.bucket_name, object_name=object_name, content_type=content_type, length=len(data))
        upload_log.info("Queueing file upload to MinIO executor")

        loop = asyncio.get_running_loop()
        client = self._get_client() # Obtener cliente inicializado

        def _upload_sync():
            # LLM_COMMENT: La operación síncrona real dentro del executor.
            data_stream = io.BytesIO(data)
            try:
                 result = client.put_object(
                    bucket_name=self.bucket_name,
                    object_name=object_name,
                    data=data_stream,
                    length=len(data),
                    content_type=content_type
                )
                 # LLM_COMMENT: Loggear etag si la subida es exitosa.
                 upload_log.debug("MinIO put_object successful", etag=getattr(result, 'etag', 'N/A'), version_id=getattr(result, 'version_id', 'N/A'))
                 return object_name
            except S3Error as e:
                 upload_log.error("S3Error during MinIO upload (sync part)", error_code=getattr(e, 'code', 'Unknown'), error_details=str(e))
                 raise MinioError(f"S3 error uploading {object_name}", e) from e
            except Exception as e:
                 upload_log.exception("Unexpected error during MinIO upload (sync part)", error=str(e))
                 raise MinioError(f"Unexpected error uploading {object_name}", e) from e

        try:
            # LLM_COMMENT: Ejecutar la función síncrona en el threadpool por defecto de asyncio.
            uploaded_object_name = await loop.run_in_executor(None, _upload_sync)
            upload_log.info("File uploaded successfully to MinIO via executor")
            return uploaded_object_name
        except MinioError as me: # Capturar nuestra excepción personalizada
            upload_log.error("Upload failed via executor", error=str(me))
            raise me # Relanzar para que el llamador la maneje
        except Exception as e: # Capturar otros errores inesperados del executor
             upload_log.exception("Unexpected executor error during upload", error=str(e))
             raise MinioError("Unexpected executor error during upload", e) from e

    def download_file_sync(self, object_name: str, file_path: str):
        """Synchronously downloads a file from MinIO to a local path."""
        # LLM_COMMENT: Operación síncrona para descargar a un archivo local.
        download_log = self.log.bind(bucket=self.bucket_name, object_name=object_name, target_path=file_path)
        download_log.info("Downloading file from MinIO (sync operation)...")
        client = self._get_client()
        try:
            client.fget_object(self.bucket_name, object_name, file_path)
            download_log.info(f"File downloaded successfully from MinIO to {file_path} (sync)")
        except S3Error as e:
            download_log.error("S3Error downloading file (sync)", error_code=getattr(e, 'code', 'Unknown'), error_details=str(e))
            if getattr(e, 'code', None) == 'NoSuchKey':
                raise MinioError(f"Object not found in MinIO: {object_name}", e) from e
            else:
                raise MinioError(f"S3 error downloading {object_name}: {e.code}", e) from e
        except Exception as e:
            download_log.exception("Unexpected error during sync file download", error=str(e))
            raise MinioError(f"Unexpected error downloading {object_name}", e) from e

    async def download_file(self, object_name: str, file_path: str):
        """Downloads a file from MinIO to a local path asynchronously."""
        # LLM_COMMENT: Envolver la descarga síncrona en run_in_executor.
        download_log = self.log.bind(bucket=self.bucket_name, object_name=object_name, target_path=file_path)
        download_log.info("Queueing file download from MinIO executor")
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self.download_file_sync, object_name, file_path)
            download_log.info("File download successful via executor")
        except MinioError as me:
            download_log.error("Download failed via executor", error=str(me))
            raise me
        except Exception as e:
            download_log.exception("Unexpected executor error during download", error=str(e))
            raise MinioError("Unexpected executor error during download", e) from e

    def check_file_exists_sync(self, object_name: str) -> bool:
        """Synchronously checks if a file exists in MinIO."""
        # LLM_COMMENT: Operación síncrona para verificar existencia (usa stat_object).
        check_log = self.log.bind(bucket=self.bucket_name, object_name=object_name)
        client = self._get_client()
        try:
            client.stat_object(self.bucket_name, object_name)
            check_log.debug("Object exists in MinIO (sync check).")
            return True
        except S3Error as e:
            # LLM_COMMENT: Manejar específicamente NoSuchKey como 'no existe'.
            if getattr(e, 'code', None) in ('NoSuchKey', 'NoSuchBucket'):
                check_log.debug("Object not found in MinIO (sync check)", code=e.code)
                return False
            # LLM_COMMENT: Loggear otros errores S3 pero considerarlos como fallo de verificación.
            check_log.error("S3Error checking object existence (sync)", error_code=getattr(e, 'code', 'Unknown'), error_details=str(e))
            raise MinioError(f"S3 error checking existence for {object_name}", e) from e
        except Exception as e:
             check_log.exception("Unexpected error checking object existence (sync)", error=str(e))
             raise MinioError(f"Unexpected error checking existence for {object_name}", e) from e

    async def check_file_exists_async(self, object_name: str) -> bool:
        """Checks if a file exists in MinIO asynchronously."""
        # LLM_COMMENT: Envolver la verificación síncrona en run_in_executor.
        check_log = self.log.bind(bucket=self.bucket_name, object_name=object_name)
        check_log.debug("Queueing file existence check in executor")
        loop = asyncio.get_running_loop()
        try:
            exists = await loop.run_in_executor(None, self.check_file_exists_sync, object_name)
            check_log.debug("File existence check completed via executor", exists=exists)
            return exists
        except MinioError as me:
            # LLM_COMMENT: Loggear error pero devolver False si fue NoSuchKey, relanzar otros.
            if "Object not found" in str(me): # Chequeo simple del mensaje de error interno
                 return False
            check_log.error("Existence check failed via executor", error=str(me))
            # Podrías querer relanzar aquí si un error diferente a Not Found es crítico
            # raise me
            return False # O tratar otros errores como 'no se pudo verificar -> no existe'
        except Exception as e:
            check_log.exception("Unexpected executor error during existence check", error=str(e))
            # Tratar error inesperado como 'no se pudo verificar -> no existe'
            return False

    def delete_file_sync(self, object_name: str):
        """Synchronously deletes a file from MinIO."""
        # LLM_COMMENT: Operación síncrona para eliminar objeto.
        delete_log = self.log.bind(bucket=self.bucket_name, object_name=object_name)
        delete_log.info("Deleting file from MinIO (sync operation)...")
        client = self._get_client()
        try:
            client.remove_object(self.bucket_name, object_name)
            delete_log.info("File deleted successfully from MinIO (sync)")
        except S3Error as e:
            # LLM_COMMENT: Loggear error pero no relanzar para permitir eliminación parcial.
            delete_log.error("S3Error deleting file (sync)", error_code=getattr(e, 'code', 'Unknown'), error_details=str(e))
            # No relanzar aquí para que el proceso de borrado principal continúe si es posible
            # raise MinioError(f"S3 error deleting {object_name}", e) from e
        except Exception as e:
            delete_log.exception("Unexpected error during sync file deletion", error=str(e))
            # No relanzar aquí tampoco
            # raise MinioError(f"Unexpected error deleting {object_name}", e) from e

    async def delete_file_async(self, object_name: str) -> None:
        """Deletes a file from MinIO asynchronously."""
        # LLM_COMMENT: Envolver la eliminación síncrona en run_in_executor.
        delete_log = self.log.bind(bucket=self.bucket_name, object_name=object_name)
        delete_log.info("Queueing file deletion from MinIO executor")
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self.delete_file_sync, object_name)
            delete_log.info("File deletion successful via executor")
        except Exception as e:
            # LLM_COMMENT: Loggear errores del executor pero no impedir que el flujo continúe.
            delete_log.exception("Unexpected executor error during deletion", error=str(e))
            # No relanzar para no bloquear el resto del proceso de borrado