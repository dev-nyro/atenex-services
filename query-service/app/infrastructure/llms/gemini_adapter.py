# query-service/app/infrastructure/llms/gemini_adapter.py
import google.generativeai as genai
import structlog
from typing import Optional, List, Type, Any, Dict 
from pydantic import BaseModel, RootModel 
import json 

from app.core.config import settings
from app.application.ports.llm_port import LLMPort 
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from app.domain.models import RespuestaEstructurada # Tu modelo Pydantic

# Para generar el JSON Schema compatible con Gemini FunctionDeclaration desde Pydantic v2
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaMode

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
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES + 1),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=2, max=10),
        retry=retry_if_exception_type((
            genai.types.generation_types.StopCandidateException,
            genai.types.generation_types.BlockedPromptException,
            TimeoutError, # Para timeouts de red genéricos
            # Podrías añadir más excepciones específicas de google.api_core si las encuentras
        )),
        reraise=True,
        before_sleep=lambda retry_state: log.warning(
            "Retrying Gemini API call",
            attempt=retry_state.attempt_number,
            wait_time=f"{retry_state.next_action.sleep:.2f}s", # type: ignore
            error_type=type(retry_state.outcome.exception()).__name__ if retry_state.outcome else "N/A", # type: ignore
            error_message=str(retry_state.outcome.exception()) if retry_state.outcome else "N/A" # type: ignore
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
            # "max_output_tokens": 8192, # Descomentar si necesitas limitar explícitamente
        }

        # FLAG_GEMINI_FUNCTION_CALLING_SCHEMA_FIX
        if response_pydantic_schema:
            # Generar el schema JSON desde el modelo Pydantic usando el método recomendado para Pydantic v2
            # La documentación de google-generativeai 0.5.x indica que se puede pasar la clase Pydantic directamente.
            # Sin embargo, el error `BaseModel.model_json_schema() got an unexpected keyword argument 'json_schema_generator'`
            # sugiere un problema en cómo la librería de Gemini intenta convertir internamente ese modelo Pydantic
            # a un JSON Schema, o una incompatibilidad de versiones/métodos de Pydantic.
            # Para forzar la generación correcta del schema JSON *antes* de pasarlo a Gemini,
            # lo generamos explícitamente aquí y lo pasamos como un diccionario.

            try:
                # Usar Pydantic v2 para generar el schema JSON.
                # `GenerateJsonSchema` permite personalizar la generación si es necesario.
                json_schema_generator = GenerateJsonSchema(
                    by_alias=True, # Usa alias de campos si están definidos en el modelo Pydantic
                    ref_template="#/components/schemas/{model}" # Estilo de referencia común, aunque para Gemini no es tan relevante
                )
                # `model_json_schema` es el método correcto en Pydantic v2.
                # No se pasa `json_schema_generator` como argumento a model_json_schema,
                # sino que se usa como argumento en el constructor de GenerateJsonSchema si se necesitan configuraciones especiales.
                # El método es simplemente `response_pydantic_schema.model_json_schema()`
                
                # OJO: Para Gemini, el `parameters` de FunctionDeclaration espera un OpenAPI Schema Object.
                # Pydantic's `model_json_schema()` produce un JSON Schema, que es muy similar y a menudo compatible.
                # Lo crucial es la estructura de `type`, `properties`, `required`, `description`.

                openapi_schema_dict = response_pydantic_schema.model_json_schema(
                     # Puedes usar `JsonSchemaMode.validation` o `JsonSchemaMode.serialization`
                     # `validation` es más común para entradas.
                )
                
                # Gemini espera que el `parameters` no tenga el título raíz "RespuestaEstructurada" que Pydantic puede añadir.
                # Se asegura que el schema pasado solo contenga 'type', 'properties', 'required', 'description' a nivel raíz.
                if "title" in openapi_schema_dict: # El título general del modelo no es parte del schema de parámetros
                    del openapi_schema_dict["title"]
                if "description" in openapi_schema_dict and openapi_schema_dict["description"] == response_pydantic_schema.__doc__:
                    # La descripción a nivel raíz del schema de parámetros, si es solo el docstring del modelo, puede ser redundante.
                    # Gemini toma la descripción de la FunctionDeclaration.
                    del openapi_schema_dict["description"]
                
                # Ajustes para asegurar compatibilidad con OpenAPI schema esperado por Gemini
                # A veces Pydantic puede añadir `$defs` o `definitions` que no son directamente parte
                # del schema de parámetros de una función para Gemini si es simple.
                # Si el schema es complejo con referencias internas, puede ser necesario aplanarlo o
                # asegurarse de que las referencias sean entendidas. Para RespuestaEstructurada y FuenteCitada,
                # Pydantic debería generar un schema anidado dentro de 'properties' sin `$defs` complejos
                # si no hay auto-referencias complejas.
                
                generate_log.debug("Generated JSON Schema for Gemini Function Calling", schema_generated=openapi_schema_dict)

                function_declaration = genai.types.FunctionDeclaration(
                    name="format_structured_response", # Nombre de la función que el LLM "llamará"
                    description=f"Formats the response according to the {response_pydantic_schema.__name__} schema.",
                    parameters=openapi_schema_dict # El schema JSON generado
                )
                
                tools_config_gemini = [genai.types.Tool(function_declarations=[function_declaration])]
                
                # Forzar al modelo a llamar a esta función para estructurar su salida
                generation_config_dict["tool_config"] = genai.types.ToolConfig(
                    function_calling_config=genai.types.FunctionCallingConfig(
                        mode=genai.types.FunctionCallingConfig.Mode.FUNCTION, # FORZAR la llamada a la función
                        allowed_function_names=["format_structured_response"] 
                    )
                )
                # Ya no se usa response_mime_type ni response_schema directamente si usamos tools/function_calling
                # para forzar el output JSON.

            except Exception as e_schema_gen:
                generate_log.error("Failed to generate or prepare JSON schema for Gemini function calling.",
                                   pydantic_model_name=response_pydantic_schema.__name__,
                                   error_details=str(e_schema_gen), exc_info=True)
                # Si falla la generación del schema, no se podrá forzar JSON estructurado de esta manera.
                # Se podría caer a un modo de generación de texto simple, o re-lanzar.
                # Por ahora, se re-lanza para indicar un problema de configuración/código.
                raise ValueError(f"Schema generation for {response_pydantic_schema.__name__} failed: {e_schema_gen}") from e_schema_gen
        
        final_generation_config = genai.types.GenerationConfig(**generation_config_dict)

        try:
            response = await self.model.generate_content_async(
                prompt,
                generation_config=final_generation_config,
                tools=tools_config_gemini # Pasar las herramientas (puede ser None)
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
            
            # Verificar si se llamó a la función para formatear la respuesta
            if response_pydantic_schema and candidate.content.parts[0].function_call:
                function_call = candidate.content.parts[0].function_call
                if function_call.name == "format_structured_response":
                    args_dict = {key: value for key, value in function_call.args.items()}
                    generated_text = json.dumps(args_dict) 
                    generate_log.debug("Received JSON via function call from Gemini API", response_length=len(generated_text))
                else: # Se llamó a otra función o ninguna, inesperado si se forzó.
                    generate_log.warning("Gemini called an unexpected function or no function when one was forced.",
                                         called_function_name=getattr(function_call, 'name', 'N/A'))
                    # Fallback a texto plano o error JSON
                    generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text')) # Fallback a texto
                    if not generated_text.strip(): # Si el texto también está vacío
                        return json.dumps({
                             "error_message": "LLM did not call the formatting function and returned no text.",
                             "respuesta_detallada": "Error: El asistente no pudo estructurar la respuesta correctamente.",
                             "fuentes_citadas": []
                        })
            else: # Respuesta de texto normal (si no se esperaba JSON o el LLM no hizo la llamada de función)
                generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text'))
                generate_log.debug("Received text response (or non-function-call) from Gemini API", response_length=len(generated_text))
                # Si esperábamos JSON pero no lo obtuvimos como function call, esto es un problema.
                if response_pydantic_schema:
                    generate_log.warning("Expected JSON via function call, but received plain text or no function call part. Attempting to parse text as JSON.")
                    # Se intentará parsear `generated_text` como JSON más abajo.

            if response_pydantic_schema:
                try:
                    # Intentar parsear el texto (ya sea de function_call.args o de la respuesta directa)
                    # como JSON. Esto valida que sea parseable, la validación del schema Pydantic se hace después.
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