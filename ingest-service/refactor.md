¡Excelente! Basándonos en tu análisis previo y la necesidad de simplificar el chunking, aquí tienes el **Plan Maestro de Refactorización Final y Corregido**:

---

### **Plan Maestro de Refactorización v2 (Corregido)**

*(**Objetivo** → migrar el microservicio “ingest-service” de la pila actual (`FastEmbed`, `pypdf`, chunker custom) a una nueva pila (`SentenceTransformers (all-MiniLM-L6-v2)`, `PyMuPDF`, `python-docx`, extractores específicos y **reutilizando el chunker custom existente**), manteniendo la arquitectura Celery/MinIO/PostgreSQL/Milvus y la lógica multi-tenant.)*

---

## 1 · Resumen de Impacto (Corregido)

| Componente          | Estado Actual                                      | Nuevo Componente / Acción                                        | Archivos Afectados Principalmente                                                                   | Cambio Clave                                                                                             |
| :------------------ | :------------------------------------------------- | :--------------------------------------------------------------- | :-------------------------------------------------------------------------------------------------- | :------------------------------------------------------------------------------------------------------- |
| **Embeddings**      | `FastEmbed` (ej. `BAAI/bge-large-en-v1.5`, 1024D) | **`SentenceTransformers` (`all-MiniLM-L6-v2`, 384D)**            | `pyproject.toml`, `app/core/config.py`, `app/services/embedder.py` (nuevo), `app/tasks/process_document.py` | Reemplazo de biblioteca, carga de modelo en worker, cambio de dimensión.                                 |
| **Extractor PDF**   | `pypdf`                                            | **`PyMuPDF` (`fitz`)**                                           | `pyproject.toml`, `app/services/extractors/pdf_extractor.py` (nuevo), `app/services/ingest_pipeline.py`   | Cambio de biblioteca para mejor rendimiento/precisión. **Requiere input `bytes`**.                       |
| **Extractor DOCX**  | `python-docx` (usado directamente)                 | **`python-docx` (encapsulado)**                                  | `app/services/extractors/docx_extractor.py` (nuevo), `app/services/ingest_pipeline.py`                 | Encapsular lógica existente en helper. **Requiere input `bytes`**.                                        |
| **Extractor TXT**   | Lectura directa (`_extract_from_txt`)              | **Lectura directa (encapsulada)**                                | `app/services/extractors/txt_extractor.py` (nuevo), `app/services/ingest_pipeline.py`                  | Encapsular lógica existente en helper. **Requiere input `bytes`**.                                        |
| **Extractor MD**    | `markdown` + `html2text` (`_extract_from_md`)      | **`markdown` + `html2text` (encapsulado)**                       | `app/services/extractors/md_extractor.py` (nuevo), `app/services/ingest_pipeline.py`                   | Encapsular lógica existente en helper. **Requiere input `bytes`**.                                        |
| **Extractor HTML**  | `BeautifulSoup` (`_extract_from_html`)             | **`BeautifulSoup` (encapsulado)**                                | `app/services/extractors/html_extractor.py` (nuevo), `app/services/ingest_pipeline.py`                 | Encapsular lógica existente en helper. **Requiere input `bytes`**.                                        |
| **Chunking**        | Función custom `chunk_text` en `ingest_pipeline` | **Misma función custom `chunk_text` (movida)**                   | `app/services/text_splitter.py` (nuevo, mueve lógica), `app/services/ingest_pipeline.py`            | **Reutilizar lógica existente**, mover a módulo dedicado. Evita dependencia LangChain.                 |
| **Milvus Schema**   | Varios campos, Dim=1024                            | Varios campos, **Dim=384** (`embedding` field), `company_id` (ya existe) | `app/core/config.py`, `app/services/ingest_pipeline.py` (`_create_milvus_collection`), Script/Manual | **CRÍTICO: Ajustar dimensión a 384**. `company_id` ya existe. `chunk_idx` opcional, no presente ahora. |
| **Configuración**   | `FASTEMBED_MODEL`, `EMBEDDING_DIMENSION=1024`      | **`EMBEDDING_MODEL_ID`**, **`EMBEDDING_DIMENSION=384`**          | `app/core/config.py`, `.env` / k8s ConfigMap                                                          | Renombrar var. modelo, actualizar dimensión default. Eliminar vars. de FastEmbed.                      |
| **Re-Ingesta**      | N/A                                                | **REQUERIDA**                                                    | Todos los datos en Milvus                                                                           | Cambio de modelo/dimensión invalida vectores existentes. **Necesaria re-indexación completa.**         |
| **Flujo de Datos**  | Pipeline recibe `pathlib.Path`                     | **Pipeline recibe `bytes`**                                      | `app/tasks/process_document.py`, `app/services/ingest_pipeline.py`                                     | Tarea Celery debe leer archivo a bytes antes de llamar al pipeline.                                    |

---

## 2 · Pasos Detallados (Corregido)

### 2.1 Actualizar Dependencias (`pyproject.toml`)

```toml
[tool.poetry.dependencies]
# --- AÑADIR ---
sentence-transformers = "^2.7" # O versión más reciente compatible
pymupdf = "^1.25.0"           # O versión más reciente compatible

# --- VERIFICAR/MANTENER (ya deberían estar) ---
python-docx = ">=1.1.0,<2.0.0"
markdown = ">=3.5.1,<4.0.0"
beautifulsoup4 = ">=4.12.3,<5.0.0"
html2text = ">=2024.1.0,<2025.0.0"

# --- ELIMINAR ---
# Comentar o eliminar completamente la línea de fastembed
# fastembed = ">=0.2.1,<0.3.0"
```

*   Ejecutar `poetry lock && poetry install` para actualizar el entorno.*

---

