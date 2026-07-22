"""
generate_detection_logs.py

Single entry point for generating chat detection logs from one or more
unprocessed_sessions_merged CSVs.

--input can be:
  • A single CSV file   : processes that file
  • A directory          : processes every *.csv inside it, one by one

Modes:
  --mode session   : Session-level summary (1 row per session)
  --mode detailed  : Turn-level detail   (1 row per turn)
  --mode both      : Generate both CSVs in one run (default)

Usage:
  # Single file (default path)
  python scripts/generate_detection_logs.py

  # Single file (custom)
  python scripts/generate_detection_logs.py --input path/to/merged.csv

  # All CSVs in a directory
  python scripts/generate_detection_logs.py --input path/to/csv_folder/

  # Mode + output directory
  python scripts/generate_detection_logs.py --input path/to/folder/ --mode session --out-dir exports/

Read-only — never modifies the input CSV(s).
"""

import argparse
import csv
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path


# ─── defaults ───────────────────────────────────────────────────────────────
DEFAULT_INPUT = (
    r"C:\Users\Astrotalk\Downloads\drive-download-20260717T090344Z-1-001"
    r"\unprocessed_sessions_merged.csv"
)

VALID_MODES = ("session", "detailed", "both")


# ─── output column definitions ─────────────────────────────────────────────
SESSION_COLUMNS = [
    "session_id",
    "n_turns",
    "original_flagged",
    "llm_flagged",
    "n_llm_flagged_turns",
    "flag_categories",
    "flag_categories_list",
    "max_confidence",
    "avg_confidence",
    "match_status",
    "languages",
]

DETAILED_COLUMNS = [
    "session_id",
    "turn_id",
    "sender",
    "message_text",
    "is_automated_message",
    "sent_at_ist",
    "has_link",
    "language",
    "original_flagged",
    "llm_flag",
    "confidence_score",
    "is_llm_flagged",
    "session_llm_flagged",
    "session_match_status",
    "session_n_llm_flags",
    "session_flag_categories",
]


# ─── helpers ────────────────────────────────────────────────────────────────
def _parse_confidence(raw: str) -> float | None:
    if not raw or not raw.strip():
        return None
    try:
        return float(raw.strip())
    except ValueError:
        return None


def _format_flag_categories(counter: Counter) -> str:
    if not counter:
        return ""
    parts = sorted(f"{cat}:{cnt}" for cat, cnt in counter.most_common())
    return "{" + ", ".join(parts) + "}"


def _determine_match_status(original_flagged: str, has_llm_flags: bool) -> str:
    orig = original_flagged.strip().lower() == "yes"
    if orig and has_llm_flags:
        return "BOTH_FLAGGED"
    elif orig and not has_llm_flags:
        return "ASTROTALK_ONLY"
    elif not orig and has_llm_flags:
        return "LLM_ONLY"
    else:
        return "BOTH_CLEAN"


