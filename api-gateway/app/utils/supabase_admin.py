# api-gateway/app/utils/supabase_admin.py
from supabase.client import Client, create_client
# from supabase.lib.client_options import ClientOptions # No necesitamos options por ahora
from functools import lru_cache
import structlog

from app.core.config import settings

log = structlog.get_logger(__name__)

@lru_cache()
def get_supabase_admin_client() -> Client:
    """
    Crea y devuelve un cliente Supabase inicializado con la Service Role Key.
    Utiliza caché para devolver la misma instancia.

    Raises:
        ValueError: Si la URL de Supabase o la Service Role Key no están configuradas.

    Returns:
        Instancia del cliente Supabase Admin.
    """
    supabase_url = settings.SUPABASE_URL
    # --- MODIFICACIÓN: Acceder a la clave como string normal ---
    service_key = settings.SUPABASE_SERVICE_ROLE_KEY
    # ----------------------------------------------------------

    if not supabase_url:
        log.critical("Supabase URL is not configured for Admin Client.")
        raise ValueError("Supabase URL not configured in settings.")
    # --- MODIFICACIÓN: Validar el placeholder como string ---
    if not service_key or service_key == "YOUR_SUPABASE_SERVICE_ROLE_KEY_HERE":
        log.critical("Supabase Service Role Key is not configured or using default for Admin Client.")
        raise ValueError("Supabase Service Role Key not configured securely in settings.")
    # -------------------------------------------------------

    log.info("Initializing Supabase Admin Client...")
    try:
        # Pasar la clave como string directamente
        supabase_admin: Client = create_client(supabase_url, service_key)
        log.info("Supabase Admin Client initialized successfully.")
        return supabase_admin
    except Exception as e:
        log.exception("Failed to initialize Supabase Admin Client", error=e)
        raise ValueError(f"Failed to initialize Supabase Admin Client: {e}")

# Instancia global (opcional, pero común con lru_cache)
# supabase_admin_client = get_supabase_admin_client() # Podría llamarse aquí o bajo demanda