### Checklist de Refactorización

- [x] 2.1 Actualizar dependencias (`pyproject.toml`): eliminar fastembed, añadir sentence-transformers y pymupdf, comentar pypdf.
- [x] 2.2 Crear/Actualizar módulos utilitarios: extractores, text_splitter, embedder.
- [x] 2.3 Refactorizar ingest_pipeline.py para usar los nuevos helpers y lógica modular.
- [x] 2.4 Refactorizar process_document.py para pasar bytes y no Path.
- [x] 2.5 Ajustar configuración y Milvus schema (dim=384, variable modelo, etc).

### 2.2 Crear/Actualizar Módulos Utilitarios

*   Crear la carpeta `app/services/extractors/` si no existe.
*   Crear/Actualizar los siguientes archivos:

```
app/
└── services/
    ├── __init__.py
    ├── extractors/              # NUEVA CARPETA
    │   ├── __init__.py
    │   ├── pdf_extractor.py     # NUEVO (usa PyMuPDF)
    │   ├── docx_extractor.py    # NUEVO (usa python-docx)
    │   ├── txt_extractor.py     # NUEVO (usa lectura simple)
    │   ├── md_extractor.py      # NUEVO (usa markdown+html2text)
    │   └── html_extractor.py    # NUEVO (usa beautifulsoup4)
    ├── text_splitter.py         # NUEVO (mueve lógica chunk_text existente)
    ├── embedder.py              # NUEVO (usa SentenceTransformers)
    ├── ingest_pipeline.py       # MODIFICADO
    └── minio_client.py          # SIN CAMBIOS (ya maneja sync/async)
    └── base_client.py           # SIN CAMBIOS
```

#### `extractors/pdf_extractor.py`

```python
# app/services/extractors/pdf_extractor.py
import fitz  # PyMuPDF
import structlog

log = structlog.get_logger(__name__)

class PdfExtractionError(Exception):
    pass

def extract_text_from_pdf(file_bytes: bytes, filename: str = "unknown.pdf") -> str:
    """Extrae texto plano de los bytes de un PDF usando PyMuPDF."""
    log.debug("Extracting text from PDF bytes", filename=filename)
    try:
        # Abrir desde stream de bytes
        with fitz.open(stream=file_bytes, filetype="pdf") as doc:
            text_content = ""
            for page_num, page in enumerate(doc):
                try:
                    text_content += page.get_text("text") + "\n" # Usar 'text' para orden lógico
                except Exception as page_err:
                    log.warning("Error extracting text from PDF page", filename=filename, page=page_num + 1, error=str(page_err))
            log.info("PDF extraction successful", filename=filename, num_pages=len(doc))
            # Opcional: Añadir paso de limpieza aquí si es necesario
            return text_content.strip()
    except Exception as e:
        log.error("Failed to extract text from PDF bytes", filename=filename, error=str(e), exc_info=True)
        raise PdfExtractionError(f"Error processing PDF {filename}: {e}") from e
```

#### `extractors/docx_extractor.py`

```python
# app/services/extractors/docx_extractor.py
import io
import docx # python-docx
import structlog

log = structlog.get_logger(__name__)

class DocxExtractionError(Exception):
    pass

def extract_text_from_docx(file_bytes: bytes, filename: str = "unknown.docx") -> str:
    """Extrae texto de los bytes de un DOCX preservando saltos de párrafo."""
    log.debug("Extracting text from DOCX bytes", filename=filename)
    try:
        # Abrir desde stream de bytes
        doc = docx.Document(io.BytesIO(file_bytes))
        # Unir el texto de cada párrafo con un salto de línea
        full_text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        log.info("DOCX extraction successful", filename=filename, num_paragraphs=len(doc.paragraphs))
        return full_text.strip()
    except Exception as e:
        log.error("Failed to extract text from DOCX bytes", filename=filename, error=str(e), exc_info=True)
        raise DocxExtractionError(f"Error processing DOCX {filename}: {e}") from e
```

#### `extractors/txt_extractor.py`

```python
# app/services/extractors/txt_extractor.py
import structlog

log = structlog.get_logger(__name__)

class TxtExtractionError(Exception):
    pass

def extract_text_from_txt(file_bytes: bytes, filename: str = "unknown.txt", encoding: str = "utf-8") -> str:
    """Extrae texto de los bytes de un archivo TXT."""
    log.debug("Extracting text from TXT bytes", filename=filename, encoding=encoding)
    try:
        # Decodificar bytes a string
        text_content = file_bytes.decode(encoding, errors='ignore') # Ignorar errores de decodificación
        log.info("TXT extraction successful", filename=filename)
        return text_content.strip()
    except Exception as e:
        log.error("Failed to extract text from TXT bytes", filename=filename, error=str(e), exc_info=True)
        raise TxtExtractionError(f"Error processing TXT {filename}: {e}") from e
```

#### `extractors/md_extractor.py`

```python
# app/services/extractors/md_extractor.py
import markdown
import html2text
import structlog

log = structlog.get_logger(__name__)

class MdExtractionError(Exception):
    pass

def extract_text_from_md(file_bytes: bytes, filename: str = "unknown.md", encoding: str = "utf-8") -> str:
    """Extrae texto de los bytes de un archivo Markdown."""
    log.debug("Extracting text from Markdown bytes", filename=filename)
    try:
        # Decodificar bytes a string
        md_content = file_bytes.decode(encoding, errors='ignore')
        # Convertir MD a HTML
        html_content = markdown.markdown(md_content)
        # Convertir HTML a Texto plano
        h = html2text.HTML2Text()
        h.ignore_links = True
        h.ignore_images = True
        h.ignore_emphasis = True # Simplifica un poco más
        text_content = h.handle(html_content)
        log.info("Markdown extraction successful", filename=filename)
        return text_content.strip()
    except Exception as e:
        log.error("Failed to extract text from Markdown bytes", filename=filename, error=str(e), exc_info=True)
        raise MdExtractionError(f"Error processing Markdown {filename}: {e}") from e
```

