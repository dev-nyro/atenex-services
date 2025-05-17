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

def _clean_pydantic_schema_for_gemini_response(pydantic_schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Limpia un esquema JSON generado por Pydantic para que sea compatible con
    el parámetro `response_schema` de Gemini `GenerationConfig`.
    Principalmente, elimina campos "default" y ajusta tipos.
    """
    
    # Referencias internas que Gemini podría no entender bien, aunque para response_schema
    # Pydantic suele aplanar bastante bien.
    definitions = pydantic_schema.get("$defs", {})

    schema_copy = {
        k: v
        for k, v in pydantic_schema.items()
        # Quitar $defs y title/description del nivel raíz, ya que response_schema es el tipo mismo
        if k not in {"$defs", "title", "description", "$schema"} 
    }

    def resolve_ref(ref_path: str) -> Dict[str, Any]:
        if not ref_path.startswith("#/$defs/"):
            return {"type": "OBJECT"} # Fallback si es una referencia externa no esperada
        
        def_key = ref_path.split("/")[-1]
        if def_key in definitions:
            return _transform_node(definitions[def_key])
        else:
            log.warning(f"Broken JSON schema reference found and could not be resolved: {ref_path}")
            return {"type": "OBJECT"} # Fallback

    def _transform_node(node: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(node, dict):
            return node

        transformed_node = {}
        for key, value in node.items():
            # Eliminar campos problemáticos como 'default' o que no son parte del schema de OpenAPI que Gemini usa
            if key in {"default", "examples", "example", "const", "title", "description"}:
                continue
            
            if key == "$ref" and isinstance(value, str):
                 # Si es una referencia, resolverla y usar el resultado transformado.
                 # El esquema de respuesta para Gemini no usa $ref, espera el objeto anidado.
                 return resolve_ref(value)

            elif key == "anyOf" and isinstance(value, list):
                # Simplificar anyOf para opcionales: anyOf: [ <type>, {"type": "null"} ] a <type> con nullable: true
                is_optional_pattern = False
                if len(value) == 2:
                    type_def_item = next((item for item in value if isinstance(item, dict) and item.get("type") != "null"), None)
                    null_def_item = next((item for item in value if isinstance(item, dict) and item.get("type") == "null"), None)
                    
                    if type_def_item and null_def_item:
                        is_optional_pattern = True
                        # Tomar el tipo no nulo y transformarlo
                        transformed_type_def = _transform_node(type_def_item)
                        # Copiar las propiedades del tipo real al nodo actual
                        for k_type, v_type in transformed_type_def.items():
                             transformed_node[k_type] = v_type
                        transformed_node["nullable"] = True 
                
                if not is_optional_pattern: 
                    # Si no es el patrón opcional, es más complejo y podría no ser soportado directamente.
                    # Por simplicidad, podríamos tomar el primer tipo o loguear un aviso.
                    # Para response_schema, es mejor que el Pydantic model sea simple.
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
        
        # Convertir 'type' a MAYÚSCULAS según la especificación OpenAPI que usa Gemini
        if "type" in transformed_node:
            json_type = transformed_node["type"]
            if isinstance(json_type, list): # type: ["string", "null"]
                if "null" in json_type:
                    transformed_node["nullable"] = True 
                actual_type = next((t for t in json_type if t != "null"), "OBJECT") # Default to OBJECT if only null
                if isinstance(actual_type, str):
                    transformed_node["type"] = actual_type.upper()
                else: # Podría ser un sub-esquema (raro para listas de tipos)
                    transformed_node["type"] = _transform_node(actual_type).get("type", "OBJECT")

            elif isinstance(json_type, str):
                transformed_node["type"] = json_type.upper()
            
            # Gemini espera que el tipo para 'array' sea 'ARRAY', no 'LIST' (si Pydantic lo genera así)
            if transformed_node["type"] == "LIST": # Pydantic puede usar 'list'
                transformed_node["type"] = "ARRAY"


        # Asegurar que `items` exista para tipo ARRAY
        if transformed_node.get("type") == "ARRAY" and "items" not in transformed_node:
            log.warning("Schema for ARRAY type missing 'items' definition for Gemini. Adding generic object item.", node_details=transformed_node)
            transformed_node["items"] = {"type": "OBJECT"} # Fallback genérico

        return transformed_node

    # Aplicar la transformación al schema raíz
    final_schema = _transform_node(schema_copy)
    
    log.debug("Cleaned Pydantic JSON Schema for Gemini response_schema", original_schema=pydantic_schema, cleaned_schema=final_schema)
    return final_schema


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
            # Se pueden agregar aquí errores específicos de google.api_core.exceptions si se identifican
            # como errores de red o servicio transitorios que justifiquen un reintento.
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
        }
        
        if response_pydantic_schema:
            generation_config_parts["response_mime_type"] = "application/json"
            
            # Obtener el esquema JSON del modelo Pydantic
            pydantic_schema_json = response_pydantic_schema.model_json_schema()
            # Limpiar el esquema para Gemini
            cleaned_schema_for_gemini = _clean_pydantic_schema_for_gemini_response(pydantic_schema_json)
            
            generation_config_parts["response_schema"] = cleaned_schema_for_gemini
            generate_log.debug("Configured Gemini for JSON output using cleaned response_schema.", 
                               schema_name=response_pydantic_schema.__name__,
                               cleaned_schema=cleaned_schema_for_gemini)
        
        final_generation_config = genai_types.GenerationConfig(**generation_config_parts)
        
        try:
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
            
            if candidate.content.parts[0].text:
                generated_text = candidate.content.parts[0].text
            else:
                generate_log.error("Gemini response part exists but has no text content.")
                if response_pydantic_schema:
                    return json.dumps({
                        "error_message": "Respuesta del LLM incompleta o en formato inesperado.",
                        "respuesta_detallada": "Error: El asistente devolvió una respuesta sin contenido textual.",
                        "fuentes_citadas": []
                    })
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