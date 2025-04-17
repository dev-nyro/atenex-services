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

# *** CORRECCIÓN: Añadir import faltante ***
import asyncpg

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
# Importar excepciones de Milvus para manejo específico si es necesario
from pymilvus.exceptions import MilvusException
# Importar excepciones de Minio para manejo específico
from minio.error import S3Error
from haystack.components.writers import DocumentWriter
from haystack.dataclasses import ByteStream

# --- Local Imports ---
from app.tasks.celery_app import celery_app
from app.core.config import settings
from app.db import postgres_client # Cliente DB async
from app.models.domain import DocumentStatus
from app.services.minio_client import MinioStorageClient # Cliente MinIO async

log = structlog.get_logger(__name__)

# --- Funciones Helper Síncronas para Haystack ---
# Estas funciones *definen* cómo crear los componentes, pero no los crean todavía.
# La creación real se hará dentro de la tarea.

def _initialize_milvus_store() -> MilvusDocumentStore:
    """Función interna SÍNCRONA para inicializar MilvusDocumentStore."""
    init_log = log.bind(component="MilvusDocumentStore")
    init_log.info("Attempting to initialize...")
    try:
        store = MilvusDocumentStore(
            connection_args={"uri": str(settings.MILVUS_URI)},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            dim=settings.EMBEDDING_DIMENSION,
            embedding_field=settings.MILVUS_EMBEDDING_FIELD,
            content_field=settings.MILVUS_CONTENT_FIELD,
            metadata_fields=settings.MILVUS_METADATA_FIELDS,
            index_params=settings.MILVUS_INDEX_PARAMS,
            search_params=settings.MILVUS_SEARCH_PARAMS,
            consistency_level="Strong",
        )
        # Opcional: realizar una operación ligera para verificar conexión aquí si es necesario
        # store.count_documents() # Puede ser lento
        init_log.info("Initialization successful.")
        return store
    except MilvusException as me:
        init_log.error("Milvus connection/initialization failed", code=getattr(me, 'code', None), message=str(me), exc_info=True)
        raise ConnectionError(f"Milvus connection failed: {me}") from me
    except Exception as e:
        init_log.exception("Unexpected error during MilvusDocumentStore initialization")
        raise RuntimeError(f"Unexpected Milvus init error: {e}") from e

def _initialize_openai_embedder() -> OpenAIDocumentEmbedder:
    """Función interna SÍNCRONA para inicializar OpenAIDocumentEmbedder."""
    init_log = log.bind(component="OpenAIDocumentEmbedder")
    init_log.info("Initializing...")
    api_key_value = settings.OPENAI_API_KEY.get_secret_value()
    if not api_key_value:
        init_log.error("OpenAI API Key is missing!")
        raise ValueError("OpenAI API Key is required.")
    embedder = OpenAIDocumentEmbedder(
        api_key=Secret.from_token(api_key_value),
        model=settings.OPENAI_EMBEDDING_MODEL,
        meta_fields_to_embed=[]
    )
    init_log.info("Initialization successful.", model=settings.OPENAI_EMBEDDING_MODEL)
    return embedder

def _initialize_splitter() -> DocumentSplitter:
    """Función interna SÍNCRONA para inicializar DocumentSplitter."""
    init_log = log.bind(component="DocumentSplitter")
    init_log.info("Initializing...")
    splitter = DocumentSplitter(
        split_by=settings.SPLITTER_SPLIT_BY,
        split_length=settings.SPLITTER_CHUNK_SIZE,
        split_overlap=settings.SPLITTER_CHUNK_OVERLAP
    )
    init_log.info("Initialization successful.", split_by=settings.SPLITTER_SPLIT_BY, length=settings.SPLITTER_CHUNK_SIZE)
    return splitter

