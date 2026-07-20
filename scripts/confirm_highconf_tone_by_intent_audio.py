"""
confirm_highconf_tone_by_intent_audio.py

Flag-level confirmation for the audio review database (store/audio_review.db),
scoped to PENDING sessions only. Confirm individual flags that are HIGH-confidence
AND delivered in a matching hostile / distressed tone — a likely true positive.
Each intent has its own confidence floor and its own set of allowed tones, and a
session may carry a MIX of these intents (each flag is judged on its own rule):

    INTENT              CONFIDENCE      ALLOWED TONE(S)
    ------------------  --------------  ------------------------------
    HATE_SPEECH         conf >= 0.85    DISTRESSED, ANGRY, AGGRESSIVE
    ABUSIVE_LANGUAGE    conf >= 0.90    ANGRY, AGGRESSIVE, DISTRESSED
    SELF_HARM           conf >= 0.90    ANGRY
    NSFW_EXPLICIT       conf >= 0.95    ANY TONE
    VIOLENCE            conf >= 0.90    DISTRESSED, ANGRY, AGGRESSIVE

A flag is confirmed only when its intent is in the table above AND its confidence
is >= that intent's floor AND its segment tone is in that intent's allowed set.
Notes:
  - conf is compared with >= (inclusive). A flag with a NULL conf is never
    confirmed (a missing confidence can't satisfy ">= threshold").
  - tone comes from the flag's segment (audio_segments.tone), matched case-
    insensitively. For every intent except NSFW_EXPLICIT a flag with no segment /
    no tone is left alone. NSFW_EXPLICIT is "ANY TONE", so it is confirmed on the
    confidence rule alone regardless of tone (even if missing).
  - only ACTIVE flags are considered (already-DISMISSED rows and amended
    originals are excluded). Flags already CONFIRMED are counted but not
    re-written.

Confirmation follows the audio script convention (see
confirm_lock_aggressive_abusive_violence_audio.py): the flag row is set to
status = 'CONFIRMED' with confirmed_by / confirmed_at, and a 'CONFIRM_FLAG' row is
written to audio_review_log.

After confirming, each affected session is LOCKED (review_status = 'LOCKED',
locked_by / locked_at, a 'LOCK' row in audio_review_log) — but ONLY if it is now
fully actioned, i.e. every active flag is CONFIRMED (or DISMISSED). A session that
still has an unactioned flag (one matching no rule) is confirmed but left
UNLOCKED so a reviewer can finish it. Verdicts are unchanged (a confirmed flag is
still active, so locked sessions stay FLAGGED).

SCOPE: PENDING sessions only (by request). Other statuses are never touched.

DRY-RUN BY DEFAULT — running with no flags only previews what would change. Pass
--commit to actually confirm.

Usage:
  python scripts/confirm_highconf_tone_by_intent_audio.py            # preview (dry-run)
  python scripts/confirm_highconf_tone_by_intent_audio.py --commit   # apply
  python scripts/confirm_highconf_tone_by_intent_audio.py --confirmed-by Amogh --commit
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import (  # noqa: E402
    get_audio_connection,
    AUDIO_DB_PATH,
)

REVIEW_STATUS = "PENDING"
REVIEWER_ID = "AUTO_CONFIRM"
NOTE = "Auto-confirm: high-confidence flag on matching hostile/distressed tone (per-intent rule)"

# Per-intent rule: (min_conf, allowed_tones). allowed_tones = None means "ANY
# TONE" — tone is ignored and the flag qualifies on the confidence rule alone.
# Tones are compared upper-cased.
RULES = {
    "HATE_SPEECH":      (0.85, {"DISTRESSED", "ANGRY", "AGGRESSIVE"}),
    "ABUSIVE_LANGUAGE": (0.90, {"ANGRY", "AGGRESSIVE", "DISTRESSED"}),
    "SELF_HARM":        (0.90, {"ANGRY"}),
    "NSFW_EXPLICIT":    (0.95, None),  # ANY TONE
    "VIOLENCE":         (0.90, {"DISTRESSED", "ANGRY", "AGGRESSIVE"}),
}


def _norm_intent(intent) -> str:
    """Canonical intent form, matching the DB/API/frontend normalisation
    ('nsfw-explicit', 'Hate Speech' -> 'NSFW_EXPLICIT' / 'HATE_SPEECH')."""
    return (intent or "").strip().upper().replace("-", "_").replace(" ", "_")


def _norm_tone(tone) -> str:
    return (tone or "").strip().upper()


def _active_audio_flag_rows(rows) -> list:
    """Active flags = amendment rows + original rows that have no amendment."""
    amended_parents = {r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None}
    return [
        r for r in rows
        if r["parent_flag_id"] is not None or r["flag_id"] not in amended_parents
    ]


def find_targets(conn):
    """Return the active flags on PENDING sessions that match their intent's
    confidence+tone rule. Each row is a dict with an added 'already_confirmed'
    flag. Rows: {flag_id, s_id, intent, conf, tone, status, already_confirmed}."""
    rows = conn.execute(
        """
        SELECT f.flag_id, f.s_id, f.parent_flag_id, f.intent, f.conf, f.status,
               f.seg_id, seg.tone, s.review_status
        FROM audio_flags f
        JOIN audio_sessions s   ON s.s_id = f.s_id
        LEFT JOIN audio_segments seg
               ON seg.s_id = f.s_id AND seg.seg_id = f.seg_id
        WHERE s.review_status = ?
        ORDER BY f.s_id, f.flag_id
        """,
        (REVIEW_STATUS,),
    ).fetchall()

    # Restrict to ACTIVE, non-dismissed flags per session.
    from collections import defaultdict
    by_sid = defaultdict(list)
    for r in rows:
        by_sid[r["s_id"]].append(r)

    targets = []
    for s_id, srows in by_sid.items():
        active = [
            r for r in _active_audio_flag_rows(srows)
            if (r["status"] or "") != "DISMISSED"
        ]
        for r in active:
            rule = RULES.get(_norm_intent(r["intent"]))
            if rule is None:
                continue  # intent not covered by any rule
            min_conf, allowed_tones = rule

            # Confidence gate (inclusive). NULL conf never qualifies.
            if r["conf"] is None or r["conf"] < min_conf:
                continue

            # Tone gate. None -> ANY TONE (accept regardless, even if missing).
            if allowed_tones is not None and _norm_tone(r["tone"]) not in allowed_tones:
                continue

            targets.append({
                "flag_id": r["flag_id"],
                "s_id": r["s_id"],
                "intent": _norm_intent(r["intent"]),
                "conf": r["conf"],
                "tone": _norm_tone(r["tone"]),
                "already_confirmed": (r["status"] or "") == "CONFIRMED",
            })
    targets.sort(key=lambda t: (t["s_id"], t["flag_id"]))
    return targets


def lockable_sessions(conn, confirm_flag_ids: set, session_ids) -> set:
    """Of `session_ids`, the ones that would be FULLY ACTIONED once the flags in
    `confirm_flag_ids` are confirmed — i.e. every active (non-dismissed) flag is
    either already CONFIRMED or in the confirm set. Only these are safe to lock;
    a session with a leftover unactioned flag (one that matched no rule) is not.
    """
    lockable = set()
    for s_id in session_ids:
        rows = conn.execute(
            "SELECT flag_id, parent_flag_id, status FROM audio_flags WHERE s_id = ?",
            (s_id,),
        ).fetchall()
        active = [
            r for r in _active_audio_flag_rows(rows)
            if (r["status"] or "") != "DISMISSED"
        ]
        if all(
            (r["status"] or "") == "CONFIRMED" or r["flag_id"] in confirm_flag_ids
            for r in active
        ):
            lockable.add(s_id)
    return lockable


def run(commit: bool, confirmed_by: str) -> dict:
    """Confirm matching flags, then lock fully-actioned sessions.
    Returns {'matched', 'to_confirm', 'sessions', 'locked'}."""
    conn = get_audio_connection()
    try:
        targets = find_targets(conn)
        if not targets:
            print(f"No matching flags on {REVIEW_STATUS} sessions. Nothing to confirm.")
            print(f"\n  PENDING sessions confirmed : 0")
            print(f"  PENDING sessions locked    : 0")
            return {"matched": 0, "to_confirm": 0, "sessions": 0, "locked": 0}

        to_confirm = [t for t in targets if not t["already_confirmed"]]
        session_ids = sorted({t["s_id"] for t in targets})
        confirm_session_ids = sorted({t["s_id"] for t in to_confirm})
        confirm_flag_ids = {t["flag_id"] for t in to_confirm}
        # Sessions that become fully actioned (safe to lock) once we confirm.
        lockable = lockable_sessions(conn, confirm_flag_ids, confirm_session_ids)
        not_lockable = [s for s in confirm_session_ids if s not in lockable]

        print(f"Found {len(targets):,} matching flag(s) across {len(session_ids):,} "
              f"{REVIEW_STATUS} session(s) "
              f"({len(to_confirm):,} need confirming, "
              f"{len(targets) - len(to_confirm):,} already CONFIRMED):\n")
        print(f"  {'SESSION':<10} {'FLAG ID':<9} {'INTENT':<18} {'CONF':<6} {'TONE':<13} {'STATE':<10}")
        print(f"  {'-'*10} {'-'*9} {'-'*18} {'-'*6} {'-'*13} {'-'*10}")
        for t in targets:
            state = "confirmed" if t["already_confirmed"] else "-> CONFIRM"
            print(f"  {t['s_id']:<10} {t['flag_id']:<9} {t['intent']:<18} "
                  f"{t['conf']!s:<6} {(t['tone'] or '-'):<13} {state:<10}")
        print()

        intent_counts = Counter(t["intent"] for t in to_confirm)
        print("  Flags to confirm by intent:")
        for intent in sorted(RULES):
            if intent_counts.get(intent):
                min_conf, allowed = RULES[intent]
                tone_str = "ANY TONE" if allowed is None else ", ".join(sorted(allowed))
                print(f"    {intent:<18} conf>={min_conf:<5} [{tone_str:<28}] {intent_counts[intent]:>6,}")
        print()
        print(f"  Sessions with >=1 flag to confirm : {len(confirm_session_ids):,}")
        print(f"  ... of which fully actioned (lock): {len(lockable):,}")
        if not_lockable:
            print(f"  ... left UNLOCKED (unactioned flags remain): {len(not_lockable):,} "
                  f"-> {', '.join(str(s) for s in not_lockable[:20])}"
                  f"{' ...' if len(not_lockable) > 20 else ''}")
        print()

        if not commit:
            print(f"DRY RUN — would confirm {len(to_confirm):,} flag(s) across "
                  f"{len(confirm_session_ids):,} {REVIEW_STATUS} session(s) and LOCK "
                  f"{len(lockable):,} of them (fully actioned). No changes written. "
                  f"Re-run with --commit to apply.")
            print(f"\n  PENDING sessions confirmed : {len(confirm_session_ids):,}")
            print(f"  PENDING sessions locked    : {len(lockable):,}")
            return {"matched": len(targets), "to_confirm": len(to_confirm),
                    "sessions": len(confirm_session_ids), "locked": len(lockable)}

        for t in to_confirm:
            conn.execute(
                """UPDATE audio_flags
                   SET status = 'CONFIRMED',
                       confirmed_by = ?,
                       confirmed_at = datetime('now')
                   WHERE flag_id = ?""",
                (confirmed_by, t["flag_id"]),
            )
            conn.execute(
                """INSERT INTO audio_review_log (s_id, flag_id, action, reviewer_id, note)
                   VALUES (?, ?, 'CONFIRM_FLAG', ?, ?)""",
                (t["s_id"], t["flag_id"], confirmed_by, NOTE),
            )

        for s_id in lockable:
            conn.execute(
                """UPDATE audio_sessions
                   SET review_status = 'LOCKED',
                       locked_by = ?,
                       locked_at = datetime('now')
                   WHERE s_id = ?""",
                (confirmed_by, s_id),
            )
            conn.execute(
                """INSERT INTO audio_review_log (s_id, action, reviewer_id, note)
                   VALUES (?, 'LOCK', ?, ?)""",
                (s_id, confirmed_by, NOTE),
            )
        conn.commit()

        print(f"Done. Confirmed {len(to_confirm):,} flag(s) across "
              f"{len(confirm_session_ids):,} {REVIEW_STATUS} session(s); "
              f"locked {len(lockable):,} fully-actioned session(s).")
        print(f"\n  PENDING sessions confirmed : {len(confirm_session_ids):,}")
        print(f"  PENDING sessions locked    : {len(lockable):,}")
        return {"matched": len(targets), "to_confirm": len(to_confirm),
                "sessions": len(confirm_session_ids), "locked": len(lockable)}
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Confirm high-confidence flags on matching hostile/distressed tones "
                    "(per-intent rules) on PENDING audio sessions."
    )
    parser.add_argument("--commit", action="store_true",
                        help="Actually confirm (default is dry-run preview only).")
    parser.add_argument("--confirmed-by", default=REVIEWER_ID,
                        help=f"Reviewer id/name for confirmed_by (default: {REVIEWER_ID}).")
    args = parser.parse_args()

    print("=" * 74)
    print("  Confirm high-confidence flags by intent+tone rule (PENDING audio sessions)")
    print("=" * 74)
    print(f"  Database     : {AUDIO_DB_PATH}")
    print(f"  Scope        : {REVIEW_STATUS} sessions only")
    print(f"  Confirmed by : {args.confirmed_by}")
    print(f"  Mode         : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print("  Rules        :")
    for intent, (min_conf, allowed) in RULES.items():
        tone_str = "ANY TONE" if allowed is None else ", ".join(sorted(allowed))
        print(f"    {intent:<18} conf >= {min_conf:<5} tone: {tone_str}")
    print()

    run(commit=args.commit, confirmed_by=args.confirmed_by)


if __name__ == "__main__":
    main()
