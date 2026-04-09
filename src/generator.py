import os
import time

from anthropic import Anthropic, APIStatusError
from dotenv import load_dotenv

from .config import get_config
from .retriever import retrieve
from .sensitive_topics import is_sensitive, get_escalation_message
from .analytics import log_query, auto_categorize

from pathlib import Path
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

_client = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _client


def generate_response(query: str, conversation_history: list = None, config: dict = None) -> dict:
    """Generate an HR policy response for a query.

    Returns dict with keys:
        - response: str (the answer text)
        - sources: list[dict] (retrieved chunks with metadata)
        - was_sensitive: bool
        - was_answered: bool
        - category: str
    """
    if config is None:
        config = get_config()

    start_time = time.time()

    # Check sensitivity
    sensitive = is_sensitive(query, config)

    # Retrieve relevant chunks
    chunks = retrieve(query, config, client_id=config.get("client", {}).get("id"))

    # Determine if we can answer
    was_answered = len(chunks) > 0

    # Build context from chunks
    if chunks:
        context_parts = []
        for i, chunk in enumerate(chunks, 1):
            source = os.path.basename(chunk["metadata"].get("source", "Unknown"))
            source_name = source.replace("_", " ").replace(".md", "").replace(".pdf", "").title()
            context_parts.append(
                f"[Document {i}: {source_name}]\n{chunk['text']}"
            )
        context = "\n\n---\n\n".join(context_parts)
    else:
        context = ""

    # Build system prompt
    system_prompt = config.get("chat", {}).get("system_prompt", "You are an HR assistant.")

    if context:
        system_prompt += f"\n\nHere are the relevant policy documents to base your answer on:\n\n{context}"
    else:
        system_prompt += (
            "\n\nNo relevant policy documents were found for this question. "
            "Respond with the standard 'contact HR' message."
        )

    # Build messages
    messages = []
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": query})

    # Generate response
    model = config.get("chat", {}).get("model", "claude-sonnet-4-20250514")
    max_tokens = config.get("chat", {}).get("max_tokens", 800)

    client = _get_client()

    # Retry up to 3 times on overloaded/transient errors
    answer = None
    for attempt in range(3):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=messages,
            )
            answer = response.content[0].text
            break
        except APIStatusError as e:
            if e.status_code in (429, 529, 503) and attempt < 2:
                time.sleep(2 ** attempt)  # 1s, 2s backoff
                continue
            raise

    if answer is None:
        answer = "I'm experiencing high demand right now. Please try again in a moment."

    # Append escalation message for sensitive topics
    if sensitive:
        answer += get_escalation_message(config)

    elapsed_ms = int((time.time() - start_time) * 1000)

    # Log to analytics
    analytics_config = config.get("analytics", {})
    if analytics_config.get("enabled", True) and analytics_config.get("log_queries", True):
        log_query(query, was_answered, sensitive, elapsed_ms)

    # Build sources list for display
    sources = []
    for chunk in chunks:
        source_file = os.path.basename(chunk["metadata"].get("source", "Unknown"))
        source_name = source_file.replace("_", " ").replace(".md", "").replace(".pdf", "").title()
        sources.append({
            "document": source_name,
            "score": chunk["score"],
            "text": chunk["text"][:300] + "..." if len(chunk["text"]) > 300 else chunk["text"],
        })

    return {
        "response": answer,
        "sources": sources,
        "was_sensitive": sensitive,
        "was_answered": was_answered,
        "category": auto_categorize(query),
    }
