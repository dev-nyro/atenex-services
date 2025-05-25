import io
import pandas as pd
import structlog
from typing import List, Tuple, Dict, Any

from app.application.ports.extraction_port import ExtractionPort, ExtractionError, UnsupportedContentTypeError
from app.infrastructure.extractors.base_extractor import BaseExtractorAdapter

log = structlog.get_logger(__name__)

class ExcelAdapter(BaseExtractorAdapter):
    """Adaptador para extraer texto de archivos Excel (XLSX, XLS)."""

    SUPPORTED_CONTENT_TYPES = [
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # .xlsx
        "application/vnd.ms-excel"  # .xls
    ]

    def extract_text(
        self,
        file_bytes: bytes,
        filename: str,
        content_type: str
    ) -> Tuple[List[Tuple[int, str]], Dict[str, Any]]:
        content_type_lower = content_type.lower()
        if content_type_lower not in self.SUPPORTED_CONTENT_TYPES:
            raise UnsupportedContentTypeError(f"ExcelAdapter does not support content type: {content_type}")

        log.debug("ExcelAdapter: Extracting text from Excel bytes", filename=filename, content_type=content_type)
        pages_content: List[Tuple[int, str]] = []
        extraction_metadata: Dict[str, Any] = {
            "total_sheets_extracted": 0,
            "sheet_names": []
        }

        try:
            # Pandas usa openpyxl para xlsx y puede usar xlrd para xls.
            # Si se necesita específicamente xlrd para .xls antiguos, asegurar que esté instalado.
            # Por defecto, pandas intentará el motor apropiado.
            excel_file = pd.ExcelFile(io.BytesIO(file_bytes))
            sheet_names = excel_file.sheet_names
            extraction_metadata["sheet_names"] = sheet_names
            
            log.info("ExcelAdapter: Processing Excel file", filename=filename, num_sheets=len(sheet_names), sheet_names_list=sheet_names)

            for i, sheet_name in enumerate(sheet_names):
                page_num_one_based = i + 1
                try:
                    df = excel_file.parse(sheet_name)
                    if not df.empty:
                        # Convertir DataFrame a Markdown. Incluir el índice puede ser útil o no.
                        # index=False evita escribir el índice numérico del DataFrame.
                        # tablefmt="pipe" es un formato común de Markdown para tablas.
                        markdown_text = df.to_markdown(index=False, tablefmt="pipe")
                        
                        # Añadir un título con el nombre de la hoja al principio del texto Markdown
                        sheet_title_md = f"# Hoja: {sheet_name}\n\n"
                        full_sheet_text = sheet_title_md + markdown_text

                        if full_sheet_text.strip():
                            pages_content.append((page_num_one_based, full_sheet_text))
                            log.debug("ExcelAdapter: Extracted text from sheet", sheet_name=sheet_name, page_num=page_num_one_based, length=len(full_sheet_text))
                        else:
                            log.debug("ExcelAdapter: Skipping empty sheet after markdown conversion", sheet_name=sheet_name, page_num=page_num_one_based)
                    else:
                        log.debug("ExcelAdapter: Skipping empty DataFrame for sheet", sheet_name=sheet_name, page_num=page_num_one_based)
                except Exception as sheet_err:
                    log.warning("ExcelAdapter: Error extracting text from sheet", filename=filename, sheet_name=sheet_name, page_num=page_num_one_based, error=str(sheet_err))
            
            extraction_metadata["total_sheets_extracted"] = len(pages_content)
            log.info("ExcelAdapter: Excel extraction successful", filename=filename, sheets_with_text=len(pages_content), total_doc_sheets=len(sheet_names))
            return pages_content, extraction_metadata
        except Exception as e:
            raise self._handle_extraction_error(e, filename, "ExcelAdapter")