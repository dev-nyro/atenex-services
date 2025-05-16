# query-service/app/infrastructure/llms/gemini_adapter.py
import google.generativeai as genai
import structlog
from typing import Optional, List, Type, Any, Dict # Añadido Dict
from pydantic import BaseModel, RootModel # Añadido RootModel
import json 

from app.core.config import settings
from app.application.ports.llm_port import LLMPort 
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from app.domain.models import RespuestaEstructurada
# Importar pydantic_to_openapi_schema si es necesario, o generar el schema manualmente
from pydantic_core import CoreSchema, core_schema
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
                # Ajuste: No solicitar JSON por defecto a nivel de modelo si lo vamos a manejar por llamada
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

        tools_config: Optional[List[genai.types.Tool]] = None
        generation_config_dict: Dict[str, Any] = {
            "temperature": 0.6,
            "top_p": 0.9,
        }

        if response_pydantic_schema:
            # Convertir el modelo Pydantic a un JSON Schema compatible con Gemini FunctionDeclaration
            # Usamos Pydantic v2 para generar el schema JSON
            json_schema_generator = GenerateJsonSchema(by_alias=True, ref_template="#/components/schemas/{model}")
            
            # Generar el schema JSON desde el modelo Pydantic
            # Para Pydantic v2, el método es model_json_schema()
            generated_schema = response_pydantic_schema.model_json_schema(
                json_schema_generator=json_schema_generator,
                mode='validation' # o 'serialization' si se prefiere
            )

            # Quitar el título general si lo genera pydantic en la raíz, ya que Gemini espera parámetros directamente
            if "title" in generated_schema:
                del generated_schema["title"]
            if "description" in generated_schema: # Gemini no usa la descripción a nivel raíz del schema
                del generated_schema["description"]


            # Crear la FunctionDeclaration para la herramienta
            # El nombre de la función es arbitrario pero debe estar presente
            function_declaration = genai.types.FunctionDeclaration(
                name="format_structured_response",
                description=f"Formats the response according to the {response_pydantic_schema.__name__} schema.",
                parameters=generated_schema # Pasar el diccionario de schema generado
            )
            
            # Envolver la FunctionDeclaration en un Tool
            # Aquí se usa el nombre "format_structured_response" para la función
            # y se espera que el LLM la "llame" para formatear su salida.
            tools_config = [genai.types.Tool(function_declarations=[function_declaration])]
            
            # Indicar al LLM que llame a esta función específica para formatear la salida.
            # Usar el ToolConfig para forzar la llamada a la función.
            generation_config_dict["tool_config"] = genai.types.ToolConfig(
                function_calling_config=genai.types.FunctionCallingConfig(
                    mode=genai.types.FunctionCallingConfig.Mode.ANY, # o .FUNCTION para forzarla siempre
                    allowed_function_names=["format_structured_response"] 
                )
            )
            # No establecemos response_mime_type ni response_schema directamente en generation_config si usamos tools/function_calling

        final_generation_config = genai.types.GenerationConfig(**generation_config_dict)

        try:
            response = await self.model.generate_content_async(
                prompt,
                generation_config=final_generation_config,
                tools=tools_config if tools_config else None # Pasar las herramientas si están definidas
            )
            
            generated_text = "" 

            if not response.candidates:
                 finish_reason = getattr(response.prompt_feedback, 'block_reason', "UNKNOWN")
                 safety_ratings_str = str(getattr(response.prompt_feedback, 'safety_ratings', "N/A"))
                 generate_log.warning("Gemini response potentially blocked (no candidates)", 
                                      finish_reason=str(finish_reason), safety_ratings=safety_ratings_str)
                 if response_pydantic_schema: # Si esperábamos JSON, devolvemos un error JSON
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
            
            # Si se usó function calling, la respuesta estará en la llamada a la función
            if response_pydantic_schema and candidate.content.parts[0].function_call:
                function_call = candidate.content.parts[0].function_call
                if function_call.name == "format_structured_response":
                    # Los argumentos de la función son el JSON que queremos
                    # function_call.args es un protobuf MessageMap, convertir a dict
                    args_dict = {key: value for key, value in function_call.args.items()}
                    generated_text = json.dumps(args_dict) # Convertir dict a string JSON
                    generate_log.debug("Received JSON via function call from Gemini API", response_length=len(generated_text))
                else:
                    generate_log.warning("Gemini called an unexpected function", called_function=function_call.name)
                    generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text')) # Fallback a texto
            else: # Respuesta de texto normal
                generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text'))
                generate_log.debug("Received text response from Gemini API", response_length=len(generated_text))


            # Si esperábamos JSON pero no lo obtuvimos a través de function_call,
            # o si el texto es un intento de JSON, intentamos validarlo
            if response_pydantic_schema:
                try:
                    # Incluso si se obtuvo de function_call, lo validamos de nuevo
                    json.loads(generated_text) 
                except json.JSONDecodeError as json_err:
                    generate_log.error("Gemini returned invalid JSON string (even after function call attempt or direct)", raw_response=truncate_text(generated_text, 500), error=str(json_err))
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