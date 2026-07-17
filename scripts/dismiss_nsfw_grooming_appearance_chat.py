"""
dismiss_nsfw_grooming_appearance_chat.py

Cleanup for the chat review database: dismiss every active flag on sessions
whose active visible flags are only NSFW_GROOMING and NSFW_APPEARANCE, both
types are present, and the active visible flag count is <= 5.

"Active visible flag" means the amendment row if a flag was edited, otherwise
the original row; flags with status='DISMISSED' are ignored. Dismissal is soft:
matching flag rows are updated to status='DISMISSED', review_log rows are
written, and the session verdict is recomputed while ignoring dismissed flags.

DRY-RUN BY DEFAULT — running with no flags only previews what would change.
Pass --commit to actually dismiss.

Usage:
  python scripts/dismiss_nsfw_grooming_appearance_chat.py
  python scripts/dismiss_nsfw_grooming_appearance_chat.py --commit
  python scripts/dismiss_nsfw_grooming_appearance_chat.py --status ALL --commit
"""

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.verdict_rules import (  # noqa: E402
    get_db_confidence_for_verdict,
    get_db_verdict_for_flags,
)
from store.db import DB_PATH, get_connection  # noqa: E402


TARGET_CATEGORIES = {"NSFW_GROOMING", "NSFW_APPEARANCE"}
MAX_ACTIVE_FLAGS = 5
DEFAULT_STATUSES = ("PENDING", "SUBMITTED_FOR_REVIEW", "LOCKED")
REVIEWER_ID = "AUTO_DISMISS"
NOTE = "Auto-dismiss: only NSFW_GROOMING + NSFW_APPEARANCE, <= 5 active flags"
SAMPLE_LIMIT = 30


def normalise(code: str) -> str:
    return (code or "").strip().upper().replace("-", "_").replace(" ", "_")


def parse_statuses(raw: str):
    if str(raw).strip().upper() == "ALL":
        return None
    return tuple(s.strip().upper() for s in raw.split(",") if s.strip())


def active_flag_rows(rows) -> list:
    amended_parents = {r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None}
    return [
        r for r in rows
        if r["parent_flag_id"] is not None or r["flag_id"] not in amended_parents
    ]


def recompute_chat_session_verdict(session_id: str, conn) -> str:
    rows = conn.execute(
        """SELECT flag_id, category_code, status, parent_flag_id
           FROM flags WHERE session_id = ?""",
        (session_id,),
    ).fetchall()
    active_codes = [
        r["category_code"] for r in active_flag_rows(rows)
        if (r["status"] or "") != "DISMISSED"
    ]
    verdict = get_db_verdict_for_flags(active_codes)
    confidence = get_db_confidence_for_verdict(verdict)
    conn.execute(
        "UPDATE sessions SET overall_verdict = ?, confidence_score = ? WHERE session_id = ?",
        (verdict, confidence, session_id),
    )
    return verdict


def find_targets(conn, statuses):
    params = []
    if statuses:
        status_clause = "WHERE s.review_status IN (%s)" % ",".join("?" for _ in statuses)
        params.extend(statuses)
    else:
        status_clause = ""

    rows_by_session = defaultdict(list)
    status_by_session = {}
    for row in conn.execute(
        f"""
        SELECT f.flag_id, f.session_id, f.parent_flag_id, f.category_code,
               f.status, s.review_status
        FROM flags f
        JOIN sessions s ON s.session_id = f.session_id
        {status_clause}
        ORDER BY f.session_id, f.flag_id
        """,
        params,
    ).fetchall():
        rows_by_session[row["session_id"]].append(row)
        status_by_session[row["session_id"]] = row["review_status"] or "-"

    targets = []
    skipped = Counter()

    for session_id, rows in rows_by_session.items():
        live = [
            r for r in active_flag_rows(rows)
            if (r["status"] or "") != "DISMISSED"
        ]
        if not live:
            skipped["no_active_flags"] += 1
            continue
        if len(live) > MAX_ACTIVE_FLAGS:
            skipped["more_than_5_active_flags"] += 1
            continue

        categories = {normalise(r["category_code"]) for r in live}
        if not categories.issubset(TARGET_CATEGORIES):
            skipped["has_other_category"] += 1
            continue
        if not TARGET_CATEGORIES.issubset(categories):
            skipped["missing_required_category"] += 1
            continue

        targets.append({
            "session_id": session_id,
            "review_status": status_by_session.get(session_id, "-"),
            "flag_ids": [r["flag_id"] for r in live],
            "categories": sorted(categories),
            "flag_count": len(live),
        })

    targets.sort(key=lambda t: t["session_id"])
    return targets, skipped


