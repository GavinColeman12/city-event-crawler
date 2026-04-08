import fitz


def parse_pdf(filepath: str) -> list[dict]:
    """Extract text from a PDF file page by page using PyMuPDF."""
    doc = fitz.open(filepath)
    pages = []
    for page_num in range(len(doc)):
        text = doc[page_num].get_text()
        if text.strip():
            pages.append({
                "text": text,
                "metadata": {
                    "source": filepath,
                    "page": page_num + 1,
                },
            })
    doc.close()
    return pages
