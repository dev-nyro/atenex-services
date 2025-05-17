# query-service/app/infrastructure/llms/gemini_adapter.py
import google.generativeai as genai
from google.generativeai import types as genai_types
import structlog
from typing import Optional, List, Type, Any, Dict
from pydantic import BaseModel
import json

from app.core.config import settings
from app.application.ports.llm_port import LLMPort
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from app.utils.helpers import truncate_text


log = structlog.get_logger(__name__)


class GeminiAdapter(LLMPort):
    _api_key: str
    _model_name: str
    _model: Optional[genai.GenerativeModel] = None 

    def __init__(self):
        self._api_key = settings.GEMINI_API_KEY.get_secret_value()
        self._model_name = settings.GEMINI_MODEL_NAME
        self._configure_client()

    def _configure_client(self):
        try:
            if self._api_key:
                genai.configure(api_key=self._api_key)
                self._model = genai.GenerativeModel(self._model_name)
                log.info("Gemini client configured successfully using GenerativeModel", model_name=self._model_name)
            else:
                log.warning("Gemini API key is missing. Client not configured.")
        except Exception as e:
            log.error("Failed to configure Gemini client (GenerativeModel)", error=str(e), exc_info=True)
            self._model = None
    
    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES + 1),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=2, max=10),
        retry=retry_if_exception_type((
            TimeoutError,
            # google.api_core.exceptions.GoogleAPIError u otras específicas de la librería si se identifican
        )),
        reraise=True,
        before_sleep=lambda retry_state: log.warning(
            "Retrying Gemini API call",
            attempt=retry_state.attempt_number,
            wait_time=f"{retry_state.next_action.sleep:.2f}s", 
            error_type=type(retry_state.outcome.exception()).__name__ if retry_state.outcome else "N/A", 
            error_message=str(retry_state.outcome.exception()) if retry_state.outcome else "N/A" 
        )
    )
    async def generate(self, prompt: str,
                       response_pydantic_schema: Optional[Type[BaseModel]] = None
                      ) -> str:
        if not self._model:
            log.error("Gemini client (GenerativeModel) not initialized. Cannot generate answer.")
            raise ConnectionError("Gemini client is not properly configured (missing API key or init failed).")

        generate_log = log.bind(
            adapter="GeminiAdapter",
            model_name=self._model_name,
            prompt_length=len(prompt),
            expecting_json=bool(response_pydantic_schema)
        )

        generation_config_parts: Dict[str, Any] = {
            "temperature": 0.6, 
            "top_p": 0.9,
            # "max_output_tokens": settings.MAX_PROMPT_TOKENS // Si se necesita limitar la salida del LLM directamente
        }
        
        if response_pydantic_schema:
            generation_config_parts["response_mime_type"] = "application/json"
            generation_config_parts["response_schema"] = response_pydantic_schema
            generate_log.debug("Configured Gemini for JSON output using response_schema.", schema_name=response_pydantic_schema.__name__)
        
        final_generation_config = genai_types.GenerationConfig(**generation_config_parts)
        
        try:
            # No se pasa 'tools' ni 'tool_config' aquí si solo se usa response_schema para forzar JSON.
            # Si se necesitaran otras function calls, se agregarían a 'tools'.
            call_kwargs: Dict[str, Any] = {"generation_config": final_generation_config}
            
            generate_log.debug("Sending request to Gemini API...")
            response = await self._model.generate_content_async(prompt, **call_kwargs)
            
            generated_text = ""

            if not response.candidates:
                 finish_reason_str = getattr(response.prompt_feedback, 'block_reason', "UNKNOWN_REASON") 
                 safety_ratings_str = str(getattr(response.prompt_feedback, 'safety_ratings', "N/A")) 
                 generate_log.warning("Gemini response potentially blocked (no candidates)",
                                      finish_reason=finish_reason_str, safety_ratings=safety_ratings_str)
                 if response_pydantic_schema:
                     return json.dumps({
                         "error_message": f"Respuesta bloqueada por Gemini (sin candidatos). Razón: {finish_reason_str}",
                         "respuesta_detallada": f"La generación de la respuesta fue bloqueada. Por favor, reformula tu pregunta o contacta a soporte si el problema persiste. Razón: {finish_reason_str}.",
                         "fuentes_citadas": []
                     })
                 return f"[Respuesta bloqueada por Gemini (sin candidatos). Razón: {finish_reason_str}]"

            candidate = response.candidates[0]

            if not candidate.content or not candidate.content.parts:
                finish_reason_cand_str = getattr(candidate, 'finish_reason', "UNKNOWN_REASON")
                safety_ratings_cand_str = str(getattr(candidate, 'safety_ratings', "N/A"))
                generate_log.warning("Gemini response candidate empty or missing parts",
                                     candidate_finish_reason=finish_reason_cand_str,
                                     candidate_safety_ratings=safety_ratings_cand_str)
                if response_pydantic_schema:
                     return json.dumps({
                         "error_message": f"Respuesta vacía de Gemini (candidato sin contenido). Razón: {finish_reason_cand_str}",
                         "respuesta_detallada": f"El asistente no pudo generar una respuesta completa. Razón: {finish_reason_cand_str}.",
                         "fuentes_citadas": []
                     })
                return f"[Respuesta vacía de Gemini (candidato sin contenido). Razón: {finish_reason_cand_str}]"
            
            if response_pydantic_schema:
                # Cuando se usa response_mime_type="application/json" y response_schema,
                # el SDK de Gemini (versiones recientes) debería devolver el texto ya como un string JSON.
                if candidate.content.parts[0].text:
                    generated_text = candidate.content.parts[0].text
                    generate_log.debug("Received potential JSON text from Gemini API.", response_length=len(generated_text))
                    # La validación Pydantic (model_validate_json) se hará en el UseCase
                else:
                    generate_log.error("Expected JSON response, but no text found in the first part of candidate content.")
                    return json.dumps({
                         "error_message": "Respuesta JSON esperada pero no se encontró texto en la respuesta del LLM.",
                         "respuesta_detallada": "Error: El asistente no devolvió una respuesta en el formato JSON esperado.",
                         "fuentes_citadas": []
                    })
            else: 
                generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text') and part.text)
                generate_log.debug("Received plain text response from Gemini API", response_length=len(generated_text))
                
            return generated_text.strip()

        except (genai_types.generation_types.BlockedPromptException, genai_types.generation_types.StopCandidateException) as security_err: 
            finish_reason_err_str = getattr(security_err, 'finish_reason', 'N/A') if hasattr(security_err, 'finish_reason') else 'Unknown security block'
            generate_log.warning("Gemini request blocked or stopped due to safety/policy.",
                                 error_type=type(security_err).__name__,
                                 error_details=str(security_err),
                                 finish_reason=finish_reason_err_str)
            if response_pydantic_schema:
                return json.dumps({
                    "error_message": f"Contenido bloqueado o detenido por Gemini: {type(security_err).__name__}",
                    "respuesta_detallada": f"La generación de la respuesta fue bloqueada o detenida por políticas de contenido. Por favor, ajusta tu consulta. (Razón: {finish_reason_err_str})",
                    "fuentes_citadas": []
                })
            return f"[Contenido bloqueado o detenido por Gemini: {type(security_err).__name__}. Razón: {finish_reason_err_str}]"
        except Exception as e: 
            generate_log.exception("Unhandled error during Gemini API call")
            if response_pydantic_schema: 
                return json.dumps({
                    "error_message": f"Error inesperado en la API de Gemini: {type(e).__name__}",
                    "respuesta_detallada": f"Error interno al comunicarse con el asistente: {type(e).__name__} - {str(e)[:100]}.",
                    "fuentes_citadas": []
                })
            raise ConnectionError(f"Gemini API call failed unexpectedly: {e}") from e

# La función _clean_pydantic_schema_for_gemini_tool se elimina ya que no se usa con response_schema.
# Si se necesitaran 'tools' para otras cosas, se reintroduciría.