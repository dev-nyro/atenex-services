# reranker-service/app/core/logging_config.py
import logging
import sys
import structlog
import os 

from app.core.config import settings

def setup_logging():
    """Configures structured logging using structlog for the Reranker Service."""

    log_level_str = settings.LOG_LEVEL.upper()
    log_level_int = getattr(logging, log_level_str, logging.INFO)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.StackInfoRenderer(), 
        structlog.dev.set_exc_info, 
        structlog.processors.TimeStamper(fmt="iso", utc=True), 
    ]

    if log_level_int <= logging.DEBUG:
        shared_processors.append(
            structlog.processors.CallsiteParameterAdder(
                {
                    structlog.processors.CallsiteParameter.FILENAME,
                    structlog.processors.CallsiteParameter.LINENO,
                    structlog.processors.CallsiteParameter.FUNC_NAME,
                }
            )
        )

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

    root_logger = logging.getLogger()
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level_int)

    # MODIFICADO: Aumentar niveles de log para librerías ruidosas
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.ERROR) # Muy verboso, solo errores
    logging.getLogger("gunicorn.error").setLevel(logging.INFO) 
    logging.getLogger("httpx").setLevel(logging.WARNING) 
    logging.getLogger("sentence_transformers").setLevel(logging.WARNING) # Menos logs de sentence-transformers
    logging.getLogger("torch").setLevel(logging.WARNING) # Menos logs de PyTorch
    logging.getLogger("transformers").setLevel(logging.WARNING) # Menos logs de Hugging Face Transformers
    logging.getLogger("transformers.modeling_utils").setLevel(logging.ERROR) 

    log = structlog.get_logger(settings.PROJECT_NAME.lower().replace(" ", "-"))
    log.info(
        "Logging configured for Reranker Service",
        log_level=log_level_str,
        json_logs_enabled=True 
    )