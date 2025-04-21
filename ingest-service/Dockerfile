# ingest-service/Dockerfile
# Use a specific Python version slim image
# LLM_COMMENT: Using Python 3.10 slim as base.
FROM python:3.10-slim

# Set environment variables using key=value format
# LLM_COMMENT: Standard Python environment variables.
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# LLM_COMMENT: Poetry configuration variables. Use standard create=false.
ENV POETRY_VERSION=1.7.1
ENV POETRY_HOME="/opt/poetry"
ENV PATH="$POETRY_HOME/bin:$PATH"
ENV POETRY_NO_INTERACTION=1
# LLM_COMMENT: Ensure Poetry installs packages globally within the container's Python environment.
ENV POETRY_VIRTUALENVS_CREATE=false

# Install system dependencies including curl and build tools
# LLM_COMMENT: Install build-essential for packages needing compilation and curl for Poetry install script.
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl build-essential && \
    rm -rf /var/lib/apt/lists/*

# Install Poetry using the official installer
# LLM_COMMENT: Download and execute the Poetry installer script.
RUN curl -sSL https://install.python-poetry.org | python3 - --version ${POETRY_VERSION}

# Set working directory
# LLM_COMMENT: Set the application's working directory.
WORKDIR /app

# Copy ONLY the dependency definition files first to leverage Docker cache
# LLM_COMMENT: Copy pyproject.toml AND poetry.lock. The presence of poetry.lock is crucial for reproducible builds.
COPY pyproject.toml poetry.lock* ./

# Install project dependencies using the lock file
# LLM_COMMENT: Run poetry install. It will use poetry.lock if present, ensuring exact dependencies are installed.
# LLM_COMMENT: --no-root: Skip installing the project package itself.
# LLM_COMMENT: --no-dev: Skip installing development dependencies.
RUN poetry install --no-root --no-dev

# Verify ONNX Runtime providers in a separate layer AFTER installation is complete
# LLM_COMMENT: This step verifies if onnxruntime (provided by onnxruntime-gpu) was installed correctly and can be imported.
RUN python -c "import onnxruntime; print('ONNX Runtime available providers:', onnxruntime.get_available_providers())"

# Copy the application code into the container
# LLM_COMMENT: Copy the application source code.
COPY ./app /app/app

# Expose the port the app runs on (relevant for API, not worker, but good practice)
# LLM_COMMENT: Expose the port defined for the service (typically for the API part).
EXPOSE 8000

# Set the command to run the application using Gunicorn (for API deployment)
# For the worker deployment, this CMD will be overridden by command/args in Kubernetes YAML
# LLM_COMMENT: Default command to run the API server. This will be overridden for the Celery worker deployment.
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "4", "-b", "0.0.0.0:8000", "app.main:app"]