"""
export_unprocessed_sessions.py

Read-only. Pulls the sessions whose VERDICT is unprocessed straight from the DB
(no input CSV). "Unprocessed" matches the UI/API definition exactly — the
literal verdict string 'UNPROCESSED' (what the API counts as count_unprocessed
and VerdictBadge renders as the grey "Unprocessed" badge). A session starts at
'UNPROCESSED' and moves to CLEAN / FLAGGED / SEVERE once classified.

    Target: overall_verdict = 'UNPROCESSED'

  * with --out   : writes those sessions' full chat in the raw INPUT CSV format
                   (same columns, same order as data/raw/Chat_data.csv:
                   turn_id, session_id, sender, message_text,
                   is_automated_message, sent_at_ist, language_detected,
                   has_link, flagged) — ready to feed straight into
                   ingest_llm_sessions.py / the batch runner.
  * without --out : prints the distinct unprocessed-verdict session count only.
  * with --del    : deletes EXACTLY the sessions written to --out this run
                    (+ their turns / flags / review_log rows AND their ids from
                    logs/checkpoint.json). Requires --out. --del alone is a DRY
                    RUN that only reports the counts; --del --commit actually
                    deletes.

Usage:
  # distinct count only
  python scripts/export_unprocessed_sessions.py

  # export the sessions in input CSV format
  python scripts/export_unprocessed_sessions.py --out exports/unprocessed.csv

  # dry-run delete of exactly what would be exported (nothing written/deleted)
  python scripts/export_unprocessed_sessions.py --out exports/unprocessed.csv --del

  # export, then delete exactly those exported sessions
  python scripts/export_unprocessed_sessions.py --out exports/unprocessed.csv --del --commit
"""

import argparse
import os
import sys
import csv
import importlib.util as _ilu
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.db import get_connection  # noqa: E402

# Load checkpoint helpers directly (same trick as ingest_llm_sessions.py) to
# avoid pipeline/__init__ pulling in heavy deps.
_cp_spec = _ilu.spec_from_file_location(
    "checkpoint",
    Path(__file__).resolve().parents[1] / "pipeline" / "checkpoint.py",
)
_cp = _ilu.module_from_spec(_cp_spec)
_cp_spec.loader.exec_module(_cp)
load_checkpoint = _cp.load_checkpoint
save_checkpoint = _cp.save_checkpoint

# Raw INPUT CSV format — same columns, same order as data/raw/Chat_data.csv,
# so the export round-trips straight back through ingest_llm_sessions.py.
CSV_COLUMNS = [
    "turn_id", "session_id", "sender", "message_text", "is_automated_message",
    "sent_at_ist", "language_detected", "has_link", "flagged",
]


def _fmt_ist(ts: str | None) -> str:
    """Reformat a stored ISO timestamp back to the input's DD-MM-YYYY HH:MM."""
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts).strftime("%d-%m-%Y %H:%M")
    except ValueError:
        return ts

# Subquery defining "verdict is unprocessed".
# Matches the UI/API definition exactly: overall_verdict = 'UNPROCESSED'
# (the literal verdict string the API counts and VerdictBadge renders as the
# grey "Unprocessed" badge). NOT the same as NULL/blank — a session starts at
# 'UNPROCESSED' and moves to CLEAN/FLAGGED/SEVERE once classified.
_TARGET_SUBQUERY = """
    SELECT session_id FROM sessions
    WHERE overall_verdict = 'UNPROCESSED'
"""


# Child tables that reference session_id — deleted before the parent `sessions`
# row.
_CHILD_TABLES = ["turns", "flags", "review_log"]

# Keep IN (...) lists under SQLite's default 999-variable cap.
_SQLITE_MAX_VARS = 900


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _count_by_ids(conn, table: str, ids: list) -> int:
    total = 0
    for chunk in _chunks(ids, _SQLITE_MAX_VARS):
        placeholders = ",".join("?" * len(chunk))
        total += conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE session_id IN ({placeholders})",
            chunk,
        ).fetchone()[0]
    return total


def _delete_by_ids(conn, table: str, ids: list) -> None:
    for chunk in _chunks(ids, _SQLITE_MAX_VARS):
        placeholders = ",".join("?" * len(chunk))
        conn.execute(
            f"DELETE FROM {table} WHERE session_id IN ({placeholders})", chunk
        )


