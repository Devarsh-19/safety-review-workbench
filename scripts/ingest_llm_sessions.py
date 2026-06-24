"""
ingest_llm_sessions.py

Ingests LLM-pre-analysed session CSV data into the SQLite DB.
One row per turn in the input; each turn may carry LLM-detected flags.

Input CSV columns
-----------------
Standard session/turn columns (same as raw AstroTalk export):
    session_id, astrologer_id, user_id, session_start, session_end,
    duration_minutes, session_type, session_date, month, language_code,
    language_detected, astrotalk_flagged, astrotalk_flag_category,
    astrotalk_severity, turn_id (message_seq), speaker (sender),
    message_text, is_automated, timestamp

Plus two extra LLM columns:
    llm_flags        - pipe-separated flag codes e.g. "NSFW|FEAR_MANIPULATION"
                       or empty/blank if the turn is clean
    confidence_score - float 0.0-1.0, LLM confidence for the flagged turn
                       (blank / 0 if no flags)

What gets written
-----------------
  sessions  - one row per unique session_id (INSERT OR IGNORE)
              overall_verdict + confidence_score auto-computed from flags
              review_status = 'PENDING'
  turns     - one row per (session_id, turn_id)  (INSERT OR IGNORE)
  flags     - one row per flag per turn, source='LLM', status='ACTIVE'
              severity derived from flag category via verdict_rules

Safe to re-run — INSERT OR IGNORE skips already-ingested rows.

Usage
-----
  python scripts/ingest_llm_sessions.py --input path/to/file.csv
  python scripts/ingest_llm_sessions.py --input path/to/file.csv --dry-run
"""

import argparse
import csv
import os
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.db import get_connection, recompute_session_verdict  # noqa: E402
from engine.verdict_rules import to_canonical_flag, get_db_verdict_for_flags  # noqa: E402

# ---------------------------------------------------------------------------
# Severity derivation — same logic as backfill_flag_severity.py
# ---------------------------------------------------------------------------
_VERDICT_TO_SEVERITY = {"SEVERE": "HIGH", "FLAGGED": "MEDIUM", "CLEAN": "LOW"}


def _severity_for_flag(category_code: str) -> str:
    verdict = get_db_verdict_for_flags([category_code])
    return _VERDICT_TO_SEVERITY.get(verdict, "MEDIUM")


# ---------------------------------------------------------------------------
# CSV column aliases → canonical field names
# ---------------------------------------------------------------------------
def _norm_row(row: dict) -> dict:
    """Normalise column name aliases from different CSV exports."""
    aliases = {
        "message_seq":          "turn_id",
        "sender":               "speaker",
        "is_automated_message": "is_automated",
        "sent_at_ist":          "timestamp",
        "flagged":              "astrotalk_flagged",
        "language":             "language_code",
    }
    return {aliases.get(k, k): v for k, v in row.items()}


