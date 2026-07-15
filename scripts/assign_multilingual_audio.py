"""
assign_multilingual_audio.py

Move every audio session whose language is NOT Hindi, English or Hinglish to a
dedicated reviewer bucket named "Multilingual", so regional-language calls are
routed to one reviewer. Operates on the audio review DB (store/audio_db).

The audio `lang` column is a free-text string set at ingest (e.g. "hindi",
"tamil"). Matching is case-insensitive and whitespace-trimmed. A session is moved
when:

  * its lang is set (not NULL / not empty), AND
  * normalise(lang) is NOT in KEEP_LANGUAGES {hindi, english, hinglish}.

Sessions already assigned to "Multilingual" are counted as already-correct and
left as is. Sessions with no language are NEVER moved (we can't confirm they are
"other") — they are only reported. Moving overwrites the current assignee
regardless of review_status; a per-status breakdown is printed so you can see
what is affected.

DRY RUN BY DEFAULT — prints what would happen and changes nothing. Pass --apply
to actually reassign and commit.

Usage:
  python scripts/assign_multilingual_audio.py                 # dry run
  python scripts/assign_multilingual_audio.py --apply         # perform the move
  python scripts/assign_multilingual_audio.py --reviewer Multilingual --apply
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import get_audio_connection, AUDIO_DB_PATH  # noqa: E402

# Languages that stay with their current reviewer. Compared after normalise().
KEEP_LANGUAGES = {"hindi", "english", "hinglish"}

DEFAULT_REVIEWER = "Multilingual"


def normalise(lang: str) -> str:
    return (lang or "").strip().lower()


def classify(rows, reviewer: str):
    """Split all sessions into (to_move, already_there, kept, no_lang).

    to_move      : rows whose lang is set and not in KEEP_LANGUAGES, and are not
                   already assigned to `reviewer`.
    already_there: matching rows already assigned to `reviewer`.
    kept         : rows whose lang IS in KEEP_LANGUAGES.
    no_lang      : rows with NULL / empty lang (left untouched, reported only).
    """
    to_move, already_there, kept, no_lang = [], [], [], []
    for r in rows:
        norm = normalise(r["lang"])
        if not norm:
            no_lang.append(r)
        elif norm in KEEP_LANGUAGES:
            kept.append(r)
        elif (r["assigned_to"] or "") == reviewer:
            already_there.append(r)
        else:
            to_move.append(r)
    return to_move, already_there, kept, no_lang


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--reviewer", default=DEFAULT_REVIEWER,
                    help=f"Reviewer bucket to move sessions into (default: {DEFAULT_REVIEWER}).")
    ap.add_argument("--apply", action="store_true",
                    help="Actually reassign the sessions and commit. Without it, dry run only.")
    args = ap.parse_args()

    conn = get_audio_connection()
    try:
        rows = conn.execute(
            "SELECT s_id, lang, assigned_to, review_status FROM audio_sessions ORDER BY s_id ASC"
        ).fetchall()
        to_move, already_there, kept, no_lang = classify(rows, args.reviewer)

        print("=" * 70)
        print("  Move non-Hindi/English/Hinglish audio sessions -> reviewer bucket")
        print("=" * 70)
        print(f"  Database        : {AUDIO_DB_PATH}")
        print(f"  Target reviewer : {args.reviewer}")
        print(f"  Keep languages  : {', '.join(sorted(KEEP_LANGUAGES))}")
        print()
        print(f"  Total sessions            : {len(rows)}")
        print(f"  Keep (Hindi/Eng/Hinglish) : {len(kept)}")
        print(f"  No language (left as is)  : {len(no_lang)}")
        print(f"  Already '{args.reviewer}'      : {len(already_there)}")
        print(f"  WOULD MOVE                : {len(to_move)}")
        print()

        if to_move:
            print("  WOULD MOVE:")
            print(f"    {'S_ID':<14} {'LANG':<16} {'FROM':<14} {'STATUS':<20}")
            print(f"    {'-'*14} {'-'*16} {'-'*14} {'-'*20}")
            for r in to_move:
                print(f"    {str(r['s_id']):<14} {str(r['lang']):<16} "
                      f"{str(r['assigned_to'] or '—'):<14} {str(r['review_status'] or '—'):<20}")
            print()
            # Per-status breakdown so it's clear if locked/submitted are affected.
            status_counts = Counter(str(r["review_status"] or "—") for r in to_move)
            print("  Breakdown of moved sessions by review_status:")
            for status, count in sorted(status_counts.items()):
                print(f"    {status:<22} {count:>6}")
            print()
            # Which languages are being routed.
            lang_counts = Counter(normalise(r["lang"]) for r in to_move)
            print("  Languages routed to the bucket:")
            for lang, count in sorted(lang_counts.items()):
                print(f"    {lang:<22} {count:>6}")
            print()
        else:
            print("  WOULD MOVE: (none)")
            print()

        if no_lang:
            print(f"  NOTE: {len(no_lang)} session(s) have no language and were left "
                  f"untouched (s_id: {', '.join(str(r['s_id']) for r in no_lang[:20])}"
                  f"{' …' if len(no_lang) > 20 else ''}).")
            print()

        if not args.apply:
            print("  DRY RUN - nothing changed. Re-run with --apply to move the above.")
            return

        if not to_move:
            print("  Nothing to move.")
            return

        # Per-row UPDATE via executemany (avoids SQLite's bound-variable limit on
        # a large `WHERE s_id IN (...)`; same pattern as the other bulk scripts).
        conn.executemany(
            "UPDATE audio_sessions SET assigned_to = ? WHERE s_id = ?",
            [(args.reviewer, r["s_id"]) for r in to_move],
        )
        conn.commit()
        print(f"  MOVED {len(to_move)} audio session(s) to '{args.reviewer}'.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
