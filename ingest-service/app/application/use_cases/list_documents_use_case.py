import uuid
import asyncio
from typing import List, Dict, Any, Optional
from app.application.ports.document_repository_port import DocumentRepositoryPort
from app.application.ports.file_storage_port import FileStoragePort
from app.application.ports.vector_store_port import VectorStorePort
from app.domain.entities import Document # Import Document for type hinting
from app.application.use_cases.get_document_status_use_case import GetDocumentStatusUseCase # For individual check logic
import structlog

log = structlog.get_logger(__name__)

class ListDocumentsUseCase:
    def __init__(self, 
                 document_repository: DocumentRepositoryPort, 
                 file_storage: FileStoragePort, # Used by GetDocumentStatusUseCase
                 vector_store: VectorStorePort): # Used by GetDocumentStatusUseCase
        self.document_repository = document_repository
        # Instantiate GetDocumentStatusUseCase to reuse its checking logic
        self.get_status_use_case = GetDocumentStatusUseCase(document_repository, file_storage, vector_store)


    async def execute(self, *,
                     company_id: uuid.UUID,
                     limit: int = 30,
                     offset: int = 0) -> List[Document]: # Return list of Document entities
        
        case_log = log.bind(company_id=str(company_id), limit=limit, offset=offset)
        case_log.info("ListDocumentsUseCase execution started.")

        docs_from_repo, total_count = await self.document_repository.list_paginated(company_id, limit, offset)
        case_log.info(f"Retrieved {len(docs_from_repo)} documents from repository, total: {total_count}.")

        if not docs_from_repo:
            return []

        # Concurrently check/enrich each document status
        enriched_docs_tasks = [
            self.get_status_use_case.execute(document_id=doc.id, company_id=company_id)
            for doc in docs_from_repo
        ]
        
        results = await asyncio.gather(*enriched_docs_tasks, return_exceptions=True)
        
        final_doc_list: List[Document] = []
        for i, res_or_exc in enumerate(results):
            original_doc = docs_from_repo[i]
            if isinstance(res_or_exc, Exception):
                case_log.error("Error enriching document status in list.", doc_id=str(original_doc.id), error=str(res_or_exc))
                # Append original doc, or a version marked with error during enrichment
                original_doc.error_message = self.get_status_use_case._append_error(original_doc.error_message, f"Status enrichment failed: {type(res_or_exc).__name__}")
                final_doc_list.append(original_doc)
            elif res_or_exc is None: # Should not happen if find_by_id was successful before
                case_log.warning("Enriched document status returned None unexpectedly.", doc_id=str(original_doc.id))
                final_doc_list.append(original_doc)
            else: # Successfully enriched
                final_doc_list.append(res_or_exc) # res_or_exc is the enriched Document object

        case_log.info("ListDocumentsUseCase execution finished.", num_returned=len(final_doc_list))
        return final_doc_list