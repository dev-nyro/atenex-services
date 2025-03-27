import uuid
import asyncio
from typing import Dict, Any, Optional, List, Type
import tempfile
import os
from pathlib import Path
import structlog
import base64 # Keep for potential alternative data passing
import io

# --- Haystack Imports ---
from haystack import Pipeline, Document
from haystack.utils import Secret
from haystack.components.converters import (
    PyPDFToDocument,
    TextFileToDocument,
    MarkdownToDocument,
    HTMLToDocument,
    DOCXToDocument,
    # Consider AzureOCRDocumentConverter or others if needed
)
from haystack.components.preprocessors import DocumentSplitter
from haystack.components.embedders import OpenAITextEmbedder, OpenAIDocumentEmbedder # Use both for clarity
from haystack_integrations.document_stores.milvus import MilvusDocumentStore # Assuming package structure
from haystack.components.writers import DocumentWriter
from haystack.dataclasses import ByteStream # For passing content directly

# --- Local Imports ---
from app.tasks.celery_app import celery_app
from app.core.config import settings
from app.db import postgres_client
from app.models.domain import DocumentStatus
# Keep storage client for downloading if needed, or OCR client
from app.services.minio_client import MinioStorageClient
# from app.services.ocr_client import OcrServiceClient # Keep if used

log = structlog.get_logger(__name__)


# --- Haystack Component Initialization ---
# It's often better to initialize components once if possible,
# but within a Celery task, re-initializing might be safer,
# especially for components holding external connections like DocumentStore.

def get_haystack_document_store() -> MilvusDocumentStore:
    """Initializes the MilvusDocumentStore."""
    # Ensure Milvus schema matches Haystack expectations or configure mappings
    return MilvusDocumentStore(
        uri=settings.MILVUS_URI,
        collection_name=settings.MILVUS_COLLECTION_NAME,
        dim=settings.EMBEDDING_DIMENSION,
        embedding_field=settings.MILVUS_EMBEDDING_FIELD,
        content_field=settings.MILVUS_CONTENT_FIELD,
        metadata_fields=settings.MILVUS_METADATA_FIELDS, # Ensure company_id etc. are listed
        index_params=settings.MILVUS_INDEX_PARAMS,
        search_params=settings.MILVUS_SEARCH_PARAMS,
        consistency_level="Strong", # Or other level as needed
        # auto_id=True, # Let Milvus generate IDs if needed, but Haystack might prefer its own
    )

def get_haystack_embedder() -> OpenAIDocumentEmbedder:
    """Initializes the OpenAI Embedder for documents."""
    # Use Secret.from_env_var() which is serializable for pipelines
    return OpenAIDocumentEmbedder(
        api_key=Secret.from_env_var("OPENAI_API_KEY"),
        model=settings.OPENAI_EMBEDDING_MODEL,
        # Consider adding prefix/suffix if needed by the model
        meta_fields_to_embed=["file_name"] # Example: embed filename with content
    )

def get_haystack_splitter() -> DocumentSplitter:
    """Initializes the DocumentSplitter."""
    return DocumentSplitter(
        split_by=settings.SPLITTER_SPLIT_BY,
        split_length=settings.SPLITTER_CHUNK_SIZE,
        split_overlap=settings.SPLITTER_CHUNK_OVERLAP
    )

def get_converter_for_content_type(content_type: str) -> Optional[Type]:
     """Returns the appropriate Haystack Converter class."""
     # Add more mappings as needed
     if content_type == "application/pdf":
         return PyPDFToDocument
     elif content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
         return DOCXToDocument
     elif content_type == "text/plain":
         return TextFileToDocument
     elif content_type == "text/markdown":
         return MarkdownToDocument
     elif content_type == "text/html":
         return HTMLToDocument
     # Add other converters like CSV, JSON etc. if supported
     else:
         return None


