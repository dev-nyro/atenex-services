# Use a specific Python version slim image
FROM python:3.10-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV POETRY_VERSION=1.7.1
ENV POETRY_HOME="/opt/poetry"
ENV POETRY_VENV="/opt/poetry/.venv"
ENV PATH="$POETRY_HOME/bin:$POETRY_VENV/bin:$PATH"

# Install system dependencies if any (e.g., build tools)
# RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Install Poetry
RUN curl -sSL https://install.python-poetry.org | python3 -

# Set working directory
WORKDIR /app

# Copy dependency definition files
COPY pyproject.toml poetry.lock* ./

# Install dependencies using Poetry
# --no-root: Don't install the project itself as editable, just dependencies
# --no-dev: Exclude development dependencies
RUN poetry install --no-root --no-dev --no-interaction --no-ansi

# Copy the application code into the container
COPY ./app /app/app

# Expose the port the app runs on
EXPOSE 8000

# Set the command to run the application using Gunicorn
# This command will be used by the ingest-api-deployment
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "4", "-b", "0.0.0.0:8000", "app.main:app"]