#### `extractors/html_extractor.py`

```python
# app/services/extractors/html_extractor.py
from bs4 import BeautifulSoup
import structlog

log = structlog.get_logger(__name__)

class HtmlExtractionError(Exception):
    pass

def extract_text_from_html(file_bytes: bytes, filename: str = "unknown.html", encoding: str = "utf-8") -> str:
    """Extrae texto de los bytes de un archivo HTML."""
    log.debug("Extracting text from HTML bytes", filename=filename)
    try:
        # Decodificar bytes (intentar detectar encoding si no se provee uno confiable)
        # Nota: BeautifulSoup puede intentar detectar el encoding por sí mismo
        soup = BeautifulSoup(file_bytes, "html.parser") # Dejar que BS4 maneje la decodificación inicial

        # Eliminar scripts, styles, etc.
        for script_or_style in soup(["script", "style", "head", "title", "meta", "[document]"]):
            script_or_style.decompose()

        # Obtener texto, separando por saltos de línea y limpiando espacios
        text_content = soup.get_text(separator="\n", strip=True)
        log.info("HTML extraction successful", filename=filename)
        return text_content.strip()
    except Exception as e:
        log.error("Failed to extract text from HTML bytes", filename=filename, error=str(e), exc_info=True)
        raise HtmlExtractionError(f"Error processing HTML {filename}: {e}") from e
```

#### `text_splitter.py` (Reutiliza Lógica Existente)

```python
# app/services/text_splitter.py
import re
from typing import List
import structlog
from app.core.config import settings # Para obtener chunk_size/overlap de config

log = structlog.get_logger(__name__)

# Obtener parámetros desde la configuración
CHUNK_SIZE = settings.SPLITTER_CHUNK_SIZE
CHUNK_OVERLAP = settings.SPLITTER_CHUNK_OVERLAP
# Nota: `SPLITTER_SPLIT_BY` no se usa en esta lógica, se basa en palabras/espacios

def split_text(text: str) -> List[str]:
    """
    Divide texto en chunks solapados por palabras.
    (Lógica movida desde el anterior ingest_pipeline.py)
    """
    if not text or text.isspace():
        log.warning("split_text received empty or whitespace-only text.")
        return []

    # Usar regex para dividir por espacios, manejando múltiples espacios
    words = re.split(r'\s+', text.strip())
    # Filtrar strings vacíos que puedan resultar del split
    words = [word for word in words if word]

    if not words:
        log.warning("split_text resulted in no words after splitting.")
        return []

    chunks: List[str] = []
    current_pos = 0
    while current_pos < len(words):
        # Calcular el final del chunk actual
        end_pos = min(current_pos + CHUNK_SIZE, len(words))
        # Unir las palabras para formar el chunk
        chunk = " ".join(words[current_pos:end_pos])
        chunks.append(chunk)

        # Calcular la posición de inicio del siguiente chunk
        # Avanza (tamaño - solapamiento) palabras
        current_pos += CHUNK_SIZE - CHUNK_OVERLAP

        # Salvaguarda: Si el solapamiento es grande, evitar retroceder demasiado
        if current_pos <= (end_pos - CHUNK_SIZE):
             current_pos = end_pos - CHUNK_OVERLAP # Mínimo avance

        # Salir si hemos procesado todas las palabras
        if current_pos >= len(words):
            break

        # Asegurar que la posición no sea negativa (si overlap > size, aunque no debería pasar)
        current_pos = max(0, current_pos)

    log.debug(f"Chunked text into {len(chunks)} chunks.", requested_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP)
    return chunks
```

#### `embedder.py`

```python
# app/services/embedder.py
from functools import lru_cache
from typing import List
from sentence_transformers import SentenceTransformer
import structlog
import numpy as np

from app.core.config import settings

log = structlog.get_logger(__name__)

MODEL_ID = settings.EMBEDDING_MODEL_ID
EXPECTED_DIM = settings.EMBEDDING_DIMENSION # Para validación

# Variable global para el modelo cacheado (mejor que depender solo de lru_cache entre llamadas)
_model_instance: Optional[SentenceTransformer] = None

def get_embedding_model() -> SentenceTransformer:
    """
    Carga y devuelve la instancia del modelo SentenceTransformer.
    Utiliza una variable global para asegurar una única carga por proceso worker.
    """
    global _model_instance
    if _model_instance is None:
        log.info(f"Loading SentenceTransformer model '{MODEL_ID}' for the first time in this worker...")
        try:
            # TODO: Considerar añadir device='cuda' si hay GPU y se configura
            _model_instance = SentenceTransformer(MODEL_ID, device='cpu')
            # Validar dimensión al cargar
            test_embedding = _model_instance.encode("test")
            actual_dim = len(test_embedding)
            if actual_dim != EXPECTED_DIM:
                log.error(f"Model '{MODEL_ID}' loaded but dimension mismatch!",
                          actual_dim=actual_dim, expected_dim=EXPECTED_DIM)
                raise RuntimeError(f"Model dimension mismatch: got {actual_dim}, expected {EXPECTED_DIM}")
            log.info(f"SentenceTransformer model '{MODEL_ID}' loaded successfully (Dim: {actual_dim}).")
        except Exception as e:
            log.critical(f"Failed to load SentenceTransformer model '{MODEL_ID}'!", error=str(e), exc_info=True)
            # Mantener _model_instance como None para que falle en el uso
            _model_instance = None
            raise RuntimeError(f"Could not load embedding model: {e}") from e
    return _model_instance

def embed_chunks(chunks: List[str]) -> List[List[float]]:
    """Genera embeddings para una lista de chunks de texto."""
    if not chunks:
        return []

    log.debug(f"Attempting to generate embeddings for {len(chunks)} chunks using '{MODEL_ID}'.")
    try:
        model = get_embedding_model() # Obtiene la instancia cacheada/cargada
        if model is None:
             # Esto no debería pasar si get_embedding_model() lanzó error, pero por seguridad
             raise RuntimeError("Embedding model is not available.")

        # Generar embeddings en batch
        embeddings_np: np.ndarray = model.encode(
            chunks,
            batch_size=32,          # Ajustable según CPU/memoria
            convert_to_numpy=True,  # Devuelve NumPy array
            show_progress_bar=False # No mostrar barra en logs
            # normalize_embeddings=True # Considerar si la métrica en Milvus (IP/COSINE) lo requiere/beneficia
        )
        log.info(f"Successfully generated {len(embeddings_np)} embeddings.")
        # Convertir a lista de listas de floats para compatibilidad (e.g., JSON, Milvus client)
        return embeddings_np.tolist()
    except Exception as e:
        log.error("Failed to generate embeddings", error=str(e), exc_info=True)
        # Re-lanzar para que la tarea Celery lo maneje
        raise RuntimeError(f"Embedding generation failed: {e}") from e
```

