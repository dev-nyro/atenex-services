# api-gateway/app/core/config.py
import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
import sys
import logging
from typing import Optional
# (-) Quitar la importación de SecretStr si ya no se usa en ningún otro lugar
# from pydantic import SecretStr

K8S_INGEST_SVC_URL_DEFAULT = "http://ingest-api-service.nyro-develop.svc.cluster.local:80"
K8S_QUERY_SVC_URL_DEFAULT = "http://query-service.nyro-develop.svc.cluster.local:80"

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file='.env',
        env_prefix='GATEWAY_',
        case_sensitive=False,
        env_file_encoding='utf-8',
        extra='ignore'
    )

    PROJECT_NAME: str = "Nyro API Gateway"
    API_V1_STR: str = "/api/v1"

    INGEST_SERVICE_URL: str = os.getenv("GATEWAY_INGEST_SERVICE_URL", K8S_INGEST_SVC_URL_DEFAULT)
    QUERY_SERVICE_URL: str = os.getenv("GATEWAY_QUERY_SERVICE_URL", K8S_QUERY_SVC_URL_DEFAULT)
    AUTH_SERVICE_URL: Optional[str] = os.getenv("GATEWAY_AUTH_SERVICE_URL")

    # JWT settings
    JWT_SECRET: str = "YOUR_DEFAULT_JWT_SECRET_KEY_CHANGE_ME_IN_ENV_OR_SECRET"
    JWT_ALGORITHM: str = "HS256"

    # --- MODIFICACIÓN: Cambiar tipo a str ---
    # La clave se leerá como una string normal desde la variable de entorno.
    # La seguridad recae en cómo se inyecta esa variable (ej. K8s Secret).
    SUPABASE_SERVICE_ROLE_KEY: str = "YOUR_SUPABASE_SERVICE_ROLE_KEY_HERE"
    # ----------------------------------------
    SUPABASE_URL: Optional[str] = os.getenv("NEXT_PUBLIC_SUPABASE_URL") # Reutilizar la URL pública

    DEFAULT_COMPANY_ID: Optional[str] = os.getenv("GATEWAY_DEFAULT_COMPANY_ID")

    LOG_LEVEL: str = "INFO"
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS: int = 20
    HTTP_CLIENT_MAX_CONNECTIONS: int = 100

@lru_cache()
def get_settings() -> Settings:
    log = logging.getLogger(__name__)
    # Asegurarse de que el logger esté configurado antes de usarlo
    if not log.handlers:
        log.setLevel(logging.INFO)
        log.addHandler(logging.StreamHandler(sys.stdout))

    log.info("Loading Gateway settings...")
    try:
        settings_instance = Settings()
        log.info("Gateway Settings Loaded:")
        log.info(f"  PROJECT_NAME: {settings_instance.PROJECT_NAME}")
        log.info(f"  INGEST_SERVICE_URL: {settings_instance.INGEST_SERVICE_URL}")
        log.info(f"  QUERY_SERVICE_URL: {settings_instance.QUERY_SERVICE_URL}")
        log.info(f"  AUTH_SERVICE_URL: {settings_instance.AUTH_SERVICE_URL or 'Not Set'}")

        # Verificación JWT Secret
        if settings_instance.JWT_SECRET == "YOUR_DEFAULT_JWT_SECRET_KEY_CHANGE_ME_IN_ENV_OR_SECRET":
            log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            log.critical("! FATAL: GATEWAY_JWT_SECRET is using the default insecure value!")
            log.critical("! Set GATEWAY_JWT_SECRET via env var or K8s Secret.")
            log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        else:
            log.info(f"  JWT_SECRET: *** SET (Loaded from Secret/Env) ***")
        log.info(f"  JWT_ALGORITHM: {settings_instance.JWT_ALGORITHM}")

        # Verificaciones Supabase URL y Service Key
        if not settings_instance.SUPABASE_URL:
             log.critical("! FATAL: Supabase URL is not configured (check NEXT_PUBLIC_SUPABASE_URL env var).")
             sys.exit("FATAL: Supabase URL not configured.")
        else:
             log.info(f"  SUPABASE_URL: {settings_instance.SUPABASE_URL}")

        # --- MODIFICACIÓN: Validar la clave de servicio como string ---
        # Comprobar si sigue siendo el valor placeholder.
        if settings_instance.SUPABASE_SERVICE_ROLE_KEY == "YOUR_SUPABASE_SERVICE_ROLE_KEY_HERE":
            log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            log.critical("! FATAL: GATEWAY_SUPABASE_SERVICE_ROLE_KEY is using the default placeholder!")
            log.critical("! Set GATEWAY_SUPABASE_SERVICE_ROLE_KEY via env var or K8s Secret.")
            log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            # Considera salir si es crítico
            # sys.exit("FATAL: GATEWAY_SUPABASE_SERVICE_ROLE_KEY not configured securely.")
        else:
            # No loguear la clave, solo confirmar que está configurada.
            log.info("  SUPABASE_SERVICE_ROLE_KEY: *** SET (Loaded from Secret/Env) ***")
        # -------------------------------------------------------------

        # Verificación Default Company ID
        if not settings_instance.DEFAULT_COMPANY_ID:
            log.warning("! WARNING: GATEWAY_DEFAULT_COMPANY_ID is not set. Company association might fail.")
        else:
            log.info(f"  DEFAULT_COMPANY_ID: {settings_instance.DEFAULT_COMPANY_ID}")

        log.info(f"  LOG_LEVEL: {settings_instance.LOG_LEVEL}")
        log.info(f"  HTTP_CLIENT_TIMEOUT: {settings_instance.HTTP_CLIENT_TIMEOUT}")
        return settings_instance
    except Exception as e:
        log.exception(f"FATAL: Error loading Gateway settings: {e}")
        sys.exit(f"FATAL: Error loading Gateway settings: {e}")

settings = get_settings()