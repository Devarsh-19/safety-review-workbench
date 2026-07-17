"""
confirm_lock_aggressive_abusive_violence_audio.py

For the audio review database: confirm and lock sessions whose active flags are
only ABUSIVE_LANGUAGE / VIOLENCE, with at least 2 active flags total, where every
active flag has confidence >= 0.85 and sits on an AGGRESSIVE, DISTRESSED, or
ANGRY segment.

"Active flag" means the amendment row if a flag was edited, otherwise the
original row. DISMISSED rows are ignored. Already-confirmed rows can qualify and
are left confirmed; unconfirmed qualifying rows are marked CONFIRMED before the
session is locked.

DRY-RUN BY DEFAULT — running with no flags only previews what would change.
Pass --commit to confirm and lock.

Usage:
  python scripts/confirm_lock_aggressive_abusive_violence_audio.py
  python scripts/confirm_lock_aggressive_abusive_violence_audio.py --commit
  python scripts/confirm_lock_aggressive_abusive_violence_audio.py --status ALL --commit
  python scripts/confirm_lock_aggressive_abusive_violence_audio.py --locked-by Amogh --commit
"""

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import AUDIO_DB_PATH, get_audio_connection  # noqa: E402


TARGET_INTENTS = {"ABUSIVE_LANGUAGE", "VIOLENCE"}
TARGET_TONES = {"AGGRESSIVE", "DISTRESSED", "ANGRY"}
MIN_FLAGS = 2
MIN_CONFIDENCE = 0.85
DEFAULT_STATUSES = ("PENDING", "SUBMITTED_FOR_REVIEW")
REVIEWER_ID = "AUTO_CONFIRM_LOCK"
NOTE = (
    "Auto-confirm+lock: abusive/violence flags, conf >= 0.85, "
    "aggressive/distressed/angry tone"
)
SAMPLE_LIMIT = 30


def normalise(value: str) -> str:
    return (value or "").strip().upper().replace("-", "_").replace(" ", "_")


def parse_statuses(raw: str):
    if str(raw).strip().upper() == "ALL":
        return None
    return tuple(s.strip().upper() for s in raw.split(",") if s.strip())


def active_audio_flag_rows(rows) -> list:
    amended_parents = {r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None}
    return [
        r for r in rows
        if r["parent_flag_id"] is not None or r["flag_id"] not in amended_parents
    ]


def fetch_candidate_rows(conn, statuses):
    params = []
    if statuses:
        status_clause = "WHERE s.review_status IN (%s)" % ",".join("?" for _ in statuses)
        params.extend(statuses)
    else:
        status_clause = ""

    return conn.execute(
        f"""
        SELECT f.flag_id, f.s_id, f.parent_flag_id, f.intent, f.conf, f.status,
               f.seg_id, seg.tone, s.review_status
        FROM audio_flags f
        JOIN audio_sessions s ON s.s_id = f.s_id
        LEFT JOIN audio_segments seg ON seg.s_id = f.s_id AND seg.seg_id = f.seg_id
        {status_clause}
        ORDER BY f.s_id, f.flag_id
        """,
        params,
    ).fetchall()


def find_targets(conn, statuses, min_confidence: float):
    rows_by_sid = defaultdict(list)
    for row in fetch_candidate_rows(conn, statuses):
        rows_by_sid[row["s_id"]].append(row)

    targets = []
    skipped = Counter()

    for s_id, rows in rows_by_sid.items():
        active = [
            r for r in active_audio_flag_rows(rows)
            if (r["status"] or "") != "DISMISSED"
        ]
        if len(active) < MIN_FLAGS:
            skipped["fewer_than_2_active_flags"] += 1
            continue

        intents = {normalise(r["intent"]) for r in active}
        if not intents.issubset(TARGET_INTENTS):
            skipped["has_other_intent"] += 1
            continue
        if not TARGET_INTENTS.issubset(intents):
            skipped["missing_required_intent"] += 1
            continue

        if any(r["conf"] is None or r["conf"] < min_confidence for r in active):
            skipped["low_or_missing_confidence"] += 1
            continue

        tones = {normalise(r["tone"]) for r in active}
        if not tones.issubset(TARGET_TONES):
            skipped["tone_not_allowed_or_missing"] += 1
            continue

        targets.append({
            "s_id": s_id,
            "review_status": active[0]["review_status"] or "-",
            "flag_ids": [r["flag_id"] for r in active],
            "to_confirm_ids": [
                r["flag_id"] for r in active
                if (r["status"] or "") != "CONFIRMED"
            ],
            "intents": sorted(intents),
            "tones": sorted(tones),
            "min_conf": min(r["conf"] for r in active),
            "flag_count": len(active),
        })

    targets.sort(key=lambda t: t["s_id"])
    return targets, skipped


