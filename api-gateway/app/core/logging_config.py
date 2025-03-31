# api-gateway/app/core/logging_config.py
import logging
import sys
import structlog
import os
from app.core.config import settings # Importar settings ya parseadas

def setup_logging():
    """Configura el logging estructurado con structlog."""

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        # Añadir info de proceso/thread si es útil
        # structlog.processors.ProcessInfoProcessor(),
        # structlog.processors.ThreadLocalContextProcessor(),
    ]

    # Add caller info only in debug mode for performance
    if settings.LOG_LEVEL.upper() == "DEBUG":
         shared_processors.append(structlog.processors.CallsiteParameterAdder(
             {
                 structlog.processors.CallsiteParameter.FILENAME,
                 structlog.processors.CallsiteParameter.LINENO,
                 structlog.processors.CallsiteParameter.FUNC_NAME,
             }
         ))

    # Configure structlog processors for eventual output
    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Configure the formatter for stdlib logging handler
    formatter = structlog.stdlib.ProcessorFormatter(
        # These run ONCE per log event before formatting
        foreign_pre_chain=shared_processors,
        # These run on the final structured dict before rendering
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(), # Render logs as JSON
        ],
    )

    # Configure root logger handler (usually StreamHandler to stdout)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger() # Obtener root logger

    # Evitar añadir handlers duplicados
    if not any(isinstance(h, logging.StreamHandler) and isinstance(h.formatter, structlog.stdlib.ProcessorFormatter) for h in root_logger.handlers):
        # Limpiar handlers existentes si es la primera vez que configuramos (opcional, cuidado)
        # root_logger.handlers.clear()
        root_logger.addHandler(handler)

    # Establecer nivel en el root logger
    root_logger.setLevel(settings.LOG_LEVEL.upper())

    # Silenciar librerías verbosas
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("gunicorn").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    log = structlog.get_logger("api_gateway") # Logger específico para el gateway
    log.info("Structlog logging configured for API Gateway", log_level=settings.LOG_LEVEL)