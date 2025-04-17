# query-service/app/pipelines/rag_pipeline.py
import structlog
import asyncio
import uuid
import functools # LLM_COMMENT: Import functools to use partial for run_in_executor keyword arguments.
from typing import Dict, Any, List, Tuple, Optional

from pymilvus.exceptions import MilvusException, ErrorCode
from fastapi import HTTPException, status

from haystack import Pipeline, Document
from haystack.components.embedders import OpenAITextEmbedder
from haystack.components.builders.prompt_builder import PromptBuilder
from milvus_haystack import MilvusDocumentStore, MilvusEmbeddingRetriever
from haystack.utils import Secret

from app.core.config import settings
from app.db import postgres_client
from app.services.gemini_client import gemini_client
from app.api.v1.schemas import RetrievedDocument

log = structlog.get_logger(__name__)

# --- Component Initialization Functions (sin cambios) ---
def get_milvus_document_store() -> MilvusDocumentStore:
    connection_uri = str(settings.MILVUS_URI)
    connection_timeout = 30.0
    log.debug("Initializing MilvusDocumentStore...", uri=connection_uri, collection=settings.MILVUS_COLLECTION_NAME)
    try:
        store = MilvusDocumentStore(
            connection_args={"uri": connection_uri, "timeout": connection_timeout},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            search_params=settings.MILVUS_SEARCH_PARAMS,
            consistency_level="Strong",
        )
        log.info("MilvusDocumentStore parameters configured.", uri=connection_uri, collection=settings.MILVUS_COLLECTION_NAME)
        return store
    except MilvusException as e:
        log.error("Failed to initialize MilvusDocumentStore", error_code=e.code, error_message=e.message, exc_info=True)
        raise RuntimeError(f"Milvus connection/initialization failed: {e.message}") from e
    except TypeError as te:
        log.error("TypeError during MilvusDocumentStore initialization (likely invalid argument)", error=str(te), exc_info=True)
        raise RuntimeError(f"Milvus initialization error (Invalid argument): {te}") from te
    except Exception as e:
        log.error("Unexpected error during MilvusDocumentStore initialization", error=str(e), exc_info=True)
        raise RuntimeError(f"Unexpected error initializing Milvus: {e}") from e

def get_openai_text_embedder() -> OpenAITextEmbedder:
    log.debug("Initializing OpenAITextEmbedder", model=settings.OPENAI_EMBEDDING_MODEL)
    api_key_value = settings.OPENAI_API_KEY.get_secret_value()
    if not api_key_value: log.warning("QUERY_OPENAI_API_KEY is missing or empty!")
    return OpenAITextEmbedder(
        api_key=Secret.from_token(api_key_value or "dummy-key"),
        model=settings.OPENAI_EMBEDDING_MODEL
    )

def get_milvus_retriever(document_store: MilvusDocumentStore) -> MilvusEmbeddingRetriever:
    log.debug("Initializing MilvusEmbeddingRetriever")
    return MilvusEmbeddingRetriever(document_store=document_store, top_k=settings.RETRIEVER_TOP_K)

def get_prompt_builder() -> PromptBuilder:
    log.debug("Initializing PromptBuilder")
    return PromptBuilder(template=settings.RAG_PROMPT_TEMPLATE)


# --- Pipeline Construction (sin cambios) ---
_rag_pipeline_instance: Optional[Pipeline] = None
def build_rag_pipeline() -> Pipeline:
    global _rag_pipeline_instance
    if _rag_pipeline_instance: return _rag_pipeline_instance
    log.info("Building Haystack RAG pipeline...")
    rag_pipeline = Pipeline()
    try:
        doc_store = get_milvus_document_store()
        text_embedder = get_openai_text_embedder()
        retriever = get_milvus_retriever(document_store=doc_store)
        prompt_builder = get_prompt_builder()

        rag_pipeline.add_component("text_embedder", text_embedder)
        rag_pipeline.add_component("retriever", retriever)
        rag_pipeline.add_component("prompt_builder", prompt_builder)

        rag_pipeline.connect("text_embedder.embedding", "retriever.query_embedding")
        rag_pipeline.connect("retriever.documents", "prompt_builder.documents")

        log.info("Haystack RAG pipeline built successfully.")
        _rag_pipeline_instance = rag_pipeline
        return rag_pipeline
    except Exception as e:
        log.error("Failed to build Haystack RAG pipeline", error=str(e), exc_info=True)
        raise RuntimeError("Could not build the RAG pipeline") from e

