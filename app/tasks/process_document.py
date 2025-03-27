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
from app.services.storage_client import StorageServiceClient
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
    autoretry_for=(Exception,), # Consider more specific retry exceptions
    retry_kwargs={'max_retries': 3, 'countdown': 15}, # Increased countdown
    acks_late=True,
    name="tasks.process_document_haystack"
)
def process_document_haystack_task(
    self, # Task instance
    document_id_str: str,
    company_id_str: str,
    file_path: str, # Path from Storage Service
    file_name: str,
    content_type: str,
    original_metadata: Dict[str, Any], # Metadata provided during upload
):
    """
    Processes a document using a Haystack pipeline:
    1. Select appropriate Converter. Handle OCR case.
    2. Build pipeline: Converter -> Splitter -> Embedder -> Writer (Milvus).
    3. Run pipeline.
    4. Update document status in PostgreSQL.
    """
    document_id = uuid.UUID(document_id_str)
    company_id = uuid.UUID(company_id_str)
    task_log = log.bind(document_id=str(document_id), company_id=str(company_id), task_id=self.request.id, file_name=file_name, file_path=file_path)
    task_log.info("Starting Haystack document processing task")

    async def async_process():
        haystack_pipeline = Pipeline()
        processed_docs_count = 0
        # Initialize clients needed (Storage for download, OCR if used)
        storage_client = StorageServiceClient()
        # ocr_client = OcrServiceClient() if settings.OCR_SERVICE_URL else None

        try:
            # 0. Mark as processing
            await postgres_client.update_document_status(document_id, DocumentStatus.PROCESSING)
            task_log.info("Document status set to PROCESSING")

            # 1. Prepare Haystack Document Input
            #    We need the file content as ByteStream for most converters
            #    Download from Storage Service (alternative: stream if possible)
            task_log.info("Downloading file from storage...")
            # This part needs the Storage Service to have a download endpoint
            # file_content_bytes = await storage_client.download_file(file_path) # Implement download_file
            # For now, simulate reading from local path if StorageService download isn't ready
            # In K8s, this path needs to be accessible by the worker (e.g., mounted volume)
            local_file_path = Path(file_path) # Assuming file_path is somehow accessible locally/mounted
            if not local_file_path.is_file():
                 task_log.error("File not found at accessible path", path=file_path)
                 raise FileNotFoundError(f"File not found at path: {file_path}")

            # Create ByteStream for Haystack
            # Combine original metadata with system metadata
            doc_meta = {
                "company_id": str(company_id), # Ensure it's a string for Haystack meta
                "document_id": str(document_id),
                "file_name": file_name,
                "file_type": content_type,
                **original_metadata # Add user-provided metadata
            }
            # Read file content into ByteStream
            with open(local_file_path, "rb") as f:
                file_bytes = f.read()

            source_stream = ByteStream(data=file_bytes, meta=doc_meta)
            task_log.info(f"File read into ByteStream ({len(file_bytes)} bytes)")


            # 2. Select Converter or Handle OCR
            ConverterClass = get_converter_for_content_type(content_type)

            if content_type in settings.EXTERNAL_OCR_REQUIRED_CONTENT_TYPES and settings.OCR_SERVICE_URL:
                # --- External OCR Path ---
                task_log.info("External OCR required, calling OCR service...")
                # ocr_client = OcrServiceClient() # Initialize if needed
                # extracted_text = await ocr_client.extract_text(company_id, file_path)
                # await ocr_client.close()
                # task_log.info(f"Text extracted via external OCR ({len(extracted_text)} chars)")
                # if not extracted_text or extracted_text.isspace():
                #     raise ValueError("External OCR returned no text.")
                # # Create a single Haystack Document from the OCR text
                # initial_doc = Document(content=extracted_text, meta=doc_meta)
                # # Build pipeline starting from Splitter
                # haystack_pipeline.add_component("splitter", get_haystack_splitter())
                # haystack_pipeline.add_component("embedder", get_haystack_embedder())
                # haystack_pipeline.add_component("writer", DocumentWriter(document_store=get_haystack_document_store()))
                # # Connect: Splitter -> Embedder -> Writer
                # haystack_pipeline.connect("splitter.documents", "embedder.documents")
                # haystack_pipeline.connect("embedder.documents", "writer.documents")
                # pipeline_input = {"splitter": {"documents": [initial_doc]}}
                # # We skip the converter in this path
                task_log.error("External OCR path not fully implemented in this example.")
                raise NotImplementedError("External OCR path needs implementation.")


            elif ConverterClass:
                # --- Haystack Converter Path ---
                task_log.info(f"Using Haystack converter: {ConverterClass.__name__}")
                haystack_pipeline.add_component("converter", ConverterClass())
                haystack_pipeline.add_component("splitter", get_haystack_splitter())
                haystack_pipeline.add_component("embedder", get_haystack_embedder())
                haystack_pipeline.add_component("writer", DocumentWriter(document_store=get_haystack_document_store()))

                # Connect: Converter -> Splitter -> Embedder -> Writer
                haystack_pipeline.connect("converter.documents", "splitter.documents")
                haystack_pipeline.connect("splitter.documents", "embedder.documents")
                haystack_pipeline.connect("embedder.documents", "writer.documents")

                # Input for the pipeline run
                pipeline_input = {"converter": {"sources": [source_stream]}}

            else:
                task_log.error("No suitable Haystack converter found and not an OCR type.", content_type=content_type)
                raise ValueError(f"Unsupported content type for Haystack processing: {content_type}")


            # 3. Run the Haystack Pipeline
            task_log.info("Running Haystack indexing pipeline...")
            # pipeline.show() # Useful for debugging pipeline structure
            pipeline_result = haystack_pipeline.run(pipeline_input)
            task_log.info("Haystack pipeline finished.")

            # Check pipeline result (structure depends on Haystack version and components)
            if "writer" in pipeline_result and "documents_written" in pipeline_result["writer"]:
                processed_docs_count = pipeline_result["writer"]["documents_written"]
                task_log.info(f"Documents written to Milvus: {processed_docs_count}")
            else:
                # Fallback or alternative check if the output structure is different
                # Maybe check logs or document store count difference if possible
                task_log.warning("Could not determine exact number of documents written from pipeline output.", output_keys=pipeline_result.keys())
                # Assume success if no error, but count might be inaccurate
                # We need the count for the postgres update. If splitter output is available, use that length.
                if "splitter" in pipeline_result and "documents" in pipeline_result["splitter"]:
                     processed_docs_count = len(pipeline_result["splitter"]["documents"])
                     task_log.info(f"Inferring processed count from splitter output: {processed_docs_count}")
                elif processed_docs_count == 0: # If still 0, mark as potentially failed or 0 chunks
                     # Decide if 0 chunks is an error or valid state
                     task_log.warning("Pipeline ran but no documents seem to have been written/counted.")
                     # Let's consider 0 chunks a valid processed state for now
                     processed_docs_count = 0


            # 4. Update Final Status in PostgreSQL
            await postgres_client.update_document_status(
                document_id,
                DocumentStatus.PROCESSED, # Or INDEXED
                chunk_count=processed_docs_count
            )
            task_log.info("Document status set to PROCESSED in PostgreSQL", chunk_count=processed_docs_count)

        except Exception as e:
            task_log.error("Error during Haystack document processing", error=str(e), exc_info=True)
            # Mark document as error in DB
            try:
                await postgres_client.update_document_status(
                    document_id,
                    DocumentStatus.ERROR,
                    error_message=f"Haystack Pipeline Error: {type(e).__name__}: {str(e)[:250]}"
                )
                task_log.info("Document status set to ERROR in PostgreSQL")
            except Exception as db_update_err:
                task_log.error("Failed to update document status to ERROR after processing failure", nested_error=str(db_update_err), exc_info=True)
            # Re-raise the exception for Celery retry mechanism
            raise e
        finally:
            # Close any clients initialized within the task
            await storage_client.close()
            # if ocr_client: await ocr_client.close()
            task_log.info("Cleaned up resources for task.")

    # --- Run the async part ---
    try:
        # Ensure DB connections are ready for the async task run
        # get_db_pool needs to handle re-connection if worker restarts
        # Milvus connection is handled internally by Haystack DocumentStore now
        asyncio.run(async_process())
        log.info("Haystack document processing task finished successfully", document_id=str(document_id))
    except Exception as e:
        # Exception already logged and status updated in async_process
        # Celery's autoretry mechanism will handle this if configured
        log.error("Async processing wrapper caught exception, letting Celery handle retry/failure", document_id=str(document_id), error=str(e))
        raise e