"""
dismiss_lowconf_intent_audio.py

Flag-level cleanup for the audio review database (store/audio_review.db), scoped
to PENDING sessions only. Dismiss individual low-confidence flags regardless of
tone — a flag whose confidence falls below its intent's floor is treated as a
likely false positive. A session may carry a MIX of these intents (each flag is
judged on its own rule):

    INTENT              CONDITION
    ------------------  -----------------------------
    HATE_SPEECH         conf < 0.85    ANY TONE
    ABUSIVE_LANGUAGE    conf < 0.90    ANY TONE
    SELF_HARM           conf < 0.90    ANY TONE
    VIOLENCE            conf < 0.90    ANY TONE

A flag is dismissed only when its intent is in the table above AND its confidence
is strictly BELOW that intent's floor. Notes:
  - conf is compared with < (strict, exclusive). A flag exactly at the floor
    (e.g. HATE_SPEECH conf = 0.85) is NOT dismissed. A flag with a NULL conf is
    never dismissed (a missing confidence can't satisfy "< threshold").
  - tone is ignored for every intent here ("ANY TONE"), so a flag with no
    segment / no tone is still eligible.
  - only ACTIVE flags are considered (already-DISMISSED rows and amended
    originals are excluded). Intents not listed above are left untouched.

Dismissal follows the audio script convention (soft dismiss): the flag row is set
to status = 'DISMISSED' (restorable), a 'DISMISSED' row is written to
audio_review_log, and each affected session's verdict is recomputed. A session
left with no active flag becomes CLEAN; one that still has other active flags
stays FLAGGED. Flags are never hard-deleted.

SCOPE: PENDING sessions only (by request). Other statuses are never touched.

DRY-RUN BY DEFAULT — running with no flags only previews what would change,
including how many sessions would become CLEAN. Pass --commit to actually dismiss.

Usage:
  python scripts/dismiss_lowconf_intent_audio.py            # preview (dry-run)
  python scripts/dismiss_lowconf_intent_audio.py --commit   # apply
"""

import argparse
import sys
from collections import Counter, defaultdict
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
NOTE = "Auto-dismiss: low-confidence flag below intent floor (any tone)"

# Per-intent rule: (max_conf, allowed_tones). A flag is dismissed when
# conf < max_conf. allowed_tones = None means "ANY TONE" (tone ignored). Every
# rule here is ANY TONE.
RULES = {
    "HATE_SPEECH":      (0.85, None),
    "ABUSIVE_LANGUAGE": (0.90, None),
    "SELF_HARM":        (0.90, None),
    "VIOLENCE":         (0.90, None),
}


def _norm_intent(intent) -> str:
    """Canonical intent form, matching the DB/API/frontend normalisation
    ('hate-speech', 'Hate Speech' -> 'HATE_SPEECH')."""
    return (intent or "").strip().upper().replace("-", "_").replace(" ", "_")


def _norm_tone(tone) -> str:
    return (tone or "").strip().upper()


def find_targets(conn):
    """Return the active flags on PENDING sessions that match their intent's
    rule (conf strictly below the intent floor). Rows: sqlite Rows with
    (flag_id, s_id, intent, conf, tone)."""
    rows = conn.execute(
        """
        SELECT f.flag_id AS flag_id, f.s_id AS s_id, f.parent_flag_id AS parent_flag_id,
               f.intent AS intent, f.conf AS conf, f.status AS status, seg.tone AS tone
        FROM audio_flags f
        JOIN audio_sessions s   ON s.s_id = f.s_id
        LEFT JOIN audio_segments seg
               ON seg.s_id = f.s_id AND seg.seg_id = f.seg_id
        WHERE s.review_status = ?
        ORDER BY f.s_id, f.flag_id
        """,
        (REVIEW_STATUS,),
    ).fetchall()

    # Restrict to ACTIVE, non-dismissed flags per session (amendment rows and
    # un-amended originals; DISMISSED rows excluded).
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
            max_conf, allowed_tones = rule

            # Confidence gate (STRICT <). NULL conf never qualifies.
            if r["conf"] is None or r["conf"] >= max_conf:
                continue

            # Tone gate. None -> ANY TONE (accept regardless, even if missing).
            if allowed_tones is not None and _norm_tone(r["tone"]) not in allowed_tones:
                continue

            targets.append(r)
    return targets


def predict_now_clean(conn, targets) -> int:
    """How many affected sessions would be left with NO active flag once the
    target flags are dismissed (i.e. would become CLEAN)."""
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
        if all(r["flag_id"] in dismissing for r in live):
            now_clean += 1
    return now_clean


def run(commit: bool) -> dict:
    """Dismiss matching flags. Returns {'flags', 'sessions', 'now_clean'}."""
    conn = get_audio_connection()
    try:
        targets = find_targets(conn)
        if not targets:
            print(f"No matching flags on {REVIEW_STATUS} sessions. Nothing to dismiss.")
            return {"flags": 0, "sessions": 0, "now_clean": 0}

        session_ids = sorted({t["s_id"] for t in targets})
        print(f"Found {len(targets):,} flag(s) to dismiss across "
              f"{len(session_ids):,} {REVIEW_STATUS} session(s):\n")
        print(f"  {'SESSION':<10} {'FLAG ID':<9} {'INTENT':<18} {'CONF':<6} {'TONE':<14}")
        print(f"  {'-'*10} {'-'*9} {'-'*18} {'-'*6} {'-'*14}")
        for t in targets:
            print(f"  {t['s_id']:<10} {t['flag_id']:<9} {_norm_intent(t['intent']):<18} "
                  f"{t['conf']!s:<6} {(_norm_tone(t['tone']) or '-'):<14}")
        print()

        intent_counts = Counter(_norm_intent(t["intent"]) for t in targets)
        print("  Flags dismissed by intent:")
        for intent in sorted(RULES):
            if intent_counts.get(intent):
                max_conf, allowed = RULES[intent]
                tone_str = "ANY TONE" if allowed is None else ", ".join(sorted(allowed))
                print(f"    {intent:<18} conf<{max_conf:<5} [{tone_str:<20}] {intent_counts[intent]:>6,}")
        print()

        predicted_clean = predict_now_clean(conn, targets)
        print(f"  Affected {REVIEW_STATUS} sessions      : {len(session_ids):,}")
        print(f"  Sessions that would become CLEAN : {predicted_clean:,}")
        print(f"  Sessions still FLAGGED after     : {len(session_ids) - predicted_clean:,}")
        print()

        if not commit:
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
        description="Dismiss low-confidence flags (below per-intent floor, any tone) "
                    "on PENDING audio sessions."
    )
    parser.add_argument("--commit", action="store_true",
                        help="Actually dismiss (default is dry-run preview only).")
    args = parser.parse_args()

    print("=" * 72)
    print("  Dismiss low-confidence flags by intent (any tone) — PENDING audio sessions")
    print("=" * 72)
    print(f"  Database : {AUDIO_DB_PATH}")
    print(f"  Scope    : {REVIEW_STATUS} sessions only")
    print(f"  Mode     : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print("  Rules    :")
    for intent, (max_conf, allowed) in RULES.items():
        tone_str = "ANY TONE" if allowed is None else ", ".join(sorted(allowed))
        print(f"    {intent:<18} conf < {max_conf:<5} tone: {tone_str}")
    print()

    run(commit=args.commit)


if __name__ == "__main__":
    main()
