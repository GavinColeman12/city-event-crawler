"""Sync Postgres helpers for the Streamlit dashboard.

Neon's serverless compute auto-suspends after idle and SSL connections
get dropped silently. We cache a single connection per Streamlit session
via ``@st.cache_resource``, but every query is wrapped to retry once on
``OperationalError`` after rebuilding the connection.
"""

from __future__ import annotations

import os
from typing import Any

import pandas as pd
import psycopg
import streamlit as st


def _resolve_database_url() -> str:
    """Try env var first, then Streamlit secrets, fall back to empty."""
    url = os.environ.get("DATABASE_URL", "")
    if url:
        return url
    try:
        return st.secrets["DATABASE_URL"]
    except (FileNotFoundError, KeyError, AttributeError):
        return ""


@st.cache_resource
def _conn() -> psycopg.Connection:
    url = _resolve_database_url()
    if not url:
        st.error("DATABASE_URL is not configured. Set it in .env or Streamlit secrets.")
        st.stop()
    return psycopg.connect(url, autocommit=True)


def _is_disconnect(exc: Exception) -> bool:
    """True for the kind of error we recover from by reconnecting."""
    if isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError)):
        return True
    return False


def _with_retry(fn):
    """Run *fn* against a connection; on disconnect, drop cache & retry once."""
    try:
        return fn(_conn())
    except Exception as exc:
        if not _is_disconnect(exc):
            raise
    # Drop the bad cached connection, then retry on a fresh one.
    try:
        _conn.clear()
    except AttributeError:
        st.cache_resource.clear()
    return fn(_conn())


def query_df(sql: str, params: tuple = ()) -> pd.DataFrame:
    def run(conn: psycopg.Connection) -> pd.DataFrame:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d.name for d in (cur.description or [])]
            rows = cur.fetchall()
        return pd.DataFrame(rows, columns=cols)

    return _with_retry(run)


def execute(sql: str, params: tuple = ()) -> None:
    def run(conn: psycopg.Connection) -> None:
        with conn.cursor() as cur:
            cur.execute(sql, params)

    _with_retry(run)


def fetchval(sql: str, params: tuple = ()) -> Any:
    def run(conn: psycopg.Connection) -> Any:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return row[0] if row else None

    return _with_retry(run)
