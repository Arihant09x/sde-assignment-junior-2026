"""
Main CLI entry point.

Usage:
  python -m solution.cli ingest <drop_path>
  python -m solution.cli report --out <dir>
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from solution.db import init_db, db_conn, adapt_sql
from solution.ingest_leads import ingest_leads_drop
from solution.ingest_events import ingest_events_drop, relink_all_orphans, count_current_orphans
from solution.reports import build_reports


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_ingest(drop_path_str: str) -> None:
    drop_path = Path(drop_path_str).resolve()
    if not drop_path.exists():
        print(f"ERROR: drop path does not exist: {drop_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Ingesting drop: {drop_path}")
    init_db()

    with db_conn() as conn:
        # Idempotency: check if this drop was already ingested
        cur = conn.cursor()
        cur.execute(adapt_sql(
            "SELECT 1 FROM drops WHERE drop_path = %s"
        ), (str(drop_path),))
        if cur.fetchone():
            print(f"Drop already ingested (idempotent skip): {drop_path}")
            return

        # ── 1. Ingest leads ────────────────────────────────────────────────
        print("  Ingesting leads...")
        lead_stats = ingest_leads_drop(drop_path, conn)
        print(f"    Rows processed: {lead_stats['lead_rows_total']}")
        print(f"    Rejected:        {lead_stats['lead_rows_rejected']}")
        print(f"    Merged dupes:    {lead_stats['duplicate_lead_rows_merged']}")
        print(f"    Field warnings:  {lead_stats['field_warnings']}")

        # ── 2. Ingest events ───────────────────────────────────────────────
        print("  Ingesting events...")
        event_stats = ingest_events_drop(drop_path, conn)
        print(f"    Events total:    {event_stats['events_total']}")
        print(f"    Malformed:       {event_stats['malformed_events']}")
        print(f"    Duplicates:      {event_stats['duplicate_events_dropped']}")
        print(f"    New orphans:     {event_stats['orphan_events_new']}")
        print(f"    Unknown dispos:  {event_stats['unknown_disposition_events']}")

        # ── 3. Re-link orphans (events that arrived before their lead) ─────
        resolved = relink_all_orphans(conn)
        if resolved:
            print(f"  Resolved {resolved} previously orphaned events")

        # ── 4. Update quality_counters ─────────────────────────────────────
        cur.execute(adapt_sql("""
            UPDATE quality_counters SET
              lead_rows_total             = lead_rows_total + %s,
              lead_rows_rejected          = lead_rows_rejected + %s,
              duplicate_lead_rows_merged  = duplicate_lead_rows_merged + %s,
              field_warnings              = field_warnings + %s,
              events_total                = events_total + %s,
              malformed_events            = malformed_events + %s,
              duplicate_events_dropped    = duplicate_events_dropped + %s,
              unknown_disposition_events  = unknown_disposition_events + %s
        """), (
            lead_stats["lead_rows_total"],
            lead_stats["lead_rows_rejected"],
            lead_stats["duplicate_lead_rows_merged"],
            lead_stats["field_warnings"],
            event_stats["events_total"],
            event_stats["malformed_events"],
            event_stats["duplicate_events_dropped"],
            event_stats["unknown_disposition_events"],
        ))

        # ── 5. Mark drop as done ───────────────────────────────────────────
        cur.execute(adapt_sql(
            "INSERT INTO drops (drop_path, ingested_at) VALUES (%s, %s)"
        ), (str(drop_path), _now_utc()))

    print(f"Done. Drop ingested successfully.")


def cmd_report(out_dir_str: str) -> None:
    out_dir = Path(out_dir_str)
    print(f"Generating reports into: {out_dir}")
    init_db()
    build_reports(out_dir)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Interaction Ledger & Reporting Pipeline"
    )
    sub = parser.add_subparsers(dest="command")

    # ingest sub-command
    ingest_p = sub.add_parser("ingest", help="Ingest a data drop")
    ingest_p.add_argument("drop_path", help="Path to the drop directory")

    # report sub-command
    report_p = sub.add_parser("report", help="Generate report files")
    report_p.add_argument("--out", required=True, help="Output directory for reports")

    args = parser.parse_args()

    if args.command == "ingest":
        cmd_ingest(args.drop_path)
    elif args.command == "report":
        cmd_report(args.out)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