### 2.3 Refactorizar `ingest_pipeline.py`

*   **Eliminar** las funciones `_extract_from_pdf`, `_extract_from_docx`, etc. y la función `chunk_text`.
*   **Eliminar** la importación y uso de `fastembed.TextEmbedding`.
*   **Importar** los nuevos helpers:

```python
# app/services/ingest_pipeline.py
# ... (otras importaciones: os, pathlib, uuid, structlog, pymilvus, etc.)
from app.core.config import settings
from .extractors.pdf_extractor import extract_text_from_pdf, PdfExtractionError
from .extractors.docx_extractor import extract_text_from_docx, DocxExtractionError
from .extractors.txt_extractor import extract_text_from_txt, TxtExtractionError
from .extractors.md_extractor import extract_text_from_md, MdExtractionError
from .extractors.html_extractor import extract_text_from_html, HtmlExtractionError
from .text_splitter import split_text # Importar el chunker movido
from .embedder import embed_chunks # Importar el nuevo embedder
# Necesitamos el tipo SentenceTransformer para la firma
from sentence_transformers import SentenceTransformer

log = structlog.get_logger(__name__)

# --- Mapeo de Content-Type a Funciones Extractoras ---
# Usar las nuevas funciones que aceptan bytes
EXTRACTORS = {
    "application/pdf": extract_text_from_pdf,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": extract_text_from_docx,
    "application/msword": extract_text_from_docx, # Asumir que python-docx puede manejar .doc básicos
    "text/plain": extract_text_from_txt,
    "text/markdown": extract_text_from_md,
    "text/html": extract_text_from_html,
}
EXTRACTION_ERRORS = (
    PdfExtractionError, DocxExtractionError, TxtExtractionError,
    MdExtractionError, HtmlExtractionError
)

# --- Constantes Milvus (Actualizar Dimensión) ---
MILVUS_COLLECTION_NAME = settings.MILVUS_COLLECTION_NAME
MILVUS_EMBEDDING_DIM = settings.EMBEDDING_DIMENSION # ¡Debe ser 384!
MILVUS_PK_FIELD = "pk_id"
MILVUS_VECTOR_FIELD = "embedding"
MILVUS_CONTENT_FIELD = "content"
MILVUS_COMPANY_ID_FIELD = "company_id" # Ya existe
MILVUS_DOCUMENT_ID_FIELD = "document_id" # Ya existe
MILVUS_FILENAME_FIELD = "file_name" # Ya existe

# ... (Mantener las funciones _ensure_milvus_connection_and_collection, _create_milvus_collection, delete_milvus_chunks)

# --- **ASEGURARSE** que `_create_milvus_collection` use `MILVUS_EMBEDDING_DIM` (384) ---
def _create_milvus_collection(alias: str) -> Collection:
    log.info(f"Defining schema for collection '{MILVUS_COLLECTION_NAME}' with dim={MILVUS_EMBEDDING_DIM}") # Log de dimensión
    fields = [
        FieldSchema(name=MILVUS_PK_FIELD, dtype=DataType.INT64, is_primary=True, auto_id=True),
        FieldSchema(name=MILVUS_VECTOR_FIELD, dtype=DataType.FLOAT_VECTOR, dim=MILVUS_EMBEDDING_DIM), # <- Usar la dimensión correcta
        FieldSchema(name=MILVUS_CONTENT_FIELD, dtype=DataType.VARCHAR, max_length=settings.SPLITTER_CHUNK_SIZE * 5), # Aumentar un poco por si acaso
        FieldSchema(name=MILVUS_COMPANY_ID_FIELD, dtype=DataType.VARCHAR, max_length=64), # max_length ajustado
        FieldSchema(name=MILVUS_DOCUMENT_ID_FIELD, dtype=DataType.VARCHAR, max_length=64), # max_length ajustado
        FieldSchema(name=MILVUS_FILENAME_FIELD, dtype=DataType.VARCHAR, max_length=512), # Ya presente
        # -- OPCIONAL: Añadir chunk_idx si se desea --
        # FieldSchema(name="chunk_idx", dtype=DataType.INT64),
    ]
    schema = CollectionSchema(fields, description="Document Chunks for Atenex RAG (MiniLM)")

    # ... (resto de la lógica de creación de colección e índices se mantiene)
    # Asegurarse de que los índices escalares para company_id y document_id se crean
    try:
        # ... (creación de colección)
        log.info("Creating vector index...")
        collection.create_index(field_name=MILVUS_VECTOR_FIELD, index_params=settings.MILVUS_INDEX_PARAMS)
        log.info("Creating scalar index for company_id...")
        collection.create_index(field_name=MILVUS_COMPANY_ID_FIELD, index_name="company_id_idx")
        log.info("Creating scalar index for document_id...")
        collection.create_index(field_name=MILVUS_DOCUMENT_ID_FIELD, index_name="document_id_idx")
        # -- OPCIONAL: Añadir índice para chunk_idx --
        # log.info("Creating scalar index for chunk_idx...")
        # collection.create_index(field_name="chunk_idx", index_name="chunk_idx_idx")
        log.info("Indexes created.")
    except MilvusException as e:
        # ... (manejo de error)
        raise RuntimeError(f"Milvus collection/index creation failed: {e}") from e
    return collection


# --- MODIFICAR la función principal del pipeline ---
def ingest_document_pipeline(
    file_bytes: bytes,            # <- CAMBIO: Recibe bytes, no path
    filename: str,                # <- AÑADIDO: Nombre del archivo para logging/metadata
    company_id: str,
    document_id: str,
    content_type: str,
    embedding_model: SentenceTransformer, # <- CAMBIO: Recibe modelo SentenceTransformer
    delete_existing: bool = True
) -> int:
    """
    Processes a single document from bytes: extracts text, chunks, embeds, and inserts into Milvus.
    Args:
        file_bytes: The content of the document as bytes.
        filename: The original name of the file.
        company_id: The company ID for multi-tenancy.
        document_id: The unique ID for the document.
        content_type: The MIME type of the file.
        embedding_model: The initialized SentenceTransformer instance.
        delete_existing: If True, delete existing chunks for this company/document before inserting.
    Returns:
        The number of chunks successfully inserted into Milvus.
    Raises:
        ValueError: If the content type is unsupported.
        Exception: If extraction, chunking, embedding or Milvus operation fails.
    """
    ingest_log = log.bind(
        company_id=company_id,
        document_id=document_id,
        filename=filename,
        content_type=content_type
    )
    ingest_log.info("Starting refactored ingestion pipeline for document")

    # --- 1. Select Extractor ---
    extractor = EXTRACTORS.get(content_type)
    if not extractor:
        ingest_log.error("Unsupported content type for extraction.")
        raise ValueError(f"Unsupported content type: {content_type}")

    # --- 2. Extract Text (Pasando Bytes) ---
    ingest_log.debug("Extracting text content from bytes...")
    try:
        # Pasar bytes y filename al extractor correspondiente
        text_content = extractor(file_bytes, filename=filename)
        if not text_content or text_content.isspace():
            ingest_log.warning("No text content extracted from the document. Skipping.")
            return 0
        ingest_log.info(f"Text extracted successfully, length: {len(text_content)} chars.")
    except EXTRACTION_ERRORS as ve: # Capturar errores específicos de extracción
        ingest_log.error("Text extraction failed.", error=str(ve))
        # Re-lanzar para que la tarea Celery lo maneje como error no recuperable (probablemente)
        raise ValueError(f"Extraction failed for {filename}: {ve}") from ve
    except Exception as e:
        ingest_log.exception("Unexpected error during text extraction")
        raise RuntimeError(f"Unexpected extraction error: {e}") from e


    # --- 3. Chunk Text (Usando la función movida) ---
    ingest_log.debug("Chunking extracted text...")
    try:
        chunks = split_text(text_content) # Usa la función importada de text_splitter
        if not chunks:
            ingest_log.warning("Text content resulted in zero chunks. Skipping.")
            return 0
        ingest_log.info(f"Text chunked into {len(chunks)} chunks.")
    except Exception as e:
        ingest_log.error("Failed to chunk text", error=str(e), exc_info=True)
        raise RuntimeError(f"Chunking failed: {e}") from e

    # --- 4. Embed Chunks (Usando el helper y modelo pasados) ---
    ingest_log.debug(f"Generating embeddings for {len(chunks)} chunks...")
    try:
        # Llamar a embed_chunks directamente; ya usa el modelo cacheado via get_model()
        # No es necesario pasar el modelo aquí si embed_chunks usa get_model() internamente
        embeddings = embed_chunks(chunks)
        ingest_log.info(f"Embeddings generated successfully for {len(embeddings)} chunks.")
        if len(embeddings) != len(chunks):
            # Esto no debería ocurrir con SentenceTransformer si no hay errores
            ingest_log.error("CRITICAL: Mismatch between number of chunks and generated embeddings!",
                             num_chunks=len(chunks), num_embeddings=len(embeddings))
            raise RuntimeError("Embedding count mismatch error.")
        # Validar dimensión del primer embedding (sanity check)
        if embeddings and len(embeddings[0]) != MILVUS_EMBEDDING_DIM:
             ingest_log.error("CRITICAL: Generated embedding dimension mismatch!",
                              actual_dim=len(embeddings[0]), expected_dim=MILVUS_EMBEDDING_DIM)
             raise RuntimeError("Embedding dimension mismatch error.")

    except Exception as e:
        ingest_log.error("Failed to generate embeddings", error=str(e), exc_info=True)
        # Re-lanzar, será capturado por la tarea Celery
        raise RuntimeError(f"Embedding generation failed: {e}") from e

    # --- 5. Prepare Data for Milvus (Mantener lógica actual, **sin chunk_idx por defecto**) ---
    # Truncar texto si excede el límite de VARCHAR en Milvus
    max_content_len = settings.MILVUS_CONTENT_FIELD_MAX_LENGTH if hasattr(settings, 'MILVUS_CONTENT_FIELD_MAX_LENGTH') else 20000 # Ejemplo, ajustar config.py si es necesario
    def truncate_utf8_bytes(s, max_bytes):
        b = s.encode('utf-8')
        if len(b) <= max_bytes: return s
        return b[:max_bytes].decode('utf-8', errors='ignore')

    truncated_chunks = [truncate_utf8_bytes(c, max_content_len) for c in chunks]

    # Preparar listas para inserción. ASEGURARSE que los nombres coinciden con los FieldSchema
    data_to_insert = [
        embeddings,                     # Corresponde a MILVUS_VECTOR_FIELD
        truncated_chunks,               # Corresponde a MILVUS_CONTENT_FIELD
        [company_id] * len(chunks),     # Corresponde a MILVUS_COMPANY_ID_FIELD
        [document_id] * len(chunks),    # Corresponde a MILVUS_DOCUMENT_ID_FIELD
        [filename] * len(chunks),       # Corresponde a MILVUS_FILENAME_FIELD
        # -- OPCIONAL: Añadir chunk_idx si se añadió al schema --
        # list(range(len(chunks))) # Correspondería a "chunk_idx"
    ]
    field_names_for_insert = [
        MILVUS_VECTOR_FIELD, MILVUS_CONTENT_FIELD, MILVUS_COMPANY_ID_FIELD,
        MILVUS_DOCUMENT_ID_FIELD, MILVUS_FILENAME_FIELD
        # , "chunk_idx" # Si se añade
    ]
    ingest_log.debug(f"Prepared {len(chunks)} entities for Milvus insertion.", fields=field_names_for_insert)


    # --- 6. Delete Existing Chunks (Optional) ---
    if delete_existing:
        ingest_log.info("Attempting to delete existing chunks before insertion...")
        try:
            deleted_count = delete_milvus_chunks(company_id, document_id)
            ingest_log.info(f"Deleted {deleted_count} existing chunks.")
        except Exception as del_err:
            # No crítico, continuar con la inserción
            ingest_log.error("Failed to delete existing chunks, proceeding with insert anyway.", error=str(del_err))

    # --- 7. Insert into Milvus ---
    ingest_log.debug(f"Inserting {len(chunks)} chunks into Milvus collection '{MILVUS_COLLECTION_NAME}'...")
    try:
        collection = _ensure_milvus_connection_and_collection()
        # Insertar usando la lista de listas (o lista de dicts si se prefiere)
        mutation_result = collection.insert(data_to_insert)
        inserted_count = mutation_result.insert_count

        if inserted_count == len(chunks):
            ingest_log.info(f"Successfully inserted {inserted_count} chunks into Milvus.")
        else:
            # Esto indica un problema serio en la inserción
            ingest_log.error(f"CRITICAL: Milvus insert count mismatch!",
                             expected=len(chunks), inserted=inserted_count, pk_errors=mutation_result.err_indices)
            # Podría lanzar error aquí o devolver 0 para indicar fallo parcial/total
            # Devolver 0 por ahora, se marcará como error en la tarea
            return 0

        # Flush para asegurar visibilidad (importante si se consulta inmediatamente después)
        log.debug("Flushing Milvus collection...")
        collection.flush()
        log.info("Milvus collection flushed.")

        return inserted_count

    except MilvusException as e:
        ingest_log.error("Failed to insert data into Milvus", error=str(e), exc_info=True)
        raise RuntimeError(f"Milvus insertion failed: {e}") from e
    except Exception as e:
        ingest_log.exception("Unexpected error during Milvus insertion")
        raise RuntimeError(f"Unexpected Milvus insertion error: {e}") from e

```

