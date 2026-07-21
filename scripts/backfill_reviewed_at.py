"""
backfill_reviewed_at.py

Backfill the reviewed_at column where it is missing, using the same fallback the
inventory export uses: reviewed_at is left as-is when present, otherwise it is
filled from locked_at, then submitted_at. Stored DATE-only (e.g. '2026-06-19').

  filled reviewed_at = DATE(COALESCE(locked_at, submitted_at))   -- only when
                       reviewed_at is currently NULL/empty

Sessions that already have a reviewed_at keep their existing value untouched.
Sessions with neither locked_at nor submitted_at (e.g. un-actioned PENDING) have
no date to borrow and are left blank — that is intentional, not a gap to fill
(created_at is the ingestion date, not a review, so it is never used).

Covers BOTH databases:
  chat  (store/astrotalk.db)     -> sessions
  audio (store/audio_review.db)  -> audio_sessions
Use --db to run only one. locked_by is NOT modified by this script.

DRY-RUN BY DEFAULT — running with no flags only previews what would change. Pass
--commit to actually write.

Usage:
  python scripts/backfill_reviewed_at.py            # preview (dry-run), both DBs
  python scripts/backfill_reviewed_at.py --commit   # apply
  python scripts/backfill_reviewed_at.py --db audio --commit
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.db import get_connection, DB_PATH  # noqa: E402
from store.audio_db import get_audio_connection, AUDIO_DB_PATH  # noqa: E402

# Rows whose reviewed_at is missing but that have a date to borrow.
_FILLABLE_WHERE = (
    "(reviewed_at IS NULL OR reviewed_at = '') "
    "AND COALESCE(NULLIF(locked_at, ''), NULLIF(submitted_at, '')) IS NOT NULL"
)
# Missing reviewed_at AND no fallback available — left blank.
_UNFILLABLE_WHERE = (
    "(reviewed_at IS NULL OR reviewed_at = '') "
    "AND COALESCE(NULLIF(locked_at, ''), NULLIF(submitted_at, '')) IS NULL"
)


def process_db(label: str, conn, tbl: str, id_col: str, commit: bool) -> int:
    print("=" * 64)
    print(f"  {label}: backfill reviewed_at (date-only) from locked_at -> submitted_at")
    print("=" * 64)

    have = conn.execute(
        f"SELECT COUNT(*) FROM {tbl} WHERE reviewed_at IS NOT NULL AND reviewed_at != ''"
    ).fetchone()[0]
    fillable = conn.execute(f"SELECT COUNT(*) FROM {tbl} WHERE {_FILLABLE_WHERE}").fetchone()[0]
    unfillable = conn.execute(f"SELECT COUNT(*) FROM {tbl} WHERE {_UNFILLABLE_WHERE}").fetchone()[0]

    print(f"  Already have reviewed_at        : {have}")
    print(f"  Missing, will backfill          : {fillable}")
    print(f"  Missing, no fallback (left blank): {unfillable}")

    if fillable:
        print("\n  Sample of rows to backfill:")
        print(f"    {'ID':<14} {'NEW reviewed_at':<16} {'SOURCE':<12}")
        print(f"    {'-'*14} {'-'*16} {'-'*12}")
        for r in conn.execute(
            f"""SELECT {id_col} AS id,
                       DATE(COALESCE(NULLIF(locked_at,''), NULLIF(submitted_at,''))) AS new_rev,
                       CASE WHEN NULLIF(locked_at,'') IS NOT NULL THEN 'locked_at'
                            ELSE 'submitted_at' END AS src
                FROM {tbl} WHERE {_FILLABLE_WHERE}
                ORDER BY {id_col} LIMIT 15"""
        ):
            print(f"    {str(r['id']):<14} {str(r['new_rev']):<16} {r['src']:<12}")

    if not commit:
        print(f"\n  DRY RUN — {fillable} row(s) would be updated. No changes written. "
              f"Re-run with --commit to apply.\n")
        return 0

    cur = conn.execute(
        f"""UPDATE {tbl}
            SET reviewed_at = DATE(COALESCE(NULLIF(locked_at,''), NULLIF(submitted_at,'')))
            WHERE {_FILLABLE_WHERE}"""
    )
    conn.commit()
    print(f"\n  Done. Backfilled reviewed_at on {cur.rowcount} row(s).\n")
    return cur.rowcount


def main() -> None:
    p = argparse.ArgumentParser(
        description="Backfill missing reviewed_at (date-only) from locked_at then "
                    "submitted_at, for the chat and/or audio database."
    )
    p.add_argument("--db", choices=("both", "chat", "audio"), default="both",
                   help="Which database(s) to backfill (default: both).")
    p.add_argument("--commit", action="store_true",
                   help="Actually write the backfill (default is dry-run preview only).")
    args = p.parse_args()

    print(f"  Mode: {'COMMIT' if args.commit else 'DRY-RUN'}\n")

    if args.db in ("both", "chat"):
        conn = get_connection()
        try:
            print(f"  DB: {DB_PATH}")
            process_db("CHAT", conn, "sessions", "session_id", args.commit)
        finally:
            conn.close()

    if args.db in ("both", "audio"):
        conn = get_audio_connection()
        try:
            print(f"  DB: {AUDIO_DB_PATH}")
            process_db("AUDIO", conn, "audio_sessions", "s_id", args.commit)
        finally:
            conn.close()


if __name__ == "__main__":
    main()