# ---------------------------------------------------------------------------
# Main ingestion
# ---------------------------------------------------------------------------
def ingest(input_path: Path, dry_run: bool = False) -> None:
    conn = get_connection()

    # Collect all rows grouped by session_id
    sessions_meta: dict[str, dict] = {}       # session_id -> session-level fields
    turns_by_session: dict[str, list] = defaultdict(list)
    flags_by_session: dict[str, list] = defaultdict(list)

    with input_path.open(encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            row = _norm_row({k.strip(): v.strip() for k, v in raw.items()})

            sid     = row.get("session_id", "").strip()
            turn_id = row.get("turn_id", "").strip()
            if not sid or not turn_id:
                continue

            # ── Session-level fields (use first row seen per session) ──────
            if sid not in sessions_meta:
                astrotalk_flagged_raw = row.get("astrotalk_flagged", "")
                astrotalk_flagged = (
                    1 if str(astrotalk_flagged_raw).strip().lower() in ("1", "yes", "true") else 0
                )
                sessions_meta[sid] = {
                    "session_id":             sid,
                    "astrologer_id":          row.get("astrologer_id") or None,
                    "user_id":                row.get("user_id") or None,
                    "session_start":          row.get("session_start") or None,
                    "session_end":            row.get("session_end") or None,
                    "duration_minutes":       _float_or_none(row.get("duration_minutes")),
                    "session_type":           row.get("session_type") or None,
                    "session_date":           row.get("session_date") or None,
                    "month":                  row.get("month") or None,
                    "language_code":          row.get("language_code") or None,
                    "language_detected":      row.get("language_detected") or None,
                    "astrotalk_flagged":      astrotalk_flagged,
                    "astrotalk_flag_category": row.get("astrotalk_flag_category") or None,
                    "astrotalk_severity":     row.get("astrotalk_severity") or None,
                    "review_status":          "PENDING",
                    "overall_verdict":        "CLEAN",   # placeholder; recomputed below
                    "confidence_score":       0.0,       # placeholder; recomputed below
                }

            # ── Turn row ───────────────────────────────────────────────────
            speaker = str(row.get("speaker", "")).upper()
            if speaker not in ("USER", "ASTROLOGER"):
                speaker = "USER"

            turns_by_session[sid].append({
                "turn_id":      int(turn_id),
                "speaker":      speaker,
                "message_text": row.get("message_text") or "",
                "is_automated": 1 if str(row.get("is_automated", "0")).lower() in ("1", "yes", "true") else 0,
                "timestamp":    row.get("timestamp") or None,
            })

            # ── LLM flags ─────────────────────────────────────────────────
            llm_flags_raw = row.get("llm_flags", "").strip()
            conf          = _float_or_none(row.get("confidence_score"))

            if llm_flags_raw:
                for raw_code in llm_flags_raw.split("|"):
                    raw_code = raw_code.strip()
                    if not raw_code:
                        continue
                    canonical = to_canonical_flag(raw_code)
                    flags_by_session[sid].append({
                        "turn_id":          int(turn_id),
                        "category_code":    canonical,
                        "detection_layer":  "LLM",
                        "source":           "LLM",
                        "status":           "ACTIVE",
                        "severity":         _severity_for_flag(canonical),
                        "confidence_score": conf,
                    })

    # ── Compute verdict + confidence per session from its collected flags ──
    for sid, meta in sessions_meta.items():
        flag_codes = [f["category_code"] for f in flags_by_session[sid]]
        verdict    = get_db_verdict_for_flags(flag_codes) if flag_codes else "CLEAN"
        conf_scores = [f["confidence_score"] for f in flags_by_session[sid]
                       if f.get("confidence_score") is not None]
        meta["overall_verdict"]  = verdict
        meta["confidence_score"] = max(conf_scores) if conf_scores else 0.0

    n_sessions = len(sessions_meta)
    n_turns    = sum(len(t) for t in turns_by_session.values())
    n_flags    = sum(len(f) for f in flags_by_session.values())

    print(f"  Parsed sessions : {n_sessions}")
    print(f"  Parsed turns    : {n_turns}")
    print(f"  Parsed flags    : {n_flags}")

    if dry_run:
        print("\nDRY RUN — no changes written.")
        conn.close()
        return

    # ── Write to DB ────────────────────────────────────────────────────────
    sessions_written = turns_written = flags_written = 0

    for sid, meta in sessions_meta.items():
        cols  = ", ".join(meta.keys())
        ph    = ", ".join("?" * len(meta))
        conn.execute(
            f"INSERT OR IGNORE INTO sessions ({cols}) VALUES ({ph})",
            list(meta.values()),
        )
        sessions_written += conn.execute(
            "SELECT changes()"
        ).fetchone()[0]

        for t in turns_by_session[sid]:
            conn.execute(
                """INSERT OR IGNORE INTO turns
                       (session_id, turn_id, speaker, message_text, is_automated, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (sid, t["turn_id"], t["speaker"], t["message_text"],
                 t["is_automated"], t["timestamp"]),
            )
            turns_written += conn.execute("SELECT changes()").fetchone()[0]

        for f in flags_by_session[sid]:
            conn.execute(
                """INSERT OR IGNORE INTO flags
                       (session_id, turn_id, category_code, detection_layer, source,
                        status, severity, confidence_score)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (sid, f["turn_id"], f["category_code"], f["detection_layer"],
                 f["source"], f["status"], f["severity"], f["confidence_score"]),
            )
            flags_written += conn.execute("SELECT changes()").fetchone()[0]

        # Auto-recompute verdict from all active flags now in DB
        recompute_session_verdict(sid, conn)

    conn.commit()
    conn.close()

    print(f"  Sessions written : {sessions_written}  (skipped already-present: {n_sessions - sessions_written})")
    print(f"  Turns written    : {turns_written}")
    print(f"  Flags written    : {flags_written}")
    print("\nDone. Verdict auto-recomputed for all ingested sessions.")


def _float_or_none(val) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def main() -> None:
    p = argparse.ArgumentParser(
        description="Ingest LLM-pre-analysed session CSV into the safety review DB"
    )
    p.add_argument("--input", required=True, help="Path to input CSV file")
    p.add_argument("--dry-run", action="store_true",
                   help="Parse and count without writing to DB")
    args = p.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: input file not found: {input_path}")
        sys.exit(1)

    print("=" * 60)
    print("  Ingest LLM sessions")
    print(f"  Input : {input_path}")
    print(f"  DB    : {os.getenv('DB_PATH', 'store/astrotalk.db')}")
    print(f"  Mode  : {'DRY-RUN' if args.dry_run else 'COMMIT'}")
    print("=" * 60)

    ingest(input_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
