"""
violation_breakdown_audio.py

Audio counterpart of scripts/violation_breakdown.py, for the audio review
database (store/audio_review.db). Distinct sessions per violation category
(audio_flags.intent), over sessions whose overall_verdict is not CLEAN.
Read-only.

Prints twelve breakdowns:
   1. Overall
   2. astrotalk_verdict = CLEAN            (AstroTalk did NOT flag the session)
   3. astrotalk_verdict = FLAGGED          (AstroTalk DID flag the session)
   4. Violations by USER only              (flagged segments spoken only by the user)
   5. Violations by ASTROLOGER only        (flagged segments spoken only by the astrologer)
   6. Violations by BOTH                    (flagged segments from both speakers)
   7. astrotalk CLEAN x USER only
   8. astrotalk CLEAN x ASTROLOGER only
   9. astrotalk CLEAN x BOTH
  10. astrotalk FLAGGED x USER only
  11. astrotalk FLAGGED x ASTROLOGER only
  12. astrotalk FLAGGED x BOTH

Mapping to the chat script:
  - category_code        -> audio_flags.intent
  - astrotalk_flagged    -> audio_sessions.astrotalk_verdict (CLEAN vs FLAGGED;
                            legacy SEVERE is normalised to FLAGGED; NULL/other
                            appears in neither the CLEAN nor the FLAGGED bucket)
  - turns.speaker        -> the flag's segment speaker, resolved to a ROLE.

Speaker attribution (mirrors AudioSessionViewer): a segment carries a raw
diarization label (e.g. 'SPEAKER_1'). Per session the distinct labels are ordered
by the numeric part of the label; the FIRST maps to speaker1_role and the SECOND
to speaker2_role (both assigned by the reviewer). A flag whose segment has no
speaker, points at a missing segment, sits on a 3rd+ speaker, or belongs to a
session whose roles are not yet assigned is UNATTRIBUTED — it appears only in the
overall / astrotalk breakdowns, never in the speaker buckets.

The speaker buckets are mutually exclusive: a session goes to USER only,
ASTROLOGER only, or BOTH, never more than one.

Only ACTIVE flags are counted (amendment rows + un-amended originals; DISMISSED
rows are excluded) — consistent with the queue's flag_count column.

Counts are DISTINCT sessions, so a session with the same intent on multiple
segments counts once. A session with several different intents appears once under
each, so column totals can exceed the number of sessions.

Export (--out) writes everything shown on screen:
  .xlsx  -> two sheets: "Summary" and "Breakdown".
  .csv   -> two files: <name>.csv (breakdown) and <name>_summary.csv (totals).

Usage:
  python scripts/violation_breakdown_audio.py
  python scripts/violation_breakdown_audio.py --out exports/violation_breakdown_audio.xlsx
  python scripts/violation_breakdown_audio.py --out exports/violation_breakdown_audio.csv
"""

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import (  # noqa: E402
    get_audio_connection,
    AUDIO_DB_PATH,
    _active_audio_flag_rows,
    _normalize_audio_verdict,
)


def _norm_intent(intent) -> str:
    return (intent or "").strip().upper().replace("-", "_").replace(" ", "_")


def _speaker_num(label) -> int:
    """Numeric part of a diarization label, matching the frontend's speakerNum
    (SPEAKER_00 before SPEAKER_01). Labels with no digits sort last."""
    m = re.search(r"\d+", str(label))
    return int(m.group()) if m else 2**53


def _role_by_label(labels, speaker1_role, speaker2_role) -> dict:
    """Order distinct labels like the UI (numeric part, then string) and map the
    first two to speaker1_role / speaker2_role. Extra speakers get no role."""
    ordered = sorted(set(labels), key=lambda l: (_speaker_num(l), str(l)))
    role_map = {}
    for idx, label in enumerate(ordered):
        if idx == 0:
            role_map[label] = speaker1_role
        elif idx == 1:
            role_map[label] = speaker2_role
        else:
            role_map[label] = None
    return role_map


