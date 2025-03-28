# ./app/tasks/process_document.py
import uuid
import asyncio
from typing import Dict, Any, Optional, List, Type
import tempfile
import os
from pathlib import Path
import structlog
import base64
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
)
from haystack.components.preprocessors import DocumentSplitter
from haystack.components.embedders import OpenAIDocumentEmbedder # Use DocumentEmbedder for indexing
from haystack_integrations.document_stores.milvus import MilvusDocumentStore
from haystack.components.writers import DocumentWriter
from haystack.dataclasses import ByteStream

# --- Local Imports ---
from app.tasks.celery_app import celery_app
from app.core.config import settings
from app.db import postgres_client
from app.models.domain import DocumentStatus
from app.services.minio_client import MinioStorageClient
# from app.services.ocr_client import OcrServiceClient

log = structlog.get_logger(__name__)


# --- Haystack Component Initialization (Functions remain the same) ---
def get_haystack_document_store() -> MilvusDocumentStore:
    """Initializes the MilvusDocumentStore."""
    log.debug("Initializing MilvusDocumentStore",
             uri=settings.MILVUS_URI,
             collection=settings.MILVUS_COLLECTION_NAME,
             dim=settings.EMBEDDING_DIMENSION,
             metadata_fields=settings.MILVUS_METADATA_FIELDS)
    # Ensure Milvus schema matches Haystack expectations or configure mappings
    return MilvusDocumentStore(
        uri=settings.MILVUS_URI, # Use uri as per milvus-haystack docs for server
        collection_name=settings.MILVUS_COLLECTION_NAME,
        dim=settings.EMBEDDING_DIMENSION,
        embedding_field=settings.MILVUS_EMBEDDING_FIELD,
        content_field=settings.MILVUS_CONTENT_FIELD,
        metadata_fields=settings.MILVUS_METADATA_FIELDS, # Crucial: Use the list from settings
        index_params=settings.MILVUS_INDEX_PARAMS,
        search_params=settings.MILVUS_SEARCH_PARAMS,
        consistency_level="Strong", # Or Bounded Staleness for higher QPS if acceptable
        # auto_id=False, # Default is False, Haystack manages IDs
        # drop_old=False, # Be careful with True in production - default is False
    )

