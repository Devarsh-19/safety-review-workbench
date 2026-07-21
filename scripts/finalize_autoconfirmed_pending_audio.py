"""
finalize_autoconfirmed_pending_audio.py

Auto-finalize PENDING audio sessions (store/audio_review.db) that ALREADY carry
an auto-confirmed flag, using two independent rules.

A session is *eligible* only when it is PENDING and has at least one ACTIVE flag
that was auto-confirmed — status = 'CONFIRMED' and confirmed_by in the automated-
actor set (default {AUTO_CONFIRM, AUTO_CONFIRM_LOCK}, the markers left by the
other confirm_lock_* audio scripts). Override the set with --auto-actors.

For each eligible session:

  Rule A — single speaker:
      If the session's audio_segments contain exactly ONE distinct speaker, set
      speaker1_role = 'USER' and LOCK the session.

  Rule B — high-confidence hostile/flirtatious/unclear tone:
      If the session has OTHER active, not-yet-confirmed flags with
      conf >= 0.90 AND segment tone in
      {DISTRESSED, ANGRY, AGGRESSIVE, FLIRTATIOUS, UNCLEAR}, CONFIRM those flags
      and LOCK the session.

A session may satisfy A, B, both, or neither; it is locked if either rule fires.
An eligible session matching neither rule is left untouched. Locking is direct
(review_status -> 'LOCKED', locked_by / locked_at). Confirmed flags stay ACTIVE,
so a locked session's verdict remains FLAGGED.

"Active flag" = the amendment row if a flag was edited, else the original;
DISMISSED rows and amended originals are ignored (same rule as the rest of the
audio workbench). conf is compared with >= (inclusive); a NULL conf never
qualifies for Rule B. Tone comes from the flag's segment (audio_segments.tone),
matched case-insensitively; a flag with no segment/tone can't satisfy Rule B.

Writes follow the audio-script convention: CONFIRM_FLAG / LOCK / SET_SPEAKER_ROLES
rows in audio_review_log, confirmed_by / confirmed_at / locked_by / locked_at set.

SCOPE: PENDING sessions only. Other statuses are never touched.

DRY-RUN BY DEFAULT — running with no flags only previews what would change. Pass
--commit to actually write.

Usage:
  python scripts/finalize_autoconfirmed_pending_audio.py            # preview (dry-run)
  python scripts/finalize_autoconfirmed_pending_audio.py --commit   # apply
  python scripts/finalize_autoconfirmed_pending_audio.py --min-conf 0.9 --commit
  python scripts/finalize_autoconfirmed_pending_audio.py --actor Amogh --commit
"""

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import AUDIO_DB_PATH, get_audio_connection  # noqa: E402

REVIEW_STATUS = "PENDING"
# Actor id stamped by THIS script on the confirms/locks/role-sets it writes.
REVIEWER_ID = "AUTO_CONFIRM_LOCK"
# A flag counts as "auto-confirmed" (making its session eligible) when it is
# CONFIRMED by one of these automated actors — the markers the other audio
# confirm_lock_* scripts write. Override with --auto-actors.
AUTO_ACTORS = {"AUTO_CONFIRM", "AUTO_CONFIRM_LOCK"}

# Rule B gates.
RULE_B_MIN_CONF = 0.90
RULE_B_TONES = {"DISTRESSED", "ANGRY", "AGGRESSIVE", "FLIRTATIOUS", "UNCLEAR"}
USER_ROLE = "USER"

NOTE_CONFIRM = "Auto-confirm: conf>=%.2f flag on hostile/flirtatious/unclear tone (Rule B)"
NOTE_LOCK_A = "Auto-lock: single-speaker session with auto-confirmed flag (Rule A)"
NOTE_LOCK_B = "Auto-lock: auto-confirmed flag + high-confidence tone flags (Rule B)"
NOTE_ROLE = "Auto: single-speaker session, speaker1 set to USER (Rule A)"


def _norm_tone(tone) -> str:
    return (tone or "").strip().upper()


def active_audio_flag_rows(rows) -> list:
    """Active flags = amendment rows + original rows that have no amendment."""
    amended_parents = {r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None}
    return [
        r for r in rows
        if r["parent_flag_id"] is not None or r["flag_id"] not in amended_parents
    ]


def _fetch_flag_rows(conn):
    """Every flag on a PENDING session, with its segment tone and confirmer."""
    return conn.execute(
        f"""
        SELECT f.flag_id, f.s_id, f.parent_flag_id, f.intent, f.conf, f.status,
               f.confirmed_by, f.seg_id, seg.tone
        FROM audio_flags f
        JOIN audio_sessions s ON s.s_id = f.s_id
        LEFT JOIN audio_segments seg ON seg.s_id = f.s_id AND seg.seg_id = f.seg_id
        WHERE s.review_status = '{REVIEW_STATUS}'
        ORDER BY f.s_id, f.flag_id
        """
    ).fetchall()


