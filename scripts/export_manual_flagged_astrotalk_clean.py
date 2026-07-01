"""
export_manual_flagged_astrotalk_clean.py

Exports the FULL chat (every turn) for sessions that a HUMAN reviewer flagged but
AstroTalk itself did NOT — i.e. the session carries an active MANUAL flag
(source = 'MANUAL'), the platform did not flag it (astrotalk_flagged = 0), and the
session has been reviewed (review_status LOCKED or SUBMITTED_FOR_REVIEW). These are
the manual catches AstroTalk missed. re_engagement_solicitation flags are excluded
(both from session selection and from the exported flag rows).

Unlike export_astrotalk_flagged_clean.py, these sessions DO carry flags, so the flag
columns are populated on flagged turns. Turns with no active flag still emit one
blank-flag row (full-chat parity). ALL active flags on a turn are shown
(MANUAL + LLM + REGEX), each as its own row.

No reviewer filter — every matching session is exported, including LLM-auto-submitted
ones (verifier 'LLM').

Same output columns as export_astrotalk_flagged_clean.py.

Read-only — never modifies the database.

Usage:
  python scripts/export_manual_flagged_astrotalk_clean.py
  python scripts/export_manual_flagged_astrotalk_clean.py --out C:/path/to/file.csv
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

# Same columns as export_astrotalk_flagged_clean.py.
CSV_COLUMNS = ["session_id", "verdict", "reviewed_by", "astrotalk_flagged", "turn_id",
               "turn_text", "severity", "flag_type", "detection_layer", "status",
               "speaker", "is_automated_message"]

# Normalized category_code that is excluded everywhere in this export.
EXCLUDED_CATEGORY = "re_engagement_solicitation"


def _norm(code: str | None) -> str:
    """Normalize a category_code: lower-case, '-'/space -> '_'."""
    if not code:
        return ""
    return code.strip().lower().replace("-", "_").replace(" ", "_")


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

    # 1) Sessions with an active, non-re-engagement MANUAL flag that AstroTalk did
    #    NOT flag (astrotalk_flagged = 0), reviewed (LOCKED or SUBMITTED_FOR_REVIEW).
    #    DISTINCT collapses the per-flag join duplicates. No reviewer filter — every
    #    matching session is exported, including LLM-auto-submitted ones.
    sessions = conn.execute(
        """SELECT DISTINCT s.session_id, s.overall_verdict, s.submitted_by,
                  s.reviewer_id, s.astrotalk_flagged
           FROM sessions s
           JOIN flags f ON f.session_id = s.session_id
           WHERE f.source = 'MANUAL'
             AND s.astrotalk_flagged = 0
             AND s.review_status IN ('LOCKED', 'SUBMITTED_FOR_REVIEW')
             AND LOWER(REPLACE(REPLACE(f.category_code,'-','_'),' ','_')) != ?
             AND f.flag_id NOT IN (
                   SELECT parent_flag_id FROM flags WHERE parent_flag_id IS NOT NULL
             )
           ORDER BY s.session_id""",
        (EXCLUDED_CATEGORY,),
    ).fetchall()
    verdict_by_session = {s["session_id"]: s["overall_verdict"] for s in sessions}
    reviewer_by_session = {
        s["session_id"]: (s["submitted_by"] or s["reviewer_id"] or "")
        for s in sessions
    }
    astrotalk_flagged_by_session = {s["session_id"]: s["astrotalk_flagged"] for s in sessions}
    session_ids = set(verdict_by_session)

    # 2) All active flags grouped by (session_id, turn_id). re_engagement flags are
    #    suppressed here too, for consistency with the session filter.
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
            if f["flag_id"] not in active_ids:
                continue
            if _norm(f["category_code"]) == EXCLUDED_CATEGORY:
                continue
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
    print(f"  Manual-flagged + astrotalk-clean sessions : {len(session_ids)}")
    print(f"  Turns (full chat)                         : {n_turns}")
    print(f"  CSV rows written                          : {n_rows}")
    print(f"  CSV written                               : {out_path}")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Export full chats for manual-flagged but AstroTalk-clean sessions"
    )
    p.add_argument("--out", default=None, help="Output CSV path")
    args = p.parse_args()

    if args.out:
        out_path = Path(args.out)
    else:
        stamp = datetime.now().strftime("%Y%m%d")
        out_path = Path(__file__).resolve().parents[1] / "exports" / f"manual_flagged_astrotalk_clean_{stamp}.csv"

    print("=" * 60)
    print("  Export manual-flagged + astrotalk-clean sessions")
    print(f"  DB: {os.getenv('DB_PATH', 'store/results.db')}")
    print("=" * 60)
    export(out_path)


if __name__ == "__main__":
    main()
