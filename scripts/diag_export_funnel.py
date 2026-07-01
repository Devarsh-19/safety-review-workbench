"""
diag_export_funnel.py

Diagnostic: shows WHY export_manual_flagged_astrotalk_clean.py returns the count it
does, by printing each filter stage as a funnel against the real DB. Read-only.

Usage:
  python scripts/diag_export_funnel.py
"""

import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.db import get_connection  # noqa: E402

# Keep in sync with export_manual_flagged_astrotalk_clean.py
EXCLUDED_CATEGORY = "re_engagement_solicitation"
DROP_ONLY_CATEGORIES = {
    "fake_remedies",
    "personal_data_collection",
    "instigation",
    "fear_manipulation",
    "financial_solicitation",
    "off_platform_solicitation",
}


def _norm(code):
    if not code:
        return ""
    return code.strip().lower().replace("-", "_").replace(" ", "_")


def _scalar(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


def _funnel(conn, astro_val):
    """Run the export funnel for a given astrotalk_flagged value. Returns a dict of
    stage counts + the low-signal category breakdown among dropped sessions."""
    scope = "review_status IN ('LOCKED','SUBMITTED_FOR_REVIEW')"
    active_not_parent = ("f.flag_id NOT IN "
                         "(SELECT parent_flag_id FROM flags WHERE parent_flag_id IS NOT NULL)")

    n_astro = _scalar(
        conn, f"SELECT COUNT(*) FROM sessions WHERE {scope} AND astrotalk_flagged=?",
        (astro_val,),
    )

    # Stage: sessions with an active, non-re-engagement MANUAL/LLM flag (pre-drop).
    pre_drop = conn.execute(
        f"""SELECT DISTINCT s.session_id FROM sessions s
            JOIN flags f ON f.session_id = s.session_id
            WHERE f.source IN ('MANUAL','LLM')
              AND s.astrotalk_flagged = ?
              AND s.{scope}
              AND LOWER(REPLACE(REPLACE(f.category_code,'-','_'),' ','_')) != ?
              AND {active_not_parent}""",
        (astro_val, EXCLUDED_CATEGORY),
    ).fetchall()
    pre_drop_ids = {r["session_id"] for r in pre_drop}

    # Per-session active, non-re-engagement category set (all sources), for the drop test.
    kept, dropped = set(), set()
    drop_cat_counter = Counter()
    if pre_drop_ids:
        ph = ",".join("?" * len(pre_drop_ids))
        rows = conn.execute(
            f"""SELECT f.session_id, f.category_code, f.flag_id, f.parent_flag_id
                FROM flags f WHERE f.session_id IN ({ph})""",
            tuple(pre_drop_ids),
        ).fetchall()
        amended_parents = {r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None}
        cats = {}
        for r in rows:
            is_active = (r["parent_flag_id"] is not None) or (r["flag_id"] not in amended_parents)
            norm = _norm(r["category_code"])
            if is_active and norm and norm != EXCLUDED_CATEGORY:
                cats.setdefault(r["session_id"], set()).add(norm)
        for sid in pre_drop_ids:
            c = cats.get(sid, set())
            if c and c.issubset(DROP_ONLY_CATEGORIES):
                dropped.add(sid)
                for cat in c:
                    drop_cat_counter[cat] += 1
            else:
                kept.add(sid)

    return {
        "n_astro":       n_astro,
        "pre_drop":      len(pre_drop_ids),
        "dropped":       len(dropped),
        "kept":          len(kept),
        "drop_counter":  drop_cat_counter,
    }


def _print_funnel(title, astro_label, f):
    print("-" * 64)
    print(f"  {title}")
    print("-" * 64)
    print(f"  {astro_label:<44} : {f['n_astro']:>8,}")
    print(f"  + has active MANUAL/LLM non-reeng flag       : {f['pre_drop']:>8,}")
    print(f"  - dropped (flags ALL low-signal)             : {f['dropped']:>8,}")
    print(f"  = FINAL EXPORT COUNT                         : {f['kept']:>8,}")
    if f["drop_counter"]:
        print("  Categories among dropped sessions (low-signal only):")
        for cat, n in f["drop_counter"].most_common():
            print(f"    {cat:<30} {n:>8,}")
    print()


def main():
    conn = get_connection()

    scope = "review_status IN ('LOCKED','SUBMITTED_FOR_REVIEW')"
    n_submitted = _scalar(conn, "SELECT COUNT(*) FROM sessions WHERE review_status='SUBMITTED_FOR_REVIEW'")
    n_locked    = _scalar(conn, "SELECT COUNT(*) FROM sessions WHERE review_status='LOCKED'")
    n_scope     = _scalar(conn, f"SELECT COUNT(*) FROM sessions WHERE {scope}")

    clean   = _funnel(conn, 0)   # AstroTalk did NOT flag  (the actual export set)
    flagged = _funnel(conn, 1)   # AstroTalk DID flag      (comparison)
    conn.close()

    print("=" * 64)
    print("  Export funnel - manual/LLM-flagged, by astrotalk_flagged")
    print("=" * 64)
    print(f"  SUBMITTED_FOR_REVIEW                         : {n_submitted:>8,}")
    print(f"  LOCKED                                       : {n_locked:>8,}")
    print(f"  Scope (submitted + locked)                   : {n_scope:>8,}")
    print()
    _print_funnel("astrotalk_flagged = 0  (EXPORTED: AstroTalk missed it)",
                  "Scope + astrotalk_flagged = 0", clean)
    _print_funnel("astrotalk_flagged = 1  (comparison: AstroTalk flagged it)",
                  "Scope + astrotalk_flagged = 1", flagged)


if __name__ == "__main__":
    main()
