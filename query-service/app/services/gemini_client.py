# ./app/services/gemini_client.py
import google.generativeai as genai
import structlog
from typing import Optional

from app.core.config import settings
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

log = structlog.get_logger(__name__)

class GeminiClient:
    """Cliente para interactuar con la API de Google Gemini."""

    def __init__(self):
        self.api_key = settings.GEMINI_API_KEY.get_secret_value()
        self.model_name = settings.GEMINI_MODEL_NAME
        try:
            genai.configure(api_key=self.api_key)
            self.model = genai.GenerativeModel(self.model_name)
            log.info("Gemini client configured successfully", model_name=self.model_name)
        except Exception as e:
            log.error("Failed to configure Gemini client", error=str(e), exc_info=True)
            # Podríamos fallar aquí o permitir que falle en la primera llamada
            self.model = None # Marcar como no inicializado

    # Añadir reintentos para errores comunes de API (ej: RateLimitError, InternalServerError)
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((
            genai.types.generation_types.StopCandidateException, # Podría indicar filtro de seguridad
            genai.types.generation_types.BlockedPromptException, # Prompt bloqueado
            # Añadir errores específicos de google.api_core.exceptions si se usa cliente gRPC/REST directamente
            # Por ahora, asumimos que el SDK maneja algunos reintentos internos, pero añadimos básicos
            TimeoutError, # Si la llamada subyacente tiene timeout
            # Podríamos necesitar capturar errores más específicos de la librería subyacente
            # Ejemplo: google.api_core.exceptions.ResourceExhausted (Rate limit)
            # Ejemplo: google.api_core.exceptions.InternalServerError
        )),
        reraise=True,
        before_sleep=lambda retry_state: log.warning(
            "Retrying Gemini API call",
            attempt=retry_state.attempt_number,
            wait_time=f"{retry_state.next_action.sleep:.2f}s",
            error=str(retry_state.outcome.exception())
        )
    )
    async def generate_answer(self, prompt: str) -> str:
        """
        Genera una respuesta usando el modelo Gemini configurado.

        Args:
            prompt: El prompt completo a enviar al modelo.

        Returns:
            La respuesta generada como string.

        Raises:
            ValueError: Si el cliente no se inicializó correctamente.
            Exception: Si la API de Gemini devuelve un error inesperado o no se puede extraer texto.
        """
        if not self.model:
            log.error("Gemini client not initialized. Cannot generate answer.")
            raise ValueError("Gemini client is not properly configured.")

        generate_log = log.bind(model_name=self.model_name, prompt_length=len(prompt))
        generate_log.info("Sending request to Gemini API...")

        try:
            # La llamada generate_content puede ser bloqueante, ejecutar en thread si es necesario
            # Por ahora, asumimos que es lo suficientemente rápida o se maneja en el event loop
            # response = await asyncio.to_thread(self.model.generate_content, prompt) # Opción si es bloqueante
            response = await self.model.generate_content_async(prompt) # Usar versión async si está disponible

            # Verificar si la respuesta fue bloqueada o no tiene contenido
            if not response.candidates or not hasattr(response.candidates[0], 'content') or not hasattr(response.candidates[0].content, 'parts'):
                 # Analizar finish_reason si está disponible
                 finish_reason = response.candidates[0].finish_reason if response.candidates else "UNKNOWN"
                 safety_ratings = response.candidates[0].safety_ratings if response.candidates else "N/A"
                 generate_log.warning("Gemini response blocked or empty", finish_reason=finish_reason, safety_ratings=str(safety_ratings))
                 # Devolver un mensaje indicando el bloqueo o vacío
                 return f"[Respuesta bloqueada por Gemini o vacía. Razón: {finish_reason}]"


            # Extraer el texto de la respuesta (asumiendo respuesta simple de texto)
            # La estructura puede variar, ajustar según sea necesario
            generated_text = "".join(part.text for part in response.candidates[0].content.parts)

            generate_log.info("Received response from Gemini API", response_length=len(generated_text))
            return generated_text.strip()

        except (genai.types.generation_types.BlockedPromptException, genai.types.generation_types.StopCandidateException) as security_err:
             generate_log.warning("Gemini request blocked due to safety settings or prompt content", error=str(security_err), finish_reason=getattr(security_err, 'finish_reason', 'N/A'))
             # Considerar devolver un mensaje específico o lanzar una excepción controlada
             return f"[Contenido bloqueado por Gemini: {type(security_err).__name__}]"
        except Exception as e:
            generate_log.exception("Error during Gemini API call")
            # Relanzar la excepción para que sea manejada por el endpoint
            raise

# Instancia global del cliente (opcional, podría instanciarse por request)
# Crear la instancia aquí puede ser beneficioso si la configuración es costosa
# Pero hay que tener cuidado con el estado compartido si el cliente no es thread-safe
# El cliente de google-generativeai debería ser seguro para usar así.
gemini_client = GeminiClient()

async def get_gemini_client() -> GeminiClient:
    """Dependency para inyectar el cliente Gemini."""
    # Podría añadir lógica aquí si la inicialización necesita ser por request
    # o si se necesita verificar el estado del cliente global.
    if not gemini_client.model:
         raise RuntimeError("Gemini client failed to initialize during startup.")
    return gemini_client