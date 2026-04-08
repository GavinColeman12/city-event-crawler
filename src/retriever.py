from .embeddings import embed_query
from .vectorstore import get_collection


def retrieve(query: str, config: dict, client_id: str = None) -> list[dict]:
    """Retrieve the most relevant policy chunks for a query.

    Returns list of dicts with 'text', 'metadata', and 'score' keys,
    filtered by the similarity threshold from config.
    """
    top_k = config.get("chat", {}).get("retrieval", {}).get("top_k", 5)
    threshold = config.get("chat", {}).get("retrieval", {}).get("similarity_threshold", 0.3)

    collection = get_collection(client_id)
    if collection.count() == 0:
        return []

    query_embedding = embed_query(query)
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k,
        include=["documents", "metadatas", "distances"],
    )

    chunks = []
    for doc, meta, distance in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        # ChromaDB cosine distance: 0 = identical, 2 = opposite
        # Convert to similarity: 1 - (distance / 2)
        similarity = 1 - (distance / 2)
        if similarity >= threshold:
            chunks.append({
                "text": doc,
                "metadata": meta,
                "score": round(similarity, 4),
            })

    return chunks
