# api-gateway/Dockerfile
ARG PYTHON_VERSION=3.10
FROM python:${PYTHON_VERSION}-slim as base

# --- Python Build Stage ---
# Installs dependencies into a virtual environment based on pyproject.toml
FROM base as python-build-stage
ARG PYTHON_VERSION
ARG POETRY_VERSION=1.7.1

# System dependencies needed for poetry installation
RUN apt-get update && apt-get install --no-install-recommends -y \
    curl \
    && apt-get autoremove -y && apt-get clean -y && rm -rf /var/lib/apt/lists/*

# Install poetry itself
ENV POETRY_HOME="/opt/poetry" \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_NO_INTERACTION=1
ENV PATH="/opt/poetry/bin:$PATH"
RUN curl -sSL https://install.python-poetry.org | python - --version ${POETRY_VERSION}

# Set working directory for the build stage
WORKDIR /app

# --- Dependency Installation ---
# Copy ONLY pyproject.toml. Poetry will resolve dependencies from this.
COPY pyproject.toml ./

# Install runtime dependencies into the project's .venv directory.
# Since poetry.lock is NOT copied, Poetry resolves from pyproject.toml.
# --no-root: Don't install the project package itself, only dependencies.
# --no-dev: Exclude development dependencies.
# --compile: Pre-compile pyc files (optional small optimization).
RUN poetry install --no-root --no-dev --compile

# --- Runtime Stage ---
# Creates the final image by copying the virtual environment and app code
FROM base
ARG PYTHON_VERSION

# Set environment variables for the runtime container
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Add the virtual environment's bin directory to the PATH
    PATH="/app/.venv/bin:$PATH" \
    # Define the default port (can be overridden by K8s env var)
    PORT=8080

# Set the working directory for the runtime container
WORKDIR /app

# Copy the virtual environment created in the build stage
COPY --from=python-build-stage /app/.venv /app/.venv

# Copy the application code
# Assumes your code is in an 'app' subdirectory within 'api-gateway'
COPY ./app ./app

# Expose the port the app runs on (matches Gunicorn command)
EXPOSE ${PORT}

# Optional: Run as non-root user for security
# RUN groupadd -r appuser && useradd --no-log-init -r -g appuser appuser
# RUN chown -R appuser:appuser /app
# USER appuser

# Command to run the application using Gunicorn
# Binds to the port defined by the PORT env var (defaulting to 8080)
# Uses the virtual environment's python/gunicorn implicitly via PATH
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "4", "-b", "0.0.0.0:8080", "app.main:app"]