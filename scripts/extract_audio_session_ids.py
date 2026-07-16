"""
extract_audio_session_ids.py

Extract every distinct audio session id (s_id) from the audio review database
(store/audio_review.db) and write them to a single JSON file. Also prints the
distinct count to the console.

s_id is the primary key of audio_sessions, so it is already distinct there;
SELECT DISTINCT is used defensively so the script is safe to point at any table
that carries an s_id column.

Read-only — never modifies the database.

Usage:
  python scripts/extract_audio_session_ids.py
  python scripts/extract_audio_session_ids.py --out C:/path/to/session_ids.json
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store.audio_db import get_audio_connection, AUDIO_DB_PATH  # noqa: E402


def extract(out_path: Path) -> int:
    conn = get_audio_connection()
    rows = conn.execute(
        "SELECT DISTINCT s_id FROM audio_sessions WHERE s_id IS NOT NULL ORDER BY s_id"
    ).fetchall()
    conn.close()

    session_ids = [r["s_id"] for r in rows]
    count = len(session_ids)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(session_ids, fh, indent=2)

    print(f"  Distinct audio session_ids : {count}")
    print(f"  JSON written               : {out_path}")
    return count


def main() -> None:
    p = argparse.ArgumentParser(
        description="Extract all distinct audio session ids (s_id) to a JSON file"
    )
    p.add_argument("--out", default=None, help="Output JSON path")
    args = p.parse_args()

    if args.out:
        out_path = Path(args.out)
    else:
        stamp = datetime.now().strftime("%Y%m%d")
        out_path = Path(__file__).resolve().parents[1] / "exports" / f"audio_session_ids_{stamp}.json"

    print("=" * 60)
    print("  Extract distinct audio session ids (s_id)")
    print(f"  DB: {AUDIO_DB_PATH}")
    print("=" * 60)
    extract(out_path)


if __name__ == "__main__":
    main()