def build_plan(conn, auto_actors, min_conf, tones):
    """Decide, per eligible PENDING session, what Rule A / Rule B would do.

    Returns a dict {s_id: plan} where plan has:
      rule_a       bool  — single-speaker
      set_role     bool  — speaker1_role needs setting to USER
      confirm_ids  list  — Rule B flag_ids to confirm
      rule_b       bool  — Rule B fired
      lock         bool  — session will be locked (A or B)
      remaining    int   — active, unactioned flags left AFTER Rule B confirms
    """
    rows_by_sid = defaultdict(list)
    for r in _fetch_flag_rows(conn):
        rows_by_sid[r["s_id"]].append(r)

    # Distinct speakers per session (0 if a session has no segments at all).
    speakers = {
        r["s_id"]: r["nspk"]
        for r in conn.execute(
            "SELECT s_id, COUNT(DISTINCT speaker) AS nspk FROM audio_segments GROUP BY s_id"
        ).fetchall()
    }
    # Current speaker1_role for PENDING sessions (to skip a no-op role write).
    role1 = {
        r["s_id"]: r["speaker1_role"]
        for r in conn.execute(
            f"SELECT s_id, speaker1_role FROM audio_sessions WHERE review_status = '{REVIEW_STATUS}'"
        ).fetchall()
    }

    plan = {}
    for s_id, rows in rows_by_sid.items():
        active = [r for r in active_audio_flag_rows(rows) if (r["status"] or "") != "DISMISSED"]

        # Eligibility: at least one ACTIVE, auto-confirmed flag.
        eligible = any(
            (r["status"] or "") == "CONFIRMED" and (r["confirmed_by"] or "") in auto_actors
            for r in active
        )
        if not eligible:
            continue

        # Rule A — exactly one distinct speaker.
        rule_a = speakers.get(s_id, 0) == 1
        set_role = rule_a and (role1.get(s_id) or "") != USER_ROLE

        # Rule B — other active, not-yet-confirmed flags: conf>=min AND tone-in-set.
        confirm_ids = [
            r["flag_id"] for r in active
            if (r["status"] or "") not in ("CONFIRMED", "DISMISSED")
            and r["conf"] is not None and r["conf"] >= min_conf
            and _norm_tone(r["tone"]) in tones
        ]
        rule_b = len(confirm_ids) > 0

        if not (rule_a or rule_b):
            continue  # eligible but neither rule fires — leave it PENDING

        confirm_set = set(confirm_ids)
        remaining = sum(
            1 for r in active
            if (r["status"] or "") not in ("CONFIRMED", "DISMISSED")
            and r["flag_id"] not in confirm_set
        )

        plan[s_id] = {
            "rule_a": rule_a,
            "set_role": set_role,
            "confirm_ids": confirm_ids,
            "rule_b": rule_b,
            "lock": True,
            "remaining": remaining,
            "speakers": speakers.get(s_id, 0),
        }
    return plan


