# sparse-search-service/app/core/logging_config.py
import logging
import sys
import structlog
from app.core.config import settings # Asegúrate que 'settings' se cargue correctamente

def setup_logging():
    """Configura el logging estructurado con structlog."""
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.LOG_LEVEL == "DEBUG":
         shared_processors.append(structlog.processors.CallsiteParameterAdder(
             {
                 structlog.processors.CallsiteParameter.FILENAME,
                 structlog.processors.CallsiteParameter.LINENO,
             }
         ))

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()

    # Evitar añadir handler múltiples veces
    if not any(isinstance(h, logging.StreamHandler) and isinstance(h.formatter, structlog.stdlib.ProcessorFormatter) for h in root_logger.handlers):
        # root_logger.handlers.clear() # Descomentar con precaución
        root_logger.addHandler(handler)

    # Establecer el nivel de log ANTES de que structlog intente usarlo
    try:
        effective_log_level = settings.LOG_LEVEL.upper()
        if effective_log_level not in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
            effective_log_level = "INFO" # Fallback seguro
            logging.getLogger("sparse_search_service_early_log").warning(f"Invalid LOG_LEVEL '{settings.LOG_LEVEL}', defaulting to 'INFO'.")
    except AttributeError: # Si settings aún no está completamente cargado
        effective_log_level = "INFO"
        logging.getLogger("sparse_search_service_early_log").warning("Settings not fully loaded during logging setup, defaulting log level to 'INFO'.")

    root_logger.setLevel(effective_log_level)


    # Silenciar bibliotecas verbosas
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("gunicorn").setLevel(logging.INFO)
    logging.getLogger("asyncpg").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING) # Si se usa httpx

    # Logger específico para este servicio
    log = structlog.get_logger("sparse_search_service")
    # Este log puede que no aparezca si el nivel global es más restrictivo en el momento de esta llamada
    log.info("Logging configured for Sparse Search Service", log_level=effective_log_level)