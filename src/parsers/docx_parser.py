from docx import Document


def parse_docx(filepath: str) -> list[dict]:
    """Extract text from a DOCX file paragraph by paragraph."""
    doc = Document(filepath)
    full_text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    if not full_text.strip():
        return []
    return [{
        "text": full_text,
        "metadata": {
            "source": filepath,
            "page": 1,
        },
    }]
