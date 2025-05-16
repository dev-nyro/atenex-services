# query-service/app/infrastructure/llms/gemini_adapter.py
import google.generativeai as genai
import structlog
from typing import Optional, List, Type, Any, Dict
from pydantic import BaseModel
import json

from app.core.config import settings
from app.application.ports.llm_port import LLMPort
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from app.domain.models import RespuestaEstructurada # Aunque no se usa directamente aquí, es el target
from app.utils.helpers import truncate_text # Importado para logging


log = structlog.get_logger(__name__)

# FLAG_CORRECTION_PYDANTIC_TO_GEMINI_SCHEMA
def _clean_pydantic_schema_for_gemini(pydantic_schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Transforms a Pydantic-generated JSON schema into a format more
    compatible with Gemini's FunctionDeclaration.parameters.
    Specifically, it handles:
    - Removes the top-level '$defs' key if present.
    - Transforms 'anyOf' for optional fields (e.g., {"anyOf": [{"type": X}, {"type": "null"}]})
      into {"type": X, "nullable": True}.
    - Converts JSON schema types to uppercase Gemini types (e.g., "string" -> "STRING").
    - Removes 'title' and 'description' from the root of the schema, as Gemini
      takes these from the FunctionDeclaration's name and description.
    """
    # log.debug("Original Pydantic Schema for cleaning: %s", pydantic_schema) # Can be very verbose

    # Store definitions from $defs if they exist, to attempt inlining $ref later
    definitions = pydantic_schema.get("$defs", {})
    schema_copy = {k: v for k, v in pydantic_schema.items() if k != "$defs"}

    def resolve_ref(ref_path: str) -> Dict[str, Any]:
        if not ref_path.startswith("#/$defs/"):
            # log.warning(f"Unsupported $ref path format: {ref_path}. Cannot resolve.")
            return {"$ref": ref_path} # Keep unresolved if not a $defs ref we can handle

        def_key = ref_path.split("/")[-1]
        if def_key in definitions:
            # Recursively transform the resolved definition
            # log.debug(f"Resolving $ref: {ref_path} with definition: {definitions[def_key]}")
            return transform_node(definitions[def_key])
        else:
            # log.warning(f"Definition not found for $ref: {ref_path}. Cannot resolve.")
            return {"$ref": ref_path} # Keep unresolved if definition is missing

    def transform_node(node: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(node, dict): # Base case for recursion if a list contains non-dicts
            return node

        transformed_node = {}
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                # Attempt to inline the reference
                # log.debug(f"Found $ref, attempting to resolve: {value}")
                inlined_ref = resolve_ref(value)
                # The inlined_ref is already transformed, merge its properties.
                # Take care not to overwrite existing keys in transformed_node if merging strategies are complex.
                # For simple inlining, replacing the node with the resolved content is common.
                # This replaces the current node content with the resolved and transformed definition.
                return inlined_ref

            elif key == "anyOf" and isinstance(value, list):
                # Handle specific 'anyOf' for optionality: [{"type": X}, {"type": "null"}]
                is_optional_pattern = False
                if len(value) == 2:
                    type_def_item = next((item for item in value if isinstance(item, dict) and item.get("type") != "null"), None)
                    null_def_item = next((item for item in value if isinstance(item, dict) and item.get("type") == "null"), None)
                    if type_def_item and null_def_item:
                        is_optional_pattern = True
                        # Merge properties from type_def_item into transformed_node
                        for k_type, v_type in type_def_item.items():
                            transformed_node[k_type] = transform_node(v_type) if isinstance(v_type, dict) else v_type
                        transformed_node["nullable"] = True # Explicitly mark as nullable

                if not is_optional_pattern:
                    # For other 'anyOf' uses, log a warning as it's not directly supported.
                    # Keep it as is for now or decide on a specific transformation if Gemini errors persist for these cases.
                    # log.warning(f"Complex 'anyOf' found, not transformed to nullable. May cause issues: {value}")
                    transformed_node[key] = [transform_node(item) for item in value] # Transform items within
                # If it was an optional pattern, we've handled it, so skip adding 'anyOf' back.
                continue


            elif isinstance(value, dict):
                transformed_node[key] = transform_node(value)
            elif isinstance(value, list) and key not in ["enum", "required"]: # Don't transform enums or required lists
                transformed_node[key] = [transform_node(item) if isinstance(item, dict) else item for item in value]
            else:
                transformed_node[key] = value

        # Convert JSON schema type to Gemini's expected type string (UPPERCASE)
        # This should happen *after* 'anyOf' processing might have set a 'type'.
        if "type" in transformed_node:
            json_type = transformed_node["type"]
            if isinstance(json_type, list): # Handles cases like {"type": ["string", "null"]} from Pydantic when default is None
                if "null" in json_type:
                    transformed_node["nullable"] = True
                # Find the non-null type
                actual_type = next((t for t in json_type if t != "null"), None)
                if actual_type and isinstance(actual_type, str):
                    transformed_node["type"] = actual_type.upper()
                elif actual_type: # If actual_type is a dict (e.g. nested schema for items in array)
                    transformed_node["type"] = transform_node(actual_type) # Recursively transform the type definition
                else: # Only "null" was found or list was empty/malformed
                    # log.warning(f"Type list {json_type} resulted in no actual type. Keeping as is or potentially erroring.")
                    pass # Or handle error
            elif isinstance(json_type, str):
                transformed_node["type"] = json_type.upper()

        return transformed_node

    final_schema = transform_node(schema_copy)

    # Remove 'title' and 'description' from the root level as Gemini takes these from FunctionDeclaration
    if "title" in final_schema:
        del final_schema["title"]
    if "description" in final_schema:
        del final_schema["description"]

    # log.debug("Schema after cleaning for Gemini: %s", final_schema) # Can be very verbose
    return final_schema


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
            TimeoutError, # General timeout that might occur
            # google.api_core.exceptions.GoogleAPIError (various sub-types for network/service issues)
            # Consider adding specific Google API errors if needed, e.g. google.api_core.exceptions.DeadlineExceeded
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
        # generate_log.debug("Sending request to Gemini API...") # Can be very verbose with long prompts

        tools_config_gemini: Optional[List[genai.types.Tool]] = None
        generation_config_dict: Dict[str, Any] = {
            "temperature": 0.6, # Example: Adjust as needed
            "top_p": 0.9,       # Example: Adjust as needed
            # "max_output_tokens": 8192, # Consider setting this if responses are too long / truncated
        }

        if response_pydantic_schema:
            try:
                # Generate Pydantic JSON schema
                # Using exclude_none=True might simplify if Gemini doesn't like explicit nulls unless nullable=True
                # json_schema_extra = getattr(response_pydantic_schema, "json_schema_extra", None)
                pydantic_schema_dict = response_pydantic_schema.model_json_schema()
                # generate_log.debug("Original Pydantic schema before cleaning", schema=pydantic_schema_dict)

                # Clean the schema for Gemini
                cleaned_schema_for_gemini_params = _clean_pydantic_schema_for_gemini(pydantic_schema_dict)
                generate_log.debug("Cleaned JSON Schema for Gemini Function Calling", schema_final=cleaned_schema_for_gemini_params)


                function_declaration = genai.types.FunctionDeclaration(
                    name="format_structured_response",
                    description=f"Formats the response according to the {response_pydantic_schema.__name__} schema. Ensure all required fields are present.",
                    parameters=cleaned_schema_for_gemini_params
                )

                tools_config_gemini = [genai.types.Tool(function_declarations=[function_declaration])]

                generation_config_dict["tool_config"] = genai.types.ToolConfig(
                    function_calling_config=genai.types.FunctionCallingConfig(
                        mode=genai.types.FunctionCallingConfig.Mode.FUNCTION, # Use FUNCTION to force a function call
                        allowed_function_names=["format_structured_response"]
                    )
                )
            except Exception as e_schema_gen:
                generate_log.error("Failed to generate or prepare JSON schema for Gemini function calling.",
                                   pydantic_model_name=response_pydantic_schema.__name__,
                                   error_details=str(e_schema_gen), exc_info=True)
                # This exception will propagate and be caught by the use case
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
                 finish_reason_str = getattr(response.prompt_feedback, 'block_reason', "UNKNOWN_REASON")
                 safety_ratings_str = str(getattr(response.prompt_feedback, 'safety_ratings', "N/A"))
                 generate_log.warning("Gemini response potentially blocked (no candidates)",
                                      finish_reason=finish_reason_str, safety_ratings=safety_ratings_str)
                 if response_pydantic_schema:
                     # Construct a fallback JSON error response if expecting JSON
                     return json.dumps({
                         "error_message": f"Respuesta bloqueada por Gemini (sin candidatos). Razón: {finish_reason_str}",
                         "respuesta_detallada": f"La generación de la respuesta fue bloqueada. Por favor, reformula tu pregunta o contacta a soporte si el problema persiste. Razón: {finish_reason_str}.",
                         "fuentes_citadas": []
                     })
                 return f"[Respuesta bloqueada por Gemini (sin candidatos). Razón: {finish_reason_str}]"

            candidate = response.candidates[0] # Assuming one candidate typically

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

            # Check for function call if schema was provided
            if response_pydantic_schema:
                function_call_part = next((part for part in candidate.content.parts if part.function_call), None)
                if function_call_part:
                    function_call = function_call_part.function_call
                    if function_call.name == "format_structured_response":
                        args_dict = {key: value for key, value in function_call.args.items()}
                        generated_text = json.dumps(args_dict)
                        generate_log.debug("Received JSON via function call from Gemini API", response_length=len(generated_text))
                    else:
                        generate_log.warning("Gemini called an unexpected function.",
                                             called_function_name=function_call.name,
                                             expected_function_name="format_structured_response")
                        # Fallback to trying to parse text if function call is wrong but text might exist
                        generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text') and part.text)
                        if not generated_text.strip() and response_pydantic_schema: # If no text and expected JSON
                            return json.dumps({
                                "error_message": f"LLM called unexpected function '{function_call.name}' and returned no usable text.",
                                "respuesta_detallada": f"Error: El asistente devolvió un formato inesperado (función: {function_call.name}).",
                                "fuentes_citadas": []
                            })
                else: # No function call found, but JSON was expected
                    generate_log.warning("Expected JSON via function call, but no function_call part found. Checking for plain text.")
                    generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text') and part.text)
                    if not generated_text.strip(): # No text either
                         return json.dumps({
                             "error_message": "LLM did not make a function call and returned no text when JSON was expected.",
                             "respuesta_detallada": "Error: El asistente no pudo estructurar la respuesta correctamente y no proporcionó texto.",
                             "fuentes_citadas": []
                         })
            else: # No schema provided, expect plain text
                generated_text = "".join(part.text for part in candidate.content.parts if hasattr(part, 'text') and part.text)
                generate_log.debug("Received text response from Gemini API", response_length=len(generated_text))


            # Final validation if JSON was expected
            if response_pydantic_schema:
                try:
                    # Attempt to parse to ensure it's valid JSON, even if it came from function call
                    json.loads(generated_text)
                except json.JSONDecodeError as json_err:
                    generate_log.error("Gemini response content is not valid JSON despite expectation.",
                                       raw_response_preview=truncate_text(generated_text, 500),
                                       error_message=str(json_err),
                                       was_function_call=(function_call_part is not None))
                    # Return a structured error JSON
                    return json.dumps({
                         "error_message": f"LLM returned malformed JSON: {str(json_err)}",
                         "respuesta_detallada": f"Error: La respuesta del asistente no pudo ser procesada (JSON malformado). Respuesta recibida: {truncate_text(generated_text, 200)}",
                         "fuentes_citadas": []
                    })

            return generated_text.strip()

        except (genai.types.generation_types.BlockedPromptException, genai.types.generation_types.StopCandidateException) as security_err:
             finish_reason_err_str = getattr(security_err, 'finish_reason', 'N/A') # Or specific logic for these types
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
            # Catch-all for other unexpected errors from the Gemini SDK or API
            generate_log.exception("Unhandled error during Gemini API call")
            # Re-raise as a ConnectionError or a more specific custom error for the use case to handle
            raise ConnectionError(f"Gemini API call failed unexpectedly: {e}") from e