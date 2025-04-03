# File: app/utils/supabase_admin.py
# api-gateway/app/utils/supabase_admin.py
from supabase.client import Client, create_client
from functools import lru_cache
import structlog
from typing import Optional # Para el tipo de retorno

from app.core.config import settings # Importar settings validadas

log = structlog.get_logger(__name__)

# Usar lru_cache para crear el cliente una sola vez por proceso/worker
# maxsize=None significa caché ilimitada (o 1 si solo hay un worker)
@lru_cache(maxsize=None)
def get_supabase_admin_client() -> Client: # Cambiado Optional[Client] a Client, lanzará error si falla
    """
    Crea y devuelve un cliente Supabase inicializado con la Service Role Key.
    Utiliza caché (lru_cache) para devolver la misma instancia.
    Lanza una excepción si la configuración es inválida o falla la inicialización.

    Returns:
        Instancia del cliente Supabase Admin (supabase.Client).

    Raises:
        ValueError: Si la configuración es inválida o la creación del cliente falla.
    """
    supabase_url = str(settings.SUPABASE_URL) # Convertir HttpUrl a string
    service_key = settings.SUPABASE_SERVICE_ROLE_KEY

    # Las validaciones de existencia y no-default ya se hicieron en config.py

    log.info("Attempting to initialize Supabase Admin Client...")
    try:
        # Crear el cliente Supabase
        supabase_admin: Client = create_client(supabase_url, service_key)

        # Optional: Intenta una operación simple para verificar que la clave funciona.
        # Esto añade una pequeña latencia al inicio pero aumenta la confianza.
        # Ejemplo: Listar usuarios con límite 0 (solo verifica la conexión/permiso)
        try:
            # Nota: Esta llamada es síncrona en supabase-py v1, necesita ser async en v2
            # Asumiendo v2+ y que estamos en un contexto async para la verificación inicial
            # Esto realmente no puede hacerse aquí fácilmente en una función síncrona cacheada.
            # La verificación real ocurrirá en el primer uso en una ruta async.
            # response = await supabase_admin.auth.admin.list_users(limit=0) # Necesitaría ser async
            # log.info(f"Supabase Admin Client connection appears valid (basic check).")
            pass # Saltamos la verificación activa aquí
        except Exception as test_e:
             # Esto probablemente no se ejecute aquí. El error ocurrirá en el primer uso.
             log.warning(f"Supabase Admin Client test query failed (will likely fail on first use): {test_e}", exc_info=False)
             # Podrías lanzar el error aquí si quieres que falle al inicio
             # raise ValueError(f"Supabase Admin Client test query failed: {test_e}") from test_e

        log.info("Supabase Admin Client initialized successfully (pending first use validation).")
        return supabase_admin

    except Exception as e:
        # Capturar cualquier error durante create_client
        log.exception("FATAL: Failed to initialize Supabase Admin Client", error=str(e))
        # Lanzar una excepción explícita para notificar el fallo claramente
        raise ValueError(f"FATAL: Failed to initialize Supabase Admin Client: {e}") from e

# Nota: No se crea una instancia global aquí. La instancia se crea y cachea
# cuando get_supabase_admin_client() es llamada por primera vez (ej. como dependencia).
# Si la función falla, la excepción se propagará a través de Depends().