# --- Celery Task ---
@celery_app.task(
    bind=True,
    autoretry_for=(Exception,),
    retry_kwargs={'max_retries': 3, 'countdown': 15},
    acks_late=True,
    name="tasks.process_document_haystack"
)
def process_document_haystack_task(
    self, # Task instance
    document_id_str: str,
    company_id_str: str,
    minio_object_name: str, # Changed from file_path
    file_name: str,
    content_type: str,
    original_metadata: Dict[str, Any],
):
    """
    Processes a document using a Haystack pipeline:
    1. Download file from MinIO.
    2. Select appropriate Converter. Handle OCR case.
    3. Build pipeline: Converter -> Splitter -> Embedder -> Writer (Milvus).
    4. Run pipeline.
    5. Update document status in PostgreSQL.
    """
    document_id = uuid.UUID(document_id_str)
    company_id = uuid.UUID(company_id_str)
    task_log = log.bind(document_id=str(document_id), company_id=str(company_id), task_id=self.request.id, file_name=file_name, object_name=minio_object_name)
    task_log.info("Starting Haystack document processing task (MinIO)")

    async def async_process():
        haystack_pipeline = Pipeline()
        processed_docs_count = 0
        minio_client = MinioStorageClient() # Initialize Minio client
        # ocr_client = OcrServiceClient() if settings.OCR_SERVICE_URL else None
        downloaded_file_stream: Optional[io.BytesIO] = None

        try:
            # 0. Mark as processing
            await postgres_client.update_document_status(document_id, DocumentStatus.PROCESSING)
            task_log.info("Document status set to PROCESSING")

            # 1. Download file from MinIO
            task_log.info("Downloading file from MinIO...")
            downloaded_file_stream = await minio_client.download_file_stream(minio_object_name)
            file_bytes = downloaded_file_stream.getvalue() # Get bytes from stream
            task_log.info(f"File downloaded ({len(file_bytes)} bytes)")

            # 2. Prepare Haystack Input (ByteStream)
            doc_meta = {
                "company_id": str(company_id),
                "document_id": str(document_id),
                "file_name": file_name,
                "file_type": content_type,
                **original_metadata
            }
            source_stream = ByteStream(data=file_bytes, meta=doc_meta)

            # 3. Select Converter or Handle OCR (Logic remains the same)
            ConverterClass = get_converter_for_content_type(content_type)

            if content_type in settings.EXTERNAL_OCR_REQUIRED_CONTENT_TYPES and settings.OCR_SERVICE_URL:
                # --- External OCR Path ---
                # ... (Implementation needed if OCR service is used) ...
                # Note: OCR service would need access to the file, potentially via MinIO presigned URL
                # or by downloading it itself using the object_name.
                task_log.error("External OCR path not fully implemented in this example.")
                raise NotImplementedError("External OCR path needs implementation.")

            elif ConverterClass:
                # --- Haystack Converter Path ---
                # ... (Pipeline definition remains the same) ...
                task_log.info(f"Using Haystack converter: {ConverterClass.__name__}")
                haystack_pipeline.add_component("converter", ConverterClass())
                haystack_pipeline.add_component("splitter", get_haystack_splitter())
                haystack_pipeline.add_component("embedder", get_haystack_embedder())
                haystack_pipeline.add_component("writer", DocumentWriter(document_store=get_haystack_document_store()))

                haystack_pipeline.connect("converter.documents", "splitter.documents")
                haystack_pipeline.connect("splitter.documents", "embedder.documents")
                haystack_pipeline.connect("embedder.documents", "writer.documents")

                pipeline_input = {"converter": {"sources": [source_stream]}}
            else:
                # ... (Error handling for unsupported type remains the same) ...
                 task_log.error("No suitable Haystack converter found and not an OCR type.", content_type=content_type)
                 raise ValueError(f"Unsupported content type for Haystack processing: {content_type}")

            # 4. Run the Haystack Pipeline
            task_log.info("Running Haystack indexing pipeline...")
            pipeline_result = haystack_pipeline.run(pipeline_input)
            task_log.info("Haystack pipeline finished.")

            # Check pipeline result (structure depends on Haystack version and components)
            if "writer" in pipeline_result and "documents_written" in pipeline_result["writer"]:
                processed_docs_count = pipeline_result["writer"]["documents_written"]
                task_log.info(f"Documents written to Milvus: {processed_docs_count}")
            else:
                task_log.warning("Could not determine exact number of documents written from pipeline output.", output_keys=list(pipeline_result.keys()))
                if "splitter" in pipeline_result and "documents" in pipeline_result["splitter"]:
                     processed_docs_count = len(pipeline_result["splitter"]["documents"])
                     task_log.info(f"Inferring processed count from splitter output: {processed_docs_count}")
                else:
                     processed_docs_count = 0 # Assume 0 if count cannot be determined
                     task_log.warning("Processed count set to 0 as it could not be determined.")


            # 4. Update Final Status in PostgreSQL
            await postgres_client.update_document_status(
                document_id,
                DocumentStatus.PROCESSED, # Or INDEXED
                chunk_count=processed_docs_count
            )
            task_log.info("Document status set to PROCESSED in PostgreSQL", chunk_count=processed_docs_count)

        except Exception as e:
            task_log.error("Error during Haystack document processing", error=str(e), exc_info=True)
            try:
                await postgres_client.update_document_status(
                    document_id,
                    DocumentStatus.ERROR,
                    error_message=f"Haystack Pipeline Error: {type(e).__name__}: {str(e)[:250]}"
                )
                task_log.info("Document status set to ERROR in PostgreSQL")
            except Exception as db_update_err:
                task_log.error("Failed to update document status to ERROR after processing failure", nested_error=str(db_update_err), exc_info=True)
            raise e
        finally:
            # Clean up downloaded stream
            if downloaded_file_stream:
                downloaded_file_stream.close()
            # Minio client doesn't need async close
            task_log.info("Cleaned up resources for task.")

    # --- Run the async part ---
    try:
        asyncio.run(async_process())
        log.info("Haystack document processing task finished successfully", document_id=str(document_id))
    except Exception as e:
        log.error("Async processing wrapper caught exception, letting Celery handle retry/failure", document_id=str(document_id), error=str(e))
        raise e