"""
data_loader.py
Loads AstroTalk CSV data (one row per message) and reconstructs
session objects for the classification pipeline.

Accepts either a single CSV file path or a folder of CSV files.
All .csv files in a folder are concatenated and processed as one dataset.

Confirmed CSV schema
--------------------
  session_id           - integer
  message_seq          - integer (turn ID)
  sender               - 'USER' or 'ASTROLOGER'
  message_text         - string
  is_automated_message - integer 0/1
  sent_at_ist          - '2026-03-19 04:02:25+00:00' (ISO 8601 with tz offset)
  has_link             - integer 0/1
  month                - integer 1-12
  flagged              - 'yes' or 'no'
  language             - comma-separated numeric codes e.g. '1,2,5'

Note: session_date and astrologer_id are NOT in this CSV.
session_date is derived from the earliest sent_at_ist in the session.

Usage
-----
    loader   = DataLoader('data/raw/astrotalk_data.csv')
    loader   = DataLoader('data/raw/')
    sessions = loader.load_sessions()   # -> list[dict]
"""

from __future__ import annotations

import sys
import warnings
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

# Allow running this file directly (python engine/data_loader.py)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ---------------------------------------------------------------------------
# AstroTalk numeric language code → human-readable name
# Code 16 is intentionally absent from this map.
# ---------------------------------------------------------------------------
LANGUAGE_MAP: dict[int, str] = {
    1:  'English',
    2:  'Hindi',
    3:  'Tamil',
    4:  'Punjabi',
    5:  'Marathi',
    6:  'Gujarati',
    7:  'Bengali',
    8:  'French',
    9:  'Odia',
    10: 'Telugu',
    11: 'Kannada',
    12: 'Malayalam',
    13: 'Sanskrit',
    14: 'Assamese',
    15: 'German',
    17: 'Spanish',
    18: 'Marwari',
    19: 'Manipuri',
    20: 'Urdu',
    21: 'Sindhi',
    22: 'Kashmiri',
    23: 'Bodo',
    24: 'Nepali',
}

# ---------------------------------------------------------------------------
# Month integer → name
# ---------------------------------------------------------------------------
MONTH_MAP: dict[int, str] = {
    1:  'January',
    2:  'February',
    3:  'March',
    4:  'April',
    5:  'May',
    6:  'June',
    7:  'July',
    8:  'August',
    9:  'September',
    10: 'October',
    11: 'November',
    12: 'December',
}

# ---------------------------------------------------------------------------
# Speaker normalisation (kept for reference; logic now in _normalise_speaker)
# ---------------------------------------------------------------------------
_SPEAKER_MAP = {
    "consultant": "ASTROLOGER",
    "user":       "USER",
}