### 2.4 Actualizar Worker Celery (`process_document.py`)

*   **Eliminar** la inicialización global/local de `fastembed.TextEmbedding`.
*   **Importar** el nuevo `embedder`: `from app.services.embedder import get_embedding_model`
*   **Asegurar Carga del Modelo:** Puedes confiar en que `get_embedding_model()` se llame y cachee en la primera ejecución de `embed_chunks` dentro de `ingest_document_pipeline`, O explícitamente precargarlo al inicio del worker. La precarga es más predecible:

```python
# app/tasks/process_document.py
# ... (otras importaciones)
from celery.signals import worker_process_init
from app.services.embedder import get_embedding_model
from sentence_transformers import SentenceTransformer # Para type hinting

# ... (configuración logger, IS_WORKER, sync_engine, minio_client)

# --- ELIMINAR inicialización global de embedding_model (FastEmbed) ---
# embedding_model: Optional[TextEmbedding] = None # <- ELIMINAR

# --- NUEVO: Variable global para el modelo cargado en el worker ---
worker_embedding_model: Optional[SentenceTransformer] = None

@worker_process_init.connect(weak=False)
def init_embedding_model_on_worker(**kwargs):
    """Signal handler to pre-load the embedding model when a worker process starts."""
    global worker_embedding_model
    log = task_struct_log.bind(signal="worker_process_init")
    log.info("Worker process initializing... Attempting to preload embedding model.")
    try:
        # Llamar a get_embedding_model fuerza la carga y cacheo
        worker_embedding_model = get_embedding_model()
        if worker_embedding_model:
            log.info("Embedding model preloaded successfully for this worker process.")
        else:
            # Esto no debería ocurrir si get_embedding_model maneja errores
            log.error("Embedding model preloading returned None unexpectedly.")
    except Exception as e:
        # Log crítico, pero no detener el worker, la tarea fallará después si el modelo no está
        log.critical("Failed to preload embedding model during worker init!", error=str(e), exc_info=True)
        worker_embedding_model = None


# --- Tarea Celery (`process_document_standalone`) ---
@celery_app.task(...) # Mantener decorador existente
def process_document_standalone(self: Task, *args, **kwargs) -> Dict[str, Any]:
    # ... (obtener args: document_id_str, company_id_str, etc.)
    # ... (configurar logger: log)
    log.info("Starting REFACTORED standalone document processing task (MiniLM)")

    # --- Pre-checks (Verificar recursos globales inicializados) ---
    # ... (mantener checks para IS_WORKER, args, sync_engine, minio_client)

    # --- NUEVO: Check explícito del modelo pre-cargado ---
    if worker_embedding_model is None:
        log.critical("Worker Embedding Model (SentenceTransformer) is not available/loaded. Task cannot proceed.")
        error_msg = "Worker embedding model init/preload failed."
        # Intentar actualizar DB a ERROR si es posible
        if sync_engine and document_id_str:
             try:
                 doc_uuid_err = uuid.UUID(document_id_str)
                 set_status_sync(engine=sync_engine, document_id=doc_uuid_err, status=DocumentStatus.ERROR, error_message=error_msg)
             except Exception as db_err:
                 log.critical("Failed to update status to ERROR after embedding model check failure!", error=str(db_err))
        # Rechazar permanentemente
        raise Reject(error_msg, requeue=False)
    # --- Fin Pre-checks ---

    # ... (validar doc_uuid, content_type)

    object_name = f"{company_id_str}/{document_id_str}/{filename}"
    temp_file_path_obj: Optional[pathlib.Path] = None
    inserted_chunk_count = 0

    try:
        # 1. Update status to PROCESSING (sin cambios)
        # ...

        # 2. Download file from MinIO (sin cambios en lógica de descarga/retry)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_dir_path = pathlib.Path(temp_dir)
            temp_file_path_obj = temp_dir_path / filename
            log.info(f"Downloading MinIO object: {object_name} -> {str(temp_file_path_obj)}")
            # ... (lógica de descarga síncrona con retry)

            # --- CAMBIO CRÍTICO: Leer archivo a bytes ---
            log.debug(f"Reading downloaded file content into bytes: {str(temp_file_path_obj)}")
            try:
                file_bytes = temp_file_path_obj.read_bytes()
                log.info(f"File content read into memory ({len(file_bytes)} bytes).")
            except Exception as read_err:
                log.error("Failed to read downloaded file into bytes", error=str(read_err), exc_info=True)
                raise RuntimeError(f"Failed to read temp file: {read_err}") from read_err
            # ------------------------------------------

            # 3. Execute Standalone Ingestion Pipeline (Pasando Bytes y Modelo)
            log.info("Executing refactored ingest pipeline (extract, chunk, embed, insert)...")
            # --- Pasar file_bytes, filename y el modelo cargado ---
            inserted_chunk_count = ingest_document_pipeline(
                file_bytes=file_bytes,            # <- Bytes leídos
                filename=filename,                # <- Nombre original
                company_id=company_id_str,
                document_id=document_id_str,
                content_type=content_type,
                embedding_model=worker_embedding_model, # <- Modelo cargado
                delete_existing=True
            )
            # ----------------------------------------------------
            log.info(f"Ingestion pipeline finished. Inserted chunks reported: {inserted_chunk_count}")

        # 4. Update status to PROCESSED (sin cambios, usa inserted_chunk_count)
        # ... (actualizar a PROCESSED con chunk_count)

        log.info(f"Document processing finished successfully. Final chunk count: {inserted_chunk_count}")
        return {"status": DocumentStatus.PROCESSED.value, "chunks_inserted": inserted_chunk_count, "document_id": document_id_str}

    # --- Error Handling ---
    # Capturar errores específicos si es necesario, pero el manejo general existente debería funcionar
    except MinioError as me:
        # ... (manejo existente: log, set_status_sync, Reject/Raise)
        pass # Mantener lógica actual
    except (ValueError, ConnectionError, RuntimeError, TypeError) as pipeline_err:
         # Incluye errores de extracción, chunking (si falla), embedding, Milvus
         log.error(f"Pipeline Error: {pipeline_err}", exc_info=True)
         error_msg = f"Pipeline Error: {type(pipeline_err).__name__} - {str(pipeline_err)[:400]}"
         # ... (set_status_sync a ERROR)
         # Rechazar para evitar reintentos de errores probablemente persistentes
         raise Reject(f"Pipeline failed: {error_msg}", requeue=False) from pipeline_err
    # ... (mantener manejo de Reject, Ignore, MaxRetriesExceededError, Exception general)

# ... (mantener alias process_document_haystack_task si se usa en otro lugar)
# process_document_haystack_task = process_document_standalone
```

