"""
export_submitted_flags.py

Exports the FULL chat (every turn, flagged and non-flagged) for sessions whose
review_status = 'SUBMITTED_FOR_REVIEW'. One row per turn; if a turn has an active
flag, the flag columns are filled in (a turn with multiple flags yields multiple
rows). Non-flagged turns appear with blank severity / flag_type.

Only the active version of each flag is used (amendment if edited, else original).

Output columns (one row per turn / turn-flag):
    session_id
    verdict          - the session's overall_verdict (CLEAN / FLAGGED / SEVERE)
    reviewed_by      - the reviewer who submitted the session for review
    astrotalk_flagged - the session's astrotalk_flagged value (platform's own flag)
    turn_id
    turn_text        - the message_text of that turn
    severity         - the flag's severity (blank if turn not flagged)
    flag_type        - the flag's category_code (blank if turn not flagged)
    detection_layer  - the flag's source: LLM / REGEX / MANUAL (blank if not flagged)
    status           - the flag's status: ACTIVE / CONFIRMED (blank if not flagged)
    speaker          - who sent the message (ASTROLOGER / USER)
    is_automated_message - 1 if the turn was an automated message, else 0

Read-only — never modifies the database.

Usage:
  python scripts/export_submitted_flags.py
  python scripts/export_submitted_flags.py --out C:/path/to/file.csv
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

CSV_COLUMNS = ["session_id", "verdict", "reviewed_by", "astrotalk_flagged", "turn_id",
               "turn_text", "severity", "flag_type", "detection_layer", "status",
               "speaker", "is_automated_message"]


def _active_flag_ids(conn, session_ids: set[str]) -> set[int]:
    """Active version of each flag: amendment if present, else original."""
    if not session_ids:
        return set()
    placeholders = ",".join("?" * len(session_ids))
    rows = conn.execute(
        f"SELECT flag_id, parent_flag_id FROM flags WHERE session_id IN ({placeholders})",
        tuple(session_ids),
    ).fetchall()
    amended_parents = {r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None}
    active = set()
    for r in rows:
        if r["parent_flag_id"] is not None:
            active.add(r["flag_id"])
        elif r["flag_id"] not in amended_parents:
            active.add(r["flag_id"])
    return active


def export(out_path: Path) -> None:
    conn = get_connection()

    # 1) Submitted + locked sessions + verdict + who reviewed (submitted_by, fallback reviewer_id)
    #    UNPROCESSED sessions are excluded — only properly reviewed verdicts.
    sessions = conn.execute(
        """SELECT session_id, overall_verdict, submitted_by, reviewer_id, astrotalk_flagged
           FROM sessions
           WHERE review_status IN ('SUBMITTED_FOR_REVIEW', 'LOCKED')
             AND overall_verdict IS NOT NULL
             AND overall_verdict != 'UNPROCESSED'
           ORDER BY session_id"""
    ).fetchall()
    verdict_by_session = {s["session_id"]: s["overall_verdict"] for s in sessions}
    reviewer_by_session = {
        s["session_id"]: (s["submitted_by"] or s["reviewer_id"] or "")
        for s in sessions
    }
    astrotalk_flagged_by_session = {s["session_id"]: s["astrotalk_flagged"] for s in sessions}
    session_ids = set(verdict_by_session)

    # 2) Active flags grouped by (session_id, turn_id)
    active_ids = _active_flag_ids(conn, session_ids)
    flags_by_turn: dict[tuple, list] = {}
    if session_ids:
        ph = ",".join("?" * len(session_ids))
        for f in conn.execute(
            f"""SELECT flag_id, session_id, turn_id, category_code, severity,
                       source, detection_layer, status
                FROM flags WHERE session_id IN ({ph})""",
            tuple(session_ids),
        ).fetchall():
            if f["flag_id"] in active_ids:
                flags_by_turn.setdefault((f["session_id"], f["turn_id"]), []).append(f)

    # 3) Order sessions: SEVERE first, then FLAGGED, then CLEAN (others last);
    #    within each verdict tier, most active flags first.
    flag_count_by_session: dict[str, int] = {}
    for flags in flags_by_turn.values():
        for f in flags:
            flag_count_by_session[f["session_id"]] = flag_count_by_session.get(f["session_id"], 0) + 1

    VERDICT_RANK = {"SEVERE": 0, "FLAGGED": 1, "CLEAN": 2}

    def sort_key(sid: str):
        verdict = verdict_by_session.get(sid, "")
        return (
            VERDICT_RANK.get(verdict, 3),
            -flag_count_by_session.get(sid, 0),
            sid,
        )

    ordered_session_ids = sorted(session_ids, key=sort_key)

    # 4) Every turn of every submitted session (full chat)
    n_turns = 0
    n_rows = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()

        for sid in ordered_session_ids:
            turns = conn.execute(
                "SELECT turn_id, speaker, message_text, is_automated FROM turns WHERE session_id = ? ORDER BY turn_id",
                (sid,),
            ).fetchall()
            for t in turns:
                n_turns += 1
                base = {
                    "session_id":            sid,
                    "verdict":               verdict_by_session.get(sid, ""),
                    "reviewed_by":           reviewer_by_session.get(sid, ""),
                    "astrotalk_flagged":     astrotalk_flagged_by_session.get(sid, ""),
                    "turn_id":               t["turn_id"],
                    "turn_text":             t["message_text"],
                    "speaker":               t["speaker"],
                    "is_automated_message":  t["is_automated"],
                    "severity":              "",
                    "flag_type":             "",
                    "detection_layer":       "",
                    "status":                "",
                }
                tflags = flags_by_turn.get((sid, t["turn_id"]), [])
                if not tflags:
                    writer.writerow(base)
                    n_rows += 1
                else:
                    for f in tflags:
                        row = dict(base)
                        row["severity"]        = f["severity"]
                        row["flag_type"]       = f["category_code"]
                        row["detection_layer"] = f["source"] or f["detection_layer"]
                        row["status"]          = f["status"]
                        writer.writerow(row)
                        n_rows += 1

    conn.close()
    print(f"  Submitted sessions : {len(session_ids)}")
    print(f"  Turns (full chat)  : {n_turns}")
    print(f"  CSV rows written   : {n_rows}")
    print(f"  CSV written        : {out_path}")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Export flags for SUBMITTED_FOR_REVIEW sessions to CSV"
    )
    p.add_argument("--out", default=None, help="Output CSV path")
    args = p.parse_args()

    if args.out:
        out_path = Path(args.out)
    else:
        stamp = datetime.now().strftime("%Y%m%d")
        out_path = Path(__file__).resolve().parents[1] / "exports" / f"submitted_flags_{stamp}.csv"

    print("=" * 60)
    print("  Export flags for submitted-for-review sessions")
    print(f"  DB: {os.getenv('DB_PATH', 'store/results.db')}")
    print("=" * 60)
    export(out_path)


if __name__ == "__main__":
    main()
