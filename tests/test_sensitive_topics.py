"""Tests for sensitive topic detection."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.sensitive_topics import is_sensitive, get_escalation_message


def test_sensitive_detection():
    config = load_config()

    # Should be sensitive
    assert is_sensitive("Am I going to get fired?", config) is True
    assert is_sensitive("I want to file a harassment complaint", config) is True
    assert is_sensitive("What happens during a PIP?", config) is True
    assert is_sensitive("Can I take FMLA leave?", config) is True
    assert is_sensitive("I need a disability accommodation", config) is True
    assert is_sensitive("I'm worried about retaliation", config) is True
    assert is_sensitive("What is the termination process?", config) is True
    assert is_sensitive("I'm being laid off", config) is True
    assert is_sensitive("I need to talk to a lawyer about this", config) is True
    assert is_sensitive("Can I take medical leave for mental health?", config) is True

    # Should NOT be sensitive
    assert is_sensitive("What's the dress code?", config) is False
    assert is_sensitive("How much PTO do I have?", config) is False
    assert is_sensitive("What health plans are available?", config) is False
    assert is_sensitive("How do I submit expenses?", config) is False
    assert is_sensitive("What days do I need to be in office?", config) is False


def test_escalation_message():
    config = load_config()
    msg = get_escalation_message(config)
    assert "hr@horizontech.com" in msg
    assert "speaking with HR directly" in msg


if __name__ == "__main__":
    test_sensitive_detection()
    test_escalation_message()
    print("All sensitive topics tests passed!")
