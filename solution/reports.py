"""
Report generation: produces the 5 output files matching the exact contract.
All aggregation done in Python from DB queries; SQL used for data retrieval.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from solution.db import db_conn, adapt_sql
from solution.normalise import to_ist_date, to_ist_iso
from solution.schemas import load_dispositions

IST = timezone(timedelta(hours=5, minutes=30))


def _parse_utc(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="\n", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


# ─── Core data fetchers ───────────────────────────────────────────────────────

def _fetch_all_leads(conn) -> list[dict]:
    cur = conn.cursor()
    cur.execute(
        "SELECT lead_id, workflow_id, phone_hash, phone_masked, batch_id, "
        "external_ref, preferred_language, merged_count "
        "FROM leads ORDER BY workflow_id, external_ref"
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _fetch_ledger_for_leads(conn, lead_ids: list[int]) -> list[dict]:
    """Fetch all ledger events for a set of lead_ids."""
    if not lead_ids:
        return []
    cur = conn.cursor()
    is_pg = "psycopg2" in type(conn).__module__
    ph = "%s" if is_pg else "?"
    placeholders = ", ".join([ph] * len(lead_ids))
    cur.execute(
        f"SELECT event_id, event_type, call_id, occurred_at, lead_id, payload_json "
        f"FROM ledger WHERE lead_id IN ({placeholders}) AND is_orphan = 0 "
        f"ORDER BY occurred_at",
        lead_ids,
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _fetch_rejections(conn) -> list[dict]:
    cur = conn.cursor()
    cur.execute(
        "SELECT workflow_id, batch_id, row_number, reason "
        "FROM rejections ORDER BY workflow_id, batch_id, row_number"
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _fetch_quality_counters(conn) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT * FROM quality_counters LIMIT 1")
    row = cur.fetchone()
    if row is None:
        return {}
    cols = [d[0] for d in cur.description]
    d = dict(zip(cols, row))
    d.pop("id", None)
    return d


def _fetch_rejection_reasons(conn) -> dict[str, int]:
    cur = conn.cursor()
    cur.execute("SELECT reason, COUNT(*) as cnt FROM rejections GROUP BY reason")
    return {row[0]: row[1] for row in cur.fetchall()}


# ─── Disposition logic ────────────────────────────────────────────────────────

def _effective_disposition(call_events: list[dict]) -> Optional[str]:
    """
    Effective disposition = latest disposition.override by occurred_at;
    if none, latest disposition.set.
    """
    overrides = [
        e for e in call_events if e["event_type"] == "disposition.override"
    ]
    if overrides:
        latest = max(overrides, key=lambda e: e["occurred_at"])
        return json.loads(latest["payload_json"]).get("disposition_code")

    sets = [e for e in call_events if e["event_type"] == "disposition.set"]
    if sets:
        latest = max(sets, key=lambda e: e["occurred_at"])
        return json.loads(latest["payload_json"]).get("disposition_code")

    return None


# ─── Lead bucket logic ────────────────────────────────────────────────────────

def _lead_bucket(calls: dict[str, list[dict]], dispositions: dict) -> str:
    """
    Bucket priority (spec):
    1. yet_to_reach_out: no calls
    2. completed: any call has terminal effective disposition
    3. in_progress_telephony_failed: every call has telephony_failed
    4. in_progress_dialed: everything else
    """
    if not calls:
        return "yet_to_reach_out"

    workflow_id = None  # determined from events

    for call_id, events in calls.items():
        # Determine workflow from events
        if workflow_id is None and events:
            # workflow comes from lead context (passed separately)
            pass
        eff = _effective_disposition(events)
        if eff:
            # We need workflow to check dispositions; set from lead context
            pass

    return None  # Will be computed in report with workflow context


def _compute_lead_bucket(
    workflow_id: str,
    calls: dict[str, list[dict]],
    dispositions: dict,
) -> str:
    if not calls:
        return "yet_to_reach_out"

    wf_disps = dispositions.get(workflow_id, {})

    # Check completed: any call with terminal effective disposition
    for call_id, events in calls.items():
        eff = _effective_disposition(events)
        if eff and wf_disps.get(eff, {}).get("is_terminal", False):
            return "completed"

    # Check in_progress_telephony_failed: ALL calls have telephony_failed
    all_failed = all(
        any(e["event_type"] == "call.telephony_failed" for e in events)
        for events in calls.values()
    )
    if all_failed:
        return "in_progress_telephony_failed"

    return "in_progress_dialed"


# ─── Main report builder ──────────────────────────────────────────────────────

def build_reports(out_dir: Path) -> None:
    """Generate all 5 report files into out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dispositions = load_dispositions()

    with db_conn() as conn:
        leads = _fetch_all_leads(conn)
        all_lead_ids = [l["lead_id"] for l in leads]
        ledger_rows = _fetch_ledger_for_leads(conn, all_lead_ids)
        rejections = _fetch_rejections(conn)
        qc = _fetch_quality_counters(conn)
        rejection_reasons = _fetch_rejection_reasons(conn)

    # ── Group ledger events by lead_id then call_id ────────────────────────
    # lead_calls[lead_id][call_id] = list of events
    lead_calls: dict[int, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))

    for evt in ledger_rows:
        lid = evt["lead_id"]
        cid = evt["call_id"]
        lead_calls[lid][cid].append(evt)

    # Only count calls that have a call.initiated event
    def valid_calls(lead_id: int) -> dict[str, list[dict]]:
        result = {}
        for cid, events in lead_calls[lead_id].items():
            if any(e["event_type"] == "call.initiated" for e in events):
                result[cid] = events
        return result

    # ── 1. batch_summary.csv ──────────────────────────────────────────────
    # {(workflow_id, batch_id): {bucket: count}}
    batch_buckets: dict[tuple, dict[str, int]] = defaultdict(
        lambda: {"yet_to_reach_out": 0, "in_progress_telephony_failed": 0,
                 "in_progress_dialed": 0, "completed": 0, "total_leads": 0}
    )
    for lead in leads:
        wf = lead["workflow_id"]
        bid = lead["batch_id"]
        lid = lead["lead_id"]
        vcalls = valid_calls(lid)
        bucket = _compute_lead_bucket(wf, vcalls, dispositions)
        key = (wf, bid)
        batch_buckets[key]["total_leads"] += 1
        batch_buckets[key][bucket] += 1

    batch_summary_rows = []
    for (wf, bid), counts in sorted(batch_buckets.items()):
        batch_summary_rows.append({
            "workflow_id": wf,
            "batch_id": bid,
            "total_leads": counts["total_leads"],
            "yet_to_reach_out": counts["yet_to_reach_out"],
            "in_progress_telephony_failed": counts["in_progress_telephony_failed"],
            "in_progress_dialed": counts["in_progress_dialed"],
            "completed": counts["completed"],
        })
    _write_csv(
        out_dir / "batch_summary.csv",
        ["workflow_id", "batch_id", "total_leads", "yet_to_reach_out",
         "in_progress_telephony_failed", "in_progress_dialed", "completed"],
        batch_summary_rows,
    )

    # ── 2. daily_overview.csv ──────────────────────────────────────────────
    # Build lead_id → workflow_id map
    lead_wf = {l["lead_id"]: l["workflow_id"] for l in leads}

    # {(workflow_id, ist_date): {total_calls, connected_calls, success_leads_set}}
    daily: dict[tuple, dict] = defaultdict(
        lambda: {"total_calls": 0, "connected_calls": 0, "success_leads": set()}
    )

    # We need to group by (workflow_id, IST date of call.initiated)
    # Iterate per lead per call
    for lead in leads:
        lid = lead["lead_id"]
        wf = lead["workflow_id"]
        wf_disps = dispositions.get(wf, {})
        vcalls = valid_calls(lid)

        for cid, events in vcalls.items():
            initiated = next(
                (e for e in events if e["event_type"] == "call.initiated"), None
            )
            if not initiated:
                continue

            dt = _parse_utc(initiated["occurred_at"])
            if not dt:
                continue
            ist_date = to_ist_date(dt)
            key = (wf, ist_date)

            daily[key]["total_calls"] += 1

            connected = any(e["event_type"] == "call.connected" for e in events)
            if connected:
                daily[key]["connected_calls"] += 1

            eff = _effective_disposition(events)
            if eff and wf_disps.get(eff, {}).get("is_success", False):
                daily[key]["success_leads"].add(lid)

    daily_rows = []
    for (wf, date), d in sorted(daily.items()):
        total = d["total_calls"]
        conn_rate = d["connected_calls"] / total if total > 0 else 0.0
        daily_rows.append({
            "workflow_id": wf,
            "date": date,
            "total_calls": total,
            "connected_calls": d["connected_calls"],
            "connectivity_rate": f"{conn_rate:.4f}",
            "success_leads": len(d["success_leads"]),
        })
    _write_csv(
        out_dir / "daily_overview.csv",
        ["workflow_id", "date", "total_calls", "connected_calls",
         "connectivity_rate", "success_leads"],
        daily_rows,
    )

    # ── 3. lead_report.csv ────────────────────────────────────────────────
    lead_report_rows = []
    for lead in leads:
        lid = lead["lead_id"]
        wf = lead["workflow_id"]
        wf_disps = dispositions.get(wf, {})
        vcalls = valid_calls(lid)

        total_calls = len(vcalls)
        connected_calls = 0
        last_call_dt: Optional[datetime] = None
        current_disposition: Optional[str] = None
        ever_success = False

        for cid, events in vcalls.items():
            initiated = next(
                (e for e in events if e["event_type"] == "call.initiated"), None
            )
            if not initiated:
                continue

            dt = _parse_utc(initiated["occurred_at"])
            if dt and (last_call_dt is None or dt > last_call_dt):
                last_call_dt = dt

            if any(e["event_type"] == "call.connected" for e in events):
                connected_calls += 1

            eff = _effective_disposition(events)
            if eff and wf_disps.get(eff, {}).get("is_success", False):
                ever_success = True

        # current_disposition = effective disposition of MOST RECENT call
        if vcalls and last_call_dt:
            most_recent_cid = None
            most_recent_dt = None
            for cid, events in vcalls.items():
                initiated = next(
                    (e for e in events if e["event_type"] == "call.initiated"), None
                )
                if initiated:
                    dt = _parse_utc(initiated["occurred_at"])
                    if dt and (most_recent_dt is None or dt > most_recent_dt):
                        most_recent_dt = dt
                        most_recent_cid = cid
            if most_recent_cid:
                current_disposition = _effective_disposition(vcalls[most_recent_cid])

        lead_report_rows.append({
            "workflow_id": wf,
            "external_ref": lead["external_ref"] or "",
            "batch_id": lead["batch_id"],
            "phone_masked": lead["phone_masked"],
            "bucket": _compute_lead_bucket(wf, vcalls, dispositions),
            "total_calls": total_calls,
            "connected_calls": connected_calls,
            "last_call_at": to_ist_iso(last_call_dt) if last_call_dt else "",
            "current_disposition": current_disposition or "",
            "ever_success": "true" if ever_success else "false",
        })

    # Sort by workflow_id, external_ref
    lead_report_rows.sort(key=lambda r: (r["workflow_id"], r["external_ref"]))
    _write_csv(
        out_dir / "lead_report.csv",
        ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket",
         "total_calls", "connected_calls", "last_call_at",
         "current_disposition", "ever_success"],
        lead_report_rows,
    )

    # ── 4. rejections.csv ────────────────────────────────────────────────
    _write_csv(
        out_dir / "rejections.csv",
        ["workflow_id", "batch_id", "row_number", "reason"],
        [{"workflow_id": r["workflow_id"], "batch_id": r["batch_id"],
          "row_number": r["row_number"], "reason": r["reason"]}
         for r in rejections],
    )

    # ── 5. data_quality.json ─────────────────────────────────────────────
    # Compute current orphan count from DB (reflects final state)
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM ledger WHERE is_orphan = 1")
        orphan_count = cur.fetchone()[0]

    quality = {
        "lead_rows_total": qc.get("lead_rows_total", 0),
        "lead_rows_rejected": qc.get("lead_rows_rejected", 0),
        "rejections_by_reason": rejection_reasons,
        "duplicate_lead_rows_merged": qc.get("duplicate_lead_rows_merged", 0),
        "field_warnings": qc.get("field_warnings", 0),
        "events_total": qc.get("events_total", 0),
        "malformed_events": qc.get("malformed_events", 0),
        "duplicate_events_dropped": qc.get("duplicate_events_dropped", 0),
        "orphan_events": orphan_count,
        "unknown_disposition_events": qc.get("unknown_disposition_events", 0),
    }

    with open(out_dir / "data_quality.json", "w", encoding="utf-8") as f:
        json.dump(quality, f, indent=2)

    print(f"Reports written to {out_dir}/")
    print(f"  batch_summary.csv:  {len(batch_summary_rows)} rows")
    print(f"  daily_overview.csv: {len(daily_rows)} rows")
    print(f"  lead_report.csv:    {len(lead_report_rows)} rows")
    print(f"  rejections.csv:     {len(rejections)} rows")
    print(f"  data_quality.json:  written")
