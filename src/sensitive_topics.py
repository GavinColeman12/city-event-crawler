import re


def is_sensitive(query: str, config: dict) -> bool:
    """Check if a query touches on sensitive HR topics via keyword match."""
    sensitive_keywords = config.get("chat", {}).get("sensitive_topics", [])
    query_lower = query.lower()
    for keyword in sensitive_keywords:
        if re.search(r'\b' + re.escape(keyword.lower()) + r'\b', query_lower):
            return True
    return False


def get_escalation_message(config: dict) -> str:
    """Return the standard escalation message for sensitive topics."""
    hr_email = config.get("client", {}).get("hr_email", "HR")
    return (
        f"\n\n---\n**For your specific situation, I'd recommend speaking with HR directly "
        f"at {hr_email} — they can provide confidential, personalized guidance.**"
    )
