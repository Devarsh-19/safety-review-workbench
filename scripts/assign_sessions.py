"""
assign_sessions.py

Assigns sessions to L1 reviewers using interleaved distribution (round-robin).
Every Nth session goes to reviewer N.

Usage:
  python scripts/assign_sessions.py
  python scripts/assign_sessions.py --dry-run
  python scripts/assign_sessions.py --reset
  python scripts/assign_sessions.py --status

Options:
  --dry-run   Show counts without writing to DB
  --reset     Clear all assignments and re-assign
              (asks for confirmation first)
  --status    Show current assignment summary
"""

import argparse
import os
import sqlite3

from dotenv import load_dotenv
load_dotenv()

DB_PATH = os.getenv("DB_PATH", "store/astrotalk.db")

# Assignment configuration
# Modify this list to change who gets sessions and in what rotation order
REVIEWERS = ["Nikhil", "Vineet", "Divyansh"]

# Reviewers whose existing assignments are never touched — not part of the
# round-robin pool, and preserved across --reset (e.g. sessions hand-assigned
# via `ingest_llm_sessions.py --assign-to Devarsh`).
PROTECTED_REVIEWERS = ["Devarsh"]


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def assign_sessions(dry_run: bool = False) -> dict:
    """
    Assigns all unassigned sessions to reviewers using interleaved
    (round-robin) distribution.  Only assigns sessions where
    assigned_to IS NULL.  Returns {reviewer: count} for reporting.
    """
    conn = get_connection()

    cursor = conn.execute(
        """SELECT session_id FROM sessions
           WHERE assigned_to IS NULL
             -- Skip CLEAN sessions auto-submitted by the LLM ingest: they are
             -- already SUBMITTED_FOR_REVIEW (reviewer 'LLM') and get auto-locked
             -- by auto_lock_clean_submitted.py, so they need no manual L1 review.
             -- COALESCE keeps rows with submitted_by IS NULL (3-valued logic
             -- would otherwise drop every non-LLM session too).
             AND NOT (COALESCE(submitted_by, '') = 'LLM' AND overall_verdict = 'CLEAN')
           ORDER BY session_id ASC"""
    )
    session_ids = [row[0] for row in cursor.fetchall()]

    total = len(session_ids)
    if total == 0:
        print("No unassigned sessions found.")
        conn.close()
        return {}

    print(f"Found {total:,} unassigned sessions.")
    print(f"Distributing across: {', '.join(REVIEWERS)}")
    print()

    # Build assignment map using round-robin
    assignments: dict[str, str] = {}
    counts: dict[str, int] = {r: 0 for r in REVIEWERS}

    for i, session_id in enumerate(session_ids):
        reviewer = REVIEWERS[i % len(REVIEWERS)]
        assignments[session_id] = reviewer
        counts[reviewer] += 1

    # Print preview
    print("Assignment preview:")
    for reviewer, count in counts.items():
        pct = count / total * 100
        print(f"  {reviewer:<15} {count:>8,} sessions  ({pct:.1f}%)")
    print()

    if dry_run:
        print("DRY RUN — no changes written to database.")
        conn.close()
        return counts

    # Write assignments to DB in batches of 1000
    print("Writing assignments to database...")
    batch: list[tuple[str, str]] = []
    written = 0

    for session_id, reviewer in assignments.items():
        batch.append((reviewer, session_id))
        if len(batch) >= 1000:
            conn.executemany(
                "UPDATE sessions SET assigned_to = ? WHERE session_id = ?",
                batch,
            )
            conn.commit()
            written += len(batch)
            batch = []
            if written % 50_000 == 0:
                print(f"  {written:,} / {total:,} written...")

    if batch:
        conn.executemany(
            "UPDATE sessions SET assigned_to = ? WHERE session_id = ?",
            batch,
        )
        conn.commit()
        written += len(batch)

    print(f"Done. {written:,} sessions assigned.")
    conn.close()
    return counts


def reset_assignments() -> None:
    """Clears session assignments after confirmation.

    Assignments for PROTECTED_REVIEWERS (e.g. Devarsh) are preserved — they are
    hand-assigned and must survive a reset.
    """
    protected_msg = (f" (keeping {', '.join(PROTECTED_REVIEWERS)})"
                     if PROTECTED_REVIEWERS else "")
    confirm = input(
        f"WARNING: This will clear session assignments{protected_msg}. "
        "Type 'yes' to confirm: "
    ).strip().lower()

    if confirm != "yes":
        print("Reset cancelled.")
        return

    conn = get_connection()
    if PROTECTED_REVIEWERS:
        ph = ",".join("?" * len(PROTECTED_REVIEWERS))
        cur = conn.execute(
            f"UPDATE sessions SET assigned_to = NULL "
            f"WHERE assigned_to IS NOT NULL AND assigned_to NOT IN ({ph})",
            tuple(PROTECTED_REVIEWERS),
        )
    else:
        cur = conn.execute(
            "UPDATE sessions SET assigned_to = NULL WHERE assigned_to IS NOT NULL"
        )
    conn.commit()
    print(f"Cleared assignments for {cur.rowcount:,} sessions"
          f"{' (protected reviewers kept)' if PROTECTED_REVIEWERS else ''}.")
    conn.close()


def print_assignment_summary() -> None:
    """Prints current assignment status from the DB."""
    conn = get_connection()

    print("Current assignment summary:")
    print()

    cursor = conn.execute(
        """SELECT
             assigned_to,
             COUNT(*) AS total,
             SUM(CASE WHEN review_status = 'PENDING'
                 THEN 1 ELSE 0 END) AS pending,
             SUM(CASE WHEN review_status = 'SUBMITTED_FOR_REVIEW'
                 THEN 1 ELSE 0 END) AS submitted,
             SUM(CASE WHEN review_status = 'LOCKED'
                 THEN 1 ELSE 0 END) AS locked
           FROM sessions
           GROUP BY assigned_to
           ORDER BY assigned_to"""
    )

    rows = cursor.fetchall()
    if not rows:
        print("  No assignments found.")
    else:
        print(
            f"  {'REVIEWER':<15} {'TOTAL':>8} "
            f"{'PENDING':>8} {'SUBMITTED':>10} {'LOCKED':>8}"
        )
        print(
            f"  {'-'*15} {'-'*8} {'-'*8} {'-'*10} {'-'*8}"
        )
        for row in rows:
            name = row["assigned_to"] or "UNASSIGNED"
            print(
                f"  {name:<15} {row['total']:>8,} "
                f"{row['pending']:>8,} "
                f"{row['submitted']:>10,} "
                f"{row['locked']:>8,}"
            )

    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Assign AstroTalk sessions to L1 reviewers"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview assignment counts without writing to DB",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear all assignments and re-assign",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show current assignment summary",
    )
    args = parser.parse_args()

    print("=" * 55)
    print("  AstroTalk Session Assignment")
    print("=" * 55)
    print(f"  Database : {DB_PATH}")
    print(f"  Reviewers: {', '.join(REVIEWERS)}")
    print()

    if args.status:
        print_assignment_summary()
    elif args.reset:
        reset_assignments()
        print()
        assign_sessions(dry_run=False)
    else:
        assign_sessions(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
