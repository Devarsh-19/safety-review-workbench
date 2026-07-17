"""
dismiss_calm_professional_flags_audio.py

For the audio review database (store/audio_review.db): dismiss flags that sit on
a CALM or PROFESSIONAL-toned segment, but only in sessions the platform itself
judged clean (astrotalk_verdict = 'CLEAN'), then mark those sessions clean.

Rationale: a flag whose segment tone is calm/professional AND that AstroTalk did
not flag is a likely false positive, so it is dismissed.

Matching:
  - tone: audio_segments.tone in (CALM, PROFESSIONAL), case-insensitive. The
    canonical tone vocabulary (see test_gemini_multi/audio_prompts.py) is
    NEUTRAL / CALM / PROFESSIONAL / DISTRESSED / ANGRY / AGGRESSIVE /
    FLIRTATIOUS / UNCLEAR; some legacy rows are lower-case.
  - confidence: audio_flags.conf >= 0.8 (configurable via --min-confidence).
    Flags with a NULL conf are excluded (a missing confidence never satisfies >=).
  - astrotalk: audio_sessions.astrotalk_verdict = 'CLEAN' (legacy SEVERE counts
    as FLAGGED, so only an explicit CLEAN qualifies).
  - flag: only ACTIVE flags are dismissed (already-DISMISSED rows and amended
    originals are excluded). A flag with no segment (seg_id NULL / missing) has
    no tone, so it is never dismissed.

Dismissal follows the audio script convention (soft dismiss): the flag row is
set to status = 'DISMISSED' (restorable), a 'DISMISSED' row is written to
audio_review_log, and the session verdict is recomputed. A session left with no
active flag becomes overall_verdict = CLEAN. Flags are never hard-deleted. A
session that still has flags on other (non-calm) segments stays FLAGGED — only
the calm/professional flags are dismissed.

SCOPE: by default only PENDING and LOCKED sessions are cleaned. Override with
--status (e.g. --status ALL).

DRY-RUN BY DEFAULT — pass --commit to actually dismiss.

Usage:
  python scripts/dismiss_calm_professional_flags_audio.py            # preview (dry-run)
  python scripts/dismiss_calm_professional_flags_audio.py --commit   # apply
  python scripts/dismiss_calm_professional_flags_audio.py --status ALL --commit
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

CALM_PROFESSIONAL_TONES = ("CALM", "PROFESSIONAL")
DEFAULT_STATUSES = ("PENDING", "LOCKED")
MIN_CONFIDENCE = 0.8
REVIEWER_ID = "AUTO_DISMISS"
NOTE = "Auto-dismiss: calm/professional tone, conf >= threshold, AstroTalk verdict CLEAN"


def find_targets(conn, statuses, min_conf):
    """Active flags on CALM/PROFESSIONAL segments in AstroTalk-clean sessions
    whose confidence is at least `min_conf`.

    Returns rows of (flag_id, s_id, tone, conf, review_status). statuses:
    iterable of review_status values to consider (None -> all statuses). Flags
    with a NULL conf are excluded (a missing confidence never satisfies >=).
    """
    params = list(CALM_PROFESSIONAL_TONES)
    if statuses:
        status_clause = "AND s.review_status IN (%s)" % ",".join("?" for _ in statuses)
        params += list(statuses)
    else:
        status_clause = ""
    params.append(min_conf)

    return conn.execute(
        f"""
        SELECT f.flag_id AS flag_id, f.s_id AS s_id,
               seg.tone AS tone, f.conf AS conf, s.review_status AS review_status
        FROM audio_flags f
        JOIN audio_sessions s  ON s.s_id = f.s_id
        JOIN audio_segments seg ON seg.s_id = f.s_id AND seg.seg_id = f.seg_id
        WHERE UPPER(s.astrotalk_verdict) = 'CLEAN'
          AND UPPER(seg.tone) IN ({",".join("?" for _ in CALM_PROFESSIONAL_TONES)})
          {status_clause}
          AND (f.status IS NULL OR f.status != 'DISMISSED')
          AND f.flag_id NOT IN (
              SELECT parent_flag_id FROM audio_flags WHERE parent_flag_id IS NOT NULL)
          AND f.conf IS NOT NULL AND f.conf >= ?
        ORDER BY f.s_id, f.flag_id
        """,
        params,
    ).fetchall()


def run(commit: bool, statuses, min_conf) -> int:
    conn = get_audio_connection()
    try:
        targets = find_targets(conn, statuses, min_conf)
        if not targets:
            print(f"No calm/professional flags (conf >= {min_conf}) in AstroTalk-clean "
                  f"sessions in scope. Nothing to dismiss.")
            return 0

        session_ids = sorted({t["s_id"] for t in targets})
        print(f"Found {len(targets):,} flag(s) on calm/professional segments (conf >= "
              f"{min_conf}) across {len(session_ids):,} AstroTalk-clean session(s):\n")
        print(f"  {'SESSION':<10} {'FLAG ID':<10} {'TONE':<14} {'CONF':<7} {'STATUS':<24}")
        print(f"  {'-'*10} {'-'*10} {'-'*14} {'-'*7} {'-'*24}")
        for t in targets:
            print(f"  {t['s_id']:<10} {t['flag_id']:<10} {(t['tone'] or '—'):<14} "
                  f"{t['conf']!s:<7} {t['review_status'] or '—':<24}")
        print()

        status_counts = Counter(t["review_status"] or "—" for t in targets)
        print("  Flags by session review_status:")
        for status, count in sorted(status_counts.items()):
            print(f"    {status:<24} {count:>6,}")
        print()

        if not commit:
            print(f"DRY RUN — would dismiss {len(targets):,} flag(s) across "
                  f"{len(session_ids):,} session(s) and recompute their verdicts "
                  f"(sessions left with no active flag become CLEAN). No changes "
                  f"written. Re-run with --commit to apply.")
            return len(targets)


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
              f"({len(session_ids) - now_clean:,} still have flags on other segments).")
        return len(targets)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dismiss calm/professional-tone flags in AstroTalk-clean audio sessions and mark them clean."
    )
    parser.add_argument("--commit", action="store_true",
                        help="Actually dismiss (default is dry-run preview only).")
    parser.add_argument("--status", default=",".join(DEFAULT_STATUSES),
                        help="Comma-separated review_status values to clean. "
                             "Default: PENDING,LOCKED. Pass 'ALL' to consider every status.")
    parser.add_argument("--min-confidence", type=float, default=MIN_CONFIDENCE,
                        help=f"Only dismiss flags with conf >= this value (default: {MIN_CONFIDENCE}).")
    args = parser.parse_args()

    if str(args.status).strip().upper() == "ALL":
        statuses = None
    else:
        statuses = tuple(s.strip().upper() for s in args.status.split(",") if s.strip())

    print("=" * 68)
    print("  Dismiss calm/professional-tone flags in AstroTalk-clean audio sessions")
    print("=" * 68)
    print(f"  Database   : {AUDIO_DB_PATH}")
    print(f"  Tones      : {', '.join(CALM_PROFESSIONAL_TONES)}")
    print(f"  Min conf   : {args.min_confidence}")
    print(f"  Scope      : {', '.join(statuses) if statuses else 'ALL statuses'}")
    print(f"  Mode       : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    run(commit=args.commit, statuses=statuses, min_conf=args.min_confidence)


if __name__ == "__main__":
    main()
