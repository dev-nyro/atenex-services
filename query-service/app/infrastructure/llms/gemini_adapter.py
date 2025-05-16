# query-service/app/infrastructure/llms/gemini_adapter.py
# LLM_CORRECTION_START: Correct imports for google-genai SDK
from google import genai
from google.genai import types as genai_types
# LLM_CORRECTION_END
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
    _model: Optional[genai.GenerativeModel] = None # LLM_CORRECTION: Use GenerativeModel from the new SDK

    def __init__(self):
        self._api_key = settings.GEMINI_API_KEY.get_secret_value()
        self._model_name = settings.GEMINI_MODEL_NAME
        self._configure_client()

    def _configure_client(self):
        try:
            if self._api_key:
                genai.configure(api_key=self._api_key)
                # For google-genai SDK, GenerativeModel is the way to interact with specific models
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
            genai_types.generation_types.StopCandidateException, 
            genai_types.generation_types.BlockedPromptException, 
            TimeoutError, 
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
        if not self._model: # LLM_CORRECTION: Check self._model
            log.error("Gemini client (GenerativeModel) not initialized. Cannot generate answer.")
            raise ConnectionError("Gemini client is not properly configured (missing API key or init failed).")

        generate_log = log.bind(
            adapter="GeminiAdapter",
            model_name=self._model_name,
            prompt_length=len(prompt),
            expecting_json=bool(response_pydantic_schema)
        )

        generation_config_dict: Dict[str, Any] = {
            "temperature": 0.6,
            "top_p": 0.9,
        }
        tools_config: Optional[List[genai_types.Tool]] = None 

        if response_pydantic_schema:
            pydantic_schema_dict = response_pydantic_schema.model_json_schema()
            schema_params = _clean_pydantic_schema_for_gemini(pydantic_schema_dict)
            generate_log.debug("Cleaned JSON Schema for Gemini Function Calling (Tool)", schema_final=schema_params)

            fn_decl = genai_types.FunctionDeclaration( 
                name="format_structured_response",
                description=f"Formats the response according to the {response_pydantic_schema.__name__} schema.",
                parameters=schema_params
            )
            tools_config = [genai_types.Tool(function_declarations=[fn_decl])] 
            
            generation_config_dict["tool_config"] = genai_types.ToolConfig( 
                function_calling_config=genai_types.FunctionCallingConfig( 
                    mode=genai_types.FunctionCallingConfig.Mode.ANY, 
                    allowed_function_names=["format_structured_response"]
                )
            )
        
        final_generation_config = genai_types.GenerationConfig(**generation_config_dict) 
        
        try:
            call_kwargs: Dict[str, Any] = {"generation_config": final_generation_config}
            if tools_config:
                call_kwargs["tools"] = tools_config
            
            # LLM_CORRECTION: Use self._model to call generate_content_async
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
            
            function_call_part_found = False # LLM_CORRECTION: Flag to track if function call was processed
            if response_pydantic_schema: 
                function_call_part = next((part for part in candidate.content.parts if part.function_call), None)
                if function_call_part:
                    function_call_part_found = True # LLM_CORRECTION
                    function_call = function_call_part.function_call
                    if function_call.name == "format_structured_response":
                        args_dict = {key: value for key, value in function_call.args.items()}
                        generated_text = json.dumps(args_dict) 
                        generate_log.debug("Received JSON via function call from Gemini API", response_length=len(generated_text))
                    else: 
                        generate_log.warning("Gemini called an unexpected function.",
                                             called_function_name=function_call.name,
                                             expected_function_name="format_structured_response")
                        generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text') and part.text) 
                        if not generated_text.strip(): 
                            return json.dumps({
                                "error_message": f"LLM called unexpected function '{function_call.name}' and returned no usable text.",
                                "respuesta_detallada": f"Error: El asistente devolvió un formato inesperado (función: {function_call.name}).",
                                "fuentes_citadas": []
                            })
                else: 
                    generate_log.warning("Expected JSON via function call, but no function_call part found. Checking for plain text that might be JSON.")
                    generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text') and part.text)
                    if not generated_text.strip(): 
                         return json.dumps({
                             "error_message": "LLM did not make a function call and returned no text when JSON was expected.",
                             "respuesta_detallada": "Error: El asistente no pudo estructurar la respuesta correctamente y no proporcionó texto.",
                             "fuentes_citadas": []
                         })
            
            if not function_call_part_found and not response_pydantic_schema: # LLM_CORRECTION: Plain text expected and no function call was made
                generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text') and part.text)
                generate_log.debug("Received text response from Gemini API", response_length=len(generated_text))
            elif not function_call_part_found and response_pydantic_schema and not generated_text: # LLM_CORRECTION: JSON expected, no function call, and no text parsed yet
                # This case implies that a function call was expected but didn't happen, and generated_text is still empty.
                # The previous logic for this path already set generated_text from parts if no function_call_part.
                # This is a safety net or re-evaluation. If generated_text is truly empty here, it means no usable output.
                generate_log.warning("JSON expected, no function call, and no usable text found in parts.")
                return json.dumps({
                    "error_message": "LLM did not make a function call and returned no usable text when JSON was expected.",
                    "respuesta_detallada": "Error: El asistente no pudo estructurar la respuesta correctamente y no proporcionó texto procesable.",
                    "fuentes_citadas": []
                })


            if response_pydantic_schema:
                try:
                    json.loads(generated_text) 
                except json.JSONDecodeError as json_err:
                    generate_log.error("Gemini response content is not valid JSON despite expectation.",
                                       raw_response_preview=truncate_text(generated_text, 500),
                                       error_message=str(json_err),
                                       was_function_call=function_call_part_found) # LLM_CORRECTION: Use flag
                    return json.dumps({
                         "error_message": f"LLM returned malformed JSON: {str(json_err)}",
                         "respuesta_detallada": f"Error: La respuesta del asistente no pudo ser procesada (JSON malformado). Respuesta recibida: {truncate_text(generated_text, 200)}",
                         "fuentes_citadas": []
                    })

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


