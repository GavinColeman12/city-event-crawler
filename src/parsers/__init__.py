import os

from .pdf_parser import parse_pdf
from .docx_parser import parse_docx
from .text_parser import parse_text

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".md", ".txt"}

_PARSER_MAP = {
    ".pdf": parse_pdf,
    ".docx": parse_docx,
    ".md": parse_text,
    ".txt": parse_text,
}


def parse_file(filepath: str) -> list[dict]:
    """Parse a file based on its extension. Returns list of text+metadata dicts."""
    ext = os.path.splitext(filepath)[1].lower()
    parser = _PARSER_MAP.get(ext)
    if parser is None:
        raise ValueError(f"Unsupported file type: {ext}")
    return parser(filepath)
