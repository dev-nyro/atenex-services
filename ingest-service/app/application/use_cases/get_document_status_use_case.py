import uuid
from typing import Optional, Dict, Any
from datetime import datetime, timezone, timedelta

from app.application.ports.document_repository_port import DocumentRepositoryPort
from app.application.ports.file_storage_port import FileStoragePort
from app.application.ports.vector_store_port import VectorStorePort
from app.domain.entities import Document
from app.domain.enums import DocumentStatus
from app.domain.exceptions import DocumentNotFoundException
import structlog

log = structlog.get_logger(__name__)

GRACE_PERIOD_SECONDS = 120 # Grace period for inconsistencies for recently processed docs

class GetDocumentStatusUseCase:
    def __init__(self, 
                 document_repository: DocumentRepositoryPort, 
                 file_storage: FileStoragePort, 
                 vector_store: VectorStorePort):
        self.document_repository = document_repository
        self.file_storage = file_storage # Needs to offer exists_sync or be async
        self.vector_store = vector_store # Needs to offer count_document_chunks_async

    async def execute(self, *,
                     document_id: uuid.UUID,
                     company_id: uuid.UUID) -> Optional[Document]:
        
        case_log = log.bind(document_id=str(document_id), company_id=str(company_id))
        case_log.info("GetDocumentStatusUseCase execution started.")

        doc = await self.document_repository.find_by_id(document_id, company_id)
        if not doc:
            case_log.warning("Document not found in repository.")
            raise DocumentNotFoundException(document_id=str(document_id))

        case_log.info("Document retrieved from repository.", status=doc.status.value)
        
        # --- Live Checks & Potential Auto-Correction ---
        needs_db_update = False
        original_status = doc.status
        original_chunk_count = doc.chunk_count
        original_error_message = doc.error_message

        # Check GCS
        live_gcs_exists = False
        if doc.file_path:
            try:
                live_gcs_exists = await self.file_storage.exists(doc.file_path)
                case_log.debug("GCS existence check complete.", gcs_path=doc.file_path, exists=live_gcs_exists)
                if not live_gcs_exists and doc.status not in [DocumentStatus.ERROR, DocumentStatus.PENDING]:
                    case_log.warning("File missing in GCS but DB status suggests otherwise.", current_db_status=doc.status.value)
                    if not self._is_in_grace_period(doc.updated_at, doc.status):
                        needs_db_update = True
                        doc.status = DocumentStatus.ERROR
                        doc.error_message = self._append_error(doc.error_message, "File missing from storage.")
                        doc.chunk_count = 0 
            except Exception as e_gcs:
                case_log.error("Error checking GCS file existence.", error=str(e_gcs), exc_info=True)
                # Assume file doesn't exist if check fails, and potentially mark for error
                if doc.status not in [DocumentStatus.ERROR, DocumentStatus.PENDING] and not self._is_in_grace_period(doc.updated_at, doc.status):
                    needs_db_update = True
                    doc.status = DocumentStatus.ERROR
                    doc.error_message = self._append_error(doc.error_message, f"GCS check error: {type(e_gcs).__name__}.")
                    doc.chunk_count = 0
        else:
            case_log.warning("Document file_path is null, cannot check GCS.", doc_id=str(doc.id))
            if doc.status not in [DocumentStatus.ERROR, DocumentStatus.PENDING] and not self._is_in_grace_period(doc.updated_at, doc.status):
                needs_db_update = True
                doc.status = DocumentStatus.ERROR
                doc.error_message = self._append_error(doc.error_message, "File path missing in DB.")
                doc.chunk_count = 0
        
        doc.gcs_exists = live_gcs_exists


        # Check Milvus
        live_milvus_chunk_count = -1
        try:
            live_milvus_chunk_count = await self.vector_store.count_document_chunks_async(str(company_id), str(document_id))
            case_log.debug("Milvus chunk count check complete.", count=live_milvus_chunk_count)

            if live_milvus_chunk_count == -1 and doc.status == DocumentStatus.PROCESSED: # Error from count
                 if not self._is_in_grace_period(doc.updated_at, doc.status):
                    needs_db_update = True
                    doc.status = DocumentStatus.ERROR
                    doc.error_message = self._append_error(doc.error_message, "Milvus count check failed for processed doc.")
            elif live_milvus_chunk_count > 0:
                if doc.status in [DocumentStatus.ERROR, DocumentStatus.UPLOADED, DocumentStatus.PROCESSING] and live_gcs_exists:
                    case_log.warning("Inconsistency: Chunks in Milvus & GCS, but DB status is not PROCESSED. Correcting.", current_db_status=doc.status.value)
                    needs_db_update = True
                    doc.status = DocumentStatus.PROCESSED
                    doc.error_message = None # Clear error if correcting to processed
                if doc.status == DocumentStatus.PROCESSED and doc.chunk_count != live_milvus_chunk_count:
                    case_log.warning("Inconsistency: DB chunk_count differs from Milvus. Updating DB.", db_count=doc.chunk_count, milvus_count=live_milvus_chunk_count)
                    needs_db_update = True
            elif live_milvus_chunk_count == 0:
                if doc.status == DocumentStatus.PROCESSED:
                    if not self._is_in_grace_period(doc.updated_at, doc.status):
                        case_log.warning("Inconsistency: DB status PROCESSED but no chunks in Milvus. Correcting.")
                        needs_db_update = True
                        doc.status = DocumentStatus.ERROR
                        doc.error_message = self._append_error(doc.error_message, "Processed data missing from Milvus.")
                if doc.status != DocumentStatus.PROCESSED and doc.chunk_count != 0: # e.g. ERROR but had chunks
                    needs_db_update = True
            
            if needs_db_update and doc.status == DocumentStatus.PROCESSED: # If corrected to processed
                doc.chunk_count = live_milvus_chunk_count
            elif doc.status == DocumentStatus.ERROR: # If set to error
                doc.chunk_count = 0


        except Exception as e_milvus:
            case_log.error("Error checking Milvus chunk count.", error=str(e_milvus), exc_info=True)
            live_milvus_chunk_count = -1 # Indicate error
            if doc.status == DocumentStatus.PROCESSED and not self._is_in_grace_period(doc.updated_at, doc.status):
                needs_db_update = True
                doc.status = DocumentStatus.ERROR
                doc.error_message = self._append_error(doc.error_message, f"Milvus check error: {type(e_milvus).__name__}.")
                doc.chunk_count = 0
        
        doc.milvus_chunk_count_live = live_milvus_chunk_count


        # Persist changes if any inconsistencies were corrected
        if needs_db_update:
            # Check if status actually changed to avoid unnecessary DB write if only chunk_count/error_message changed
            if doc.status != original_status or doc.chunk_count != original_chunk_count or doc.error_message != original_error_message:
                case_log.warning("Inconsistency detected, updating document in DB.", new_status=doc.status.value, new_chunk_count=doc.chunk_count, new_error=doc.error_message)
                try:
                    await self.document_repository.update_status(
                        doc_id=doc.id, 
                        status=doc.status, 
                        chunk_count=doc.chunk_count, 
                        error_message=doc.error_message,
                        updated_at=datetime.now(timezone.utc) # Ensure updated_at is refreshed
                    )
                    case_log.info("Document successfully updated in DB after consistency checks.")
                except Exception as e_db_update:
                    case_log.error("Failed to update document in DB after consistency checks.", error=str(e_db_update), exc_info=True)
                    # Potentially revert doc object to original state or handle error
                    # For now, we'll return the corrected doc object, but log the DB update failure.
                    doc.status = original_status # Revert if DB update failed to reflect actual state
                    doc.chunk_count = original_chunk_count
                    doc.error_message = self._append_error(original_error_message, "DB update failed during self-correction.")
            else:
                 case_log.info("DB update deemed necessary but no actual change in status, chunk_count or error_message. Skipping DB write.")


        return doc

    def _is_in_grace_period(self, updated_at: Optional[datetime], status: DocumentStatus) -> bool:
        if status != DocumentStatus.PROCESSED: # Grace period only for recently processed docs
            return False
        if not updated_at:
            return False # No timestamp, assume not in grace period
        
        now_utc = datetime.now(timezone.utc)
        # Ensure updated_at is offset-aware for comparison
        if updated_at.tzinfo is None:
            updated_at_aware = updated_at.replace(tzinfo=timezone.utc) # Assume UTC if naive
        else:
            updated_at_aware = updated_at.astimezone(timezone.utc)

        if (now_utc - updated_at_aware).total_seconds() < GRACE_PERIOD_SECONDS:
            log.debug("Document is within grace period for inconsistency checks.")
            return True
        return False

    def _append_error(self, existing_msg: Optional[str], new_err: str) -> str:
        if not existing_msg:
            return new_err
        if new_err in existing_msg: # Avoid duplicate messages
            return existing_msg
        return f"{existing_msg.strip()} {new_err.strip()}"