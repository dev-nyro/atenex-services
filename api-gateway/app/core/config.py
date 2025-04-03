# File: app/core/logging_config.py
# api-gateway/app/core/logging_config.py
import logging
import sys
import structlog
import os
from app.core.config import settings # Importar settings ya parseadas y validadas

def setup_logging():
    """Configura el logging estructurado con structlog para salida JSON."""

    # Procesadores compartidos por structlog y logging stdlib
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars, # Añadir contexto de structlog.contextvars
        structlog.stdlib.add_logger_name, # Añadir nombre del logger
        structlog.stdlib.add_log_level, # Añadir nivel del log (info, warning, etc.)
        structlog.processors.TimeStamper(fmt="iso", utc=True), # Timestamp ISO 8601 en UTC
        structlog.processors.StackInfoRenderer(), # Renderizar info de stack si está presente
        # structlog.processors.format_exc_info, # Formatear excepciones (JSONRenderer lo hace bien)
        # Añadir info de proceso/thread si es útil para depurar concurrencia
        # structlog.processors.ProcessInfoProcessor(),
    ]

    # Añadir información del llamador (fichero, línea) SOLO en modo DEBUG por rendimiento
    if settings.LOG_LEVEL.upper() == "DEBUG":
         # Usar CallsiteParameterAdder para añadir selectivamente
         shared_processors.append(structlog.processors.CallsiteParameterAdder(
             parameters={
                 structlog.processors.CallsiteParameter.FILENAME,
                 structlog.processors.CallsiteParameter.LINENO,
                 # structlog.processors.CallsiteParameter.FUNC_NAME, # Opcional
             }
         ))

    # Configurar structlog para usar el sistema de logging estándar de Python
    structlog.configure(
        processors=shared_processors + [
            # Prepara el diccionario de eventos para el formateador de stdlib logging
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(), # Usar logger factory de stdlib
        wrapper_class=structlog.stdlib.BoundLogger, # Clase wrapper estándar
        cache_logger_on_first_use=True, # Cachear loggers para rendimiento
    )

    # Configurar el formateador que usará el handler de stdlib logging
    # Este formateador tomará el diccionario preparado por structlog y lo renderizará
    formatter = structlog.stdlib.ProcessorFormatter(
        # Procesador final que renderiza el diccionario de eventos
        # Usar JSONRenderer para salida estructurada compatible con agregadores de logs
        processor=structlog.processors.JSONRenderer(),
        # Alternativa para desarrollo local (logs más legibles en consola):
        # processor=structlog.dev.ConsoleRenderer(colors=True), # Requiere 'pip install colorama'

        # Procesadores que se ejecutan ANTES del renderizador final (JSONRenderer/ConsoleRenderer)
        # foreign_pre_chain se aplica a logs de librerías que usan logging directamente
        foreign_pre_chain=shared_processors + [
             structlog.stdlib.ProcessorFormatter.remove_processors_meta, # Limpiar metadatos internos
        ],
    )

    # Configurar el handler raíz de logging (salida a stdout)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter) # Usar el formateador de structlog

    root_logger = logging.getLogger() # Obtener el logger raíz

    # Evitar añadir handlers duplicados si esta función se llama accidentalmente más de una vez
    # Comprobar si ya existe un handler con nuestro formateador específico
    has_structlog_handler = any(
        isinstance(h, logging.StreamHandler) and isinstance(h.formatter, structlog.stdlib.ProcessorFormatter)
        for h in root_logger.handlers
    )

    if not has_structlog_handler:
        # Limpiar handlers existentes (opcional, puede ser destructivo si otros módulos configuran logging)
        # root_logger.handlers.clear()
        root_logger.addHandler(handler)
    else:
        # Si ya existe, al menos asegurar que el formateador esté actualizado (por si acaso)
        for h in root_logger.handlers:
            if isinstance(h, logging.StreamHandler) and isinstance(h.formatter, structlog.stdlib.ProcessorFormatter):
                h.setFormatter(formatter)
                break

    # Establecer el nivel de log en el logger raíz basado en la configuración
    try:
        root_logger.setLevel(settings.LOG_LEVEL.upper())
    except ValueError:
        # Esto no debería ocurrir si el validador en config.py funciona
        root_logger.setLevel(logging.INFO)
        logging.warning(f"Invalid LOG_LEVEL '{settings.LOG_LEVEL}' detected after validation. Defaulting to INFO.")


    # Ajustar niveles de log para librerías de terceros verbosas
    # Poner en WARNING o ERROR para reducir ruido, INFO/DEBUG si necesitas sus logs
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO) # Errores de uvicorn sí son importantes
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING) # Logs de acceso pueden ser muy ruidosos
    logging.getLogger("gunicorn.error").setLevel(logging.INFO)
    logging.getLogger("gunicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING) # Logs de httpx suelen ser verbosos
    logging.getLogger("jose").setLevel(logging.INFO) # JWTs fallidos pueden ser INFO/WARNING
    logging.getLogger("asyncio").setLevel(logging.WARNING) # Evitar logs internos de asyncio
    logging.getLogger("watchfiles").setLevel(logging.WARNING) # Si usas --reload

    # Log inicial para confirmar que la configuración se aplicó
    # Usar un logger específico para la configuración del logging
    log_config_logger = structlog.get_logger("api_gateway.logging_config")
    log_config_logger.info("Structlog logging configured", log_level=settings.LOG_LEVEL.upper(), output_format="JSON") # O Console si se usa ConsoleRenderer