#!/usr/bin/env python3
"""
backfill_severity.py

Derives a session-level SEVERITY (HIGH / MEDIUM / LOW) for every chat and audio
session from the "Severity Criteria" rulebook, exports the result to CSV, and
(optionally) backfills the severity into both review databases.

Severity Criteria
-----------------
Evaluated per session over its ACTIVE flags (see "Flag population" below).
HIGH is checked first (it wins over LOW); then LOW; otherwise MEDIUM.

HIGH — any of:
  H1  CSAM_RISK flags >= 3 in the session, with SENDER = Astrologer for at
      least 3 of them.
  H2  NSFW_EXPLICIT flags >= 5 in the session, with SENDER = Astrologer for at
      least 5 of them.
  H3  NSFW_EXPLICIT present AND it co-occurs with any other flag category AND
      total flags in the session >= 20 AND SENDER = Astrologer for at least 2
      of the NSFW_EXPLICIT messages.

LOW — any of (only if not HIGH):
  L1  All flags in the session authored by the User (any category, any count) —
      a pure user-only session.
  L2  Total flags >= 10 AND #flags with SENDER = Astrologer <= 3.
  L3  Total flags <= 5.
  L4  None of these categories present: NSFW, NSFW_EXPLICIT, NSFW_GROOMING,
      NSFW_APPEARANCE, CSAM_RISK, VIOLENCE, ABUSIVE_LANGUAGE, SELF_HARM.

MEDIUM — the default bucket: any session not meeting a HIGH condition and not
meeting any LOW path.

CLEAN — a session with no active flags at all (nothing to grade).

Flag population (what counts as a flag)
---------------------------------------
"Active" flags only, matching scripts/astro_flag_ge5.py and the review queue:
  * not DISMISSED, and
  * not an amended original (the amendment row is the live version).

Astrologer attribution
----------------------
  * chat : flags.turn_id -> turns.speaker == 'ASTROLOGER'.
  * audio: flag.seg_id -> the segment's diarization label -> a ROLE. Per session
           the distinct labels are ordered by the numeric part of the label;
           the FIRST maps to speaker1_role and the SECOND to speaker2_role.
           Flags on a 3rd+ speaker, an unmatched segment, or a session whose
           roles are not assigned are treated as NOT the astrologer.

Outputs
-------
Two CSVs (columns: session_id, severity), one per channel:
    exports/severity_chat.csv
    exports/severity_audio.csv

Usage
-----
    # Export both CSVs only (no DB writes):
    python scripts/backfill_severity.py

    # Also backfill severity into both databases:
    python scripts/backfill_severity.py --backfill

    # Backfill only one channel:
    python scripts/backfill_severity.py --backfill chat
    python scripts/backfill_severity.py --backfill audio

    # Preview DB writes without committing:
    python scripts/backfill_severity.py --backfill --dry-run
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.db import get_connection, DB_PATH                    # noqa: E402
from store.audio_db import (                                    # noqa: E402
    get_audio_connection,
    AUDIO_DB_PATH,
    initialise_audio_db,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = PROJECT_ROOT / "exports"

# Categories that, when entirely absent from a session, satisfy LOW rule L4.
HARMFUL_CATEGORIES = {
    "NSFW",
    "NSFW_EXPLICIT",
    "NSFW_GROOMING",
    "NSFW_APPEARANCE",
    "CSAM_RISK",
    "VIOLENCE",
    "ABUSIVE_LANGUAGE",
    "SELF_HARM",
}


# --------------------------------------------------------------------------- #
# Severity classification (pure logic, DB-agnostic)
# --------------------------------------------------------------------------- #
def classify_severity(flags: list[tuple[str, bool]]) -> tuple[str, str]:
    """Return ``(severity, rule)`` for one session.

    ``flags`` is a list of ``(category_code, is_astrologer)`` for the session's
    active flags. ``rule`` is the matched rule id (e.g. 'H1', 'L3', '-') and is
    returned for transparency/auditing.

    A session with no active flags is CLEAN (nothing to grade).
    """
    total = len(flags)
    if total == 0:                                             # no active flags
        return "CLEAN", "no_flags"

    astro_count = sum(1 for _, is_astro in flags if is_astro)
    categories = {cat for cat, _ in flags}

    csam_astro = sum(1 for cat, a in flags if cat == "CSAM_RISK" and a)
    nsfwx_total = sum(1 for cat, _ in flags if cat == "NSFW_EXPLICIT")
    nsfwx_astro = sum(1 for cat, a in flags if cat == "NSFW_EXPLICIT" and a)

    # ---- HIGH (checked first; wins over LOW) ----
    if csam_astro >= 3:                                          # H1
        return "HIGH", "H1"
    if nsfwx_astro >= 5:                                         # H2
        return "HIGH", "H2"
    if (                                                        # H3
        nsfwx_total >= 1
        and any(cat != "NSFW_EXPLICIT" for cat, _ in flags)      # co-occurrence
        and total >= 20
        and nsfwx_astro >= 2
    ):
        return "HIGH", "H3"

    # ---- LOW ----
    if total >= 1 and astro_count == 0:                         # L1: user-only
        return "LOW", "L1"
    if total >= 10 and astro_count <= 3:                        # L2
        return "LOW", "L2"
    if total <= 5:                                             # L3
        return "LOW", "L3"
    if not (categories & HARMFUL_CATEGORIES):                   # L4
        return "LOW", "L4"

    # ---- MEDIUM (default bucket) ----
    return "MEDIUM", "-"


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #
CHAT_FLAGS_SQL = """
    SELECT f.session_id                                       AS session_id,
           f.category_code                                    AS category,
           CASE WHEN t.speaker = 'ASTROLOGER' THEN 1 ELSE 0 END AS is_astro
    FROM flags f
    LEFT JOIN turns t
           ON t.session_id = f.session_id AND t.turn_id = f.turn_id
    WHERE (f.status IS NULL OR f.status != 'DISMISSED')
      AND f.flag_id NOT IN (
          SELECT parent_flag_id FROM flags WHERE parent_flag_id IS NOT NULL
      )
