# api-gateway/app/core/config.py
import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
import sys
import logging
from typing import Optional

K8S_INGEST_SVC_URL_DEFAULT = "http://ingest-api-service.nyro-develop.svc.cluster.local:80"
K8S_QUERY_SVC_URL_DEFAULT = "http://query-service.nyro-develop.svc.cluster.local:80"
# K8S_AUTH_SVC_URL_DEFAULT = "http://auth-service.nyro-develop.svc.cluster.local:80" # Si aplica

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
    AUTH_SERVICE_URL: Optional[str] = os.getenv("GATEWAY_AUTH_SERVICE_URL") # Para proxy de auth

    # JWT settings - Leído desde Secret K8s o .env
    # IMPORTANTE: El valor por defecto aquí SÓLO debe usarse para desarrollo local
    # NUNCA debe ser el valor real en producción/k8s.
    JWT_SECRET: str = "YOUR_DEFAULT_JWT_SECRET_KEY_CHANGE_ME_IN_ENV_OR_SECRET"
    JWT_ALGORITHM: str = "HS256"

    LOG_LEVEL: str = "INFO"
    HTTP_CLIENT_TIMEOUT: int = 60
    HTTP_CLIENT_MAX_KEEPALIAS_CONNECTIONS: int = 20
    HTTP_CLIENT_MAX_CONNECTIONS: int = 100

@lru_cache()
def get_settings() -> Settings:
    log = logging.getLogger(__name__)
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
        # *** VERIFICACIÓN CRÍTICA DEL SECRETO JWT ***
        if settings_instance.JWT_SECRET == "YOUR_DEFAULT_JWT_SECRET_KEY_CHANGE_ME_IN_ENV_OR_SECRET":
            log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            log.critical("! FATAL: GATEWAY_JWT_SECRET is using the default insecure value!")
            log.critical("! Set GATEWAY_JWT_SECRET via env var or K8s Secret.")
            log.critical("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            # Considera salir si es crítico en producción:
            # if os.getenv("ENVIRONMENT") == "production":
            #     sys.exit("FATAL: GATEWAY_JWT_SECRET not configured securely.")
        else:
            log.info(f"  JWT_SECRET: *** SET (Loaded from Secret/Env) ***")
        log.info(f"  JWT_ALGORITHM: {settings_instance.JWT_ALGORITHM}")
        log.info(f"  LOG_LEVEL: {settings_instance.LOG_LEVEL}")
        log.info(f"  HTTP_CLIENT_TIMEOUT: {settings_instance.HTTP_CLIENT_TIMEOUT}")
        return settings_instance
    except Exception as e:
        log.exception(f"FATAL: Error loading Gateway settings: {e}")
        sys.exit(f"FATAL: Error loading Gateway settings: {e}")

settings = get_settings()