#!/usr/bin/env python3
"""
assign_speaker_roles_by_flags.py

Auto-assigns speaker roles on audio sessions that are NOT fully assigned (at
least one of speaker1_role / speaker2_role is NULL), inferring the roles from
which speaker(s) carry the session's active flags — and then filling in every
remaining determinable slot so no speaker is left half-mapped.

The two roles are mutually exclusive opposites (ASTROLOGER / USER, enforced by
store.audio_db.set_speaker_roles), so knowing ONE speaker's role fully
determines the other's. The script leans on that to close the gaps the flags
alone can't: whenever one role is known, the partner speaker (if the session
really has a second speaker) gets the opposite role.

Rules (per session, applied in this priority order)
----------------------------------------------------
1. Existing roles win. If a role is already set, it is never overwritten. If
   exactly one of two present speakers already has a role, the other is set to
   its opposite (completing a partially-assigned session — no flags needed).
2. Otherwise infer from which ranked speakers have an ACTIVE flag:
     * Only ONE speaker flagged           -> that speaker = USER.
     * BOTH speaker 1 AND speaker 2 flagged-> speaker 1 = ASTROLOGER,
                                              speaker 2 = USER.
   Then the partner speaker (if present) gets the opposite role, so BOTH
   speakers of a flagged session end up mapped.
3. No existing role and no active flag -> fall back to a talk-time heuristic so
   the session is still mapped: the speaker with the most total talk-time (sum
   of segment durations, segment count as tiebreak) is taken to be the
   ASTROLOGER and the other the USER, since the astrologer does most of the
   talking in these consultations. A lone single speaker defaults to ASTROLOGER.
   Pass --no-fallback to skip this and leave such sessions untouched instead.

"Speaker 1 / Speaker 2" are the ranked diarization labels: per session the
distinct labels are ordered by the numeric part of the label (SPEAKER_1 before
SPEAKER_2); the 1st maps to speaker1_role, the 2nd to speaker2_role — the same
convention as AudioSessionViewer and scripts/astro_flag_ge5.py. A session with
only one distinct speaker has no second slot to fill.

"Active" flag = not DISMISSED and not an amended original (matches the review
queue and the other audio scripts).

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


def _opposite(role: str) -> str:
    """The other of the two mutually-exclusive roles."""
    return ASTROLOGER if role == USER else USER


# For every session that is not fully assigned (>=1 role NULL) and has diarized
# speakers, one row per ranked speaker: its rank (1/2/...), whether that speaker
# carries an active flag, and the session's current roles.
ROLE_INFER_SQL = """
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
        SELECT DISTINCT af.s_id, af.seg_id
        FROM audio_flags af
        WHERE (af.status IS NULL OR af.status != 'DISMISSED')
          AND af.flag_id NOT IN (
              SELECT parent_flag_id FROM audio_flags WHERE parent_flag_id IS NOT NULL
          )
    ),
    seg_rank AS (
        -- active_flags is DISTINCT (s_id, seg_id), so the LEFT JOIN never fans a
        -- segment out; talk / seg_count stay one-row-per-segment accurate.
        SELECT seg.s_id AS s_id, rl.rn AS rank,
               MAX(CASE WHEN f.seg_id IS NOT NULL THEN 1 ELSE 0 END) AS is_flagged,
               SUM(COALESCE(seg.ts_end, 0) - COALESCE(seg.ts_start, 0)) AS talk,
               COUNT(*) AS seg_count
        FROM audio_segments seg
        JOIN ranked_labels rl ON rl.s_id = seg.s_id AND rl.label = seg.speaker
        LEFT JOIN active_flags f ON f.s_id = seg.s_id AND f.seg_id = seg.seg_id
        GROUP BY seg.s_id, rl.rn
    )
    SELECT sr.s_id AS s_id, sr.rank AS rank, sr.is_flagged AS is_flagged,
           sr.talk AS talk, sr.seg_count AS seg_count,
           ss.speaker1_role AS spk1, ss.speaker2_role AS spk2
    FROM seg_rank sr
    JOIN audio_sessions ss ON ss.s_id = sr.s_id
    WHERE ss.speaker1_role IS NULL OR ss.speaker2_role IS NULL
    ORDER BY sr.s_id, sr.rank
"""

# Count of sessions that still need work: not fully assigned AND have at least
# one diarized speaker (so there is a slot that could be filled).
NEEDING_WORK_SQL = """
    SELECT COUNT(*) FROM audio_sessions ss
    WHERE (ss.speaker1_role IS NULL OR ss.speaker2_role IS NULL)
      AND EXISTS (SELECT 1 FROM audio_segments seg
                  WHERE seg.s_id = ss.s_id AND seg.speaker IS NOT NULL)
