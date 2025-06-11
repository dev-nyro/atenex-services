import uuid
import tempfile
import pathlib
import asyncio # Para ejecutar tareas async desde sync si es necesario
from typing import Dict, Any, List

from app.domain.entities import Document, Chunk, ChunkMetadata
from app.domain.enums import DocumentStatus, ChunkVectorStatus
from app.application.ports.document_repository_port import DocumentRepositoryPort
from app.application.ports.chunk_repository_port import ChunkRepositoryPort
from app.application.ports.file_storage_port import FileStoragePort
from app.application.ports.docproc_service_port import DocProcServicePort
from app.application.ports.embedding_service_port import EmbeddingServicePort
from app.application.ports.vector_store_port import VectorStorePort
import structlog

log = structlog.get_logger(__name__)

class ProcessDocumentUseCase:
    def __init__(self,
                 document_repository: DocumentRepositoryPort,
                 chunk_repository: ChunkRepositoryPort,
                 file_storage: FileStoragePort,
                 docproc_service: DocProcServicePort,
                 embedding_service: EmbeddingServicePort,
                 vector_store: VectorStorePort):
        self.document_repository = document_repository
        self.chunk_repository = chunk_repository
        self.file_storage = file_storage
        self.docproc_service = docproc_service
        self.embedding_service = embedding_service
        self.vector_store = vector_store

    def execute(self, *,
                document_id_str: str, # Renamed for clarity
                company_id_str: str,  # Renamed for clarity
                filename: str, # Renamed from file_path as it's just the name here
                content_type: str # Renamed from file_type for consistency
                ) -> Dict[str, Any]:
        
        document_id = uuid.UUID(document_id_str)
        company_id = uuid.UUID(company_id_str)

        case_log = log.bind(document_id=document_id_str, company_id=company_id_str, filename=filename)
        case_log.info("ProcessDocumentUseCase execution started.")

        gcs_file_path = f"{company_id_str}/{document_id_str}/{filename}" # Construct GCS path

        try:
            # 1. Actualizar estado a PROCESSING
            case_log.info("Updating document status to PROCESSING.")
            # Llamamos al método síncrono del repositorio
            if not self.document_repository.update_status_sync(document_id, DocumentStatus.PROCESSING):
                case_log.warning("Failed to update document status to PROCESSING, document might not exist or already processed/deleted.")
                # Consider if this should be a hard failure or specific return
                return {"error": "Failed to set document to processing state", "status_code": 409}


            # 2. Descargar archivo de almacenamiento
            with tempfile.TemporaryDirectory() as temp_dir:
                local_path = pathlib.Path(temp_dir) / filename
                case_log.info("Downloading file from storage.", gcs_path=gcs_file_path, local_path=str(local_path))
                self.file_storage.download(gcs_file_path, str(local_path)) # Assumes sync download
                case_log.info("File downloaded successfully.")

                # 3. Procesar documento (DocProc)
                case_log.info("Processing document with DocProcService.")
                with open(local_path, "rb") as f:
                    file_bytes = f.read()
                
                docproc_result = self.docproc_service.process_document(
                    file_bytes=file_bytes,
                    original_filename=filename,
                    content_type=content_type,
                    document_id=document_id_str,
                    company_id=company_id_str
                )
            case_log.info("Document processed by DocProcService.", num_chunks_from_docproc=len(docproc_result.get("data", {}).get("chunks", [])))
            
            # Extract chunks from docproc response, expecting a list of dicts
            chunks_from_docproc = docproc_result.get("data", {}).get("chunks", [])
            if not chunks_from_docproc:
                case_log.warning("DocProcService returned no chunks. Finalizing as processed with 0 chunks.")
                self.document_repository.update_status_sync(document_id, DocumentStatus.PROCESSED, chunk_count=0)
                return {"document_id": document_id_str, "status": DocumentStatus.PROCESSED.value, "message": "Document processed, no textual content found.", "chunk_count": 0}

            # 4. Obtener embeddings
            case_log.info("Getting embeddings for processed chunks.")
            chunk_texts_for_embedding = [chunk['text'] for chunk in chunks_from_docproc if chunk.get('text','').strip()]
            if not chunk_texts_for_embedding:
                case_log.warning("No non-empty text found in chunks from DocProc. Finalizing with 0 chunks.")
                self.document_repository.update_status_sync(document_id, DocumentStatus.PROCESSED, chunk_count=0)
                return {"document_id": document_id_str, "status": DocumentStatus.PROCESSED.value, "message": "Document processed, no valid textual content in chunks.", "chunk_count": 0}

            embeddings, model_info = self.embedding_service.get_embeddings(chunk_texts_for_embedding) # Assumes sync
            case_log.info("Embeddings received.", num_embeddings=len(embeddings), model_info=model_info)

            if len(embeddings) != len(chunk_texts_for_embedding):
                 error_msg = "Mismatch between number of texts sent for embedding and embeddings received."
                 case_log.error(error_msg, num_texts=len(chunk_texts_for_embedding), num_embeddings=len(embeddings))
                 self.document_repository.update_status_sync(document_id, DocumentStatus.ERROR, error_message=error_msg)
                 raise RuntimeError(error_msg)

            # Prepare chunks_with_embeddings for MilvusAdapter
            # Ensure that only chunks that had text and got an embedding are passed
            chunks_with_embeddings_for_milvus: List[Dict[str, Any]] = []
            embedding_idx = 0
            for i, original_chunk_data in enumerate(chunks_from_docproc):
                if original_chunk_data.get('text','').strip(): # If this chunk had text
                    if embedding_idx < len(embeddings):
                        chunks_with_embeddings_for_milvus.append({
                            "text": original_chunk_data['text'],
                            "embedding": embeddings[embedding_idx],
                            "source_metadata": original_chunk_data.get('source_metadata', {}),
                            # Include original_chunk_index if needed by MilvusAdapter for PK generation strategy that relies on it
                            "original_chunk_index": i 
                        })
                        embedding_idx += 1
                    else:
                        case_log.warning("More text chunks than embeddings, skipping a chunk.", original_chunk_index=i)


            # 5. Indexar en vector store
            case_log.info("Indexing chunks in VectorStore (Milvus).")
            inserted_milvus_count, _, chunks_for_pg_raw = self.vector_store.index_chunks(
                chunks_with_embeddings=chunks_with_embeddings_for_milvus,
                filename=filename,
                company_id=company_id_str,
                document_id=document_id_str,
                delete_existing=True # Always delete existing for a full reprocess
            )
            case_log.info("VectorStore indexing complete.", inserted_milvus_count=inserted_milvus_count)

            # 6. Guardar chunks en DB
            # Convert raw dicts from MilvusAdapter (which are prepared for PG) to Chunk domain entities
            chunk_domain_objects_for_pg: List[Chunk] = []
            for pg_data in chunks_for_pg_raw:
                # The 'metadata' in pg_data should already be a dict from MilvusAdapter preparation
                # If 'id' is already a UUID from MilvusAdapter, use it, else it will be generated by DB default.
                # Chunk entity expects metadata as ChunkMetadata object.
                pg_data_meta = pg_data.get('metadata', {})
                chunk_meta_obj = ChunkMetadata(**pg_data_meta) if isinstance(pg_data_meta, dict) else ChunkMetadata()
                
                chunk_domain_objects_for_pg.append(Chunk(
                    id=pg_data.get('id'), # Allow adapter to provide ID, or DB to generate
                    document_id=uuid.UUID(pg_data['document_id']),
                    company_id=uuid.UUID(pg_data['company_id']),
                    chunk_index=pg_data['chunk_index'],
                    content=pg_data['content'],
                    metadata=chunk_meta_obj,
                    embedding_id=pg_data.get('embedding_id'), # This is the Milvus PK
                    vector_status=ChunkVectorStatus(pg_data.get('vector_status', 'pending'))
                ))
            
            if chunk_domain_objects_for_pg:
                case_log.info("Saving chunk details to PostgreSQL.", num_chunks_to_save=len(chunk_domain_objects_for_pg))
                inserted_pg_count = self.chunk_repository.save_bulk(chunk_domain_objects_for_pg) # Assumes sync
                case_log.info("Chunks saved to PostgreSQL.", inserted_pg_count=inserted_pg_count)
                final_chunk_count = inserted_pg_count
            else:
                case_log.warning("No chunks prepared for PostgreSQL insertion.")
                final_chunk_count = 0
            
            # 7. Actualizar estado a PROCESSED
            case_log.info("Updating document status to PROCESSED.", final_chunk_count=final_chunk_count)
            self.document_repository.update_status_sync(document_id, DocumentStatus.PROCESSED, chunk_count=final_chunk_count)
            
            case_log.info("ProcessDocumentUseCase execution finished successfully.")
            return {
                "document_id": document_id_str,
                "status": DocumentStatus.PROCESSED.value,
                "message": "Document processed successfully.",
                "chunk_count": final_chunk_count
            }

        except Exception as e:
            error_msg_for_db = f"Processing failed: {type(e).__name__} - {str(e)[:250]}"
            case_log.error("Error during ProcessDocumentUseCase execution", error=str(e), exc_info=True)
            try:
                self.document_repository.update_status_sync(document_id, DocumentStatus.ERROR, error_message=error_msg_for_db)
                case_log.info("Document status updated to ERROR due to exception.")
            except Exception as db_update_err:
                case_log.error("Failed to update document status to ERROR after main exception", nested_error=str(db_update_err))
            # Re-raise the original exception to be handled by Celery
            raise