# ─── core: load & aggregate ────────────────────────────────────────────────
def load_all_rows(input_path: Path) -> list[dict]:
    """Read the merged CSV into memory (used by both modes)."""
    with input_path.open("r", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def build_session_context(rows: list[dict]) -> dict:
    """
    Aggregate turn-level rows into per-session context dicts.
    Shared by both session-level and detailed generators.
    """
    sessions: dict = {}

    for row in rows:
        sid = row["session_id"]
        if sid not in sessions:
            sessions[sid] = {
                "n_turns": 0,
                "original_flagged": row.get("flagged", "no"),
                "llm_flag_counter": Counter(),
                "confidences": [],
                "languages": set(),
                "n_llm_flagged_turns": 0,
            }

        s = sessions[sid]
        s["n_turns"] += 1

        # Language
        lang_raw = row.get("language", "")
        if lang_raw:
            for lang in lang_raw.split(","):
                lang = lang.strip()
                if lang:
                    s["languages"].add(lang)

        # LLM flag (may be pipe-separated for multi-flags)
        llm_flag_raw = row.get("llm_flag", "").strip()
        if llm_flag_raw:
            s["n_llm_flagged_turns"] += 1
            for flag in llm_flag_raw.split("|"):
                flag = flag.strip()
                if flag:
                    s["llm_flag_counter"][flag] += 1

            conf = _parse_confidence(row.get("confidence_score", ""))
            if conf is not None:
                s["confidences"].append(conf)

    # Derive match status + formatting
    for sid, s in sessions.items():
        has_llm = bool(s["llm_flag_counter"])
        s["session_llm_flagged"] = 1 if has_llm else 0
        s["session_match_status"] = _determine_match_status(
            s["original_flagged"], has_llm
        )
        s["session_flag_categories"] = _format_flag_categories(s["llm_flag_counter"])

    return sessions


# ─── generator: session-level ──────────────────────────────────────────────
def generate_session_log(
    sessions: dict, out_path: Path
) -> list[dict]:
    """Write session-level detection log CSV. Returns the rows for summary."""
    log_rows = []

    for sid, s in sessions.items():
        llm_flagged = bool(s["llm_flag_counter"])
        confidences = s["confidences"]

        row = {
            "session_id": sid,
            "n_turns": s["n_turns"],
            "original_flagged": s["original_flagged"],
            "llm_flagged": 1 if llm_flagged else 0,
            "n_llm_flagged_turns": s["n_llm_flagged_turns"],
            "flag_categories": s["session_flag_categories"],
            "flag_categories_list": ", ".join(sorted(s["llm_flag_counter"].keys())),
            "max_confidence": f"{max(confidences):.2f}" if confidences else "",
            "avg_confidence": (
                f"{sum(confidences) / len(confidences):.2f}" if confidences else ""
            ),
            "match_status": s["session_match_status"],
            "languages": ", ".join(sorted(s["languages"])),
        }
        log_rows.append(row)

    # Sort: flagged first, then by LLM-flagged turns desc
    STATUS_RANK = {"BOTH_FLAGGED": 0, "LLM_ONLY": 1, "ASTROTALK_ONLY": 2, "BOTH_CLEAN": 3}
    log_rows.sort(key=lambda r: (
        STATUS_RANK.get(r["match_status"], 4),
        -int(r["n_llm_flagged_turns"]),
        r["session_id"],
    ))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SESSION_COLUMNS)
        writer.writeheader()
        writer.writerows(log_rows)

    return log_rows