def _initialize_document_writer(store: MilvusDocumentStore) -> DocumentWriter:
    """Función interna SÍNCRONA para inicializar DocumentWriter."""
    init_log = log.bind(component="DocumentWriter")
    init_log.info("Initializing...")
    writer = DocumentWriter(document_store=store)
    init_log.info("Initialization successful.")
    return writer

def get_converter_for_content_type(content_type: str) -> Optional[Type]:
    """Devuelve la clase del conversor Haystack apropiada para el tipo de archivo."""
    converters = {
        "application/pdf": PyPDFToDocument,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DOCXToDocument,
        "application/msword": DOCXToDocument,  # Word antiguo
        "application/vnd.ms-excel": None,  # Excel antiguo (no soportado nativo, requiere integración extra)
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": None,  # Excel moderno (ver nota abajo)
        "image/png": None,  # Imágenes requieren OCR externo
        "image/jpeg": None,
        "image/jpg": None,
        "text/plain": TextFileToDocument,
        "text/markdown": MarkdownToDocument,
        "text/html": HTMLToDocument,
    }
    converter = converters.get(content_type)
    if converter is None:
        # Mensaje de error claro y en español para el frontend
        raise ValueError("El tipo de archivo no es soportado actualmente. Solo se permiten PDF, Word, Excel y algunas imágenes. Si necesitas soporte para este tipo de archivo, contacta al administrador.")
    return converter
# NOTA: Para Excel e imágenes, devolveremos error claro y en español si se intenta subir uno, hasta que se integre OCR o parser de Excel.

# --- Celery Task Definition ---
NON_RETRYABLE_ERRORS = (FileNotFoundError, ValueError, TypeError, NotImplementedError, KeyError, AttributeError)
# *** CORREGIDO: asyncpg ahora está definido porque se importó arriba ***
RETRYABLE_ERRORS = (IOError, ConnectionError, TimeoutError, asyncpg.PostgresConnectionError, S3Error, MilvusException, Exception)