def print_preview(targets, skipped: Counter, min_confidence: float) -> None:
    print(f"Found {len(targets):,} qualifying audio session(s).")
    if targets:
        total_flags = sum(t["flag_count"] for t in targets)
        to_confirm = sum(len(t["to_confirm_ids"]) for t in targets)
        print(f"  Active flags in qualifying sessions : {total_flags:,}")
        print(f"  Flags needing confirmation          : {to_confirm:,}")
        print()
        print(f"  {'SESSION':<12} {'STATUS':<22} {'FLAGS':>5} {'MIN CONF':>8} {'INTENTS':<32} {'TONES'}")
        print(f"  {'-'*12} {'-'*22} {'-'*5} {'-'*8} {'-'*32} {'-'*28}")
        for t in targets[:SAMPLE_LIMIT]:
            print(
                f"  {t['s_id']:<12} {t['review_status']:<22} {t['flag_count']:>5} "
                f"{t['min_conf']:>8.2f} {','.join(t['intents'])[:32]:<32} "
                f"{','.join(t['tones'])}"
            )
        if len(targets) > SAMPLE_LIMIT:
            print(f"  ... {len(targets) - SAMPLE_LIMIT:,} more")
        print()

    status_counts = Counter(t["review_status"] for t in targets)
    if status_counts:
        print("  Qualifying sessions by review_status:")
        for status, count in sorted(status_counts.items()):
            print(f"    {status:<24} {count:>6,}")
        print()

    print("  Skipped session counts:")
    for key in (
        "fewer_than_2_active_flags",
        "has_other_intent",
        "missing_required_intent",
        "low_or_missing_confidence",
        "tone_not_allowed_or_missing",
    ):
        print(f"    {key:<32} {skipped[key]:>6,}")
    print()
    print(f"  Criteria: intents={sorted(TARGET_INTENTS)}, flags>={MIN_FLAGS}, "
          f"conf>={min_confidence}, tones={sorted(TARGET_TONES)}")
    print()


def apply_changes(conn, targets, locked_by: str) -> None:
    for target in targets:
        for flag_id in target["to_confirm_ids"]:
            conn.execute(
                """UPDATE audio_flags
                   SET status = 'CONFIRMED',
                       confirmed_by = ?,
                       confirmed_at = datetime('now')
                   WHERE flag_id = ?""",
                (locked_by, flag_id),
            )
            conn.execute(
                """INSERT INTO audio_review_log (s_id, flag_id, action, reviewer_id, note)
                   VALUES (?, ?, 'CONFIRM_FLAG', ?, ?)""",
                (target["s_id"], flag_id, locked_by, NOTE),
            )

        conn.execute(
            """UPDATE audio_sessions
               SET review_status = 'LOCKED',
                   locked_by = ?,
                   locked_at = datetime('now')
               WHERE s_id = ?""",
            (locked_by, target["s_id"]),
        )
        conn.execute(
            """INSERT INTO audio_review_log (s_id, action, reviewer_id, note)
               VALUES (?, 'LOCK', ?, ?)""",
            (target["s_id"], locked_by, NOTE),
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Confirm and lock high-confidence aggressive abusive/violence audio sessions."
    )
    parser.add_argument("--commit", action="store_true",
                        help="Actually confirm and lock (default is dry-run preview only).")
    parser.add_argument("--locked-by", default=REVIEWER_ID,
                        help=f"Reviewer id/name for confirmed_by and locked_by (default: {REVIEWER_ID}).")
    parser.add_argument("--min-confidence", type=float, default=MIN_CONFIDENCE,
                        help=f"Minimum flag confidence required (default: {MIN_CONFIDENCE}).")
    parser.add_argument("--status", default=",".join(DEFAULT_STATUSES),
                        help="Comma-separated review_status values to consider. "
                             "Default: PENDING,SUBMITTED_FOR_REVIEW,LOCKED. "
                             "Pass 'ALL' for every status.")
    args = parser.parse_args()

    statuses = parse_statuses(args.status)

    print("=" * 78)
    print("  Confirm + lock aggressive abusive/violence audio sessions")
    print("=" * 78)
    print(f"  Database   : {AUDIO_DB_PATH}")
    print(f"  Scope      : {', '.join(statuses) if statuses else 'ALL statuses'}")
    print(f"  Locked by  : {args.locked_by}")
    print(f"  Mode       : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    conn = get_audio_connection()
    try:
        targets, skipped = find_targets(conn, statuses, args.min_confidence)
        print_preview(targets, skipped, args.min_confidence)

        if not args.commit:
            print(f"DRY RUN — would confirm/lock {len(targets):,} audio session(s). "
                  "No changes written. Re-run with --commit to apply.")
            return

        if not targets:
            print("Nothing to confirm or lock.")
            return

        with conn:
            apply_changes(conn, targets, args.locked_by)
        print(f"Done. Confirmed flags and locked {len(targets):,} audio session(s).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
