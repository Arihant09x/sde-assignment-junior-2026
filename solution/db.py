"""
Database layer: schema creation and connection management.
Supports PostgreSQL (via psycopg2) and SQLite.
"""
from __future__ import annotations

import os
import contextlib
from typing import Generator

from solution.config import DATABASE_URL


def _is_postgres() -> bool:
    return DATABASE_URL.startswith("postgresql") or DATABASE_URL.startswith("postgres")


def get_connection():
    """Return a live DB connection. Caller is responsible for close()."""
    if _is_postgres():
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    else:
        import sqlite3
        # Strip sqlite:/// prefix
        path = DATABASE_URL.replace("sqlite:///", "")
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        # Enable WAL for concurrency and foreign keys
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn


@contextlib.contextmanager
def db_conn() -> Generator:
    """Context manager that yields a connection and commits/rolls back."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def placeholder(n: int = 1) -> str:
    """Return SQL placeholder: %s for postgres, ? for sqlite."""
    ph = "%s" if _is_postgres() else "?"
    if n == 1:
        return ph
    return ", ".join([ph] * n)


def ph() -> str:
    """Single placeholder."""
    return "%s" if _is_postgres() else "?"


def adapt_sql(sql: str) -> str:
    """Convert %s placeholders to ? for SQLite."""
    if not _is_postgres():
        return sql.replace("%s", "?")
    return sql


DDL = """
-- ─── Workflow schemas cache ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS workflows (
    workflow_id     TEXT PRIMARY KEY,
    schema_json     TEXT NOT NULL        -- raw JSON blob
);

-- ─── Ingested drops (idempotency) ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS drops (
    drop_path       TEXT PRIMARY KEY,    -- absolute or relative path of the drop
    ingested_at     TEXT NOT NULL
);

-- ─── Ingested batch files (idempotency) ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS batches (
    batch_key       TEXT PRIMARY KEY,    -- "<workflow_id>/<batch_id>" e.g. motor_renewal/batch_001
    workflow_id     TEXT NOT NULL,
    batch_id        TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    ingested_at     TEXT NOT NULL
);

-- ─── Leads ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS leads (
    lead_id         INTEGER PRIMARY KEY {autoincrement},
    workflow_id     TEXT NOT NULL,
    phone_hash      TEXT NOT NULL,       -- SHA-256(salt + "+91XXXXXXXXXX"), used for linking
    phone_masked    TEXT NOT NULL,       -- "+91******XXXX" – safe for reports
    batch_id        TEXT NOT NULL,       -- first batch that created this lead
    external_ref    TEXT,               -- policy_number / order_id (reference field)
    -- non-PII fields stored as-is
    policy_expiry   TEXT,
    premium_amount  TEXT,
    order_date      TEXT,
    product         TEXT,
    preferred_language TEXT,
    -- PII stored encrypted/hashed
    customer_name_enc TEXT,             -- AES-256-encrypted name (or null if no key)
    -- quality
    merged_count    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (workflow_id, phone_hash)
);

-- ─── Rejections ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rejections (
    id              INTEGER PRIMARY KEY {autoincrement},
    workflow_id     TEXT NOT NULL,
    batch_id        TEXT NOT NULL,
    row_number      INTEGER NOT NULL,
    reason          TEXT NOT NULL
);

-- ─── Field warnings (quality counter) ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS field_warnings (
    id              INTEGER PRIMARY KEY {autoincrement},
    workflow_id     TEXT NOT NULL,
    batch_id        TEXT NOT NULL,
    row_number      INTEGER NOT NULL,
    field_name      TEXT NOT NULL
);

-- ─── Interaction ledger (append-only) ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS ledger (
    id              INTEGER PRIMARY KEY {autoincrement},
    event_id        TEXT NOT NULL UNIQUE,   -- deduplication key
    event_type      TEXT NOT NULL,
    workflow_id     TEXT NOT NULL,
    call_id         TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,          -- stored as ISO-8601 UTC
    lead_id         INTEGER,                -- NULL = orphan
    phone_hash      TEXT,                   -- NULL for disposition.override
    payload_json    TEXT NOT NULL,
    is_orphan       INTEGER NOT NULL DEFAULT 0,
    ingested_at     TEXT NOT NULL
);

-- ─── Orphan link table (re-link when lead arrives) ──────────────────────────
CREATE TABLE IF NOT EXISTS orphan_events (
    event_id        TEXT PRIMARY KEY,
    workflow_id     TEXT NOT NULL,
    call_id         TEXT NOT NULL,
    phone_hash      TEXT                    -- NULL for override orphans
);

-- ─── Quality counters (single-row summary updated per ingest) ────────────────
CREATE TABLE IF NOT EXISTS quality_counters (
    id              INTEGER PRIMARY KEY {autoincrement},
    lead_rows_total         INTEGER NOT NULL DEFAULT 0,
    lead_rows_rejected      INTEGER NOT NULL DEFAULT 0,
    duplicate_lead_rows_merged INTEGER NOT NULL DEFAULT 0,
    field_warnings          INTEGER NOT NULL DEFAULT 0,
    events_total            INTEGER NOT NULL DEFAULT 0,
    malformed_events        INTEGER NOT NULL DEFAULT 0,
    duplicate_events_dropped INTEGER NOT NULL DEFAULT 0,
    orphan_events           INTEGER NOT NULL DEFAULT 0,
    unknown_disposition_events INTEGER NOT NULL DEFAULT 0
);

-- ─── Indices for performance ─────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_leads_wf_hash ON leads (workflow_id, phone_hash);
CREATE INDEX IF NOT EXISTS idx_ledger_call ON ledger (call_id);
CREATE INDEX IF NOT EXISTS idx_ledger_lead ON ledger (lead_id);
CREATE INDEX IF NOT EXISTS idx_ledger_wf_hash ON ledger (workflow_id, phone_hash);
"""


def init_db() -> None:
    """Create all tables if they don't exist. Safe to call multiple times."""
    is_pg = _is_postgres()
    autoincrement = "" if is_pg else "AUTOINCREMENT"
    if is_pg:
        # postgres uses SERIAL
        ddl = DDL.replace(
            "INTEGER PRIMARY KEY {autoincrement}",
            "SERIAL PRIMARY KEY"
        )
    else:
        ddl = DDL.replace("{autoincrement}", autoincrement)

    with db_conn() as conn:
        cur = conn.cursor()
        # Split on double-newline to handle multi-statement DDL
        for stmt in ddl.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
        # Ensure exactly one quality_counters row
        cur.execute("SELECT COUNT(*) FROM quality_counters")
        row = cur.fetchone()
        count = row[0] if row else 0
        if count == 0:
            cur.execute("INSERT INTO quality_counters DEFAULT VALUES")

