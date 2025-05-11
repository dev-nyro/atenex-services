# reranker-service/Dockerfile

# ---- Stage 1: Builder ----
FROM python:3.10-slim AS builder # Cambiado 'as' a 'AS' para consistencia con FROM

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VERSION=1.8.2 \
    POETRY_HOME="/opt/poetry" \
    PATH="/opt/poetry/bin:$PATH" \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false # Instala paquetes en el entorno global del sistema

# 1. Instalar dependencias de sistema necesarias para instalar Poetry y compilar paquetes
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl gcc build-essential

# 2. Instalar Poetry usando curl
RUN curl -sSL https://install.python-poetry.org | python3 - --version ${POETRY_VERSION}
# Verificar que Poetry se instaló (opcional pero bueno para depurar)
RUN poetry --version

# 3. Configurar el directorio de trabajo y copiar archivos de definición de dependencias
WORKDIR /build
COPY pyproject.toml poetry.lock ./

# 4. Instalar dependencias Python usando Poetry (solo dependencias principales, sin dev, sin la raíz del proyecto)
RUN poetry install --no-root --only main

# 5. Purgar las dependencias de compilación que ya no son necesarias para reducir el tamaño de esta etapa
# (y potencialmente capas intermedias si se almacenan en caché).
RUN apt-get purge -y --auto-remove curl gcc build-essential && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# ---- Stage 2: Final Image (Runtime) ----
FROM python:3.10-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME="/app" \
    # Las variables como PORT y WORKERS se pueden establecer aquí como defaults
    # o mejor aún, gestionarlas a través de variables de entorno en el despliegue (Kubernetes, Docker Compose)
    RERANKER_PORT_DEFAULT=8004 \
    RERANKER_WORKERS_DEFAULT=2 \
    HF_HOME="/app/.cache/huggingface" \
    TRANSFORMERS_CACHE="/app/.cache/huggingface" # Asegura que las librerías usen este path

WORKDIR ${APP_HOME}

# Copiar las dependencias Python instaladas desde la etapa builder
COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
# Copiar los ejecutables (como gunicorn, uvicorn si son dependencias de Poetry) de la etapa builder
COPY --from=builder /usr/local/bin /usr/local/bin

# Copiar el código de la aplicación
COPY ./app ${APP_HOME}/app

# Crear y establecer permisos para el directorio de caché de Hugging Face
# NOTA: chmod 777 es muy permisivo. Considera crear un usuario no-root y darle propiedad.
RUN mkdir -p ${HF_HOME} && \
    chown -R nobody:nogroup ${APP_HOME}/.cache && \
    chmod -R 777 ${APP_HOME}/.cache

# Usar las variables de entorno o sus defaults para EXPOSE y CMD
EXPOSE ${RERANKER_PORT:-${RERANKER_PORT_DEFAULT}}

# CMD para ejecutar la aplicación
# Usar `exec` permite que gunicorn reciba señales del sistema correctamente.
# Las variables de entorno se expanden por sh.
CMD sh -c "exec gunicorn -k uvicorn.workers.UvicornWorker app.main:app --bind 0.0.0.0:${RERANKER_PORT:-${RERANKER_PORT_DEFAULT}} --workers ${RERANKER_WORKERS:-${RERANKER_WORKERS_DEFAULT}} --timeout 120"