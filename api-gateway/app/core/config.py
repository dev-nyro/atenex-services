# api-gateway/app/core/config.py
import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
import sys
import logging
from typing import Optional

K8S_INGEST_SVC_URL_DEFAULT = "http://ingest-api-service.nyro-develop.svc.cluster.local:80"
K8S_QUERY_SVC_URL_DEFAULT = "http://query-service.nyro-develop.svc.cluster.local:80"

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Lee variables con el prefijo GATEWAY_ desde .env o el entorno
        env_file='.env',
        env_prefix='GATEWAY_',
        case_sensitive=False,
        env_file_encoding='utf-8',
        extra='ignore'
    )

    PROJECT_NAME: str = "Nyro API Gateway"
    API_V1_STR: str = "/api/v1"

    INGEST_SERVICE_URL: str # Se leerá como GATEWAY_INGEST_SERVICE_URL
    QUERY_SERVICE_URL: str  # Se leerá como GATEWAY_QUERY_SERVICE_URL
    AUTH_SERVICE_URL: Optional[str] = None # Se leerá como GATEWAY_AUTH_SERVICE_URL

    # JWT settings
    JWT_SECRET: str # Se leerá como GATEWAY_JWT_SECRET (desde Secret)
    JWT_ALGORITHM: str = "HS256" # Se leerá como GATEWAY_JWT_ALGORITHM

    # Supabase Admin settings
    # --- MODIFICACIÓN: Leer variable específica del backend ---
    # Leerá la variable de entorno GATEWAY_SUPABASE_URL
    SUPABASE_URL: str
    # Leerá la variable de entorno GATEWAY_SUPABASE_SERVICE_ROLE_KEY (desde Secret)
    SUPABASE_SERVICE_ROLE_KEY: str
    # -------------------------------------------------------

    # Leerá la variable de entorno GATEWAY_DEFAULT_COMPANY_ID
    DEFAULT_COMPANY_ID: Optional[str] = None

    LOG_LEVEL: str = "INFO"
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS: int = 20
    HTTP_CLIENT_MAX_CONNECTIONS: int = 100

@lru_cache()
def get_settings() -> Settings:
    log = logging.getLogger(__name__)
    if not log.handlers:
        log.setLevel(logging.INFO)
        log.addHandler(logging.StreamHandler(sys.stdout))

    log.info("Loading Gateway settings...")
    try:
        # Pydantic-settings leerá las variables de entorno correspondientes
        # (GATEWAY_SUPABASE_URL, GATEWAY_JWT_SECRET, etc.)
        settings_instance = Settings()

        log.info("Gateway Settings Loaded:")
        log.info(f"  PROJECT_NAME: {settings_instance.PROJECT_NAME}")
        log.info(f"  INGEST_SERVICE_URL: {settings_instance.INGEST_SERVICE_URL}")
        log.info(f"  QUERY_SERVICE_URL: {settings_instance.QUERY_SERVICE_URL}")
        log.info(f"  AUTH_SERVICE_URL: {settings_instance.AUTH_SERVICE_URL or 'Not Set'}")

        # Verificación JWT Secret (Placeholder)
        if settings_instance.JWT_SECRET == "YOUR_DEFAULT_JWT_SECRET_KEY_CHANGE_ME_IN_ENV_OR_SECRET":
            log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            log.critical("! FATAL: GATEWAY_JWT_SECRET is using the default insecure value!")
            log.critical("! Set GATEWAY_JWT_SECRET via env var or K8s Secret.")
            log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        else:
            log.info(f"  JWT_SECRET: *** SET (Loaded from Secret/Env) ***")
        log.info(f"  JWT_ALGORITHM: {settings_instance.JWT_ALGORITHM}")

        # Verificaciones Supabase URL y Service Key
        # --- MODIFICACIÓN: Usar el nombre correcto de la variable ---
        if not settings_instance.SUPABASE_URL:
             # Esto no debería pasar si es obligatorio y no tiene default, pydantic fallaría antes.
             # Pero mantenemos la verificación por si acaso.
             log.critical("! FATAL: GATEWAY_SUPABASE_URL is not configured.")
             sys.exit("FATAL: GATEWAY_SUPABASE_URL not configured.")
        else:
             log.info(f"  SUPABASE_URL: {settings_instance.SUPABASE_URL}")
        # -----------------------------------------------------------

        # Verificación Service Key (Placeholder)
        if settings_instance.SUPABASE_SERVICE_ROLE_KEY == "YOUR_SUPABASE_SERVICE_ROLE_KEY_HERE":
            log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            log.critical("! FATAL: GATEWAY_SUPABASE_SERVICE_ROLE_KEY is using the default placeholder!")
            log.critical("! Set GATEWAY_SUPABASE_SERVICE_ROLE_KEY via env var or K8s Secret.")
            log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        else:
            log.info("  SUPABASE_SERVICE_ROLE_KEY: *** SET (Loaded from Secret/Env) ***")

        # Verificación Default Company ID
        if not settings_instance.DEFAULT_COMPANY_ID:
            log.warning("! WARNING: GATEWAY_DEFAULT_COMPANY_ID is not set. Company association might fail.")
        else:
            log.info(f"  DEFAULT_COMPANY_ID: {settings_instance.DEFAULT_COMPANY_ID}")

        log.info(f"  LOG_LEVEL: {settings_instance.LOG_LEVEL}")
        log.info(f"  HTTP_CLIENT_TIMEOUT: {settings_instance.HTTP_CLIENT_TIMEOUT}")
        return settings_instance
    except Exception as e:
        # Captura errores de validación de Pydantic o cualquier otro error al cargar
        log.exception(f"FATAL: Error loading/validating Gateway settings: {e}")
        sys.exit(f"FATAL: Error loading/validating Gateway settings: {e}")

settings = get_settings()