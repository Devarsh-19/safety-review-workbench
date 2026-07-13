"""
audio_analytics.py

Read-only analytics over the audio review database (store/audio_review.db).
Produces seven cuts of the moderation data:

  1. Violation breakdown   - flags & distinct sessions per intent, severity split
  2. Confidence analysis   - conf stats per intent, near-threshold counts, histogram
  3. Co-occurrence         - intent pairs that appear together in the same session
  4. Tone distribution     - segment tone counts, and tone of flag-carrying segments
  5. Temporal placement    - where in the call (early/mid/late third) flags land
  6. Language mix          - sessions / flagged sessions / flags per detected language
  7. Video vs audio-only   - has_video split and per-group flag rate

Flag semantics match the review queue and submit gate (store/audio_db.py):
an "active" flag is a non-DISMISSED row that is either an amendment or an
original with no amendment; amended originals are excluded so an edited flag
counts once. Pass --all-flags to analyse every LLM+manual row regardless of
review state.

Usage:
  python scripts/audio_analytics.py
  python scripts/audio_analytics.py --db store/audio_review.db
  python scripts/audio_analytics.py --all-flags
  python scripts/audio_analytics.py --csv out_dir/    # also dump per-intent CSVs

Read-only: never writes to the database.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "store" / "audio_review.db"

# CSAM_RISK is flagged at a lower confidence bar than everything else; the
# "near threshold" band is measured against each intent's own bar.
CONF_THRESHOLD = defaultdict(lambda: 0.5, {"CSAM_RISK": 0.2})
NEAR_BAND = 0.1  # a flag is "near threshold" if within this much of its bar


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load(conn: sqlite3.Connection) -> tuple[dict, dict, dict]:
    """Return (sessions, segments_by_session, flags_by_session)."""
    sessions = {
        r["s_id"]: dict(r)
        for r in conn.execute("SELECT * FROM audio_sessions")
    }
    segments: dict[int, list[dict]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM audio_segments"):
        segments[r["s_id"]].append(dict(r))
    flags: dict[int, list[dict]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM audio_flags"):
        flags[r["s_id"]].append(dict(r))
    return sessions, segments, flags


def active_flags(rows: list[dict]) -> list[dict]:
    """Active = amendment rows + originals with no amendment, minus DISMISSED.
    Mirrors _active_audio_flag_rows in store/audio_db.py."""
    amended_parents = {r["parent_flag_id"] for r in rows if r["parent_flag_id"] is not None}
    survivors = [
        r for r in rows
        if r["parent_flag_id"] is not None or r["flag_id"] not in amended_parents
    ]
    return [r for r in survivors if (r.get("status") or "") != "DISMISSED"]


def session_duration(session: dict, segs: list[dict]) -> float | None:
    """Real duration if the pipeline stored it, else the last segment end."""
    dur = session.get("duration_seconds")
    if dur:
        return float(dur)
    ends = [s["ts_end"] for s in segs if s.get("ts_end") is not None]
    return max(ends) if ends else None


def norm_tone(tone: str | None) -> str:
    return (tone or "UNKNOWN").strip().upper() or "UNKNOWN"


def norm_lang(lang: str | None) -> str:
    return (lang or "UNKNOWN").strip().upper() or "UNKNOWN"


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def header(title: str) -> None:
    print("\n" + "=" * 66)
    print(f"  {title}")
    print("=" * 66)


def bar(count: int, total: int, width: int = 24) -> str:
    if total <= 0:
        return ""
    filled = round(width * count / total)
    return "#" * filled + "." * (width - filled)


def two_col(rows: list[tuple[str, str]], left_head: str, right_head: str) -> None:
    if not rows:
        print("  (none)")
        return
    lw = max(len(left_head), max(len(r[0]) for r in rows))
    print(f"  {left_head:<{lw}}   {right_head}")
    print("  " + "-" * (lw + 3 + len(right_head)))
    for left, right in rows:
        print(f"  {left:<{lw}}   {right}")


# --------------------------------------------------------------------------- #
# 1. Violation breakdown
# --------------------------------------------------------------------------- #
def violation_breakdown(flags_by_session: dict) -> list[dict]:
    per_intent_flags = Counter()
    per_intent_sessions: dict[str, set] = defaultdict(set)
    per_intent_sev: dict[str, Counter] = defaultdict(Counter)
    total_flags = 0
    for s_id, flags in flags_by_session.items():
        for f in flags:
            intent = f["intent"] or "UNKNOWN"
            per_intent_flags[intent] += 1
            per_intent_sessions[intent].add(s_id)
            per_intent_sev[intent][(f["severity"] or "?").upper()] += 1
            total_flags += 1

    header("1. VIOLATION BREAKDOWN")
    print(f"  Total flags: {total_flags}   Distinct intents: {len(per_intent_flags)}\n")
    if not per_intent_flags:
        print("  (no flags)")
        return []

    name_w = max(len("INTENT"), max(len(i) for i in per_intent_flags))
    print(f"  {'INTENT':<{name_w}}   FLAGS   SESSIONS   SEVERITY (by flag)")
    print("  " + "-" * (name_w + 40))
    rows_out = []
    for intent, n in per_intent_flags.most_common():
        sev = per_intent_sev[intent]
        sev_str = ", ".join(f"{k}:{v}" for k, v in sev.most_common())
        sess = len(per_intent_sessions[intent])
        print(f"  {intent:<{name_w}}   {n:>5}   {sess:>8}   {sev_str}")
        rows_out.append({"intent": intent, "flags": n, "sessions": sess, "severity": sev_str})
    return rows_out


# --------------------------------------------------------------------------- #
# 2. Confidence analysis
# --------------------------------------------------------------------------- #
def confidence_analysis(flags_by_session: dict) -> None:
    by_intent: dict[str, list[float]] = defaultdict(list)
    for flags in flags_by_session.values():
        for f in flags:
            if f["conf"] is not None:
                by_intent[f["intent"] or "UNKNOWN"].append(float(f["conf"]))

    header("2. CONFIDENCE ANALYSIS")
    if not by_intent:
        print("  (no confidence values)")
        return

    name_w = max(len("INTENT"), max(len(i) for i in by_intent))
    print(f"  {'INTENT':<{name_w}}    N    MEAN   MIN    MAX   NEAR-THRESH")
    print("  " + "-" * (name_w + 44))
    for intent in sorted(by_intent, key=lambda i: -statistics.mean(by_intent[i])):
        vals = by_intent[intent]
        bar_lo = CONF_THRESHOLD[intent]
        near = sum(1 for v in vals if v < bar_lo + NEAR_BAND)
        print(f"  {intent:<{name_w}}  {len(vals):>3}   {statistics.mean(vals):.2f}  "
              f"{min(vals):.2f}   {max(vals):.2f}   {near:>3} (<{bar_lo + NEAR_BAND:.2f})")

    # Overall histogram in 0.1 bins.
    print("\n  Overall confidence histogram:")
    all_vals = [v for vals in by_intent.values() for v in vals]
    bins = Counter(min(int(v * 10), 9) for v in all_vals)
    total = len(all_vals)
    for b in range(10):
        lo, hi = b / 10, (b + 1) / 10
        c = bins.get(b, 0)
        print(f"    {lo:.1f}-{hi:.1f}  {c:>4}  {bar(c, total)}")


# --------------------------------------------------------------------------- #
# 3. Co-occurrence
# --------------------------------------------------------------------------- #
def co_occurrence(flags_by_session: dict) -> None:
    pair_counts = Counter()
    multi_intent_sessions = 0
    for flags in flags_by_session.values():
        intents = sorted({f["intent"] or "UNKNOWN" for f in flags})
        if len(intents) >= 2:
            multi_intent_sessions += 1
            for a, b in combinations(intents, 2):
                pair_counts[(a, b)] += 1

    header("3. INTENT CO-OCCURRENCE (same session)")
    print(f"  Sessions with >=2 distinct intents: {multi_intent_sessions}\n")
    if not pair_counts:
        print("  (no sessions carry two or more distinct intents)")
        return
    rows = [(f"{a}  +  {b}", str(n)) for (a, b), n in pair_counts.most_common()]
    two_col(rows, "INTENT PAIR", "SESSIONS")


# --------------------------------------------------------------------------- #
# 4. Tone distribution
# --------------------------------------------------------------------------- #
def tone_distribution(segments_by_session: dict, flags_by_session: dict) -> None:
    seg_tone = Counter()
    for segs in segments_by_session.values():
        for s in segs:
            seg_tone[norm_tone(s.get("tone"))] += 1

    # Tone of the segments that actually carry a flag.
    seg_lookup = {
        (s_id, s["seg_id"]): norm_tone(s.get("tone"))
        for s_id, segs in segments_by_session.items()
        for s in segs
    }
    flagged_tone = Counter()
    for s_id, flags in flags_by_session.items():
        for f in flags:
            tone = seg_lookup.get((s_id, f["seg_id"]))
            if tone:
                flagged_tone[tone] += 1

    header("4. TONE DISTRIBUTION")
    total_seg = sum(seg_tone.values())
    print(f"  All segments ({total_seg}):")
    for tone, c in seg_tone.most_common():
        print(f"    {tone:<14} {c:>4}  {bar(c, total_seg)}")

    print("\n  Tone of flag-carrying segments:")
    total_fl = sum(flagged_tone.values())
    if not total_fl:
        print("    (no flag maps to a segment tone)")
        return
    for tone, c in flagged_tone.most_common():
        print(f"    {tone:<14} {c:>4}  {bar(c, total_fl)}")


# --------------------------------------------------------------------------- #
# 5. Temporal placement
# --------------------------------------------------------------------------- #
def temporal_placement(sessions: dict, segments_by_session: dict, flags_by_session: dict) -> None:
    seg_start = {
        (s_id, s["seg_id"]): s.get("ts_start")
        for s_id, segs in segments_by_session.items()
        for s in segs
    }
    thirds = Counter()  # EARLY / MID / LATE
    placed = unplaced = 0
    for s_id, flags in flags_by_session.items():
        dur = session_duration(sessions.get(s_id, {}), segments_by_session.get(s_id, []))
        for f in flags:
            ts = f["ts_start"]
            if ts is None:
                ts = seg_start.get((s_id, f["seg_id"]))
            if ts is None or not dur:
                unplaced += 1
                continue
            frac = min(max(float(ts) / dur, 0.0), 0.999)
            thirds[("EARLY", "MID", "LATE")[int(frac * 3)]] += 1
            placed += 1

    header("5. TEMPORAL PLACEMENT (flag position within the call)")
    print(f"  Placed flags: {placed}   Unplaced (no timestamp/duration): {unplaced}\n")
    if not placed:
        print("  (no flags could be located on a timeline)")
        return
    for band in ("EARLY", "MID", "LATE"):
        c = thirds.get(band, 0)
        pct = 100 * c / placed
        print(f"    {band:<6} {c:>4}  ({pct:4.1f}%)  {bar(c, placed)}")


# --------------------------------------------------------------------------- #
# 6. Language mix
# --------------------------------------------------------------------------- #
def language_mix(sessions: dict, flags_by_session: dict) -> None:
    per_lang_sessions = Counter()
    per_lang_flagged = Counter()
    per_lang_flags = Counter()
    for s_id, sess in sessions.items():
        lang = norm_lang(sess.get("lang"))
        per_lang_sessions[lang] += 1
        n = len(flags_by_session.get(s_id, []))
        per_lang_flags[lang] += n
        if n:
            per_lang_flagged[lang] += 1

    header("6. LANGUAGE MIX")
    if not per_lang_sessions:
        print("  (no sessions)")
        return
    name_w = max(len("LANGUAGE"), max(len(l) for l in per_lang_sessions))
    print(f"  {'LANGUAGE':<{name_w}}   SESSIONS   FLAGGED   FLAGS   FLAGS/SESSION")
    print("  " + "-" * (name_w + 46))
    for lang, sess in per_lang_sessions.most_common():
        fl = per_lang_flags[lang]
        rate = fl / sess if sess else 0
        print(f"  {lang:<{name_w}}   {sess:>8}   {per_lang_flagged[lang]:>7}   "
              f"{fl:>5}   {rate:>11.2f}")


# --------------------------------------------------------------------------- #
# 7. Video vs audio-only
# --------------------------------------------------------------------------- #
def video_split(sessions: dict, flags_by_session: dict) -> None:
    # has_video NULL is treated as audio-only, matching the queue filter
    # (has_video = 0 OR has_video IS NULL) -> "no".
    groups = {"HAS VIDEO": [], "AUDIO-ONLY": []}
    for s_id, sess in sessions.items():
        key = "HAS VIDEO" if sess.get("has_video") else "AUDIO-ONLY"
        groups[key].append(s_id)

    header("7. VIDEO vs AUDIO-ONLY")
    print(f"  {'GROUP':<12}   SESSIONS   FLAGGED   FLAGS   FLAGS/SESSION")
    print("  " + "-" * 54)
    for key, ids in groups.items():
        sess = len(ids)
        flagged = sum(1 for s in ids if flags_by_session.get(s))
        flags = sum(len(flags_by_session.get(s, [])) for s in ids)
        rate = flags / sess if sess else 0
        print(f"  {key:<12}   {sess:>8}   {flagged:>7}   {flags:>5}   {rate:>11.2f}")


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
def write_csv(out_dir: Path, breakdown: list[dict]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "violation_breakdown.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["intent", "flags", "sessions", "severity"])
        for r in breakdown:
            w.writerow([r["intent"], r["flags"], r["sessions"], r["severity"]])
    print(f"\nWrote {path}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--db", default=str(DEFAULT_DB),
                    help=f"SQLite DB path (default: {DEFAULT_DB}).")
    ap.add_argument("--all-flags", action="store_true",
                    help="Analyse every flag row, not just active (non-dismissed) flags.")
    ap.add_argument("--csv", metavar="DIR",
                    help="Also write the violation breakdown to DIR/violation_breakdown.csv.")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        sys.exit(f"Database not found: {db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sessions, segments_by_session, raw_flags = load(conn)
    finally:
        conn.close()

    if args.all_flags:
        flags_by_session = {s: [f for f in fl] for s, fl in raw_flags.items()}
    else:
        flags_by_session = {s: active_flags(fl) for s, fl in raw_flags.items()}

    total_sessions = len(sessions)
    flagged_sessions = sum(1 for s in sessions if flags_by_session.get(s))

    print("=" * 66)
    print("  AUDIO MODERATION ANALYTICS")
    print("=" * 66)
    print(f"  DB              : {db_path}")
    print(f"  Flag scope      : {'all rows' if args.all_flags else 'active (non-dismissed)'}")
    print(f"  Sessions        : {total_sessions}")
    print(f"  Flagged sessions: {flagged_sessions}  "
          f"({100 * flagged_sessions / total_sessions:.1f}%)" if total_sessions else "")

    breakdown = violation_breakdown(flags_by_session)
    confidence_analysis(flags_by_session)
    co_occurrence(flags_by_session)
    tone_distribution(segments_by_session, flags_by_session)
    temporal_placement(sessions, segments_by_session, flags_by_session)
    language_mix(sessions, flags_by_session)
    video_split(sessions, flags_by_session)

    if args.csv:
        write_csv(Path(args.csv), breakdown)


if __name__ == "__main__":
    main()
