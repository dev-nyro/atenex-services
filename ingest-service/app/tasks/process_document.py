# ./app/tasks/process_document.py (CORREGIDO - Llamada a MinIO en executor)
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
from haystack.components.embedders import OpenAIDocumentEmbedder
from milvus_haystack import MilvusDocumentStore
from haystack.components.writers import DocumentWriter
from haystack.dataclasses import ByteStream

# --- Local Imports ---
from app.tasks.celery_app import celery_app
from app.core.config import settings
from app.db import postgres_client
from app.models.domain import DocumentStatus
from app.services.minio_client import MinioStorageClient # Importar cliente corregido

log = structlog.get_logger(__name__)

# --- Funciones de inicialización de Haystack (get_haystack_document_store, get_haystack_embedder, etc.) ---
# (Sin cambios necesarios aquí, se mantienen como estaban en la versión anterior corregida)
def get_haystack_document_store() -> MilvusDocumentStore:
    """Initializes the MilvusDocumentStore."""
    log.debug("Initializing MilvusDocumentStore",
             uri=str(settings.MILVUS_URI),
             collection=settings.MILVUS_COLLECTION_NAME,
             dim=settings.EMBEDDING_DIMENSION,
             metadata_fields=settings.MILVUS_METADATA_FIELDS)
    return MilvusDocumentStore(
        uri=str(settings.MILVUS_URI),
        collection_name=settings.MILVUS_COLLECTION_NAME,
        dim=settings.EMBEDDING_DIMENSION,
        embedding_field=settings.MILVUS_EMBEDDING_FIELD,
        content_field=settings.MILVUS_CONTENT_FIELD,
        metadata_fields=settings.MILVUS_METADATA_FIELDS,
        index_params=settings.MILVUS_INDEX_PARAMS,
        search_params=settings.MILVUS_SEARCH_PARAMS,
        consistency_level="Strong",
    )

