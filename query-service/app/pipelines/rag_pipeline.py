# query-service/app/pipelines/rag_pipeline.py
import structlog
import asyncio
import uuid
from typing import Dict, Any, List, Tuple, Optional

from pymilvus.exceptions import MilvusException, ErrorCode
from fastapi import HTTPException, status
from haystack import AsyncPipeline, Document
# LLM_COMMENT: Import FastembedTextEmbedder instead of OpenAITextEmbedder
from haystack_integrations.components.embedders.fastembed import FastembedTextEmbedder # LLM_COMMENT_CHANGE
from haystack.components.builders.prompt_builder import PromptBuilder
from milvus_haystack import MilvusDocumentStore, MilvusEmbeddingRetriever
# LLM_COMMENT: Secret is no longer needed here for FastEmbed, but might be used elsewhere. Keeping for now.
from haystack.utils import Secret

from app.core.config import settings
from app.db import postgres_client
from app.services.gemini_client import gemini_client # LLM_COMMENT: Gemini client remains
from app.api.v1.schemas import RetrievedDocument

log = structlog.get_logger(__name__)

# --- Component Initialization Functions ---

def get_milvus_document_store() -> MilvusDocumentStore:
    # LLM_COMMENT: Milvus connection setup remains unchanged.
    connection_uri = str(settings.MILVUS_URI)
    log.debug("Initializing MilvusDocumentStore...", uri=connection_uri,
              collection=settings.MILVUS_COLLECTION_NAME)
    try:
        store = MilvusDocumentStore(
            connection_args={"uri": connection_uri},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            search_params=settings.MILVUS_SEARCH_PARAMS,
            consistency_level="Strong",
        )
        log.info("MilvusDocumentStore parameters configured.", uri=connection_uri,
                 collection=settings.MILVUS_COLLECTION_NAME)
        return store
    except Exception as e:
        log.error("Failed to initialize MilvusDocumentStore", error=str(e), exc_info=True)
        raise RuntimeError(f"Milvus initialization error: {e}")

# LLM_COMMENT: Renamed function to reflect the change to FastEmbed.
def get_fastembed_text_embedder() -> FastembedTextEmbedder: # LLM_COMMENT_CHANGE
    # LLM_COMMENT: Instantiate FastembedTextEmbedder using settings from config.py.
    log.debug("Initializing FastembedTextEmbedder", model=settings.FASTEMBED_MODEL_NAME, prefix=settings.FASTEMBED_QUERY_PREFIX) # LLM_COMMENT_CHANGE
    return FastembedTextEmbedder(
        model=settings.FASTEMBED_MODEL_NAME,
        prefix=settings.FASTEMBED_QUERY_PREFIX or "", # Use prefix from settings, default to empty string if None
        # LLM_COMMENT: Add other FastEmbed params like cache_dir, threads if needed via settings.
        # cache_dir=settings.FASTEMBED_CACHE_DIR,
        # threads=settings.FASTEMBED_THREADS
    )

def get_milvus_retriever(document_store: MilvusDocumentStore) -> MilvusEmbeddingRetriever:
    # LLM_COMMENT: Retriever initialization remains the same, depends on DocumentStore.
    log.debug("Initializing MilvusEmbeddingRetriever")
    return MilvusEmbeddingRetriever(
        document_store=document_store,
        top_k=settings.RETRIEVER_TOP_K
    )

def get_prompt_builder() -> PromptBuilder:
    # LLM_COMMENT: PromptBuilder initialization remains the same.
    log.debug("Initializing PromptBuilder")
    return PromptBuilder(template=settings.RAG_PROMPT_TEMPLATE)


# --- Pipeline Construction (Using AsyncPipeline) ---
_rag_pipeline_instance: Optional[AsyncPipeline] = None
def build_rag_pipeline() -> AsyncPipeline:
    global _rag_pipeline_instance
    if _rag_pipeline_instance:
        return _rag_pipeline_instance
    log.info("Building Haystack Async RAG pipeline with FastEmbed...") # LLM_COMMENT_CHANGE
    pipeline = AsyncPipeline()

    try:
        doc_store = get_milvus_document_store()
        # LLM_COMMENT: Use the new function to get FastEmbed instance.
        text_embedder = get_fastembed_text_embedder() # LLM_COMMENT_CHANGE
        retriever = get_milvus_retriever(document_store=doc_store)
        prompt_builder = get_prompt_builder()

        # LLM_COMMENT: Component names ("text_embedder", "retriever", "prompt_builder") are kept for consistency.
        pipeline.add_component("text_embedder", text_embedder)
        pipeline.add_component("retriever", retriever)
        pipeline.add_component("prompt_builder", prompt_builder)

        # LLM_COMMENT: Connections remain the same based on component names and standard socket names (embedding, documents).
        pipeline.connect("text_embedder.embedding", "retriever.query_embedding")
        pipeline.connect("retriever.documents", "prompt_builder.documents")

        _rag_pipeline_instance = pipeline
        log.info("Haystack Async RAG pipeline with FastEmbed built successfully.") # LLM_COMMENT_CHANGE
        return pipeline
    except Exception as e:
        log.error("Failed to build Haystack Async RAG pipeline with FastEmbed", error=str(e), exc_info=True) # LLM_COMMENT_CHANGE
        raise RuntimeError("Could not build the Async RAG pipeline with FastEmbed") from e # LLM_COMMENT_CHANGE

