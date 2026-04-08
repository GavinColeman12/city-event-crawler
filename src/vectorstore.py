from pathlib import Path

import chromadb

from .embeddings import embed_texts

COLLECTION_NAME = "documents"
CHROMA_PATH = str(Path(__file__).resolve().parent.parent / "chroma_db")

_client = None
_collections: dict = {}


def get_client() -> chromadb.ClientAPI:
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=CHROMA_PATH)
    return _client


def _collection_name(client_id: str = None) -> str:
    if client_id:
        return f"client_{client_id}"
    return COLLECTION_NAME


def get_collection(client_id: str = None, client: chromadb.ClientAPI = None):
    name = _collection_name(client_id)
    if name not in _collections:
        if client is None:
            client = get_client()
        _collections[name] = client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
        )
    return _collections[name]


def add_chunks(chunks: list[dict], client_id: str = None) -> int:
    """Add document chunks to the vector store in batches. Returns number of chunks added."""
    if not chunks:
        return 0

    collection = get_collection(client_id)
    batch_size = 500

    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        texts = [c["text"] for c in batch]
        metadatas = [c["metadata"] for c in batch]
        ids = [
            f"{c['metadata']['source']}::p{c['metadata'].get('page', 0)}::chunk_{c['metadata']['chunk_index']}"
            for c in batch
        ]
        embeddings = embed_texts(texts)
        collection.add(
            documents=texts,
            embeddings=embeddings,
            metadatas=metadatas,
            ids=ids,
        )

    return len(chunks)


def list_documents(client_id: str = None) -> dict[str, int]:
    """List all ingested documents with their chunk counts."""
    collection = get_collection(client_id)
    total = collection.count()
    if total == 0:
        return {}

    doc_counts: dict[str, int] = {}
    batch_size = 5000
    offset = 0

    while offset < total:
        results = collection.get(
            include=["metadatas"],
            limit=batch_size,
            offset=offset,
        )
        for meta in results["metadatas"]:
            source = meta.get("source", "unknown")
            doc_counts[source] = doc_counts.get(source, 0) + 1
        fetched = len(results["metadatas"])
        if fetched == 0:
            break
        offset += fetched

    return doc_counts


def get_ingested_sources(client_id: str = None) -> set[str]:
    """Return the set of all source filepaths already in the store."""
    return set(list_documents(client_id).keys())


def delete_by_source(source: str, client_id: str = None) -> int:
    """Delete all chunks for a given source file. Returns count deleted."""
    collection = get_collection(client_id)
    results = collection.get(
        where={"source": source},
        include=[],
    )
    ids = results["ids"]
    if ids:
        for i in range(0, len(ids), 500):
            collection.delete(ids=ids[i:i+500])
    return len(ids)
