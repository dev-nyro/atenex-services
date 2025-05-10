# reranker-service/app/core/logging_config.py
import logging
import sys
import structlog
from app.core.config import settings # Importar settings del servicio actual

def setup_logging():
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
    
    # Evitar duplicar handlers si uvicorn/gunicorn ya configuró uno básico
    # Esta comprobación es simple; podría ser más robusta
    if not any(isinstance(h.formatter, structlog.stdlib.ProcessorFormatter) for h in root_logger.handlers):
        if root_logger.hasHandlers():
            root_logger.handlers.clear() # Limpiar handlers existentes si vamos a añadir el nuestro
        root_logger.addHandler(handler)
    
    root_logger.setLevel(settings.LOG_LEVEL.upper())

    # Silenciar librerías verbosas
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("gunicorn").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("sentence_transformers").setLevel(logging.INFO) # O WARNING
    logging.getLogger("torch").setLevel(logging.INFO) # O WARNING
    logging.getLogger("transformers").setLevel(logging.WARNING) # Muy verboso en DEBUG/INFO

    log = structlog.get_logger("reranker_service")
    log.info("Logging configured for Reranker Service", log_level=settings.LOG_LEVEL)