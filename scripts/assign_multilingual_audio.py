"""
assign_multilingual_audio.py

Move PENDING audio sessions whose language is NOT a Hindi / English / Hinglish
(combination) to a dedicated reviewer bucket named "Multilingual", so
regional-language calls are routed to one reviewer. Operates on the audio review
DB (store/audio_db).

Only PENDING sessions are ever moved — sessions already SUBMITTED_FOR_REVIEW or
LOCKED keep their current reviewer (they are in-review / finalised). Non-pending
sessions that would otherwise qualify are reported but left untouched.

The audio `lang` column is a free-text string set at ingest and can hold a
SINGLE language ("hindi", "tamil") or a COMBINATION ("hindi, english",
"hindi-english", "english / hinglish"). Matching therefore tokenises the value
into its component languages (splitting on any non-letter separator, ignoring
connector words like "and"/"mixed") and treats it as allowed when EVERY component
is allowed. A PENDING session is moved only when:

  * its lang has at least one recognised token, AND
  * at least ONE token is NOT in KEEP_LANGUAGES {hindi, english, hinglish}.

So "hindi, english" and "hindi-hinglish" are KEPT (all components allowed), while
"hindi, tamil" and "telugu" are MOVED (tamil / telugu are not allowed).

Sessions with no recognisable language (NULL / empty / punctuation only) are
NEVER moved — only reported.

If an earlier run mis-parked all-allowed combinations on "Multilingual", the
PENDING ones are reported as "wrongly parked"; pass --repair (with --apply) to
reset those back to unassigned (their original reviewer cannot be recovered, so
they become unassigned for redistribution via assign_audio_sessions.py).

DRY RUN BY DEFAULT — prints what would happen and changes nothing. Pass --apply
to actually reassign and commit.

Usage:
  python scripts/assign_multilingual_audio.py                    # dry run
  python scripts/assign_multilingual_audio.py --apply            # move pending others
  python scripts/assign_multilingual_audio.py --apply --repair   # move + unpark mis-moved
  python scripts/assign_multilingual_audio.py --reviewer Multilingual --apply
"""

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import get_audio_connection, AUDIO_DB_PATH  # noqa: E402

# Allowed component languages. A session's lang may combine any of these in any
# order/separator and still be kept. Compared after tokenisation.
KEEP_LANGUAGES = {"hindi", "english", "hinglish"}

# Connector words that may appear between languages in a compound value; ignored
# when tokenising so "hindi and english" reads as {hindi, english}. Dropping a
# connector can never cause a wrong KEEP — any genuinely-other language token
# still remains and forces a move.
CONNECTORS = {"and", "mix", "mixed", "with"}

# Only sessions in this status are eligible to be moved / repaired. Submitted and
# locked sessions are left with their current reviewer.
MOVABLE_STATUS = "PENDING"

DEFAULT_REVIEWER = "Multilingual"


def language_tokens(lang: str) -> list[str]:
    """Split a free-text lang value into recognised language tokens.

    Lower-cases, splits on any run of non-letters (comma, slash, hyphen, plus,
    ampersand, whitespace, digits …), and drops connector words. Returns [] when
    nothing meaningful remains (NULL / empty / punctuation only)."""
    norm = (lang or "").strip().lower()
    return [t for t in re.split(r"[^a-z]+", norm) if t and t not in CONNECTORS]


def is_all_allowed(lang: str) -> bool:
    """True when the value has ≥1 token and EVERY token is an allowed language."""
    tokens = language_tokens(lang)
    return bool(tokens) and all(t in KEEP_LANGUAGES for t in tokens)


def classify(rows, reviewer: str) -> dict[str, list]:
    """Bucket every session. Keys:

    to_move        : PENDING, lang has a non-allowed token, not already on reviewer.
    skipped_status : non-PENDING, non-allowed token, not on reviewer (would move if
                     pending — left untouched).
    correctly_there: on reviewer, lang has a non-allowed token (leave as is).
    wrongly_parked : PENDING, all-allowed, on reviewer (mis-moved — repairable).
    parked_non_pending: non-PENDING, all-allowed, on reviewer (mis-moved but not
                     PENDING — reported, not repaired).
    kept           : all-allowed, not on reviewer (leave as is).
    no_lang        : no recognisable language token (left untouched, reported).
    """
    b = {k: [] for k in (
        "to_move", "skipped_status", "correctly_there",
        "wrongly_parked", "parked_non_pending", "kept", "no_lang",
    )}
    for r in rows:
        tokens = language_tokens(r["lang"])
        on_reviewer = (r["assigned_to"] or "") == reviewer
        is_pending = (r["review_status"] or "") == MOVABLE_STATUS
        if not tokens:
            b["no_lang"].append(r)
        elif all(t in KEEP_LANGUAGES for t in tokens):        # all components allowed
            if on_reviewer:
                b["wrongly_parked" if is_pending else "parked_non_pending"].append(r)
            else:
                b["kept"].append(r)
        else:                                                 # ≥1 other-language token
            if on_reviewer:
                b["correctly_there"].append(r)
            elif is_pending:
                b["to_move"].append(r)
            else:
                b["skipped_status"].append(r)
    return b