### 2.5 Configuración (`app/core/config.py`)

```python
# app/core/config.py
# ... (otras configuraciones)

class Settings(BaseSettings):
    # ...

    # --- Embeddings (ACTUALIZADO) ---
    # Nombre/ID del modelo SentenceTransformer a usar
    EMBEDDING_MODEL_ID: str = Field(default="sentence-transformers/all-MiniLM-L6-v2", validation_alias="INGEST_EMBEDDING_MODEL_ID")
    # Dimensión esperada del modelo (CRÍTICO: debe coincidir con el modelo y Milvus)
    EMBEDDING_DIMENSION: int = Field(default=384, validation_alias="INGEST_EMBEDDING_DIMENSION")
    # OPENAI_API_KEY: Optional[SecretStr] = None # Mantener si se usa en otro lugar, si no, eliminar

    # --- ELIMINAR Configuración de FastEmbed ---
    # FASTEMBED_MODEL: str = DEFAULT_FASTEMBED_MODEL # <- ELIMINAR
    # USE_GPU: bool = False # <- ELIMINAR (o reevaluar para SentenceTransformers)

    # --- Processing ---
    SUPPORTED_CONTENT_TYPES: List[str] = Field(default=[
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", # DOCX
        "application/msword", # DOC (manejado por python-docx, puede ser básico)
        "text/plain",
        "text/markdown",
        "text/html"
    ])
    # Parámetros para el chunker custom (ahora en text_splitter.py)
    SPLITTER_CHUNK_SIZE: int = Field(default=1000, validation_alias="INGEST_SPLITTER_CHUNK_SIZE")
    SPLITTER_CHUNK_OVERLAP: int = Field(default=200, validation_alias="INGEST_SPLITTER_CHUNK_OVERLAP")
    # SPLITTER_SPLIT_BY: str = "word" # <- No usado por el chunker actual

    # ... (Validadores)

    @field_validator('EMBEDDING_DIMENSION')
    @classmethod
    def check_embedding_dimension(cls, v: int, info: ValidationInfo) -> int:
        if v <= 0: raise ValueError("EMBEDDING_DIMENSION must be a positive integer.")
        # Validar que sea 384 si se usa el MiniLM por defecto? O dejar flexible?
        # model_id = info.data.get('EMBEDDING_MODEL_ID', '') # Acceder a otros campos
        # if 'MiniLM' in model_id and v != 384:
        #     log.warning(f"MiniLM model typically has 384 dimensions, but configured dimension is {v}")
        logging.debug(f"Using EMBEDDING_DIMENSION: {v}")
        return v

    # ... (Resto de la clase Settings)

# ... (Instancia global y carga de settings)
# Asegurarse que los logs al inicio reflejen las nuevas variables EMBEDDING_MODEL_ID, EMBEDDING_DIMENSION
# y eliminen las de FASTEMBED_MODEL.
```