def run(commit: bool, actor: str, auto_actors: set, min_conf: float, tones: set) -> dict:
    conn = get_audio_connection()
    try:
        plan = build_plan(conn, auto_actors, min_conf, tones)
        if not plan:
            print(f"No eligible {REVIEW_STATUS} sessions (auto-confirmed flag + a matching rule). "
                  f"Nothing to do.")
            return {"sessions": 0, "confirmed": 0, "locked": 0, "roles_set": 0}

        n_confirm = sum(len(p["confirm_ids"]) for p in plan.values())
        n_lock = sum(1 for p in plan.values() if p["lock"])
        n_role = sum(1 for p in plan.values() if p["set_role"])
        n_a = sum(1 for p in plan.values() if p["rule_a"])
        n_b = sum(1 for p in plan.values() if p["rule_b"])
        with_remaining = [s for s, p in plan.items() if p["remaining"] > 0]

        print(f"Eligible {REVIEW_STATUS} session(s): {len(plan):,}  "
              f"(Rule A single-speaker: {n_a:,} | Rule B tone-confirm: {n_b:,})\n")
        print(f"  {'SESSION':<10} {'SPK':<4} {'RULE A':<7} {'RULE B':<7} "
              f"{'CONFIRM':<8} {'REMAIN':<7} {'ACTION':<24}")
        print(f"  {'-'*10} {'-'*4} {'-'*7} {'-'*7} {'-'*8} {'-'*7} {'-'*24}")
        for s_id in sorted(plan):
            p = plan[s_id]
            actions = []
            if p["set_role"]:
                actions.append("set speaker1=USER")
            if p["rule_b"]:
                actions.append(f"confirm {len(p['confirm_ids'])}")
            actions.append("lock")
            print(f"  {s_id:<10} {p['speakers']:<4} "
                  f"{'yes' if p['rule_a'] else '-':<7} {'yes' if p['rule_b'] else '-':<7} "
                  f"{len(p['confirm_ids']):<8} {p['remaining']:<7} {', '.join(actions):<24}")
        print()

        print(f"  Flags to confirm (Rule B)        : {n_confirm:,}")
        print(f"  Sessions to lock                 : {n_lock:,}")
        print(f"  speaker1_role -> USER (Rule A)   : {n_role:,}")
        if with_remaining:
            print(f"  WARNING: {len(with_remaining):,} session(s) will be LOCKED with "
                  f"unactioned active flag(s) remaining:")
            print(f"           {', '.join(str(s) for s in sorted(with_remaining)[:30])}"
                  f"{' ...' if len(with_remaining) > 30 else ''}")
        print()

        if not commit:
            print(f"DRY RUN — would confirm {n_confirm:,} flag(s), set {n_role:,} role(s), "
                  f"and lock {n_lock:,} {REVIEW_STATUS} session(s). No changes written. "
                  f"Re-run with --commit to apply.")
            return {"sessions": len(plan), "confirmed": n_confirm,
                    "locked": n_lock, "roles_set": n_role}

        for s_id in sorted(plan):
            p = plan[s_id]
            # Rule B: confirm the qualifying flags.
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
                    (s_id, fid, actor, NOTE_CONFIRM % min_conf),
                )
            # Rule A: mark the single speaker as USER.
            if p["set_role"]:
                conn.execute(
                    "UPDATE audio_sessions SET speaker1_role = ? WHERE s_id = ?",
                    (USER_ROLE, s_id),
                )
                conn.execute(
                    """INSERT INTO audio_review_log (s_id, action, reviewer_id, note)
                       VALUES (?, 'SET_SPEAKER_ROLES', ?, ?)""",
                    (s_id, actor, NOTE_ROLE),
                )
            # Lock (either rule).
            conn.execute(
                """UPDATE audio_sessions
                   SET review_status = 'LOCKED', locked_by = ?, locked_at = datetime('now')
                   WHERE s_id = ?""",
                (actor, s_id),
            )
            note_lock = NOTE_LOCK_A if p["rule_a"] and not p["rule_b"] else NOTE_LOCK_B
            conn.execute(
                """INSERT INTO audio_review_log (s_id, action, reviewer_id, note)
                   VALUES (?, 'LOCK', ?, ?)""",
                (s_id, actor, note_lock),
            )
        conn.commit()

        print(f"Done. Confirmed {n_confirm:,} flag(s), set {n_role:,} speaker role(s), "
              f"locked {n_lock:,} {REVIEW_STATUS} session(s).")
        return {"sessions": len(plan), "confirmed": n_confirm,
                "locked": n_lock, "roles_set": n_role}
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Finalize PENDING audio sessions with an auto-confirmed flag: "
                    "single-speaker -> mark USER + lock (Rule A); high-conf hostile/"
                    "flirtatious/unclear tone flags -> confirm + lock (Rule B)."
    )
    parser.add_argument("--commit", action="store_true",
                        help="Actually write the changes (default is dry-run preview only).")
    parser.add_argument("--actor", default=REVIEWER_ID,
                        help=f"Actor id stamped on confirms/locks/role-sets (default: {REVIEWER_ID}).")
    parser.add_argument("--auto-actors", default=",".join(sorted(AUTO_ACTORS)),
                        help="Comma-separated confirmed_by values that count as auto-confirmed "
                             f"(default: {','.join(sorted(AUTO_ACTORS))}).")
    parser.add_argument("--min-conf", type=float, default=RULE_B_MIN_CONF,
                        help=f"Rule B minimum confidence, inclusive (default: {RULE_B_MIN_CONF}).")
    args = parser.parse_args()

    auto_actors = {a.strip() for a in args.auto_actors.split(",") if a.strip()}

    print("=" * 78)
    print("  Finalize auto-confirmed PENDING audio sessions (Rule A + Rule B)")
    print("=" * 78)
    print(f"  Database     : {AUDIO_DB_PATH}")
    print(f"  Scope        : {REVIEW_STATUS} sessions with an auto-confirmed flag")
    print(f"  Auto-actors  : {', '.join(sorted(auto_actors))}")
    print(f"  Rule A       : exactly 1 distinct speaker -> speaker1_role=USER + lock")
    print(f"  Rule B       : conf >= {args.min_conf} AND tone in "
          f"{{{', '.join(sorted(RULE_B_TONES))}}} -> confirm + lock")
    print(f"  Stamped by   : {args.actor}")
    print(f"  Mode         : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    run(commit=args.commit, actor=args.actor, auto_actors=auto_actors,
        min_conf=args.min_conf, tones=RULE_B_TONES)


if __name__ == "__main__":
    main()
