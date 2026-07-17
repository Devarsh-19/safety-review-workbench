"""
assign_audio_sessions.py

Assigns audio sessions to L1 reviewers using interleaved distribution
(round-robin) — audio counterpart of assign_sessions.py, against the
audio review DB (store/audio_review.db).

Usage:
  python scripts/assign_audio_sessions.py
  python scripts/assign_audio_sessions.py --dry-run
  python scripts/assign_audio_sessions.py --reset
  python scripts/assign_audio_sessions.py --status
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import (  # noqa: E402
    get_audio_connection, initialise_audio_db, AUDIO_DB_PATH, is_multilingual_language,
)

# Modify this list to change who gets sessions and in what rotation order.
REVIEWERS = ["Nikhil", "Vineet", "Divyansh","Gaurav","Yusuf"]
# REVIEWERS = ["Devarsh"]

# Regional-language (non-Hindi/English/Hinglish) sessions are assigned to this
# reviewer only, never to the REVIEWERS rotation.
MULTILINGUAL_REVIEWER = "Multilingual"


def assign_sessions(dry_run: bool = False) -> dict:
    """Assign unassigned audio sessions: regional (non-Hindi/English/Hinglish)
    sessions go to the Multilingual reviewer; the rest round-robin across
    REVIEWERS."""
    conn = get_audio_connection()
    rows = conn.execute(
        "SELECT s_id, lang FROM audio_sessions WHERE assigned_to IS NULL ORDER BY s_id ASC"
    ).fetchall()

    total = len(rows)
    if total == 0:
        print("No unassigned audio sessions found.")
        conn.close()
        return {}

    # Split by language: regional -> Multilingual, everything else -> rotation.
    regional = [r["s_id"] for r in rows if is_multilingual_language(r["lang"])]
    general  = [r["s_id"] for r in rows if not is_multilingual_language(r["lang"])]

    print(f"Found {total:,} unassigned audio sessions "
          f"({len(general):,} Hindi/English/Hinglish, {len(regional):,} regional).")
    print(f"Distributing across: {', '.join(REVIEWERS)} "
          f"(+ {MULTILINGUAL_REVIEWER} for regional)")
    print()

    counts = {r: 0 for r in REVIEWERS}
    counts[MULTILINGUAL_REVIEWER] = len(regional)
    assignments = [(MULTILINGUAL_REVIEWER, s_id) for s_id in regional]
    for i, s_id in enumerate(general):
        reviewer = REVIEWERS[i % len(REVIEWERS)]
        assignments.append((reviewer, s_id))
        counts[reviewer] += 1

    print("Assignment preview:")
    for reviewer, count in counts.items():
        print(f"  {reviewer:<15} {count:>8,} sessions  ({count / total * 100:.1f}%)")
    print()

    if dry_run:
        print("DRY RUN — no changes written to database.")
        conn.close()
        return counts

    conn.executemany(
        "UPDATE audio_sessions SET assigned_to = ? WHERE s_id = ?", assignments
    )
    conn.commit()
    conn.close()
    print(f"Done. {total:,} audio sessions assigned.")
    return counts


def reset_assignments() -> None:
    confirm = input(
        "WARNING: This will clear ALL audio session assignments. Type 'yes' to confirm: "
    ).strip().lower()
    if confirm != "yes":
        print("Reset cancelled.")
        return
    conn = get_audio_connection()
    cur = conn.execute(
        "UPDATE audio_sessions SET assigned_to = NULL WHERE assigned_to IS NOT NULL"
    )
    conn.commit()
    conn.close()
    print(f"Cleared assignments for {cur.rowcount:,} audio sessions.")

def print_assignment_summary() -> None:
    conn = get_audio_connection()
    rows = conn.execute(
        """SELECT assigned_to,
                  COUNT(*) AS total,
                  SUM(CASE WHEN review_status = 'PENDING'              THEN 1 ELSE 0 END) AS pending,
                  SUM(CASE WHEN review_status = 'SUBMITTED_FOR_REVIEW' THEN 1 ELSE 0 END) AS submitted,
                  SUM(CASE WHEN review_status = 'LOCKED'               THEN 1 ELSE 0 END) AS locked
           FROM audio_sessions
           GROUP BY assigned_to
           ORDER BY assigned_to"""
    ).fetchall()
    conn.close()

    print("Current audio assignment summary:")
    print()
    if not rows:
        print("  No audio sessions found.")
        return
    print(f"  {'REVIEWER':<15} {'TOTAL':>8} {'PENDING':>8} {'SUBMITTED':>10} {'LOCKED':>8}")
    print(f"  {'-'*15} {'-'*8} {'-'*8} {'-'*10} {'-'*8}")
    for row in rows:
        name = row["assigned_to"] or "UNASSIGNED"
        print(f"  {name:<15} {row['total']:>8,} {row['pending']:>8,} "
              f"{row['submitted']:>10,} {row['locked']:>8,}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Assign audio sessions to L1 reviewers")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--reset", action="store_true", help="Clear all assignments and re-assign")
    parser.add_argument("--status", action="store_true", help="Show current assignment summary")
    args = parser.parse_args()

    print("=" * 55)
    print("  AstroTalk AUDIO Session Assignment")
    print("=" * 55)
    print(f"  Database : {AUDIO_DB_PATH}")
    print(f"  Reviewers: {', '.join(REVIEWERS)}")
    print()

    initialise_audio_db()
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
