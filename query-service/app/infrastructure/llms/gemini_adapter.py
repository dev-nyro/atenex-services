# query-service/app/infrastructure/llms/gemini_adapter.py
# LLM_CORRECTION_START: Correct imports for google-genai SDK
from google import genai
from google.generativeai import types as genai_types
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
            genai_types.generation_types.StopCandidateException, 
            genai_types.generation_types.BlockedPromptException, 
            TimeoutError, 
            # Potentially other google.api_core.exceptions for network issues
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

        generation_config_dict: Dict[str, Any] = {
            "temperature": 0.6,
            "top_p": 0.9,
        }
        tools_config: Optional[List[genai_types.Tool]] = None 

        # LLM_CORRECTION_GEMINI_FUNCTION_CALLING: Explicitly use Function Calling for structured JSON
        if response_pydantic_schema:
            # Pydantic v2 usa model_json_schema()
            pydantic_schema_dict = response_pydantic_schema.model_json_schema()
            
            # Gemini necesita una limpieza del schema Pydantic estándar
            schema_params_for_gemini = _clean_pydantic_schema_for_gemini_tool(pydantic_schema_dict)
            
            generate_log.debug("Cleaned Pydantic JSON Schema for Gemini Function Declaration", schema_used=schema_params_for_gemini)

            # Define la declaración de la función para Gemini
            fn_decl = genai_types.FunctionDeclaration( 
                name="format_structured_response", # Nombre arbitrario para la función que "generará" el JSON
                description=f"Formats the response according to the {response_pydantic_schema.__name__} schema.",
                parameters=schema_params_for_gemini # El schema Pydantic limpio
            )
            
            # Configura la herramienta (Tool) que usa la declaración de función
            tools_config = [genai_types.Tool(function_declarations=[fn_decl])] 
            
            # Indica a Gemini que use esta herramienta para generar la respuesta
            # 'ANY' modo fuerza al modelo a llamar a la función especificada.
            # 'AUTO' el modelo decide, 'NONE' no llama.
            generation_config_dict["tool_config"] = genai_types.ToolConfig( 
                function_calling_config=genai_types.FunctionCallingConfig( 
                    mode=genai_types.FunctionCallingConfig.Mode.ANY, # Forzar la llamada a la función
                    allowed_function_names=["format_structured_response"] # Asegurar que solo llame a esta
                )
            )
        
        final_generation_config = genai_types.GenerationConfig(**generation_config_dict) 
        
        try:
            call_kwargs: Dict[str, Any] = {"generation_config": final_generation_config}
            if tools_config: # Si se configuraron herramientas (para JSON estructurado)
                call_kwargs["tools"] = tools_config
            
            generate_log.debug("Sending request to Gemini API...", call_kwargs_keys=list(call_kwargs.keys()))
            response = await self._model.generate_content_async(prompt, **call_kwargs)
            generated_text = ""

            if not response.candidates:
                 finish_reason_str = getattr(response.prompt_feedback, 'block_reason', "UNKNOWN_REASON") 
                 safety_ratings_str = str(getattr(response.prompt_feedback, 'safety_ratings', "N/A")) 
                 generate_log.warning("Gemini response potentially blocked (no candidates)",
                                      finish_reason=finish_reason_str, safety_ratings=safety_ratings_str)
                 if response_pydantic_schema:
                     # Devuelve un JSON de error si se esperaba JSON
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
            
            # LLM_CORRECTION_GEMINI_FUNCTION_CALLING: Extraer JSON de la llamada a función
            function_call_part_found = False
            if response_pydantic_schema: 
                function_call_part = next((part for part in candidate.content.parts if part.function_call), None)
                if function_call_part:
                    function_call_part_found = True
                    function_call = function_call_part.function_call
                    if function_call.name == "format_structured_response":
                        args_dict = {key: value for key, value in function_call.args.items()}
                        generated_text = json.dumps(args_dict) 
                        generate_log.debug("Received JSON via function call from Gemini API", response_length=len(generated_text))
                    else: 
                        generate_log.warning("Gemini called an unexpected function when JSON was expected.",
                                             called_function_name=function_call.name)
                        # Fallback a texto plano si la función no es la esperada.
                        generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text') and part.text) 
                        if not generated_text.strip(): 
                            # Si es un JSON vacío de error
                            return json.dumps({
                                "error_message": f"LLM called unexpected function '{function_call.name}' and returned no usable text.",
                                "respuesta_detallada": f"Error: El asistente devolvió un formato inesperado (función: {function_call.name}).",
                                "fuentes_citadas": []
                            })
                else: # No se encontró FunctionCall, intentar parsear texto si existe
                    generate_log.warning("Expected JSON via function call, but no function_call part found. Attempting to parse plain text output as JSON.")
                    generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text') and part.text)
                    if not generated_text.strip(): # Si el texto está vacío, es un error
                         return json.dumps({
                             "error_message": "LLM did not make a function call and returned no text when JSON was expected.",
                             "respuesta_detallada": "Error: El asistente no pudo estructurar la respuesta correctamente y no proporcionó texto.",
                             "fuentes_citadas": []
                         })
            
            # Si no se esperaba JSON, o si se esperaba pero no hubo function call (y se parseó texto arriba)
            if not function_call_part_found and not response_pydantic_schema:
                generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text') and part.text)
                generate_log.debug("Received plain text response from Gemini API", response_length=len(generated_text))
            elif not function_call_part_found and response_pydantic_schema and not generated_text:
                # Este caso ya debería estar cubierto por la lógica anterior si generated_text queda vacío
                generate_log.error("JSON expected, no function call, and no usable text found in parts (final check).")
                return json.dumps({
                    "error_message": "LLM did not provide a structured JSON response as expected and returned no usable text.",
                    "respuesta_detallada": "Error: El asistente no pudo estructurar la respuesta correctamente.",
                    "fuentes_citadas": []
                })

            # Validación final de JSON si se esperaba
            if response_pydantic_schema:
                try:
                    # Intentar validar el Pydantic model aquí mismo para asegurar que sea correcto antes de devolver
                    # Esto es redundante si el use_case lo hace, pero puede ayudar a aislar errores.
                    # En este caso, solo nos aseguramos que es un JSON válido, el UseCase hará el model_validate_json.
                    json.loads(generated_text) 
                except json.JSONDecodeError as json_err:
                    generate_log.error("Gemini response content is not valid JSON despite expectation.",
                                       raw_response_preview=truncate_text(generated_text, 500),
                                       error_message=str(json_err),
                                       was_function_call=function_call_part_found)
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
            if response_pydantic_schema: # Devuelve un JSON de error si se esperaba JSON
                return json.dumps({
                    "error_message": f"Error inesperado en la API de Gemini: {type(e).__name__}",
                    "respuesta_detallada": f"Error interno al comunicarse con el asistente: {type(e).__name__} - {str(e)[:100]}.",
                    "fuentes_citadas": []
                })
            raise ConnectionError(f"Gemini API call failed unexpectedly: {e}") from e

