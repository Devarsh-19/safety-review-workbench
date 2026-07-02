"""
ingest_llm_sessions.py

Ingests LLM-pre-analysed session CSV data into the SQLite DB.
Reuses DataLoader for all session/turn parsing (language mapping,
speaker normalization, timestamps, duration etc.) — same as batch_runner.
Adds LLM flag parsing on top from two extra columns:

    llm_flag         - pipe-separated flag codes e.g. "NSFW|FEAR_MANIPULATION"
                       (also accepted as: llm_flags)
    confidence_score - float 0.0-1.0 LLM confidence for that turn

If sessions/turns already exist in DB (e.g. from a prior
batch_runner --ingest-only run), they are skipped. LLM flags are
always inserted (de-duped) and verdict is always recomputed.

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
from tqdm import tqdm
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from store.db import (                                          # noqa: E402
    get_connection, recompute_session_verdict,
)
from store.writer import write_session_complete                  # noqa: E402
from engine.data_loader import DataLoader                        # noqa: E402
from engine.verdict_rules import to_canonical_flag, get_db_verdict_for_flags  # noqa: E402

# Load checkpoint directly to avoid pipeline/__init__ pulling in heavy deps
import importlib.util as _ilu
_cp_spec = _ilu.spec_from_file_location(
    "checkpoint",
    Path(__file__).resolve().parents[1] / "pipeline" / "checkpoint.py",
)
_cp = _ilu.module_from_spec(_cp_spec)
_cp_spec.loader.exec_module(_cp)
load_checkpoint = _cp.load_checkpoint
save_checkpoint = _cp.save_checkpoint

# ---------------------------------------------------------------------------
# Severity derivation
# ---------------------------------------------------------------------------
_VERDICT_TO_SEVERITY = {"SEVERE": "HIGH", "FLAGGED": "MEDIUM", "CLEAN": "LOW"}


def _severity_for_flag(category_code: str) -> str:
    verdict = get_db_verdict_for_flags([category_code])
    return _VERDICT_TO_SEVERITY.get(verdict, "MEDIUM")


def _float_or_none(val) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Parse LLM flags from raw CSV — keyed by (session_id, turn_id)
# ---------------------------------------------------------------------------
def _parse_llm_flags(input_path: Path) -> dict[tuple, list[dict]]:
    """
    Read the raw CSV and extract llm_flag / confidence_score per turn.
    Returns {(session_id, turn_id): [flag_dict, ...]}
    Column aliases: llm_flag / llm_flags, message_seq / turn_id.
    """
    flags_by_turn: dict[tuple, list[dict]] = defaultdict(list)

    with input_path.open(encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            row = {k.strip(): v.strip() for k, v in raw.items()}

            sid     = row.get("session_id", "").strip()
            turn_id = row.get("message_seq") or row.get("turn_id", "")
            turn_id = str(turn_id).strip()
            if not sid or not turn_id:
                continue

            # accept both llm_flag and llm_flags
            llm_raw = row.get("llm_flag") or row.get("llm_flags") or ""
            llm_raw = llm_raw.strip()

            if not llm_raw:
                continue

            # A turn can carry MULTIPLE flags, pipe-separated, with a matching
            # pipe-separated confidence list, e.g.
            #   llm_flag         = "NSFW|ABUSIVE_LANGUAGE"
            #   confidence_score = "0.9|0.7"
            # Split both and pair positionally so each flag keeps its own score.
            # (Parsing the whole cell as one float would drop the score to None
            #  for every multi-flag turn.)
            codes = [c.strip() for c in llm_raw.split("|") if c.strip()]
            confs = [c.strip() for c in (row.get("confidence_score") or "").split("|")]

            for i, raw_code in enumerate(codes):
                canonical = to_canonical_flag(raw_code)
                conf = _float_or_none(confs[i]) if i < len(confs) else None
                flags_by_turn[(sid, int(turn_id))].append({
                    "turn_id":          int(turn_id),
                    "category_code":    canonical,
                    "detection_layer":  "LLM",
                    "source":           "LLM",
                    "status":           "ACTIVE",
                    "severity":         _severity_for_flag(canonical),
                    "confidence_score": conf,
                })

    return flags_by_turn


# ---------------------------------------------------------------------------
# Main ingestion
# ---------------------------------------------------------------------------
def ingest(input_path: Path, dry_run: bool = False, auto_submit: bool = True,
           assign_to: str | None = None) -> None:
    processed_ids = load_checkpoint()

    # Assigning to a reviewer must NOT lock or submit — disable auto-submit so
    # the sessions stay PENDING and simply become visible in the reviewer queue.
    if assign_to:
        auto_submit = False

    # ── Step 1: parse sessions+turns via DataLoader (handles all mapping) ──
    print("  Loading sessions via DataLoader...")
    loader   = DataLoader(str(input_path))
    sessions = loader.load_sessions()
    print(f"  Loaded {len(sessions)} sessions from CSV")

    # ── Step 2: parse LLM flags directly from raw CSV ─────────────────────
    flags_by_turn = _parse_llm_flags(input_path)
    n_flagged_turns = len(flags_by_turn)
    n_flags_total   = sum(len(v) for v in flags_by_turn.values())
    print(f"  LLM flagged turns : {n_flagged_turns}")
    print(f"  LLM flags total   : {n_flags_total}")

    # Build per-session flag list
    flags_by_session: dict[str, list] = defaultdict(list)
    for (sid, _tid), flist in flags_by_turn.items():
        flags_by_session[sid].extend(flist)

    if dry_run:
        if assign_to:
            print(f"  Would assign to {assign_to}: {len(sessions)} "
                  f"(review_status stays PENDING — no submit/lock)")
        elif auto_submit:
            n_clean = sum(
                1 for s in sessions
                if not flags_by_session.get(str(s["session_id"]))
            )
            print(f"  Would auto-submit CLEAN (reviewer=LLM): {n_clean}")
        print("\nDRY RUN — no changes written.")
        return

    # ── Step 3: open ONE connection for the whole ingest ──────────────────
    # A single reused connection + WAL + batched commits is the entire speed
    # win: the old code opened 2-3 connections and committed 2-3 times PER
    # session (each commit forces an fsync). journal_mode=WAL is persistent —
    # the DB stays WAL afterwards, which also lets the live dashboard read
    # while ingestion writes.
    conn = get_connection()
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous  = NORMAL")
    conn.execute("PRAGMA temp_store   = MEMORY")

    existing_sessions = {
        r[0] for r in conn.execute("SELECT session_id FROM sessions").fetchall()
    }
    # Preload existing LLM flag keys ONCE — replaces the per-flag SELECT.
    existing_flag_keys = {
        (r[0], r[1], r[2])
        for r in conn.execute(
            "SELECT session_id, turn_id, category_code FROM flags WHERE source = 'LLM'"
        ).fetchall()
    }
    # Preload review_status for every session — avoids a per-session SELECT.
    review_status_by_sid = {
        r[0]: r[1]
        for r in conn.execute("SELECT session_id, review_status FROM sessions").fetchall()
    }

    sessions_written = turns_written = flags_written = auto_submitted = assigned = 0
    to_submit: list[str] = []   # CLEAN + PENDING sessions to auto-submit
    to_assign: list[str] = []   # sessions to assign
    COMMIT_EVERY = 500

    try:
        for n, session in enumerate(tqdm(sessions, desc="Ingesting", unit="session"), start=1):
            sid = str(session["session_id"])

            session_data = {
                "session_id":              sid,
                "astrologer_id":           session.get("astrologer_id"),
                "user_id":                 session.get("user_id"),
                "session_start":           session.get("session_start"),
                "session_end":             session.get("session_end"),
                "duration_minutes":        session.get("duration_minutes"),
                "session_type":            session.get("session_type", "chat"),
                "session_date":            session.get("session_date"),
                "month":                   session.get("month"),
                "language_code":           session.get("language_code"),
                "language_detected":       session.get("language_detected"),
                "astrotalk_flagged":       session.get("astrotalk_flagged", 0),
                "astrotalk_flag_category": session.get("astrotalk_flag_category"),
                "astrotalk_severity":      session.get("astrotalk_severity"),
                "review_status":           "PENDING",
                "overall_verdict":         "CLEAN",
                "confidence_score":        0.0,
            }

            turns = [
                {
                    "turn_id":           m["turn_id"],
                    "speaker":           m["speaker"],
                    "message_text":      m["message_text"],
                    "timestamp":         m.get("timestamp"),
                    "language_detected": m.get("language_detected"),
                    "is_automated":      m.get("is_automated", 0),
                    "has_link":          m.get("has_link", 0),
                }
                for m in session.get("messages", [])
            ]

            # ── Step 4a: write session + turns (skip existing), shared conn ──
            if sid not in existing_sessions:
                write_session_complete(sid, session_data, turns, [], conn=conn)
                sessions_written += 1
                turns_written    += len(turns)
                review_status_by_sid[sid] = "PENDING"

            # ── Step 4b: insert LLM flags (de-duped via preloaded key set) ──
            new_flag_rows = []
            for f in flags_by_session.get(sid, []):
                key = (sid, f["turn_id"], f["category_code"])
                if key in existing_flag_keys:
                    continue
                existing_flag_keys.add(key)
                new_flag_rows.append((
                    sid, f["turn_id"], f["category_code"], f["detection_layer"],
                    f["source"], f["status"], f["severity"], f["confidence_score"],
                ))
            if new_flag_rows:
                conn.executemany(
                    """INSERT INTO flags
                           (session_id, turn_id, category_code, detection_layer,
                            source, status, severity, confidence_score)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    new_flag_rows,
                )
                flags_written += len(new_flag_rows)

            # ── Step 5: fix existing LLM flags with null turn_id ────────────
            # batch_runner writes LLM flags with turn_id=None; update them
            # using category_code + session_id match from CSV flag data.
            for f in flags_by_session.get(sid, []):
                conn.execute(
                    """UPDATE flags
                          SET turn_id = ?
                        WHERE session_id = ?
                          AND category_code = ?
                          AND source = 'LLM'
                          AND turn_id IS NULL""",
                    (f["turn_id"], sid, f["category_code"]),
                )

            # ── Step 6: recompute verdict from all active flags ─────────────
            verdict = recompute_session_verdict(sid, conn)
            review_status = review_status_by_sid.get(sid)

            # ── Step 7/8: queue auto-submit / assign for a batched pass ─────
            # A session with no active flags (verdict CLEAN) is submitted for
            # L2 review under reviewer 'LLM'. Only PENDING sessions are touched,
            # so re-ingesting never clobbers submitted/reviewed/locked ones.
            if auto_submit and verdict == "CLEAN" and review_status == "PENDING":
                to_submit.append(sid)
            if assign_to:
                to_assign.append(sid)

            processed_ids.add(sid)

            if n % COMMIT_EVERY == 0:
                conn.commit()

        conn.commit()

        # ── Step 7: batched auto-submit (same fields as submit_session_for_review) ──
        if to_submit:
            conn.executemany(
                """UPDATE sessions
                      SET review_status = 'SUBMITTED_FOR_REVIEW',
                          submitted_by  = 'LLM',
                          submitted_at  = datetime('now'),
                          reviewer_id   = 'LLM',
                          reviewer_note = ?,
                          reviewed_at   = datetime('now')
                    WHERE session_id = ? AND review_status = 'PENDING'""",
                [("Auto-submitted by LLM ingest: no flags", sid) for sid in to_submit],
            )
            auto_submitted = len(to_submit)

        # ── Step 8: batched assignment (sets assigned_to ONLY) ─────────────
        if assign_to:
            conn.executemany(
                "UPDATE sessions SET assigned_to = ? WHERE session_id = ?",
                [(assign_to, sid) for sid in to_assign],
            )
            assigned = len(to_assign)

        conn.commit()
    finally:
        conn.close()

    # ── Step 9: save checkpoint ────────────────────────────────────────────
    save_checkpoint(processed_ids)

    print(f"\n  Sessions written     : {sessions_written}  "
          f"(skipped existing: {len(sessions) - sessions_written})")
    print(f"  Turns written        : {turns_written}")
    print(f"  LLM flags written    : {flags_written}")
    if assign_to:
        print(f"  Assigned to {assign_to:<8}: {assigned}  (review_status=PENDING — not submitted/locked)")
    else:
        print(f"  CLEAN auto-submitted : {auto_submitted}  (reviewer=LLM, SUBMITTED_FOR_REVIEW)")
    print(f"  Checkpoint updated   : {len(processed_ids)} total sessions logged.")
    print(f"\nDone. Verdict recomputed for all {len(sessions)} sessions.")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Ingest LLM-pre-analysed session CSV into the safety review DB"
    )
    p.add_argument("--input",   required=True, help="Path to input CSV file")
    p.add_argument("--dry-run", action="store_true",
                   help="Parse and count without writing to DB")
    p.add_argument("--no-auto-submit", action="store_true",
                   help="Do NOT auto-submit no-flag (CLEAN) sessions for L2 "
                        "review as reviewer 'LLM'.")
    p.add_argument("--assign-to", default=None, metavar="REVIEWER",
                   help="Assign every ingested session to this reviewer "
                        "(sets assigned_to; stays PENDING — no submit/lock). "
                        "Implies --no-auto-submit for the run.")
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
    auto_submit_on = not args.no_auto_submit and not args.assign_to
    print(f"  Auto-submit CLEAN (reviewer=LLM): {'ON' if auto_submit_on else 'OFF'}")
    if args.assign_to:
        print(f"  Assign ingested sessions to     : {args.assign_to} (PENDING, no submit/lock)")
    print("=" * 60)

    ingest(input_path, dry_run=args.dry_run, auto_submit=not args.no_auto_submit,
           assign_to=args.assign_to)


if __name__ == "__main__":
    main()