"""


def _reason(orig1, orig2, flagged: set, new1, new2, by_talk: bool = False) -> str:
    """Human-readable explanation of what drove the assignment."""
    sets = [p for p in (f"spk1={new1}" if new1 else None,
                        f"spk2={new2}" if new2 else None) if p]
    tgt = ", ".join(sets)
    if orig1 is not None or orig2 is not None:
        return f"complement of existing role -> {tgt}"
    f = flagged & {1, 2}
    if f == {1, 2}:
        return f"both speakers flagged -> {tgt}"
    if f == {1}:
        return f"only speaker 1 flagged -> {tgt}"
    if f == {2}:
        return f"only speaker 2 flagged -> {tgt}"
    if by_talk:
        return f"no flags; assigned by talk-time -> {tgt}"
    return f"-> {tgt}"


def _decide(present: set, flagged: set, spk1, spk2, talk=None,
            seg_count=None, fallback: bool = True) -> tuple:
    """Decide the roles to WRITE for one session.

    Returns (new_speaker1_role, new_speaker2_role, reason), where a None slot
    means "leave unchanged" (already set, not present, or unknowable). Returns
    None when there is nothing to write.

    Only ranks 1 and 2 map to the two role columns; a 3rd+ speaker is ignored.
    Priority: existing roles win, then active-flag inference, then (unless
    fallback is off) a talk-time heuristic so every session gets mapped.
    """
    present = present & {1, 2}
    flagged = flagged & {1, 2}
    talk = talk or {}
    seg_count = seg_count or {}
    roles = {1: spk1, 2: spk2}

    def complement():
        # Two speakers present, exactly one role known -> partner is the opposite.
        if present == {1, 2}:
            if roles[1] is not None and roles[2] is None:
                roles[2] = _opposite(roles[1])
            elif roles[2] is not None and roles[1] is None:
                roles[1] = _opposite(roles[2])

    # 1) Existing role(s) win: complete the partner from what's already there.
    complement()

    # 2) Only if nothing is known yet, infer from active flags, then complete
    #    the partner so both speakers of a flagged session get mapped.
    if roles[1] is None and roles[2] is None:
        if flagged == {1, 2}:
            cand = {1: ASTROLOGER, 2: USER}
        elif flagged == {1}:
            cand = {1: USER}
        elif flagged == {2}:
            cand = {2: USER}
        else:
            cand = {}
        for r, role in cand.items():
            if r in present:
                roles[r] = role
        complement()

    # 3) Still nothing (no roles, no flags): talk-time fallback. The speaker who
    #    talks most is taken as the ASTROLOGER; a lone speaker defaults to it.
    by_talk = False
    if fallback and roles[1] is None and roles[2] is None:
        by_talk = True
        if present == {1, 2}:
            # Higher (talk, seg_count) wins ASTROLOGER; ties favour speaker 1.
            key1 = (talk.get(1, 0) or 0, seg_count.get(1, 0) or 0)
            key2 = (talk.get(2, 0) or 0, seg_count.get(2, 0) or 0)
            if key1 >= key2:
                roles[1], roles[2] = ASTROLOGER, USER
            else:
                roles[1], roles[2] = USER, ASTROLOGER
        elif present == {1}:
            roles[1] = ASTROLOGER
        elif present == {2}:
            roles[2] = ASTROLOGER

    # Write only the slots that were NULL and are now determined.
    new1 = roles[1] if (spk1 is None and roles[1] is not None) else None
    new2 = roles[2] if (spk2 is None and roles[2] is not None) else None
    if new1 is None and new2 is None:
        return None
    return new1, new2, _reason(spk1, spk2, flagged, new1, new2, by_talk)


def compute_assignments(conn, fallback: bool = True) -> list:
    """Return a list of (s_id, speaker1_role, speaker2_role, reason)."""
    by_session: dict = {}
    for row in conn.execute(ROLE_INFER_SQL):
        d = by_session.setdefault(
            row["s_id"],
            {"present": set(), "flagged": set(), "talk": {}, "seg_count": {},
             "spk1": row["spk1"], "spk2": row["spk2"]},
        )
        d["present"].add(row["rank"])
        d["talk"][row["rank"]] = row["talk"]
        d["seg_count"][row["rank"]] = row["seg_count"]
        if row["is_flagged"]:
            d["flagged"].add(row["rank"])

    assignments = []
    for s_id in sorted(by_session):
        d = by_session[s_id]
        decision = _decide(d["present"], d["flagged"], d["spk1"], d["spk2"],
                           d["talk"], d["seg_count"], fallback)
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
    parser.add_argument("--no-fallback", action="store_true",
                        help="Do NOT use the talk-time heuristic for sessions "
                             "with no flags; leave those untouched instead.")
    args = parser.parse_args()

    print("=" * 64)
    print("  Auto-assign speaker roles from active-flag ownership")
    print(f"  AUDIO DB: {AUDIO_DB_PATH}")
    print("=" * 64)

    conn = get_audio_connection()
    try:
        needing_work = conn.execute(NEEDING_WORK_SQL).fetchone()[0]
        assignments = compute_assignments(conn, fallback=not args.no_fallback)

        print(f"  Fallback (talk-time)       : {'off' if args.no_fallback else 'on':>6}")
        print(f"  Sessions needing roles     : {needing_work:>6,}")
        print(f"  Will assign / complete     : {len(assignments):>6,}")
        print(f"  Residual (still unmapped)  : {needing_work - len(assignments):>6,}")

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