# ─── generator: detailed turn-level ────────────────────────────────────────
def generate_detailed_log(
    rows: list[dict], sessions: dict, out_path: Path
) -> tuple[int, int]:
    """Write turn-level detailed detection log CSV. Returns (n_written, n_flagged_turns)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)

    n_written = 0
    n_flagged_turns = 0

    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=DETAILED_COLUMNS)
        writer.writeheader()

        for row in rows:
            sid = row["session_id"]
            ctx = sessions[sid]
            llm_flag_raw = row.get("llm_flag", "").strip()
            is_llm_flagged = 1 if llm_flag_raw else 0

            if is_llm_flagged:
                n_flagged_turns += 1

            out_row = {
                "session_id":              sid,
                "turn_id":                 row.get("turn_id", ""),
                "sender":                  row.get("sender", ""),
                "message_text":            row.get("message_text", ""),
                "is_automated_message":    row.get("is_automated_message", ""),
                "sent_at_ist":             row.get("sent_at_ist", ""),
                "has_link":                row.get("has_link", ""),
                "language":                row.get("language", ""),
                "original_flagged":        ctx["original_flagged"],
                "llm_flag":                llm_flag_raw,
                "confidence_score":        row.get("confidence_score", ""),
                "is_llm_flagged":          is_llm_flagged,
                "session_llm_flagged":     ctx["session_llm_flagged"],
                "session_match_status":    ctx["session_match_status"],
                "session_n_llm_flags":     ctx["n_llm_flagged_turns"],
                "session_flag_categories": ctx["session_flag_categories"],
            }
            writer.writerow(out_row)
            n_written += 1

    return n_written, n_flagged_turns


# ─── summary printer ───────────────────────────────────────────────────────
def print_summary(
    sessions: dict,
    session_out: Path | None = None,
    detailed_out: Path | None = None,
    n_turns_written: int = 0,
    n_flagged_turns: int = 0,
) -> None:
    total = len(sessions)
    status_counts = Counter(s["session_match_status"] for s in sessions.values())
    orig_flagged = sum(
        1 for s in sessions.values()
        if s["original_flagged"].strip().lower() == "yes"
    )
    llm_flagged = sum(1 for s in sessions.values() if s["session_llm_flagged"])

    cat_counter = Counter()
    for s in sessions.values():
        for cat in s["llm_flag_counter"]:
            cat_counter[cat] += 1

    print()
    print("  +---------------------------------------------------+")
    print("  |              DETECTION LOG SUMMARY                 |")
    print("  +---------------------------------------------------+")
    print(f"  |  Total sessions               : {total:>6}            |")
    print(f"  |  Originally flagged (yes)     : {orig_flagged:>6}            |")
    print(f"  |  LLM flagged (sessions)       : {llm_flagged:>6}            |")
    if n_turns_written:
        print(f"  |  Total turns                  : {n_turns_written:>6}            |")
        print(f"  |  LLM-flagged turns            : {n_flagged_turns:>6}            |")
    print("  +---------------------------------------------------+")
    print("  |  Match Status:                                     |")
    print(f"  |    BOTH_FLAGGED (both flagged)   : {status_counts.get('BOTH_FLAGGED', 0):>6}            |")
    print(f"  |    ASTROTALK_ONLY (orig only)    : {status_counts.get('ASTROTALK_ONLY', 0):>6}            |")
    print(f"  |    LLM_ONLY (new)                : {status_counts.get('LLM_ONLY', 0):>6}            |")
    print(f"  |    BOTH_CLEAN                    : {status_counts.get('BOTH_CLEAN', 0):>6}            |")
    print("  +---------------------------------------------------+")
    dismissed = status_counts.get("ASTROTALK_ONLY", 0)
    rate = dismissed / max(orig_flagged, 1) * 100
    print(f"  |  Dismissed: {dismissed}/{orig_flagged} = {rate:.1f}% of original flags        |")
    print("  +---------------------------------------------------+")
    print("  |  Flag Categories (sessions):                       |")
    for cat, cnt in cat_counter.most_common():
        label = f"    {cat}"
        print(f"  |  {label:<38}: {cnt:>4} |")
    print("  +---------------------------------------------------+")
    print()

    if session_out:
        print(f"  Session log  : {session_out}")
    if detailed_out:
        print(f"  Detailed log : {detailed_out}")
    print()


# ─── main ───────────────────────────────────────────────────────────────────
def run(input_path: Path, out_dir: Path, mode: str, file_label: str = "") -> None:
    """Process a single CSV file and write detection logs.

    Args:
        input_path: Path to the input CSV file.
        out_dir:    Directory where output CSVs are written.
        mode:       'session', 'detailed', or 'both'.
        file_label: Optional label derived from the source filename.
                    Used as a prefix to avoid output collisions when
                    processing multiple files from a directory.
    """
    print(f"  Input  : {input_path}")
    print(f"  Output : {out_dir}/")
    print(f"  Mode   : {mode}")
    print()

    if not input_path.exists():
        print(f"  ERROR: Input file not found: {input_path}")
        sys.exit(1)

    # ── Load once, reuse for both modes ──────────────────────────────────
    print("  Loading CSV...")
    all_rows = load_all_rows(input_path)
    print(f"  Loaded {len(all_rows)} turns")

    if not all_rows:
        print("  SKIP: CSV is empty, nothing to process.")
        return

    print("  Building session context...")
    sessions = build_session_context(all_rows)
    print(f"  Found {len(sessions)} sessions")

    stamp = datetime.now().strftime("%Y%m%d")
    # Build output filename prefix: use file_label if provided, else generic
    prefix = f"{file_label}_" if file_label else ""

    session_out = None
    detailed_out = None
    n_turns_written = 0
    n_flagged_turns = 0

    # ── Session-level ────────────────────────────────────────────────────
    if mode in ("session", "both"):
        session_out = out_dir / f"{prefix}detection_logs_{stamp}.csv"
        print(f"\n  Generating session-level log -> {session_out.name}")
        log_rows = generate_session_log(sessions, session_out)
        print(f"  Written {len(log_rows)} session rows")

    # ── Detailed turn-level ──────────────────────────────────────────────
    if mode in ("detailed", "both"):
        detailed_out = out_dir / f"{prefix}detection_logs_detailed_{stamp}.csv"
        print(f"\n  Generating detailed turn-level log -> {detailed_out.name}")
        n_turns_written, n_flagged_turns = generate_detailed_log(
            all_rows, sessions, detailed_out
        )
        print(f"  Written {n_turns_written} turn rows ({n_flagged_turns} flagged)")

    # ── Summary ──────────────────────────────────────────────────────────
    print_summary(sessions, session_out, detailed_out, n_turns_written, n_flagged_turns)


def _collect_csv_files(input_path: Path) -> list[Path]:
    """If input_path is a directory, return all *.csv files sorted.
    If it's a file, return it as a single-element list."""
    if input_path.is_dir():
        csv_files = sorted(input_path.glob("*.csv"))
        if not csv_files:
            print(f"  ERROR: No CSV files found in directory: {input_path}")
            sys.exit(1)
        return csv_files
    else:
        return [input_path]


