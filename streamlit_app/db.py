"""Sync Postgres helpers for the Streamlit dashboard.

Uses ``psycopg_pool.ConnectionPool`` with ``check=ConnectionPool.check_connection``
so dead connections (Neon's serverless compute auto-suspends and drops SSL
silently) are detected and replaced before being handed to a query. The pool
itself is cached for the Streamlit session.
"""

from __future__ import annotations

import os
from typing import Any

import pandas as pd
import psycopg
import streamlit as st
from psycopg_pool import ConnectionPool


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
def _pool() -> ConnectionPool:
    url = _resolve_database_url()
    if not url:
        st.error("DATABASE_URL is not configured. Set it in .env or Streamlit secrets.")
        st.stop()
    pool = ConnectionPool(
        conninfo=url,
        min_size=0,                # don't hold idle connections open against Neon
        max_size=4,
        max_idle=30.0,             # close idle connections after 30s
        max_lifetime=240.0,        # recycle connections every ~4 min (under Neon's idle suspend)
        kwargs={"autocommit": True},
        # Health check: pool runs `SELECT 1` and replaces the connection
        # if it's dead. Cheap insurance against Neon idle drops.
        check=ConnectionPool.check_connection,
        open=True,
    )
    return pool


def query_df(sql: str, params: tuple = ()) -> pd.DataFrame:
    with _pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in (cur.description or [])]
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=cols)


def execute(sql: str, params: tuple = ()) -> None:
    with _pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)


def fetchval(sql: str, params: tuple = ()) -> Any:
    with _pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return row[0] if row else None
