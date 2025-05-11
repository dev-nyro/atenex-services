# docproc-service/Dockerfile
FROM python:3.10-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VERSION=1.8.2 \
    POETRY_HOME="/opt/poetry" \
    PATH="/opt/poetry/bin:$PATH" \
    POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false

# Instalar dependencias de compilación, incluyendo libmupdf-dev
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      curl \
      build-essential \
      libmupdf-dev \
      swig && \
    rm -rf /var/lib/apt/lists/*

RUN curl -sSL https://install.python-poetry.org | python3 - --version ${POETRY_VERSION}
RUN poetry --version

WORKDIR /build
COPY pyproject.toml poetry.lock ./
# Instalar dependencias de la aplicación, PyMuPDF se compilará aquí
RUN poetry install --no-root --only main

FROM python:3.10-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME="/app" \
    DOCPROC_PORT=8005

WORKDIR ${APP_HOME}

# YA NO ES NECESARIO INSTALAR libmupdf0 aquí
# Las bibliotecas necesarias por PyMuPDF deberían haber sido empaquetadas
# o las versiones runtime de las bibliotecas de libmupdf-dev
# son típicamente manejadas por el sistema base o la instalación de Python.
# Si PyMuPDF las necesita del sistema y no las encuentra, entonces se necesitaría el paquete
# que provee libmupdf.so, que usualmente sería algo como 'libmupdf' (sin -dev y sin 0)
# pero primero probemos sin ello, ya que la wheel de PyMuPDF podría ser autosuficiente.
# RUN apt-get update && \
#     apt-get install -y --no-install-recommends \
#       libmupdf # O el nombre correcto del paquete runtime si es necesario.
#                 # Para Debian Bookworm, libmupdf-dev instala las .so.
#                 # Por ahora, omitimos esta instalación en runtime.
#     && rm -rf /var/lib/apt/lists/*


# Copiar los paquetes Python instalados desde la etapa builder
COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
# Copiar los ejecutables instalados por Poetry (si alguno)
COPY --from=builder /usr/local/bin /usr/local/bin

# Copiar el código de la aplicación
COPY ./app ${APP_HOME}/app

EXPOSE ${DOCPROC_PORT}

# Usar exec para que Gunicorn sea el proceso PID 1 y maneje las señales correctamente
CMD exec gunicorn -k uvicorn.workers.UvicornWorker -w 4 -b 0.0.0.0:${DOCPROC_PORT} app.main:app --timeout 300