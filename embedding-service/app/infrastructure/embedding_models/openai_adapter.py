# embedding-service/app/infrastructure/embedding_models/openai_adapter.py
import structlog
from typing import List, Tuple, Dict, Any, Optional
import asyncio
import time
from openai import AsyncOpenAI, APIConnectionError, RateLimitError, AuthenticationError, OpenAIError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.application.ports.embedding_model_port import EmbeddingModelPort
from app.core.config import settings

log = structlog.get_logger(__name__)

class OpenAIAdapter(EmbeddingModelPort):
    """
    Adapter for OpenAI's Embedding API.
    """
    _client: Optional[AsyncOpenAI] = None
    _model_name: str
    _embedding_dimension: int
    _dimensions_override: Optional[int]
    _model_initialized: bool = False
    _initialization_error: Optional[str] = None

    def __init__(self):
        self._model_name = settings.OPENAI_EMBEDDING_MODEL_NAME
        self._embedding_dimension = settings.EMBEDDING_DIMENSION
        self._dimensions_override = settings.OPENAI_EMBEDDING_DIMENSIONS_OVERRIDE
        # Client initialization is deferred to an async method.
        log.info("OpenAIAdapter initialized", model_name=self._model_name, target_dimension=self._embedding_dimension)

    async def initialize_model(self):
        """
        Initializes the OpenAI client.
        This should be called during service startup (e.g., lifespan).
        """
        if self._model_initialized:
            log.debug("OpenAI client already initialized.", model_name=self._model_name)
            return

        init_log = log.bind(adapter="OpenAIAdapter", action="initialize_model", model_name=self._model_name)
        init_log.info("Initializing OpenAI client...")
        start_time = time.perf_counter()

        if not settings.OPENAI_API_KEY.get_secret_value():
            self._initialization_error = "OpenAI API Key is not configured."
            init_log.critical(self._initialization_error)
            self._model_initialized = False
            raise ConnectionError(self._initialization_error)

        try:
            self._client = AsyncOpenAI(
                api_key=settings.OPENAI_API_KEY.get_secret_value(),
                base_url=settings.OPENAI_API_BASE,
                timeout=settings.OPENAI_TIMEOUT_SECONDS,
                max_retries=0 # We use tenacity for retries in embed_texts
            )

            # Optional: Perform a lightweight test call to verify API key and connectivity
            # For example, listing models (can be slow, consider if truly needed for health)
            # await self._client.models.list(limit=1)

            self._model_initialized = True
            self._initialization_error = None
            duration_ms = (time.perf_counter() - start_time) * 1000
            init_log.info("OpenAI client initialized successfully.", duration_ms=duration_ms)

        except AuthenticationError as e:
            self._initialization_error = f"OpenAI API Authentication Failed: {e}. Check your API key."
            init_log.critical(self._initialization_error, exc_info=False) # exc_info=False for auth errors usually
            self._client = None
            self._model_initialized = False
            raise ConnectionError(self._initialization_error) from e
        except APIConnectionError as e:
            self._initialization_error = f"OpenAI API Connection Error: {e}. Check network or OpenAI status."
            init_log.critical(self._initialization_error, exc_info=True)
            self._client = None
            self._model_initialized = False
            raise ConnectionError(self._initialization_error) from e
        except Exception as e:
            self._initialization_error = f"Failed to initialize OpenAI client for model '{self._model_name}': {str(e)}"
            init_log.critical(self._initialization_error, exc_info=True)
            self._client = None
            self._model_initialized = False
            raise ConnectionError(self._initialization_error) from e

    @retry(
        stop=stop_after_attempt(settings.OPENAI_MAX_RETRIES + 1), # settings.OPENAI_MAX_RETRIES are retries, so +1 for initial attempt
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((APIConnectionError, RateLimitError, OpenAIError)), # Add other retryable OpenAI errors if needed
        before_sleep=lambda retry_state: log.warning(
            "Retrying OpenAI embedding call",
            model_name=settings.OPENAI_EMBEDDING_MODEL_NAME,
            attempt_number=retry_state.attempt_number,
            wait_time=retry_state.next_action.sleep,
            error=str(retry_state.outcome.exception()) if retry_state.outcome else "Unknown error"
        )
    )
    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        if not self._model_initialized or not self._client:
            log.error("OpenAI client not initialized. Cannot generate embeddings.", init_error=self._initialization_error)
            raise ConnectionError("OpenAI embedding model is not available.")

        if not texts:
            return []

        embed_log = log.bind(adapter="OpenAIAdapter", action="embed_texts", num_texts=len(texts), model=self._model_name)
        embed_log.debug("Generating embeddings via OpenAI API...")

        try:
            api_params = {
                "model": self._model_name,
                "input": texts,
                "encoding_format": "float"
            }
            if self._dimensions_override is not None:
                api_params["dimensions"] = self._dimensions_override

            response = await self._client.embeddings.create(**api_params) # type: ignore

            if not response.data or not all(item.embedding for item in response.data):
                embed_log.error("OpenAI API returned no embedding data or empty embeddings.", api_response=response.model_dump_json(indent=2))
                raise ValueError("OpenAI API returned no valid embedding data.")

            # Verify dimensions of the first embedding as a sanity check
            if response.data and response.data[0].embedding:
                actual_dim = len(response.data[0].embedding)
                if actual_dim != self._embedding_dimension:
                    embed_log.warning(
                        "Dimension mismatch in OpenAI response.",
                        expected_dim=self._embedding_dimension,
                        actual_dim=actual_dim,
                        model_used=response.model
                    )
                    # This indicates a potential configuration issue or unexpected API change.
                    # Depending on strictness, could raise an error or just log.
                    # For now, we'll trust the configured EMBEDDING_DIMENSION.

            embeddings_list = [item.embedding for item in response.data]
            embed_log.debug("Embeddings generated successfully via OpenAI.", num_embeddings=len(embeddings_list), usage_tokens=response.usage.total_tokens if response.usage else "N/A")
            return embeddings_list
        except AuthenticationError as e:
            embed_log.error("OpenAI API Authentication Error during embedding", error=str(e))
            raise ConnectionError(f"OpenAI authentication failed: {e}") from e # Propagate as ConnectionError to be caught by endpoint
        except RateLimitError as e:
            embed_log.error("OpenAI API Rate Limit Exceeded during embedding", error=str(e))
            raise OpenAIError(f"OpenAI rate limit exceeded: {e}") from e # Let tenacity handle retry
        except APIConnectionError as e:
            embed_log.error("OpenAI API Connection Error during embedding", error=str(e))
            raise OpenAIError(f"OpenAI connection error: {e}") from e # Let tenacity handle retry
        except OpenAIError as e: # Catch other OpenAI specific errors
            embed_log.error(f"OpenAI API Error during embedding: {type(e).__name__}", error=str(e))
            raise RuntimeError(f"OpenAI API error: {e}") from e
        except Exception as e:
            embed_log.exception("Unexpected error during OpenAI embedding process")
            raise RuntimeError(f"Embedding generation failed with unexpected error: {e}") from e

    def get_model_info(self) -> Dict[str, Any]:
        return {
            "model_name": self._model_name,
            "dimension": self._embedding_dimension, # This is the validated, final dimension
        }

    async def health_check(self) -> Tuple[bool, str]:
        if self._model_initialized and self._client:
            # A more robust health check could involve a lightweight API call,
            # but be mindful of cost and rate limits for frequent health checks.
            # For now, if client is initialized, we assume basic health.
            # A true test is done during initialize_model or first embedding call.
            return True, f"OpenAI client initialized for model {self._model_name}."
        elif self._initialization_error:
            return False, f"OpenAI client initialization failed: {self._initialization_error}"
        else:
            return False, "OpenAI client not initialized."