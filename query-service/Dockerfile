# syntax=docker/dockerfile:1.7
FROM python:3.10-slim AS builder

ENV POETRY_HOME=/opt/poetry \
    POETRY_VIRTUALENVS_CREATE=false \
    PATH="/opt/poetry/bin:${PATH}" \
    PYTHONUNBUFFERED=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl build-essential && \
    curl -sSL https://install.python-poetry.org | python3 - && \
    apt-get purge -y build-essential && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml poetry.lock* ./

# ⚡️ monta caché de Poetry/pip; descarga sólo runtime
RUN --mount=type=cache,target=/root/.cache \
    poetry install --no-root --no-ansi --no-interaction --only main --sync

# ---------- runtime ----------
FROM python:3.10-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
COPY app ./app
CMD ["python", "-m", "app.main"]