# Helper function (kept from original code, as it's used by the adapter logic above)
def _clean_pydantic_schema_for_gemini(pydantic_schema: Dict[str, Any]) -> Dict[str, Any]:
    definitions = pydantic_schema.get("$defs", {})
    schema_copy = {
        k: v
        for k, v in pydantic_schema.items()
        if k not in {"$defs", "title", "description", "$schema"}
    }

    def resolve_ref(ref_path: str) -> Dict[str, Any]:
        if not ref_path.startswith("#/$defs/"):
            return {"$ref": ref_path}
        def_key = ref_path.split("/")[-1]
        if def_key in definitions:
            return transform_node(definitions[def_key])
        else:
            return {"$ref": ref_path}

    def transform_node(node: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(node, dict): return node
        transformed_node = {}
        for key, value in node.items():
            if key in {"default", "examples", "example", "const", "title", "description"}: continue
            if key == "$ref" and isinstance(value, str): return resolve_ref(value)
            elif key == "anyOf" and isinstance(value, list):
                is_optional_pattern = False
                if len(value) == 2:
                    type_def_item = next((item for item in value if isinstance(item, dict) and item.get("type") != "null"), None)
                    null_def_item = next((item for item in value if isinstance(item, dict) and item.get("type") == "null"), None)
                    if type_def_item and null_def_item:
                        is_optional_pattern = True
                        for k_type, v_type in type_def_item.items():
                            transformed_node[k_type] = transform_node(v_type) if isinstance(v_type, dict) else v_type
                        transformed_node["nullable"] = True
                if not is_optional_pattern: transformed_node[key] = [transform_node(item) for item in value]
                continue
            elif isinstance(value, dict): transformed_node[key] = transform_node(value)
            elif isinstance(value, list) and key not in ["enum", "required"]:
                transformed_node[key] = [transform_node(item) if isinstance(item, dict) else item for item in value]
            else: transformed_node[key] = value
        if "type" in transformed_node:
            json_type = transformed_node["type"]
            if isinstance(json_type, list):
                if "null" in json_type: transformed_node["nullable"] = True
                actual_type = next((t for t in json_type if t != "null"), None)
                if actual_type and isinstance(actual_type, str): transformed_node["type"] = actual_type.upper()
                elif actual_type: transformed_node["type"] = transform_node(actual_type) 
            elif isinstance(json_type, str): transformed_node["type"] = json_type.upper()
        return transformed_node

    final_schema = transform_node(schema_copy)
    if "title" in final_schema: del final_schema["title"]
    if "description" in final_schema: del final_schema["description"]
    return final_schema