*   Actualizar el archivo `.env` o el ConfigMap/Secret de Kubernetes para reflejar los nuevos nombres (`INGEST_EMBEDDING_MODEL_ID`, `INGEST_EMBEDDING_DIMENSION`) y eliminar los antiguos (`INGEST_FASTEMBED_MODEL`).

### 2.6 Migración de Esquema Milvus (**CRÍTICO**)

1.  **Decisión Principal:** Debido al cambio en `EMBEDDING_DIMENSION` (de 1024 a 384), la forma más limpia y recomendada es **eliminar la colección Milvus existente y dejar que el servicio la recree con el nuevo esquema** al iniciarse o al procesar el primer documento.
    *   `collection.drop()`
2.  **Alternativa (Compleja):** Migrar implicaría crear una nueva colección con la dimensión correcta, leer todos los datos de la antigua (excepto los vectores), re-generar los embeddings con el nuevo modelo MiniLM para todos los chunks, e insertar en la nueva colección. Esto es propenso a errores y lento.
3.  **Verificación del Esquema Actual:** Antes de eliminar, verifica si tu esquema Milvus actual ya contiene `company_id` y `document_id` como VARCHAR. Si no, la recreación también añadirá estos campos correctamente según `_create_milvus_collection`.
4.  **No Añadir `company_id`:** Reiterar que el código actual ya maneja `company_id`, no es necesario añadirlo.
5.  **`chunk_idx` (Opcional):** Si decides que necesitas un índice numérico para los chunks dentro de un documento, añade `FieldSchema(name="chunk_idx", dtype=DataType.INT64)` a `_create_milvus_collection` y la lista `list(range(len(chunks)))` a `data_to_insert` en `ingest_pipeline.py`.

