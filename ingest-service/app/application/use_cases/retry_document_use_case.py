import uuid
from app.application.ports.document_repository_port import DocumentRepositoryPort
from app.application.ports.task_queue_port import TaskQueuePort
from app.domain.enums import DocumentStatus
from app.domain.exceptions import DocumentNotFoundException, OperationConflictException, TaskQueueException, DatabaseException
import structlog

log = structlog.get_logger(__name__)

class RetryDocumentUseCase:
    def __init__(self, 
                 document_repository: DocumentRepositoryPort, 
                 task_queue: TaskQueuePort):
        self.document_repository = document_repository
        self.task_queue = task_queue

    async def execute(self, *,
                     document_id: uuid.UUID,
                     company_id: uuid.UUID) -> dict: # Removed file_name and file_type from params

        use_case_log = log.bind(document_id=str(document_id), company_id=str(company_id))
        use_case_log.info("RetryDocumentUseCase execution started.")

        doc = await self.document_repository.find_by_id(document_id, company_id)
        if not doc:
            use_case_log.warning("Document not found for retry.")
            raise DocumentNotFoundException(document_id=str(document_id))
        
        use_case_log.info("Document found.", current_status=doc.status.value, file_name=doc.file_name, file_type=doc.file_type)

        if doc.status != DocumentStatus.ERROR:
            use_case_log.warning("Document is not in ERROR state, cannot retry.")
            raise OperationConflictException(f"Document is not in 'error' state (current state: {doc.status.value}). Cannot retry.")
        
        # Update status to PENDING (or UPLOADED if file is verified to be in GCS)
        # For simplicity here, setting to PENDING to re-trigger the whole pipeline
        # Error message and chunk_count should be cleared/reset
        try:
            await self.document_repository.update_status(doc_id=document_id, status=DocumentStatus.PENDING, chunk_count=0, error_message=None)
            use_case_log.info("Document status updated to PENDING for retry.")
        except Exception as e:
            use_case_log.error("Failed to update document status for retry.", error=str(e), exc_info=True)
            raise DatabaseException(f"Failed to update document status for retry: {e}")

        try:
            task_id = await self.task_queue.enqueue_process_document_task(
                document_id=str(document_id),
                company_id=str(company_id),
                filename=doc.file_name, # Use filename from DB
                content_type=doc.file_type  # Use filetype from DB
            )
            use_case_log.info("Document reprocessing task enqueued successfully.", task_id=task_id)
        except Exception as e:
            use_case_log.error("Failed to re-enqueue document processing task.", error=str(e), exc_info=True)
            # Attempt to revert status to ERROR if task queueing fails
            try:
                 await self.document_repository.update_status(document_id, DocumentStatus.ERROR, error_message=f"Retry failed: Could not enqueue task - {type(e).__name__}")
            except Exception as db_err:
                 use_case_log.error("Failed to revert document status to ERROR after task queue failure.", nested_error=str(db_err))
            raise TaskQueueException(f"Failed to re-enqueue document processing task: {e}")

        return {
            "document_id": str(document_id),
            "task_id": task_id,
            "status": DocumentStatus.PENDING.value, # Or UPLOADED, consistent with what the task expects
            "message": "Document retry accepted, processing started."
        }