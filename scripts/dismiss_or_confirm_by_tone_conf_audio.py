"""
dismiss_or_confirm_by_tone_conf_audio.py

For the audio review database (store/audio_review.db): process PENDING audio
sessions by applying the following cascading rules to each active flag:

  1. DISMISS: any flag with confidence <= 0.90, regardless of tone.
     (Both neutral/professional/calm-toned AND other-toned low-confidence flags
      are dismissed — they are all considered unreliable at this threshold.)

  2. CONFIRM: all remaining active flags (i.e. conf > 0.90).

After processing every flag in a session:
  - Recompute the session verdict.
  - If the session's audio_segments contain exactly ONE distinct speaker,
    set speaker1_role = 'USER' and LOCK the session.
  - If the session has > 1 distinct speaker, LOCK the session without changing
    speaker roles (roles remain as-is or NULL).

"Active flag" = the amendment row if a flag was edited, else the original;
DISMISSED rows and amended originals are ignored.

A flag with NULL conf is treated as low-confidence (dismissed).

SCOPE: by default only PENDING sessions are processed. Override with --status.

DRY-RUN BY DEFAULT — pass --commit to actually write changes.

Usage:
  python scripts/dismiss_or_confirm_by_tone_conf_audio.py             # preview
  python scripts/dismiss_or_confirm_by_tone_conf_audio.py --commit    # apply
  python scripts/dismiss_or_confirm_by_tone_conf_audio.py --status ALL --commit
  python scripts/dismiss_or_confirm_by_tone_conf_audio.py --locked-by Amogh --commit
"""

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import (  # noqa: E402
    AUDIO_DB_PATH,
    get_audio_connection,
    recompute_audio_session_verdict,
)

# ── Configuration ─────────────────────────────────────────────────────────────

CONF_THRESHOLD = 0.90          # flags with conf <= this value are dismissed
DEFAULT_STATUSES = ("PENDING",)
REVIEWER_ID = "AUTO_DISMISS_CONFIRM"
USER_ROLE = "USER"
SAMPLE_LIMIT = 40

NOTE_DISMISS = "Auto-dismiss: conf <= %.2f (low confidence)"
NOTE_CONFIRM = "Auto-confirm: conf > %.2f"
NOTE_LOCK_SINGLE = "Auto-lock: single-speaker session, speaker1 set to USER"
NOTE_LOCK_MULTI = "Auto-lock: all flags actioned"

# ── Helpers ───────────────────────────────────────────────────────────────────


def _norm(value: str) -> str:
    return (value or "").strip().upper()


def parse_statuses(raw: str):
    if str(raw).strip().upper() == "ALL":
        return None
    return tuple(s.strip().upper() for s in raw.split(",") if s.strip())


def active_audio_flag_rows(rows) -> list:
    """Active flags = amendment rows + original rows that have no amendment."""
    amended_parents = {r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None}
    return [
        r for r in rows
        if r["parent_flag_id"] is not None or r["flag_id"] not in amended_parents
    ]


# ── Data fetching ─────────────────────────────────────────────────────────────


def fetch_candidate_rows(conn, statuses):
    """All flags on sessions within the given status scope, with segment tone."""
    params = []
    if statuses:
        status_clause = "WHERE s.review_status IN (%s)" % ",".join("?" for _ in statuses)
        params.extend(statuses)
    else:
        status_clause = ""

    return conn.execute(
        f"""
        SELECT f.flag_id, f.s_id, f.parent_flag_id, f.intent, f.conf, f.status,
               f.seg_id, seg.tone, seg.speaker, s.review_status,
               s.speaker1_role, s.speaker2_role
        FROM audio_flags f
        JOIN audio_sessions s  ON s.s_id = f.s_id
        LEFT JOIN audio_segments seg ON seg.s_id = f.s_id AND seg.seg_id = f.seg_id
        {status_clause}
        ORDER BY f.s_id, f.flag_id
        """,
        params,
    ).fetchall()


def fetch_speaker_counts(conn):
    """Distinct speaker count per session."""
    return {
        r["s_id"]: r["nspk"]
        for r in conn.execute(
            "SELECT s_id, COUNT(DISTINCT speaker) AS nspk FROM audio_segments GROUP BY s_id"
        ).fetchall()
    }


# ── Planning ──────────────────────────────────────────────────────────────────