---

## 3 · Código Guía MiniLM (Sin Cambios)

El código en `embedder.py` ya sigue esta guía.

---

## 4 · Checklist Final (Corregido y Detallado)

1.  **Dependencias:** Eliminar `fastembed`. Añadir `sentence-transformers`, `pymupdf`. Verificar `python-docx` y los necesarios para MD/HTML. **Eliminar `langchain`**. Ejecutar `poetry lock && poetry install`.
2.  **Crear Módulos:** Crear `app/services/extractors/` y los archivos `.py` para `pdf`, `docx`, `txt`, `md`, `html`.
3.  **Crear/Mover Módulos:** Crear `app/services/text_splitter.py` moviendo la lógica de `chunk_text` existente. Crear `app/services/embedder.py` con la lógica de `SentenceTransformer`.
4.  **Refactorizar `ingest_pipeline.py`:**
    *   Cambiar firma para aceptar `file_bytes`, `filename`.
    *   Usar los nuevos helpers de `extractors/`, `text_splitter`, `embedder`.
    *   Actualizar `_create_milvus_collection` para usar `EMBEDDING_DIMENSION=384`.
    *   Verificar que la lógica de `collection.insert()` coincida con el esquema definido (incluyendo `company_id`, `document_id`, `file_name` que ya estaban).
5.  **Refactorizar `process_document.py` (Tarea Celery):**
    *   Eliminar referencias/inicialización de `FastEmbed`.
    *   Importar y usar `get_embedding_model`. Considerar precarga con `@worker_process_init.connect`. Añadir check de `worker_embedding_model`.
    *   **Leer el archivo descargado a `bytes`**.
    *   Llamar a `ingest_document_pipeline` pasando `file_bytes`, `filename` y el modelo cargado (`worker_embedding_model`).
6.  **Ajustar `config.py`:** Renombrar `FASTEMBED_MODEL` -> `EMBEDDING_MODEL_ID`. Cambiar `EMBEDDING_DIMENSION` a 384. Eliminar config obsoleta.
7.  **Ejecutar Migración Milvus:** **LA FORMA MÁS SEGURA ES BORRAR LA COLECCIÓN ANTIGUA (`collection.drop()`)**. El servicio la recreará con la dimensión 384. **¡ESTO BORRARÁ TODOS LOS DATOS INDEXADOS!**

