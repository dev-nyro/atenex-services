# ingest-service/app/tasks/process_document.py
import uuid
import asyncio
from typing import Dict, Any, Optional, List, Type
import tempfile
import os
from pathlib import Path
import structlog
import io
import time
import traceback # Para formatear excepciones

# --- Haystack Imports ---
from haystack import Pipeline, Document
from haystack.utils import Secret
from haystack.components.converters import (
    PyPDFToDocument, TextFileToDocument, MarkdownToDocument,
    HTMLToDocument, DOCXToDocument,
)
from haystack.components.preprocessors import DocumentSplitter
from haystack.components.embedders import OpenAIDocumentEmbedder
from milvus_haystack import MilvusDocumentStore # Importación correcta
from haystack.components.writers import DocumentWriter
from haystack.dataclasses import ByteStream

# --- Local Imports ---
from app.tasks.celery_app import celery_app
from app.core.config import settings
from app.db import postgres_client # Cliente DB async
from app.models.domain import DocumentStatus
from app.services.minio_client import MinioStorageClient # Cliente MinIO async

log = structlog.get_logger(__name__)

# --- Funciones Helper Síncronas para Haystack (se ejecutarán en executor o sync) ---
def initialize_haystack_components() -> Dict[str, Any]:
    """Inicializa y devuelve un diccionario con los componentes Haystack necesarios."""
    task_log = log.bind(component_init="haystack")
    task_log.info("Initializing Haystack components...")
    try:
        document_store = MilvusDocumentStore(
            uri=str(settings.MILVUS_URI), # Asegurar que URI es string
            collection_name=settings.MILVUS_COLLECTION_NAME,
            dim=settings.EMBEDDING_DIMENSION,
            embedding_field=settings.MILVUS_EMBEDDING_FIELD,
            content_field=settings.MILVUS_CONTENT_FIELD,
            metadata_fields=settings.MILVUS_METADATA_FIELDS,
            index_params=settings.MILVUS_INDEX_PARAMS,
            search_params=settings.MILVUS_SEARCH_PARAMS,
            consistency_level="Strong", # O Bounded, Session, Eventually
        )
        task_log.debug("MilvusDocumentStore initialized")

        # Usar el valor del SecretStr correctamente
        api_key_value = settings.OPENAI_API_KEY.get_secret_value()
        if not api_key_value:
            task_log.error("OpenAI API Key is missing!")
            raise ValueError("OpenAI API Key is required but missing in configuration.")

        embedder = OpenAIDocumentEmbedder(
            api_key=Secret.from_token(api_key_value), # Usar from_token ya que tenemos el valor
            model=settings.OPENAI_EMBEDDING_MODEL,
            meta_fields_to_embed=[] # Ajustar si es necesario
        )
        task_log.debug("OpenAIDocumentEmbedder initialized", model=settings.OPENAI_EMBEDDING_MODEL)

        splitter = DocumentSplitter(
            split_by=settings.SPLITTER_SPLIT_BY,
            split_length=settings.SPLITTER_CHUNK_SIZE,
            split_overlap=settings.SPLITTER_CHUNK_OVERLAP
        )
        task_log.debug("DocumentSplitter initialized", split_by=settings.SPLITTER_SPLIT_BY, len=settings.SPLITTER_CHUNK_SIZE, overlap=settings.SPLITTER_CHUNK_OVERLAP)

        writer = DocumentWriter(document_store=document_store)
        task_log.debug("DocumentWriter initialized")

        task_log.info("Haystack components initialized successfully.")
        return {
            "document_store": document_store,
            "embedder": embedder,
            "splitter": splitter,
            "writer": writer,
        }
    except Exception as e:
        task_log.exception("Failed to initialize Haystack components", error=str(e))
        raise RuntimeError(f"Haystack component initialization failed: {e}") from e

def get_converter_for_content_type(content_type: str) -> Optional[Type]:
     """Devuelve la clase del conversor Haystack apropiada."""
     # Mapeo simple
     converters = {
         "application/pdf": PyPDFToDocument,
         "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DOCXToDocument,
         "text/plain": TextFileToDocument,
         "text/markdown": MarkdownToDocument,
         "text/html": HTMLToDocument,
         # Añadir más tipos si se soportan
     }
     converter = converters.get(content_type)
     if converter:
         log.debug("Selected Haystack converter", converter=converter.__name__, content_type=content_type)
     else:
         log.warning("No specific Haystack converter found for content type", content_type=content_type)
     return converter

def build_haystack_pipeline(converter_instance, splitter, embedder, writer) -> Pipeline:
    """Construye el pipeline Haystack dinámicamente."""
    task_log = log.bind(pipeline_build="haystack")
    task_log.info("Building Haystack processing pipeline...")
    pipeline = Pipeline()
    try:
        pipeline.add_component("converter", converter_instance)
        pipeline.add_component("splitter", splitter)
        pipeline.add_component("embedder", embedder)
        pipeline.add_component("writer", writer)

        pipeline.connect("converter.documents", "splitter.documents")
        pipeline.connect("splitter.documents", "embedder.documents")
        pipeline.connect("embedder.documents", "writer.documents")
        task_log.info("Haystack pipeline built successfully.")
        return pipeline
    except Exception as e:
        task_log.exception("Failed to build Haystack pipeline", error=str(e))
        raise RuntimeError(f"Haystack pipeline construction failed: {e}") from e

