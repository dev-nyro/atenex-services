# ingest-service/app/services/clients/docproc_service_client.py
from typing import List, Dict, Any, Tuple, Optional
import httpx
import structlog

from app.core.config import settings
from app.services.base_client import BaseServiceClient 
from app.domain.exceptions import ServiceDependencyException

log = structlog.get_logger(__name__)


class DocProcServiceClient(BaseServiceClient):
    def __init__(self, base_url: Optional[str] = None):
        effective_base_url = base_url or str(settings.DOCPROC_SERVICE_URL).rstrip('/')
        parsed_url = httpx.URL(effective_base_url)
        self.service_endpoint_path = parsed_url.path or "/"
        client_base_url = f"{parsed_url.scheme}://{parsed_url.host}"
        if parsed_url.port:
            client_base_url += f":{parsed_url.port}"
        
        super().__init__(base_url=client_base_url, service_name="DocProcService")
        self.log = log.bind(service_client="DocProcServiceClient", base_url=self.base_url, endpoint_path=self.service_endpoint_path)

    async def process_document_async(
        self,
        file_bytes: bytes,
        original_filename: str,
        content_type: str,
        document_id: Optional[str] = None,
        company_id: Optional[str] = None
    ) -> Dict[str, Any]:
        
        if not file_bytes:
            self.log.error("process_document_async called with empty file_bytes.")
            raise ServiceDependencyException(service_name=self.service_name, original_error="File content cannot be empty.", message="DocProc submission error")

        files = {'file': (original_filename, file_bytes, content_type)}
        data: Dict[str, Any] = {'original_filename': original_filename, 'content_type': content_type}
        if document_id: data['document_id'] = document_id
        if company_id: data['company_id'] = company_id
        
        self.log.debug("Requesting document processing (async)", filename=original_filename, content_type=content_type, size_len=len(file_bytes))
        
        try:
            response = await self._request_async(method="POST", endpoint=self.service_endpoint_path, files=files, data=data)
            response_data = response.json()
            if "data" not in response_data or "chunks" not in response_data.get("data", {}):
                self.log.error("Invalid response format from DocProc Service (async)", response_data_preview=str(response_data)[:200])
                raise ServiceDependencyException(service_name=self.service_name, message="Invalid response format from DocProc Service.", original_error=str(response_data)[:200])
            self.log.info("Successfully processed document via DocProc Service (async).", filename=original_filename, num_chunks=len(response_data["data"]["chunks"]))
            return response_data
        except httpx.HTTPStatusError as e:
            err_msg = f"DocProc Service HTTP error: {e.response.status_code}"
            self.log.error(err_msg, response_text=e.response.text, filename=original_filename)
            raise ServiceDependencyException(service_name=self.service_name, message=err_msg, original_error=e.response.text) from e
        except Exception as e:
            self.log.exception("Unexpected error in DocProcServiceClient (async)", filename=original_filename)
            raise ServiceDependencyException(service_name=self.service_name, message=f"Unexpected error: {type(e).__name__}", original_error=str(e)) from e

    def process_document_sync(
        self,
        file_bytes: bytes,
        original_filename: str,
        content_type: str,
        document_id: Optional[str] = None,
        company_id: Optional[str] = None
    ) -> Dict[str, Any]:

        if not file_bytes:
            self.log.error("process_document_sync called with empty file_bytes.")
            raise ServiceDependencyException(service_name=self.service_name, original_error="File content cannot be empty.", message="DocProc submission error")

        files = {'file': (original_filename, file_bytes, content_type)}
        data: Dict[str, Any] = {'original_filename': original_filename, 'content_type': content_type}
        if document_id: data['document_id'] = document_id
        if company_id: data['company_id'] = company_id
        
        self.log.debug("Requesting document processing (sync)", filename=original_filename, content_type=content_type, size_len=len(file_bytes))
        
        try:
            response = self._request_sync(method="POST", endpoint=self.service_endpoint_path, files=files, data=data)
            response_data = response.json()
            if "data" not in response_data or "chunks" not in response_data.get("data", {}):
                self.log.error("Invalid response format from DocProc Service (sync)", response_data_preview=str(response_data)[:200])
                raise ServiceDependencyException(service_name=self.service_name, message="Invalid response format from DocProc Service.", original_error=str(response_data)[:200])
            self.log.info("Successfully processed document via DocProc Service (sync).", filename=original_filename, num_chunks=len(response_data["data"]["chunks"]))
            return response_data
        except httpx.HTTPStatusError as e:
            err_msg = f"DocProc Service HTTP error: {e.response.status_code}"
            self.log.error(err_msg, response_text=e.response.text, filename=original_filename)
            raise ServiceDependencyException(service_name=self.service_name, message=err_msg, original_error=e.response.text) from e
        except Exception as e:
            self.log.exception("Unexpected error in DocProcServiceClient (sync)", filename=original_filename)
            raise ServiceDependencyException(service_name=self.service_name, message=f"Unexpected error: {type(e).__name__}", original_error=str(e)) from e