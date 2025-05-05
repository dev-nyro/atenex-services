# query-service/app/infrastructure/llms/gemini_adapter.py
import google.generativeai as genai
import structlog
from typing import Optional, List # Added List

# LLM_REFACTOR_STEP_2: Update import paths and add Port import
from app.core.config import settings
from app.application.ports.llm_port import LLMPort # Importar el puerto
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

log = structlog.get_logger(__name__)

# LLM_REFACTOR_STEP_2: Implementar el puerto LLMPort
class GeminiAdapter(LLMPort):
    """Adaptador concreto para interactuar con la API de Google Gemini."""

    def __init__(self):
        self._api_key = settings.GEMINI_API_KEY.get_secret_value()
        self._model_name = settings.GEMINI_MODEL_NAME
        self.model: Optional[genai.GenerativeModel] = None
        self._configure_client()

    def _configure_client(self):
        """Configura el cliente de Gemini."""
        try:
            if self._api_key:
                genai.configure(api_key=self._api_key)
                self.model = genai.GenerativeModel(self._model_name)
                log.info("Gemini client configured successfully", model_name=self._model_name)
            else:
                log.warning("Gemini API key is missing. Client not configured.")
        except Exception as e:
            log.error("Failed to configure Gemini client", error=str(e), exc_info=True)
            self.model = None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((
            genai.types.generation_types.StopCandidateException,
            genai.types.generation_types.BlockedPromptException,
            TimeoutError,
            # TODO: Add more specific google.api_core exceptions if needed
        )),
        reraise=True,
        before_sleep=lambda retry_state: log.warning(
            "Retrying Gemini API call",
            attempt=retry_state.attempt_number,
            wait_time=f"{retry_state.next_action.sleep:.2f}s",
            error=str(retry_state.outcome.exception())
        )
    )
    async def generate(self, prompt: str) -> str:
        """Genera una respuesta usando el modelo Gemini configurado."""
        if not self.model:
            log.error("Gemini client not initialized. Cannot generate answer.")
            raise ConnectionError("Gemini client is not properly configured (missing API key or init failed).")

        generate_log = log.bind(adapter="GeminiAdapter", model_name=self._model_name, prompt_length=len(prompt))
        generate_log.debug("Sending request to Gemini API...")

        try:
            response = await self.model.generate_content_async(prompt)

            # Check for blocked response or empty content
            if not response.candidates:
                 # Try to get finish_reason from prompt_feedback if candidates list is empty
                 finish_reason = getattr(response.prompt_feedback, 'block_reason', "UNKNOWN")
                 safety_ratings = getattr(response.prompt_feedback, 'safety_ratings', "N/A")
                 generate_log.warning("Gemini response potentially blocked (no candidates)", finish_reason=str(finish_reason), safety_ratings=str(safety_ratings))
                 return f"[Respuesta bloqueada por Gemini (sin candidatos). Razón: {finish_reason}]"

            candidate = response.candidates[0]
            if not hasattr(candidate, 'content') or not hasattr(candidate.content, 'parts') or not candidate.content.parts:
                 finish_reason = getattr(candidate, 'finish_reason', "UNKNOWN")
                 safety_ratings = getattr(candidate, 'safety_ratings', "N/A")
                 generate_log.warning("Gemini response candidate empty or missing parts", finish_reason=str(finish_reason), safety_ratings=str(safety_ratings))
                 return f"[Respuesta vacía de Gemini. Razón: {finish_reason}]"


            generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text'))

            generate_log.debug("Received response from Gemini API", response_length=len(generated_text))
            return generated_text.strip()

        except (genai.types.generation_types.BlockedPromptException, genai.types.generation_types.StopCandidateException) as security_err:
             # These exceptions might carry more specific info
             finish_reason_err = getattr(security_err, 'finish_reason', 'N/A') # Attempt to get reason if available
             generate_log.warning("Gemini request blocked due to safety settings or prompt content", error=str(security_err), finish_reason=str(finish_reason_err))
             return f"[Contenido bloqueado por Gemini: {type(security_err).__name__}]"
        except Exception as e:
            # Log other potential API errors or library issues
            generate_log.exception("Error during Gemini API call")
            raise ConnectionError(f"Gemini API call failed: {e}") from e

# LLM_REFACTOR_STEP_2: No longer need global instance or getter here.
# Instantiation and injection will be handled by the application setup (e.g., in main.py or dependency injector).