def print_preview(targets, skipped: Counter) -> None:
    total_flags = sum(t["flag_count"] for t in targets)
    print(f"Found {len(targets):,} qualifying chat session(s) "
          f"({total_flags:,} active flag(s) to dismiss).")
    if targets:
        print()
        print(f"  {'SESSION':<18} {'STATUS':<24} {'FLAGS':>5} {'CATEGORIES'}")
        print(f"  {'-'*18} {'-'*24} {'-'*5} {'-'*38}")
        for t in targets[:SAMPLE_LIMIT]:
            print(
                f"  {t['session_id']:<18} {t['review_status']:<24} "
                f"{t['flag_count']:>5} {','.join(t['categories'])}"
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
        "no_active_flags",
        "more_than_5_active_flags",
        "has_other_category",
        "missing_required_category",
    ):
        print(f"    {key:<32} {skipped[key]:>6,}")
    print()


def run(commit: bool, statuses) -> int:
    conn = get_connection()
    try:
        targets, skipped = find_targets(conn, statuses)
        print_preview(targets, skipped)

        if not commit:
            print(f"DRY RUN — would dismiss {sum(t['flag_count'] for t in targets):,} "
                  f"flag(s) across {len(targets):,} session(s), then recompute "
                  "their verdicts. No changes written. Re-run with --commit to apply.")
            return len(targets)

        if not targets:
            print("Nothing to dismiss.")
            return 0

        with conn:
            for target in targets:
                for flag_id in target["flag_ids"]:
                    conn.execute(
                        "UPDATE flags SET status = 'DISMISSED' WHERE flag_id = ?",
                        (flag_id,),
                    )
                    conn.execute(
                        """INSERT INTO review_log (session_id, flag_id, action, reviewer_id, note)
                           VALUES (?, ?, 'DISMISSED', ?, ?)""",
                        (target["session_id"], flag_id, REVIEWER_ID, NOTE),
                    )
                recompute_chat_session_verdict(target["session_id"], conn)

        print(f"Done. Dismissed {sum(t['flag_count'] for t in targets):,} flag(s) "
              f"across {len(targets):,} chat session(s).")
        return len(targets)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dismiss chat sessions with only NSFW grooming + appearance flags, <= 5 active flags."
    )
    parser.add_argument("--commit", action="store_true",
                        help="Actually dismiss (default is dry-run preview only).")
    parser.add_argument("--status", default=",".join(DEFAULT_STATUSES),
                        help="Comma-separated review_status values to clean. "
                             "Default: PENDING,SUBMITTED_FOR_REVIEW,LOCKED. "
                             "Pass 'ALL' to consider every status.")
    args = parser.parse_args()

    statuses = parse_statuses(args.status)

    print("=" * 78)
    print("  Dismiss NSFW_GROOMING + NSFW_APPEARANCE chat sessions")
    print("=" * 78)
    print(f"  Database   : {DB_PATH}")
    print(f"  Categories : {', '.join(sorted(TARGET_CATEGORIES))}")
    print(f"  Max flags  : {MAX_ACTIVE_FLAGS}")
    print(f"  Scope      : {', '.join(statuses) if statuses else 'ALL statuses'}")
    print(f"  Mode       : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    run(commit=args.commit, statuses=statuses)


if __name__ == "__main__":
    main()
