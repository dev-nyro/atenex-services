# query-service/app/pipelines/rag_pipeline.py
import structlog
import asyncio
import uuid
from typing import Dict, Any, List, Tuple, Optional

from pymilvus.exceptions import MilvusException, ErrorCode
from fastapi import HTTPException, status
from haystack import Document # LLM_COMMENT: Keep Haystack Document import
# LLM_COMMENT: Import FastEmbed Text Embedder for Haystack
from haystack_integrations.components.embedders.fastembed import FastembedTextEmbedder
from haystack.components.builders.prompt_builder import PromptBuilder
from milvus_haystack import MilvusDocumentStore, MilvusEmbeddingRetriever # LLM_COMMENT: Keep Milvus imports
from haystack.utils import Secret

from app.core.config import settings
from app.db import postgres_client
from app.services.gemini_client import gemini_client # LLM_COMMENT: Keep Gemini client import
from app.api.v1.schemas import RetrievedDocument # LLM_COMMENT: Import schema for logging

log = structlog.get_logger(__name__)

# --- Component Initialization Functions ---
# LLM_COMMENT: Encapsulate component initialization for clarity and potential reuse/caching

def get_milvus_document_store() -> MilvusDocumentStore:
    """Initializes and returns a MilvusDocumentStore instance."""
    connection_uri = str(settings.MILVUS_URI)
    store_log = log.bind(component="MilvusDocumentStore", uri=connection_uri, collection=settings.MILVUS_COLLECTION_NAME)
    store_log.debug("Initializing...")
    try:
        store = MilvusDocumentStore(
            connection_args={"uri": connection_uri},
            collection_name=settings.MILVUS_COLLECTION_NAME,
            embedding_dim=settings.EMBEDDING_DIMENSION, # LLM_COMMENT: Dimension is important for potential auto-creation
            embedding_field=settings.MILVUS_EMBEDDING_FIELD,
            content_field=settings.MILVUS_CONTENT_FIELD,
            metadata_fields=settings.MILVUS_METADATA_FIELDS,
            index_params=settings.MILVUS_INDEX_PARAMS, # LLM_COMMENT: Pass index params
            search_params=settings.MILVUS_SEARCH_PARAMS, # LLM_COMMENT: Pass search params
            consistency_level="Strong", # LLM_COMMENT: Ensure consistency for RAG
        )
        store_log.info("Initialization successful.")
        return store
    except Exception as e:
        store_log.error("Initialization failed", error=str(e), exc_info=True)
        raise RuntimeError(f"Milvus initialization error: {e}")

def get_fastembed_text_embedder() -> FastembedTextEmbedder:
    """Initializes and returns a FastembedTextEmbedder instance."""
    embedder_log = log.bind(component="FastembedTextEmbedder", model=settings.FASTEMBED_MODEL_NAME, prefix=settings.FASTEMBED_QUERY_PREFIX)
    embedder_log.debug("Initializing...")
    # LLM_COMMENT: No API key needed for FastEmbed, uses local model.
    embedder = FastembedTextEmbedder(
        model=settings.FASTEMBED_MODEL_NAME,
        prefix=settings.FASTEMBED_QUERY_PREFIX # LLM_COMMENT: Apply query prefix if defined
        # meta_fields_to_embed=[] # LLM_COMMENT: Usually not needed for query embedding
    )
    embedder_log.info("Initialization successful.")
    return embedder

# LLM_COMMENT: PromptBuilder initialization remains the same logic but uses templates from config
def get_prompt_builder(template: str) -> PromptBuilder:
    """Initializes PromptBuilder with a given template."""
    log.debug("Initializing PromptBuilder...")
    return PromptBuilder(template=template)

# --- Pipeline Execution Logic ---
# LLM_COMMENT: Refactored RAG pipeline execution into distinct async steps for clarity
# LLM_COMMENT: Added conditional prompt logic based on document retrieval

async def embed_query(query: str) -> List[float]:
    """Embeds the user query using FastEmbed."""
    embed_log = log.bind(action="embed_query")
    try:
        embedder = get_fastembed_text_embedder()
        # LLM_COMMENT: FastEmbed components might be synchronous internally, run in thread
        result = await asyncio.to_thread(embedder.run, text=query)
        embedding = result.get("embedding")
        if not embedding:
            raise ValueError("Embedding process returned no embedding vector.")
        embed_log.info("Query embedded successfully", vector_dim=len(embedding))
        return embedding
    except Exception as e:
        embed_log.error("Embedding failed", error=str(e), exc_info=True)
        raise ConnectionError(f"Embedding service error: {e}") from e # LLM_COMMENT: Raise specific error type

