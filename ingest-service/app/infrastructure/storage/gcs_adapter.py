import asyncio
from typing import Optional
from app.application.ports.file_storage_port import FileStoragePort
from app.services.gcs_client import GCSClient, GCSClientError # Assuming GCSClient is in app/services/
import structlog

log = structlog.get_logger(__name__)

class GCSAdapter(FileStoragePort):
    def __init__(self, bucket_name: Optional[str] = None):
        # The actual GCSClient from app/services/gcs_client.py handles settings.GCS_BUCKET_NAME
        self.client = GCSClient(bucket_name=bucket_name) 
        log.info("GCSAdapter initialized", configured_bucket=self.client.bucket_name)

    async def upload(self, object_name: str, data: bytes, content_type: str) -> str:
        log.debug("GCSAdapter: upload (async)", object_name=object_name)
        return await self.client.upload_file_async(object_name, data, content_type)

    def download(self, object_name: str, target_path: str) -> None:
        log.debug("GCSAdapter: download (sync)", object_name=object_name, target_path=target_path)
        # GCSClient already has download_file_sync
        self.client.download_file_sync(object_name, target_path)

    async def exists(self, object_name: str) -> bool:
        log.debug("GCSAdapter: exists (async)", object_name=object_name)
        return await self.client.check_file_exists_async(object_name)
    
    def exists_sync(self, object_name: str) -> bool:
        log.debug("GCSAdapter: exists_sync (sync)", object_name=object_name)
        # GCSClient already has check_file_exists_sync
        return self.client.check_file_exists_sync(object_name)

    async def delete(self, object_name: str) -> None:
        log.debug("GCSAdapter: delete (async)", object_name=object_name)
        await self.client.delete_file_async(object_name)

    def delete_sync(self, object_name: str) -> None:
        log.debug("GCSAdapter: delete_sync (sync)", object_name=object_name)
        # GCSClient already has delete_file_sync
        self.client.delete_file_sync(object_name)