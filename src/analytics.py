import sqlite3
import re
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "analytics.db"

_CATEGORY_KEYWORDS = {
    "benefits": [
        "insurance", "401k", "401(k)", "dental", "vision", "hsa", "health",
        "medical", "life insurance", "disability", "eap", "counseling",
        "gym", "wellness", "commuter", "cell phone stipend", "benefits",
        "open enrollment", "cobra", "fidelity",
    ],
    "pto": [
        "pto", "vacation", "sick", "leave", "holiday", "time off",
        "bereavement", "jury duty", "voting", "parental", "maternity",
        "paternity", "floating holiday",
    ],
    "remote": [
        "remote", "wfh", "work from home", "hybrid", "home office",
        "coworking", "international remote", "work model",
    ],
    "expenses": [
        "expense", "reimbursement", "travel", "expensify", "per diem",
        "meal allowance", "hotel", "flight", "mileage", "navan",
    ],
    "compensation": [
        "salary", "raise", "bonus", "pay", "compensation", "overtime",
        "pay schedule", "direct deposit",
    ],
    "policy": [
        "conduct", "dress code", "harassment", "security", "password",
        "drug", "alcohol", "social media", "confidential", "nda",
        "attendance", "dress", "weapons", "safety",
    ],
}


def auto_categorize(query: str) -> str:
    """Categorize a query by keyword matching. Returns category string."""
    query_lower = query.lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            if keyword in query_lower:
                return category
    return "other"


def _get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = _get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_text TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'other',
            was_answered INTEGER NOT NULL DEFAULT 1,
            was_sensitive INTEGER NOT NULL DEFAULT 0,
            response_time_ms INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS unanswered (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_text TEXT NOT NULL UNIQUE,
            timestamp TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.commit()
    conn.close()


def log_query(query: str, was_answered: bool, was_sensitive: bool, response_time_ms: int = 0):
    """Log a query to the analytics database. Anonymous — no user identifier."""
    init_db()
    category = auto_categorize(query)
    conn = _get_connection()
    conn.execute(
        "INSERT INTO queries (query_text, timestamp, category, was_answered, was_sensitive, response_time_ms) VALUES (?, ?, ?, ?, ?, ?)",
        (query, datetime.now().isoformat(), category, int(was_answered), int(was_sensitive), response_time_ms),
    )

    if not was_answered:
        existing = conn.execute(
            "SELECT id, count FROM unanswered WHERE query_text = ?", (query,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE unanswered SET count = count + 1, timestamp = ? WHERE id = ?",
                (datetime.now().isoformat(), existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO unanswered (query_text, timestamp, count) VALUES (?, ?, 1)",
                (query, datetime.now().isoformat()),
            )

    conn.commit()
    conn.close()


def get_dashboard_data(days: int = 30) -> dict:
    """Get analytics data for the HR dashboard."""
    init_db()
    conn = _get_connection()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    # Total questions
    total = conn.execute(
        "SELECT COUNT(*) as cnt FROM queries WHERE timestamp >= ?", (cutoff,)
    ).fetchone()["cnt"]

    # Questions per day
    rows = conn.execute(
        "SELECT DATE(timestamp) as day, COUNT(*) as cnt FROM queries WHERE timestamp >= ? GROUP BY DATE(timestamp) ORDER BY day",
        (cutoff,),
    ).fetchall()
    questions_per_day = [{"date": r["day"], "count": r["cnt"]} for r in rows]

    # Top 10 most asked
    rows = conn.execute(
        "SELECT query_text, COUNT(*) as cnt FROM queries WHERE timestamp >= ? GROUP BY query_text ORDER BY cnt DESC LIMIT 10",
        (cutoff,),
    ).fetchall()
    top_questions = [{"query": r["query_text"], "count": r["cnt"]} for r in rows]

    # Top unanswered
    rows = conn.execute(
        "SELECT query_text, count FROM unanswered ORDER BY count DESC LIMIT 20"
    ).fetchall()
    unanswered_questions = [{"query": r["query_text"], "count": r["count"]} for r in rows]

    # Sensitive topic count
    sensitive_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM queries WHERE timestamp >= ? AND was_sensitive = 1",
        (cutoff,),
    ).fetchone()["cnt"]

    # Sensitive per day (for trend)
    rows = conn.execute(
        "SELECT DATE(timestamp) as day, COUNT(*) as cnt FROM queries WHERE timestamp >= ? AND was_sensitive = 1 GROUP BY DATE(timestamp) ORDER BY day",
        (cutoff,),
    ).fetchall()
    sensitive_per_day = [{"date": r["day"], "count": r["cnt"]} for r in rows]

    # Answer rate
    answered = conn.execute(
        "SELECT COUNT(*) as cnt FROM queries WHERE timestamp >= ? AND was_answered = 1",
        (cutoff,),
    ).fetchone()["cnt"]
    answer_rate = (answered / total * 100) if total > 0 else 0

    # Category breakdown
    rows = conn.execute(
        "SELECT category, COUNT(*) as cnt FROM queries WHERE timestamp >= ? GROUP BY category ORDER BY cnt DESC",
        (cutoff,),
    ).fetchall()
    category_breakdown = {r["category"]: r["cnt"] for r in rows}

    conn.close()

    return {
        "total_questions": total,
        "questions_per_day": questions_per_day,
        "top_questions": top_questions,
        "unanswered_questions": unanswered_questions,
        "sensitive_count": sensitive_count,
        "sensitive_per_day": sensitive_per_day,
        "answer_rate": round(answer_rate, 1),
        "category_breakdown": category_breakdown,
    }


def get_raw_queries(days: int = 30) -> list[dict]:
    """Return all raw query rows for the given time window (for dashboard filtering)."""
    init_db()
    conn = _get_connection()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT query_text, timestamp, category, was_answered, was_sensitive, response_time_ms "
        "FROM queries WHERE timestamp >= ? ORDER BY timestamp DESC",
        (cutoff,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_categories() -> list[str]:
    """Return all known category names."""
    return list(_CATEGORY_KEYWORDS.keys()) + ["other"]