def get_haystack_embedder() -> OpenAIDocumentEmbedder:
    """Initializes the OpenAI Embedder for documents."""
    # Check if API key env var is actually set
    api_key_env_var = "OPENAI_API_KEY" # Default used by Secret.from_env_var("OPENAI_API_KEY")
    if not os.getenv(api_key_env_var):
        log.error(f"Environment variable {api_key_env_var} not set for OpenAI Embedder.")
        # Depending on policy, you might raise an error here or let Haystack fail later
        # raise ValueError(f"{api_key_env_var} not set.")

    return OpenAIDocumentEmbedder(
        # api_key=Secret.from_env_var("OPENAI_API_KEY"), # Default secret handling is good
        model=settings.OPENAI_EMBEDDING_MODEL,
        # Optional: Add batch size, progress bar etc.
        # batch_size=32,
        # progress_bar=True, # Might be noisy in logs
        meta_fields_to_embed=[] # Adjust if you want specific metadata embedded with content
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
     # Mapping remains the same
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
     else:
         log.warning("No specific Haystack converter found for content type", content_type=content_type)
         return None


# --- Celery Task ---
@celery_app.task(
    bind=True,
    autoretry_for=(Exception,), # Retry on generic exceptions
    retry_kwargs={'max_retries': 2, 'countdown': 60}, # Adjust retry strategy
    acks_late=True, # Acknowledge after task completion/failure
    name="tasks.process_document_haystack"
)
def process_document_haystack_task(
    self, # Task instance for context (e.g., self.request.id)
    document_id_str: str,
    company_id_str: str,
    minio_object_name: str,
    file_name: str,
    content_type: str,
    original_metadata: Dict[str, Any], # Metadata from API request
):
    """
    Processes a document using a Haystack pipeline (MinIO -> Haystack -> Milvus -> Supabase Status).
    """
    document_id = uuid.UUID(document_id_str)
    company_id = uuid.UUID(company_id_str)
    task_log = log.bind(document_id=str(document_id), company_id=str(company_id),
                      task_id=self.request.id, file_name=file_name, object_name=minio_object_name)
    task_log.info("Starting Haystack document processing task")

    async def async_process():
        haystack_pipeline = Pipeline()
        processed_docs_count = 0
        minio_client = MinioStorageClient()
        downloaded_file_stream: Optional[io.BytesIO] = None

        try:
            # 0. Mark as processing in Supabase
            await postgres_client.update_document_status(document_id, DocumentStatus.PROCESSING)
            task_log.info("Document status set to PROCESSING")

            # 1. Download file from MinIO
            task_log.info("Downloading file from MinIO...")
            downloaded_file_stream = await minio_client.download_file_stream(minio_object_name)
            file_bytes = downloaded_file_stream.getvalue()
            if not file_bytes:
                raise ValueError("Downloaded file from MinIO is empty.")
            task_log.info(f"File downloaded successfully ({len(file_bytes)} bytes)")

            # 2. Prepare Haystack Input (ByteStream) with FILTERED metadata
            # *** CORRECCIÓN IMPORTANTE ***
            # Filter metadata to only include fields defined in settings.MILVUS_METADATA_FIELDS
            allowed_meta_keys = set(settings.MILVUS_METADATA_FIELDS)
            doc_meta = {
                "company_id": str(company_id),     # Ensure these core fields are strings if needed
                "document_id": str(document_id),
                "file_name": file_name,
                "file_type": content_type,
            }
            # Add allowed fields from original_metadata
            filtered_original_meta = {}
            for key, value in original_metadata.items():
                if key in allowed_meta_keys:
                    # Optional: Basic type casting/validation if needed before adding
                    # For simplicity, adding directly here. Ensure types match Milvus schema.
                    doc_meta[key] = value
                    filtered_original_meta[key] = value
                else:
                    task_log.debug("Ignoring metadata field not defined in MILVUS_METADATA_FIELDS", field=key)

            task_log.debug("Filtered metadata for Haystack/Milvus", final_meta=doc_meta)

            source_stream = ByteStream(data=file_bytes, meta=doc_meta) # Use filtered metadata

            # 3. Select Converter or Handle OCR
            ConverterClass = get_converter_for_content_type(content_type)

            if content_type in settings.EXTERNAL_OCR_REQUIRED_CONTENT_TYPES and settings.OCR_SERVICE_URL:
                 # Placeholder for OCR path - requires implementation
                 task_log.error("External OCR path not implemented.")
                 raise NotImplementedError("External OCR path needs implementation.")

            elif ConverterClass:
                 # Build Haystack indexing pipeline
                 task_log.info(f"Using Haystack converter: {ConverterClass.__name__}")
                 haystack_pipeline.add_component("converter", ConverterClass())
                 haystack_pipeline.add_component("splitter", get_haystack_splitter())
                 haystack_pipeline.add_component("embedder", get_haystack_embedder())
                 # Initialize DocumentStore inside the task for safety with connections
                 document_store = get_haystack_document_store()
                 haystack_pipeline.add_component("writer", DocumentWriter(document_store=document_store))

                 # Connect components
                 haystack_pipeline.connect("converter.documents", "splitter.documents")
                 haystack_pipeline.connect("splitter.documents", "embedder.documents")
                 haystack_pipeline.connect("embedder.documents", "writer.documents")

                 pipeline_input = {"converter": {"sources": [source_stream]}}
            else:
                 task_log.error("Unsupported content type for Haystack processing", content_type=content_type)
                 raise ValueError(f"Unsupported content type: {content_type}")

            # 4. Run the Haystack Pipeline
            task_log.info("Running Haystack indexing pipeline...")
            # Note: Haystack pipeline.run() is synchronous. Consider running in a thread pool
            # if embedding/writing becomes a bottleneck blocking the asyncio event loop,
            # though Celery workers handle concurrency at the process level.
            pipeline_result = haystack_pipeline.run(pipeline_input)
            task_log.info("Haystack pipeline finished.")

            # 5. Check pipeline result and get count
            if "writer" in pipeline_result and isinstance(pipeline_result["writer"], dict) and "documents_written" in pipeline_result["writer"]:
                 processed_docs_count = pipeline_result["writer"]["documents_written"]
                 task_log.info(f"Documents written to Milvus: {processed_docs_count}")
            else:
                 # Fallback logic if writer output format changes or isn't as expected
                 task_log.warning("Could not determine count from writer output, checking splitter.",
                                  writer_output=pipeline_result.get("writer"))
                 if "splitter" in pipeline_result and isinstance(pipeline_result["splitter"], dict) and "documents" in pipeline_result["splitter"]:
                      processed_docs_count = len(pipeline_result["splitter"]["documents"])
                      task_log.info(f"Inferred processed chunk count from splitter: {processed_docs_count}")
                 else:
                      processed_docs_count = 0 # Assume 0 if count cannot be determined
                      task_log.warning("Processed chunk count could not be determined, setting to 0.")


            # 6. Update Final Status in Supabase
            final_status = DocumentStatus.PROCESSED # Or DocumentStatus.INDEXED if you prefer
            await postgres_client.update_document_status(
                document_id,
                final_status,
                chunk_count=processed_docs_count,
                # file_path is already set, no need to update unless necessary
            )
            task_log.info("Document status set to PROCESSED in Supabase", chunk_count=processed_docs_count)

        except Exception as e:
            task_log.error("Error during Haystack document processing", error=str(e), exc_info=True)
            try:
                # Attempt to mark as error in Supabase
                await postgres_client.update_document_status(
                    document_id,
                    DocumentStatus.ERROR,
                    error_message=f"Processing Error: {type(e).__name__}: {str(e)[:250]}" # Truncated error
                )
                task_log.info("Document status set to ERROR in Supabase due to processing failure.")
            except Exception as db_update_err:
                # Log if updating status also fails
                task_log.error("Failed to update document status to ERROR after processing failure",
                               nested_error=str(db_update_err), exc_info=True)
            # Re-raise the exception to let Celery know the task failed (for retries etc.)
            raise e
        finally:
            # Clean up resources
            if downloaded_file_stream:
                downloaded_file_stream.close()
            task_log.debug("Cleaned up resources for task.")

    # --- Run the async part within the synchronous Celery task ---
    try:
        # Use asyncio.run() to execute the async logic within the sync task runner
        asyncio.run(async_process())
        task_log.info("Haystack document processing task finished successfully.")
    except Exception:
        # The error is already logged and potentially status updated inside async_process.
        # We just need Celery to see the failure. The exception is re-raised from async_process.
        task_log.warning("Haystack processing task failed.")
        # No need to raise again here if async_process already raises.
        pass # Celery will handle the exception raised by asyncio.run() which comes from async_process