"""End-to-end tests: ingest sample data, query, and verify responses."""
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from src.config import load_config
from src.generator import generate_response
from src.sensitive_topics import is_sensitive
from src.analytics import get_dashboard_data, init_db

# Test questions with expected keywords in the response
TEST_QUESTIONS = [
    # PTO & Leave
    ("How much PTO do I get after 2 years?", ["20 days", "160 hours"]),
    ("Can I carry over PTO to next year?", ["5 days", "40 hours", "March 31"]),
    ("What's the parental leave policy for dads?", ["8 weeks", "100%"]),
    ("How do I request time off?", ["BambooHR", "2 weeks"]),

    # Benefits
    ("What health insurance plans do you offer?", ["Bronze", "Silver", "Gold"]),
    ("What's the 401k match?", ["4%", "100%"]),
    ("How much is the learning stipend?", ["2,500"]),
    ("When can I start using benefits?", ["first day of the month", "30 days"]),

    # Remote Work
    ("Can I work from another country?", ["4 weeks", "core hours"]),
    ("What days do I need to be in the office?", ["Tuesday", "Wednesday", "Thursday"]),
    ("Is there a home office stipend?", ["500"]),

    # Expenses
    ("What's the meal allowance when traveling?", ["80", "15", "25", "40"]),
    ("How do I submit expenses?", ["Expensify", "30 days"]),
    ("Can I fly business class?", ["4 hours", "manager"]),

    # Policy
    ("What's the dress code?", ["business casual"]),
    ("How do I report harassment?", ["hr@horizontech.com", "1-800-555-ETHICS"]),

    # Compensation
    ("When are annual raises?", ["March"]),
    ("What's the bonus structure?", ["5", "10", "15", "20"]),

    # IT Security
    ("What's the password policy?", ["12 characters", "MFA"]),

    # Out of scope
    ("What's the stock price?", ["don't have", "contact HR"]),
]


@pytest.fixture(scope="session")
def config():
    return load_config()


@pytest.fixture(scope="session", autouse=True)
def ensure_ingested(config):
    """Ensure sample data is ingested before running tests."""
    from src.vectorstore import get_collection
    client_id = config.get("client", {}).get("id")
    collection = get_collection(client_id)
    if collection.count() == 0:
        pytest.skip(
            "No documents ingested. Run 'python ingest.py --path sample_data/' first."
        )


@pytest.mark.parametrize("question,expected_keywords", TEST_QUESTIONS)
def test_question_response(question, expected_keywords, config):
    """Test that each question returns a response containing expected keywords."""
    result = generate_response(question, config=config)

    assert result["response"], f"Empty response for: {question}"

    response_lower = result["response"].lower()
    found_any = False
    for keyword in expected_keywords:
        if keyword.lower() in response_lower:
            found_any = True
            break

    assert found_any, (
        f"Question: {question}\n"
        f"Expected one of: {expected_keywords}\n"
        f"Response: {result['response'][:500]}"
    )


def test_sensitive_topic_escalation(config):
    """Test that sensitive queries trigger escalation message."""
    result = generate_response("What happens if I get fired?", config=config)
    assert result["was_sensitive"] is True
    assert "hr@horizontech.com" in result["response"].lower() or "speaking with HR" in result["response"]


def test_unanswered_logging(config):
    """Test that out-of-scope queries are logged as unanswered."""
    result = generate_response("What is the company's stock ticker symbol?", config=config)
    # Should either not be answered or contain the fallback message
    response_lower = result["response"].lower()
    assert (
        not result["was_answered"]
        or "don't have" in response_lower
        or "contact hr" in response_lower
    )


def test_analytics_recording(config):
    """Test that queries are recorded in analytics."""
    init_db()
    # Generate a query to ensure something is logged
    generate_response("How much PTO do I get?", config=config)
    data = get_dashboard_data(days=1)
    assert data["total_questions"] > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
