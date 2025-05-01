# ---- Stage 1: Builder ----
# Use a specific Python version slim image as builder base
# LLM_COMMENT: Builder stage to install dependencies including build tools.
FROM python:3.10-slim as builder

# Set environment variables (Simplified and PATH corrected)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VERSION=1.7.1 \
    POETRY_HOME="/opt/poetry" \
    # LLM_COMMENT: Correctly add POETRY_HOME/bin to the PATH for subsequent RUN commands.
    PATH="/opt/poetry/bin:$PATH" \
    POETRY_NO_INTERACTION=1 \
    # LLM_COMMENT: Install globally within this stage's Python environment.
    POETRY_VIRTUALENVS_CREATE=false

# Install system build dependencies and curl
# LLM_COMMENT: Install build tools needed for some Python packages + curl for Poetry.
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl build-essential && \
    rm -rf /var/lib/apt/lists/*

# Install Poetry using the official installer
# LLM_COMMENT: The installer script will place the `poetry` executable/shim in $POETRY_HOME/bin which is now in PATH.
RUN curl -sSL https://install.python-poetry.org | python3 - --version ${POETRY_VERSION}

# --- Verification Step ---
# LLM_COMMENT: Verify poetry command is accessible via PATH immediately after installation.
RUN poetry --version

# Set working directory for builder
WORKDIR /build

# Copy ONLY the dependency definition files
# LLM_COMMENT: Copy dependency files to leverage cache.
COPY pyproject.toml poetry.lock* ./

# Install project dependencies using the lock file via the PATH
# LLM_COMMENT: Run poetry install. Uses the 'poetry' command found via PATH.
# LLM_COMMENT: --only main: Installs only main dependencies (no dev).
RUN poetry install --no-root --only main

# ---- Stage 2: Final Image ----
# Use the same slim base image for the final stage
# LLM_COMMENT: Final stage using the same slim base.
FROM python:3.10-slim

# Set environment variables for the final image (no POETRY needed here)
# LLM_COMMENT: Set runtime environment variables.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # LLM_COMMENT: WORKDIR variable is not standard, use /app directly.
    WORKDIR_PATH="/app"

# Set working directory
WORKDIR ${WORKDIR_PATH}

# Install ONLY runtime system dependencies (if any are needed)
# RUN apt-get update && apt-get install -y --no-install-recommends some-runtime-lib && rm -rf /var/lib/apt/lists/*
# LLM_COMMENT: Currently, no runtime system dependencies seem explicitly required.

# Copy installed dependencies from the builder stage
# LLM_COMMENT: Copy only the installed packages from the builder stage's global site-packages.
COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
# LLM_COMMENT: Copy any executables installed by dependencies (like gunicorn, celery) from the builder's bin.
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy the application code
# LLM_COMMENT: Copy application code.
COPY ./app /app/app

# Expose the port the API runs on
# LLM_COMMENT: Expose the API port.
EXPOSE 8000

# Set the default command to run the API (can be overridden)
# LLM_COMMENT: Default command for API. Workers will override this in Kubernetes YAML.
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "4", "-b", "0.0.0.0:8000", "app.main:app"]