# --- Pipeline Execution ---
# *** FUNCIÓN CORREGIDA: run_rag_pipeline ***
async def run_rag_pipeline(
    query: str,
    company_id: str,
    user_id: Optional[str],
    top_k: Optional[int] = None,
    chat_id: Optional[uuid.UUID] = None
) -> Tuple[str, List[Document], Optional[uuid.UUID]]:
    """
    Ejecuta el pipeline RAG usando `run_in_executor` con `functools.partial`
    para pasar argumentos de palabra clave `data` y `params` a `pipeline.run`.
    Luego llama a Gemini y loguea la interacción.
    """
    # LLM_COMMENT: This function orchestrates the RAG pipeline execution.
    # LLM_COMMENT: It takes user query, company/user context, and optional chat info.
    # LLM_COMMENT: Key step: Calls Haystack's pipeline.run asynchronously using run_in_executor.
    run_log = log.bind(query=query, company_id=company_id, user_id=user_id or "N/A", chat_id=str(chat_id) if chat_id else "N/A")
    run_log.info("Running RAG pipeline execution flow...")

    try:
        pipeline = build_rag_pipeline() # Obtener/construir el pipeline
        # LLM_COMMENT: Ensure pipeline instance is available before proceeding.
    except Exception as build_err:
         run_log.error("Failed to get or build RAG pipeline for execution", error=str(build_err))
         raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="RAG pipeline is not available.")

    # Determinar parámetros para el retriever
    retriever_top_k = top_k if top_k is not None else settings.RETRIEVER_TOP_K
    retriever_filters = {"field": settings.MILVUS_COMPANY_ID_FIELD, "operator": "==", "value": company_id}
    # LLM_COMMENT: Filters are prepared for the Milvus retriever component based on company_id.

    run_log.debug("Pipeline execution parameters set", filters=retriever_filters, top_k=retriever_top_k)

    # Preparar data y params para pipeline.run
    pipeline_data = {
        "text_embedder": {"text": query},
        "retriever": {"filters": [retriever_filters], "top_k": retriever_top_k},
        "prompt_builder": {"query": query}
    }
    pipeline_params = {"retriever": {"filters": [retriever_filters], "top_k": retriever_top_k}}

    # Ejecutar pipeline en ThreadPool, pasando data y params según Haystack 2.x
    loop = asyncio.get_running_loop()
    pipeline_run_partial = functools.partial(
        pipeline.run,
        data=pipeline_data,
        params=pipeline_params
    )
    pipeline_result = await loop.run_in_executor(None, pipeline_run_partial)

    # Ejecutamos el pipeline, LLM y logging dentro de un solo bloque para capturar excepciones:
    try:
        run_log.info("Haystack pipeline (embed, retrieve, prompt) executed successfully.")

        # Extraer resultados
        retrieved_docs: List[Document] = pipeline_result.get("retriever", {}).get("documents", [])
        prompt_builder_output = pipeline_result.get("prompt_builder", {})
        generated_prompt = prompt_builder_output.get("prompt") if isinstance(prompt_builder_output, dict) else None

        if not retrieved_docs:
            run_log.warning("No relevant documents found by retriever for the query.")
        else:
            run_log.info(f"Retriever found {len(retrieved_docs)} documents.")

        if not generated_prompt:
            run_log.error("Failed to extract prompt from prompt_builder output", component_output=prompt_builder_output)
            generated_prompt = f"Pregunta: {query}\n\n(No se pudo construir el prompt con documentos recuperados). Por favor responde a la pregunta."

        run_log.debug("Generated prompt for LLM", prompt_length=len(generated_prompt))

        # Llamada a Gemini
        answer = await gemini_client.generate_answer(generated_prompt)
        run_log.info("Answer generated by Gemini", answer_length=len(answer))

        # Log en BD
        formatted_docs_for_log = [
            RetrievedDocument.from_haystack_doc(doc).model_dump(exclude_none=True)
            for doc in retrieved_docs
        ]
        user_uuid = uuid.UUID(user_id) if user_id and isinstance(user_id, str) else None
        company_uuid = uuid.UUID(company_id) if isinstance(company_id, str) else company_id
        log_id = await postgres_client.log_query_interaction(
            company_id=company_uuid, user_id=user_uuid, query=query, answer=answer,
            retrieved_documents_data=formatted_docs_for_log, chat_id=chat_id,
            metadata={"retriever_top_k": retriever_top_k, "llm_model": settings.GEMINI_MODEL_NAME}
        )
        run_log.info("Query interaction logged to database", db_log_id=str(log_id))

        return answer, retrieved_docs, log_id
    except HTTPException:
        raise
    except MilvusException as milvus_err:
        run_log.error("Milvus error during pipeline execution", error_code=milvus_err.code, error_message=milvus_err.message, exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Vector database error: {milvus_err.message}")
    except ValueError as val_err:
        run_log.error("ValueError during pipeline execution", error=str(val_err), exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Pipeline configuration or input error: {val_err}")
    except Exception as e:
        run_log.exception("Unexpected error occurred during RAG pipeline execution")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error processing query: {type(e).__name__}")

# --- Dependency Check Function (sin cambios desde la última versión) ---
# LLM_COMMENT: This function checks the status of external dependencies (Milvus, API keys) during startup or health checks.
async def check_pipeline_dependencies() -> Dict[str, str]:
    results = {"milvus_connection": "pending", "openai_api": "pending", "gemini_api": "pending"}
    try:
        store = get_milvus_document_store()
        count = await asyncio.to_thread(store.count_documents)
        results["milvus_connection"] = "ok"
        log.debug("Milvus dependency check successful.", document_count=count)
        # LLM_COMMENT: Milvus check involves connecting and performing a simple operation like counting documents.
    except MilvusException as e:
        # LLM_COMMENT: Handle specific Milvus errors like CollectionNotFound (acceptable) or connection errors (warning).
        if e.code == ErrorCode.COLLECTION_NOT_FOUND:
            results["milvus_connection"] = "ok (collection not found yet)"
            log.info("Milvus dependency check: Collection not found (expected if empty, will be created on write).")
        elif e.code == ErrorCode.UNEXPECTED_ERROR and "connect failed" in e.message.lower():
            results["milvus_connection"] = f"error: Connection Failed (code={e.code}, msg={e.message})"
            log.warning("Milvus dependency check failed: Connection Error", error_code=e.code, error_message=e.message, exc_info=False)
        else:
            results["milvus_connection"] = f"error: MilvusException (code={e.code}, msg={e.message})"
            log.warning("Milvus dependency check failed with Milvus error", error_code=e.code, error_message=e.message, exc_info=False)
    except RuntimeError as rte:
         results["milvus_connection"] = f"error: Initialization Failed ({rte})"
         log.warning("Milvus dependency check failed during store initialization", error=str(rte), exc_info=False)
    except Exception as e:
        results["milvus_connection"] = f"error: Unexpected {type(e).__name__}"
        log.warning("Milvus dependency check failed with unexpected error", error=str(e), exc_info=True)

    # LLM_COMMENT: Check for the presence of necessary API keys.
    if settings.OPENAI_API_KEY.get_secret_value() and settings.OPENAI_API_KEY.get_secret_value() != "dummy-key":
        results["openai_api"] = "key_present"
    else:
        results["openai_api"] = "key_missing"
        log.warning("OpenAI API Key missing or is dummy key in config.")

    if settings.GEMINI_API_KEY.get_secret_value():
        results["gemini_api"] = "key_present"
    else:
        results["gemini_api"] = "key_missing"
        log.warning("Gemini API Key missing in config.")

    return results