"""
Centralized dependency injection for adapters and use cases in the ingest-service.
Provides factory functions that instantiate adapters and use cases as needed.
This avoids premature instantiation of singletons, especially for resources like DB connections or HTTP clients
that have a lifecycle or might need different configurations (e.g., sync vs async clients).
"""
from app.infrastructure.persistence.postgres_document_repository import PostgresDocumentRepositoryAdapter
from app.infrastructure.persistence.postgres_chunk_repository import PostgresChunkRepositoryAdapter
from app.infrastructure.storage.gcs_adapter import GCSAdapter
from app.infrastructure.vectorstore.milvus_adapter import MilvusAdapter
from app.infrastructure.clients.remote_docproc_adapter import RemoteDocProcAdapter
from app.infrastructure.clients.remote_embedding_adapter import RemoteEmbeddingAdapter
from app.infrastructure.tasks.celery_task_adapter import CeleryTaskAdapter

from app.application.ports.document_repository_port import DocumentRepositoryPort
from app.application.ports.chunk_repository_port import ChunkRepositoryPort
from app.application.ports.file_storage_port import FileStoragePort
from app.application.ports.vector_store_port import VectorStorePort
from app.application.ports.docproc_service_port import DocProcServicePort
from app.application.ports.embedding_service_port import EmbeddingServicePort
from app.application.ports.task_queue_port import TaskQueuePort

from app.application.use_cases.upload_document_use_case import UploadDocumentUseCase
from app.application.use_cases.get_document_status_use_case import GetDocumentStatusUseCase
from app.application.use_cases.list_documents_use_case import ListDocumentsUseCase
from app.application.use_cases.retry_document_use_case import RetryDocumentUseCase
from app.application.use_cases.delete_document_use_case import DeleteDocumentUseCase
from app.application.use_cases.get_document_stats_use_case import GetDocumentStatsUseCase
from app.application.use_cases.process_document_use_case import ProcessDocumentUseCase

# --- Adapter Factories ---
# These functions will be called by FastAPI's Depends or explicitly in the worker.

def get_document_repository() -> DocumentRepositoryPort:
    # PostgresDocumentRepositoryAdapter uses get_db_pool() and get_sync_engine() internally,
    # which manage their own lifecycle.
    return PostgresDocumentRepositoryAdapter()

def get_chunk_repository() -> ChunkRepositoryPort:
    # PostgresChunkRepositoryAdapter uses get_sync_engine() internally.
    return PostgresChunkRepositoryAdapter()

def get_file_storage() -> FileStoragePort:
    # GCSAdapter instantiates GCSClient which is stateless after init.
    return GCSAdapter()

def get_vector_store() -> VectorStorePort:
    # MilvusAdapter manages its connection and collection.
    return MilvusAdapter()

def get_docproc_service() -> DocProcServicePort:
    # RemoteDocProcAdapter instantiates DocProcServiceClient.
    return RemoteDocProcAdapter()

def get_embedding_service() -> EmbeddingServicePort:
    # RemoteEmbeddingAdapter instantiates EmbeddingServiceClient.
    return RemoteEmbeddingAdapter()

def get_task_queue() -> TaskQueuePort:
    # CeleryTaskAdapter uses the global celery_app.
    return CeleryTaskAdapter()


# --- Use Case Factories (for FastAPI Depends) ---

def get_upload_document_use_case(
    doc_repo: DocumentRepositoryPort = Depends(get_document_repository),
    file_storage: FileStoragePort = Depends(get_file_storage),
    task_queue: TaskQueuePort = Depends(get_task_queue)
) -> UploadDocumentUseCase:
    return UploadDocumentUseCase(
        document_repository=doc_repo,
        file_storage=file_storage,
        task_queue=task_queue
    )

def get_get_document_status_use_case(
    doc_repo: DocumentRepositoryPort = Depends(get_document_repository),
    file_storage: FileStoragePort = Depends(get_file_storage),
    vector_store: VectorStorePort = Depends(get_vector_store)
) -> GetDocumentStatusUseCase:
    return GetDocumentStatusUseCase(
        document_repository=doc_repo,
        file_storage=file_storage,
        vector_store=vector_store
    )

def get_list_documents_use_case(
    doc_repo: DocumentRepositoryPort = Depends(get_document_repository),
    file_storage: FileStoragePort = Depends(get_file_storage), # Needed for GetDocumentStatusUseCase dependency
    vector_store: VectorStorePort = Depends(get_vector_store)   # Needed for GetDocumentStatusUseCase dependency
) -> ListDocumentsUseCase:
    return ListDocumentsUseCase(
        document_repository=doc_repo,
        file_storage=file_storage,
        vector_store=vector_store
    )

def get_retry_document_use_case(
    doc_repo: DocumentRepositoryPort = Depends(get_document_repository),
    task_queue: TaskQueuePort = Depends(get_task_queue)
) -> RetryDocumentUseCase:
    return RetryDocumentUseCase(
        document_repository=doc_repo,
        task_queue=task_queue
    )

def get_delete_document_use_case(
    doc_repo: DocumentRepositoryPort = Depends(get_document_repository),
    file_storage: FileStoragePort = Depends(get_file_storage),
    vector_store: VectorStorePort = Depends(get_vector_store)
) -> DeleteDocumentUseCase:
    return DeleteDocumentUseCase(
        document_repository=doc_repo,
        file_storage=file_storage,
        vector_store=vector_store
    )

def get_get_document_stats_use_case(
    doc_repo: DocumentRepositoryPort = Depends(get_document_repository)
) -> GetDocumentStatsUseCase:
    return GetDocumentStatsUseCase(
        document_repository=doc_repo
    )

# For Celery Worker (instantiated directly in the task)
def create_process_document_use_case_for_worker() -> ProcessDocumentUseCase:
    """
    Factory function to create ProcessDocumentUseCase with its (synchronous) dependencies
    for use within the Celery worker context.
    """
    return ProcessDocumentUseCase(
        document_repository=get_document_repository(), # Will use sync methods internally
        chunk_repository=get_chunk_repository(),
        file_storage=get_file_storage(),         # Will use sync methods internally
        docproc_service=get_docproc_service(),     # Will use sync methods internally
        embedding_service=get_embedding_service(), # Will use sync methods internally
        vector_store=get_vector_store()          # Will use sync methods internally
    )