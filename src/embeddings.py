import os

from sentence_transformers import SentenceTransformer

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

_model = None


def get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        try:
            _model = SentenceTransformer("all-MiniLM-L6-v2")
        except Exception:
            os.environ.pop("HF_HUB_OFFLINE", None)
            os.environ.pop("TRANSFORMERS_OFFLINE", None)
            _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Generate embeddings for a list of texts using sentence-transformers."""
    model = get_model()
    embeddings = model.encode(texts, show_progress_bar=False)
    return embeddings.tolist()


def embed_query(query: str) -> list[float]:
    """Generate an embedding for a single query string."""
    return embed_texts([query])[0]
