# query-service/Dockerfile
# ---- Stage 1: Builder ----
FROM python:3.10-slim as builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VERSION=1.8.2 \
    POETRY_HOME="/opt/poetry" \
    PATH="/opt/poetry/bin:$PATH" \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl build-essential cmake && \
    rm -rf /var/lib/apt/lists/*

RUN curl -sSL https://install.python-poetry.org | python3 - --version ${POETRY_VERSION}
RUN poetry --version

WORKDIR /build

COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root --only main -vvv

RUN rm -rf /root/.cache/huggingface

# ---- Stage 2: Final Image ----
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME="/app" \
    PORT=8001

WORKDIR ${APP_HOME}

# Instalar dependencias de runtime del sistema si son necesarias (cmake no es necesario en runtime para bm2s)
# RUN apt-get update && apt-get install -y --no-install-recommends some-runtime-lib && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY ./app ${APP_HOME}/app

EXPOSE ${PORT}

CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "2", "-b", "0.0.0.0:${PORT}", "app.main:app"]