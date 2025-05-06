# query-service/app/application/use_cases/ask_query_use_case.py
import structlog
import asyncio
import uuid
import re
from typing import Dict, Any, List, Tuple, Optional, Type, Set

# Import Ports and Domain Models
from app.application.ports import (
    ChatRepositoryPort, LogRepositoryPort, VectorStorePort, LLMPort,
    SparseRetrieverPort, RerankerPort, DiversityFilterPort, ChunkContentRepositoryPort
)
from app.domain.models import RetrievedChunk, ChatMessage

# Keep necessary components (Embedder, PromptBuilder)
from haystack_integrations.components.embedders.fastembed import FastembedTextEmbedder
from haystack.components.builders.prompt_builder import PromptBuilder
from haystack import Document # Still needed for PromptBuilder context

from app.core.config import settings
from app.api.v1.schemas import RetrievedDocument as RetrievedDocumentSchema # For logging format
from app.utils.helpers import truncate_text
from fastapi import HTTPException, status

log = structlog.get_logger(__name__)

GREETING_REGEX = re.compile(r"^\s*(hola|hello|hi|buenos días|buenas tardes|buenas noches|hey|qué tal|hi there)\s*[\.,!?]*\s*$", re.IGNORECASE)

# RRF Constant
RRF_K = 60