def build_plan(conn, statuses, conf_threshold):
    """For each qualifying session, determine which flags to dismiss/confirm
    and whether to lock + set speaker roles.

    Returns a dict {s_id: plan} where plan has:
      dismiss_ids  list[int]  — flag_ids to dismiss (conf <= threshold or NULL)
      confirm_ids  list[int]  — flag_ids to confirm (conf > threshold)
      speakers     int        — distinct speakers in session's segments
      set_role     bool       — whether to set speaker1_role = USER
      lock         bool       — whether to lock the session
      review_status str       — current review_status
    """
    rows_by_sid = defaultdict(list)
    for r in fetch_candidate_rows(conn, statuses):
        rows_by_sid[r["s_id"]].append(r)

    speaker_counts = fetch_speaker_counts(conn)
    plan = {}

    for s_id, rows in rows_by_sid.items():
        active = [
            r for r in active_audio_flag_rows(rows)
            if (r["status"] or "") != "DISMISSED"
        ]
        if not active:
            continue  # no active flags — skip

        # Skip flags already fully actioned (CONFIRMED or DISMISSED)
        unactioned = [r for r in active if (r["status"] or "") not in ("CONFIRMED", "DISMISSED")]
        if not unactioned:
            continue  # everything already actioned — skip

        dismiss_ids = []
        confirm_ids = []

        for r in unactioned:
            if r["conf"] is None or r["conf"] <= conf_threshold:
                # Rule 1: low confidence → dismiss
                dismiss_ids.append(r["flag_id"])
            else:
                # Rule 2: remaining (conf > threshold) → confirm
                confirm_ids.append(r["flag_id"])

        if not dismiss_ids and not confirm_ids:
            continue

        nspk = speaker_counts.get(s_id, 0)
        # Single speaker → set speaker1_role = USER
        set_role = nspk == 1 and _norm(rows[0]["speaker1_role"]) != USER_ROLE

        plan[s_id] = {
            "dismiss_ids": dismiss_ids,
            "confirm_ids": confirm_ids,
            "speakers": nspk,
            "set_role": set_role,
            "lock": True,  # always lock after processing
            "review_status": rows[0]["review_status"] or "-",
            "total_active": len(active),
        }

    return dict(sorted(plan.items()))


# ── Preview ───────────────────────────────────────────────────────────────────


def print_preview(plan, conf_threshold):
    print(f"Found {len(plan):,} session(s) to process.\n")
    if not plan:
        return

    total_dismiss = sum(len(p["dismiss_ids"]) for p in plan.values())
    total_confirm = sum(len(p["confirm_ids"]) for p in plan.values())
    total_lock = sum(1 for p in plan.values() if p["lock"])
    total_role = sum(1 for p in plan.values() if p["set_role"])
    single_spk = sum(1 for p in plan.values() if p["speakers"] == 1)

    print(f"  Flags to dismiss (conf <= {conf_threshold})   : {total_dismiss:,}")
    print(f"  Flags to confirm (conf > {conf_threshold})    : {total_confirm:,}")
    print(f"  Sessions to lock                   : {total_lock:,}")
    print(f"  Single-speaker sessions (→ USER)   : {single_spk:,}")
    print(f"  speaker1_role sets needed           : {total_role:,}")
    print()

    print(f"  {'SESSION':<12} {'STATUS':<22} {'SPK':<4} {'DISMISS':<8} "
          f"{'CONFIRM':<8} {'SET ROLE':<10} {'LOCK':<6}")
    print(f"  {'-'*12} {'-'*22} {'-'*4} {'-'*8} {'-'*8} {'-'*10} {'-'*6}")
    for i, (s_id, p) in enumerate(plan.items()):
        if i >= SAMPLE_LIMIT:
            print(f"  ... {len(plan) - SAMPLE_LIMIT:,} more")
            break
        print(
            f"  {s_id:<12} {p['review_status']:<22} {p['speakers']:<4} "
            f"{len(p['dismiss_ids']):<8} {len(p['confirm_ids']):<8} "
            f"{'yes' if p['set_role'] else '-':<10} {'yes' if p['lock'] else '-':<6}"
        )
    print()

    status_counts = Counter(p["review_status"] for p in plan.values())
    if status_counts:
        print("  Sessions by review_status:")
        for status, count in sorted(status_counts.items()):
            print(f"    {status:<24} {count:>6,}")
        print()


# ── Execution ─────────────────────────────────────────────────────────────────


