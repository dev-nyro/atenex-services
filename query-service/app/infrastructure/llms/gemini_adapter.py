# query-service/app/infrastructure/llms/gemini_adapter.py
import structlog
from typing import Optional, List, Type, Any, Dict
from pydantic import BaseModel
import json

# MODIFICADO: Importar el nuevo SDK
from google import genai as google_genai_sdk # Renombrar para evitar confusión con el antiguo
from google.genai import types as google_genai_types # Usar el módulo 'types' del nuevo SDK

from app.core.config import settings
from app.application.ports.llm_port import LLMPort
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from app.domain.models import RespuestaEstructurada 
from app.utils.helpers import truncate_text


log = structlog.get_logger(__name__)

# La función _clean_pydantic_schema_for_gemini probablemente necesite ajustes
# ya que el nuevo SDK podría esperar un formato de schema ligeramente diferente
# o tener sus propias utilidades para esto (aunque Pydantic es común).
# Por ahora, se mantiene la lógica de limpieza, pero esto es un punto a verificar.
def _clean_pydantic_schema_for_gemini(pydantic_schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Transforms a Pydantic-generated JSON schema into a format more
    compatible with Gemini's FunctionDeclaration.parameters or GenerateContentConfig response_schema.
    """
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
                elif actual_type: transformed_node["type"] = transform_node(actual_type) # type: ignore
            elif isinstance(json_type, str): transformed_node["type"] = json_type.upper()
        return transformed_node
    final_schema = transform_node(schema_copy)
    if "title" in final_schema: del final_schema["title"]
    if "description" in final_schema: del final_schema["description"]
    return final_schema


class GeminiAdapter(LLMPort):
    def __init__(self):
        self._api_key = settings.GEMINI_API_KEY.get_secret_value()
        self._model_name = settings.GEMINI_MODEL_NAME # e.g., 'gemini-2.0-flash' o 'gemini-1.5-flash-latest'
        self.client: Optional[google_genai_sdk.Client] = None # MODIFICADO: tipo del nuevo SDK
        self._configure_client()

    def _configure_client(self):
        try:
            if self._api_key:
                # MODIFICADO: Configuración del nuevo SDK
                self.client = google_genai_sdk.Client(api_key=self._api_key)
                log.info("Google GenAI SDK client configured successfully", model_name_to_be_used=self._model_name)
            else:
                log.warning("Gemini API key is missing. Client not configured.")
        except Exception as e:
            log.error("Failed to configure Google GenAI SDK client", error=str(e), exc_info=True)
            self.client = None

    @retry(
        stop=stop_after_attempt(settings.HTTP_CLIENT_MAX_RETRIES + 1),
        wait=wait_exponential(multiplier=settings.HTTP_CLIENT_BACKOFF_FACTOR, min=2, max=10),
        # MODIFICADO: Ajustar excepciones al nuevo SDK si es necesario, o usar genéricas
        retry=retry_if_exception_type((
            google_genai_sdk.APIError, # Asumiendo que el nuevo SDK tiene una clase base APIError
            TimeoutError,
            httpx.RequestError # Si el nuevo SDK usa httpx internamente y puede lanzar esto
        )),
        reraise=True,
        before_sleep=lambda retry_state: log.warning(
            "Retrying Google GenAI API call",
            attempt=retry_state.attempt_number,
            wait_time=f"{retry_state.next_action.sleep:.2f}s", 
            error_type=type(retry_state.outcome.exception()).__name__ if retry_state.outcome else "N/A", 
            error_message=str(retry_state.outcome.exception()) if retry_state.outcome else "N/A" 
        )
    )
    async def generate(self, prompt: str,
                       response_pydantic_schema: Optional[Type[BaseModel]] = None
                      ) -> str:
        if not self.client:
            log.error("Google GenAI SDK client not initialized. Cannot generate answer.")
            raise ConnectionError("Google GenAI SDK client is not properly configured.")

        generate_log = log.bind(
            adapter="GeminiAdapterSDKvNew", model_name=self._model_name,
            prompt_length=len(prompt), expecting_json=bool(response_pydantic_schema)
        )

        # MODIFICADO: Argumentos para el nuevo SDK client.models.generate_content
        # El prompt es el argumento 'contents'
        # La configuración va en 'config' que es un types.GenerateContentConfig
        
        config_args: Dict[str, Any] = {
            "temperature": 0.6,
            "top_p": 0.9,
        }
        tools_arg: Optional[List[google_genai_types.Tool]] = None

        if response_pydantic_schema:
            config_args["response_mime_type"] = "application/json"
            # El nuevo SDK puede tomar directamente la clase Pydantic para response_schema
            # o un diccionario de schema limpio.
            config_args["response_schema"] = response_pydantic_schema 
            # Para "function calling" con JSON explícito, la documentación del nuevo SDK indica
            # que especificar response_mime_type="application/json" y response_schema es la forma.
            # Si quisiéramos un "tool" explícito (como get_weather_example), lo haríamos así:
            # pydantic_schema_dict = response_pydantic_schema.model_json_schema()
            # schema_params = _clean_pydantic_schema_for_gemini(pydantic_schema_dict)
            # fn_decl = google_genai_types.FunctionDeclaration(name="format_structured_response", description="...", parameters=schema_params)
            # tools_arg = [google_genai_types.Tool(function_declarations=[fn_decl])]
            # config_args["tools"] = tools_arg
            # (Omitido por ahora ya que response_schema debería ser suficiente para JSON estructurado)
            
            # Si necesitamos forzar el modo FUNCTION (aunque el nuevo SDK prefiere automatic):
            # function_calling_config_dict = {
            #     "mode": "FUNCTION", # O el enum correspondiente del nuevo SDK
            #     "allowed_function_names": ["format_structured_response"]
            # }
            # tool_config_for_generation_config = {"function_calling_config": function_calling_config_dict}
            # config_args["tool_config"] = google_genai_types.ToolConfig(**tool_config_for_generation_config)
            # -> Según la doc de JSON response, solo response_schema y mime_type son necesarios.
            # -> El antiguo ToolConfig y FunctionCallingConfig no se usan de la misma manera.

        final_generation_config = google_genai_types.GenerateContentConfig(**config_args)

        try:
            # MODIFICADO: Uso del cliente del nuevo SDK de forma asíncrona
            # (asumiendo que el cliente global self.client es síncrono,
            # necesitamos usar self.client.aio para llamadas async)
            # o, si self.client ya es un AsyncClient de httpx (para nuestro propio uso),
            # aquí usaríamos el client.aio del SDK de genai.
            # Para simplificar, si has instanciado `client = genai.Client()`,
            # para async es `await client.aio.models.generate_content(...)`

            # La documentación actual de google-genai no muestra una clase `AsyncClient` explícita
            # sino que los métodos async están bajo `client.aio.*`.
            # Si `google_genai_sdk.Client()` es síncrono, necesitamos un cliente async o run_in_executor.
            # Vamos a asumir que el SDK maneja la asincronía internamente para `generate_content_async`
            # o que se debe usar `client.aio.models.generate_content`.
            # Por la doc, para async es `client.aio.models.generate_content`
            
            # Para un SDK que podría no tener un cliente async incorporado de forma sencilla:
            # response = await asyncio.to_thread(
            #     self.client.models.generate_content, # type: ignore
            #     model=self._model_name,
            #     contents=prompt,
            #     generation_config=final_generation_config,
            #     # tools=tools_arg # Solo si se define un tool explícito
            # )
            
            # Usando el patrón recomendado para async con el nuevo SDK
            if not hasattr(self.client, 'aio'):
                raise RuntimeError("El cliente google-genai no tiene el atributo 'aio' esperado para operaciones asíncronas.")

            response = await self.client.aio.models.generate_content( # type: ignore
                model=f"models/{self._model_name}", # El nuevo SDK espera el prefijo "models/" para los de Gemini
                contents=prompt,
                generation_config=final_generation_config,
                tools=tools_arg 
            )
            

            generated_text = ""
            # El procesamiento de la respuesta en el nuevo SDK puede ser diferente.
            # La documentación muestra `response.text` para contenido de texto simple
            # y `response.parsed` para JSON cuando `response_schema` se usó.

            if not response.candidates:
                 finish_reason_str = str(getattr(response, 'prompt_feedback', {}).get('block_reason', "UNKNOWN_REASON"))
                 generate_log.warning("Gemini response potentially blocked (no candidates)",
                                      finish_reason=finish_reason_str)
                 if response_pydantic_schema:
                     return json.dumps({
                         "error_message": f"Respuesta bloqueada por Gemini (sin candidatos). Razón: {finish_reason_str}",
                         "respuesta_detallada": f"La generación de la respuesta fue bloqueada. Por favor, reformula tu pregunta o contacta a soporte si el problema persiste. Razón: {finish_reason_str}.",
                         "fuentes_citadas": []
                     })
                 return f"[Respuesta bloqueada por Gemini (sin candidatos). Razón: {finish_reason_str}]"

            candidate = response.candidates[0]

            if response_pydantic_schema:
                # Con el nuevo SDK y response_schema, el contenido parseado debería estar en response.text (como string JSON)
                # o, si el SDK lo parsea, en response.parts[0].text (si es json en texto) o a través de un método
                # como model_dump_json() si response es un Pydantic model, o .parsed.
                # La documentación del nuevo SDK dice:
                # "Cuando sea posible, el SDK analizará el JSON que se muestra y mostrará el resultado en response.parsed.
                #  Si proporcionaste una clase pydantic como el esquema, el SDK convertirá ese JSON en una instancia de la clase."
                # Pero `response` en sí no es el objeto parseado, es `GenerateContentResponse`.
                # `response.text` es la forma general de obtener el contenido textual.
                if hasattr(candidate, 'text') and candidate.text: # El nuevo SDK puede tener .text directamente en el candidate
                    generated_text = candidate.text
                elif candidate.content and candidate.content.parts:
                    # Si es un function call, el JSON estará en los args
                    function_call_part = next((part for part in candidate.content.parts if part.function_call), None)
                    if function_call_part and function_call_part.function_call.name == "format_structured_response":
                        args_dict = {key: value for key, value in function_call_part.function_call.args.items()} # type: ignore
                        generated_text = json.dumps(args_dict)
                        generate_log.debug("Received JSON via function call from Google GenAI SDK API", response_length=len(generated_text))
                    else: # Si no es function call pero se esperaba JSON, debería estar en .text
                        generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text') and part.text)
                        if not generated_text.strip():
                            generate_log.warning("Expected JSON, but got no text content from Gemini candidate parts.")
                            return json.dumps({
                                "error_message": "LLM did not return expected JSON content.",
                                "respuesta_detallada": "Error: El asistente no pudo estructurar la respuesta correctamente.",
                                "fuentes_citadas": []
                            })
                else:
                    generate_log.warning("Expected JSON, but candidate has no text or content parts.")
                    return json.dumps({
                        "error_message": "LLM candidate has no usable content for expected JSON.",
                        "respuesta_detallada": "Error: El asistente no devolvió contenido usable.",
                        "fuentes_citadas": []
                    })

                # Validar si es JSON
                try:
                    json.loads(generated_text) 
                except json.JSONDecodeError as json_err:
                    generate_log.error("Google GenAI SDK response content is not valid JSON despite expectation.",
                                       raw_response_preview=truncate_text(generated_text, 500),
                                       error_message=str(json_err))
                    return json.dumps({
                         "error_message": f"LLM returned malformed JSON: {str(json_err)}",
                         "respuesta_detallada": f"Error: La respuesta del asistente no pudo ser procesada (JSON malformado). Respuesta recibida: {truncate_text(generated_text, 200)}",
                         "fuentes_citadas": []
                    })
            else: # Respuesta de texto plano
                generated_text = response.text # El nuevo SDK tiene response.text directamente
                generate_log.debug("Received text response from Google GenAI SDK API", response_length=len(generated_text))

            return generated_text.strip()

        except google_genai_sdk.APIError as api_err: # Capturar errores específicos del nuevo SDK
             generate_log.error("Google GenAI SDK APIError", error_details=str(api_err), exc_info=True)
             if response_pydantic_schema:
                 return json.dumps({
                     "error_message": f"Error de API de Gemini: {type(api_err).__name__}",
                     "respuesta_detallada": f"Hubo un error con el servicio de lenguaje. Por favor, intenta de nuevo. (Detalle: {str(api_err)[:100]})",
                     "fuentes_citadas": []
                 })
             return f"[Error de API de Gemini: {str(api_err)}]"
        except Exception as e:
            generate_log.exception("Unhandled error during Google GenAI SDK API call")
            raise ConnectionError(f"Google GenAI SDK API call failed unexpectedly: {e}") from e