import uuid
from typing import Optional, Dict, Any
from datetime import datetime, timezone
from app.domain.entities import Document
from app.domain.enums import DocumentStatus
from app.application.ports.document_repository_port import DocumentRepositoryPort
from app.application.ports.file_storage_port import FileStoragePort
from app.application.ports.task_queue_port import TaskQueuePort
from app.domain.exceptions import DuplicateDocumentException, InvalidInputException, StorageException, TaskQueueException
import structlog

log = structlog.get_logger(__name__)

class UploadDocumentUseCase:
    def __init__(self, 
                 document_repository: DocumentRepositoryPort, 
                 file_storage: FileStoragePort, 
                 task_queue: TaskQueuePort):
        self.document_repository = document_repository
        self.file_storage = file_storage
        self.task_queue = task_queue

    async def execute(self, *,
                     company_id: uuid.UUID,
                     user_id: uuid.UUID, # user_id is passed but not used in this use case logic directly
                     file_name: str,
                     file_type: str,
                     file_content: bytes,
                     metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        
        use_case_log = log.bind(company_id=str(company_id), file_name=file_name, file_type=file_type)
        use_case_log.info("UploadDocumentUseCase execution started.")

        if not file_name or not file_type or not file_content:
            use_case_log.warning("Invalid input for document upload.")
            raise InvalidInputException("File name, type, and content are required.")

        # 1. Validar duplicados
        use_case_log.debug("Checking for duplicate document.")
        existing = await self.document_repository.find_by_name_and_company(file_name, company_id)
        if existing and existing.status != DocumentStatus.ERROR: # Allow re-upload if previous was error
            use_case_log.warning("Duplicate document found and not in error state.", existing_doc_id=str(existing.id), existing_status=existing.status.value)
            raise DuplicateDocumentException(filename=file_name, message=f"Document '{file_name}' already exists with status '{existing.status.value}'.")
        
        document_id = uuid.uuid4()
        current_time = datetime.now(timezone.utc)
        file_path = f"{company_id}/{document_id}/{file_name}" # Corrected file_path construction

        # 2. Crear registro en DB (PENDING)
        document = Document(
            id=document_id,
            company_id=company_id,
            file_name=file_name,
            file_type=file_type,
            status=DocumentStatus.PENDING,
            metadata=metadata or {},
            file_path=file_path, # Set file_path here
            chunk_count=0,
            uploaded_at=current_time,
            updated_at=current_time
        )
        use_case_log.debug("Saving document record to DB with PENDING status.", document_id=str(document_id))
        try:
            await self.document_repository.save(document)
        except Exception as e:
            use_case_log.error("Failed to save document record to DB.", error=str(e), exc_info=True)
            raise DatabaseException(f"Failed to create initial document record: {e}")


        # 3. Subir a almacenamiento
        use_case_log.debug("Uploading file to storage.", gcs_path=file_path)
        try:
            await self.file_storage.upload(file_path, file_content, file_type)
        except Exception as e:
            use_case_log.error("Failed to upload file to storage.", error=str(e), exc_info=True)
            # Attempt to mark document as error in DB
            try:
                await self.document_repository.update_status(document_id, DocumentStatus.ERROR, error_message=f"Storage upload failed: {type(e).__name__}")
            except Exception as db_err:
                use_case_log.error("Failed to update document to ERROR after storage failure.", nested_error=str(db_err))
            raise StorageException(f"Failed to upload file to storage: {e}")

        # 4. Actualizar estado a UPLOADED
        use_case_log.debug("Updating document status to UPLOADED in DB.")
        try:
            await self.document_repository.update_status(document_id, DocumentStatus.UPLOADED)
        except Exception as e:
            use_case_log.error("Failed to update document status to UPLOADED.", error=str(e), exc_info=True)
            # If this fails, the document is in GCS but DB state is PENDING. Consider cleanup or retry logic.
            # For now, re-raise as DB exception
            raise DatabaseException(f"Failed to update document status after upload: {e}")


        # 5. Encolar tarea de procesamiento
        use_case_log.debug("Enqueuing document processing task.")
        try:
            task_id = await self.task_queue.enqueue_process_document_task(
                document_id=str(document_id),
                company_id=str(company_id),
                filename=file_name, # Pass original filename for task
                content_type=file_type
            )
            use_case_log.info("Document processing task enqueued.", task_id=task_id)
        except Exception as e:
            use_case_log.error("Failed to enqueue document processing task.", error=str(e), exc_info=True)
            # Mark document as error because task could not be queued
            try:
                await self.document_repository.update_status(document_id, DocumentStatus.ERROR, error_message=f"Failed to queue processing task: {type(e).__name__}")
            except Exception as db_err:
                use_case_log.error("Failed to update document to ERROR after task queue failure.", nested_error=str(db_err))
            raise TaskQueueException(f"Failed to enqueue document processing task: {e}")
        
        use_case_log.info("UploadDocumentUseCase execution finished successfully.")
        return {
            "document_id": str(document_id),
            "task_id": task_id,
            "status": DocumentStatus.UPLOADED.value,
            "message": "Document upload accepted, processing started."
        }