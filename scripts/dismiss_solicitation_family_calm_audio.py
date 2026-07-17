"""
dismiss_solicitation_family_calm_audio.py

For the audio review database (store/audio_review.db): dismiss the flags of
audio sessions whose flags fall ENTIRELY within a specific "solicitation /
manipulation" family AND whose flagged segments were delivered in a neutral,
professional or calm tone.

This is a hybrid of two existing scripts:
  * lock_pending_allowed_flags_audio.py — the "every active flag must be in the
    allowed set, one out-of-list flag disqualifies the whole session" gate.
  * dismiss_calm_professional_flags_audio.py — the soft-dismiss mechanics
    (status='DISMISSED', audio_review_log row, recompute session verdict).

Two SESSION-LEVEL gates, both must pass:

  1. INTENT PURITY — every ACTIVE flag's intent is in ALLOWED_INTENTS (below).
     A single flag outside the list (e.g. ABUSIVE_LANGUAGE, HATE_SPEECH, an NSFW
     code) disqualifies the ENTIRE session — "these flags only, not mixed with
     other flags". A session with no active flag is left as is.

  2. TONE — every ACTIVE flag sits on a segment whose tone is one of
     NEUTRAL / PROFESSIONAL / CALM (case-insensitive; some legacy rows are
     lower-case). If even one of the flags was delivered on an angry, aggressive,
     distressed, etc. segment, the whole session is SKIPPED: a solicitation
     shouted in anger is a genuine concern, not a false positive. A flag with no
     segment (seg_id NULL / missing) has no tone and so fails this gate, skipping
     the session.

"Active flag" matches the review app (store/audio_db flag_count): the amendment
row if a flag was edited, else the original; DISMISSED rows are ignored.

When BOTH gates pass, every active flag on the session is soft-dismissed
(status='DISMISSED', restorable), a 'DISMISSED' row is written to
audio_review_log, and the session verdict is recomputed — a session left with no
active flag becomes overall_verdict = CLEAN. Flags are never hard-deleted.

SCOPE: by default only PENDING and LOCKED sessions are considered. Override with
--status (e.g. --status ALL).

DRY-RUN BY DEFAULT — pass --commit to actually dismiss.

Usage:
  python scripts/dismiss_solicitation_family_calm_audio.py            # preview (dry-run)
  python scripts/dismiss_solicitation_family_calm_audio.py --commit   # apply
  python scripts/dismiss_solicitation_family_calm_audio.py --status ALL --commit
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import (  # noqa: E402
    get_audio_connection,
    recompute_audio_session_verdict,
    AUDIO_DB_PATH,
)

# The only intents allowed to appear on a dismissable session. Any flag whose
# intent is outside this set disqualifies the whole session.
ALLOWED_INTENTS = {
    "FAKE_REMEDIES",
    "UNAUTHORIZED_MEDICAL_ADVICE",
    "FINANCIAL_SOLICITATION",
    "IDENTITY_FRAUD",
    "INSTIGATION",
    "OFF_PLATFORM_SOLICITATION",
    "PERSONAL_DATA_COLLECTION",
    "FEAR_MANIPULATION",
    "COMPETITOR_PROMOTION",
    "RE_ENGAGEMENT_SOLICITATION",
}

# Segment tones that count as "neutral, professional and calm". Canonical tone
# vocabulary (test_gemini_multi/audio_prompts.py): NEUTRAL / CALM / PROFESSIONAL
# / DISTRESSED / ANGRY / AGGRESSIVE / FLIRTATIOUS / UNCLEAR.
ALLOWED_TONES = {"NEUTRAL", "PROFESSIONAL", "CALM"}

DEFAULT_STATUSES = ("PENDING", "LOCKED")
REVIEWER_ID = "AUTO_DISMISS"
NOTE = "Auto-dismiss: solicitation-family flags only, all on neutral/professional/calm segments"


def normalise(code: str) -> str:
    return (code or "").strip().upper().replace("-", "_").replace(" ", "_")


def active_flags_by_session(conn, statuses):
    """Per in-scope session, the list of ACTIVE flags with their intent + tone.

    Active = amendment row if a flag was edited, else the original; DISMISSED
    rows are ignored. Same rule as store/audio_db's flag_count column. Tone is
    the flag's segment tone (NULL when the flag has no matching segment).

    Returns {s_id: [ {flag_id, intent, tone}, ... ]} for sessions with >= 1
    active flag.
    """
    if statuses:
        status_clause = "AND s.review_status IN (%s)" % ",".join("?" for _ in statuses)
        params = list(statuses)
    else:
        status_clause = ""
        params = []

    rows = conn.execute(
        f"""
        SELECT f.flag_id AS flag_id, f.parent_flag_id AS parent_flag_id,
               f.s_id AS s_id, f.intent AS intent, f.status AS status,
               seg.tone AS tone
        FROM audio_flags f
        JOIN audio_sessions s   ON s.s_id = f.s_id
        LEFT JOIN audio_segments seg ON seg.s_id = f.s_id AND seg.seg_id = f.seg_id
        WHERE 1=1 {status_clause}
        """,
        params,
    ).fetchall()

    amended_parents = {r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None}

    by_session = defaultdict(list)
    for r in rows:
        is_active = r["parent_flag_id"] is not None or r["flag_id"] not in amended_parents
        if not is_active or (r["status"] or "") == "DISMISSED":
            continue
        by_session[r["s_id"]].append(
            {"flag_id": r["flag_id"], "intent": normalise(r["intent"]), "tone": normalise(r["tone"])}
        )
    return by_session


def classify(by_session):
    """Split sessions into (dismissable, skipped).

    dismissable: [(s_id, [flags...])] where every active flag's intent is in
                 ALLOWED_INTENTS AND every flag's tone is in ALLOWED_TONES.
    skipped    : [(s_id, reason)] for the rest, reason describing the first gate
                 that failed.
    """
    dismissable = []
    skipped = []
    for sid, flags in sorted(by_session.items()):
        if not flags:
            continue  # no active flags -> leave as is

        bad_intents = sorted({f["intent"] for f in flags if f["intent"] not in ALLOWED_INTENTS})
        if bad_intents:
            skipped.append((sid, f"out-of-list intent(s): {bad_intents}"))
            continue

        bad_tones = sorted({(f["tone"] or "—") for f in flags if f["tone"] not in ALLOWED_TONES})
        if bad_tones:
            skipped.append((sid, f"non-calm tone(s): {bad_tones}"))
            continue

        dismissable.append((sid, flags))
    return dismissable, skipped


def run(commit: bool, statuses) -> int:
    conn = get_audio_connection()
    try:
        by_session = active_flags_by_session(conn, statuses)
        dismissable, skipped = classify(by_session)

        total_flags = sum(len(flags) for _, flags in dismissable)

        print(f"  In-scope sessions with active flags : {len(by_session)}")
        print(f"  Dismissable (all intents allowed + all calm tone): {len(dismissable)} "
              f"session(s), {total_flags} flag(s)")
        print(f"  Skipped     (mixed intent or non-calm tone): {len(skipped)}")
        print()

        if dismissable:
            print("  WOULD DISMISS:")
            print(f"    {'SESSION':<10} {'INTENTS':<40} {'TONES':<20}")
            print(f"    {'-'*10} {'-'*40} {'-'*20}")
            for sid, flags in dismissable:
                intents = sorted({f["intent"] for f in flags})
                tones = sorted({f["tone"] for f in flags})
                print(f"    {str(sid):<10} {', '.join(intents):<40} {', '.join(tones):<20}")
        else:
            print("  WOULD DISMISS: (none)")
        print()

        if skipped:
            print("  SKIPPED (left as is):")
            for sid, reason in skipped:
                print(f"    {str(sid):<10} {reason}")
            print()

        if not commit:
            print(f"  DRY RUN — would dismiss {total_flags} flag(s) across "
                  f"{len(dismissable)} session(s) and recompute their verdicts "
                  f"(sessions left with no active flag become CLEAN). No changes "
                  f"written. Re-run with --commit to apply.")
            return total_flags

        if not dismissable:
            print("  Nothing to dismiss.")
            return 0

        for sid, flags in dismissable:
            for f in flags:
                conn.execute(
                    "UPDATE audio_flags SET status = 'DISMISSED' WHERE flag_id = ?",
                    (f["flag_id"],),
                )
                conn.execute(
                    """INSERT INTO audio_review_log (s_id, flag_id, action, reviewer_id, note)
                       VALUES (?, ?, 'DISMISSED', ?, ?)""",
                    (sid, f["flag_id"], REVIEWER_ID, NOTE),
                )

        now_clean = 0
        for sid, _ in dismissable:
            if recompute_audio_session_verdict(sid, conn) == "CLEAN":
                now_clean += 1
        conn.commit()

        print(f"  Done. Dismissed {total_flags} flag(s) across {len(dismissable)} "
              f"session(s); {now_clean} session(s) are now CLEAN.")
        return total_flags
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dismiss solicitation-family flags on audio sessions that carry only those "
                    "intents and were delivered in a neutral/professional/calm tone."
    )
    parser.add_argument("--commit", action="store_true",
                        help="Actually dismiss (default is dry-run preview only).")
    parser.add_argument("--status", default=",".join(DEFAULT_STATUSES),
                        help="Comma-separated review_status values to consider. "
                             "Default: PENDING,LOCKED. Pass 'ALL' to consider every status.")
    args = parser.parse_args()

    if str(args.status).strip().upper() == "ALL":
        statuses = None
    else:
        statuses = tuple(s.strip().upper() for s in args.status.split(",") if s.strip())

    print("=" * 72)
    print("  Dismiss solicitation-family flags on pure, calm-toned audio sessions")
    print("=" * 72)
    print(f"  Database       : {AUDIO_DB_PATH}")
    print(f"  Allowed intents: {', '.join(sorted(ALLOWED_INTENTS))}")
    print(f"  Allowed tones  : {', '.join(sorted(ALLOWED_TONES))}")
    print(f"  Scope          : {', '.join(statuses) if statuses else 'ALL statuses'}")
    print(f"  Mode           : {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    run(commit=args.commit, statuses=statuses)


if __name__ == "__main__":
    main()
