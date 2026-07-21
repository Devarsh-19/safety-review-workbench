"""
export_all_ingested_sessions.py

Exports EVERY ingested session (~4 lakh) as ONE ROW PER SESSION with a
clean/flagged indicator. Unlike export_submitted_flags.py this is not limited
to submitted sessions and does not expand to per-turn rows — it is a
session-level inventory of the whole database.

A session counts as flagged if it has at least one ACTIVE flag (amendment if
edited, else original — same active-flag logic as export_submitted_flags.py).

Output columns (one row per session):
    session_id
    session_date
    session_type          - 'chat' or 'voice'
    duration_minutes
    n_turns               - total turns in the session
    review_status         - PENDING / SUBMITTED_FOR_REVIEW / LOCKED / ...
    verdict               - overall_verdict (CLEAN / FLAGGED / SEVERE, blank if unreviewed)
    reviewed_by           - submitted_by, fallback reviewer_id
    astrotalk_flagged     - platform's own flag (0/1)
    is_flagged            - 1 if the session has any active flag, else 0
    n_active_flags        - number of active flags on the session
    flag_categories       - active flag counts per category, e.g. {NSFW:3,CSAM:4}
    flag_sources          - distinct active flag sources (LLM/REGEX/MANUAL), ';'-joined

Read-only — never modifies the database.

Usage:
  python scripts/export_all_ingested_sessions.py
  python scripts/export_all_ingested_sessions.py --out C:/path/to/file.csv
"""

import argparse
import csv
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.db import get_connection  # noqa: E402

CSV_COLUMNS = [
    "session_id", "session_date", "session_type", "duration_minutes", "n_turns",
    "review_status", "verdict", "reviewed_by", "reviewed_at", "locked_by",
    "session_note",
    "astrotalk_flagged",
    "is_flagged", "n_active_flags", "user_flag_count", "astrologer_flag_count",
    "flag_categories", "flag_sources",
]


def _active_flag_summaries(conn, speaker_by_turn: dict) -> dict[str, dict]:
    """Per-session summary of ACTIVE flags (amendment if present, else original).

    Single pass over the whole flags table — no per-session queries. Each active
    flag is also bucketed by the speaker of its turn (user vs astrologer) via
    speaker_by_turn[(session_id, turn_id)]; flags with a NULL/unresolvable turn
    fall into neither bucket, so user_n + astro_n <= n.
    """
    rows = conn.execute(
        """SELECT flag_id, parent_flag_id, session_id, turn_id, source,
                  detection_layer, category_code
           FROM flags"""
    ).fetchall()

    amended_parents = {r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None}

    summaries: dict[str, dict] = {}
    for r in rows:
        is_active = (
            r["parent_flag_id"] is not None
            or r["flag_id"] not in amended_parents
        )
        if not is_active:
            continue
        s = summaries.setdefault(
            r["session_id"],
            {"n": 0, "sources": set(), "categories": {}, "user_n": 0, "astro_n": 0},
        )
        s["n"] += 1
        src = r["source"] or r["detection_layer"]
        if src:
            s["sources"].add(src)
        cat = r["category_code"] or "UNKNOWN"
        s["categories"][cat] = s["categories"].get(cat, 0) + 1
        speaker = speaker_by_turn.get((r["session_id"], r["turn_id"]))
        if speaker == "USER":
            s["user_n"] += 1
        elif speaker == "ASTROLOGER":
            s["astro_n"] += 1
    return summaries


def _format_categories(categories: dict[str, int]) -> str:
    """Render category counts as a compact dict string, e.g. {NSFW:3,CSAM:4}."""
    if not categories:
        return ""
    return "{" + ",".join(f"{k}:{v}" for k, v in sorted(categories.items())) + "}"


def export(out_path: Path) -> None:
    conn = get_connection()

    print("  Loading turn counts...")
    n_turns_by_session = {
        r["session_id"]: r["n"]
        for r in conn.execute(
            "SELECT session_id, COUNT(*) AS n FROM turns GROUP BY session_id"
        ).fetchall()
    }

    print("  Loading turn speakers...")
    speaker_by_turn = {
        (r["session_id"], r["turn_id"]): r["speaker"]
        for r in conn.execute("SELECT session_id, turn_id, speaker FROM turns").fetchall()
    }

    print("  Loading active flags...")
    flag_summary = _active_flag_summaries(conn, speaker_by_turn)

    print("  Exporting sessions...")
    n_sessions = 0
    n_flagged = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()

        for s in conn.execute(
            """SELECT session_id, session_date, session_type, duration_minutes,
                      review_status, overall_verdict, submitted_by, reviewer_id,
                      DATE(COALESCE(reviewed_at, locked_at, submitted_at)) AS reviewed_at,
                      locked_by, session_note,
                      astrotalk_flagged
               FROM sessions ORDER BY session_id"""
        ):
            n_sessions += 1
            fs = flag_summary.get(s["session_id"])
            if fs:
                n_flagged += 1
            writer.writerow({
                "session_id":              s["session_id"],
                "session_date":            s["session_date"],
                "session_type":            s["session_type"],
                "duration_minutes":        s["duration_minutes"],
                "n_turns":                 n_turns_by_session.get(s["session_id"], 0),
                "review_status":           s["review_status"] or "",
                "verdict":                 s["overall_verdict"] or "",
                "reviewed_by":             s["submitted_by"] or s["reviewer_id"] or "",
                "reviewed_at":             s["reviewed_at"] or "",
                "locked_by":               s["locked_by"] or "",
                "session_note":            s["session_note"] or "",
                "astrotalk_flagged":       s["astrotalk_flagged"],
                "is_flagged":              1 if fs else 0,
                "n_active_flags":          fs["n"] if fs else 0,
                "user_flag_count":         fs["user_n"] if fs else 0,
                "astrologer_flag_count":   fs["astro_n"] if fs else 0,
                "flag_categories":         _format_categories(fs["categories"]) if fs else "",
                "flag_sources":            ";".join(sorted(fs["sources"])) if fs else "",
            })

    conn.close()
    print(f"  Sessions exported  : {n_sessions}")
    print(f"  Flagged (>=1 flag) : {n_flagged}")
    print(f"  Clean (no flags)   : {n_sessions - n_flagged}")
    print(f"  CSV written        : {out_path}")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Export ALL ingested sessions (one row per session) with clean/flagged status"
    )
    p.add_argument("--out", default=None, help="Output CSV path")
    args = p.parse_args()

    if args.out:
        out_path = Path(args.out)
    else:
        stamp = datetime.now().strftime("%Y%m%d")
        out_path = Path(__file__).resolve().parents[1] / "exports" / f"all_ingested_sessions_{stamp}.csv"

    print("=" * 60)
    print("  Export ALL ingested sessions (clean vs flagged)")
    print(f"  DB: {os.getenv('DB_PATH', 'store/results.db')}")
    print("=" * 60)
    export(out_path)


if __name__ == "__main__":
    main()