def _label_from_filename(path: Path) -> str:
    """Derive a clean label from a CSV filename for use in output naming.
    e.g. 'unprocessed_sessions_merged.csv' -> 'unprocessed_sessions_merged'
    """
    return path.stem


def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Generate chat detection logs (session-level, turn-level, or both) "
            "from one or more CSV files. --input accepts a single CSV file or "
            "a directory containing CSVs (all will be processed)."
        )
    )
    p.add_argument(
        "--input",
        default=DEFAULT_INPUT,
        help=(
            "Path to a CSV file OR a directory of CSVs. "
            "If a directory, every *.csv inside is processed one by one."
        ),
    )
    p.add_argument(
        "--mode",
        default="both",
        choices=VALID_MODES,
        help="Which log(s) to generate: session, detailed, or both (default: both)",
    )
    p.add_argument(
        "--out-dir",
        default=None,
        help="Output directory (default: exports/)",
    )
    args = p.parse_args()

    input_path = Path(args.input)
    out_dir = (
        Path(args.out_dir) if args.out_dir
        else Path(__file__).resolve().parents[1] / "exports"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_files = _collect_csv_files(input_path)
    is_directory = input_path.is_dir()
    total_files = len(csv_files)

    print("=" * 60)
    print("  Chat Detection Log Generator")
    print("=" * 60)

    if is_directory:
        print(f"  Directory : {input_path}")
        print(f"  CSV files : {total_files}")
        print()

    for idx, csv_file in enumerate(csv_files, 1):
        if total_files > 1:
            print("-" * 60)
            print(f"  [{idx}/{total_files}] {csv_file.name}")
            print("-" * 60)

        # Use filename as label when processing a directory (avoids collisions)
        label = _label_from_filename(csv_file) if is_directory else ""
        run(csv_file, out_dir, args.mode, file_label=label)

    if total_files > 1:
        print("=" * 60)
        print(f"  DONE: Processed {total_files} CSV files.")
        print(f"  Output directory: {out_dir}")
        print("=" * 60)


if __name__ == "__main__":
    main()