def _delete_targets(conn, commit: bool, exported_ids: set) -> None:
    """Delete (or, without --commit, just count) EXACTLY the sessions written to
    the --out file this run — their child rows across turns / flags / review_log,
    and their ids in the resume checkpoint (logs/checkpoint.json)."""
    ids = sorted(exported_ids)

    if not ids:
        print("\n  Nothing exported - nothing to delete.")
        return

    counts = {t: _count_by_ids(conn, t, ids) for t in _CHILD_TABLES}
    counts["sessions"] = _count_by_ids(conn, "sessions", ids)

    # How many of the exported ids are actually logged in the checkpoint.
    checkpoint_ids = load_checkpoint()
    n_in_checkpoint = len(exported_ids & checkpoint_ids)

    if not commit:
        print("\n  DRY RUN - nothing deleted. Rows that WOULD be deleted "
              f"(for the {len(ids)} exported session(s)):")
        for t in [*_CHILD_TABLES, "sessions"]:
            print(f"    {t:<12}: {counts[t]}")
        print(f"    {'checkpoint':<12}: {n_in_checkpoint}")
        print("\n  Re-run with --del --commit to delete them.")
        return

    # Children first, parent last — all in one transaction.
    for t in _CHILD_TABLES:
        _delete_by_ids(conn, t, ids)
    _delete_by_ids(conn, "sessions", ids)
    conn.commit()

    # Prune the resume checkpoint so re-ingesting these ids isn't skipped.
    if n_in_checkpoint:
        save_checkpoint(checkpoint_ids - exported_ids)

    print("\n  DELETED:")
    for t in [*_CHILD_TABLES, "sessions"]:
        print(f"    {t:<12}: {counts[t]}")
    print(f"    {'checkpoint':<12}: {n_in_checkpoint}")


def run(out_path: Path | None, do_delete: bool = False, commit: bool = False) -> None:
    conn = get_connection()
    try:
        n_target = conn.execute(
            f"SELECT COUNT(*) FROM ({_TARGET_SUBQUERY})"
        ).fetchone()[0]

        print(f"  Sessions with unprocessed verdict: {n_target}")

        if out_path is None and not do_delete:
            # Count-only mode.
            print(f"\n  UNPROCESSED-VERDICT SESSION COUNT: {n_target}")
            return

        # --del deletes exactly what was exported, so an export must run first.
        exported_ids: set = set()
        if n_target == 0:
            print("\n  Nothing to export.")
        else:
            exported_ids = _export(conn, out_path)

        if do_delete:
            _delete_targets(conn, commit, exported_ids)
    finally:
        conn.close()


def _export(conn, out_path: Path) -> set:
    """Write the target sessions to out_path and return the set of session_ids
    actually written (this is the exact set --del operates on)."""
    # Pull every turn for the target sessions, in raw input CSV format.
    # Subquery (not an IN list) keeps us clear of SQLite's variable cap.
    # `flagged` is derived per-turn: 1 if the turn carries any ACTIVE flag.
    rows = conn.execute(
        f"""SELECT t.turn_id, t.session_id, t.speaker, t.message_text,
                   t.is_automated, t.timestamp, t.language_detected, t.has_link,
                   EXISTS(
                       SELECT 1 FROM flags f
                       WHERE f.session_id = t.session_id
                         AND f.turn_id    = t.turn_id
                         AND f.status     = 'ACTIVE'
                   ) AS flagged
            FROM turns t
            WHERE t.session_id IN ({_TARGET_SUBQUERY})
            ORDER BY t.session_id, t.turn_id"""
    ).fetchall()

    exported_ids: set = set()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for t in rows:
            exported_ids.add(t["session_id"])
            writer.writerow({
                "turn_id":              t["turn_id"],
                "session_id":           t["session_id"],
                "sender":               t["speaker"],
                "message_text":         t["message_text"],
                "is_automated_message": t["is_automated"],
                "sent_at_ist":          _fmt_ist(t["timestamp"]),
                "language_detected":    t["language_detected"] if t["language_detected"] is not None else "",
                "has_link":             t["has_link"],
                "flagged":              t["flagged"],
            })

    print(f"  Turns written                    : {len(rows)}")
    print(f"  Sessions written                 : {len(exported_ids)}")
    print(f"  CSV written                      : {out_path}")
    return exported_ids


def main() -> None:
    p = argparse.ArgumentParser(
        description="Export (or count) sessions whose verdict is unprocessed, in input CSV format"
    )
    p.add_argument("--out", default=None,
                   help="Output CSV path. If given, sessions are written in input CSV "
                        "format. If omitted, only the distinct count is printed.")
    p.add_argument("--del", dest="do_delete", action="store_true",
                   help="Delete EXACTLY the sessions written to --out this run (their "
                        "turns / flags / review_log rows AND their ids from "
                        "logs/checkpoint.json). Requires --out. Without --commit this "
                        "is a DRY RUN that only reports the counts.")
    p.add_argument("--commit", action="store_true",
                   help="Actually perform the deletion. Only meaningful with --del.")
    args = p.parse_args()

    # --del deletes exactly the exported sessions, so it requires an --out target.
    if args.do_delete and not args.out:
        p.error("--del requires --out (deletion targets exactly the exported sessions)")

    out_path = Path(args.out) if args.out else None

    if args.do_delete:
        mode = "DELETE (COMMIT)" if args.commit else "DELETE (DRY RUN)"
    elif out_path:
        mode = "EXPORT"
    else:
        mode = "COUNT ONLY"

    print("=" * 60)
    print(f"  Unprocessed-verdict sessions ({mode})")
    print(f"  DB    : {os.getenv('DB_PATH', 'store/results.db')}")
    print("=" * 60)

    run(out_path, do_delete=args.do_delete, commit=args.commit)


if __name__ == "__main__":
    main()
