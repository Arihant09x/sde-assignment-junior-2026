"""
Integration tests: disposition logic, bucket logic, idempotency.
Uses an in-memory SQLite database (no side effects).
"""
import pytest
import sqlite3
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import os

# Point to sqlite in-memory for all tests
os.environ.setdefault("DATABASE_URL", "sqlite:////:memory:")

from solution.normalise import hash_phone, mask_phone
from solution.reports import (
    _effective_disposition,
    _compute_lead_bucket,
)
from solution.schemas import load_dispositions


# ─── Effective disposition ────────────────────────────────────────────────────

class TestEffectiveDisposition:
    def _make_event(self, etype, occurred_at, payload):
        return {
            "event_type": etype,
            "occurred_at": occurred_at,
            "payload_json": json.dumps(payload),
        }

    def test_no_events(self):
        assert _effective_disposition([]) is None

    def test_disposition_set_only(self):
        events = [
            self._make_event("call.initiated", "2026-10-01T10:00:00Z", {}),
            self._make_event("call.connected", "2026-10-01T10:00:10Z", {}),
            self._make_event("disposition.set", "2026-10-01T10:05:00Z",
                             {"disposition_code": "renewed"}),
        ]
        assert _effective_disposition(events) == "renewed"

    def test_override_wins_over_set(self):
        events = [
            self._make_event("disposition.set", "2026-10-01T10:05:00Z",
                             {"disposition_code": "renewed"}),
            self._make_event("disposition.override", "2026-10-01T11:00:00Z",
                             {"disposition_code": "not_interested",
                              "overridden_by": "supervisor_1", "reason": "qa"}),
        ]
        assert _effective_disposition(events) == "not_interested"

    def test_latest_override_wins(self):
        events = [
            self._make_event("disposition.override", "2026-10-01T11:00:00Z",
                             {"disposition_code": "not_interested",
                              "overridden_by": "s1", "reason": "r1"}),
            self._make_event("disposition.override", "2026-10-02T11:00:00Z",
                             {"disposition_code": "dnd",
                              "overridden_by": "s2", "reason": "r2"}),
        ]
        assert _effective_disposition(events) == "dnd"

    def test_latest_set_when_multiple(self):
        events = [
            self._make_event("disposition.set", "2026-10-01T10:05:00Z",
                             {"disposition_code": "callback_requested"}),
            self._make_event("disposition.set", "2026-10-02T10:05:00Z",
                             {"disposition_code": "renewed"}),
        ]
        assert _effective_disposition(events) == "renewed"

    def test_no_disposition_events(self):
        events = [
            self._make_event("call.initiated", "2026-10-01T10:00:00Z", {}),
            self._make_event("call.connected", "2026-10-01T10:00:10Z", {}),
        ]
        assert _effective_disposition(events) is None


# ─── Lead bucket logic ────────────────────────────────────────────────────────

class TestLeadBucket:
    DISPOSITIONS = load_dispositions()

    def _initiated_event(self):
        return {"event_type": "call.initiated", "occurred_at": "2026-10-01T10:00:00Z",
                "payload_json": "{}"}

    def _telephony_failed_event(self):
        return {"event_type": "call.telephony_failed", "occurred_at": "2026-10-01T10:00:05Z",
                "payload_json": json.dumps({"reason": "carrier_rejected"})}

    def _disp_set(self, code):
        return {"event_type": "disposition.set", "occurred_at": "2026-10-01T10:05:00Z",
                "payload_json": json.dumps({"disposition_code": code})}

    def test_no_calls(self):
        assert _compute_lead_bucket("motor_renewal", {}, self.DISPOSITIONS) == "yet_to_reach_out"

    def test_terminal_disposition_completed(self):
        calls = {
            "call_1": [self._initiated_event(), self._disp_set("renewed")]
        }
        assert _compute_lead_bucket("motor_renewal", calls, self.DISPOSITIONS) == "completed"

    def test_non_terminal_in_progress(self):
        calls = {
            "call_1": [self._initiated_event(), self._disp_set("callback_requested")]
        }
        assert _compute_lead_bucket("motor_renewal", calls, self.DISPOSITIONS) == "in_progress_dialed"

    def test_all_telephony_failed(self):
        calls = {
            "call_1": [self._initiated_event(), self._telephony_failed_event()],
            "call_2": [self._initiated_event(), self._telephony_failed_event()],
        }
        assert _compute_lead_bucket(
            "motor_renewal", calls, self.DISPOSITIONS
        ) == "in_progress_telephony_failed"

    def test_mixed_failed_and_other(self):
        # Only one call telephony failed; other has no disposition → in_progress_dialed
        calls = {
            "call_1": [self._initiated_event(), self._telephony_failed_event()],
            "call_2": [self._initiated_event()],  # no telephony_failed
        }
        assert _compute_lead_bucket(
            "motor_renewal", calls, self.DISPOSITIONS
        ) == "in_progress_dialed"

    def test_completed_takes_priority_over_failed(self):
        # Even if one call failed, if another is terminal → completed
        calls = {
            "call_1": [self._initiated_event(), self._telephony_failed_event()],
            "call_2": [self._initiated_event(), self._disp_set("renewed")],
        }
        assert _compute_lead_bucket("motor_renewal", calls, self.DISPOSITIONS) == "completed"

    def test_policy_feedback_terminal(self):
        calls = {
            "call_1": [self._initiated_event(),
                       {"event_type": "disposition.set", "occurred_at": "2026-10-01T10:05:00Z",
                        "payload_json": json.dumps({"disposition_code": "feedback_captured"})}]
        }
        assert _compute_lead_bucket("policy_feedback", calls, self.DISPOSITIONS) == "completed"


# ─── Idempotency test ─────────────────────────────────────────────────────────

class TestIdempotency:
    """
    Runs the ingest pipeline twice on the same drop and checks that the DB
    state is identical (not doubled).
    """
    def setup_method(self):
        # Use a temp SQLite file per test
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        # Re-import to pick up new env
        import importlib
        import solution.config
        importlib.reload(solution.config)
        import solution.db
        importlib.reload(solution.db)

    def teardown_method(self):
        os.close(self.db_fd)
        try:
            os.unlink(self.db_path)
        except Exception:
            pass

    def test_double_ingest_same_result(self):
        from solution.db import init_db, db_conn
        from solution.cli import cmd_ingest

        # Generate data
        import subprocess, sys
        data_dir = Path(tempfile.mkdtemp())
        subprocess.run(
            [sys.executable, "assignment-a-data/tools/generate_data.py",
             "--seed", "99", "--scale", "small", "--out", str(data_dir)],
            cwd=Path(__file__).parent.parent.parent,
            check=True, capture_output=True
        )

        drop1 = data_dir / "drop_1"

        # First ingest
        cmd_ingest(str(drop1))

        # Count leads after first ingest
        from solution.db import get_connection
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM leads")
        count1 = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM rejections")
        rej1 = cur.fetchone()[0]
        conn.close()

        # Second ingest of same drop
        cmd_ingest(str(drop1))

        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM leads")
        count2 = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM rejections")
        rej2 = cur.fetchone()[0]
        conn.close()

        assert count1 == count2, "Lead count should be identical after re-ingest"
        assert rej1 == rej2, "Rejection count should be identical after re-ingest"