"""

# Audio flags carry no speaker: resolve seg_id -> segment label -> role.
# Labels are ranked by their numeric part (SPEAKER_1 before SPEAKER_2);
# 1st -> speaker1_role, 2nd -> speaker2_role.
AUDIO_FLAGS_SQL = """
    WITH ranked_labels AS (
        SELECT s_id, speaker AS label,
               ROW_NUMBER() OVER (
                   PARTITION BY s_id
                   ORDER BY CAST(
                       CASE WHEN instr(speaker,'_') > 0
                            THEN substr(speaker, instr(speaker,'_')+1)
                            ELSE speaker END AS INTEGER), speaker
               ) AS rn
        FROM (SELECT DISTINCT s_id, speaker FROM audio_segments WHERE speaker IS NOT NULL)
    ),
    seg_role AS (
        SELECT seg.s_id, seg.seg_id,
               CASE rl.rn WHEN 1 THEN ss.speaker1_role
                          WHEN 2 THEN ss.speaker2_role
                          ELSE NULL END AS role
        FROM audio_segments seg
        JOIN audio_sessions ss ON ss.s_id = seg.s_id
        LEFT JOIN ranked_labels rl ON rl.s_id = seg.s_id AND rl.label = seg.speaker
    ),
    active_flags AS (
        SELECT af.s_id, af.flag_id, af.seg_id, af.intent
        FROM audio_flags af
        WHERE (af.status IS NULL OR af.status != 'DISMISSED')
          AND af.flag_id NOT IN (
              SELECT parent_flag_id FROM audio_flags WHERE parent_flag_id IS NOT NULL
          )
    )
    SELECT f.s_id                                             AS session_id,
           f.intent                                           AS category,
           CASE WHEN sr.role = 'ASTROLOGER' THEN 1 ELSE 0 END AS is_astro
    FROM active_flags f
    LEFT JOIN seg_role sr ON sr.s_id = f.s_id AND sr.seg_id = f.seg_id
