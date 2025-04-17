# query-service/app/pipelines/rag_pipeline.py
import structlog
import asyncio
import uuid
from typing import Dict, Any, List, Tuple, Optional

from pymilvus.exceptions import MilvusException, ErrorCode
from fastapi import HTTPException, status
from haystack import Document
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

def get_prompt_builder() -> PromptBuilder:
    log.debug("Initializing PromptBuilder")
    return PromptBuilder(template=settings.RAG_PROMPT_TEMPLATE)


# --- Pipeline Execution (Using manual steps) ---
async def run_rag_pipeline(
    query: str,
    company_id: str,
    user_id: Optional[str],
    top_k: Optional[int] = None,
    chat_id: Optional[uuid.UUID] = None
) -> Tuple[str, List[Document], Optional[uuid.UUID]]:
    """
    Ejecuta el Async RAG pipeline (con FastEmbed) usando el método manual embed->retrieve->prompt steps.
    Llama a Gemini y loguea la interacción.
    """
    run_log = log.bind(query=query, company_id=company_id, user_id=user_id or "N/A", chat_id=str(chat_id) if chat_id else "N/A")
    run_log.info("Running RAG pipeline (manual flow) with FastEmbed...")

    # Define retrieval params
    retriever_top_k = top_k or settings.RETRIEVER_TOP_K
    filters = [{"field": settings.MILVUS_COMPANY_ID_FIELD, "operator": "==", "value": company_id}]

    # Step 1: Embed query
    try:
        embedding = get_fastembed_text_embedder().run(query).get("embedding")
    except Exception as e:
        run_log.error("Embedding failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Embedding error")

    # Step 2: Retrieve documents
    try:
        retriever = MilvusEmbeddingRetriever(document_store=get_milvus_document_store(), filters=filters, top_k=retriever_top_k)
        docs = await asyncio.to_thread(retriever.run, embedding)
        docs = docs.get("documents", [])
    except Exception as e:
        run_log.error("Retrieval failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Retrieval error")

    # Step 3: Build prompt
    try:
        prompt_text = PromptBuilder(template=settings.RAG_PROMPT_TEMPLATE).run(query=query, documents=docs).get("prompt")
    except Exception as e:
        run_log.error("Prompt building failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Prompt building error")

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