async def retrieve_documents(embedding: List[float], company_id: str, top_k: int) -> List[Document]:
    """Retrieves relevant documents from Milvus based on the query embedding and company_id."""
    retrieve_log = log.bind(action="retrieve_documents", company_id=company_id, top_k=top_k)
    try:
        document_store = get_milvus_document_store()
        # LLM_COMMENT: Construct filters for multi-tenancy
        filters = {"company_id": company_id}
        retriever = MilvusEmbeddingRetriever(
            document_store=document_store,
            filters=filters,
            top_k=top_k
        )
        # LLM_COMMENT: Milvus retrieval might be synchronous, run in thread
        result = await asyncio.to_thread(retriever.run, query_embedding=embedding)
        documents = result.get("documents", [])
        retrieve_log.info("Documents retrieved successfully", count=len(documents))
        return documents
    except MilvusException as me:
         retrieve_log.error("Milvus retrieval failed", error_code=me.code, error_message=me.message, exc_info=False)
         raise ConnectionError(f"Vector DB retrieval error (Milvus code: {me.code})") from me
    except Exception as e:
        retrieve_log.error("Retrieval failed", error=str(e), exc_info=True)
        raise ConnectionError(f"Retrieval service error: {e}") from e # LLM_COMMENT: Raise specific error type

async def build_prompt(query: str, documents: List[Document]) -> str:
    """Builds the final prompt for the LLM, selecting template based on retrieved documents."""
    build_log = log.bind(action="build_prompt", num_docs=len(documents))
    try:
        # LLM_COMMENT: Conditional prompt template selection
        if documents:
            template = settings.RAG_PROMPT_TEMPLATE
            prompt_builder = get_prompt_builder(template)
            result = await asyncio.to_thread(prompt_builder.run, query=query, documents=documents)
            build_log.info("RAG prompt built")
        else:
            template = settings.GENERAL_PROMPT_TEMPLATE
            prompt_builder = get_prompt_builder(template)
            # LLM_COMMENT: Only pass query when no documents are used
            result = await asyncio.to_thread(prompt_builder.run, query=query)
            build_log.info("General prompt built (no documents retrieved)")

        prompt = result.get("prompt")
        if not prompt:
            raise ValueError("Prompt generation returned empty prompt.")
        return prompt
    except Exception as e:
        build_log.error("Prompt building failed", error=str(e), exc_info=True)
        raise ValueError(f"Prompt building error: {e}") from e # LLM_COMMENT: Raise specific error type

async def generate_llm_answer(prompt: str) -> str:
    """Generates the final answer using the Gemini client."""
    llm_log = log.bind(action="generate_llm_answer", model=settings.GEMINI_MODEL_NAME)
    try:
        answer = await gemini_client.generate_answer(prompt)
        llm_log.info("Answer generated successfully", answer_length=len(answer))
        return answer
    except Exception as e:
        llm_log.error("LLM generation failed", error=str(e), exc_info=True)
        raise ConnectionError(f"LLM service error: {e}") from e # LLM_COMMENT: Raise specific error type

