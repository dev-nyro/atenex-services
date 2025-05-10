# embedding-service/Dockerfile
# ---- Stage 1: Builder ----
FROM python:3.10-slim as builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VERSION=1.8.2 \
    POETRY_HOME="/opt/poetry" \
    PATH="/opt/poetry/bin:$PATH" \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false

# onnxruntime puede necesitar libgomp1
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl build-essential libgomp1 && \
    rm -rf /var/lib/apt/lists/*

RUN curl -sSL https://install.python-poetry.org | python3 - --version ${POETRY_VERSION}
RUN poetry --version

WORKDIR /build

COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root --only main

# ---- Stage 2: Final Image ----
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME="/app" \
    EMBEDDING_PORT=8003 \
    HF_HOME="/app/.cache/huggingface" \
    TRANSFORMERS_CACHE="/app/.cache/huggingface" \
    FASTEMBED_CACHE_DIR="/app/.cache/fastembed"

WORKDIR ${APP_HOME}

# Reinstalar libgomp1 si es estrictamente necesario en runtime para onnx
RUN apt-get update && \
    apt-get install -y --no-install-recommends libgomp1 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY ./app ${APP_HOME}/app

RUN mkdir -p ${HF_HOME} ${FASTEMBED_CACHE_DIR} && \
    chown -R nobody:nogroup ${APP_HOME}/.cache && \
    chmod -R 777 ${APP_HOME}/.cache

EXPOSE ${EMBEDDING_PORT}

CMD ["gunicorn", "-w", "2", "-k", "uvicorn.workers.UvicornWorker", "app.main:app", "--bind", "0.0.0.0:${EMBEDDING_PORT}", "--timeout", "120"]