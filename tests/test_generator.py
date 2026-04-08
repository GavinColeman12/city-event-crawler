"""Tests for the generator module."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.analytics import auto_categorize


def test_auto_categorize():
    assert auto_categorize("What health insurance plans are available?") == "benefits"
    assert auto_categorize("What's the 401k match?") == "benefits"
    assert auto_categorize("How much PTO do I get?") == "pto"
    assert auto_categorize("Can I carry over vacation days?") == "pto"
    assert auto_categorize("What's the parental leave policy?") == "pto"
    assert auto_categorize("Can I work from home?") == "remote"
    assert auto_categorize("What's the hybrid schedule?") == "remote"
    assert auto_categorize("How do I submit an expense report?") == "expenses"
    assert auto_categorize("What's the travel reimbursement policy?") == "expenses"
    assert auto_categorize("What's the dress code?") == "policy"
    assert auto_categorize("How do I report harassment?") == "policy"
    assert auto_categorize("When are annual raises?") == "compensation"
    assert auto_categorize("What's the bonus structure?") == "compensation"
    assert auto_categorize("What's the stock price?") == "other"
    assert auto_categorize("Where is the office kitchen?") == "other"


if __name__ == "__main__":
    test_auto_categorize()
    print("All generator tests passed!")
