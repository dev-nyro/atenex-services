import uuid
from typing import Optional, Dict, Any
from app.application.ports.document_repository_port import DocumentRepositoryPort
from app.application.ports.file_storage_port import FileStoragePort
from app.application.ports.vector_store_port import VectorStorePort

class DeleteDocumentUseCase:
    def __init__(self, 
                 document_repository: DocumentRepositoryPort, 
                 file_storage: FileStoragePort, 
                 vector_store: VectorStorePort):
        self.document_repository = document_repository
        self.file_storage = file_storage
        self.vector_store = vector_store

    async def execute(self, *,
                     document_id: uuid.UUID,
                     company_id: uuid.UUID) -> Dict[str, Any]:
        doc = await self.document_repository.find_by_id(document_id, company_id)
        if not doc:
            return {"error": "Document not found", "status": "not_found"}
        # Eliminar chunks en vector store
        self.vector_store.delete_document_chunks(str(company_id), str(document_id))
        # Eliminar archivo en storage
        if doc.file_path:
            await self.file_storage.delete(doc.file_path)
        # Eliminar registro en DB
        await self.document_repository.delete(document_id, company_id)
        return {"status": "deleted", "document_id": str(document_id)}
