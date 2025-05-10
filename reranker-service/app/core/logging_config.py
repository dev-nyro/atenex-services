# reranker-service/app/core/logging_config.py
import logging
import sys
import structlog
import os # Import os to check for Gunicorn environment

# Import settings from the current service's config module
from app.core.config import settings

def setup_logging():
    """Configures structured logging using structlog for the Reranker Service."""

    log_level_str = settings.LOG_LEVEL.upper()
    log_level_int = getattr(logging, log_level_str, logging.INFO)

    # Common processors for structlog
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.StackInfoRenderer(), # For tracebacks
        structlog.dev.set_exc_info, # Add exception info if present
        structlog.processors.TimeStamper(fmt="iso", utc=True), # ISO format timestamps in UTC
    ]

    # Add callsite parameters only if log level is DEBUG for performance
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

    # Configure structlog
    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger, # Standard bound logger
        cache_logger_on_first_use=True,
    )

    # Configure the stdlib formatter for output
    # This formatter will process the already structured log records from structlog
    formatter = structlog.stdlib.ProcessorFormatter(
        # Processor for formatting the records from structlog before rendering.
        foreign_pre_chain=shared_processors, # Apply shared processors to non-structlog records too
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta, # Remove structlog's internal keys
            structlog.processors.JSONRenderer(), # Render the final log record as JSON
            # For development, you might prefer:
            # structlog.dev.ConsoleRenderer(colors=True),
        ],
    )

    # Get the root logger
    root_logger = logging.getLogger()
    # Clear any existing handlers to avoid duplicate logs, especially in Gunicorn/Uvicorn
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # Add a new StreamHandler with our configured formatter
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level_int)

    # Configure levels for noisy libraries
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING) # Access logs can be very verbose
    logging.getLogger("gunicorn.error").setLevel(logging.INFO) # Gunicorn's own error logs
    logging.getLogger("httpx").setLevel(logging.WARNING) # HTTP client library
    logging.getLogger("sentence_transformers").setLevel(logging.INFO) # Can be verbose
    logging.getLogger("torch").setLevel(logging.INFO) # PyTorch
    logging.getLogger("transformers.modeling_utils").setLevel(logging.WARNING) # Suppress download messages unless error

    # Get a logger specific to this service after configuration
    log = structlog.get_logger(settings.PROJECT_NAME.lower().replace(" ", "-"))
    log.info(
        "Logging configured for Reranker Service",
        log_level=log_level_str,
        json_logs_enabled=True # Assuming JSONRenderer is used
    )