@celery_app.task(
    bind=True,
    autoretry_for=RETRYABLE_ERRORS,
    retry_backoff=True,
    retry_backoff_max=300, # 5 minutos máximo backoff
    retry_jitter=True,
    retry_kwargs={'max_retries': 3}, # Reintentar 3 veces
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
    """Tarea Celery para procesar un documento usando Haystack."""
    document_id = uuid.UUID(document_id_str)
    company_id = uuid.UUID(company_id_str)
    # Configurar logger con contexto de la tarea
    task_log = log.bind(
        document_id=str(document_id),
        company_id=str(company_id),
        task_id=self.request.id or "unknown",
        attempt=self.request.retries + 1,
        filename=file_name,
        content_type=content_type
    )
    task_log.info("Starting Haystack document processing task execution")

    # --- Función async interna para orquestar el flujo ---
    async def async_process_flow():
        minio_client = None
        downloaded_file_stream: Optional[io.BytesIO] = None
        pipeline: Optional[Pipeline] = None # Inicializar pipeline como None

        try:
            # 0. Marcar como PROCESSING en DB
            task_log.info("Updating document status to PROCESSING")
            # Asegurarse que la conexión DB esté disponible (puede requerir get_db_pool si no está globalmente disponible aquí)
            # Si postgres_client maneja el pool internamente, esto está bien.
            await postgres_client.update_document_status(document_id, DocumentStatus.PROCESSING)

            # 1. Descargar archivo de MinIO
            task_log.info("Attempting to download file from MinIO")
            minio_client = MinioStorageClient() # Asume que maneja errores de conexión internamente
            downloaded_file_stream = await minio_client.download_file_stream(minio_object_name)
            file_bytes = downloaded_file_stream.getvalue()
            if not file_bytes:
                raise ValueError("Downloaded file is empty.")
            task_log.info(f"File downloaded successfully ({len(file_bytes)} bytes)")

            # 2. Inicializar componentes Haystack y construir pipeline (Síncrono -> Executor)
            task_log.info("Initializing Haystack components and building pipeline via executor...")
            loop = asyncio.get_running_loop()
            # Ejecutar inicialización síncrona en executor
            store = await loop.run_in_executor(None, _initialize_milvus_store)
            embedder = await loop.run_in_executor(None, _initialize_openai_embedder)
            splitter = await loop.run_in_executor(None, _initialize_splitter)
            writer = await loop.run_in_executor(None, _initialize_document_writer, store) # Pasar store inicializado

            ConverterClass = get_converter_for_content_type(content_type)
            if not ConverterClass:
                raise ValueError(f"Unsupported content type: {content_type}")
            converter_instance = ConverterClass()

            # Construir el pipeline (esto es rápido, no necesita executor)
            pipeline = Pipeline()
            pipeline.add_component("converter", converter_instance)
            pipeline.add_component("splitter", splitter)
            pipeline.add_component("embedder", embedder)
            pipeline.add_component("writer", writer)
            pipeline.connect("converter.documents", "splitter.documents")
            pipeline.connect("splitter.documents", "embedder.documents")
            pipeline.connect("embedder.documents", "writer.documents")
            task_log.info("Haystack components initialized and pipeline built.")

            # 3. Preparar Metadatos y ByteStream
            allowed_meta_keys = set(settings.MILVUS_METADATA_FIELDS)
            # Asegurar que los metadatos clave siempre estén presentes
            doc_meta = {
                "company_id": str(company_id),
                "document_id": str(document_id),
                "file_name": file_name or "unknown",
                "file_type": content_type or "unknown"
            }
            # Añadir metadatos originales si están permitidos y no colisionan
            added_original_meta = 0
            for key, value in original_metadata.items():
                # Solo añadir si está en la lista permitida Y no es uno de los campos clave ya definidos
                if key in allowed_meta_keys and key not in ["company_id", "document_id", "file_name", "file_type"]:
                    doc_meta[key] = str(value) if value is not None else None # Convertir a string por si acaso
                    added_original_meta += 1
            task_log.debug("Prepared metadata for Haystack", final_meta=doc_meta, added_original_count=added_original_meta)

            source_stream = ByteStream(data=file_bytes, meta=doc_meta)
            pipeline_input = {"converter": {"sources": [source_stream]}}

            # 4. Ejecutar Pipeline Haystack (Síncrono -> Executor)
            task_log.info("Running Haystack pipeline via executor...")
            start_time = time.monotonic()
            # Usar el pipeline construido previamente
            pipeline_result = await loop.run_in_executor(None, pipeline.run, pipeline_input)
            duration = time.monotonic() - start_time
            task_log.info(f"Haystack pipeline execution finished via executor", duration_sec=round(duration, 2))

            # 5. Procesar Resultado y Contar Chunks
            processed_chunk_count = 0
            writer_output = pipeline_result.get("writer", {})
            if isinstance(writer_output, dict) and "documents_written" in writer_output:
                processed_chunk_count = writer_output["documents_written"]
                task_log.info(f"Chunks written to Milvus: {processed_chunk_count}")
            else:
                # Fallback si 'documents_written' no está (podría indicar error o versión distinta)
                task_log.warning("Could not determine count from writer output, attempting fallback", output=writer_output)
                splitter_output = pipeline_result.get("splitter", {})
                if isinstance(splitter_output, dict) and "documents" in splitter_output and isinstance(splitter_output["documents"], list):
                     processed_chunk_count = len(splitter_output["documents"])
                     task_log.warning(f"Inferred chunk count from splitter output: {processed_chunk_count}")
                else:
                    task_log.error("Pipeline failed or did not produce expected output structure. No documents processed/written.", pipeline_output=pipeline_result)
                    raise RuntimeError("Pipeline execution failed or yielded unexpected results.")

            if processed_chunk_count == 0:
                 task_log.warning("Pipeline ran but resulted in 0 chunks being written.")
                 # Considerar si 0 chunks es un error o un caso válido (documento vacío post-conversión?)
                 # Por ahora, lo marcamos como procesado pero con 0 chunks.

            # 6. Actualizar Estado Final en DB
            final_status = DocumentStatus.PROCESSED # O INDEXED si quieres ese estado
            task_log.info(f"Updating document status to {final_status.value} with {processed_chunk_count} chunks.")
            await postgres_client.update_document_status(
                document_id=document_id,
                status=final_status,
                chunk_count=processed_chunk_count,
                error_message=None # Limpiar cualquier error previo
            )
            task_log.info("Document status updated successfully in PostgreSQL.")

        except NON_RETRYABLE_ERRORS as e_non_retry:
            err_msg = f"Non-retryable error: {type(e_non_retry).__name__}: {str(e_non_retry)[:500]}"
            formatted_traceback = traceback.format_exc()
            task_log.error(f"Processing failed permanently: {err_msg}", traceback=formatted_traceback)
            try:
                await postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=err_msg)
            except Exception as db_err:
                task_log.critical("Failed update status to ERROR after non-retryable failure!", db_error=str(db_err))
            # No relanzar para que Celery no reintente

        except RETRYABLE_ERRORS as e_retry:
            # Obtener el número máximo de reintentos de forma segura
            max_retries = getattr(self, 'max_retries', 3)
            err_msg = f"Error reintentable (intento {self.request.retries + 1} de {max_retries}): {type(e_retry).__name__}: {str(e_retry)[:500]}"
            formatted_traceback = traceback.format_exc()
            # Mensaje para el frontend en español
            frontend_msg = "Ocurrió un error temporal durante el procesamiento. El sistema intentará nuevamente."
            task_log.warning(f"Processing failed, will retry: {err_msg}", traceback=formatted_traceback)
            try:
                # Actualizar estado a ERROR pero indicando que es parte de un reintento
                await postgres_client.update_document_status(
                    document_id, DocumentStatus.ERROR,
                    error_message=frontend_msg
                )
            except Exception as db_err:
                task_log.error("Failed update status to ERROR during retryable failure!", db_error=str(db_err))
            # Relanzar la excepción para que Celery maneje el reintento
            raise e_retry

        finally:
            # Limpieza de recursos
            if downloaded_file_stream:
                downloaded_file_stream.close()
            # Si se crearon archivos temporales, limpiarlos aquí
            task_log.debug("Cleaned up task resources.")

    # --- Ejecutar el flujo async ---
    try:
        # Timeout global para el procesamiento (5 minutos)
        TIMEOUT_SECONDS = 300
        try:
            asyncio.run(asyncio.wait_for(async_process_flow(), timeout=TIMEOUT_SECONDS))
            task_log.info("Haystack document processing task completed successfully.")
            return {"status": "success", "document_id": str(document_id)}
        except asyncio.TimeoutError:
            timeout_msg = f"Processing exceeded timeout of {TIMEOUT_SECONDS} seconds. Marking as ERROR."
            task_log.error(timeout_msg)
            # Intentar actualizar el estado en la base de datos
            try:
                asyncio.run(postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=timeout_msg))
            except Exception as db_err:
                task_log.critical("Failed to update status to ERROR after timeout!", db_error=str(db_err))
            return {"status": "failure", "document_id": str(document_id), "error": timeout_msg}
    except Exception as top_level_exc:
        # Mensaje para el frontend en español
        frontend_msg = "Ocurrió un error inesperado durante el procesamiento del documento. Por favor, inténtalo de nuevo o contacta soporte."
        task_log.exception("Haystack processing task failed at top level (after potential retries). This indicates a final failure.", exc_info=top_level_exc)
        try:
            asyncio.run(postgres_client.update_document_status(document_id, DocumentStatus.ERROR, error_message=frontend_msg))
        except Exception as db_err:
            task_log.critical("Failed to update status to ERROR after top-level failure!", db_error=str(db_err))
        return {"status": "failure", "document_id": str(document_id), "error": frontend_msg}