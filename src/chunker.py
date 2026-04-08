import tiktoken

_encoder = tiktoken.get_encoding("cl100k_base")

CHUNK_SIZE = 500
CHUNK_OVERLAP = 50


def _token_length(text: str) -> int:
    return len(_encoder.encode(text))


def chunk_documents(
    documents: list[dict],
    chunk_size: int = CHUNK_SIZE,
    chunk_overlap: int = CHUNK_OVERLAP,
) -> list[dict]:
    """Split parsed documents into token-sized chunks with overlap.

    Each input document is a dict with "text" and "metadata" keys.
    Returns a list of chunks, each inheriting the source metadata
    plus a chunk_index field.
    """
    all_chunks = []

    for doc in documents:
        text = doc["text"]
        metadata = doc["metadata"]
        sentences = _split_into_sentences(text)
        chunks = _merge_sentences_into_chunks(sentences, chunk_size, chunk_overlap)

        for i, chunk_text in enumerate(chunks):
            all_chunks.append({
                "text": chunk_text,
                "metadata": {
                    **metadata,
                    "chunk_index": i,
                },
            })

    return all_chunks


def _split_into_sentences(text: str) -> list[str]:
    """Split text on sentence boundaries (periods, newlines) while keeping fragments together."""
    import re
    parts = re.split(r'(?<=[.!?])\s+|\n\n+', text)
    return [p.strip() for p in parts if p.strip()]


def _merge_sentences_into_chunks(
    sentences: list[str],
    chunk_size: int,
    chunk_overlap: int,
) -> list[str]:
    """Merge sentences into chunks of approximately chunk_size tokens with overlap."""
    if not sentences:
        return []

    chunks = []
    current_sentences = []
    current_tokens = 0

    for sentence in sentences:
        sentence_tokens = _token_length(sentence)

        if sentence_tokens > chunk_size:
            if current_sentences:
                chunks.append(" ".join(current_sentences))
                current_sentences = []
                current_tokens = 0
            chunks.extend(_split_by_tokens(sentence, chunk_size, chunk_overlap))
            continue

        if current_tokens + sentence_tokens > chunk_size and current_sentences:
            chunks.append(" ".join(current_sentences))

            overlap_sentences = []
            overlap_tokens = 0
            for s in reversed(current_sentences):
                s_tokens = _token_length(s)
                if overlap_tokens + s_tokens > chunk_overlap:
                    break
                overlap_sentences.insert(0, s)
                overlap_tokens += s_tokens

            current_sentences = overlap_sentences
            current_tokens = overlap_tokens

        current_sentences.append(sentence)
        current_tokens += sentence_tokens

    if current_sentences:
        chunks.append(" ".join(current_sentences))

    return chunks


def _split_by_tokens(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """Split a long text into chunks by token count when no sentence boundaries exist."""
    tokens = _encoder.encode(text)
    chunks = []
    start = 0
    while start < len(tokens):
        end = min(start + chunk_size, len(tokens))
        chunk_tokens = tokens[start:end]
        chunks.append(_encoder.decode(chunk_tokens))
        start = end - chunk_overlap if end < len(tokens) else end
    return chunks
