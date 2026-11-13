## Solution – Assignment A: Interaction Ledger & Reporting Pipeline

All code lives here under `solution/`.

### Module layout

```
solution/
├── __init__.py
├── cli.py          # Entry point: ingest + report commands
├── config.py       # DATABASE_URL, PII_SALT from env/.env
├── db.py           # Schema DDL, connection factory (SQLite + PostgreSQL)
├── normalise.py    # Phone, date, decimal, header, PII, timestamp utilities
├── schemas.py      # Workflow JSON schema loader, disposition CSV loader
├── ingest_leads.py # Lead CSV ingestion (validate, merge, reject)
├── ingest_events.py# JSONL event ingestion (dedupe, link, orphan handling)
├── reports.py      # 5 report file generators
└── tests/
    ├── test_normalise.py  # 68 unit tests for all normalisation utilities
    └── test_pipeline.py   # 14 tests: disposition, bucket, idempotency
```

### Quick start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure database (SQLite default, no config needed)
#    For PostgreSQL/NeonDB, create .env:
#    DATABASE_URL=postgresql://user:pass@host/dbname
#    PII_SALT=your-secret-salt

# 3. Generate synthetic data
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# 4. Ingest both drops
python -m solution.cli ingest data/drop_1
python -m solution.cli ingest data/drop_2

# 5. Generate reports
python -m solution.cli report --out reports/

# 6. Run tests
pytest solution/tests/ -v
```

### Database

By default uses SQLite (`pipeline.db` in repo root). To use NeonDB/PostgreSQL:

```
DATABASE_URL=postgresql://user:password@ep-xxx.neon.tech/dbname?sslmode=require
```

Set this in your `.env` file or as an environment variable.
