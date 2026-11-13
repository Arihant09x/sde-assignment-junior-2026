"""
Event ingestion: reads JSONL event files into the append-only ledger.
Handles deduplication, linking, orphans, overrides, unknown dispositions.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from solution.db import db_conn, adapt_sql, ph
from solution.normalise import normalize_phone, hash_phone, parse_timestamp, to_utc_iso
from solution.schemas import load_dispositions


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lookup_lead(conn, workflow_id: str, phone_hash: str) -> Optional[int]:
    """Return lead_id for (workflow_id, phone_hash), or None if not found."""
    cur = conn.cursor()
    cur.execute(adapt_sql(
        "SELECT lead_id FROM leads WHERE workflow_id = %s AND phone_hash = %s"
    ), (workflow_id, phone_hash))
    row = cur.fetchone()
    if row is None:
        return None
    return row[0] if isinstance(row, (list, tuple)) else row["lead_id"]


def _lookup_lead_by_call(conn, call_id: str) -> Optional[int]:
    """
    Find lead_id for a disposition.override via its call_id.
    Looks at non-orphan ledger events that share the call_id.
    """
    cur = conn.cursor()
    cur.execute(adapt_sql(
        "SELECT lead_id FROM ledger "
        "WHERE call_id = %s AND lead_id IS NOT NULL AND is_orphan = 0 "
        "LIMIT 1"
    ), (call_id,))
    row = cur.fetchone()
    if row is None:
        return None
    return row[0] if isinstance(row, (list, tuple)) else row["lead_id"]


def _resolve_orphans(conn, workflow_id: str, phone_hash: str, lead_id: int) -> int:
    """
    Re-link any orphan events for (workflow_id, phone_hash) now that lead arrived.
    Returns count of resolved orphans.
    """
    cur = conn.cursor()
    cur.execute(adapt_sql(
        "SELECT event_id FROM orphan_events "
        "WHERE workflow_id = %s AND phone_hash = %s"
    ), (workflow_id, phone_hash))
    orphan_event_ids = [
        (row[0] if isinstance(row, (list, tuple)) else row["event_id"])
        for row in cur.fetchall()
    ]
    if not orphan_event_ids:
        return 0

    # Also resolve orphan overrides whose call now has a linked event
    # First update ledger rows
    for eid in orphan_event_ids:
        cur.execute(adapt_sql(
            "UPDATE ledger SET lead_id = %s, is_orphan = 0 WHERE event_id = %s"
        ), (lead_id, eid))

    # Remove from orphan table
    for eid in orphan_event_ids:
        cur.execute(adapt_sql(
            "DELETE FROM orphan_events WHERE event_id = %s"
        ), (eid,))

    return len(orphan_event_ids)


def _resolve_override_orphans(conn) -> int:
    """
    Re-link disposition.override orphans that had no call_id match yet.
    Called after each batch of events is ingested.
    """
    cur = conn.cursor()
    # Overrides stored with phone_hash=NULL in orphan_events
    cur.execute(adapt_sql(
        "SELECT oe.event_id, oe.call_id FROM orphan_events oe "
        "WHERE oe.phone_hash IS NULL"
    ))
    orphan_overrides = cur.fetchall()
    resolved = 0
    for row in orphan_overrides:
        eid = row[0] if isinstance(row, (list, tuple)) else row["event_id"]
        cid = row[1] if isinstance(row, (list, tuple)) else row["call_id"]
        lead_id = _lookup_lead_by_call(conn, cid)
        if lead_id is not None:
            cur.execute(adapt_sql(
                "UPDATE ledger SET lead_id = %s, is_orphan = 0 WHERE event_id = %s"
            ), (lead_id, eid))
            cur.execute(adapt_sql(
                "DELETE FROM orphan_events WHERE event_id = %s"
            ), (eid,))
            resolved += 1
    return resolved


def ingest_event_file(file_path: Path, conn) -> dict:
    """
    Process one JSONL event file. Returns quality stats for this file.
    """
    dispositions = load_dispositions()
    stats = {
        "events_total": 0,
        "malformed_events": 0,
        "duplicate_events_dropped": 0,
        "orphan_events_new": 0,       # newly created orphans this run
        "unknown_disposition_events": 0,
    }
    now = _now_utc()

    with open(file_path, encoding="utf-8") as f:
        for raw_line in f:
            raw_line = raw_line.strip()
            if not raw_line:
                continue

            stats["events_total"] += 1

            # ── Step 1: Malformed check ────────────────────────────────────
            try:
                evt = json.loads(raw_line)
            except json.JSONDecodeError:
                stats["malformed_events"] += 1
                continue

            event_id = evt.get("event_id")
            if not event_id:
                stats["malformed_events"] += 1
                continue

            event_type = evt.get("event_type", "")
            workflow_id = evt.get("workflow_id", "")
            call_id = evt.get("call_id", "")
            occurred_at_raw = evt.get("occurred_at", "")
            payload = evt.get("payload", {})

            # ── Step 2: Duplicate check ────────────────────────────────────
            cur = conn.cursor()
            cur.execute(adapt_sql(
                "SELECT 1 FROM ledger WHERE event_id = %s"
            ), (event_id,))
            if cur.fetchone():
                stats["duplicate_events_dropped"] += 1
                continue

            # Parse timestamp
            dt = parse_timestamp(occurred_at_raw)
            occurred_utc = to_utc_iso(dt) if dt else occurred_at_raw

            # ── Step 3: Link to lead ───────────────────────────────────────
            is_override = event_type == "disposition.override"
            phone_hash: Optional[str] = None
            lead_id: Optional[int] = None
            is_orphan = False

            if is_override:
                # Overrides link only via call_id (no phone field)
                lead_id = _lookup_lead_by_call(conn, call_id)
                if lead_id is None:
                    is_orphan = True
            else:
                raw_phone = evt.get("phone", "")
                canonical = normalize_phone(raw_phone)
                if canonical:
                    phone_hash = hash_phone(canonical)
                    lead_id = _lookup_lead(conn, workflow_id, phone_hash)
                    if lead_id is None:
                        is_orphan = True
                else:
                    # Can't normalise phone → orphan
                    is_orphan = True

            # ── Step 4: Unknown disposition check ─────────────────────────
            if event_type in ("disposition.set", "disposition.override"):
                disp_code = payload.get("disposition_code", "")
                wf_disps = dispositions.get(workflow_id, {})
                if disp_code not in wf_disps:
                    stats["unknown_disposition_events"] += 1
                    # Still insert into ledger (it arrived) but mark it unlinked?
                    # Spec says "count it and ignore it" - we skip persisting it
                    continue

            # ── Insert into ledger ─────────────────────────────────────────
            cur.execute(adapt_sql(
                "INSERT INTO ledger "
                "(event_id, event_type, workflow_id, call_id, occurred_at, "
                " lead_id, phone_hash, payload_json, is_orphan, ingested_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            ), (
                event_id, event_type, workflow_id, call_id, occurred_utc,
                lead_id, phone_hash, json.dumps(payload),
                1 if is_orphan else 0, now,
            ))

            if is_orphan:
                stats["orphan_events_new"] += 1
                # Track in orphan_events for later re-linking
                if _is_postgres_conn(conn):
                    cur.execute(
                        "INSERT INTO orphan_events (event_id, workflow_id, call_id, phone_hash) "
                        "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (event_id, workflow_id, call_id, phone_hash)
                    )
                else:
                    cur.execute(
                        "INSERT OR IGNORE INTO orphan_events (event_id, workflow_id, call_id, phone_hash) "
                        "VALUES (?,?,?,?)",
                        (event_id, workflow_id, call_id, phone_hash)
                    )

    return stats


def _is_postgres_conn(conn) -> bool:
    return "psycopg2" in type(conn).__module__


def ingest_events_drop(drop_path: Path, conn) -> dict:
    """
    Ingest all JSONL files under <drop_path>/events/*.jsonl.
    Returns aggregated quality stats.
    """
    total_stats = {
        "events_total": 0,
        "malformed_events": 0,
        "duplicate_events_dropped": 0,
        "orphan_events_new": 0,
        "unknown_disposition_events": 0,
    }

    events_dir = drop_path / "events"
    if not events_dir.exists():
        return total_stats

    for jl_file in sorted(events_dir.iterdir()):
        if jl_file.suffix.lower() not in (".jsonl", ".ndjson"):
            continue
        file_stats = ingest_event_file(jl_file, conn)
        for k in total_stats:
            total_stats[k] += file_stats.get(k, 0)

    # After all events in this drop, resolve orphan overrides
    _resolve_override_orphans(conn)

    return total_stats


def relink_all_orphans(conn) -> int:
    """
    Attempt to re-link all current orphans against existing leads.
    Called after lead ingestion to rescue events that arrived before their lead.
    Returns total number resolved.
    """
    cur = conn.cursor()
    # Get all phone-based orphans
    cur.execute(
        "SELECT oe.event_id, oe.workflow_id, oe.phone_hash "
        "FROM orphan_events oe WHERE oe.phone_hash IS NOT NULL"
    )
    rows = cur.fetchall()
    resolved = 0
    seen_groups: set[tuple[str, str]] = set()

    for row in rows:
        wf = row[1] if isinstance(row, (list, tuple)) else row["workflow_id"]
        ph_hash = row[2] if isinstance(row, (list, tuple)) else row["phone_hash"]
        key = (wf, ph_hash)
        if key in seen_groups:
            continue
        seen_groups.add(key)
        lead_id = _lookup_lead(conn, wf, ph_hash)
        if lead_id is not None:
            resolved += _resolve_orphans(conn, wf, ph_hash, lead_id)

    # Resolve override orphans
    resolved += _resolve_override_orphans(conn)
    return resolved


def count_current_orphans(conn) -> int:
    """Count events still marked orphan in the ledger."""
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM ledger WHERE is_orphan = 1")
    row = cur.fetchone()
    return row[0] if isinstance(row, (list, tuple)) else row["count"]