def _print_rows(rows) -> None:
    print(f"    {'S_ID':<14} {'LANG':<18} {'FROM':<14} {'STATUS':<20}")
    print(f"    {'-'*14} {'-'*18} {'-'*14} {'-'*20}")
    for r in rows:
        print(f"    {str(r['s_id']):<14} {str(r['lang']):<18} "
              f"{str(r['assigned_to'] or '—'):<14} {str(r['review_status'] or '—'):<20}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--reviewer", default=DEFAULT_REVIEWER,
                    help=f"Reviewer bucket to move sessions into (default: {DEFAULT_REVIEWER}).")
    ap.add_argument("--apply", action="store_true",
                    help="Actually reassign the sessions and commit. Without it, dry run only.")
    ap.add_argument("--repair", action="store_true",
                    help="Also un-park PENDING all-allowed sessions wrongly moved to the reviewer "
                         "(sets them back to unassigned). Only acts with --apply.")
    args = ap.parse_args()

    conn = get_audio_connection()
    try:
        rows = conn.execute(
            "SELECT s_id, lang, assigned_to, review_status FROM audio_sessions ORDER BY s_id ASC"
        ).fetchall()
        b = classify(rows, args.reviewer)
        to_move = b["to_move"]

        print("=" * 70)
        print("  Move non-Hindi/English/Hinglish PENDING audio sessions -> reviewer bucket")
        print("=" * 70)
        print(f"  Database        : {AUDIO_DB_PATH}")
        print(f"  Target reviewer : {args.reviewer}")
        print(f"  Keep languages  : {', '.join(sorted(KEEP_LANGUAGES))} (any combination)")
        print(f"  Movable status  : {MOVABLE_STATUS} only")
        print()
        print(f"  Total sessions                      : {len(rows)}")
        print(f"  Keep (all-allowed combination)      : {len(b['kept'])}")
        print(f"  No language (left as is)            : {len(b['no_lang'])}")
        print(f"  Other-lang but not PENDING (skipped): {len(b['skipped_status'])}")
        print(f"  Already on '{args.reviewer}' (other lang)  : {len(b['correctly_there'])}")
        print(f"  Wrongly parked, PENDING (repairable): {len(b['wrongly_parked'])}")
        print(f"  Wrongly parked, not PENDING (leave) : {len(b['parked_non_pending'])}")
        print(f"  WOULD MOVE                          : {len(to_move)}")
        print()

        if to_move:
            print("  WOULD MOVE (PENDING, has a non-Hindi/English/Hinglish language):")
            _print_rows(to_move)
            print()
            lang_counts = Counter((r["lang"] or "").strip().lower() for r in to_move)
            print("  Languages routed to the bucket:")
            for lang, count in sorted(lang_counts.items()):
                print(f"    {lang:<22} {count:>6}")
            print()
        else:
            print("  WOULD MOVE: (none)")
            print()

        if b["skipped_status"]:
            print(f"  SKIPPED (other language but not {MOVABLE_STATUS} — left with current reviewer):")
            _print_rows(b["skipped_status"])
            print()

        if b["wrongly_parked"]:
            print(f"  WRONGLY PARKED on '{args.reviewer}' (PENDING, all-allowed — should not be here):")
            _print_rows(b["wrongly_parked"])
            print("  -> --repair set: reset to UNASSIGNED." if args.repair
                  else "  -> pass --repair (with --apply) to reset these to unassigned.")
            print()

        if b["parked_non_pending"]:
            print(f"  NOTE: {len(b['parked_non_pending'])} all-allowed session(s) are parked on "
                  f"'{args.reviewer}' but are not {MOVABLE_STATUS}; left untouched.")
            print()

        if b["no_lang"]:
            print(f"  NOTE: {len(b['no_lang'])} session(s) have no recognisable language and were left "
                  f"untouched (s_id: {', '.join(str(r['s_id']) for r in b['no_lang'][:20])}"
                  f"{' …' if len(b['no_lang']) > 20 else ''}).")
            print()

        if not args.apply:
            print("  DRY RUN - nothing changed. Re-run with --apply to move the above"
                  f"{' (add --repair to also unpark)' if b['wrongly_parked'] else ''}.")
            return

        moved = repaired = 0
        if to_move:
            conn.executemany(
                "UPDATE audio_sessions SET assigned_to = ? WHERE s_id = ? AND review_status = ?",
                [(args.reviewer, r["s_id"], MOVABLE_STATUS) for r in to_move],
            )
            moved = len(to_move)
        if args.repair and b["wrongly_parked"]:
            conn.executemany(
                "UPDATE audio_sessions SET assigned_to = NULL WHERE s_id = ? AND review_status = ?",
                [(r["s_id"], MOVABLE_STATUS) for r in b["wrongly_parked"]],
            )
            repaired = len(b["wrongly_parked"])

        if moved or repaired:
            conn.commit()
            print(f"  MOVED {moved} PENDING audio session(s) to '{args.reviewer}'.")
            if repaired:
                print(f"  UNPARKED {repaired} wrongly-moved PENDING session(s) to unassigned "
                      f"(redistribute with assign_audio_sessions.py).")
        else:
            print("  Nothing to change.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
