def parse_text(filepath: str) -> list[dict]:
    """Read a text or markdown file, preserving structure."""
    with open(filepath, "r", encoding="utf-8") as f:
        text = f.read()
    if not text.strip():
        return []
    return [{
        "text": text,
        "metadata": {
            "source": filepath,
            "page": 1,
        },
    }]
