# query-service/app/infrastructure/llms/gemini_adapter.py
import google.generativeai as genai
import structlog
from typing import Optional, List, Type, Any # Import Type for Pydantic model
from pydantic import BaseModel # For schema validation
import json # For robust JSON parsing if needed

from app.core.config import settings
from app.application.ports.llm_port import LLMPort 
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
# Import Pydantic model for structured response to pass as schema
from app.domain.models import RespuestaEstructurada


log = structlog.get_logger(__name__)

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
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES +1), # Tenacity counts first attempt as one
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=2, max=10),
        retry=retry_if_exception_type((
            genai.types.generation_types.StopCandidateException,
            genai.types.generation_types.BlockedPromptException,
            TimeoutError,
            # Considerar añadir google.api_core.exceptions.DeadlineExceeded, google.api_core.exceptions.ServiceUnavailable
        )),
        reraise=True,
        before_sleep=lambda retry_state: log.warning(
            "Retrying Gemini API call",
            attempt=retry_state.attempt_number,
            wait_time=f"{retry_state.next_action.sleep:.2f}s",
            error_type=type(retry_state.outcome.exception()).__name__,
            error_message=str(retry_state.outcome.exception())
        )
    )
    async def generate(self, prompt: str, 
                       response_pydantic_schema: Optional[Type[BaseModel]] = None
                      ) -> str: # Return value is always string (JSON string if schema provided)
        """
        Genera una respuesta usando el modelo Gemini configurado.
        Si response_pydantic_schema se proporciona, solicita una salida JSON.
        """
        if not self.model:
            log.error("Gemini client not initialized. Cannot generate answer.")
            raise ConnectionError("Gemini client is not properly configured (missing API key or init failed).")

        generate_log = log.bind(
            adapter="GeminiAdapter", 
            model_name=self._model_name, 
            prompt_length=len(prompt),
            expecting_json=bool(response_pydantic_schema)
        )
        generate_log.debug("Sending request to Gemini API...")

        generation_config_dict: Dict[str, Any] = {
            # Default max_output_tokens for Gemini Flash 2.5 is 65536, which is generous.
            # We can override it here if needed, but often default is fine.
            # "max_output_tokens": 8192, # Example if we need to limit
            "temperature": 0.6, # Ajustar según necesidad, 0.2-0.7 es común para RAG
            "top_p": 0.9,
            # "top_k": (opcional, no suele usarse con top_p)
        }
        
        if response_pydantic_schema:
            generation_config_dict["response_mime_type"] = "application/json"
            # El SDK de google-generativeai puede tomar directamente la clase Pydantic como schema.
            generation_config_dict["response_schema"] = response_pydantic_schema
        
        final_generation_config = genai.types.GenerationConfig(**generation_config_dict)

        try:
            response = await self.model.generate_content_async(
                prompt,
                generation_config=final_generation_config
            )
            
            generated_text = "" # Inicializar

            if not response.candidates:
                 finish_reason = getattr(response.prompt_feedback, 'block_reason', "UNKNOWN")
                 safety_ratings_str = str(getattr(response.prompt_feedback, 'safety_ratings', "N/A"))
                 generate_log.warning("Gemini response potentially blocked (no candidates)", 
                                      finish_reason=str(finish_reason), safety_ratings=safety_ratings_str)
                 # Devolver un JSON de error si se esperaba JSON
                 if response_pydantic_schema:
                     return json.dumps({
                         "error_message": f"Respuesta bloqueada por Gemini (sin candidatos). Razón: {finish_reason}",
                         "respuesta_detallada": f"Respuesta bloqueada por Gemini (sin candidatos). Razón: {finish_reason}",
                         "fuentes_citadas": []
                     })
                 return f"[Respuesta bloqueada por Gemini (sin candidatos). Razón: {finish_reason}]"

            candidate = response.candidates[0]
            if not hasattr(candidate, 'content') or not hasattr(candidate.content, 'parts') or not candidate.content.parts:
                 finish_reason = getattr(candidate, 'finish_reason', "UNKNOWN")
                 safety_ratings_str = str(getattr(candidate, 'safety_ratings', "N/A"))
                 generate_log.warning("Gemini response candidate empty or missing parts", 
                                      finish_reason=str(finish_reason), safety_ratings=str(safety_ratings_str))
                 if response_pydantic_schema:
                     return json.dumps({
                         "error_message": f"Respuesta vacía de Gemini. Razón: {finish_reason}",
                         "respuesta_detallada": f"Respuesta vacía de Gemini. Razón: {finish_reason}",
                         "fuentes_citadas": []
                     })
                 return f"[Respuesta vacía de Gemini. Razón: {finish_reason}]"

            # response.text debería devolver el string JSON si se configuró response_mime_type
            # Si no, .parts[0].text
            if response_pydantic_schema and response.text:
                 generated_text = response.text
            else: # Fallback si .text no está o no es JSON
                 generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text'))


            if response_pydantic_schema:
                generate_log.debug("Received JSON-like string from Gemini API", response_length=len(generated_text))
                try:
                    # Validar que sea un JSON parseable, aunque la validación del schema Pydantic se hará en el UseCase.
                    json.loads(generated_text) 
                except json.JSONDecodeError as json_err:
                    generate_log.error("Gemini returned invalid JSON string", raw_response=truncate_text(generated_text, 500), error=str(json_err))
                    # Devolver un error JSON formateado
                    return json.dumps({
                         "error_message": f"LLM returned malformed JSON: {str(json_err)}",
                         "respuesta_detallada": f"Error: La respuesta del asistente no pudo ser procesada (JSON malformado). Respuesta recibida: {truncate_text(generated_text, 200)}",
                         "fuentes_citadas": []
                    })
            else:
                 generate_log.debug("Received text response from Gemini API", response_length=len(generated_text))

            return generated_text.strip()

        except (genai.types.generation_types.BlockedPromptException, genai.types.generation_types.StopCandidateException) as security_err:
             finish_reason_err = getattr(security_err, 'finish_reason', 'N/A') 
             generate_log.warning("Gemini request blocked due to safety settings or prompt content", error=str(security_err), finish_reason=str(finish_reason_err))
             if response_pydantic_schema:
                 return json.dumps({
                     "error_message": f"Contenido bloqueado por Gemini: {type(security_err).__name__}",
                     "respuesta_detallada": f"Contenido bloqueado por Gemini: {type(security_err).__name__}",
                     "fuentes_citadas": []
                 })
             return f"[Contenido bloqueado por Gemini: {type(security_err).__name__}]"
        except Exception as e:
            generate_log.exception("Error during Gemini API call")
            raise ConnectionError(f"Gemini API call failed: {e}") from e