# LLM_CORRECTION_GEMINI_FUNCTION_CALLING: Helper para limpiar schema Pydantic para Gemini Tools
def _clean_pydantic_schema_for_gemini_tool(pydantic_schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Limpia un esquema JSON generado por Pydantic v2 para que sea compatible
    con las FunctionDeclaration de Gemini.
    Elimina: '$defs', 'title', 'description' del nivel raíz.
    Convierte tipos `array` con `anyOf` (para opcionales) a un tipo simple con `nullable: true`.
    Convierte tipos a MAYÚSCULAS.
    """
    
    # Referencias internas que Gemini podría no entender bien
    definitions = pydantic_schema.get("$defs", {})

    # Copia el schema sin los campos problemáticos de nivel raíz
    schema_copy = {
        k: v
        for k, v in pydantic_schema.items()
        if k not in {"$defs", "title", "description", "$schema"} # Quitar $schema también por si acaso
    }

    def resolve_ref_and_clean(ref_path: str) -> Dict[str, Any]:
        # Resuelve referencias internas y limpia el nodo referenciado
        if not ref_path.startswith("#/$defs/"): # Si no es una ref interna, devolverla tal cual.
            return {"$ref": ref_path} # Debería ser raro en esquemas Pydantic simples.
        
        def_key = ref_path.split("/")[-1]
        if def_key in definitions:
            # Recursivamente limpiar la definición referenciada
            return _transform_node_for_gemini(definitions[def_key])
        else:
            # Referencia rota, devolverla para que falle en otro lado o se ignore
            log.warning(f"Broken JSON schema reference found and could not be resolved: {ref_path}")
            return {"$ref": ref_path} 

    def _transform_node_for_gemini(node: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(node, dict):
            return node # No es un diccionario, no se transforma (e.g., un valor simple)

        transformed_node = {}
        for key, value in node.items():
            # Eliminar campos que Gemini no usa o que pueden causar problemas
            if key in {"default", "examples", "example", "const", "title", "description"}: # Quitar 'title' y 'description' también de nodos internos
                continue
            
            if key == "$ref" and isinstance(value, str):
                 # Si es una referencia, resolverla y usar el resultado transformado
                 return resolve_ref_and_clean(value) # Importante: esto reemplaza el nodo actual

            elif key == "anyOf" and isinstance(value, list):
                # Manejar el patrón de Pydantic para campos opcionales: anyOf: [ <type>, {"type": "null"} ]
                is_optional_pattern = False
                if len(value) == 2:
                    type_def_item = next((item for item in value if isinstance(item, dict) and item.get("type") != "null"), None)
                    null_def_item = next((item for item in value if isinstance(item, dict) and item.get("type") == "null"), None)
                    
                    if type_def_item and null_def_item:
                        is_optional_pattern = True
                        # Copiar las propiedades del tipo real
                        for k_type, v_type in type_def_item.items():
                            transformed_node[k_type] = _transform_node_for_gemini(v_type) if isinstance(v_type, dict) else v_type
                        transformed_node["nullable"] = True # Marcar como nullable para Gemini
                
                if not is_optional_pattern: # Si no es el patrón opcional, transformar los items de anyOf
                    transformed_node[key] = [_transform_node_for_gemini(item) for item in value]
                continue # Salta al siguiente key del nodo original

            elif isinstance(value, dict):
                transformed_node[key] = _transform_node_for_gemini(value)
            elif isinstance(value, list) and key not in ["enum", "required"]: # 'enum' y 'required' deben mantener sus listas tal cual
                transformed_node[key] = [_transform_node_for_gemini(item) if isinstance(item, dict) else item for item in value]
            else:
                transformed_node[key] = value
        
        # Convertir el campo 'type' a MAYÚSCULAS si es un string, como espera Gemini
        if "type" in transformed_node:
            json_type = transformed_node["type"]
            if isinstance(json_type, list): # Casos como type: ["string", "null"]
                if "null" in json_type:
                    transformed_node["nullable"] = True # Marcar explícitamente
                # Tomar el primer tipo no-null
                actual_type = next((t for t in json_type if t != "null"), None)
                if actual_type and isinstance(actual_type, str):
                    transformed_node["type"] = actual_type.upper() 
                elif actual_type: # Si el tipo actual es un sub-esquema (raro para 'type')
                     transformed_node["type"] = _transform_node_for_gemini(actual_type)
                else: # Solo había "null" o lista vacía, problemático
                    del transformed_node["type"] # Mejor quitarlo si es inválido
                    log.warning("Unsupported type list found in Pydantic schema for Gemini, type removed.", original_type_list=json_type)
            elif isinstance(json_type, str):
                transformed_node["type"] = json_type.upper()

        return transformed_node

    final_schema = _transform_node_for_gemini(schema_copy)
    
    # Asegurar que 'properties' y 'required' estén al nivel correcto si no lo están
    # (generalmente Pydantic lo hace bien para 'object')
    if final_schema.get("type") == "OBJECT" and "properties" not in final_schema:
        log.warning("Schema type is OBJECT but no 'properties' key. Moving root keys into 'properties'.", current_schema_keys=list(final_schema.keys()))
        # Esto es un parche por si _transform_node_for_gemini devuelve un objeto plano
        # en lugar de un objeto con una clave 'properties'. Es poco probable con Pydantic v2.
        # properties_from_root = {k:v for k,v in final_schema.items() if k != "type"}
        # final_schema["properties"] = properties_from_root
        # for k_prop in list(properties_from_root.keys()): # Crear copia para iterar
        #     if k_prop != "type" : del final_schema[k_prop]
        pass


    return final_schema