def collect(conn):
    """Build per-(session,intent) and per-session speaker/astrotalk records over
    non-CLEAN sessions. Returns (cat_rows, totals, all_sessions)."""
    sessions = {
        r["s_id"]: r
        for r in conn.execute(
            "SELECT s_id, overall_verdict, astrotalk_verdict, speaker1_role, speaker2_role "
            "FROM audio_sessions"
        ).fetchall()
    }

    # segment speaker label per (s_id, seg_id), and the label set per session.
    seg_speaker = {}
    labels_by_sid = defaultdict(list)
    for r in conn.execute("SELECT s_id, seg_id, speaker FROM audio_segments").fetchall():
        seg_speaker[(r["s_id"], r["seg_id"])] = r["speaker"]
        if r["speaker"]:
            labels_by_sid[r["s_id"]].append(r["speaker"])

    role_map_by_sid = {
        s_id: _role_by_label(labels, sessions[s_id]["speaker1_role"], sessions[s_id]["speaker2_role"])
        for s_id, labels in labels_by_sid.items()
    }

    # Active, non-dismissed flags grouped by session.
    flags_by_sid = defaultdict(list)
    for r in conn.execute(
        "SELECT flag_id, s_id, seg_id, intent, status, parent_flag_id FROM audio_flags"
    ).fetchall():
        flags_by_sid[r["s_id"]].append(r)

    # Per-(session, intent) speaker presence, and per-session speaker presence,
    # counted only on non-CLEAN sessions.
    cat_speaker = defaultdict(lambda: {"user": False, "astro": False})   # (s_id, intent)
    sess_speaker = defaultdict(lambda: {"user": False, "astro": False})  # s_id
    sess_intents = defaultdict(set)

    for s_id, rows in flags_by_sid.items():
        sess = sessions.get(s_id)
        if not sess:
            continue
        if _normalize_audio_verdict(sess["overall_verdict"]) == "CLEAN":
            continue  # violation report: skip CLEAN sessions

        active = [r for r in _active_audio_flag_rows(rows) if (r["status"] or "") != "DISMISSED"]
        role_map = role_map_by_sid.get(s_id, {})
        for f in active:
            intent = _norm_intent(f["intent"])
            label = seg_speaker.get((s_id, f["seg_id"]))
            role = role_map.get(label) if label else None
            sess_intents[s_id].add(intent)
            if role == "USER":
                cat_speaker[(s_id, intent)]["user"] = True
                sess_speaker[s_id]["user"] = True
            elif role == "ASTROLOGER":
                cat_speaker[(s_id, intent)]["astro"] = True
                sess_speaker[s_id]["astro"] = True
            else:
                cat_speaker[(s_id, intent)]  # touch so the (s_id,intent) exists
                sess_speaker[s_id]

    # ---- per-category rows (distinct sessions) --------------------------------
    def astro_of(s_id):
        return _normalize_audio_verdict(sessions[s_id]["astrotalk_verdict"])

    cats = defaultdict(lambda: defaultdict(int))
    for (s_id, intent), sp in cat_speaker.items():
        av = astro_of(s_id)
        u, a = sp["user"], sp["astro"]
        user_only = u and not a
        astro_only = a and not u
        both = u and a
        c = cats[intent]
        c["overall"] += 1
        if av == "CLEAN":
            c["astro_clean"] += 1
        elif av == "FLAGGED":
            c["astro_flagged"] += 1
        if user_only:  c["by_user"] += 1
        if astro_only: c["by_astrologer"] += 1
        if both:       c["by_both"] += 1
        if av == "CLEAN" and user_only:  c["astro_clean_user"] += 1
        if av == "CLEAN" and astro_only: c["astro_clean_astrologer"] += 1
        if av == "CLEAN" and both:       c["astro_clean_both"] += 1
        if av == "FLAGGED" and user_only:  c["astro_flagged_user"] += 1
        if av == "FLAGGED" and astro_only: c["astro_flagged_astrologer"] += 1
        if av == "FLAGGED" and both:       c["astro_flagged_both"] += 1

    # Normalise every row to carry all count keys (defaultdict only holds the
    # ones that were incremented), so downstream indexing/export is safe.
    count_keys = (
        "overall", "astro_clean", "astro_flagged",
        "by_user", "by_astrologer", "by_both",
        "astro_clean_user", "astro_clean_astrologer", "astro_clean_both",
        "astro_flagged_user", "astro_flagged_astrologer", "astro_flagged_both",
    )
    cat_rows = [
        dict(intent=intent, **{k: counts.get(k, 0) for k in count_keys})
        for intent, counts in cats.items()
    ]
    cat_rows.sort(key=lambda r: (-r["overall"], r["intent"]))

    # ---- session-level totals -------------------------------------------------
    totals = defaultdict(int)
    for s_id in sess_intents:  # every non-CLEAN session with >=1 active flag
        av = astro_of(s_id)
        u, a = sess_speaker[s_id]["user"], sess_speaker[s_id]["astro"]
        user_only, astro_only, both = (u and not a), (a and not u), (u and a)
        totals["overall"] += 1
        if av == "CLEAN":   totals["astro_clean"] += 1
        elif av == "FLAGGED": totals["astro_flagged"] += 1
        if user_only:  totals["by_user"] += 1
        if astro_only: totals["by_astrologer"] += 1
        if both:       totals["by_both"] += 1
        if not (u or a): totals["unattributed"] += 1
        if av == "CLEAN" and user_only:  totals["astro_clean_user"] += 1
        if av == "CLEAN" and astro_only: totals["astro_clean_astrologer"] += 1
        if av == "CLEAN" and both:       totals["astro_clean_both"] += 1
        if av == "FLAGGED" and user_only:  totals["astro_flagged_user"] += 1
        if av == "FLAGGED" and astro_only: totals["astro_flagged_astrologer"] += 1
        if av == "FLAGGED" and both:       totals["astro_flagged_both"] += 1

    # ---- all sessions in DB (regardless of verdict) ---------------------------
    all_sessions = {"total": 0, "astro_clean": 0, "astro_flagged": 0}
    for s in sessions.values():
        all_sessions["total"] += 1
        av = _normalize_audio_verdict(s["astrotalk_verdict"])
        if av == "CLEAN":
            all_sessions["astro_clean"] += 1
        elif av == "FLAGGED":
            all_sessions["astro_flagged"] += 1

    return cat_rows, totals, all_sessions


