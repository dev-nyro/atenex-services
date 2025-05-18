FROM python:3.10-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VERSION=1.8.2 \
    POETRY_HOME="/opt/poetry" \
    PATH="/opt/poetry/bin:$PATH" \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl gcc build-essential

RUN curl -sSL https://install.python-poetry.org | python3 - --version ${POETRY_VERSION}
RUN poetry --version

WORKDIR /build
COPY pyproject.toml poetry.lock ./

RUN poetry install --no-root --only main

RUN apt-get purge -y --auto-remove curl gcc build-essential && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

FROM python:3.10-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME="/app" \
    RERANKER_PORT_DEFAULT=8004 \
    RERANKER_WORKERS_DEFAULT=2 \
    # AÑADIR ESTOS DEFAULTS SEGUROS PARA CUDA:
    RERANKER_MODEL_DEVICE_DEFAULT=gpu \
    RERANKER_GUNICORN_WORKERS_CUDA_DEFAULT=1 \
    RERANKER_TOKENIZER_WORKERS_CUDA_DEFAULT=0 \
    HF_HOME="/app/.cache/huggingface" \
    TRANSFORMERS_CACHE="/app/.cache/huggingface"

WORKDIR ${APP_HOME}

COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY ./app ${APP_HOME}/app

RUN mkdir -p ${HF_HOME} && \
    chown -R nobody:nogroup ${APP_HOME}/.cache && \
    chmod -R 777 ${APP_HOME}/.cache

EXPOSE ${RERANKER_PORT:-${RERANKER_PORT_DEFAULT}}

# El CMD usará las variables de entorno RERANKER_WORKERS y RERANKER_TOKENIZER_WORKERS
# que serán validadas y posiblemente forzadas por config.py si se usa CUDA.
CMD sh -c "exec gunicorn -k uvicorn.workers.UvicornWorker app.main:app --bind 0.0.0.0:${RERANKER_PORT:-${RERANKER_PORT_DEFAULT}} --workers ${RERANKER_WORKERS:-${RERANKER_WORKERS_DEFAULT}} --timeout 120"