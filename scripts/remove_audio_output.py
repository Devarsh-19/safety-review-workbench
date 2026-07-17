"""
remove_audio_output.py

False-positive cleanup for the AUDIO review DB.

Some audio sessions get FLAGGED solely because of a single standalone abusive /
vulgar token (or the fixed phrase "teri g na tod du") and nothing else. On their
own, at the whole-session level, these are treated as noise: when a session's
ONLY flags are these tokens, the session should be CLEAN.

For every audio session whose active flags are ALL target tokens, this script
dismisses those flags (soft-remove, status='DISMISSED' — the audio convention,
restorable) and recomputes the session verdict, which lands on CLEAN.

Matching rules
--------------
- Case-insensitive ("BC", "Bc", "bc" all match).
- EXACT whole-transcript match after trimming. A token embedded in a longer
  sentence or inside a bigger word is NOT touched — e.g. "tu bc mat bol" and
  "abcd" do not match "bc". This is the "must not mix other words containing
  them as a single sentence" requirement.
- A session is only cleaned when EVERY one of its active (non-dismissed) flags
  is a target token. Sessions that also carry other, genuine flags are left
  completely untouched — including their target-token flags.

DRY-RUN by default. Pass --commit to actually write.

Usage:
  python scripts/remove_audio_output.py            # preview what would change
  python scripts/remove_audio_output.py --commit   # apply
"""

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import (                                    # noqa: E402
    AUDIO_DB_PATH,
    get_audio_connection,
    recompute_audio_session_verdict,
)

# Reviewer marker written into the audit trail for every change this script makes.
REVIEWER_ID = "AUTO_CLEAN_ABUSIVE"

# The exact texts to clean up. Compared case-insensitively, as whole transcripts.
TARGET_TEXTS = [
    "teri g na tod du",
    "tere sath marwani hai",
    "tere g mai toad sakta hu",
    "bc",
    "mc",
    "lanja",
    "chutiya",
    "bsdk",
]

# Punctuation/quotes that may wrap a transcript but are not part of the token.
_STRIP_EDGES = " \t\r\n.,!?;:\"'`~*_-–—…()[]{}"


def normalize(text: str | None) -> str:
    """Lower-case, collapse internal whitespace, strip surrounding punctuation.
    Used identically for the target list and each flag transcript so the
    comparison is an EXACT whole-string match (not a substring search)."""
    if text is None:
        return ""
    t = re.sub(r"\s+", " ", text.strip().lower())
    return t.strip(_STRIP_EDGES)


TARGET_SET = {normalize(t) for t in TARGET_TEXTS}


def is_target(transcript: str | None) -> bool:
    return normalize(transcript) in TARGET_SET


def active_flags(rows: list) -> list:
    """Active flags = amendment rows + original rows that have no amendment
    (same model as store.audio_db._active_audio_flag_rows)."""
    amended_parents = {r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None}
    return [
        r for r in rows
        if r["parent_flag_id"] is not None or r["flag_id"] not in amended_parents
    ]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clean audio sessions whose only flags are standalone abusive tokens."
    )
    parser.add_argument("--commit", action="store_true",
                        help="Apply changes (default is dry-run preview only).")
    args = parser.parse_args()

    print("=" * 64)
    print("  Remove audio output — standalone abusive-token cleanup")
    print(f"  Database : {AUDIO_DB_PATH}")
    print(f"  Targets  : {', '.join(repr(t) for t in sorted(TARGET_SET))}")
    print(f"  Mode     : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print("=" * 64)

    conn = get_audio_connection()
    try:
        rows = conn.execute(
            """SELECT flag_id, s_id, intent, transcript, status, parent_flag_id
               FROM audio_flags"""
        ).fetchall()

        by_session: dict = defaultdict(list)
        for r in rows:
            by_session[r["s_id"]].append(r)

        # Build the worklist: sessions whose every active, non-dismissed flag is
        # a target token.
        worklist = []          # (s_id, [flag_ids to dismiss], [matched texts])
        for s_id, flags in by_session.items():
            live = [
                r for r in active_flags(flags)
                if (r["status"] or "").upper() != "DISMISSED"
            ]
            if not live:
                continue  # already clean at the flag level
            if all(is_target(r["transcript"]) for r in live):
                worklist.append((
                    s_id,
                    [r["flag_id"] for r in live],
                    [r["transcript"] for r in live],
                ))

        if not worklist:
            print("\nNo sessions qualify — nothing to clean.")
            return

        flag_total = sum(len(ids) for _, ids, _ in worklist)
        text_counts = Counter(
            normalize(t) for _, _, texts in worklist for t in texts
        )

        # Current review-status breakdown of the affected sessions.
        s_ids = [s for s, _, _ in worklist]
        placeholders = ",".join("?" * len(s_ids))
        status_rows = conn.execute(
            f"""SELECT s_id, review_status, overall_verdict
                FROM audio_sessions WHERE s_id IN ({placeholders})""",
            s_ids,
        ).fetchall()
        status_counts = Counter(r["review_status"] for r in status_rows)

        print(f"\nSessions to clean : {len(worklist)}")
        print(f"Flags to dismiss  : {flag_total}")
        print("\nMatched token counts:")
        for text, count in text_counts.most_common():
            print(f"    {text!r:<20} x{count}")
        print("\nAffected session review_status:")
        for status, count in status_counts.most_common():
            print(f"    {status or '(none)':<22} {count}")

        if not args.commit:
            print("\nDRY RUN — re-run with --commit to apply.")
            return

        cleaned = 0
        for s_id, flag_ids, _ in worklist:
            for flag_id in flag_ids:
                conn.execute(
                    "UPDATE audio_flags SET status = 'DISMISSED' WHERE flag_id = ?",
                    (flag_id,),
                )
                conn.execute(
                    """INSERT INTO audio_review_log (s_id, flag_id, action, reviewer_id, note)
                       VALUES (?, ?, 'DISMISSED', ?, ?)""",
                    (s_id, flag_id, REVIEWER_ID,
                     "Auto-clean: standalone abusive token (session had no other flags)"),
                )
            # Session now has no active non-dismissed flags -> verdict CLEAN.
            recompute_audio_session_verdict(s_id, conn)
            cleaned += 1
        conn.commit()
        print(f"\nCOMMIT: dismissed {flag_total} flag(s) and marked "
              f"{cleaned} session(s) CLEAN.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