async def run_rag_pipeline(
    query: str,
    company_id: str,
    user_id: Optional[str], # LLM_COMMENT: user_id can be optional for logging
    top_k: Optional[int] = None,
    chat_id: Optional[uuid.UUID] = None # LLM_COMMENT: Pass chat_id for logging context
) -> Tuple[str, List[Document], Optional[uuid.UUID]]:
    """
    Orchestrates the RAG pipeline steps: embed, retrieve, build prompt, generate answer, log interaction.
    """
    run_log = log.bind(company_id=company_id, user_id=user_id or "N/A", chat_id=str(chat_id) if chat_id else "N/A", query=query[:50]+"...")
    run_log.info("Executing RAG pipeline")
    retriever_k = top_k or settings.RETRIEVER_TOP_K
    log_id: Optional[uuid.UUID] = None # LLM_COMMENT: Initialize log_id as None

    try:
        # 1. Embed Query
        query_embedding = await embed_query(query)

        # 2. Retrieve Documents
        retrieved_docs = await retrieve_documents(query_embedding, company_id, retriever_k)

        # 3. Build Prompt (Conditional based on retrieved docs)
        final_prompt = await build_prompt(query, retrieved_docs)

        # 4. Generate Answer
        answer = await generate_llm_answer(final_prompt)

        # 5. Log Interaction (Best effort)
        try:
            # LLM_COMMENT: Format retrieved docs for logging
            docs_for_log = [RetrievedDocument.from_haystack_doc(d).model_dump(exclude_none=True)
                            for d in retrieved_docs]
            log_id = await postgres_client.log_query_interaction(
                company_id=uuid.UUID(company_id), # LLM_COMMENT: Ensure UUID type
                user_id=uuid.UUID(user_id) if user_id else None, # LLM_COMMENT: Ensure UUID type or None
                query=query, answer=answer,
                retrieved_documents_data=docs_for_log,
                chat_id=chat_id,
                # LLM_COMMENT: Added metadata for context in logs
                metadata={"top_k": retriever_k, "llm_model": settings.GEMINI_MODEL_NAME, "embedder_model": settings.FASTEMBED_MODEL_NAME, "num_retrieved": len(retrieved_docs)}
            )
            run_log.info("Interaction logged successfully", db_log_id=str(log_id))
        except Exception as log_err:
            run_log.error("Failed to log RAG interaction to DB", error=str(log_err), exc_info=False) # LLM_COMMENT: Log error but don't fail the request

        run_log.info("RAG pipeline completed successfully")
        return answer, retrieved_docs, log_id

    # LLM_COMMENT: Catch specific errors from pipeline steps and map to HTTP exceptions
    except ConnectionError as ce: # Errors connecting to Milvus, Embedder, LLM
        run_log.error("Connection error during RAG pipeline", error=str(ce), exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"A dependency is unavailable: {ce}")
    except ValueError as ve: # Errors during embedding or prompt building
        run_log.error("Value error during RAG pipeline", error=str(ve), exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Data processing error: {ve}")
    except Exception as e: # Catch-all for unexpected errors
        run_log.exception("Unexpected error during RAG pipeline execution")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An internal error occurred.")

# --- Dependency Check Function ---
async def check_pipeline_dependencies() -> Dict[str, str]:
    """Checks the status of pipeline dependencies (Milvus, Gemini)."""
    # LLM_COMMENT: Keep dependency check logic, adapted for FastEmbed/Gemini
    results = {"milvus_connection": "pending", "gemini_api": "pending", "fastembed_model": "pending"}
    check_log = log.bind(action="check_dependencies")

    # Check Milvus
    try:
        store = get_milvus_document_store()
        # LLM_COMMENT: Use a potentially lighter check like has_collection if available, or count_documents
        collection_exists = await asyncio.to_thread(store.conn.has_collection, store.collection_name)
        if collection_exists:
            results["milvus_connection"] = "ok"
            check_log.debug("Milvus dependency check: Connection ok, collection exists.")
        else:
             results["milvus_connection"] = "ok (collection not found yet)"
             check_log.info("Milvus dependency check: Connection ok, collection does not exist (will be created on write).")
    except MilvusException as me:
        results["milvus_connection"] = f"error: MilvusException (code={me.code}, msg={me.message})"
        check_log.warning("Milvus dependency check failed", error_code=me.code, error_message=me.message, exc_info=False)
    except Exception as e:
        results["milvus_connection"] = f"error: Unexpected {type(e).__name__}"
        check_log.warning("Milvus dependency check failed", error=str(e), exc_info=True)

    # Check Gemini (API Key Presence)
    if settings.GEMINI_API_KEY.get_secret_value():
        results["gemini_api"] = "key_present"
        check_log.debug("Gemini dependency check: API Key is present.")
    else:
        results["gemini_api"] = "key_missing"
        check_log.warning("Gemini dependency check: API Key is MISSING.")

    # Check FastEmbed (Model loading is lazy, just check config)
    if settings.FASTEMBED_MODEL_NAME:
         results["fastembed_model"] = f"configured ({settings.FASTEMBED_MODEL_NAME})"
         check_log.debug("FastEmbed dependency check: Model configured.", model=settings.FASTEMBED_MODEL_NAME)
    else:
         results["fastembed_model"] = "config_missing"
         check_log.error("FastEmbed dependency check: Model name MISSING in configuration.")


    return results