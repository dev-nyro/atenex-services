# query-service/app/pipelines/rag_pipeline.py
import structlog
import asyncio
import uuid
from typing import Dict, Any, List, Tuple, Optional

from pymilvus.exceptions import MilvusException, ErrorCode
from fastapi import HTTPException, status
from haystack import AsyncPipeline, Document
from haystack_integrations.components.embedders.fastembed.fastembed_text_embedder import FastembedTextEmbedder
from haystack.components.builders.prompt_builder import PromptBuilder
from milvus_haystack import MilvusDocumentStore, MilvusEmbeddingRetriever
from haystack.utils import Secret

from app.core.config import settings
from app.db import postgres_client
from app.services.gemini_client import gemini_client
from app.api.v1.schemas import RetrievedDocument

log = structlog.get_logger(__name__)

# --- Component Initialization Functions ---

def get_milvus_document_store() -> MilvusDocumentStore:
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

def get_fastembed_text_embedder() -> FastembedTextEmbedder:
    log.debug("Initializing FastembedTextEmbedder", model=settings.FASTEMBED_MODEL_NAME, prefix=settings.FASTEMBED_QUERY_PREFIX)
    return FastembedTextEmbedder(
        model=settings.FASTEMBED_MODEL_NAME,
        prefix=settings.FASTEMBED_QUERY_PREFIX or "",
    )

def get_milvus_retriever(document_store: MilvusDocumentStore) -> MilvusEmbeddingRetriever:
    log.debug("Initializing MilvusEmbeddingRetriever")
    return MilvusEmbeddingRetriever(
        document_store=document_store,
        top_k=settings.RETRIEVER_TOP_K
    )

def get_prompt_builder() -> PromptBuilder:
    log.debug("Initializing PromptBuilder")
    return PromptBuilder(template=settings.RAG_PROMPT_TEMPLATE)


# --- Pipeline Construction (Using AsyncPipeline) ---
_rag_pipeline_instance: Optional[AsyncPipeline] = None
def build_rag_pipeline() -> AsyncPipeline:
    global _rag_pipeline_instance
    if _rag_pipeline_instance:
        return _rag_pipeline_instance
    log.info("Building Haystack Async RAG pipeline with FastEmbed...")
    pipeline = AsyncPipeline()

    try:
        doc_store = get_milvus_document_store()
        text_embedder = get_fastembed_text_embedder()
        retriever = get_milvus_retriever(document_store=doc_store)
        prompt_builder = get_prompt_builder()

        pipeline.add_component("text_embedder", text_embedder)
        pipeline.add_component("retriever", retriever)
        pipeline.add_component("prompt_builder", prompt_builder)

        pipeline.connect("text_embedder.embedding", "retriever.query_embedding")
        pipeline.connect("retriever.documents", "prompt_builder.documents")

        _rag_pipeline_instance = pipeline
        log.info("Haystack Async RAG pipeline with FastEmbed built successfully.")
        return pipeline
    except Exception as e:
        log.error("Failed to build Haystack Async RAG pipeline with FastEmbed", error=str(e), exc_info=True)
        raise RuntimeError("Could not build the Async RAG pipeline with FastEmbed") from e

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
    run_log = log.bind(query=query, company_id=company_id,
                       user_id=user_id or "N/A", chat_id=str(chat_id) if chat_id else "N/A")
    run_log.info("Running Async RAG pipeline execution flow (FastEmbed)...")

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

    # Prepare a single data dict for run_async, including all component inputs
    pipeline_data = {
        "text_embedder": {"text": query},
        "retriever": {"filters": filters, "top_k": retriever_top_k},
        "prompt_builder": {"query": query}
    }
    run_log.debug("Pipeline inputs prepared", data_structure=pipeline_data)

    try:
        result = await pipeline.run_async(data=pipeline_data)
        run_log.info("AsyncPipeline executed successfully.")
    except Exception as e:
        run_log.error("Pipeline execution error", error=str(e), exc_info=True)
        if "embedding dimension" in str(e).lower():
            run_log.critical("Potential embedding dimension mismatch!", configured_dim=settings.EMBEDDING_DIMENSION, error_details=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error processing query: {type(e).__name__}"
        )

    docs: List[Document] = result.get("retriever", {}).get("documents", [])
    prompt_out = result.get("prompt_builder", {})
    prompt_text = prompt_out.get("prompt") or f"Pregunta: {query}\n(no se construyó prompt)"

    try:
        answer = await gemini_client.generate_answer(prompt_text)
    except Exception as e:
        run_log.error("Gemini generation failed", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM generation error"
        )

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
            metadata={"top_k": retriever_top_k, "model": settings.GEMINI_MODEL_NAME, "embedder": settings.FASTEMBED_MODEL_NAME}
        )
    except Exception as e:
        run_log.error("Failed to log interaction", error=str(e), exc_info=True)

    return answer, docs, log_id

# --- Dependency Check Function ---
async def check_pipeline_dependencies() -> Dict[str, str]:
    results = {"milvus_connection": "pending", "gemini_api": "pending"}
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

    if settings.GEMINI_API_KEY.get_secret_value():
        results["gemini_api"] = "key_present"
    else:
        results["gemini_api"] = "key_missing"
        log.warning("Gemini API Key missing in config.")

    return results