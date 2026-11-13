"""
pytest configuration: set DATABASE_URL to a temp SQLite file for each test session.
Keeps tests isolated from the production database.
"""
import os
import tempfile
import pytest


@pytest.fixture(scope="session", autouse=True)
def test_database(tmp_path_factory):
    """Point DATABASE_URL to a session-scoped temp SQLite file."""
    db_file = tmp_path_factory.mktemp("db") / "test_pipeline.db"
    os.environ["DATABASE_URL"] = f"sqlite:///{db_file}"
    yield str(db_file)