# --- Celery Task Definition ---
# Errores NO reintentables: FileNotFoundError, ValueError, TypeError, NotImplementedError, KeyError
NON_RETRYABLE_ERRORS = (FileNotFoundError, ValueError, TypeError, NotImplementedError, KeyError, AttributeError)
# Errores SÍ reintentables: IOError, ConnectionError, TimeoutError, y genérico Exception (con precaución)
RETRYABLE_ERRORS = (IOError, ConnectionError, TimeoutError, asyncpg.PostgresConnectionError, S3Error, Exception)

@celery_app.task(
    bind=True,
    autoretry_for=RETRYABLE_ERRORS, # Reintentar solo en errores recuperables
    retry_backoff=True, # Backoff exponencial
    retry_backoff_max=300, # Máximo 5 minutos de espera
    retry_jitter=True, # Añadir aleatoriedad a la espera
    retry_kwargs={'max_retries': 3, 'countdown': 60}, # Máximo 3 reintentos, empezando con 60s
    reject_on_worker_lost=True,
    acks_late=True,
    name="tasks.process_document_haystack"
)
def process_document_haystack_task(
    self, # Instancia de la tarea Celery
    document_id_str: str,
    company_id_str: str,
    minio_object_name: str,
    file_name: str,
    content_type: str,
    original_metadata: Dict[str, Any],
):
    """
    Tarea Celery para procesar un documento: descarga de MinIO, procesa con Haystack, indexa en Milvus.
    Utiliza asyncio.run para la lógica async y run_in_executor para Haystack.
    """
    document_id = uuid.UUID(document_id_str)
    company_id = uuid.UUID(company_id_str)
    # Logger contextual para la tarea
    task_log = log.bind(
        document_id=str(document_id),
        company_id=str(company_id),
        task_id=self.request.id or "unknown",
        file_name=file_name,
        object_name=minio_object_name,
        content_type=content_type,
        attempt=self.request.retries + 1 # Número de intento actual
    )
    task_log.info("Starting Haystack document processing task execution")

    # --- Función async interna para orquestar el flujo ---
    async def async_process_flow():
        minio_client = None # Inicializar fuera del try
        downloaded_file_stream: Optional[io.BytesIO] = None
        haystack_components = {}
        pipeline_run_successful = False
        processed_chunk_count = 0

        try:
            # 0. Marcar como PROCESSING en DB (async)
            task_log.info("Updating document status to PROCESSING")
            await postgres_client.update_document_status(document_id, DocumentStatus.PROCESSING)

            # 1. Descargar archivo de MinIO (async, usa executor internamente)
            task_log.info("Attempting to download file from MinIO")
            minio_client = MinioStorageClient() # Instanciar aquí
            downloaded_file_stream = await minio_client.download_file_stream(minio_object_name)
            file_bytes = downloaded_file_stream.getvalue()
            if not file_bytes:
                task_log.error("Downloaded file is empty.")
                # Este es un error de datos, no reintentable
                raise ValueError("Downloaded file is empty.")
            task_log.info(f"File downloaded successfully ({len(file_bytes)} bytes)")

            # 2. Inicializar Componentes Haystack (Síncrono, podría ser largo)
            # Ejecutar en executor para no bloquear el worker por mucho tiempo si es lento
            task_log.info("Initializing Haystack components via executor...")
            loop = asyncio.get_running_loop()
            haystack_components = await loop.run_in_executor(None, initialize_haystack_components)
            task_log.info("Haystack components ready.")

            # 3. Preparar Metadatos y ByteStream para Haystack
            # Filtrar metadatos para incluir solo los definidos en config + los esenciales
            allowed_meta_keys = set(settings.MILVUS_METADATA_FIELDS)
            doc_meta = {
                "company_id": str(company_id), # Asegurar string
                "document_id": str(document_id), # Asegurar string
                "file_name": file_name or "unknown",
                "file_type": content_type or "unknown",
            }
            added_original_meta = 0
            for key, value in original_metadata.items():
                if key in allowed_meta_keys and key not in doc_meta:
                    doc_meta[key] = str(value) if value is not None else None
                    added_original_meta += 1
            task_log.debug("Prepared metadata for Haystack", final_meta=doc_meta, added_original_count=added_original_meta)
            source_stream = ByteStream(data=file_bytes, meta=doc_meta)

            # 4. Seleccionar Conversor y Construir Pipeline (Síncrono)
            ConverterClass = get_converter_for_content_type(content_type)
            if not ConverterClass:
                 task_log.error("Unsupported content type for Haystack converters", content_type=content_type)
                 raise ValueError(f"Unsupported content type: {content_type}")

            converter_instance = ConverterClass()
            pipeline = build_haystack_pipeline(
                converter_instance,
                haystack_components["splitter"],
                haystack_components["embedder"],
                haystack_components["writer"]
            )
            pipeline_input = {"converter": {"sources": [source_stream]}}

            # 5. Ejecutar Pipeline Haystack (Síncrono y potencialmente largo -> Executor)
            task_log.info("Running Haystack pipeline via executor...")
            start_time = time.monotonic()
            pipeline_result = await loop.run_in_executor(None, pipeline.run, pipeline_input)
            duration = time.monotonic() - start_time
            task_log.info(f"Haystack pipeline execution finished via executor", duration_sec=round(duration, 2))

            # 6. Procesar Resultado y Contar Chunks
            writer_output = pipeline_result.get("writer", {})
            if isinstance(writer_output, dict) and "documents_written" in writer_output:
                processed_chunk_count = writer_output["documents_written"]
                task_log.info(f"Chunks written to Milvus: {processed_chunk_count}")
                pipeline_run_successful = True # Asumir éxito si el writer reporta algo
            else:
                # Intentar inferir de otro componente si es posible, o marcar como 0/error
                task_log.warning("Could not determine documents written from writer output", output=writer_output)
                # Podrías intentar contar desde splitter output como fallback
                splitter_output = pipeline_result.get("splitter", {})
                if isinstance(splitter_output, dict) and "documents" in splitter_output:
                     processed_chunk_count = len(splitter_output["documents"])
                     task_log.warning(f"Inferred chunk count from splitter: {processed_chunk_count}")
                     pipeline_run_successful = True # Considerar éxito si hubo chunks
                else:
                     processed_chunk_count = 0
                     pipeline_run_successful = False # Marcar como fallo si no se pudo procesar/escribir nada
                     task_log.error("Pipeline execution seems to have failed, no documents processed/written.")
                     # Levantar una excepción para marcar como error en DB
                     raise RuntimeError("Haystack pipeline failed to process or write any documents.")

            # 7. Actualizar Estado Final en DB (async)
            final_status = DocumentStatus.PROCESSED # O INDEXED
            task_log.info(f"Updating document status to {final_status.value} with {processed_chunk_count} chunks.")
            await postgres_client.update_document_status(
                document_id,
                final_status,
                chunk_count=processed_chunk_count,
                error_message=None # Limpiar cualquier error previo
            )
            task_log.info("Document status updated successfully in PostgreSQL.")

        except NON_RETRYABLE_ERRORS as e_non_retry:
            # Errores de datos, lógica, archivo no encontrado, tipo no soportado, etc.
            err_msg = f"Non-retryable task error: {type(e_non_retry).__name__}: {str(e_non_retry)[:500]}"
            task_log.error(f"Processing failed permanently: {err_msg}", exc_info=True)
            try:
                await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=err_msg)
                task_log.info("Document status set to ERROR due to non-retryable failure.")
            except Exception as db_err:
                task_log.critical("Failed to update document status to ERROR after non-retryable failure!", db_error=str(db_err), original_error=err_msg, exc_info=True)
            # No relanzar e_non_retry para que Celery no reintente
            # La tarea se marcará como SUCCESS en Celery, pero la DB indicará ERROR.
            # Si quieres que Celery marque FAILED, elimina el try/except y deja que la excepción se propague,
            # asegurándote que NON_RETRYABLE_ERRORS no estén en autoretry_for.

        except RETRYABLE_ERRORS as e_retry:
            # Errores de red, IO, timeout, DB temporal, S3 temporal, etc.
            err_msg = f"Retryable task error: {type(e_retry).__name__}: {str(e_retry)[:500]}"
            task_log.warning(f"Processing failed, will retry if possible: {err_msg}", exc_info=True)
            try:
                # Marcar error temporalmente en DB (puede ser sobrescrito en reintento exitoso)
                await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=f"Task Error (Retry Attempt {self.request.retries + 1}): {err_msg}")
            except Exception as db_err:
                 task_log.error("Failed to update document status to ERROR during retryable failure!", db_error=str(db_err), original_error=err_msg, exc_info=True)
            # Relanzar la excepción original para que Celery la capture y aplique la lógica de reintento
            raise e_retry

        finally:
            # Limpieza final
            if downloaded_file_stream:
                downloaded_file_stream.close()
            task_log.debug("Cleaned up task resources.")

    # --- Ejecutar el flujo async dentro de la tarea Celery síncrona ---
    try:
        asyncio.run(async_process_flow())
        task_log.info("Haystack document processing task completed.")
    except Exception as top_level_exc:
        # Esta excepción sólo debería ocurrir si async_process_flow relanza una excepción
        # (normalmente una RETRYABLE_ERROR para que Celery la maneje).
        # Las NON_RETRYABLE_ERRORS se capturan dentro de async_process_flow y no se relanzan.
        task_log.exception("Haystack processing task failed at top level (likely pending retry or unexpected issue).")
        # No necesitamos hacer nada más aquí, Celery manejará el reintento o marcará como FAILED
        # si la excepción relanzada coincide con autoretry_for o si se agotan los reintentos.
        pass