# Use a specific Python version slim image
FROM python:3.10-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV POETRY_VERSION=1.7.1
ENV POETRY_HOME="/opt/poetry"
# Correct the VENV path to be inside POETRY_HOME for consistency
ENV POETRY_VENV="/opt/poetry/venv"
# Add both the global poetry bin and the virtualenv bin to PATH
ENV PATH="$POETRY_HOME/bin:$POETRY_VENV/bin:$PATH"
# Tell poetry *not* to create a virtualenv inside the project directory
ENV POETRY_VIRTUALENVS_CREATE=false

# Install system dependencies including curl
# Combine update, install, and cleanup in one RUN layer to reduce image size
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl build-essential && \
    rm -rf /var/lib/apt/lists/*

# Install Poetry using the downloaded script
# Use python3 explicitly if needed, though 'python' should map to python3 here
RUN curl -sSL https://install.python-poetry.org | python3 -

# Set working directory
WORKDIR /app

# Copy dependency definition files
COPY pyproject.toml poetry.lock* ./

# Install project dependencies using Poetry
# Poetry will now install packages into the system python site-packages
# because POETRY_VIRTUALENVS_CREATE is false
RUN poetry install --no-root --no-dev --no-interaction --no-ansi

# Copy the application code into the container
COPY ./app /app/app

# Expose the port the app runs on
EXPOSE 8000

# Set the command to run the application using Gunicorn
# This command will be used by the ingest-api-deployment
# Gunicorn was added to pyproject.toml and installed by 'poetry install'
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "4", "-b", "0.0.0.0:8000", "app.main:app"]