def _print_table(title, rows, col, total_sessions=None, violation_sessions=None):
    print("-" * 64)
    print(f"  {title}")
    print("-" * 64)
    if total_sessions is not None:
        print(f"  Total sessions                           {total_sessions:>8,}")
    if violation_sessions is not None:
        print(f"  Sessions with violations                 {violation_sessions:>8,}")
        print()
    shown = [(r["intent"], r.get(col, 0)) for r in rows if r.get(col, 0) > 0]
    if not shown:
        print("  (no violation data)")
        print()
        return
    for cat, n in shown:
        print(f"  {cat:<40} {n:>8,}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Audio violation breakdown by astrotalk_verdict.")
    parser.add_argument("--out", help="Optional CSV/XLSX output path.")
    args = parser.parse_args()

    conn = get_audio_connection()
    try:
        cat_rows, totals, all_sessions = collect(conn)
    finally:
        conn.close()

    print("=" * 64)
    print("  Audio Violation Breakdown (distinct sessions per intent)")
    print(f"  DB: {AUDIO_DB_PATH}")
    print("=" * 64)
    print(f"  Total sessions in DB                         : {all_sessions['total']:>8,}")
    print(f"  ... of which astrotalk_verdict = CLEAN       : {all_sessions['astro_clean']:>8,}")
    print(f"  ... of which astrotalk_verdict = FLAGGED     : {all_sessions['astro_flagged']:>8,}")
    print(f"  Sessions with violations (overall)           : {totals['overall']:>8,}")
    print(f"  ... of which astrotalk_verdict = CLEAN       : {totals['astro_clean']:>8,}")
    print(f"  ... of which astrotalk_verdict = FLAGGED     : {totals['astro_flagged']:>8,}")
    print(f"  ... violations by USER only                  : {totals['by_user']:>8,}")
    print(f"  ... violations by ASTROLOGER only            : {totals['by_astrologer']:>8,}")
    print(f"  ... violations by BOTH speakers              : {totals['by_both']:>8,}")
    print(f"  ... unattributed (no speaker/role)           : {totals['unattributed']:>8,}")
    print()

    _print_table("Overall", cat_rows, "overall",
                 total_sessions=all_sessions["total"], violation_sessions=totals["overall"])
    _print_table("astrotalk_verdict = CLEAN  (AstroTalk did NOT flag)", cat_rows, "astro_clean",
                 total_sessions=all_sessions["astro_clean"], violation_sessions=totals["astro_clean"])
    _print_table("astrotalk_verdict = FLAGGED  (AstroTalk DID flag)", cat_rows, "astro_flagged",
                 total_sessions=all_sessions["astro_flagged"], violation_sessions=totals["astro_flagged"])
    _print_table("Violations by USER only (segments spoken only by user)", cat_rows, "by_user",
                 violation_sessions=totals["by_user"])
    _print_table("Violations by ASTROLOGER only (segments spoken only by astrologer)", cat_rows, "by_astrologer",
                 violation_sessions=totals["by_astrologer"])
    _print_table("Violations by BOTH (segments from both speakers)", cat_rows, "by_both",
                 violation_sessions=totals["by_both"])
    _print_table("astrotalk CLEAN  x  USER only", cat_rows, "astro_clean_user",
                 violation_sessions=totals["astro_clean_user"])
    _print_table("astrotalk CLEAN  x  ASTROLOGER only", cat_rows, "astro_clean_astrologer",
                 violation_sessions=totals["astro_clean_astrologer"])
    _print_table("astrotalk CLEAN  x  BOTH speakers", cat_rows, "astro_clean_both",
                 violation_sessions=totals["astro_clean_both"])
    _print_table("astrotalk FLAGGED  x  USER only", cat_rows, "astro_flagged_user",
                 violation_sessions=totals["astro_flagged_user"])
    _print_table("astrotalk FLAGGED  x  ASTROLOGER only", cat_rows, "astro_flagged_astrologer",
                 violation_sessions=totals["astro_flagged_astrologer"])
    _print_table("astrotalk FLAGGED  x  BOTH speakers", cat_rows, "astro_flagged_both",
                 violation_sessions=totals["astro_flagged_both"])

    if args.out:
        breakdown_header = ["intent", "overall", "astrotalk_clean", "astrotalk_flagged",
                            "user_only", "astrologer_only", "both",
                            "clean_user_only", "clean_astrologer_only", "clean_both",
                            "flagged_user_only", "flagged_astrologer_only", "flagged_both"]
        breakdown_rows = [
            [r["intent"], r["overall"], r["astro_clean"], r["astro_flagged"],
             r["by_user"], r["by_astrologer"], r["by_both"],
             r["astro_clean_user"], r["astro_clean_astrologer"], r["astro_clean_both"],
             r["astro_flagged_user"], r["astro_flagged_astrologer"], r["astro_flagged_both"]]
            for r in cat_rows
        ]
        summary_rows = [
            ("Total sessions in DB",                               all_sessions["total"]),
            ("Total sessions astrotalk_verdict = CLEAN",           all_sessions["astro_clean"]),
            ("Total sessions astrotalk_verdict = FLAGGED",         all_sessions["astro_flagged"]),
            ("Sessions with violations (overall)",                 totals["overall"]),
            ("Sessions with violations astrotalk CLEAN",           totals["astro_clean"]),
            ("Sessions with violations astrotalk FLAGGED",         totals["astro_flagged"]),
            ("Sessions with USER-only violations",                 totals["by_user"]),
            ("Sessions with ASTROLOGER-only violations",           totals["by_astrologer"]),
            ("Sessions with violations by BOTH speakers",          totals["by_both"]),
            ("Sessions with unattributed violations (no speaker/role)", totals["unattributed"]),
            ("Sessions astrotalk CLEAN x USER only",               totals["astro_clean_user"]),
            ("Sessions astrotalk CLEAN x ASTROLOGER only",         totals["astro_clean_astrologer"]),
            ("Sessions astrotalk CLEAN x BOTH",                    totals["astro_clean_both"]),
            ("Sessions astrotalk FLAGGED x USER only",             totals["astro_flagged_user"]),
            ("Sessions astrotalk FLAGGED x ASTROLOGER only",       totals["astro_flagged_astrologer"]),
            ("Sessions astrotalk FLAGGED x BOTH",                  totals["astro_flagged_both"]),
        ]

        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if out_path.suffix.lower() == ".csv":
            with open(out_path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(breakdown_header)
                writer.writerows(breakdown_rows)
            summary_path = out_path.with_name(out_path.stem + "_summary.csv")
            with open(summary_path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["metric", "session_count"])
                writer.writerows(summary_rows)
            print(f"  CSV written: {out_path}")
            print(f"  CSV written: {summary_path}")
        else:
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = "Summary"
            ws.append(["metric", "session_count"])
            for row in summary_rows:
                ws.append(list(row))
            ws.column_dimensions["A"].width = 52
            ws.column_dimensions["B"].width = 14
            ws2 = wb.create_sheet("Breakdown")
            ws2.append(breakdown_header)
            for row in breakdown_rows:
                ws2.append(row)
            ws2.column_dimensions["A"].width = 36
            wb.save(out_path)
            print(f"  Excel written: {out_path}  (sheets: Summary, Breakdown)")


if __name__ == "__main__":
    main()