class DataLoader:
    """
    Reads AstroTalk CSV exports (one row per message) and exposes
    clean session dicts for the classification pipeline.

    Initialise with a file path or folder path:
        loader = DataLoader('data/raw/astrotalk_data.csv')
        loader = DataLoader('data/raw/')

    Public interface:
        sessions = loader.load_sessions()  -> list[dict]
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._sessions: list[dict[str, Any]] = []
        self._loaded = False
        self._duplicates_removed = 0
        from engine.language_detector import LanguageDetector
        self.language_detector = LanguageDetector()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_sessions(self) -> list[dict[str, Any]]:
        """
        Load CSV(s), build session dicts, print a summary, and return
        the session list. Keeps the same public interface as the previous
        loader so batch_runner.py requires no changes.
        """
        df = self._load_dataframe()
        self._sessions = self._build_sessions(df)
        self._loaded = True
        self._print_summary()
        return self._sessions

    # ------------------------------------------------------------------
    # Private — I/O
    # ------------------------------------------------------------------

    def _load_dataframe(self) -> pd.DataFrame:
        """
        Load one CSV or all .csv files in a folder into a single DataFrame.
        Unreadable files are skipped with a warning; raises if nothing loads.
        All columns are read as strings to avoid pandas type inference
        silently mangling IDs or codes.
        """
        if self.path.is_dir():
            csv_files = sorted(self.path.glob("*.csv"))
            if not csv_files:
                raise FileNotFoundError(
                    f"No .csv files found in directory: {self.path}"
                )
            frames: list[pd.DataFrame] = []
            for f in csv_files:
                try:
                    frames.append(
                        pd.read_csv(f, dtype=str, keep_default_na=False)
                    )
                    print(f"[DataLoader] Loaded: {f.name}")
                except Exception as exc:
                    warnings.warn(
                        f"[DataLoader] Skipping {f.name} — could not read: {exc}"
                    )
            if not frames:
                raise RuntimeError(
                    f"All CSV files in {self.path} failed to load."
                )
            return pd.concat(frames, ignore_index=True)

        if not self.path.exists():
            raise FileNotFoundError(f"CSV file not found: {self.path}")
        df = pd.read_csv(self.path, dtype=str, keep_default_na=False)
        print(f"[DataLoader] Loaded: {self.path.name}")
        return df

    # ------------------------------------------------------------------
    # Private — session construction
    # ------------------------------------------------------------------

    def _build_sessions(self, df: pd.DataFrame) -> list[dict[str, Any]]:
        """
        Group the flat message rows by session_id and build one
        session dict per group. Returns a list sorted by session_start.
        """
        # Drop rows whose session_id is null or empty string
        df = df[df["session_id"].str.strip().astype(bool)].copy()

        sessions: list[dict[str, Any]] = []

        for session_id, group in df.groupby("session_id", sort=False):
            session_id = str(session_id).strip()

            # Filter out automated messages (is_automated_message = 1)
            # Only proceed with real messages (is_automated_message = 0)
            group = group.copy()
            group["_is_auto"] = group["is_automated_message"].apply(self._normalise_automated)
            group = group[group["_is_auto"] == 0].drop(columns=["_is_auto"])

            # Skip sessions that have no real messages after filtering
            if group.empty:
                continue

            # Dedup exact-duplicate rows (export artefacts)
            group, removed = self._dedup_messages(group)
            self._duplicates_removed += removed

            # ── Timestamps ────────────────────────────────────────────
            timestamps = group["sent_at_ist"].apply(self._parse_timestamp)
            valid_ts   = [t for t in timestamps if t is not None]
            session_start = min(valid_ts) if valid_ts else None
            session_end   = max(valid_ts) if valid_ts else None
            # Derive session_date from earliest timestamp — not in CSV directly
            session_date  = session_start[:10] if session_start else None
            duration = self._calc_duration(session_start, session_end)

            # ── AstroTalk flag ─────────────────────────────────────────
            astrotalk_flagged = (
                1
                if (group["flagged"].str.strip().str.lower() == "yes").any()
                else 0
            )

            # ── Scalar session fields (first non-empty value in group) ─
            def first_val(col: str) -> str | None:
                if col not in group.columns:
                    return None
                nonempty = group[col].str.strip()
                nonempty = nonempty[nonempty != ""]
                return nonempty.iloc[0] if not nonempty.empty else None

            # ── Month integer → name ───────────────────────────────────
            month_val = first_val("month")
            try:
                month_name = MONTH_MAP.get(int(month_val), str(month_val))
            except Exception:
                month_name = str(month_val) if month_val else None

            # ── Language code → name mapping ───────────────────────────
            language_code, language_detected = self._parse_language(
                first_val("language")
            )

            # ── Multilingual session check ─────────────────────────────
            raw_lang        = first_val("language")
            lang_str        = str(raw_lang).strip() if raw_lang else ''
            lang_codes      = [c.strip() for c in lang_str.split(',') if c.strip()]
            is_multilingual = len(lang_codes) > 1

            # ── Turn list ──────────────────────────────────────────────
            messages: list[dict[str, Any]] = []
            for _, row in group.iterrows():
                turn_id = self._parse_turn_id(
                    row.get("message_seq", ""), len(messages) + 1
                )
                # Per-turn language: run detector on multilingual sessions;
                # inherit session language otherwise.
                # detect() returns LanguageResult — use .primary_language for name.
                if is_multilingual:
                    try:
                        result        = self.language_detector.detect(
                                            str(row.get("message_text", ""))
                                        )
                        turn_language = result.primary_language if result else language_detected
                    except Exception:
                        turn_language = language_detected
                else:
                    turn_language = language_detected

                messages.append({
                    "turn_id":           turn_id,
                    "speaker":           self._normalise_speaker(
                                             row.get("sender", "")
                                         ),
                    "message_text":      str(row.get("message_text", "")).strip(),
                    "is_automated":      self._normalise_automated(
                                             row.get("is_automated_message", 0)
                                         ),
                    "timestamp":         self._parse_timestamp(
                                             row.get("sent_at_ist")
                                             or row.get("sent_at")
                                             or row.get("timestamp")
                                         ),
                    "language_detected": turn_language,
                    "has_link":          int(row.get("has_link", 0) or 0),
                })

            sessions.append({
                "session_id":              session_id,
                "astrologer_id":           (
                                               first_val("astrologer_id")
                                               if "astrologer_id" in group.columns
                                               else None
                                           ),
                "user_id":                 None,
                "session_date":            session_date,
                "month":                   month_name,
                "language_code":           language_code,
                "language_detected":       language_detected,
                "session_type":            "chat",
                "astrotalk_flagged":       astrotalk_flagged,
                "astrotalk_flag_category": None,
                "astrotalk_severity":      None,
                "session_start":           session_start,
                "session_end":             session_end,
                "duration_minutes":        duration,
                "messages":                messages,
            })

        # Sort by session_start ascending; sessions with no timestamp sort last
        sessions.sort(key=lambda s: s["session_start"] or "")
        return sessions

    @staticmethod
    def _dedup_messages(
        group: pd.DataFrame,
    ) -> tuple[pd.DataFrame, int]:
        """
        Drop rows that are exact duplicates on (sender, message_text,
        sent_at_ist). Handles repeated rows from multi-file export artefacts.
        First occurrence is kept.
        """
        before = len(group)
        group  = group.drop_duplicates(
            subset=["sender", "message_text", "sent_at_ist"], keep="first"
        )
        return group, before - len(group)

    # ------------------------------------------------------------------
    # Private — field normalisation
    # ------------------------------------------------------------------

    def _normalise_speaker(self, val: Any) -> str:
        if val is None:
            return 'UNKNOWN'
        v = str(val).strip().upper()
        if v == 'USER':
            return 'USER'
        if v in ('ASTROLOGER', 'CONSULTANT'):
            return 'ASTROLOGER'
        return 'UNKNOWN'

    @staticmethod
    def _normalise_automated(val: Any) -> int:
        """Safely coerce is_automated_message to 0 or 1."""
        if isinstance(val, bool):
            return int(val)
        try:
            return 1 if int(str(val).strip()) else 0
        except (ValueError, TypeError):
            return 0

    def _parse_language(self, val: Any) -> tuple:
        """
        Parse a language field that may contain comma-separated numeric codes
        (e.g. '1,2,5'). Returns (all_codes_str, primary_language_name).
        """
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return None, None

        raw = str(val).strip()

        codes = []
        for part in raw.split(','):
            part = part.strip()
            try:
                code = int(float(part))
                if code in LANGUAGE_MAP:
                    codes.append(code)
            except Exception:
                continue

        if not codes:
            return raw, None

        primary_code = codes[0]
        primary_name = LANGUAGE_MAP.get(primary_code)
        all_codes    = ','.join(str(c) for c in codes)
        return all_codes, primary_name

    def _parse_timestamp(self, val: Any) -> str | None:
        if val is None:
            return None
        if isinstance(val, float) and pd.isna(val):
            return None
        try:
            ts = pd.to_datetime(str(val).strip(), utc=True)
            return ts.isoformat()
        except Exception:
            return None

    def _calc_duration(self, start_str: str | None, end_str: str | None) -> float | None:
        if not start_str or not end_str:
            return None
        try:
            start = pd.to_datetime(start_str, utc=True)
            end   = pd.to_datetime(end_str,   utc=True)
            if end < start:
                return None
            delta = (end - start).total_seconds() / 60
            return round(delta, 1)
        except Exception:
            return None

    @staticmethod
    def _parse_turn_id(val: Any, fallback: int) -> int:
        """
        Parse message_seq as int. Returns fallback (sequential counter)
        if val is absent or non-numeric.
        """
        try:
            return int(str(val).strip())
        except (ValueError, TypeError):
            return fallback

    # ------------------------------------------------------------------
    # Private — summary
    # ------------------------------------------------------------------

    def _print_summary(self) -> None:
        sessions   = self._sessions
        total      = len(sessions)
        total_msgs = sum(len(s["messages"]) for s in sessions)
        flagged    = sum(1 for s in sessions if s["astrotalk_flagged"])

        all_dates = [
            s.get("session_date")
            for s in sessions
            if s.get("session_date")
        ]
        if all_dates:
            date_range_start = min(all_dates)
            date_range_end   = max(all_dates)
        else:
            all_ts = [
                s.get("session_start")
                for s in sessions
                if s.get("session_start")
            ]
            date_range_start = min(all_ts)[:10] if all_ts else "N/A"
            date_range_end   = max(all_ts)[:10] if all_ts else "N/A"

        print()
        print("=" * 40)
        print("  AstroTalk DataLoader - Session Summary")
        print("=" * 40)
        print(f"  Total sessions  : {total}")
        print(f"  Total messages  : {total_msgs}")
        print(f"  Duplicates rmvd : {self._duplicates_removed}")
        print(f"  Date range      : {date_range_start} to {date_range_end}")
        print(f"  Sessions flagged by AstroTalk: {flagged}")

        lang_counts = Counter(
            s["language_detected"]
            for s in sessions
            if s.get("language_detected")
        )
        if lang_counts:
            print("  Language distribution:")
            max_count = max(lang_counts.values())
            bar_max   = 20
            for lang, count in sorted(lang_counts.items(), key=lambda x: -x[1]):
                bar = "█" * round((count / max_count) * bar_max)
                print(f"    {lang:<14}: {bar:<20} {count}")

        print("=" * 40)
        print()


# ---------------------------------------------------------------------------
# Quick self-test — python engine/data_loader.py <path>
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="AstroTalk DataLoader self-test")
    parser.add_argument("path", help="CSV file or folder containing CSV files")
    args = parser.parse_args()

    loader   = DataLoader(args.path)
    sessions = loader.load_sessions()
    if sessions:
        s = sessions[0]
        print(f"First session : {s['session_id']}  ({len(s['messages'])} messages)")
        print(f"  astrologer  : {s['astrologer_id']}")
        print(f"  date        : {s['session_date']}  lang={s['language_code']}")
        print(f"  detected    : {s['language_detected']}")
        print(f"  flagged     : {s['astrotalk_flagged']}")
        if s['messages']:
            m = s['messages'][0]
            print(f"  first turn  : [{m['speaker']}] {m['message_text'][:60]}")
            print(f"    timestamp : {m['timestamp']}")
            print(f"    has_link  : {m['has_link']}")
