"""
split_by_intent.py
------------------
Combines all audio-moderation JSON files specified (or all *.json in a
directory) and splits the sessions into three output files:

  1. <out_prefix>_nsfw.json      -- sessions that have at least one flag with
                                   intent in: NSFW, NSFW_EXPLICIT,
                                   NSFW_GROOMING, NSFW_APPEARANCE
  2. <out_prefix>_videos.json    -- sessions where has_video == True
                                   (and NOT already captured by the NSFW bucket)
  3. <out_prefix>_rest.json      -- everything else

Priority: NSFW > Videos > Rest

Usage examples
--------------
# Split a single file
python split_by_intent.py --input data/file.json

# Combine every JSON file in a directory and split
python split_by_intent.py --input data/

# Combine specific files
python split_by_intent.py --input data/file1.json data/file2.json

# Custom output prefix / directory
python split_by_intent.py --input data/ --out-dir data/split --out-prefix 2026-07-13
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Dict, Any

# ── Constants ──────────────────────────────────────────────────────────────
NSFW_INTENTS = {"NSFW", "NSFW_EXPLICIT", "NSFW_GROOMING", "NSFW_APPEARANCE"}


# ── Helpers ───────────────────────────────────────────────────────────────
def load_json_file(path: Path) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    elif isinstance(data, dict):
        return [data]
    else:
        raise ValueError(f"Unexpected JSON structure in {path}: {type(data)}")


def collect_input_paths(inputs: List[str]) -> List[Path]:
    paths: List[Path] = []
    for inp in inputs:
        p = Path(inp)
        if p.is_dir():
            found = sorted(p.glob("*.json"))
            if not found:
                print(f"[WARN] No *.json files found in directory: {p}", file=sys.stderr)
            paths.extend(found)
        elif p.is_file():
            paths.append(p)
        else:
            print(f"[WARN] Path not found, skipping: {p}", file=sys.stderr)
    return paths


def has_nsfw_flag(session: Dict[str, Any]) -> bool:
    for segment in session.get("segments", []):
        for flag in segment.get("flags", []):
            if flag.get("intent", "").upper() in NSFW_INTENTS:
                return True
    return False


def is_video_session(session: Dict[str, Any]) -> bool:
    return bool(session.get("has_video", False))


def split_sessions(sessions):
    nsfw, videos, rest = [], [], []
    for s in sessions:
        if has_nsfw_flag(s):
            nsfw.append(s)
        elif is_video_session(s):
            videos.append(s)
        else:
            rest.append(s)
    return nsfw, videos, rest


def write_json(data, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  -> {path}  ({len(data):,} sessions)")


# ── Main ──────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Combine moderation JSON files and split by intent category."
    )
    parser.add_argument("--input", "-i", nargs="+", required=True, metavar="PATH",
                        help="JSON file(s) or directory containing JSON files.")
    parser.add_argument("--out-dir", "-o", default=None, metavar="DIR",
                        help="Output directory (default: same as first input).")
    parser.add_argument("--out-prefix", "-p", default="split", metavar="PREFIX",
                        help="Prefix for output filenames (default: 'split').")
    args = parser.parse_args()

    input_paths = collect_input_paths(args.input)
    if not input_paths:
        print("[ERROR] No valid input JSON files found.", file=sys.stderr)
        sys.exit(1)

    print(f"\nLoading {len(input_paths)} file(s)...")
    all_sessions = []
    for p in input_paths:
        sessions = load_json_file(p)
        print(f"  {p.name}: {len(sessions):,} sessions")
        all_sessions.extend(sessions)

    print(f"\nTotal combined: {len(all_sessions):,} sessions")

    nsfw, videos, rest = split_sessions(all_sessions)
    print(f"\nSplit results:")
    print(f"  NSFW (NSFW / NSFW_EXPLICIT / NSFW_GROOMING / NSFW_APPEARANCE): {len(nsfw):,}")
    print(f"  Videos (has_video=True, non-NSFW):                             {len(videos):,}")
    print(f"  Rest:                                                           {len(rest):,}")

    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        first = input_paths[0]
        out_dir = first.parent if first.is_file() else first

    prefix = args.out_prefix
    print(f"\nWriting output to: {out_dir}")
    write_json(nsfw,   out_dir / f"{prefix}_nsfw.json")
    write_json(videos, out_dir / f"{prefix}_videos.json")
    write_json(rest,   out_dir / f"{prefix}_rest.json")
    print("\nDone.")


if __name__ == "__main__":
    main()