"""


def _severity_by_session(conn, all_ids_sql, flags_sql) -> dict:
    """Return ``{session_id: (severity, rule)}`` for every session in a DB.

    Sessions with no active flags are included (they classify as LOW).
    """
    flags_by_session: dict = {}
    for row in conn.execute(flags_sql):
        flags_by_session.setdefault(row["session_id"], []).append(
            (row["category"], bool(row["is_astro"]))
        )

    result: dict = {}
    for row in conn.execute(all_ids_sql):
        sid = row[0]
        result[sid] = classify_severity(flags_by_session.get(sid, []))
    return result


def compute_chat() -> dict:
    conn = get_connection()
    try:
        return _severity_by_session(
            conn, "SELECT session_id FROM sessions", CHAT_FLAGS_SQL
        )
    finally:
        conn.close()


def compute_audio() -> dict:
    conn = get_audio_connection()
    try:
        return _severity_by_session(
            conn, "SELECT s_id FROM audio_sessions", AUDIO_FLAGS_SQL
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def _sort_key(sid):
    """Sort numeric ids numerically, everything else as text."""
    return (0, int(sid)) if str(sid).isdigit() else (1, str(sid))


def write_csv(severities: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["session_id", "severity"])
        for sid in sorted(severities, key=_sort_key):
            writer.writerow([sid, severities[sid][0]])


def _distribution(severities: dict) -> str:
    counts = Counter(sev for sev, _ in severities.values())
    order = ["HIGH", "MEDIUM", "LOW", "CLEAN"]
    return ", ".join(f"{k}={counts.get(k, 0)}" for k in order)


def backfill_chat(severities: dict, dry_run: bool) -> int:
    updates = [(sev, sid) for sid, (sev, _) in severities.items()]
    if dry_run:
        return len(updates)
    conn = get_connection()
    try:
        conn.executemany(
            "UPDATE sessions SET astrotalk_severity = ? WHERE session_id = ?",
            updates,
        )
        conn.commit()
    finally:
        conn.close()
    return len(updates)


def backfill_audio(severities: dict, dry_run: bool) -> int:
    updates = [(sev, sid) for sid, (sev, _) in severities.items()]
    if dry_run:
        return len(updates)
    # Ensure the astrotalk_severity column exists (idempotent migration).
    initialise_audio_db()
    conn = get_audio_connection()
    try:
        conn.executemany(
            "UPDATE audio_sessions SET astrotalk_severity = ? WHERE s_id = ?",
            updates,
        )
        conn.commit()
    finally:
        conn.close()
    return len(updates)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Derive session severity from the Severity Criteria, "
                    "export CSVs, and optionally backfill the databases."
    )
    parser.add_argument(
        "--backfill",
        nargs="?",
        const="both",
        choices=["both", "chat", "audio"],
        default=None,
        help="Write severity into the DB(s). Bare --backfill writes both; "
             "pass 'chat' or 'audio' to scope it. Omit to only export CSVs.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="Directory for the CSV exports (default: exports/).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --backfill, preview the row counts without writing.",
    )
    args = parser.parse_args()

    print("=" * 64)
    print("  Session severity (Severity Criteria)")
    print(f"  CHAT  DB: {DB_PATH}")
    print(f"  AUDIO DB: {AUDIO_DB_PATH}")
    print("=" * 64)

    chat = compute_chat()
    audio = compute_audio()
    print(f"  CHAT  sessions: {len(chat):>6,}  ({_distribution(chat)})")
    print(f"  AUDIO sessions: {len(audio):>6,}  ({_distribution(audio)})")

    # CSV export always runs.
    chat_csv = args.out_dir / "severity_chat.csv"
    audio_csv = args.out_dir / "severity_audio.csv"
    write_csv(chat, chat_csv)
    write_csv(audio, audio_csv)
    print("\n  Exported:")
    print(f"    {chat_csv}")
    print(f"    {audio_csv}")

    # Optional DB backfill.
    if args.backfill:
        tag = " (DRY RUN)" if args.dry_run else ""
        print(f"\n  Backfilling severity into DB{tag}:")
        if args.backfill in ("both", "chat"):
            n = backfill_chat(chat, args.dry_run)
            print(f"    chat  : {n:>6,} sessions -> sessions.astrotalk_severity")
        if args.backfill in ("both", "audio"):
            n = backfill_audio(audio, args.dry_run)
            print(f"    audio : {n:>6,} sessions -> audio_sessions.astrotalk_severity")

    print("\nDone.")


if __name__ == "__main__":
    main()