def apply_changes(conn, plan, conf_threshold, actor):
    """Actually write the dismiss/confirm/role/lock changes."""
    for s_id, p in plan.items():

        # 1. Dismiss low-confidence flags
        for fid in p["dismiss_ids"]:
            conn.execute(
                "UPDATE audio_flags SET status = 'DISMISSED' WHERE flag_id = ?",
                (fid,),
            )
            conn.execute(
                """INSERT INTO audio_review_log (s_id, flag_id, action, reviewer_id, note)
                   VALUES (?, ?, 'DISMISSED', ?, ?)""",
                (s_id, fid, actor, NOTE_DISMISS % conf_threshold),
            )

        # 2. Confirm remaining flags
        for fid in p["confirm_ids"]:
            conn.execute(
                """UPDATE audio_flags
                   SET status = 'CONFIRMED', confirmed_by = ?, confirmed_at = datetime('now')
                   WHERE flag_id = ?""",
                (actor, fid),
            )
            conn.execute(
                """INSERT INTO audio_review_log (s_id, flag_id, action, reviewer_id, note)
                   VALUES (?, ?, 'CONFIRM_FLAG', ?, ?)""",
                (s_id, fid, actor, NOTE_CONFIRM % conf_threshold),
            )

        # 3. Recompute verdict after dismiss/confirm
        recompute_audio_session_verdict(s_id, conn)

        # 4. Single speaker → set speaker1_role = USER
        if p["set_role"]:
            conn.execute(
                "UPDATE audio_sessions SET speaker1_role = ? WHERE s_id = ?",
                (USER_ROLE, s_id),
            )
            conn.execute(
                """INSERT INTO audio_review_log (s_id, action, reviewer_id, note)
                   VALUES (?, 'SET_SPEAKER_ROLES', ?, ?)""",
                (s_id, actor, NOTE_LOCK_SINGLE),
            )

        # 5. Lock the session
        if p["lock"]:
            lock_note = NOTE_LOCK_SINGLE if p["speakers"] == 1 else NOTE_LOCK_MULTI
            conn.execute(
                """UPDATE audio_sessions
                   SET review_status = 'LOCKED', locked_by = ?, locked_at = datetime('now'),
                       reviewed_at = datetime('now')
                   WHERE s_id = ?""",
                (actor, s_id),
            )
            conn.execute(
                """INSERT INTO audio_review_log (s_id, action, reviewer_id, note)
                   VALUES (?, 'LOCK', ?, ?)""",
                (s_id, actor, lock_note),
            )


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dismiss low-confidence flags, confirm the rest, "
                    "set single-speaker as USER, and lock audio sessions."
    )
    parser.add_argument("--commit", action="store_true",
                        help="Actually write changes (default is dry-run preview only).")
    parser.add_argument("--locked-by", default=REVIEWER_ID,
                        help=f"Reviewer id/name for confirmed_by and locked_by "
                             f"(default: {REVIEWER_ID}).")
    parser.add_argument("--conf-threshold", type=float, default=CONF_THRESHOLD,
                        help=f"Confidence threshold: flags with conf <= this value "
                             f"are dismissed (default: {CONF_THRESHOLD}).")
    parser.add_argument("--status", default=",".join(DEFAULT_STATUSES),
                        help="Comma-separated review_status values to consider. "
                             "Default: PENDING. Pass 'ALL' for every status.")
    args = parser.parse_args()

    statuses = parse_statuses(args.status)

    print("=" * 78)
    print("  Dismiss low-conf / confirm rest / single-speaker→USER / lock (audio)")
    print("=" * 78)
    print(f"  Database       : {AUDIO_DB_PATH}")
    print(f"  Scope          : {', '.join(statuses) if statuses else 'ALL statuses'}")
    print(f"  Conf threshold : <= {args.conf_threshold} → dismiss, > {args.conf_threshold} → confirm")
    print(f"  Single speaker : speaker1_role = USER + lock")
    print(f"  Multi speaker  : lock (no role change)")
    print(f"  Locked by      : {args.locked_by}")
    print(f"  Mode           : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    conn = get_audio_connection()
    try:
        plan = build_plan(conn, statuses, args.conf_threshold)
        print_preview(plan, args.conf_threshold)

        if not args.commit:
            total_d = sum(len(p["dismiss_ids"]) for p in plan.values())
            total_c = sum(len(p["confirm_ids"]) for p in plan.values())
            print(f"DRY RUN — would dismiss {total_d:,} flag(s), confirm {total_c:,} flag(s), "
                  f"and lock {len(plan):,} session(s). No changes written. "
                  f"Re-run with --commit to apply.")
            return

        if not plan:
            print("Nothing to process.")
            return

        with conn:
            apply_changes(conn, plan, args.conf_threshold, args.locked_by)

        total_d = sum(len(p["dismiss_ids"]) for p in plan.values())
        total_c = sum(len(p["confirm_ids"]) for p in plan.values())
        total_r = sum(1 for p in plan.values() if p["set_role"])
        print(f"Done. Dismissed {total_d:,} flag(s), confirmed {total_c:,} flag(s), "
              f"set {total_r:,} speaker role(s), locked {len(plan):,} session(s).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
