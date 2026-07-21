#!/usr/bin/env python3
"""
assign_speaker_roles_by_flags.py

Auto-assigns speaker roles on UNASSIGNED audio sessions (both speaker1_role and
speaker2_role are NULL), inferring the roles from which speaker(s) carry the
session's active flags.

Rules (per unassigned session)
------------------------------
Look at which ranked speakers have at least one ACTIVE flag:
  * Only ONE speaker is flagged            -> that speaker = USER.
  * BOTH speaker 1 AND speaker 2 flagged   -> speaker 1 = ASTROLOGER,
                                              speaker 2 = USER.
  * No speaker has an active flag          -> left untouched (nothing to infer).

"Speaker 1 / Speaker 2" are the ranked diarization labels: per session the
distinct labels are ordered by the numeric part of the label (SPEAKER_1 before
SPEAKER_2); the 1st maps to speaker1_role, the 2nd to speaker2_role — the same
convention as AudioSessionViewer and scripts/astro_flag_ge5.py.

"Active" flag = not DISMISSED and not an amended original (matches the review
queue and the other audio scripts).

When only one speaker is flagged, only that speaker's role is set; the other
speaker's role is left NULL (nothing in the flags tells us what it is). This
mirrors the existing single-speaker handling (speaker1_role = USER).

Usage
-----
    python scripts/assign_speaker_roles_by_flags.py --dry-run
    python scripts/assign_speaker_roles_by_flags.py
    python scripts/assign_speaker_roles_by_flags.py --reviewer alice
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import get_audio_connection, AUDIO_DB_PATH   # noqa: E402

ASTROLOGER = "ASTROLOGER"
USER = "USER"

# Per unassigned session, the ranked speakers (1 / 2) that carry an active flag.
FLAG_RANKS_SQL = """
    WITH ranked_labels AS (
        SELECT s_id, speaker AS label,
               ROW_NUMBER() OVER (
                   PARTITION BY s_id
                   ORDER BY CAST(
                       CASE WHEN instr(speaker,'_') > 0
                            THEN substr(speaker, instr(speaker,'_')+1)
                            ELSE speaker END AS INTEGER), speaker
               ) AS rn
        FROM (SELECT DISTINCT s_id, speaker FROM audio_segments WHERE speaker IS NOT NULL)
    ),
    active_flags AS (
        SELECT af.s_id, af.seg_id
        FROM audio_flags af
        WHERE (af.status IS NULL OR af.status != 'DISMISSED')
          AND af.flag_id NOT IN (
              SELECT parent_flag_id FROM audio_flags WHERE parent_flag_id IS NOT NULL
          )
    ),
    flag_ranks AS (
        SELECT DISTINCT seg.s_id AS s_id, rl.rn AS rank
        FROM active_flags f
        JOIN audio_segments seg ON seg.s_id = f.s_id AND seg.seg_id = f.seg_id
        JOIN ranked_labels rl ON rl.s_id = seg.s_id AND rl.label = seg.speaker
    )
    SELECT fr.s_id AS s_id, fr.rank AS rank
    FROM flag_ranks fr
    JOIN audio_sessions ss ON ss.s_id = fr.s_id
    WHERE ss.speaker1_role IS NULL AND ss.speaker2_role IS NULL
    ORDER BY fr.s_id, fr.rank
"""


def _decide(ranks: set) -> tuple:
    """Return (speaker1_role, speaker2_role, reason) or None if nothing to do.

    Only ranks 1 and 2 can be assigned; flags on a 3rd+ speaker are ignored.
    A role of None means "leave unchanged".
    """
    ranks = ranks & {1, 2}
    if ranks == {1, 2}:
        return ASTROLOGER, USER, "both speakers flagged -> spk1=ASTROLOGER, spk2=USER"
    if ranks == {1}:
        return USER, None, "only speaker 1 flagged -> spk1=USER"
    if ranks == {2}:
        return None, USER, "only speaker 2 flagged -> spk2=USER"
    return None  # no rank-1/2 active flags


def compute_assignments(conn) -> list:
    """Return a list of (s_id, speaker1_role, speaker2_role, reason)."""
    ranks_by_session: dict = {}
    for row in conn.execute(FLAG_RANKS_SQL):
        ranks_by_session.setdefault(row["s_id"], set()).add(row["rank"])

    assignments = []
    for s_id in sorted(ranks_by_session):
        decision = _decide(ranks_by_session[s_id])
        if decision is None:
            continue
        sp1, sp2, reason = decision
        assignments.append((s_id, sp1, sp2, reason))
    return assignments


def apply_assignments(conn, assignments: list, reviewer: str) -> None:
    for s_id, sp1, sp2, reason in assignments:
        sets, params = [], []
        if sp1 is not None:
            sets.append("speaker1_role = ?")
            params.append(sp1)
        if sp2 is not None:
            sets.append("speaker2_role = ?")
            params.append(sp2)
        params.append(s_id)
        conn.execute(
            f"UPDATE audio_sessions SET {', '.join(sets)} WHERE s_id = ?", params
        )
        conn.execute(
            """INSERT INTO audio_review_log (s_id, action, reviewer_id, note)
               VALUES (?, 'SET_SPEAKER_ROLES', ?, ?)""",
            (s_id, reviewer, f"auto: {reason}"),
        )
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auto-assign speaker roles on unassigned audio sessions "
                    "from active-flag ownership."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview assignments without writing.")
    parser.add_argument("--reviewer", default="auto_role_assign",
                        help="reviewer_id recorded in audio_review_log "
                             "(default: auto_role_assign).")
    args = parser.parse_args()

    print("=" * 64)
    print("  Auto-assign speaker roles from active-flag ownership")
    print(f"  AUDIO DB: {AUDIO_DB_PATH}")
    print("=" * 64)

    conn = get_audio_connection()
    try:
        unassigned = conn.execute(
            "SELECT COUNT(*) FROM audio_sessions "
            "WHERE speaker1_role IS NULL AND speaker2_role IS NULL"
        ).fetchone()[0]
        assignments = compute_assignments(conn)

        print(f"  Unassigned sessions        : {unassigned:>6,}")
        print(f"  Assignable (have flags)    : {len(assignments):>6,}")
        print(f"  Skipped (no active flags)  : {unassigned - len(assignments):>6,}")

        if assignments:
            print("\n  Planned assignments:")
            for s_id, sp1, sp2, reason in assignments:
                print(f"    session {s_id}: "
                      f"spk1={sp1 or '(unchanged)'}, spk2={sp2 or '(unchanged)'}"
                      f"   [{reason}]")

        if args.dry_run:
            print("\nDRY RUN — no changes written.")
            return

        if assignments:
            apply_assignments(conn, assignments, args.reviewer)
        print(f"\nDone. Assigned {len(assignments)} session(s).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
