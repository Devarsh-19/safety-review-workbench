"""
backfill_flag_severity.py

Corrects the severity on existing flags so it matches the flag's category:
  SEVERE category  -> HIGH
  FLAGGED category -> MEDIUM
  CLEAN            -> LOW

Older manual flags were created with a hardcoded severity = 'MEDIUM' regardless
of category, so a manually-tagged NSFW (a SEVERE category) showed MEDIUM. This
one-off script updates only the `severity` column to the correct level.

Usage:
  python scripts/backfill_flag_severity.py --dry-run
  python scripts/backfill_flag_severity.py
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.db import get_connection                       # noqa: E402
from engine.verdict_rules import get_db_verdict_for_flags  # noqa: E402


def _severity_for_category(category_code: str) -> str:
    verdict = get_db_verdict_for_flags([category_code])
    return {"SEVERE": "HIGH", "FLAGGED": "MEDIUM", "CLEAN": "LOW"}.get(verdict, "MEDIUM")


def backfill(dry_run: bool = False) -> None:
    conn = get_connection()
    rows = conn.execute(
        "SELECT flag_id, category_code, severity FROM flags ORDER BY flag_id"
    ).fetchall()

    print(f"Found {len(rows)} flags")
    if not rows:
        conn.close()
        return

    updates: list[tuple[str, int]] = []   # (new_severity, flag_id)
    for r in rows:
        correct = _severity_for_category(r["category_code"])
        if (r["severity"] or "").upper() != correct:
            updates.append((correct, r["flag_id"]))

    print(f"  Need correction : {len(updates)}")
    print(f"  Already correct : {len(rows) - len(updates)}")

    if updates:
        print("\n  Changes:")
        for sev, fid in updates[:30]:
            cat = next(r["category_code"] for r in rows if r["flag_id"] == fid)
            old = next(r["severity"] for r in rows if r["flag_id"] == fid)
            print(f"    flag {fid} ({cat}): {old} -> {sev}")
        if len(updates) > 30:
            print(f"    ... and {len(updates) - 30} more")

    if dry_run:
        print("\nDRY RUN — no changes written.")
        conn.close()
        return

    if updates:
        conn.executemany(
            "UPDATE flags SET severity = ? WHERE flag_id = ?",
            updates,
        )
        conn.commit()
    print(f"\nDone. Updated {len(updates)} flags.")
    conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Backfill flag severity from category")
    p.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = p.parse_args()
    print("=" * 55)
    print("  Backfill flag severity from category")
    print(f"  DB: {os.getenv('DB_PATH', 'store/astrotalk.db')}")
    print("=" * 55)
    backfill(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
