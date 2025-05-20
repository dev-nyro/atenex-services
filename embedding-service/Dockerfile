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

# onnxruntime puede necesitar libgomp1, PyTorch might need it too.
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
    # HuggingFace cache directories variables, defined for clarity and use by the app
    # The actual creation path will use ${APP_HOME} directly in RUN for robustness
    HF_HOME_SUBPATH=".cache/huggingface/hub" \
    TRANSFORMERS_CACHE_SUBPATH=".cache/huggingface/transformers" \
    SENTENCE_TRANSFORMERS_HOME_SUBPATH=".cache/sentence_transformers"

WORKDIR ${APP_HOME}

# Reinstalar libgomp1 si es estrictamente necesario en runtime.
RUN apt-get update && \
    apt-get install -y --no-install-recommends libgomp1 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

COPY ./app ${APP_HOME}/app

# Create cache directories and set permissions
# Use APP_HOME directly to ensure correct path creation.
RUN mkdir -p ${APP_HOME}/${HF_HOME_SUBPATH} \
             ${APP_HOME}/${TRANSFORMERS_CACHE_SUBPATH} \
             ${APP_HOME}/${SENTENCE_TRANSFORMERS_HOME_SUBPATH} && \
    chown -R nobody:nogroup ${APP_HOME}/.cache && \
    chmod -R 777 ${APP_HOME}/.cache 

EXPOSE ${EMBEDDING_PORT}

# Use gunicorn_conf.py for configuration
# Ensure gunicorn_conf.py is copied into the APP_HOME directory
# The COPY ./app ${APP_HOME}/app command should handle gunicorn_conf.py if it's inside ./app
CMD ["gunicorn", "-c", "app/gunicorn_conf.py", "app.main:app"]