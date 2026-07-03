"""
export_astrotalk_flagged_clean.py

Exports the FULL chat (every turn) for AstroTalk-flagged sessions whose review
verdict came out CLEAN — i.e. the platform manually flagged the session
(astrotalk_flagged = 1) but L1/L2/LLM review found nothing (overall_verdict =
'CLEAN'), and the session has been reviewed (review_status LOCKED or
SUBMITTED_FOR_REVIEW). These are the platform's manual flags that review cleared.

Same output columns as export_submitted_flags.py (one row per turn; a turn with
multiple active flags yields multiple rows — though CLEAN sessions normally have
none, so flag columns are blank). The astrotalk_flagged column is included.

Only sessions VERIFIED BY one of the given reviewers (COALESCE(submitted_by,
reviewer_id)) are exported. Defaults to Divyansh, Nikhil, Vineet, Yusuf — which
also excludes LLM-auto-submitted sessions (verifier 'LLM').

Read-only — never modifies the database.

Usage:
  python scripts/export_astrotalk_flagged_clean.py
  python scripts/export_astrotalk_flagged_clean.py --out C:/path/to/file.csv
  python scripts/export_astrotalk_flagged_clean.py --reviewers "Nikhil,Yusuf"
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

# Same columns as export_submitted_flags.py (astrotalk_flagged already included).
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


def export(out_path: Path, reviewers: list[str]) -> None:
    conn = get_connection()

    # 1) AstroTalk-flagged sessions whose review verdict is CLEAN, reviewed
    #    (LOCKED or SUBMITTED_FOR_REVIEW), and VERIFIED BY one of the given
    #    reviewers. The verifier is COALESCE(submitted_by, reviewer_id) — the
    #    same value shown in the reviewed_by column (this also excludes
    #    LLM-auto-submitted sessions, whose verifier is 'LLM').
    reviewer_ph = ",".join("?" * len(reviewers))
    sessions = conn.execute(
        f"""SELECT session_id, overall_verdict, submitted_by, reviewer_id, astrotalk_flagged
            FROM sessions
            WHERE astrotalk_flagged = 1
              AND overall_verdict = 'CLEAN'
              AND review_status IN ('LOCKED', 'SUBMITTED_FOR_REVIEW')
              AND COALESCE(submitted_by, reviewer_id) IN ({reviewer_ph})
            ORDER BY session_id""",
        tuple(reviewers),
    ).fetchall()
    verdict_by_session = {s["session_id"]: s["overall_verdict"] for s in sessions}
    reviewer_by_session = {
        s["session_id"]: (s["submitted_by"] or s["reviewer_id"] or "")
        for s in sessions
    }
    astrotalk_flagged_by_session = {s["session_id"]: s["astrotalk_flagged"] for s in sessions}
    session_ids = set(verdict_by_session)

    # 2) Active flags grouped by (session_id, turn_id) — normally empty for CLEAN
    #    sessions, kept for column parity with export_submitted_flags.
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

    ordered_session_ids = sorted(session_ids)

    # 3) Every turn of every matching session (full chat)
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
    print(f"  Reviewers (verified by)                     : {', '.join(reviewers)}")
    print(f"  AstroTalk-flagged + reviewer-clean sessions : {len(session_ids)}")
    print(f"  Turns (full chat)                           : {n_turns}")
    print(f"  CSV rows written                            : {n_rows}")
    print(f"  CSV written                                 : {out_path}")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Export full chats for AstroTalk-flagged but reviewed-CLEAN sessions"
    )
    p.add_argument("--out", default=None, help="Output CSV path")
    p.add_argument(
        "--reviewers", default="Divyansh,Nikhil,Vineet,Yusuf",
        help="Comma-separated reviewer names; only sessions verified by these "
             "(submitted_by / reviewer_id) are exported.",
    )
    args = p.parse_args()

    reviewers = [r.strip() for r in args.reviewers.split(",") if r.strip()]
    if not reviewers:
        print("ERROR: no reviewers given via --reviewers")
        sys.exit(1)

    if args.out:
        out_path = Path(args.out)
    else:
        stamp = datetime.now().strftime("%Y%m%d")
        out_path = Path(__file__).resolve().parents[1] / "exports" / f"astrotalk_flagged_clean_{stamp}.csv"

    print("=" * 60)
    print("  Export AstroTalk-flagged + reviewer-clean sessions")
    print(f"  DB: {os.getenv('DB_PATH', 'store/astrotalk.db')}")
    print(f"  Reviewers: {', '.join(reviewers)}")
    print("=" * 60)
    export(out_path, reviewers)


if __name__ == "__main__":
    main()
