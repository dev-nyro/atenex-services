# Fase 1: Construir el entorno virtual con Poetry
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
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir poetry==${POETRY_VERSION}

# Configurar Poetry para no crear venvs dentro del proyecto
RUN poetry config virtualenvs.create false

WORKDIR /app

# Crear usuario y grupo no root
RUN groupadd -g ${APP_GID} ${APP_USER} && \
    useradd -u ${APP_UID} -g ${APP_GID} -ms /bin/sh ${APP_USER}

# Copiar solo los archivos de dependencias primero para aprovechar el cache de Docker
COPY --chown=${APP_USER}:${APP_USER} pyproject.toml poetry.lock ./

# Instalar dependencias (sin dev) usando Poetry
# Ejecutar como el usuario de la app para que los permisos sean correctos
USER ${APP_USER}
RUN poetry install --no-dev --no-interaction --no-ansi --sync

# Fase 2: Crear la imagen final de producción
FROM python:3.10-slim

# Argumentos para UID/GID (deben coincidir con la fase builder)
ARG APP_USER=nonroot
ARG APP_UID=1001
ARG APP_GID=1001

# Recrear usuario y grupo no root en la imagen final
# (necesario porque no se copian de la fase builder)
RUN groupadd -g ${APP_GID} ${APP_USER} && \
    useradd -u ${APP_UID} -g ${APP_GID} -ms /bin/sh ${APP_USER}

WORKDIR /app

# Establecer variables de entorno
ENV PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    # Añadir el path de los paquetes instalados por Poetry
    # (El path puede variar ligeramente dependiendo de cómo poetry install lo maneje sin venv create)
    # Ajustar si es necesario inspeccionando la imagen builder
    PYTHONPATH=/app/.venv/lib/python3.10/site-packages:$PYTHONPATH \
    # Puerto por defecto (puede ser sobreescrito)
    PORT=8001

# Copiar el entorno virtual instalado desde la fase builder
# Asegurar que los permisos sean correctos
COPY --from=builder --chown=${APP_USER}:${APP_USER} /app/.venv /app/.venv

# Copiar el código de la aplicación
# Asegurar que los permisos sean correctos
COPY --chown=${APP_USER}:${APP_USER} app ./app

# Cambiar al usuario no root
USER ${APP_USER}

# Exponer el puerto en el que correrá la aplicación
EXPOSE ${PORT}

# Comando para correr la aplicación con Gunicorn
# Usar el puerto definido en la variable de entorno
# Ajustar número de workers (-w) según sea necesario (e.g., 2 * num_cores + 1)
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "4", "-b", "0.0.0.0:${PORT}", "app.main:app"]

# Comando alternativo para correr con Uvicorn directamente
# CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "${PORT}"]