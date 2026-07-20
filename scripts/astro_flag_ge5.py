"""
astro_flag_ge5.py

Counts DISTINCT sessions in which the ASTROLOGER was flagged >= N times
(default N = 5), across both review databases:

  - chat  (store/astrotalk.db)     -> flags / turns / sessions
  - audio (store/audio_review.db)  -> audio_flags / audio_segments / audio_sessions

Read-only.

"Flagged times" = the number of ACTIVE flags attributed to the astrologer in a
session. Active = not DISMISSED and not an amended original (the amendment row
is the active version), matching the queue's flag_count and the violation
breakdown scripts.

Astrologer attribution:
  - chat : flags.turn_id -> turns.speaker = 'ASTROLOGER'.
  - audio: flag.seg_id -> the segment's diarization label -> a ROLE. Per session
           the distinct labels are ordered by the numeric part of the label; the
           FIRST maps to speaker1_role and the SECOND to speaker2_role (both set
           by the reviewer). This mirrors AudioSessionViewer and
           scripts/violation_breakdown_audio.py. Flags on a 3rd+ speaker, an
           unmatched segment, or a session whose roles are not assigned are
           unattributed and do not count toward the astrologer.

Usage:
  python scripts/astro_flag_ge5.py
  python scripts/astro_flag_ge5.py --min 3
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.db import get_connection, DB_PATH                        # noqa: E402
from store.audio_db import get_audio_connection, AUDIO_DB_PATH      # noqa: E402


CHAT_SQL = """
    WITH astro_active AS (
        SELECT f.session_id, f.flag_id
        FROM flags f
        JOIN turns t ON t.session_id = f.session_id AND t.turn_id = f.turn_id
        WHERE t.speaker = 'ASTROLOGER'
          AND (f.status IS NULL OR f.status != 'DISMISSED')          -- drop dismissed
          AND f.flag_id NOT IN (                                     -- drop amended originals
              SELECT parent_flag_id FROM flags WHERE parent_flag_id IS NOT NULL
          )
    )
    SELECT COUNT(*) FROM (
        SELECT session_id
        FROM astro_active
        GROUP BY session_id
        HAVING COUNT(*) >= ?
    )
"""

# Audio has no speaker on the flag itself: resolve seg_id -> segment label ->
# role. Labels are ranked by their numeric part (SPEAKER_00 before SPEAKER_01),
# 1st -> speaker1_role, 2nd -> speaker2_role.
AUDIO_SQL = """
    WITH ranked_labels AS (
        SELECT s_id, speaker AS label,
               ROW_NUMBER() OVER (
                   PARTITION BY s_id
                   ORDER BY CAST(
                       CASE WHEN instr(speaker,'_') > 0
                            THEN substr(speaker, instr(speaker,'_')+1)
                            ELSE speaker END AS INTEGER), speaker
               ) AS rn
        FROM (SELECT DISTINCT s_id, speaker FROM audio_segments WHERE speaker IS NOT NULL)
    ),
    seg_role AS (
        SELECT seg.s_id, seg.seg_id,
               CASE rl.rn WHEN 1 THEN ss.speaker1_role
                          WHEN 2 THEN ss.speaker2_role
                          ELSE NULL END AS role
        FROM audio_segments seg
        JOIN audio_sessions ss ON ss.s_id = seg.s_id
        LEFT JOIN ranked_labels rl ON rl.s_id = seg.s_id AND rl.label = seg.speaker
    ),
    active_flags AS (
        SELECT af.s_id, af.flag_id, af.seg_id
        FROM audio_flags af
        WHERE (af.status IS NULL OR af.status != 'DISMISSED')
          AND af.flag_id NOT IN (
              SELECT parent_flag_id FROM audio_flags WHERE parent_flag_id IS NOT NULL
          )
    ),
    astro_active AS (
        SELECT f.s_id, f.flag_id
        FROM active_flags f
        JOIN seg_role sr ON sr.s_id = f.s_id AND sr.seg_id = f.seg_id
        WHERE sr.role = 'ASTROLOGER'
    )
    SELECT COUNT(*) FROM (
        SELECT s_id
        FROM astro_active
        GROUP BY s_id
        HAVING COUNT(*) >= ?
    )
"""


def main():
    parser = argparse.ArgumentParser(
        description="Distinct sessions where the astrologer was flagged >= N times (chat + audio)."
    )
    parser.add_argument("--min", type=int, default=5,
                        help="Minimum astrologer flag count per session (default: 5).")
    args = parser.parse_args()

    conn = get_connection()
    try:
        chat_n = conn.execute(CHAT_SQL, (args.min,)).fetchone()[0]
    finally:
        conn.close()

    aconn = get_audio_connection()
    try:
        audio_n = aconn.execute(AUDIO_SQL, (args.min,)).fetchone()[0]
    finally:
        aconn.close()

    print("=" * 64)
    print(f"  Sessions where the ASTROLOGER was flagged >= {args.min} times")
    print("=" * 64)
    print(f"  CHAT   ({DB_PATH})")
    print(f"    distinct sessions: {chat_n:>8,}")
    print(f"  AUDIO  ({AUDIO_DB_PATH})")
    print(f"    distinct sessions: {audio_n:>8,}")
    print("-" * 64)
    print(f"  TOTAL: {chat_n + audio_n:>8,}")


if __name__ == "__main__":
    main()
