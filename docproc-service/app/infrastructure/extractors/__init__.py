from .base_extractor import BaseExtractorAdapter
from .pdf_adapter import PdfAdapter
from .docx_adapter import DocxAdapter
from .txt_adapter import TxtAdapter
from .html_adapter import HtmlAdapter
from .md_adapter import MdAdapter
from .excel_adapter import ExcelAdapter # NUEVA LÍNEA

__all__ = [
    "BaseExtractorAdapter",
    "PdfAdapter",
    "DocxAdapter",
    "TxtAdapter",
    "HtmlAdapter",
    "MdAdapter",
    "ExcelAdapter", # NUEVA LÍNEA
]