"""
dismiss_lowconf_tone_by_intent_audio.py

Flag-level cleanup for the audio review database (store/audio_review.db), scoped
to PENDING sessions only. Dismiss individual flags that are low-confidence AND
delivered in a calm/allowed tone — a likely false positive. Each intent has its
own confidence ceiling and its own set of allowed tones:

    INTENT              CONFIDENCE      ALLOWED TONE(S)
    ------------------  --------------  ---------------------
    NSFW_EXPLICIT       conf <= 0.90    NEUTRAL
    NSFW_GROOMING       conf <= 0.90    NEUTRAL, FLIRTATIOUS
    NSFW_APPEARANCE     conf <= 0.90    NEUTRAL
    VIOLENCE            conf <= 0.85    NEUTRAL
    NSFW                conf <= 0.85    NEUTRAL
    ABUSIVE_LANGUAGE    conf <= 0.85    ANY TONE

A flag is dismissed only when its intent is in the table above AND its confidence
is <= that intent's ceiling AND its segment tone is in that intent's allowed set.
Notes:
  - conf is compared with <= (inclusive). A flag with a NULL conf is never
    dismissed (a missing confidence can't satisfy "<= threshold").
  - tone comes from the flag's segment (audio_segments.tone), matched case-
    insensitively. For every intent except ABUSIVE_LANGUAGE a flag with no
    segment / no tone is left alone. ABUSIVE_LANGUAGE is "ANY TONE", so it is
    dismissed on the confidence rule alone regardless of tone (even if missing).
  - only ACTIVE flags are considered (already-DISMISSED rows and amended
    originals are excluded).

Dismissal follows the audio script convention (soft dismiss): the flag row is set
to status = 'DISMISSED' (restorable), a 'DISMISSED' row is written to
audio_review_log, and each affected session's verdict is recomputed. A session
left with no active flag becomes CLEAN; one that still has other active flags
stays FLAGGED. Flags are never hard-deleted.

SCOPE: PENDING sessions only (by request). SUBMITTED_FOR_REVIEW, LOCKED and
REVIEWED sessions are never touched.

DRY-RUN BY DEFAULT — running with no flags only previews what would change. Pass
--commit to actually dismiss.

Usage:
  python scripts/dismiss_lowconf_tone_by_intent_audio.py            # preview (dry-run)
  python scripts/dismiss_lowconf_tone_by_intent_audio.py --commit   # apply
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
    recompute_audio_session_verdict,
    _active_audio_flag_rows,
    AUDIO_DB_PATH,
)

REVIEW_STATUS = "PENDING"
REVIEWER_ID = "AUTO_DISMISS"
NOTE = "Auto-dismiss: low-confidence flag on allowed tone (per-intent rule)"

# Per-intent rule: (max_conf, allowed_tones). allowed_tones = None means "ANY
# TONE" — tone is ignored and the flag qualifies on the confidence rule alone.
# Tones are compared upper-cased.
RULES = {
    "NSFW_EXPLICIT":    (0.90, {"NEUTRAL"}),
    "NSFW_GROOMING":    (0.90, {"NEUTRAL", "FLIRTATIOUS"}),
    "NSFW_APPEARANCE":  (0.90, {"NEUTRAL"}),
    "VIOLENCE":         (0.85, {"NEUTRAL"}),
    "NSFW":             (0.85, {"NEUTRAL"}),
    "ABUSIVE_LANGUAGE": (0.85, None),  # ANY TONE
}


def _norm_intent(intent) -> str:
    """Canonical intent form, matching the DB/API/frontend normalisation
    ('nsfw-explicit', 'NSFW Explicit' -> 'NSFW_EXPLICIT')."""
    return (intent or "").strip().upper().replace("-", "_").replace(" ", "_")


def _norm_tone(tone) -> str:
    return (tone or "").strip().upper()


def find_targets(conn):
    """Return the active flags on PENDING sessions that match their intent's
    confidence+tone rule. Rows: (flag_id, s_id, intent, conf, tone)."""
    rows = conn.execute(
        """
        SELECT f.flag_id AS flag_id, f.s_id AS s_id, f.intent AS intent,
               f.conf AS conf, seg.tone AS tone
        FROM audio_flags f
        JOIN audio_sessions s   ON s.s_id = f.s_id
        LEFT JOIN audio_segments seg
               ON seg.s_id = f.s_id AND seg.seg_id = f.seg_id
        WHERE s.review_status = ?
          AND (f.status IS NULL OR f.status != 'DISMISSED')
          AND f.flag_id NOT IN (
              SELECT parent_flag_id FROM audio_flags WHERE parent_flag_id IS NOT NULL)
        ORDER BY f.s_id, f.flag_id
        """,
        (REVIEW_STATUS,),
    ).fetchall()

    targets = []
    for r in rows:
        rule = RULES.get(_norm_intent(r["intent"]))
        if rule is None:
            continue  # intent not covered by any rule
        max_conf, allowed_tones = rule

        # Confidence gate (inclusive). NULL conf never qualifies.
        if r["conf"] is None or r["conf"] > max_conf:
            continue

        # Tone gate. None -> ANY TONE (accept regardless, even if missing).
        if allowed_tones is not None and _norm_tone(r["tone"]) not in allowed_tones:
            continue

        targets.append(r)
    return targets


def predict_now_clean(conn, targets) -> int:
    """How many affected sessions would be left with NO active flag once the
    target flags are dismissed (i.e. would become CLEAN). A session goes CLEAN
    when every one of its live flags is in the dismiss set."""
    dismiss_by_sid: dict[int, set] = {}
    for t in targets:
        dismiss_by_sid.setdefault(t["s_id"], set()).add(t["flag_id"])

    now_clean = 0
    for s_id, dismissing in dismiss_by_sid.items():
        rows = conn.execute(
            "SELECT flag_id, parent_flag_id, status FROM audio_flags WHERE s_id = ?",
            (s_id,),
        ).fetchall()
        live = [r for r in _active_audio_flag_rows(rows) if (r["status"] or "") != "DISMISSED"]
        # CLEAN if no live flag survives the dismissal.
        if all(r["flag_id"] in dismissing for r in live):
            now_clean += 1
    return now_clean


def run(commit: bool) -> dict:
    """Dismiss matching flags. Returns {'flags': N, 'sessions': M, 'now_clean': C}."""
    conn = get_audio_connection()
    try:
        targets = find_targets(conn)
        if not targets:
            print(f"No matching flags on {REVIEW_STATUS} sessions. Nothing to dismiss.")
            return {"flags": 0, "sessions": 0, "now_clean": 0}

        session_ids = sorted({t["s_id"] for t in targets})
        print(f"Found {len(targets):,} flag(s) to dismiss across "
              f"{len(session_ids):,} {REVIEW_STATUS} session(s):\n")
        print(f"  {'SESSION':<10} {'FLAG ID':<9} {'INTENT':<20} {'CONF':<6} {'TONE':<14}")
        print(f"  {'-'*10} {'-'*9} {'-'*20} {'-'*6} {'-'*14}")
        for t in targets:
            print(f"  {t['s_id']:<10} {t['flag_id']:<9} {_norm_intent(t['intent']):<20} "
                  f"{t['conf']!s:<6} {(_norm_tone(t['tone']) or '-'):<14}")
        print()

        # How many flags each intent rule matched.
        intent_counts = Counter(_norm_intent(t["intent"]) for t in targets)
        print("  Flags dismissed by intent:")
        for intent, count in sorted(intent_counts.items()):
            max_conf, allowed = RULES[intent]
            tone_str = "ANY TONE" if allowed is None else ", ".join(sorted(allowed))
            print(f"    {intent:<20} conf<={max_conf:<5} [{tone_str:<20}] {count:>6,}")
        print()

        if not commit:
            predicted_clean = predict_now_clean(conn, targets)
            print(f"  Affected {REVIEW_STATUS} sessions      : {len(session_ids):,}")
            print(f"  Sessions that would become CLEAN : {predicted_clean:,}")
            print(f"  Sessions still FLAGGED after     : {len(session_ids) - predicted_clean:,}")
            print()
            print(f"DRY RUN — would dismiss {len(targets):,} flag(s) across "
                  f"{len(session_ids):,} session(s); {predicted_clean:,} would become CLEAN "
                  f"({len(session_ids) - predicted_clean:,} keep other active flags). "
                  f"No changes written. Re-run with --commit to apply.")
            return {"flags": len(targets), "sessions": len(session_ids),
                    "now_clean": predicted_clean}

        for t in targets:
            conn.execute(
                "UPDATE audio_flags SET status = 'DISMISSED' WHERE flag_id = ?",
                (t["flag_id"],),
            )
            conn.execute(
                """INSERT INTO audio_review_log (s_id, flag_id, action, reviewer_id, note)
                   VALUES (?, ?, 'DISMISSED', ?, ?)""",
                (t["s_id"], t["flag_id"], REVIEWER_ID, NOTE),
            )

        now_clean = 0
        for s_id in session_ids:
            if recompute_audio_session_verdict(s_id, conn) == "CLEAN":
                now_clean += 1
        conn.commit()

        print(f"Done. Dismissed {len(targets):,} flag(s) across {len(session_ids):,} "
              f"{REVIEW_STATUS} session(s); {now_clean:,} session(s) are now CLEAN "
              f"({len(session_ids) - now_clean:,} still have other active flags).")
        return {"flags": len(targets), "sessions": len(session_ids), "now_clean": now_clean}
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dismiss low-confidence flags on allowed tones (per-intent rules) "
                    "on PENDING audio sessions."
    )
    parser.add_argument("--commit", action="store_true",
                        help="Actually dismiss (default is dry-run preview only).")
    args = parser.parse_args()

    print("=" * 72)
    print("  Dismiss low-confidence flags by intent+tone rule (PENDING audio sessions)")
    print("=" * 72)
    print(f"  Database : {AUDIO_DB_PATH}")
    print(f"  Scope    : {REVIEW_STATUS} sessions only")
    print(f"  Mode     : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print("  Rules    :")
    for intent, (max_conf, allowed) in RULES.items():
        tone_str = "ANY TONE" if allowed is None else ", ".join(sorted(allowed))
        print(f"    {intent:<20} conf <= {max_conf:<5} tone: {tone_str}")
    print()

    run(commit=args.commit)


if __name__ == "__main__":
    main()
