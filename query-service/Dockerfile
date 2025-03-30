# Fase 1: Construir el entorno virtual con Poetry
# Usar 'as' en minúsculas para consistencia
FROM python:3.10-slim as builder

# Argumentos para versiones y UID/GID
ARG POETRY_VERSION=1.7.1
ARG APP_USER=nonroot
ARG APP_UID=1001
ARG APP_GID=1001

# Instalar dependencias del sistema (si fueran necesarias)
# RUN apt-get update && apt-get install -y --no-install-recommends some-package && rm -rf /var/lib/apt/lists/*

# Instalar Poetry
ENV POETRY_VERSION=${POETRY_VERSION}
# Actualizar pip y luego instalar poetry
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir poetry==${POETRY_VERSION}

# Configurar Poetry para no crear venvs dentro del proyecto
RUN poetry config virtualenvs.create false

# Establecer directorio de trabajo
WORKDIR /app

# Crear usuario y grupo no root ANTES de copiar archivos
RUN groupadd -g ${APP_GID} ${APP_USER} && \
    useradd -u ${APP_UID} -g ${APP_GID} -ms /bin/sh ${APP_USER}

# Copiar solo los archivos de dependencias primero para aprovechar el cache de Docker
# Asegurar que los permisos son para el usuario no root
# !! ESTE PASO REQUIERE QUE poetry.lock EXISTA EN EL REPOSITORIO !!
COPY --chown=${APP_USER}:${APP_USER} pyproject.toml poetry.lock ./

# Cambiar al usuario no root ANTES de instalar dependencias
USER ${APP_USER}

# Instalar dependencias (sin dev) usando Poetry
RUN poetry install --no-dev --no-interaction --no-ansi --sync

# Fase 2: Crear la imagen final de producción
FROM python:3.10-slim

# Argumentos para UID/GID (deben coincidir con la fase builder)
ARG APP_USER=nonroot
ARG APP_UID=1001
ARG APP_GID=1001

# Recrear usuario y grupo no root en la imagen final
RUN groupadd -g ${APP_GID} ${APP_USER} && \
    useradd -u ${APP_UID} -g ${APP_GID} -ms /bin/sh ${APP_USER}

# Establecer directorio de trabajo
WORKDIR /app

# Establecer variables de entorno
ENV PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    # Definir PYTHONPATH explícitamente (corregido)
    PYTHONPATH=/app/.venv/lib/python3.10/site-packages \
    # Puerto por defecto (puede ser sobreescrito por K8s env var si se define)
    PORT=8001

# Copiar el entorno virtual instalado desde la fase builder
COPY --from=builder --chown=${APP_USER}:${APP_USER} /app/.venv /app/.venv

# Copiar el código de la aplicación
COPY --chown=${APP_USER}:${APP_USER} app ./app

# Cambiar al usuario no root para la ejecución
USER ${APP_USER}

# Exponer el puerto en el que correrá la aplicación
EXPOSE ${PORT}

# Comando para correr la aplicación con Gunicorn
# Usar el puerto definido en la variable de entorno
# Ajustar número de workers (-w) según sea necesario (e.g., 2 * num_cores + 1)
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "4", "-b", "0.0.0.0:${PORT}", "app.main:app"]

# Comando alternativo para correr con Uvicorn directamente
# CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "${PORT}"]