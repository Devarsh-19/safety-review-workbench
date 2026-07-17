"""
dismiss_nsfw_explicit_lowconf_calm_audio.py

For the audio review database (store/audio_review.db): dismiss NSFW_EXPLICIT
flags whose confidence is below a threshold (default 0.85) AND whose segment was
delivered in a neutral / calm / professional tone. A low-confidence explicit
flag on a calm segment is a likely false positive.

Matching (per flag, not per session):
  - intent  : audio_flags.intent = 'NSFW_EXPLICIT' (case-insensitive).
  - confidence : audio_flags.conf < --max-confidence (default 0.85). Flags with a
    NULL conf are excluded (a missing confidence never satisfies "< threshold").
  - tone    : audio_segments.tone in NEUTRAL / CALM / PROFESSIONAL (case-
    insensitive; some legacy rows are lower-case). A flag with no segment
    (seg_id NULL / missing) has no tone and is never dismissed.
  - flag    : only ACTIVE flags are dismissed (already-DISMISSED rows and amended
    originals are excluded).

Dismissal follows the audio script convention (soft dismiss): the flag row is
set to status = 'DISMISSED' (restorable), a 'DISMISSED' row is written to
audio_review_log, and each affected session's verdict is recomputed. A session
left with no active flag becomes overall_verdict = CLEAN. Flags are never
hard-deleted; sessions that still have other active flags stay FLAGGED.

SCOPE: by default only PENDING and LOCKED sessions are cleaned. Override with
--status (e.g. --status ALL).

DRY-RUN BY DEFAULT — pass --commit to actually dismiss.

Usage:
  python scripts/dismiss_nsfw_explicit_lowconf_calm_audio.py            # preview (dry-run)
  python scripts/dismiss_nsfw_explicit_lowconf_calm_audio.py --commit   # apply
  python scripts/dismiss_nsfw_explicit_lowconf_calm_audio.py --max-confidence 0.9 --commit
  python scripts/dismiss_nsfw_explicit_lowconf_calm_audio.py --status ALL --commit
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import (  # noqa: E402
    get_audio_connection,
    recompute_audio_session_verdict,
    AUDIO_DB_PATH,
)

TARGET_INTENT = "NSFW_EXPLICIT"
ALLOWED_TONES = ("NEUTRAL", "CALM", "PROFESSIONAL")
DEFAULT_STATUSES = ("PENDING", "LOCKED")
MAX_CONFIDENCE = 0.85
REVIEWER_ID = "AUTO_DISMISS"
NOTE = "Auto-dismiss: NSFW_EXPLICIT, conf < threshold, neutral/calm/professional tone"


def find_targets(conn, statuses, max_conf):
    """Active NSFW_EXPLICIT flags on neutral/calm/professional segments whose
    confidence is below `max_conf`.

    Returns rows of (flag_id, s_id, tone, conf, review_status). statuses:
    iterable of review_status values to consider (None -> all statuses). Flags
    with a NULL conf are excluded (a missing confidence never satisfies "<").
    """
    params = [TARGET_INTENT, *ALLOWED_TONES]
    if statuses:
        status_clause = "AND s.review_status IN (%s)" % ",".join("?" for _ in statuses)
        params += list(statuses)
    else:
        status_clause = ""
    params.append(max_conf)

    return conn.execute(
        f"""
        SELECT f.flag_id AS flag_id, f.s_id AS s_id,
               seg.tone AS tone, f.conf AS conf, s.review_status AS review_status
        FROM audio_flags f
        JOIN audio_sessions s   ON s.s_id = f.s_id
        JOIN audio_segments seg ON seg.s_id = f.s_id AND seg.seg_id = f.seg_id
        WHERE UPPER(f.intent) = ?
          AND UPPER(seg.tone) IN ({",".join("?" for _ in ALLOWED_TONES)})
          {status_clause}
          AND (f.status IS NULL OR f.status != 'DISMISSED')
          AND f.flag_id NOT IN (
              SELECT parent_flag_id FROM audio_flags WHERE parent_flag_id IS NOT NULL)
          AND f.conf IS NOT NULL AND f.conf < ?
        ORDER BY f.s_id, f.flag_id
        """,
        params,
    ).fetchall()


def run(commit: bool, statuses, max_conf) -> dict:
    """Dismiss matching flags. Returns {'flags': N, 'sessions': M, 'now_clean': C}
    — flag count, affected-session count, and (commit only) how many became CLEAN.
    """
    conn = get_audio_connection()
    try:
        targets = find_targets(conn, statuses, max_conf)
        if not targets:
            print(f"No {TARGET_INTENT} flags (conf < {max_conf}) on neutral/calm/professional "
                  f"segments in scope. Nothing to dismiss.")
            return {"flags": 0, "sessions": 0, "now_clean": 0}

        session_ids = sorted({t["s_id"] for t in targets})
        print(f"Found {len(targets):,} {TARGET_INTENT} flag(s) (conf < {max_conf}) on "
              f"neutral/calm/professional segments across {len(session_ids):,} session(s):\n")
        print(f"  {'SESSION':<10} {'FLAG ID':<10} {'TONE':<14} {'CONF':<7} {'STATUS':<24}")
        print(f"  {'-'*10} {'-'*10} {'-'*14} {'-'*7} {'-'*24}")
        for t in targets:
            print(f"  {t['s_id']:<10} {t['flag_id']:<10} {(t['tone'] or '-'):<14} "
                  f"{t['conf']!s:<7} {t['review_status'] or '-':<24}")
        print()

        status_counts = Counter(t["review_status"] or "-" for t in targets)
        print("  Flags by session review_status:")
        for status, count in sorted(status_counts.items()):
            print(f"    {status:<24} {count:>6,}")
        print()

        if not commit:
            print(f"  Affected sessions: {len(session_ids):,}")
            print(f"DRY RUN - would dismiss {len(targets):,} flag(s) across "
                  f"{len(session_ids):,} session(s) and recompute their verdicts "
                  f"(sessions left with no active flag become CLEAN). No changes "
                  f"written. Re-run with --commit to apply.")
            return {"flags": len(targets), "sessions": len(session_ids), "now_clean": 0}

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

        now_clean = 0
        for s_id in session_ids:
            if recompute_audio_session_verdict(s_id, conn) == "CLEAN":
                now_clean += 1
        conn.commit()

        print(f"Done. Dismissed {len(targets):,} flag(s) across {len(session_ids):,} "
              f"session(s); {now_clean:,} session(s) are now CLEAN "
              f"({len(session_ids) - now_clean:,} still have other active flags).")
        return {"flags": len(targets), "sessions": len(session_ids), "now_clean": now_clean}
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dismiss low-confidence NSFW_EXPLICIT flags on neutral/calm/professional audio segments."
    )
    parser.add_argument("--commit", action="store_true",
                        help="Actually dismiss (default is dry-run preview only).")
    parser.add_argument("--status", default=",".join(DEFAULT_STATUSES),
                        help="Comma-separated review_status values to clean. "
                             "Default: PENDING,LOCKED. Pass 'ALL' to consider every status.")
    parser.add_argument("--max-confidence", type=float, default=MAX_CONFIDENCE,
                        help=f"Only dismiss flags with conf < this value (default: {MAX_CONFIDENCE}).")
    args = parser.parse_args()

    if str(args.status).strip().upper() == "ALL":
        statuses = None
    else:
        statuses = tuple(s.strip().upper() for s in args.status.split(",") if s.strip())

    print("=" * 72)
    print("  Dismiss low-confidence NSFW_EXPLICIT flags on calm-toned audio segments")
    print("=" * 72)
    print(f"  Database   : {AUDIO_DB_PATH}")
    print(f"  Intent     : {TARGET_INTENT}")
    print(f"  Tones      : {', '.join(ALLOWED_TONES)}")
    print(f"  Max conf   : < {args.max_confidence}")
    print(f"  Scope      : {', '.join(statuses) if statuses else 'ALL statuses'}")
    print(f"  Mode       : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    result = run(commit=args.commit, statuses=statuses, max_conf=args.max_confidence)

    print()
    print(f"  TOTAL flags dismissed{'' if args.commit else ' (would)'}: {result['flags']:,}")
    print(f"  TOTAL affected sessions           : {result['sessions']:,}")
    if args.commit:
        print(f"  Sessions now CLEAN                : {result['now_clean']:,}")


if __name__ == "__main__":
    main()
