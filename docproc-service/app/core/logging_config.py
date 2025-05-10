import logging
import sys
import structlog
from app.core.config import settings # type: ignore
import os

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
    
    # Clear existing handlers only if we're not in a managed environment that might set them up (like Gunicorn)
    # A simple check is if any handlers are already StreamHandlers to stdout.
    # This avoids duplicate logs when Uvicorn/Gunicorn also configures logging.
    is_stdout_handler_present = any(
        isinstance(h, logging.StreamHandler) and h.stream == sys.stdout
        for h in root_logger.handlers
    )
    if not is_stdout_handler_present:
        # Clear all handlers if no specific stdout handler detected, to avoid conflicts
        # But this might be too aggressive if other handlers are desired (e.g. file logger from a lib)
        # For service logging, focusing on stdout is usually fine.
        # root_logger.handlers.clear() # Commented out to be less aggressive
        root_logger.addHandler(handler)
    elif not root_logger.handlers: # If no handlers at all, add ours.
        root_logger.addHandler(handler)


    root_logger.setLevel(settings.LOG_LEVEL)

    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("gunicorn.error").setLevel(logging.WARNING) # Gunicorn logs to stderr by default
    logging.getLogger("httpx").setLevel(logging.WARNING)
    
    # For PyMuPDF, set to WARNING to avoid too many debug messages unless needed
    logging.getLogger("fitz").setLevel(logging.WARNING)


    log = structlog.get_logger(settings.PROJECT_NAME)
    log.info("Logging configured", log_level=settings.LOG_LEVEL)