# --- Pipeline Execution (Using run_async) ---
async def run_rag_pipeline(
    query: str,
    company_id: str,
    user_id: Optional[str],
    top_k: Optional[int] = None,
    chat_id: Optional[uuid.UUID] = None
) -> Tuple[str, List[Document], Optional[uuid.UUID]]:
    """
    Ejecuta el Async RAG pipeline (con FastEmbed) usando el método `run_async`.
    Llama a Gemini y loguea la interacción.
    """
    # LLM_COMMENT: Orchestrates the RAG flow using AsyncPipeline.run_async with FastEmbed.
    run_log = log.bind(query=query, company_id=company_id,
                       user_id=user_id or "N/A", chat_id=str(chat_id) if chat_id else "N/A")
    run_log.info("Running Async RAG pipeline execution flow (FastEmbed)...") # LLM_COMMENT_CHANGE

    try:
        pipeline = build_rag_pipeline()
    except Exception as e:
        run_log.error("Pipeline build failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RAG pipeline unavailable"
        )

    retriever_top_k = top_k or settings.RETRIEVER_TOP_K
    filters = [{"field": settings.MILVUS_COMPANY_ID_FIELD,
                "operator": "==",
                "value": company_id}]
    # LLM_COMMENT: Filters remain the same, targeting company_id in Milvus metadata.

    # Prepare inputs for run_async (single data dictionary)
    pipeline_data = {
        "text_embedder": {"text": query}, # LLM_COMMENT: Input for FastembedTextEmbedder component named "text_embedder"
        "prompt_builder": {"query": query},
        "retriever": {"filters": filters, "top_k": retriever_top_k} # LLM_COMMENT: Runtime parameters for retriever
    }

    run_log.debug("Pipeline inputs prepared", data_structure=pipeline_data)

    try:
        # LLM_COMMENT: Execute using run_async with the combined data dictionary.
        result = await pipeline.run_async(data=pipeline_data)
        run_log.info("AsyncPipeline executed successfully.")
    except Exception as e:
        # LLM_COMMENT: Specific handling for potential FastEmbed/pipeline errors added below if needed.
        run_log.error("Pipeline execution error", error=str(e), exc_info=True)
        # LLM_COMMENT: Check if the error is related to embedding dimension mismatch (example)
        if "embedding dimension" in str(e).lower():
            run_log.critical("Potential embedding dimension mismatch!", configured_dim=settings.EMBEDDING_DIMENSION, error_details=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error processing query: {type(e).__name__}"
        )

    # Extract documents & prompt (Logic remains the same)
    docs: List[Document] = result.get("retriever", {}).get("documents", [])
    prompt_out = result.get("prompt_builder", {})
    prompt_text = prompt_out.get("prompt") or f"Pregunta: {query}\n(no se construyó prompt)"

    # Generate answer via Gemini (Logic remains the same)
    try:
        answer = await gemini_client.generate_answer(prompt_text)
    except Exception as e:
        run_log.error("Gemini generation failed", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM generation error"
        )

    # Log query interaction (Logic remains the same)
    log_id: Optional[uuid.UUID] = None
    try:
        docs_for_log = [RetrievedDocument.from_haystack_doc(d).model_dump(exclude_none=True)
                        for d in docs]
        log_id = await postgres_client.log_query_interaction(
            company_id=uuid.UUID(company_id) if isinstance(company_id, str) else company_id,
            user_id=uuid.UUID(user_id) if user_id else None,
            query=query, answer=answer,
            retrieved_documents_data=docs_for_log,
            chat_id=chat_id,
            metadata={"top_k": retriever_top_k, "model": settings.GEMINI_MODEL_NAME, "embedder": settings.FASTEMBED_MODEL_NAME} # LLM_COMMENT: Added embedder info to log metadata
        )
    except Exception as e:
        run_log.error("Failed to log interaction", error=str(e), exc_info=True)

    return answer, docs, log_id

# --- Dependency Check Function ---
# LLM_COMMENT: Updated to remove OpenAI API key check.
async def check_pipeline_dependencies() -> Dict[str, str]:
    results = {"milvus_connection": "pending", "gemini_api": "pending"} # LLM_COMMENT_CHANGE: Removed "openai_api"
    # LLM_COMMENT: Milvus check remains the same.
    try:
        store = get_milvus_document_store()
        count = await asyncio.to_thread(store.count_documents)
        results["milvus_connection"] = "ok"
        log.debug("Milvus dependency check successful.", document_count=count)
    except MilvusException as e:
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

    # LLM_COMMENT: Removed OpenAI API key check. FastEmbed runs locally.
    # LLM_COMMENT: Gemini API key check remains.
    if settings.GEMINI_API_KEY.get_secret_value():
        results["gemini_api"] = "key_present"
    else:
        results["gemini_api"] = "key_missing"
        log.warning("Gemini API Key missing in config.")

    return results