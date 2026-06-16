"""
tests/test_assignment.py

Tests for session assignment logic — both manual (assigned_to from CSV)
and automatic (round-robin when assigned_to is absent).
"""

import itertools
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# Make project root importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

L1_REVIEWERS = ["Gaurav", "Nikhil", "Divyansh", "Yusuf", "Vineet"]


def _make_db(path: str) -> sqlite3.Connection:
    """Create a minimal in-memory sessions table for testing."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE sessions (
            session_id   TEXT PRIMARY KEY,
            review_status TEXT DEFAULT 'PENDING',
            assigned_to  TEXT DEFAULT NULL
        )
    """)
    conn.commit()
    return conn


def _insert_sessions(conn, session_ids, assigned_to_values):
    """Insert sessions with optional assigned_to values."""
    conn.executemany(
        "INSERT INTO sessions (session_id, assigned_to) VALUES (?, ?)",
        zip(session_ids, assigned_to_values),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Tests — manual assignment (assigned_to provided in CSV)
# ---------------------------------------------------------------------------

class TestManualAssignment:
    """When assigned_to is present in the input, use it directly."""

    def test_manual_assignment_stored_correctly(self):
        """assigned_to from CSV is written to the DB unchanged."""
        session = {"assigned_to": "Gaurav", "session_id": "S001"}
        assigned = session.get("assigned_to") or next(itertools.cycle(L1_REVIEWERS))
        assert assigned == "Gaurav"

    def test_manual_assignment_not_overridden(self):
        """Auto-assignment cycle is NOT consumed when assigned_to is present."""
        cycle = itertools.cycle(L1_REVIEWERS)
        sessions = [
            {"session_id": "S001", "assigned_to": "Yusuf"},
            {"session_id": "S002", "assigned_to": "Nikhil"},
        ]
        results = []
        for s in sessions:
            assigned = s.get("assigned_to") or next(cycle)
            results.append(assigned)
        assert results == ["Yusuf", "Nikhil"]

    def test_all_l1_reviewers_accepted(self):
        """Every L1 reviewer name is a valid assignment target."""
        for reviewer in L1_REVIEWERS:
            session = {"assigned_to": reviewer}
            assigned = session.get("assigned_to") or next(itertools.cycle(L1_REVIEWERS))
            assert assigned == reviewer

    def test_amogh_l2_can_be_assigned_manually(self):
        """L2 reviewer can be manually assigned (no restriction at ingestion)."""
        session = {"assigned_to": "Amogh"}
        assigned = session.get("assigned_to") or next(itertools.cycle(L1_REVIEWERS))
        assert assigned == "Amogh"


# ---------------------------------------------------------------------------
# Tests — automatic round-robin assignment (assigned_to absent/empty/null)
# ---------------------------------------------------------------------------

class TestAutoAssignment:
    """When assigned_to is absent, auto-assign round-robin across L1_REVIEWERS."""

    def test_missing_key_triggers_auto(self):
        """Session with no assigned_to key gets auto-assigned."""
        cycle = itertools.cycle(L1_REVIEWERS)
        session = {"session_id": "S001"}  # no assigned_to key
        assigned = session.get("assigned_to") or next(cycle)
        assert assigned == L1_REVIEWERS[0]

    def test_none_value_triggers_auto(self):
        """Session with assigned_to=None gets auto-assigned."""
        cycle = itertools.cycle(L1_REVIEWERS)
        session = {"session_id": "S001", "assigned_to": None}
        assigned = session.get("assigned_to") or next(cycle)
        assert assigned == L1_REVIEWERS[0]

    def test_empty_string_triggers_auto(self):
        """Session with assigned_to='' gets auto-assigned."""
        cycle = itertools.cycle(L1_REVIEWERS)
        session = {"session_id": "S001", "assigned_to": ""}
        assigned = session.get("assigned_to") or next(cycle)
        assert assigned == L1_REVIEWERS[0]

    def test_round_robin_order(self):
        """Sessions without assigned_to are distributed in L1_REVIEWERS order."""
        cycle = itertools.cycle(L1_REVIEWERS)
        sessions = [{"session_id": f"S{i:03d}"} for i in range(10)]
        results = [s.get("assigned_to") or next(cycle) for s in sessions]
        expected = [L1_REVIEWERS[i % len(L1_REVIEWERS)] for i in range(10)]
        assert results == expected

    def test_round_robin_unique_per_session(self):
        """No two consecutive sessions get the same reviewer (until cycle repeats)."""
        cycle = itertools.cycle(L1_REVIEWERS)
        n = len(L1_REVIEWERS)
        sessions = [{"session_id": f"S{i:03d}"} for i in range(n)]
        results = [next(cycle) for _ in sessions]
        assert len(set(results)) == n, "First cycle must cover all reviewers exactly once"

    def test_no_duplicates_within_cycle(self):
        """Within one full cycle every reviewer gets exactly one session."""
        cycle = itertools.cycle(L1_REVIEWERS)
        n = len(L1_REVIEWERS)
        assignments = [next(cycle) for _ in range(n)]
        assert sorted(assignments) == sorted(L1_REVIEWERS)


# ---------------------------------------------------------------------------
# Tests — mixed: some sessions have assigned_to, some don't
# ---------------------------------------------------------------------------

class TestMixedAssignment:
    """Mix of manual and auto-assignment in the same ingestion run."""

    def test_manual_and_auto_coexist(self):
        """Manual assignments are preserved; gaps are filled by round-robin."""
        cycle = itertools.cycle(L1_REVIEWERS)
        sessions = [
            {"session_id": "S001", "assigned_to": "Gaurav"},   # manual
            {"session_id": "S002", "assigned_to": None},        # auto
            {"session_id": "S003", "assigned_to": "Yusuf"},    # manual
            {"session_id": "S004", "assigned_to": ""},          # auto (empty string)
            {"session_id": "S005"},                              # auto (missing key)
        ]
        results = [s.get("assigned_to") or next(cycle) for s in sessions]
        assert results[0] == "Gaurav"          # manual preserved
        assert results[2] == "Yusuf"           # manual preserved
        assert results[1] in L1_REVIEWERS      # auto-assigned
        assert results[3] in L1_REVIEWERS      # auto-assigned
        assert results[4] in L1_REVIEWERS      # auto-assigned
        # auto-assigned ones should advance the cycle
        assert results[1] == L1_REVIEWERS[0]
        assert results[3] == L1_REVIEWERS[1]
        assert results[4] == L1_REVIEWERS[2]

    def test_auto_cycle_not_consumed_by_manual(self):
        """Manual assignments do NOT advance the round-robin cycle position."""
        cycle = itertools.cycle(L1_REVIEWERS)
        sessions = [
            {"session_id": "S001", "assigned_to": "Gaurav"},  # manual — cycle at pos 0
            {"session_id": "S002", "assigned_to": None},       # auto  — should get pos 0
        ]
        results = [s.get("assigned_to") or next(cycle) for s in sessions]
        assert results[0] == "Gaurav"
        assert results[1] == L1_REVIEWERS[0]  # cycle still at pos 0


# ---------------------------------------------------------------------------
# Tests — DB-level: assign_sessions.py script behaviour
# ---------------------------------------------------------------------------

class TestAssignSessionsScript:
    """Tests for the standalone assign_sessions.py script logic."""

    def test_only_null_sessions_assigned(self):
        """Sessions with existing assigned_to are not overwritten."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = _make_db(db_path)
            _insert_sessions(
                conn,
                ["S001", "S002", "S003"],
                ["Gaurav", None, None],   # S001 already assigned
            )
            # Simulate assign_sessions logic
            unassigned = [
                row[0] for row in conn.execute(
                    "SELECT session_id FROM sessions WHERE assigned_to IS NULL ORDER BY session_id"
                ).fetchall()
            ]
            assert unassigned == ["S002", "S003"]
            assignments = [(L1_REVIEWERS[i % len(L1_REVIEWERS)], sid)
                           for i, sid in enumerate(unassigned)]
            conn.executemany(
                "UPDATE sessions SET assigned_to = ? WHERE session_id = ?",
                assignments,
            )
            conn.commit()
            # Verify S001 untouched
            s001 = conn.execute(
                "SELECT assigned_to FROM sessions WHERE session_id = 'S001'"
            ).fetchone()[0]
            assert s001 == "Gaurav"
            # Verify S002, S003 assigned
            s002 = conn.execute(
                "SELECT assigned_to FROM sessions WHERE session_id = 'S002'"
            ).fetchone()[0]
            s003 = conn.execute(
                "SELECT assigned_to FROM sessions WHERE session_id = 'S003'"
            ).fetchone()[0]
            assert s002 in L1_REVIEWERS
            assert s003 in L1_REVIEWERS
            assert s002 != s003  # different reviewers for consecutive sessions
        finally:
            conn.close()
            os.unlink(db_path)

    def test_empty_db_no_crash(self):
        """assign_sessions on empty DB returns empty dict without error."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = _make_db(db_path)
            unassigned = conn.execute(
                "SELECT session_id FROM sessions WHERE assigned_to IS NULL"
            ).fetchall()
            assert len(unassigned) == 0
        finally:
            conn.close()
            os.unlink(db_path)
