# reranker-service/Dockerfile
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    PIP_DEFAULT_TIMEOUT=100 \
    POETRY_VERSION=1.7.1 \
    POETRY_HOME="/opt/poetry" \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    PATH="$POETRY_HOME/bin:$PATH"

# Instalar poetry
RUN apt-get update && apt-get install -y curl \
    && curl -sSL https://install.python-poetry.org | python3 - \
    && apt-get remove -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copiar solo los archivos de dependencias primero para cachear la capa de dependencias
COPY pyproject.toml poetry.lock ./

# Instalar dependencias del proyecto.
# --no-root para no instalar el proyecto actual aún (solo dependencias)
# --no-dev para no instalar dependencias de desarrollo
RUN poetry install --no-root --no-dev

# Copiar el resto de la aplicación
COPY ./app /app/app

# Puerto que expondrá Gunicorn/Uvicorn, default a 8004 si RERANKER_PORT no está seteado
ENV RERANKER_PORT=${RERANKER_PORT:-8004}
EXPOSE ${RERANKER_PORT}

# Comando para ejecutar la aplicación
# Usar variables de entorno para configurar Gunicorn workers
ENV RERANKER_WORKERS=${RERANKER_WORKERS:-2}
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "app.main:app", "--bind", "0.0.0.0:${RERANKER_PORT}", "--workers", "${RERANKER_WORKERS}"]