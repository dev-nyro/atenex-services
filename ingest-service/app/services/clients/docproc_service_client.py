# ingest-service/app/services/clients/docproc_service_client.py
from typing import List, Dict, Any, Tuple, Optional
import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, before_sleep_log
import logging

from app.core.config import settings
from app.services.base_client import BaseServiceClient

log = structlog.get_logger(__name__)

class DocProcServiceClientError(Exception):
    """Custom exception for Document Processing Service client errors."""
    def __init__(self, message: str, status_code: int = None, detail: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail

class DocProcServiceClient(BaseServiceClient):
    """
    Client for interacting with the Atenex Document Processing Service.
    """
    def __init__(self, base_url: str = None):
        effective_base_url = base_url or str(settings.DOCPROC_SERVICE_URL).rstrip('/')
        
        parsed_url = httpx.URL(effective_base_url)
        self.service_endpoint_path = parsed_url.path
        client_base_url = f"{parsed_url.scheme}://{parsed_url.host}:{parsed_url.port}" if parsed_url.port else f"{parsed_url.scheme}://{parsed_url.host}"

        super().__init__(base_url=client_base_url, service_name="DocProcService")
        self.log = log.bind(service_client="DocProcServiceClient", service_url=effective_base_url)

    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=1, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
        before_sleep=before_sleep_log(log, logging.WARNING)
    )
    async def process_document(
        self,
        file_bytes: bytes,
        original_filename: str,
        content_type: str,
        document_id: Optional[str] = None,
        company_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Sends a document file to the DocProc Service for text extraction and chunking.
        """
        if not file_bytes:
            self.log.error("process_document called with empty file_bytes.")
            raise DocProcServiceClientError("File content cannot be empty.")

        files = {'file': (original_filename, file_bytes, content_type)}
        data: Dict[str, Any] = {
            'original_filename': original_filename,
            'content_type': content_type
        }
        if document_id:
            data['document_id'] = document_id
        if company_id:
            data['company_id'] = company_id
        
        self.log.debug(f"Requesting document processing from {self.service_endpoint_path}", 
                       filename=original_filename, content_type=content_type, size_len=len(file_bytes))
        
        try:
            response = await self._request(
                method="POST",
                endpoint=self.service_endpoint_path,
                files=files,
                data=data
            )
            response_data = response.json()
            
            if "data" not in response_data or \
               "document_metadata" not in response_data["data"] or \
               "chunks" not in response_data["data"]:
                self.log.error("Invalid response format from DocProc Service", response_data_preview=str(response_data)[:200])
                raise DocProcServiceClientError(
                    message="Invalid response format from DocProc Service.",
                    status_code=response.status_code,
                    detail=response_data
                )
            
            self.log.info("Successfully processed document via DocProc Service.", 
                          filename=original_filename, 
                          num_chunks=len(response_data["data"]["chunks"]))
            return response_data

        except httpx.HTTPStatusError as e:
            self.log.error(
                "HTTP error from DocProc Service",
                status_code=e.response.status_code,
                response_text=e.response.text,
                filename=original_filename
            )
            error_detail = e.response.text
            try:
                error_detail_json = e.response.json()
                if isinstance(error_detail_json, dict) and "detail" in error_detail_json:
                    error_detail = error_detail_json["detail"]
            except:
                pass
            
            raise DocProcServiceClientError(
                message=f"DocProc Service returned HTTP error: {e.response.status_code}",
                status_code=e.response.status_code,
                detail=error_detail
            ) from e
        except httpx.RequestError as e:
            self.log.error("Request error calling DocProc Service", error_msg=str(e), filename=original_filename)
            raise DocProcServiceClientError(
                message=f"Request to DocProc Service failed: {type(e).__name__}",
                detail=str(e)
            ) from e
        except Exception as e:
            self.log.exception("Unexpected error in DocProcServiceClient", filename=original_filename)
            raise DocProcServiceClientError(message=f"Unexpected error: {e}") from e

    async def health_check(self) -> bool:
        """
        Checks the health of the Document Processing Service.
        """
        health_log = self.log.bind(action="docproc_health_check")
        try:
            parsed_service_url = httpx.URL(str(settings.DOCPROC_SERVICE_URL))
            health_endpoint_url_base = f"{parsed_service_url.scheme}://{parsed_service_url.host}"
            if parsed_service_url.port:
                health_endpoint_url_base += f":{parsed_service_url.port}"
            
            async with httpx.AsyncClient(base_url=health_endpoint_url_base, timeout=5) as client:
                response = await client.get("/health")
            response.raise_for_status()
            health_data = response.json()
            if health_data.get("status") == "ok":
                health_log.info("DocProc Service is healthy.")
                return True
            else:
                health_log.warning("DocProc Service reported unhealthy.", health_data=health_data)
                return False
        except httpx.HTTPStatusError as e:
            health_log.error("DocProc Service health check failed (HTTP error)", status_code=e.response.status_code, response_text=e.response.text)
            return False
        except httpx.RequestError as e:
            health_log.error("DocProc Service health check failed (Request error)", error_msg=str(e))
            return False
        except Exception as e:
            health_log.exception("Unexpected error during DocProc Service health check")
            return False