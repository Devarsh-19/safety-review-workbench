"""
merge.py
========
Merge LLM moderation output back onto the original input CSV.

Joins on (session_id, turn_id) and appends exactly two columns —
`llm_flag` and `confidence_score`. Original rows/columns are preserved; turns
with multiple flags are `|`-joined (scores in the same order). Rows with no
flag (clean / automated / unmatched) get empty strings.

Accepts either results file:
  - moderation_raw.json     (raw responses; parsed here on the fly)
  - moderation_results.json (already parsed; intents_triggered used directly)

Usage:
    cd llm_call
    python merge.py --input to_check.csv
    python merge.py --input to_check.csv --results moderation_raw.json
    python merge.py --input to_check.csv --output to_check_flagged.csv
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from parser import parse_llm_response


def _norm_turn(v) -> str:
    """Normalise a turn id to a comparable string ('5', '5.0', 5 -> '5')."""
    try:
        return str(int(float(v)))
    except (ValueError, TypeError):
        return str(v).strip()


def build_flag_map(results: dict) -> dict:
    """(session_id, turn_id) -> [(llm_flag, confidence_score), ...] from results.

    Handles both raw entries (parse raw_response) and already-parsed entries
    (use intents_triggered directly). Skips api_errors and unparseable rows.
    """
    flag_map: dict = {}
    for sid, entry in results.items():
        if entry.get("status") == "api_error":
            continue

        intents = entry.get("intents_triggered")
        if intents is None:                       # raw entry -> parse on the fly
            try:
                intents = parse_llm_response(entry.get("raw_response", "")).get(
                    "intents_triggered", []
                )
            except Exception:
                continue                          # unparseable -> skip

        for it in intents:
            key = (str(sid).strip(), _norm_turn(it.get("turn_id")))
            flag_map.setdefault(key, []).append(
                (str(it.get("llm_flag", "")), it.get("confidence_score", ""))
            )
    return flag_map


def merge(input_csv: Path, results_json: Path, output_csv: Path) -> pd.DataFrame:
    """Merge results onto the input CSV and write output_csv. Returns the df."""
    df = pd.read_csv(input_csv)

    with open(results_json, encoding="utf-8") as f:
        results = json.load(f)
    flag_map = build_flag_map(results)

    turn_col = "turn_id" if "turn_id" in df.columns else \
               "message_seq" if "message_seq" in df.columns else None
    if not turn_col:
        raise ValueError("No turn_id / message_seq column found in the input CSV.")

    def _row_flags(row):
        hits = flag_map.get(
            (str(row["session_id"]).strip(), _norm_turn(row[turn_col])), []
        )
        if not hits:
            return pd.Series({"llm_flag": "", "confidence_score": ""})
        return pd.Series({
            "llm_flag": "|".join(h[0] for h in hits),
            "confidence_score": "|".join(str(h[1]) for h in hits),
        })

    df[["llm_flag", "confidence_score"]] = df.apply(_row_flags, axis=1)
    df.to_csv(output_csv, index=False)
    return df


def main():
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Merge LLM moderation flags onto the original CSV "
                    "(joins on session_id + turn_id)."
    )
    parser.add_argument("--input", type=str, required=True,
                        help="Original input CSV of session messages.")
    parser.add_argument("--results", type=str, default="moderation_raw.json",
                        help="Results JSON: moderation_raw.json or "
                             "moderation_results.json (default: moderation_raw.json).")
    parser.add_argument("--output", type=str, default=None,
                        help="Output CSV (default: <input>_merged.csv).")
    args = parser.parse_args()

    here = Path(__file__).parent

    def _resolve(p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else here / path

    input_csv = _resolve(args.input)
    results_json = _resolve(args.results)
    output_csv = (_resolve(args.output) if args.output
                  else input_csv.with_name(input_csv.stem + "_merged.csv"))

    if not input_csv.exists():
        print(f"  [X] Input CSV not found: {input_csv}")
        sys.exit(1)
    if not results_json.exists():
        print(f"  [X] Results JSON not found: {results_json}")
        sys.exit(1)

    df = merge(input_csv, results_json, output_csv)
    flagged = int((df["llm_flag"] != "").sum())
    print(f"  Merged -> {output_csv}")
    print(f"  Rows: {len(df)} | flagged: {flagged} | clean: {len(df) - flagged}")


if __name__ == "__main__":
    main()