class AskQueryUseCase:
    """
    Caso de uso para procesar una consulta de usuario, manejar el chat y ejecutar el pipeline RAG completo.
    """
    def __init__(self,
                 chat_repo: ChatRepositoryPort,
                 log_repo: LogRepositoryPort,
                 vector_store: VectorStorePort,
                 llm: LLMPort,
                 # Optional components for advanced RAG
                 sparse_retriever: Optional[SparseRetrieverPort] = None,
                 chunk_content_repo: Optional[ChunkContentRepositoryPort] = None,
                 reranker: Optional[RerankerPort] = None,
                 diversity_filter: Optional[DiversityFilterPort] = None):
        self.chat_repo = chat_repo
        self.log_repo = log_repo
        self.vector_store = vector_store
        self.llm = llm
        # Assign components based on settings
        self.sparse_retriever = sparse_retriever if settings.BM25_ENABLED else None
        self.chunk_content_repo = chunk_content_repo
        self.reranker = reranker if settings.RERANKER_ENABLED else None
        # LLM_CORRECTION: Use diversity_filter if enabled, store the instance (could be MMR or Stub based on main.py logic)
        self.diversity_filter = diversity_filter if settings.DIVERSITY_FILTER_ENABLED else None

        self._embedder = self._initialize_embedder()
        self._prompt_builder_rag = self._initialize_prompt_builder(settings.RAG_PROMPT_TEMPLATE)
        self._prompt_builder_general = self._initialize_prompt_builder(settings.GENERAL_PROMPT_TEMPLATE)

        log.info("AskQueryUseCase Initialized",
                 bm25_enabled=bool(self.sparse_retriever),
                 reranker_enabled=bool(self.reranker),
                 diversity_filter_enabled=settings.DIVERSITY_FILTER_ENABLED, # Check the setting directly
                 diversity_filter_type=type(self.diversity_filter).__name__ if self.diversity_filter else "None"
                 )
        if self.sparse_retriever and not self.chunk_content_repo:
            log.error("SparseRetriever is enabled but ChunkContentRepositoryPort is missing!")


    def _initialize_embedder(self) -> FastembedTextEmbedder:
        embedder_log = log.bind(component="FastembedTextEmbedder", model=settings.FASTEMBED_MODEL_NAME)
        embedder_log.debug("Initializing FastEmbed Embedder...")
        try:
            embedder = FastembedTextEmbedder(
                model=settings.FASTEMBED_MODEL_NAME,
                prefix=settings.FASTEMBED_QUERY_PREFIX
            )
            embedder_log.info("FastEmbed Embedder initialized.")
            return embedder
        except Exception as e:
            embedder_log.exception("Failed to initialize FastEmbed Embedder")
            raise RuntimeError(f"Could not initialize embedding model: {e}") from e

    def _initialize_prompt_builder(self, template: str) -> PromptBuilder:
        log.debug("Initializing PromptBuilder...")
        return PromptBuilder(template=template)

    async def _embed_query(self, query: str) -> List[float]:
        embed_log = log.bind(action="embed_query_use_case")
        try:
            result = await asyncio.to_thread(self._embedder.run, text=query)
            embedding = result.get("embedding")
            if not embedding: raise ValueError("Embedding returned no vector.")
            if len(embedding) != settings.EMBEDDING_DIMENSION: raise ValueError("Embedding dimension mismatch.")
            embed_log.debug("Query embedded successfully", vector_dim=len(embedding))
            return embedding
        except Exception as e:
            embed_log.error("Embedding failed", error=str(e), exc_info=True)
            raise ConnectionError(f"Embedding service error: {e}") from e

    async def _build_prompt(self, query: str, documents: List[Document]) -> str:
        builder_log = log.bind(action="build_prompt_use_case", num_docs=len(documents))
        try:
            if documents:
                prompt_builder = self._prompt_builder_rag
                builder_log.debug("Using RAG prompt template.")
                result = await asyncio.to_thread(prompt_builder.run, query=query, documents=documents)
            else:
                prompt_builder = self._prompt_builder_general
                builder_log.debug("Using General prompt template.")
                result = await asyncio.to_thread(prompt_builder.run, query=query)
            prompt = result.get("prompt")
            if not prompt: raise ValueError("Prompt generation returned empty.")

            # --- LLM_COMMENT: Add token estimation/logging if needed ---
            # try:
            #    # Requires tokenizer library (e.g., tiktoken or Gemini's)
            #    # token_count = estimate_tokens(prompt)
            #    # builder_log.info("Estimated prompt token count", count=token_count)
            # except Exception as tok_err:
            #     builder_log.warning("Could not estimate prompt token count", error=str(tok_err))

            builder_log.debug("Prompt built successfully.")
            return prompt
        except Exception as e:
            builder_log.error("Prompt building failed", error=str(e), exc_info=True)
            raise ValueError(f"Prompt building error: {e}") from e

    def _reciprocal_rank_fusion(self,
                                dense_results: List[RetrievedChunk],
                                sparse_results: List[Tuple[str, float]],
                                k: int = RRF_K) -> Dict[str, float]:
        """Combines dense and sparse results using Reciprocal Rank Fusion."""
        fused_scores: Dict[str, float] = {}
        # Process dense results
        for rank, chunk in enumerate(dense_results):
            fused_scores[chunk.id] = fused_scores.get(chunk.id, 0.0) + 1.0 / (k + rank + 1)

        # Process sparse results
        for rank, (chunk_id, _) in enumerate(sparse_results):
             fused_scores[chunk_id] = fused_scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
        return fused_scores

    async def _fetch_content_for_fused_results(
        self,
        fused_scores: Dict[str, float],
        dense_map: Dict[str, RetrievedChunk],
        # --- LLM_CORRECTION: Fetch up to the final context chunk limit or slightly more ---
        top_n: int # Use settings.MAX_CONTEXT_CHUNKS here or slightly larger buffer
        ) -> List[RetrievedChunk]:
        """
        Gets the top_n chunks based on fused scores and fetches content
        for chunks that were only found in sparse results.
        """
        if not fused_scores: return []

        sorted_chunk_ids = sorted(fused_scores.items(), key=lambda item: item[1], reverse=True)
        # Fetch content for the top N based on fused score, respecting the top_n limit passed in
        top_ids = [cid for cid, score in sorted_chunk_ids[:top_n]]
        chunks_with_content: List[RetrievedChunk] = []
        ids_needing_content: List[str] = []
        final_scores: Dict[str, float] = dict(sorted_chunk_ids[:top_n]) # Scores only for the top N requested

        for cid in top_ids:
            if cid in dense_map and dense_map[cid].content:
                chunk = dense_map[cid]
                chunk.score = final_scores[cid] # Update with fused score
                chunks_with_content.append(chunk)
            else:
                # Check if we already have this chunk from dense map but without content (shouldn't happen ideally)
                if cid in dense_map:
                     chunk = dense_map[cid]
                     chunk.score = final_scores[cid]
                     # Still needs content fetching, but keep existing metadata/embedding
                     ids_needing_content.append(cid)
                     # Add placeholder to maintain order temporarily
                     chunks_with_content.append(chunk)
                else:
                    # Truly only found via sparse search
                    ids_needing_content.append(cid)
                    # Add placeholder to maintain order temporarily
                    # Create a temporary chunk with ID and score, content and metadata to be filled
                    chunks_with_content.append(RetrievedChunk(id=cid, score=final_scores[cid], content=None, metadata={"retrieval_source": "sparse/fused"}))


        if ids_needing_content and self.chunk_content_repo:
             log.debug("Fetching content for chunks found via sparse/fused search", count=len(ids_needing_content))
             try:
                 content_map = await self.chunk_content_repo.get_chunk_contents_by_ids(ids_needing_content)
                 # Create a map for faster lookup of chunks needing content
                 chunk_map_to_update = {c.id: c for c in chunks_with_content if c.id in ids_needing_content}

                 for cid in ids_needing_content:
                     if cid in content_map:
                         chunk_to_update = chunk_map_to_update.get(cid)
                         if chunk_to_update:
                              chunk_to_update.content = content_map[cid]
                              # Add fetched flag to metadata if needed
                              chunk_to_update.metadata["content_fetched"] = True
                         else:
                            log.warning("Chunk needing content not found in placeholder list", chunk_id=cid)
                     else: log.warning("Content not found for sparsely retrieved chunk after fetch", chunk_id=cid)
             except Exception: log.exception("Failed to fetch content for sparse/fused results")
        elif ids_needing_content: log.warning("Cannot fetch content for sparse/fused results, ChunkContentRepository not available.")

        # Filter out chunks that still don't have content after fetch attempt
        final_chunks = [c for c in chunks_with_content if c.content is not None]

        # Re-sort the final list by score, as fetching might change order slightly if placeholders were used
        final_chunks.sort(key=lambda c: c.score or 0.0, reverse=True)
        return final_chunks


    async def execute(
        self, query: str, company_id: uuid.UUID, user_id: uuid.UUID,
        chat_id: Optional[uuid.UUID] = None, top_k: Optional[int] = None
    ) -> Tuple[str, List[RetrievedChunk], Optional[uuid.UUID], uuid.UUID]:
        """ Orchestrates the full RAG pipeline """
        exec_log = log.bind(use_case="AskQueryUseCase", company_id=str(company_id), user_id=str(user_id), query=truncate_text(query, 50))
        # --- LLM_CORRECTION: Use configured retriever_k, allow override via request ---
        retriever_k = top_k if top_k is not None and 0 < top_k <= settings.RETRIEVER_TOP_K else settings.RETRIEVER_TOP_K
        exec_log = exec_log.bind(effective_retriever_k=retriever_k)

        pipeline_stages_used = ["dense_retrieval", "llm_generation"] # Base stages
        final_chat_id: uuid.UUID
        log_id: Optional[uuid.UUID] = None

        try:
            # 1. Manage Chat State
            if chat_id:
                if not await self.chat_repo.check_chat_ownership(chat_id, user_id, company_id):
                    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Chat not found or access denied.")
                final_chat_id = chat_id
            else:
                initial_title = f"Chat: {truncate_text(query, 40)}"
                final_chat_id = await self.chat_repo.create_chat(user_id=user_id, company_id=company_id, title=initial_title)
            exec_log = exec_log.bind(chat_id=str(final_chat_id))
            exec_log.info("Chat state managed", is_new=(not chat_id))

            # 2. Save User Message
            await self.chat_repo.save_message(chat_id=final_chat_id, role='user', content=query)

            # 3. Handle Greetings
            if GREETING_REGEX.match(query):
                answer = "¡Hola! ¿En qué puedo ayudarte hoy con la información de tus documentos?"
                await self.chat_repo.save_message(chat_id=final_chat_id, role='assistant', content=answer, sources=None)
                exec_log.info("Use case finished (greeting).")
                return answer, [], log_id, final_chat_id

            # --- RAG Pipeline ---
            exec_log.info("Proceeding with RAG pipeline...")

            # 4. Embed Query
            query_embedding = await self._embed_query(query)

            # 5. Coarse Retrieval (Dense + Sparse)
            dense_task = self.vector_store.search(query_embedding, str(company_id), retriever_k)
            sparse_task = asyncio.create_task(asyncio.sleep(0)) # Dummy if disabled
            sparse_results: List[Tuple[str, float]] = [] # Default empty
            if self.sparse_retriever:
                 pipeline_stages_used.append("sparse_retrieval (bm25s)")
                 sparse_task = self.sparse_retriever.search(query, str(company_id), retriever_k)

            # Await retrievals
            dense_chunks, sparse_results_maybe = await asyncio.gather(dense_task, sparse_task)
            if self.sparse_retriever: # Ensure sparse_results list is populated if run
                sparse_results = sparse_results_maybe if isinstance(sparse_results_maybe, list) else []
            exec_log.info("Retrieval phase completed", dense_count=len(dense_chunks), sparse_count=len(sparse_results), retriever_k=retriever_k)

            # 6. Fusion & Content Fetch
            pipeline_stages_used.append("fusion (rrf)")
            dense_map = {c.id: c for c in dense_chunks} # Map dense chunks by ID for easy lookup
            fused_scores = self._reciprocal_rank_fusion(dense_chunks, sparse_results)

            # --- LLM_CORRECTION: Fetch content for up to MAX_CONTEXT_CHUNKS candidates after fusion ---
            # Fetch slightly more than needed in case some lack content, but respect the final limit target.
            fusion_fetch_k = settings.MAX_CONTEXT_CHUNKS + 10 # Add a small buffer
            combined_chunks_with_content = await self._fetch_content_for_fused_results(fused_scores, dense_map, fusion_fetch_k)
            exec_log.info("Fusion & Content Fetch completed", initial_fused_count=len(fused_scores), chunks_with_content=len(combined_chunks_with_content), fetch_limit=fusion_fetch_k)

            # Check if any chunks remain after fetching content
            if not combined_chunks_with_content:
                 exec_log.warning("No chunks with content available after fusion/fetch.")
                 final_prompt = await self._build_prompt(query, []) # Use general prompt
                 answer = await self.llm.generate(final_prompt)
                 await self.chat_repo.save_message(chat_id=final_chat_id, role='assistant', content=answer, sources=None)
                 try: # Best effort logging
                    log_id = await self.log_repo.log_query_interaction(company_id=company_id, user_id=user_id, query=query, answer=answer, retrieved_documents_data=[], chat_id=final_chat_id, metadata={"pipeline_stages": pipeline_stages_used, "result": "no_docs_found"})
                 except Exception: exec_log.error("Failed to log interaction for no_docs case")
                 return answer, [], log_id, final_chat_id # Return empty list for retrieved docs

            # 7. Reranking (Conditional)
            chunks_to_process_further = combined_chunks_with_content
            if self.reranker: # Check if reranker instance exists (implies enabled and loaded)
                pipeline_stages_used.append("reranking (bge)")
                exec_log.debug("Performing reranking...", count=len(chunks_to_process_further))
                # Pass only up to MAX_CONTEXT_CHUNKS + buffer to reranker if it's very large? For now, rerank all fetched.
                chunks_to_process_further = await self.reranker.rerank(query, chunks_to_process_further)
                exec_log.info("Reranking completed.", count=len(chunks_to_process_further))

            # 8. Apply Diversity Filter / Final Limit
            final_chunks_for_llm = chunks_to_process_further
            # --- LLM_CORRECTION: Apply diversity filter OR truncate to MAX_CONTEXT_CHUNKS ---
            if self.diversity_filter: # Check if filter instance exists (covers enabled/disabled logic from init)
                 # Use the MAX_CONTEXT_CHUNKS setting as the target size 'k_final' for the filter
                 k_final = settings.MAX_CONTEXT_CHUNKS
                 filter_type = type(self.diversity_filter).__name__
                 pipeline_stages_used.append(f"diversity_filter ({filter_type})")
                 exec_log.debug(f"Applying {filter_type} k={k_final}...", count=len(chunks_to_process_further))
                 final_chunks_for_llm = await self.diversity_filter.filter(chunks_to_process_further, k_final)
                 exec_log.info(f"{filter_type} applied.", final_count=len(final_chunks_for_llm))
            else:
                 # If diversity_filter is None (because not enabled in settings),
                 # apply the limit manually before sending to LLM.
                 final_chunks_for_llm = chunks_to_process_further[:settings.MAX_CONTEXT_CHUNKS]
                 exec_log.info(f"Diversity filter disabled. Truncating to MAX_CONTEXT_CHUNKS.", final_count=len(final_chunks_for_llm), limit=settings.MAX_CONTEXT_CHUNKS)


            # 9. Build Prompt
            haystack_docs_for_prompt = [
                Document(id=c.id, content=c.content, meta=c.metadata, score=c.score)
                for c in final_chunks_for_llm if c.content # Double check content exists
            ]
            if not haystack_docs_for_prompt:
                 exec_log.warning("No documents remaining after filtering for RAG prompt, using general prompt.")
                 final_prompt = await self._build_prompt(query, [])
            else:
                 exec_log.info(f"Building RAG prompt with {len(haystack_docs_for_prompt)} final chunks.")
                 final_prompt = await self._build_prompt(query, haystack_docs_for_prompt)

            # 10. Generate Answer
            answer = await self.llm.generate(final_prompt)
            exec_log.info("LLM answer generated.", length=len(answer))

            # 11. Save Assistant Message
            assistant_sources = [
                {"chunk_id": c.id, "document_id": c.document_id, "file_name": c.file_name, "score": c.score, "preview": truncate_text(c.content, 150)}
                for c in final_chunks_for_llm # Use the final list passed to LLM
            ]
            await self.chat_repo.save_message(chat_id=final_chat_id, role='assistant', content=answer, sources=assistant_sources or None)

            # 12. Log Interaction
            try:
                # --- LLM_CORRECTION: Use final_chunks_for_llm for logging sources ---
                docs_for_log = [RetrievedDocumentSchema(**c.model_dump()).model_dump(exclude_none=True) for c in final_chunks_for_llm]
                log_metadata = {
                    "pipeline_stages": pipeline_stages_used,
                    "retriever_k": retriever_k,
                    "fusion_fetch_k": fusion_fetch_k, # Log the fetch limit
                    "max_context_chunks_limit": settings.MAX_CONTEXT_CHUNKS, # Log the setting value
                    "num_final_chunks_to_llm": len(final_chunks_for_llm),
                    "diversity_filter_enabled": settings.DIVERSITY_FILTER_ENABLED,
                    "reranker_enabled": settings.RERANKER_ENABLED,
                    "bm25_enabled": settings.BM25_ENABLED,
                 }
                log_id = await self.log_repo.log_query_interaction(
                    company_id=company_id, user_id=user_id, query=query, answer=answer,
                    retrieved_documents_data=docs_for_log, chat_id=final_chat_id, metadata=log_metadata
                )
                exec_log.info("Interaction logged successfully", db_log_id=str(log_id))
            except Exception as log_err:
                exec_log.error("Failed to log RAG interaction", error=str(log_err), exc_info=False)

            exec_log.info("Use case execution finished successfully.")
            # --- LLM_CORRECTION: Return final_chunks_for_llm ---
            return answer, final_chunks_for_llm, log_id, final_chat_id

        except ConnectionError as ce:
            exec_log.error("Connection error during use case execution", error=str(ce), exc_info=True)
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"A dependency is unavailable: {ce}")
        except ValueError as ve:
            exec_log.error("Value error during use case execution", error=str(ve), exc_info=True)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Data processing error: {ve}")
        except HTTPException as http_exc: raise http_exc
        except Exception as e:
            exec_log.exception("Unexpected error during use case execution")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"An internal error occurred: {type(e).__name__}")