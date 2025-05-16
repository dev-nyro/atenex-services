# query-service/app/infrastructure/llms/gemini_adapter.py
import google.generativeai as genai
import structlog
from typing import Optional, List, Type, Any, Dict 
from pydantic import BaseModel
import json 

from app.core.config import settings
from app.application.ports.llm_port import LLMPort 
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from app.domain.models import RespuestaEstructurada 

from pydantic.json_schema import GenerateJsonSchema 
# from pydantic_core.core_schema import CoreSchema # No es necesario si usamos model_json_schema


log = structlog.get_logger(__name__)

def _remove_top_level_titles_and_descriptions(schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Remueve 'title' y 'description' del nivel raíz del schema si están presentes,
    ya que Gemini los toma de la FunctionDeclaration.
    También intenta manejar el problema de '$defs'.
    """
    cleaned_schema = schema.copy()
    if "title" in cleaned_schema:
        del cleaned_schema["title"]
    # La descripción a nivel de raíz del esquema de parámetros es manejada por FunctionDeclaration.description
    if "description" in cleaned_schema:
        # Potencialmente eliminarlo solo si es el docstring del modelo Pydantic raíz,
        # pero por seguridad, si Gemini no lo quiere, lo quitamos.
        del cleaned_schema["description"]

    # El error "Unknown field for Schema: $defs" sugiere que Gemini no quiere `$defs`
    # en el schema que se le pasa a `parameters`.
    # Pydantic v2 usa `$defs` para definiciones.
    # Una solución simple si las definiciones son para tipos anidados directos
    # es intentar eliminarlas si el resto del schema es autocontenido o si Gemini
    # puede inferir tipos anidados sin la sección `$defs` explícita a este nivel.
    # Esto es una simplificación; un aplanamiento real de $defs sería más complejo.
    if "$defs" in cleaned_schema:
        log.warning("'$defs' found in generated Pydantic schema. Removing for Gemini compatibility. "
                    "If complex $defs are used, this might lead to issues. Schema before: %s", cleaned_schema)
        # Esta es una suposición de que Gemini podría no necesitar/querer `$defs` a este nivel.
        # Es crucial que la estructura de `properties` sea completa para los tipos anidados.
        # Si `FuenteCitada` se define bien dentro de `properties` de `RespuestaEstructurada`
        # al generar el schema, puede que `$defs` no sea crucial para Gemini aquí.
        del cleaned_schema["$defs"] # Intenta eliminarla y ver si Gemini lo procesa mejor.
                                   # ¡Esto puede ser demasiado agresivo si hay referencias $ref reales!
                                   # Sin embargo, el error es explícito sobre "$defs".
        log.debug("Schema after attempting to remove $defs: %s", cleaned_schema)


    return cleaned_schema

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
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES + 1),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=2, max=10),
        retry=retry_if_exception_type((
            genai.types.generation_types.StopCandidateException,
            genai.types.generation_types.BlockedPromptException,
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

        tools_config_gemini: Optional[List[genai.types.Tool]] = None
        generation_config_dict: Dict[str, Any] = {
            "temperature": 0.6, 
            "top_p": 0.9,
        }
        
        if response_pydantic_schema:
            try:
                # Generar el schema JSON desde el modelo Pydantic
                # Pydantic V2: model_json_schema()
                # Usar ref_template personalizado puede no ser necesario si no hay refs complejas no deseadas
                # o si el aplanamiento de $defs no es la solución.
                # La documentación de Gemini sugiere pasar la clase directamente,
                # pero dado el error anterior, generar el schema como dict y limpiarlo es más robusto.
                
                # FLAG_CORRECTION_PYDANTIC_TO_GEMINI_SCHEMA
                # Genera el schema usando Pydantic V2
                # El `JsonSchemaMode.validation` es el default y generalmente correcto.
                generated_schema_dict = response_pydantic_schema.model_json_schema()

                # Limpiar el schema de campos que podrían causar problemas con Gemini
                cleaned_schema_for_gemini = _remove_top_level_titles_and_descriptions(generated_schema_dict)
                
                generate_log.debug("Cleaned JSON Schema for Gemini Function Calling", schema_final=cleaned_schema_for_gemini)

                function_declaration = genai.types.FunctionDeclaration(
                    name="format_structured_response", 
                    description=f"Formats the response according to the {response_pydantic_schema.__name__} schema. Ensure all required fields are present.",
                    parameters=cleaned_schema_for_gemini 
                )
                
                tools_config_gemini = [genai.types.Tool(function_declarations=[function_declaration])]
                
                generation_config_dict["tool_config"] = genai.types.ToolConfig(
                    function_calling_config=genai.types.FunctionCallingConfig(
                        mode=genai.types.FunctionCallingConfig.Mode.FUNCTION, 
                        allowed_function_names=["format_structured_response"] 
                    )
                )
            except Exception as e_schema_gen:
                generate_log.error("Failed to generate or prepare JSON schema for Gemini function calling.",
                                   pydantic_model_name=response_pydantic_schema.__name__,
                                   error_details=str(e_schema_gen), exc_info=True)
                raise ValueError(f"Schema generation for {response_pydantic_schema.__name__} failed: {e_schema_gen}") from e_schema_gen
        
        final_generation_config = genai.types.GenerationConfig(**generation_config_dict)

        try:
            response = await self.model.generate_content_async(
                prompt,
                generation_config=final_generation_config,
                tools=tools_config_gemini 
            )
            
            generated_text = "" 

            if not response.candidates:
                 finish_reason = getattr(response.prompt_feedback, 'block_reason', "UNKNOWN")
                 safety_ratings_str = str(getattr(response.prompt_feedback, 'safety_ratings', "N/A"))
                 generate_log.warning("Gemini response potentially blocked (no candidates)", 
                                      finish_reason=str(finish_reason), safety_ratings=safety_ratings_str)
                 if response_pydantic_schema: 
                     return json.dumps({
                         "error_message": f"Respuesta bloqueada por Gemini (sin candidatos). Razón: {finish_reason}",
                         "respuesta_detallada": f"Respuesta bloqueada por Gemini (sin candidatos). Razón: {finish_reason}",
                         "fuentes_citadas": []
                     })
                 return f"[Respuesta bloqueada por Gemini (sin candidatos). Razón: {finish_reason}]"

            candidate = response.candidates[0]
            
            if not candidate.content or not candidate.content.parts:
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
            
            if response_pydantic_schema and candidate.content.parts[0].function_call:
                function_call = candidate.content.parts[0].function_call
                if function_call.name == "format_structured_response":
                    args_dict = {key: value for key, value in function_call.args.items()}
                    generated_text = json.dumps(args_dict) 
                    generate_log.debug("Received JSON via function call from Gemini API", response_length=len(generated_text))
                else: 
                    generate_log.warning("Gemini called an unexpected function or no function when one was forced.",
                                         called_function_name=getattr(function_call, 'name', 'N/A'))
                    generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text')) 
                    if not generated_text.strip(): 
                        return json.dumps({
                             "error_message": "LLM did not call the formatting function and returned no text.",
                             "respuesta_detallada": "Error: El asistente no pudo estructurar la respuesta correctamente.",
                             "fuentes_citadas": []
                        })
            else: 
                generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text'))
                generate_log.debug("Received text response (or non-function-call) from Gemini API", response_length=len(generated_text))
                if response_pydantic_schema:
                    generate_log.warning("Expected JSON via function call, but received plain text or no function call part. Attempting to parse text as JSON.")

            if response_pydantic_schema:
                try:
                    json.loads(generated_text) 
                except json.JSONDecodeError as json_err:
                    generate_log.error("Gemini response content is not valid JSON", 
                                       raw_response_preview=truncate_text(generated_text, 500), 
                                       error=str(json_err),
                                       was_function_call=(candidate.content.parts[0].function_call is not None))
                    return json.dumps({
                         "error_message": f"LLM returned malformed JSON: {str(json_err)}",
                         "respuesta_detallada": f"Error: La respuesta del asistente no pudo ser procesada (JSON malformado). Respuesta recibida: {truncate_text(generated_text, 200)}",
                         "fuentes_citadas": []
                    })

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