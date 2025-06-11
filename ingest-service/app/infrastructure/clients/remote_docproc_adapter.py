from typing import Optional, Dict, Any
from app.application.ports.docproc_service_port import DocProcServicePort
from app.services.clients.docproc_service_client import DocProcServiceClient
from app.domain.exceptions import ServiceDependencyException # Import corrected
import asyncio

class RemoteDocProcAdapter(DocProcServicePort):
    def __init__(self):
        # DocProcServiceClient now handles its own base URL from settings
        self.client = DocProcServiceClient() 

    async def process_document(self, file_bytes: bytes, original_filename: str, content_type: str, document_id: Optional[str], company_id: Optional[str]) -> Dict[str, Any]:
        # This method remains async for potential async use cases if any
        try:
            # Ensure the async client method is called
            return await self.client.process_document_async(file_bytes, original_filename, content_type, document_id, company_id)
        except ServiceDependencyException: # Catch specific exception from client
            raise
        except Exception as e: # Catch any other unexpected error
            raise ServiceDependencyException(service_name="DocProcService", original_error=str(e)) from e

    def process_document_sync(self, file_bytes: bytes, original_filename: str, content_type: str, document_id: Optional[str], company_id: Optional[str]) -> Dict[str, Any]:
        try:
            # Call the new synchronous method on the client
            return self.client.process_document_sync(file_bytes, original_filename, content_type, document_id, company_id)
        except ServiceDependencyException:
            raise
        except Exception as e:
            raise ServiceDependencyException(service_name="DocProcService", original_error=str(e)) from e