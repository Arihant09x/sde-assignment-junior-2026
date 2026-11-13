"""
Lead ingestion: reads CSV files, validates/normalises rows, merges duplicates.
Populates: leads, rejections, field_warnings, quality_counters.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from solution.db import db_conn, adapt_sql, ph
from solution.normalise import (
    normalize_phone, hash_phone, mask_phone, encrypt_name,
    parse_date, parse_decimal, parse_enum,
)
from solution.schemas import load_workflow_schema, build_header_map, get_reference_field


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ingest_lead_file(
    workflow_id: str,
    batch_id: str,
    file_path: Path,
    conn,
) -> dict:
    """
    Ingest one lead CSV file. Returns quality stats for this file.
    Assumes the batch has already been checked for idempotency.
    """
    schema = load_workflow_schema(workflow_id)
    ref_field = get_reference_field(schema)
    fields_in_order = schema["fields"]
    dispositions_catalogue = None  # not needed here

    stats = {
        "lead_rows_total": 0,
        "lead_rows_rejected": 0,
        "duplicate_lead_rows_merged": 0,
        "field_warnings": 0,
        "rejections": [],       # list of (row_number, reason)
        "field_warning_rows": [],  # list of (row_number, field_name)
    }

    with open(file_path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return stats

        header_map = build_header_map(schema, list(reader.fieldnames))
        row_number = 0  # counts non-blank data rows

        for raw_row in reader:
            # Skip blank rows (every cell empty or whitespace)
            if all(not v or not v.strip() for v in raw_row.values()):
                continue
            row_number += 1
            stats["lead_rows_total"] += 1

            # ── Parse and validate each field in schema order ──────────────
            parsed: dict[str, Optional[object]] = {}
            rejection_reason: Optional[str] = None
            row_field_warnings: list[str] = []

            for field_def in fields_in_order:
                fname = field_def["name"]
                ftype = field_def["type"]
                required = field_def.get("required", False)

                # Find which CSV column maps to this field (if any)
                raw_val: Optional[str] = None
                for col, fdef in header_map.items():
                    if fdef["name"] == fname:
                        raw_val = raw_row.get(col, "")
                        break

                # Trim; empty → None
                raw_val = raw_val.strip() if raw_val else None
                if raw_val == "":
                    raw_val = None

                # Required missing check
                if raw_val is None and required:
                    rejection_reason = f"missing_required:{fname}"
                    break

                # Type validation
                if raw_val is not None:
                    if ftype == "phone":
                        norm = normalize_phone(raw_val)
                        if norm is None:
                            # Phone present but invalid → reject
                            rejection_reason = "invalid_phone"
                            break
                        parsed[fname] = norm
                    elif ftype == "date":
                        val = parse_date(raw_val)
                        if val is None:
                            if required:
                                rejection_reason = f"missing_required:{fname}"
                                break
                            row_field_warnings.append(fname)
                        parsed[fname] = val
                    elif ftype == "decimal":
                        val = parse_decimal(raw_val)
                        if val is None:
                            if required:
                                rejection_reason = f"missing_required:{fname}"
                                break
                            row_field_warnings.append(fname)
                        parsed[fname] = val
                    elif ftype == "enum":
                        val = parse_enum(raw_val, field_def.get("values", []))
                        if val is None:
                            if required:
                                rejection_reason = f"missing_required:{fname}"
                                break
                            row_field_warnings.append(fname)
                        parsed[fname] = val
                    elif ftype == "string":
                        parsed[fname] = raw_val
                else:
                    parsed[fname] = None  # optional field, no value

            # ── Handle rejection ───────────────────────────────────────────
            if rejection_reason:
                stats["lead_rows_rejected"] += 1
                stats["rejections"].append((row_number, rejection_reason))
                # Insert rejection record
                cur = conn.cursor()
                cur.execute(adapt_sql(
                    "INSERT INTO rejections (workflow_id, batch_id, row_number, reason) "
                    "VALUES (%s, %s, %s, %s)"
                ), (workflow_id, batch_id, row_number, rejection_reason))
                continue

            # ── Field warnings (only for non-rejected rows) ────────────────
            for fw in row_field_warnings:
                stats["field_warnings"] += 1
                stats["field_warning_rows"].append((row_number, fw))
                cur = conn.cursor()
                cur.execute(adapt_sql(
                    "INSERT INTO field_warnings (workflow_id, batch_id, row_number, field_name) "
                    "VALUES (%s, %s, %s, %s)"
                ), (workflow_id, batch_id, row_number, fw))

            # ── Canonical phone ────────────────────────────────────────────
            phone_canonical: str = parsed["phone"]
            phone_hash = hash_phone(phone_canonical)
            phone_masked = mask_phone(phone_canonical)

            # ── Apply defaults (after validation, before merge) ────────────
            for field_def in fields_in_order:
                fname = field_def["name"]
                if "default" in field_def and parsed.get(fname) is None:
                    parsed[fname] = field_def["default"]

            # ── Upsert into leads ──────────────────────────────────────────
            cur = conn.cursor()
            cur.execute(adapt_sql(
                "SELECT lead_id, merged_count FROM leads "
                "WHERE workflow_id = %s AND phone_hash = %s"
            ), (workflow_id, phone_hash))
            existing = cur.fetchone()

            now = _now_utc()
            if existing is None:
                # New lead
                cur.execute(adapt_sql(
                    "INSERT INTO leads "
                    "(workflow_id, phone_hash, phone_masked, batch_id, external_ref, "
                    " policy_expiry, premium_amount, order_date, product, preferred_language, "
                    " customer_name_enc, merged_count, created_at, updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                ), (
                    workflow_id, phone_hash, phone_masked, batch_id,
                    parsed.get(ref_field),
                    parsed.get("policy_expiry"),
                    parsed.get("premium_amount"),
                    parsed.get("order_date"),
                    parsed.get("product"),
                    parsed.get("preferred_language"),
                    encrypt_name(parsed.get("customer_name") or ""),
                    0, now, now,
                ))
            else:
                # Merge: last non-null wins
                lead_id = existing[0] if isinstance(existing, (list, tuple)) else existing["lead_id"]
                merged = existing[1] if isinstance(existing, (list, tuple)) else existing["merged_count"]
                stats["duplicate_lead_rows_merged"] += 1

                # Build SET clause only for non-null values
                updates: list[tuple[str, object]] = []
                col_map = {
                    ref_field: "external_ref",
                    "policy_expiry": "policy_expiry",
                    "premium_amount": "premium_amount",
                    "order_date": "order_date",
                    "product": "product",
                    "preferred_language": "preferred_language",
                }
                for field_name, col_name in col_map.items():
                    if field_name and parsed.get(field_name) is not None:
                        updates.append((col_name, parsed[field_name]))
                if parsed.get("customer_name") is not None:
                    updates.append(("customer_name_enc", encrypt_name(parsed["customer_name"])))

                if updates:
                    set_clause = ", ".join(f"{col} = {ph()}" for col, _ in updates)
                    vals = [v for _, v in updates]
                    vals += [merged + 1, now, lead_id]
                    cur.execute(adapt_sql(
                        f"UPDATE leads SET {set_clause}, merged_count = {ph()}, "
                        f"updated_at = {ph()} WHERE lead_id = {ph()}"
                    ), vals)
                else:
                    cur.execute(adapt_sql(
                        "UPDATE leads SET merged_count = %s, updated_at = %s WHERE lead_id = %s"
                    ), (merged + 1, now, lead_id))

    return stats


def ingest_leads_drop(drop_path: Path, conn) -> dict:
    """
    Ingest all lead CSVs under <drop_path>/leads/<workflow_id>/*.csv.
    Returns aggregated quality stats.
    """
    total_stats = {
        "lead_rows_total": 0,
        "lead_rows_rejected": 0,
        "duplicate_lead_rows_merged": 0,
        "field_warnings": 0,
    }

    leads_root = drop_path / "leads"
    if not leads_root.exists():
        return total_stats

    # Workflow dirs, then files in filename order (spec)
    for wf_dir in sorted(leads_root.iterdir()):
        if not wf_dir.is_dir():
            continue
        workflow_id = wf_dir.name

        for csv_file in sorted(wf_dir.iterdir()):
            if csv_file.suffix.lower() != ".csv":
                continue
            batch_id = csv_file.stem  # e.g. 'batch_001'
            batch_key = f"{workflow_id}/{batch_id}"

            # Idempotency: skip already-ingested batches
            cur = conn.cursor()
            cur.execute(adapt_sql(
                "SELECT 1 FROM batches WHERE batch_key = %s"
            ), (batch_key,))
            if cur.fetchone():
                continue  # already done

            # Ingest this file
            file_stats = ingest_lead_file(workflow_id, batch_id, csv_file, conn)

            # Record the batch as done
            now = _now_utc()
            cur.execute(adapt_sql(
                "INSERT INTO batches (batch_key, workflow_id, batch_id, file_path, ingested_at) "
                "VALUES (%s,%s,%s,%s,%s)"
            ), (batch_key, workflow_id, batch_id, str(csv_file), now))

            # Accumulate
            for k in total_stats:
                total_stats[k] += file_stats.get(k, 0)

    return total_stats

