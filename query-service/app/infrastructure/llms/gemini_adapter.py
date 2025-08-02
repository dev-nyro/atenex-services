# query-service/app/infrastructure/llms/gemini_adapter.py
import google.generativeai as genai
from google.generativeai import types as genai_types
from google.api_core import exceptions as google_api_exceptions 

import structlog
from typing import Optional, List, Type, Any, Dict, AsyncGenerator
from pydantic import BaseModel
import json
import logging # Para before_sleep_log

from app.core.config import settings
from app.application.ports.llm_port import LLMPort
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, before_sleep_log
from app.utils.helpers import truncate_text


log = structlog.get_logger(__name__)

def _clean_pydantic_schema_for_gemini_response(pydantic_schema: Dict[str, Any]) -> Dict[str, Any]:
    definitions = pydantic_schema.get("$defs", {})

    schema_copy = {
        k: v
        for k, v in pydantic_schema.items()
        if k not in {"$defs", "title", "description", "$schema"} 
    }

    def resolve_ref(ref_path: str) -> Dict[str, Any]:
        if not ref_path.startswith("#/$defs/"):
            log.warning("Encountered non-internal JSON schema reference, falling back to OBJECT.", ref_path=ref_path)
            return {"type": "OBJECT"} 
        
        def_key = ref_path.split("/")[-1]
        if def_key in definitions:
            return _transform_node(definitions[def_key])
        else:
            log.warning(f"Broken JSON schema reference found and could not be resolved: {ref_path}")
            return {"type": "OBJECT"} 

    def _transform_node(node: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(node, dict):
            return node

        transformed_node = {}
        for key, value in node.items():
            if key in {"default", "examples", "example", "const", "title", "description"}:
                continue
            
            if key == "$ref" and isinstance(value, str):
                 return resolve_ref(value)

            elif key == "anyOf" and isinstance(value, list):
                is_optional_pattern = False
                if len(value) == 2:
                    type_def_item = next((item for item in value if isinstance(item, dict) and item.get("type") != "null"), None)
                    null_def_item = next((item for item in value if isinstance(item, dict) and item.get("type") == "null"), None)
                    
                    if type_def_item and null_def_item:
                        is_optional_pattern = True
                        transformed_type_def = _transform_node(type_def_item)
                        for k_type, v_type in transformed_type_def.items():
                             transformed_node[k_type] = v_type
                        transformed_node["nullable"] = True 
                
                if not is_optional_pattern: 
                    if value:
                        first_type = _transform_node(value[0])
                        for k_first, v_first in first_type.items():
                            transformed_node[k_first] = v_first
                        log.warning("Complex 'anyOf' in Pydantic schema for Gemini response_schema, took first option.",
                                    original_anyof=value, chosen_type=first_type)
                    else:
                        log.warning("Empty 'anyOf' in Pydantic schema for Gemini response_schema.", original_anyof=value)
                continue 

            elif isinstance(value, dict):
                transformed_node[key] = _transform_node(value)
            elif isinstance(value, list) and key not in ["enum", "required"]: 
                transformed_node[key] = [_transform_node(item) if isinstance(item, dict) else item for item in value]
            else:
                transformed_node[key] = value
        
        if "type" in transformed_node:
            json_type = transformed_node["type"]
            if isinstance(json_type, list): 
                if "null" in json_type:
                    transformed_node["nullable"] = True 
                actual_type = next((t for t in json_type if t != "null"), "OBJECT") 
                if isinstance(actual_type, str):
                    transformed_node["type"] = actual_type.upper()
                else: 
                    transformed_node["type"] = _transform_node(actual_type).get("type", "OBJECT")

            elif isinstance(json_type, str):
                transformed_node["type"] = json_type.upper()
            
            if transformed_node["type"] == "LIST": 
                transformed_node["type"] = "ARRAY"

        if transformed_node.get("type") == "ARRAY" and "items" not in transformed_node:
            log.warning("Schema for ARRAY type missing 'items' definition for Gemini. Adding generic object item.", node_details=transformed_node)
            transformed_node["items"] = {"type": "OBJECT"} 

        return transformed_node

    final_schema = _transform_node(schema_copy)
    
    log.debug("Cleaned Pydantic JSON Schema for Gemini response_schema", original_schema_preview=str(pydantic_schema)[:200], cleaned_schema_preview=str(final_schema)[:200])
    return final_schema


class GeminiAdapter(LLMPort):
    _api_key: str
    _model_name: str
    _model: Optional[genai.GenerativeModel] = None 
    _safety_settings: List[Dict[str, str]]

    def __init__(self):
        self._api_key = settings.GEMINI_API_KEY.get_secret_value()
        self._model_name = settings.GEMINI_MODEL_NAME
        # Configuraciones de seguridad más permisivas para evitar bloqueos
        self._safety_settings = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_ONLY_HIGH"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_ONLY_HIGH"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_ONLY_HIGH"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_ONLY_HIGH"},
        ]
        self._configure_client()

    def _configure_client(self):
        try:
            if self._api_key:
                genai.configure(api_key=self._api_key)
                self._model = genai.GenerativeModel(
                    self._model_name,
                    safety_settings=self._safety_settings
                )
                log.info("Gemini client configured successfully using GenerativeModel",
                         model_name=self._model_name,
                         safety_settings=self._safety_settings)
            else:
                log.warning("Gemini API key is missing. Client not configured.")
        except Exception as e:
            log.error("Failed to configure Gemini client (GenerativeModel)", error=str(e), exc_info=True)
            self._model = None
    
    def _create_error_json_response(self, error_message: str, detailed_message: str) -> str:
        """Helper para crear un JSON de error estructurado."""
        return json.dumps({
            "error_message": error_message,
            "respuesta_detallada": detailed_message,
            "fuentes_citadas": [],
            "resumen_ejecutivo": None,
            "siguiente_pregunta_sugerida": None
        })

    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES + 1),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=2, max=10),
        retry=retry_if_exception_type((
            TimeoutError,
        )),
        reraise=True,
        before_sleep=before_sleep_log(log, logging.WARNING) 
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
            "max_output_tokens": settings.GEMINI_MAX_OUTPUT_TOKENS,
        }
        if self._max_output_tokens:
            generation_config_parts["max_output_tokens"] = self._max_output_tokens
        
        if response_pydantic_schema:
            generation_config_parts["response_mime_type"] = "application/json"
            
            pydantic_schema_json = response_pydantic_schema.model_json_schema()
            cleaned_schema_for_gemini = _clean_pydantic_schema_for_gemini_response(pydantic_schema_json)
            generation_config_parts["response_schema"] = cleaned_schema_for_gemini
            generate_log.debug("Configured Gemini for JSON output using cleaned response_schema.", 
                               schema_name=response_pydantic_schema.__name__,
                               max_output_tokens=self._max_output_tokens)
        
        final_generation_config = genai_types.GenerationConfig(**generation_config_parts)
        
        try:
            call_kwargs: Dict[str, Any] = {"generation_config": final_generation_config}
            
            prompt_size_for_log = len(prompt)
            if prompt_size_for_log > 500: 
                prompt_preview = truncate_text(prompt, 500)
                generate_log.debug("Sending request to Gemini API...", prompt_preview=prompt_preview, prompt_total_length=prompt_size_for_log)
            else:
                generate_log.debug("Sending request to Gemini API...", prompt_text=prompt)


            response = await self._model.generate_content_async(prompt, **call_kwargs)
            
            generate_log.info("Gemini API response received.", 
                              num_candidates=len(response.candidates), 
                              prompt_feedback_block_reason=getattr(response.prompt_feedback, 'block_reason', 'N/A'),
                              usage_prompt_tokens=getattr(response.usage_metadata, 'prompt_token_count', 'N/A'),
                              usage_candidates_tokens=getattr(response.usage_metadata, 'candidates_token_count', 'N/A'),
                              usage_total_tokens=getattr(response.usage_metadata, 'total_token_count', 'N/A')
            )
            generated_text = ""

            try:
                usage_metadata = response.usage_metadata if hasattr(response, 'usage_metadata') else None
                prompt_token_count = getattr(usage_metadata, 'prompt_token_count', 'N/A')
                candidates_token_count = getattr(usage_metadata, 'candidates_token_count', 'N/A')
                total_token_count = getattr(usage_metadata, 'total_token_count', 'N/A')
                
                generate_log.info("Gemini API response received.", 
                                  num_candidates=len(response.candidates) if response.candidates else 0,
                                  prompt_feedback_block_reason=str(getattr(response.prompt_feedback, 'block_reason', 'N/A')),
                                  usage_prompt_tokens=prompt_token_count,
                                  usage_candidates_tokens=candidates_token_count,
                                  usage_total_tokens=total_token_count)
            except Exception as e_log_resp:
                generate_log.warning("Could not fully log Gemini response details", error_logging_response=str(e_log_resp))


            if not response.candidates:
                 finish_reason_str = getattr(response.prompt_feedback, 'block_reason', "UNKNOWN_REASON").name if hasattr(getattr(response.prompt_feedback, 'block_reason', None) , 'name') else str(getattr(response.prompt_feedback, 'block_reason', "UNKNOWN_REASON"))
                 safety_ratings_str = str(getattr(response.prompt_feedback, 'safety_ratings', "N/A")) 
                 generate_log.warning("Gemini response potentially blocked (no candidates)",
                                      finish_reason=finish_reason_str, safety_ratings=safety_ratings_str)
                 if response_pydantic_schema:
                     return self._create_error_json_response(
                         error_message=f"Respuesta bloqueada por Gemini (sin candidatos). Razón: {finish_reason_str}",
                         detailed_message=f"La generación de la respuesta fue bloqueada. Por favor, reformula tu pregunta o contacta a soporte si el problema persiste. Razón: {finish_reason_str}."
                     )
                 return f"[Respuesta bloqueada por Gemini (sin candidatos). Razón: {finish_reason_str}]"

            candidate = response.candidates[0]
            generate_log.info("Gemini candidate details",
                 finish_reason=str(candidate.finish_reason), safety_ratings=str(candidate.safety_ratings))


            if candidate.finish_reason.name == "MAX_TOKENS":
                generate_log.warning(f"Gemini response TRUNCATED due to max_output_tokens ({settings.GEMINI_MAX_OUTPUT_TOKENS}). This can lead to malformed JSON or incomplete answers.")


            if not candidate.content or not candidate.content.parts:
                generate_log.warning("Gemini response candidate empty or missing parts",
                                     candidate_details=str(candidate))
                if response_pydantic_schema:
                     return self._create_error_json_response(
                         error_message=f"Respuesta vacía de Gemini (candidato sin contenido). Razón: {candidate_finish_reason}",
                         detailed_message=f"El asistente no pudo generar una respuesta completa. Razón: {candidate_finish_reason}."
                     )
                return f"[Respuesta vacía de Gemini (candidato sin contenido). Razón: {candidate_finish_reason}]"
            
            if candidate.content.parts[0].text:
                generated_text = candidate.content.parts[0].text
            else:
                generate_log.error("Gemini response part exists but has no text content.")
                if response_pydantic_schema:
                    return self._create_error_json_response(
                        error_message="Respuesta del LLM incompleta o en formato inesperado (sin texto).",
                        detailed_message="Error: El asistente devolvió una respuesta sin contenido textual."
                    )
                return "[Respuesta del LLM incompleta o sin contenido textual]"

            if response_pydantic_schema:
                generate_log.debug("Received potential JSON text from Gemini API.", response_length=len(generated_text))
            else: 
                generate_log.debug("Received plain text response from Gemini API", response_length=len(generated_text))
                
            return generated_text.strip()

        except (genai_types.generation_types.BlockedPromptException, genai_types.generation_types.StopCandidateException) as security_err: 
            finish_reason_err_str = getattr(security_err, 'finish_reason', 'N/A') if hasattr(security_err, 'finish_reason') else 'Unknown security block'
            generate_log.warning("Gemini request blocked or stopped due to safety/policy.",
                                 error_type=type(security_err).__name__,
                                 error_details=str(security_err),
                                 finish_reason=finish_reason_err_str)
            if response_pydantic_schema:
                return self._create_error_json_response(
                    error_message=f"Contenido bloqueado o detenido por Gemini: {type(security_err).__name__}",
                    detailed_message=f"La generación de la respuesta fue bloqueada o detenida por políticas de contenido. Por favor, ajusta tu consulta. (Razón: {finish_reason_err_str})"
                )
            return f"[Contenido bloqueado o detenido por Gemini: {type(security_err).__name__}. Razón: {finish_reason_err_str}]"
        
        except google_api_exceptions.GoogleAPICallError as api_call_err:
            # Captura errores más genéricos de la API de Google, que pueden incluir errores HTTP no capturados por las más específicas.
            generate_log.error("Gemini API call failed with GoogleAPICallError", error_details=str(api_call_err), exc_info=True)
            raise ConnectionError(f"Gemini API call error: {api_call_err}") from api_call_err
        except google_api_exceptions.InvalidArgument as invalid_arg_err:
            prompt_preview_for_error = truncate_text(prompt, 200)
            generate_log.error("Gemini API call failed due to invalid argument. This could be due to the prompt or JSON schema if provided.",
                               error_details=str(invalid_arg_err), 
                               prompt_preview=prompt_preview_for_error,
                               json_schema_expected=response_pydantic_schema.__name__ if response_pydantic_schema else "None",
                               exc_info=True)
            raise ValueError(f"Gemini API InvalidArgument: {invalid_arg_err}") from invalid_arg_err
        except Exception as e: 
            generate_log.exception("Unhandled error during Gemini API call")
            if response_pydantic_schema: 
                return self._create_error_json_response(
                    error_message=f"Error inesperado en la API de Gemini: {type(e).__name__}",
                    detailed_message=f"Error interno al comunicarse con el asistente: {type(e).__name__} - {truncate_text(str(e),100)}."
                )
            raise ConnectionError(f"Gemini API call failed unexpectedly: {e}") from e

    async def generate_stream(self, prompt: str) -> AsyncGenerator[str, None]:
        if not self._model:
            log.error("Gemini client (GenerativeModel) not initialized for streaming.")
            raise ConnectionError("Gemini client is not properly configured for streaming.")

        stream_log = log.bind(
            adapter="GeminiAdapter",
            action="generate_stream",
            model_name=self._model_name,
            prompt_length=len(prompt)
        )
        
        generation_config_parts: Dict[str, Any] = {
            "temperature": 0.6,
            "top_p": 0.9,
        }
        if self._max_output_tokens:
            generation_config_parts["max_output_tokens"] = self._max_output_tokens
            
        final_generation_config = genai_types.GenerationConfig(**generation_config_parts)

        try:
            stream_log.debug("Sending stream request to Gemini API...")
            
            response_stream = await self._model.generate_content_async(prompt, stream=True, generation_config=final_generation_config)
            
            async for chunk in response_stream:
                if chunk.text:
                    yield chunk.text
                if chunk.prompt_feedback and chunk.prompt_feedback.block_reason:
                    stream_log.warning("Stream chunk indicated prompt blocking", reason=chunk.prompt_feedback.block_reason)
            
            stream_log.debug("Streaming finished.")

        except (genai_types.generation_types.BlockedPromptException, genai_types.generation_types.StopCandidateException) as security_err:
            finish_reason_err_str = getattr(security_err, 'finish_reason', 'N/A') if hasattr(security_err, 'finish_reason') else 'Unknown security block'
            stream_log.warning("Gemini stream blocked or stopped.", error_type=type(security_err).__name__, reason=finish_reason_err_str)
            yield f"[STREAM ERROR: Contenido bloqueado por Gemini. Razón: {finish_reason_err_str}]"
        except google_api_exceptions.GoogleAPICallError as e: 
            stream_log.error("Gemini API stream call failed with GoogleAPICallError", error_details=str(e), exc_info=True)
            yield f"[STREAM ERROR: Error de API de Gemini - {type(e).__name__}]"
        except Exception as e:
            stream_log.exception("Error during Gemini API stream")
            yield f"[STREAM ERROR: {type(e).__name__} - {str(e)}]"