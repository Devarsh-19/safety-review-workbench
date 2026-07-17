"""
dismiss_low_confidence_flags.py

Dismiss low-confidence flags from both review stores:

  * chat DB  — store/astrotalk.db, flags.confidence_score
  * audio DB — store/audio_review.db, audio_flags.conf

A flag qualifies when it is the active version of a flag (amendment row if
present, otherwise the original), is not already confirmed/dismissed, has a
non-NULL confidence value, and that value is below the configured threshold
(default: 0.8).

Dismissal is soft for both stores: the active row is set to
status='DISMISSED', a review-log row is written, then the session verdict is
recomputed while ignoring dismissed flags.

DRY-RUN BY DEFAULT — running with no flags only previews what would change.
Pass --commit to actually dismiss. The default scope is PENDING,
SUBMITTED_FOR_REVIEW, and LOCKED sessions.

Usage:
  python scripts/dismiss_low_confidence_flags.py
  python scripts/dismiss_low_confidence_flags.py --chat-only
  python scripts/dismiss_low_confidence_flags.py --audio-only
  python scripts/dismiss_low_confidence_flags.py --threshold 0.75
  python scripts/dismiss_low_confidence_flags.py --status ALL --commit
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import (  # noqa: E402
    AUDIO_DB_PATH,
    get_audio_connection,
    recompute_audio_session_verdict,
)
from store.db import DB_PATH, get_connection  # noqa: E402
from engine.verdict_rules import (  # noqa: E402
    get_db_confidence_for_verdict,
    get_db_verdict_for_flags,
)


DEFAULT_STATUSES = ("PENDING", "SUBMITTED_FOR_REVIEW", "LOCKED")
DEFAULT_THRESHOLD = 0.8
REVIEWER_ID = "AUTO_DISMISS"
NOTE = "Auto-dismiss: confidence score below threshold"
SAMPLE_LIMIT = 25


def parse_statuses(raw: str):
    if str(raw).strip().upper() == "ALL":
        return None
    return tuple(s.strip().upper() for s in raw.split(",") if s.strip())


def find_chat_targets(conn, statuses, threshold: float):
    params = [threshold]
    if statuses:
        status_clause = "AND s.review_status IN (%s)" % ",".join("?" for _ in statuses)
        params.extend(statuses)
    else:
        status_clause = ""

    return conn.execute(
        f"""
        SELECT f.flag_id, f.session_id, f.parent_flag_id, f.category_code,
               f.confidence_score AS confidence, f.status, s.review_status
        FROM flags f
        JOIN sessions s ON s.session_id = f.session_id
        WHERE f.confidence_score IS NOT NULL
          AND f.confidence_score < ?
          {status_clause}
          AND (f.status IS NULL OR f.status NOT IN ('CONFIRMED', 'DISMISSED'))
          AND (
              f.parent_flag_id IS NOT NULL
              OR f.flag_id NOT IN (
                  SELECT parent_flag_id FROM flags WHERE parent_flag_id IS NOT NULL
              )
          )
        ORDER BY f.session_id, f.flag_id
        """,
        params,
    ).fetchall()


def count_chat_sessions_becoming_clean(conn, session_ids, dismiss_flag_ids: set[int]) -> int:
    clean = 0
    for session_id in session_ids:
        rows = conn.execute(
            """SELECT flag_id, category_code, status, parent_flag_id
               FROM flags WHERE session_id = ?""",
            (session_id,),
        ).fetchall()
        amended_parents = {
            r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None
        }
        active_codes_after = [
            r["category_code"] for r in rows
            if (r["status"] or "") != "DISMISSED"
            and r["flag_id"] not in dismiss_flag_ids
            and (
                r["parent_flag_id"] is not None
                or r["flag_id"] not in amended_parents
            )
        ]
        if get_db_verdict_for_flags(active_codes_after) == "CLEAN":
            clean += 1
    return clean


def recompute_chat_session_verdict(session_id: str, conn) -> str:
    rows = conn.execute(
        """SELECT flag_id, category_code, status, parent_flag_id
           FROM flags WHERE session_id = ?""",
        (session_id,),
    ).fetchall()
    amended_parents = {r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None}
    active_codes = [
        r["category_code"] for r in rows
        if (r["status"] or "") != "DISMISSED"
        and (
            r["parent_flag_id"] is not None
            or r["flag_id"] not in amended_parents
        )
    ]
    verdict = get_db_verdict_for_flags(active_codes)
    confidence = get_db_confidence_for_verdict(verdict)
    conn.execute(
        "UPDATE sessions SET overall_verdict = ?, confidence_score = ? WHERE session_id = ?",
        (verdict, confidence, session_id),
    )
    return verdict


def find_audio_targets(conn, statuses, threshold: float):
    params = [threshold]
    if statuses:
        status_clause = "AND s.review_status IN (%s)" % ",".join("?" for _ in statuses)
        params.extend(statuses)
    else:
        status_clause = ""

    return conn.execute(
        f"""
        SELECT f.flag_id, f.s_id, f.parent_flag_id, f.intent,
               f.conf AS confidence, f.status, s.review_status
        FROM audio_flags f
        JOIN audio_sessions s ON s.s_id = f.s_id
        WHERE f.conf IS NOT NULL
          AND f.conf < ?
          {status_clause}
          AND (f.status IS NULL OR f.status NOT IN ('CONFIRMED', 'DISMISSED'))
          AND (
              f.parent_flag_id IS NOT NULL
              OR f.flag_id NOT IN (
                  SELECT parent_flag_id FROM audio_flags WHERE parent_flag_id IS NOT NULL
              )
          )
        ORDER BY f.s_id, f.flag_id
        """,
        params,
    ).fetchall()


def count_audio_sessions_becoming_clean(conn, session_ids, dismiss_flag_ids: set[int]) -> int:
    clean = 0
    for s_id in session_ids:
        rows = conn.execute(
            "SELECT flag_id, status, parent_flag_id FROM audio_flags WHERE s_id = ?",
            (s_id,),
        ).fetchall()
        amended_parents = {
            r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None
        }
        has_active_after = any(
            (r["status"] or "") != "DISMISSED"
            and r["flag_id"] not in dismiss_flag_ids
            and (
                r["parent_flag_id"] is not None
                or r["flag_id"] not in amended_parents
            )
            for r in rows
        )
        if not has_active_after:
            clean += 1
    return clean


def print_targets(label: str, targets, id_col: str, type_col: str) -> None:
    if not targets:
        print(f"{label}: no qualifying flags.")
        return

    session_ids = {t[id_col] for t in targets}
    print(f"{label}: {len(targets):,} flag(s) across {len(session_ids):,} session(s)")
    print(f"  {'SESSION':<18} {'FLAG ID':<10} {'TYPE':<30} {'CONF':<8} {'STATUS':<24}")
    print(f"  {'-'*18} {'-'*10} {'-'*30} {'-'*8} {'-'*24}")
    for t in targets[:SAMPLE_LIMIT]:
        print(
            f"  {str(t[id_col]):<18} {t['flag_id']:<10} "
            f"{str(t[type_col] or '-')[:30]:<30} {t['confidence']!s:<8} "
            f"{t['review_status'] or '-':<24}"
        )
    if len(targets) > SAMPLE_LIMIT:
        print(f"  ... {len(targets) - SAMPLE_LIMIT:,} more")
    print()

    status_counts = Counter(t["review_status"] or "-" for t in targets)
    print("  Flags by review_status:")
    for status, count in sorted(status_counts.items()):
        print(f"    {status:<24} {count:>6,}")
    print()


def run_chat(commit: bool, statuses, threshold: float) -> tuple[int, int, int]:
    conn = get_connection()
    try:
        targets = find_chat_targets(conn, statuses, threshold)
        print_targets("Chat DB", targets, "session_id", "category_code")
        touched_sessions = sorted({t["session_id"] for t in targets})
        clean_sessions = count_chat_sessions_becoming_clean(
            conn,
            touched_sessions,
            {t["flag_id"] for t in targets},
        )
        if not targets or not commit:
            return len(targets), len(touched_sessions), clean_sessions

        with conn:
            for t in targets:
                conn.execute(
                    """INSERT INTO review_log (session_id, flag_id, action, reviewer_id, note)
                       VALUES (?, ?, 'DISMISSED', ?, ?)""",
                    (t["session_id"], t["flag_id"], REVIEWER_ID, NOTE),
                )
                conn.execute(
                    "UPDATE flags SET status = 'DISMISSED' WHERE flag_id = ?",
                    (t["flag_id"],),
                )
            for session_id in touched_sessions:
                recompute_chat_session_verdict(session_id, conn)
        return len(targets), len(touched_sessions), clean_sessions
    finally:
        conn.close()


def run_audio(commit: bool, statuses, threshold: float) -> tuple[int, int, int]:
    conn = get_audio_connection()
    try:
        targets = find_audio_targets(conn, statuses, threshold)
        print_targets("Audio DB", targets, "s_id", "intent")
        touched_sessions = sorted({t["s_id"] for t in targets})
        clean_sessions = count_audio_sessions_becoming_clean(
            conn,
            touched_sessions,
            {t["flag_id"] for t in targets},
        )
        if not targets or not commit:
            return len(targets), len(touched_sessions), clean_sessions

        with conn:
            for t in targets:
                conn.execute(
                    "UPDATE audio_flags SET status = 'DISMISSED' WHERE flag_id = ?",
                    (t["flag_id"],),
                )
                conn.execute(
                    """INSERT INTO audio_review_log (s_id, flag_id, action, reviewer_id, note)
                       VALUES (?, ?, 'DISMISSED', ?, ?)""",
                    (t["s_id"], t["flag_id"], REVIEWER_ID, NOTE),
                )
            for s_id in touched_sessions:
                recompute_audio_session_verdict(s_id, conn)
        return len(targets), len(touched_sessions), clean_sessions
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dismiss active chat/audio flags with confidence below threshold."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--chat-only", action="store_true", help="Only process chat flags.")
    group.add_argument("--audio-only", action="store_true", help="Only process audio flags.")
    parser.add_argument("--commit", action="store_true",
                        help="Actually dismiss (default is dry-run preview only).")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help=f"Dismiss flags with confidence below this value "
                             f"(default: {DEFAULT_THRESHOLD}).")
    parser.add_argument("--status", default=",".join(DEFAULT_STATUSES),
                        help="Comma-separated review_status values to clean. "
                             "Default: PENDING,SUBMITTED_FOR_REVIEW,LOCKED. "
                             "Pass 'ALL' to consider every status.")
    args = parser.parse_args()

    statuses = parse_statuses(args.status)
    process_chat = not args.audio_only
    process_audio = not args.chat_only

    print("=" * 72)
    print("  Dismiss low-confidence flags")
    print("=" * 72)
    print(f"  Chat DB    : {DB_PATH}")
    print(f"  Audio DB   : {AUDIO_DB_PATH}")
    print(f"  Threshold  : confidence < {args.threshold}")
    print(f"  Scope      : {', '.join(statuses) if statuses else 'ALL statuses'}")
    print(f"  Mode       : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    chat_count, chat_sessions, chat_clean = (
        run_chat(args.commit, statuses, args.threshold) if process_chat else (0, 0, 0)
    )
    audio_count, audio_sessions, audio_clean = (
        run_audio(args.commit, statuses, args.threshold) if process_audio else (0, 0, 0)
    )

    total = chat_count + audio_count
    total_sessions = chat_sessions + audio_sessions
    total_clean = chat_clean + audio_clean
    if args.commit:
        print(f"Done. Dismissed {chat_count:,} chat flag(s) and "
              f"{audio_count:,} audio flag(s), affecting {total_sessions:,} "
              f"session(s) ({chat_sessions:,} chat, {audio_sessions:,} audio). "
              f"{total_clean:,} session(s) are now CLEAN "
              f"({chat_clean:,} chat, {audio_clean:,} audio).")
    else:
        print(f"DRY RUN — would dismiss {total:,} flag(s) "
              f"({chat_count:,} chat, {audio_count:,} audio), affecting "
              f"{total_sessions:,} session(s) ({chat_sessions:,} chat, "
              f"{audio_sessions:,} audio). {total_clean:,} session(s) would become "
              f"CLEAN ({chat_clean:,} chat, {audio_clean:,} audio). No changes "
              f"written. Re-run with --commit to apply.")


if __name__ == "__main__":
    main()
