FROM python:3.10-slim as builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VERSION=1.8.2 \
    POETRY_HOME="/opt/poetry" \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    PIP_DEFAULT_TIMEOUT=100

ENV PATH="${POETRY_HOME}/bin:${PATH}"

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl build-essential cmake && \
    rm -rf /var/lib/apt/lists/* && \
    curl -sSL https://install.python-poetry.org | python3 - --version ${POETRY_VERSION}

RUN poetry --version

WORKDIR /build

COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root --only main -vvv

RUN rm -rf /root/.cache/huggingface

FROM python:3.10-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME="/app"

ARG UID=10001
RUN addgroup --system --gid ${UID} appgroup && \
    adduser \
    --system \
    --disabled-password \
    --ingroup appgroup \
    --uid ${UID} \
    --home "/nonexistent" \
    --shell "/sbin/nologin" \
    --no-create-home \
    appuser

WORKDIR ${APP_HOME}

COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY ./app ${APP_HOME}/app

RUN chown -R appuser:appgroup ${APP_HOME}

USER appuser

CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-c", "app/gunicorn_conf.py", "app.main:app"]