# AI Usage Disclosure

## Tools used

Antigravity (Claude Sonnet 4.5 Thinking) — an agentic AI assistant

## What I used them for

I used the AI to help scaffold the solution from scratch after I had read through the full spec and understood what was needed. Specifically:

- **Generated the initial structure** of `normalise.py`, `db.py`, `ingest_leads.py`, `ingest_events.py`, `reports.py`, and `cli.py` based on a detailed prompt I wrote describing the exact rules from the spec
- **Wrote the first draft of unit tests** for phone normalisation, date parsing, decimal cleaning, and timestamp utilities — I then reviewed every assertion against the spec
- **Helped debug edge cases** in the SQL placeholder abstraction (`%s` for Postgres vs `?` for SQLite) after I noticed a test failure
- **Assisted with wording** in `SUBMISSION.md` after I had written the content outline myself

## What I wrote or decided myself

- **The database schema design** — I decided on the `phone_hash` as PII-safe join key, the `orphan_events` table for re-linking, and the `quality_counters` single-row pattern
- **The PII strategy** — SHA-256(salt + canonical_phone) for the join key, AES-256-GCM for name encryption, masked phone stored at ingest time
- **The idempotency design** — drop-level guard in `drops` table + batch-level guard in `batches` table, so partial re-runs are safe
- **All decisions in the SUBMISSION.md table** — every ambiguous spec point I resolved myself (e.g. that `orphan_events` in data_quality.json reflects final state not cumulative count, that defaults are applied after merging, that `call.initiated` must be present to count a call)
- **The orphan re-linking design** — events quarantined in `orphan_events`, then resolved via `relink_all_orphans()` after each ingest; override orphans linked via `call_id` after phone-based orphans are resolved

## Where the AI was wrong or unhelpful

- The first generated `norm_header` test assumed CamelCase would be split (`"CustomerName"` → `"customer name"`), but the spec says the function only replaces `_` and whitespace — not word boundaries. I caught this by running the tests and reading the spec again, then corrected both the test and my understanding.
- An early draft of `_fetch_ledger_for_leads` used `?` hardcoded as the SQL placeholder, which would break on PostgreSQL. I noticed this during code review and fixed it to use the connection-type-aware placeholder logic already used elsewhere.
- The initial orphan tracking used a messy inline `INSERT OR IGNORE` with string replacement that was fragile. I rewrote it cleanly using a helper `_is_postgres_conn()` check.

## How I verified AI-generated code

- Ran `pytest solution/tests/ -v` after every change — 82 tests, all pass
- Did a full end-to-end run on `--seed 42 --scale small` data and inspected every report file manually
- Checked idempotency by running `ingest` twice and confirming the "already ingested" skip message, then verified the DB counts didn't change
- Read every function line by line before accepting it, especially the normalization rules and the SQL queries
- Cross-referenced the spec for every field (e.g. confirmed `success_leads` is distinct leads, not call count; confirmed `last_call_at` is from `call.initiated` not `call.ended`)
