import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, before_sleep_log
import structlog
from typing import Any, Dict, Optional
import logging

from app.core.config import settings

log = structlog.get_logger(__name__)

class BaseServiceClient:
    def __init__(self, base_url: str, service_name: str, client_timeout: Optional[int] = None):
        self.base_url = base_url.rstrip('/')
        self.service_name = service_name
        self.timeout = client_timeout or settings.HTTP_CLIENT_TIMEOUT
        self._async_client: Optional[httpx.AsyncClient] = None
        self._sync_client: Optional[httpx.Client] = None
        self.log = log.bind(service_name=self.service_name, base_url=self.base_url)

    @property
    def async_client(self) -> httpx.AsyncClient:
        if self._async_client is None or self._async_client.is_closed:
            self.log.debug(f"Initializing httpx.AsyncClient for {self.service_name}")
            self._async_client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout
            )
        return self._async_client

    @property
    def sync_client(self) -> httpx.Client:
        if self._sync_client is None: # httpx.Client doesn't have an is_closed property
            self.log.debug(f"Initializing httpx.Client for {self.service_name}")
            self._sync_client = httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout
            )
        return self._sync_client

    async def close_async(self):
        if self._async_client and not self._async_client.is_closed:
            self.log.info(f"Closing async client for {self.service_name}")
            await self._async_client.aclose()
            self._async_client = None

    def close_sync(self):
        if self._sync_client:
            self.log.info(f"Closing sync client for {self.service_name}")
            self._sync_client.close()
            self._sync_client = None
    
    async def close(self): # Generic close for lifespan manager if needed
        await self.close_async()
        self.close_sync()


    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=1, max=10),
        retry=retry_if_exception_type(httpx.RequestError),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True
    )
    async def _request_async(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_payload: Optional[Dict[str, Any]] = None, # Renamed from json to avoid conflict
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        request_log = self.log.bind(method=method, endpoint=endpoint, type="async")
        request_log.debug("Requesting service", params=params)
        try:
            response = await self.async_client.request(
                method=method,
                url=endpoint,
                params=params,
                json=json_payload,
                data=data,
                files=files,
                headers=headers,
            )
            response.raise_for_status()
            request_log.info("Received response from service", status_code=response.status_code)
            return response
        except httpx.HTTPStatusError as e:
            request_log.error("HTTP error from service", status_code=e.response.status_code, detail=e.response.text, exc_info=True)
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            request_log.error("Network error when calling service", error=str(e), exc_info=True)
            raise
        except Exception as e:
            request_log.error("Unexpected error when calling service", error=str(e), exc_info=True)
            raise

    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=1, max=10),
        retry=retry_if_exception_type(httpx.RequestError),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True
    )
    def _request_sync(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_payload: Optional[Dict[str, Any]] = None, # Renamed
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        request_log = self.log.bind(method=method, endpoint=endpoint, type="sync")
        request_log.debug("Requesting service", params=params)
        try:
            response = self.sync_client.request(
                method=method,
                url=endpoint,
                params=params,
                json=json_payload,
                data=data,
                files=files,
                headers=headers,
            )
            response.raise_for_status()
            request_log.info("Received response from service", status_code=response.status_code)
            return response
        except httpx.HTTPStatusError as e:
            request_log.error("HTTP error from service", status_code=e.response.status_code, detail=e.response.text, exc_info=True)
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            request_log.error("Network error when calling service", error=str(e), exc_info=True)
            raise
        except Exception as e:
            request_log.error("Unexpected error when calling service", error=str(e), exc_info=True)
            raise