"""
Configuration: loads DATABASE_URL from env or .env file.
Defaults to a local SQLite file if DATABASE_URL is not set.
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# PII salt – change in production via env var; must be stable across runs
PII_SALT = os.environ.get("PII_SALT", "au_assignment_salt_2026")

# Database URL: set DATABASE_URL in .env or environment.
# Falls back to SQLite if not set.
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    f"sqlite:///{Path(__file__).parent.parent / 'pipeline.db'}"
)

IST_OFFSET = "+05:30"