def get_haystack_embedder() -> OpenAIDocumentEmbedder:
    """Initializes the OpenAI Embedder for documents."""
    api_key_env_var = "INGEST_OPENAI_API_KEY"
    if not os.getenv(api_key_env_var):
         log.warning(f"Environment variable {api_key_env_var} not found for OpenAI Embedder. Haystack might fail.")
    return OpenAIDocumentEmbedder(
        model=settings.OPENAI_EMBEDDING_MODEL,
        meta_fields_to_embed=[]
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
     if content_type == "application/pdf": return PyPDFToDocument
     elif content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document": return DOCXToDocument
     elif content_type == "text/plain": return TextFileToDocument
     elif content_type == "text/markdown": return MarkdownToDocument
     elif content_type == "text/html": return HTMLToDocument
     else:
         log.warning("No specific Haystack converter found for content type", content_type=content_type)
         return None


# --- Celery Task ---
@celery_app.task(
    bind=True,
    autoretry_for=(Exception,),
    retry_kwargs={'max_retries': 2, 'countdown': 60},
    acks_late=True,
    name="tasks.process_document_haystack"
)
def process_document_haystack_task(
    self, # Instancia de la tarea
    document_id_str: str,
    company_id_str: str,
    minio_object_name: str,
    file_name: str,
    content_type: str,
    original_metadata: Dict[str, Any],
):
    """
    Procesa un documento usando un pipeline Haystack (MinIO -> Haystack -> Milvus -> Supabase Status).
    """
    document_id = uuid.UUID(document_id_str)
    company_id = uuid.UUID(company_id_str)
    task_log = log.bind(document_id=str(document_id), company_id=str(company_id),
                      task_id=self.request.id, file_name=file_name, object_name=minio_object_name, content_type=content_type)
    task_log.info("Starting Haystack document processing task")

    # Usar una función async interna para poder usar await con postgres_client
    async def async_process():
        haystack_pipeline = Pipeline()
        processed_docs_count = 0
        # Crear instancia del cliente MinIO aquí dentro o pasarla si es seguro compartirla
        minio_client = MinioStorageClient()
        downloaded_file_stream: Optional[io.BytesIO] = None
        document_store: Optional[MilvusDocumentStore] = None

        try:
            # 0. Marcar como procesando en Supabase
            await postgres_client.update_document_status(document_id, DocumentStatus.PROCESSING)
            task_log.info("Document status set to PROCESSING")

            # 1. Descargar archivo de MinIO (usando executor)
            task_log.info("Downloading file from MinIO via executor...")
            loop = asyncio.get_running_loop()
            try:
                # *** CORREGIDO: Llamar a la función síncrona en el executor ***
                downloaded_file_stream = await loop.run_in_executor(
                    None, # Usa el default ThreadPoolExecutor
                    minio_client.download_file_stream_sync, # Llama a la función sync
                    minio_object_name
                )
            except FileNotFoundError as fnf_err:
                 task_log.error("File not found in MinIO storage.", object_name=minio_object_name, error=str(fnf_err))
                 # Marcar como error en DB y salir (no reintentar si el archivo no existe)
                 await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message="File not found in storage")
                 return # Salir de la función async_process, la tarea se marcará como exitosa pero con error en DB
            except Exception as download_err:
                 task_log.error("Failed to download file from MinIO.", error=str(download_err), exc_info=True)
                 raise download_err # Relanzar para que Celery reintente si es apropiado

            file_bytes = downloaded_file_stream.getvalue()
            if not file_bytes:
                raise ValueError("Downloaded file from MinIO is empty.")
            task_log.info(f"File downloaded successfully via executor ({len(file_bytes)} bytes)")

            # 2. Preparar Input Haystack (ByteStream) con metadatos FILTRADOS
            # (Sin cambios en esta lógica)
            allowed_meta_keys = set(settings.MILVUS_METADATA_FIELDS)
            doc_meta = {
                "company_id": str(company_id),
                "document_id": str(document_id),
                "file_name": file_name or "unknown",
                "file_type": content_type or "unknown",
            }
            filtered_original_meta_count = 0
            for key, value in original_metadata.items():
                if key in allowed_meta_keys and key not in doc_meta:
                    doc_meta[key] = str(value) if value is not None else None
                    filtered_original_meta_count += 1
                elif key not in doc_meta:
                    task_log.debug("Ignoring metadata field not in MILVUS_METADATA_FIELDS", field=key)

            task_log.debug("Filtered metadata for Haystack/Milvus", final_meta=doc_meta, original_allowed_added=filtered_original_meta_count)
            source_stream = ByteStream(data=file_bytes, meta=doc_meta)

            # 3. Seleccionar Conversor o Manejar OCR
            # (Sin cambios en esta lógica)
            ConverterClass = get_converter_for_content_type(content_type)
            if content_type in settings.EXTERNAL_OCR_REQUIRED_CONTENT_TYPES:
                raise NotImplementedError(f"OCR processing for {content_type} not implemented.")
            elif ConverterClass:
                 task_log.info(f"Using Haystack converter: {ConverterClass.__name__}")
                 document_store = get_haystack_document_store()
                 haystack_pipeline.add_component("converter", ConverterClass())
                 haystack_pipeline.add_component("splitter", get_haystack_splitter())
                 haystack_pipeline.add_component("embedder", get_haystack_embedder())
                 haystack_pipeline.add_component("writer", DocumentWriter(document_store=document_store))
                 haystack_pipeline.connect("converter.documents", "splitter.documents")
                 haystack_pipeline.connect("splitter.documents", "embedder.documents")
                 haystack_pipeline.connect("embedder.documents", "writer.documents")
                 pipeline_input = {"converter": {"sources": [source_stream]}}
            else:
                 task_log.error("Unsupported content type for Haystack processing", content_type=content_type)
                 raise ValueError(f"Unsupported content type for processing: {content_type}")

            # 4. Ejecutar el Pipeline Haystack (usando executor)
            if not haystack_pipeline.inputs():
                 raise RuntimeError("Pipeline construction failed.")
            task_log.info("Running Haystack indexing pipeline via executor...", pipeline_input_keys=list(pipeline_input.keys()))
            loop = asyncio.get_running_loop()
            pipeline_result = await loop.run_in_executor(
                None, lambda: haystack_pipeline.run(pipeline_input)
            )
            task_log.info("Haystack pipeline finished via executor.")

            # 5. Verificar resultado y obtener contador
            # (Sin cambios en esta lógica)
            writer_output = pipeline_result.get("writer", {})
            if isinstance(writer_output, dict) and "documents_written" in writer_output:
                 processed_docs_count = writer_output["documents_written"]
                 task_log.info(f"Documents written to Milvus (from writer): {processed_docs_count}")
            else:
                 splitter_output = pipeline_result.get("splitter", {})
                 if isinstance(splitter_output, dict) and "documents" in splitter_output:
                      processed_docs_count = len(splitter_output["documents"])
                      task_log.warning(f"Inferred processed chunk count from splitter: {processed_docs_count}", writer_output=writer_output)
                 else:
                      processed_docs_count = 0
                      task_log.warning("Processed chunk count could not be determined, setting to 0.", pipeline_output=pipeline_result)

            # 6. Actualizar Estado Final en Supabase
            final_status = DocumentStatus.PROCESSED
            await postgres_client.update_document_status(
                document_id, final_status, chunk_count=processed_docs_count, error_message=None
            )
            task_log.info("Document status set to PROCESSED/INDEXED in Supabase", chunk_count=processed_docs_count)

        except Exception as e:
            task_log.error("Error during Haystack document processing", error=str(e), exc_info=True)
            try:
                await postgres_client.update_document_status(
                    document_id, DocumentStatus.ERROR, error_message=f"Task Error: {type(e).__name__}: {str(e)[:500]}"
                )
                task_log.info("Document status set to ERROR in Supabase due to processing failure.")
            except Exception as db_update_err:
                task_log.error("Failed to update document status to ERROR after processing failure", nested_error=str(db_update_err), exc_info=True)
            raise e # Re-lanzar para que Celery maneje reintentos/fallo
        finally:
            if downloaded_file_stream:
                downloaded_file_stream.close()
            task_log.debug("Cleaned up resources for task.")

    # --- Ejecutar la lógica async dentro de la tarea síncrona de Celery ---
    try:
        asyncio.run(async_process())
        task_log.info("Haystack document processing task finished successfully.")
    except Exception as task_exception:
        task_log.exception("Haystack processing task failed at top level.")
        # Celery necesita ver la excepción para marcar como fallo/reintentar
        raise task_exception