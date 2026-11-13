# Submission

## Candidate
- **Name:** Arihant Chougule(arihantc677@gmail.com)
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** ~7 hours

## How to run

```bash
# 1. Clone and enter the repo
git clone <your-repo-url>
cd sde-assignment-junior-2026

# 2. Create virtual environment and install dependencies
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. (Optional) Use PostgreSQL instead of SQLite
# Create a .env file with:
#   DATABASE_URL=postgresql://user:pass@host/dbname?sslmode=require
#   PII_SALT=your-secret-stable-salt

# 4. Generate synthetic data
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# 5. Ingest the drops in order
python -m solution.cli ingest data/drop_1
python -m solution.cli ingest data/drop_2

# 6. Generate reports
python -m solution.cli report --out reports/

# 7. Run tests
pytest solution/tests/ -v
```

### Expected output files
- `reports/batch_summary.csv` — 6 rows (3 batches × 2 workflows)
- `reports/daily_overview.csv` — one row per workflow per IST date that has calls
- `reports/lead_report.csv` — one row per unique lead
- `reports/rejections.csv` — rejected rows with reason (no PII)
- `reports/data_quality.json` — quality counters

## Design

### Architecture

```text
CSV / JSONL files
       │
       ▼
   CLI (cli.py)
       │
       ├── Lead ingestion
       │     • Header mapping using schema aliases
       │     • Field validation in schema order
       │     • First valid row creates the lead, later rows merge (last non-null wins)
       │     • Defaults applied after merging
       │
       ├── Event ingestion
       │     • Append-only ledger with UNIQUE event_id
       │     • Links events to leads via phone_hash
       │     • Orphans are quarantined and re-linked when the lead arrives later
       │     • disposition.override is linked via call_id
       │
       └── Report generation
             • Single pass over leads + ledger
             • Bucket logic, effective disposition, and daily aggregation
             • Exactly 5 output files as per the contract
```

### Database Schema
I used SQLite by default (PostgreSQL is also supported via `DATABASE_URL`).

| Table | Purpose |
| :--- | :--- |
| `drops` | Tracks which drops have already been ingested |
| `batches` | Tracks which batches have been ingested |
| `leads` | One row per unique `(workflow_id, phone_hash)` |
| `rejections` | Invalid rows (no PII stored) |
| `field_warnings` | Optional invalid fields on accepted rows |
| `ledger` | Append-only event log (`event_id` is `UNIQUE`) |
| `orphan_events` | Events waiting for their lead to arrive |
| `quality_counters` | Running totals for data quality metrics |

#### Key design decisions
- `phone_hash = SHA-256(salt + "+91XXXXXXXXXX")` is used as the join key between leads and events.
- `phone_masked` (`+91******XXXX`) is stored at ingest time for reports.
- `customer_name` is encrypted (AES-256-GCM when available).
- The ledger is strictly append-only. Overrides create new rows; they never update old ones.

### PII Approach
Phone numbers appear in both leads and events, so matching is required.

- I hash the canonical phone number using a stable salt (`PII_SALT`). The raw phone number is never written to the database. Only the hash and a masked version (`+91******XXXX`) are stored.
- Customer names are encrypted. Since names are not needed in any report, this is mainly for future support use-cases.

## Decisions & Assumptions

| Decision | Reason | Alternative considered |
| :--- | :--- | :--- |
| SQLite as default | Easier local development | Postgres-only |
| Drop-level idempotency | Matches the delivery unit in the spec | File-level idempotency |
| Orphans stored and re-linked later | Required by the spec | Re-scanning the full ledger every time |
| `orphan_events` count reflects final state | Spec says “reflect the final state” | Cumulative counter |
| Defaults applied after merging | Explicitly stated in the spec | Apply only on first insert |
| A call is counted only if `call.initiated` exists | Explicit in the spec | Count any event under a `call_id` |
| `success_leads` = distinct leads | Spec requires distinct leads | Counting calls instead |

## Failure Modes Considered

| Scenario | Handled? | Notes |
| :--- | :--- | :--- |
| Re-ingesting the same drop | Yes | Drop-level guard |
| Malformed JSON lines | Yes | Counted and skipped |
| Duplicate `event_ids` | Yes | `UNIQUE` constraint |
| Event arrives before its lead | Yes | Stored as orphan and re-linked later |
| `disposition.override` (no phone) | Yes | Linked via `call_id` |
| Unknown disposition codes | Yes | Counted and ignored |
| Blank / extra CSV columns | Yes | Handled as per spec |
| Crash mid-ingest | Partially | Safe to re-run because of idempotency |

## Testing

### What I tested thoroughly
- Phone, date, decimal, and header normalisation (multiple formats)
- Effective disposition logic (including overrides)
- Lead bucket priority
- End-to-end idempotency

### What I tested manually
- Orphan re-linking across two drops
- Final report correctness on seed 42

### What I didn’t fully automate
- PostgreSQL-specific paths
- Performance on `--scale large`

## Results (`seed=42`, small scale)
- **Lead rows:** 2057 total → 108 rejected → 57 merged → 1892 final leads
- **Events:** 8745 total → 8 malformed → 235 duplicates → 461 final orphans
- **Ingest + report time:** ~4 seconds total on a normal laptop
- The remaining 461 orphans are mostly events for phone numbers that were never uploaded as leads (by design of the generator).

## If I had another day
- Move report aggregation into SQL for better performance
- Add proper integration tests for orphan re-linking
- Improve performance on the large dataset
- Add a short production readiness note (scheduling, monitoring, dead-letter queue)