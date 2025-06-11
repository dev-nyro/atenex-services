from abc import ABC, abstractmethod
from typing import Optional, Dict, Any

class DocProcServicePort(ABC):
    @abstractmethod
    def process_document(self, file_bytes: bytes, original_filename: str, content_type: str, document_id: Optional[str], company_id: Optional[str]) -> Dict[str, Any]:
        pass
    
    @abstractmethod
    def process_document_sync(self, file_bytes: bytes, original_filename: str, content_type: str, document_id: Optional[str], company_id: Optional[str]) -> Dict[str, Any]:
        pass