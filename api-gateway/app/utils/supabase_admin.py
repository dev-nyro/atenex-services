# File: app/utils/supabase_admin.py
# api-gateway/app/utils/supabase_admin.py
from supabase.client import Client, create_client
# from supabase.lib.client_options import ClientOptions # Opciones no usadas actualmente
from functools import lru_cache
import structlog
from typing import Optional # Para el tipo de retorno

from app.core.config import settings # Importar settings validadas

log = structlog.get_logger(__name__)

# Usar lru_cache para crear el cliente una sola vez por proceso/worker
@lru_cache()
def get_supabase_admin_client() -> Optional[Client]:
    """
    Crea y devuelve un cliente Supabase inicializado con la Service Role Key.
    Utiliza caché (lru_cache) para devolver la misma instancia en llamadas subsiguientes
    dentro del mismo proceso. Devuelve None si la configuración es inválida.

    Returns:
        Instancia del cliente Supabase Admin (supabase.Client) o None si falla la inicialización.
    """
    supabase_url = settings.SUPABASE_URL
    service_key = settings.SUPABASE_SERVICE_ROLE_KEY

    # Las validaciones de existencia y no-default ya se hicieron en config.py
    # Si llegamos aquí, SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY son válidos (no None, no default)

    log.info("Attempting to initialize Supabase Admin Client...")
    try:
        # Crear el cliente Supabase usando la URL y la Service Role Key
        # Nota: create_client puede lanzar excepciones si los args son inválidos,
        # aunque pydantic ya debería haberlos validado.
        supabase_admin: Client = create_client(supabase_url, service_key)

        # Validación adicional (opcional pero recomendada):
        # Intentar una operación simple para verificar que la clave funciona.
        # Cuidado: esto puede consumir recursos o fallar si los permisos son mínimos.
        # Ejemplo: Listar tablas del schema 'auth' (requiere permisos de admin)
        # try:
        #     response = await supabase_admin.table('users').select('id', head=True, count='exact').limit(0).execute() # Solo contar, sin traer datos
        #     log.info(f"Supabase Admin Client connection verified. Found {response.count} users.")
        # except Exception as test_e:
        #     log.warning(f"Supabase Admin Client initialized, but test query failed: {test_e}. Check Service Role Key permissions.", exc_info=True)
        #     # Decidir si continuar o devolver None/lanzar error si la verificación es crítica

        log.info("Supabase Admin Client initialized successfully.")
        return supabase_admin

    except Exception as e:
        # Capturar cualquier error durante create_client o la verificación opcional
        log.exception("FATAL: Failed to initialize Supabase Admin Client", error=str(e))
        # Lanzar una excepción explícita para notificar el fallo
        raise ValueError("FATAL: Failed to initialize Supabase Admin Client. Check configuration and permissions.")

# Nota: No se crea una instancia global aquí. La instancia se crea y cachea
# cuando get_supabase_admin_client() es llamada por primera vez (ej. en lifespan o como dependencia).