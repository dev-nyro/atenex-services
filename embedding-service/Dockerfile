# embedding-service/Dockerfile
# ---- Stage 1: Builder ----
FROM python:3.10-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VERSION=1.8.2 \
    POETRY_HOME="/opt/poetry" \
    PATH="/opt/poetry/bin:$PATH" \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false

# Instalar dependencias base para compilar algunos paquetes de Python si es necesario.
# Aunque el objetivo es una imagen ligera para OpenAI, algunas dependencias transitivas aún podrían necesitarlas.
# libgomp1 es para OpenMP, puede ser requerida por alguna librería subyacente.
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl build-essential libgomp1 && \
    rm -rf /var/lib/apt/lists/*

RUN curl -sSL https://install.python-poetry.org | python3 - --version ${POETRY_VERSION}
RUN poetry --version

WORKDIR /build

COPY pyproject.toml poetry.lock ./
# Por defecto, solo instala las dependencias del grupo 'main'.
# Si se necesitara construir una imagen con Sentence Transformers, este comando debería ser:
# RUN poetry install --no-root --only main --with st
RUN poetry install --no-root --only main

# ---- Stage 2: Final Image ----
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME="/app" \
    EMBEDDING_PORT=8003 \
    HF_HOME_SUBPATH=".cache/huggingface/hub" \
    TRANSFORMERS_CACHE_SUBPATH=".cache/huggingface/transformers" \
    SENTENCE_TRANSFORMERS_HOME_SUBPATH=".cache/sentence_transformers"

WORKDIR ${APP_HOME}

# Reinstalar libgomp1 si es estrictamente necesario en runtime para alguna dependencia.
RUN apt-get update && \
    apt-get install -y --no-install-recommends libgomp1 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY ./app ${APP_HOME}/app
# CORRECCIÓN: Copiar gunicorn_conf.py desde app/gunicorn_conf.py a la raíz de APP_HOME
COPY ./app/gunicorn_conf.py ${APP_HOME}/gunicorn_conf.py


# Crear directorios de caché. Aunque Sentence Transformers no se instale por defecto,
# estos directorios son inofensivos si están vacíos y simplifican el Dockerfile
# para un futuro cambio de proveedor sin modificar esta sección.
RUN mkdir -p ${APP_HOME}/${HF_HOME_SUBPATH} \
             ${APP_HOME}/${TRANSFORMERS_CACHE_SUBPATH} \
             ${APP_HOME}/${SENTENCE_TRANSFORMERS_HOME_SUBPATH} && \
    chown -R nobody:nogroup ${APP_HOME}/.cache && \
    chmod -R 777 ${APP_HOME}/.cache 

EXPOSE ${EMBEDDING_PORT}

CMD ["gunicorn", "-c", "gunicorn_conf.py", "app.main:app"]