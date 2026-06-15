"""Orchestrates full batch run across the dataset"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from engine.data_loader          import DataLoader
from engine.language_detector    import LanguageDetector   # noqa: F401 — used internally by LLMClassifier
from engine.classifier           import LLMClassifier
from engine.aggregator           import SessionAggregator
from engine.consultant_analyser  import ConsultantAnalyser
from engine.verdict_rules        import (
    get_db_confidence_for_verdict,
    get_db_verdict_for_flags,
)

from store.db     import initialise_db, DB_PATH
from store.writer import write_session_complete

from pipeline.logger     import get_logger, log_session_result, log_batch_summary, log_error
from pipeline.checkpoint import (
    load_checkpoint, save_checkpoint, clear_checkpoint, checkpoint_exists,
)

from dotenv import load_dotenv
load_dotenv()
# ---------------------------------------------------------------------------
# Vocabulary translation: engine → store schema
# ---------------------------------------------------------------------------

# SessionResult.final_severity → sessions.overall_verdict
_VERDICT_MAP: dict[str, str] = {
    "Red":   "SEVERE",
    "Amber": "FLAGGED",
    "Green": "CLEAN",
}

# confidence string → numeric score stored in the DB
_CONF_SCORE: dict[str, float] = {
    "High":   0.9,
    "Medium": 0.6,
    "Low":    0.3,
}

# Engine severity strings (both Red/Amber and High/Medium/Low) → flags.severity
_FLAG_SEV: dict[str, str] = {
    "Red":    "HIGH",
    "Amber":  "MEDIUM",
    "High":   "HIGH",
    "Medium": "MEDIUM",
    "Low":    "LOW",
}

# Inverted confidence → false_positive_risk
_FP_RISK: dict[str, str] = {
    "High":   "LOW",
    "Medium": "MEDIUM",
    "Low":    "HIGH",
}


# ---------------------------------------------------------------------------
# Internal helpers — build store-compatible dicts from engine outputs
# ---------------------------------------------------------------------------

def _to_legacy_session(session: dict) -> dict:
    """Convert new DataLoader session dict to the legacy shape expected by
    ConsultantAnalyser and LLMClassifier (role/message/timestamp keys)."""
    legacy_messages = [
        {
            "role":       "CONSULTANT" if m.get("speaker") == "ASTROLOGER" else "USER",
            "message":    m.get("message_text", ""),
            "timestamp":  m.get("timestamp"),
            "message_id": m.get("turn_id"),
        }
        for m in session.get("messages", [])
    ]
    try:
        order_id = int(session.get("session_id"))
    except (TypeError, ValueError):
        order_id = 0
    return {
        "order_id":        order_id,
        "consultant_name": session.get("astrologer_id") or "",
        "user_name":       "",
        "category":        None,
        "messages":        legacy_messages,
    }


def _build_session_data(session: dict, result, classification) -> dict:
    return {
        "session_id":              str(session.get("session_id")),
        "astrologer_id":           session.get("astrologer_id"),
        "user_id":                 None,
        "session_start":           session.get("session_start"),
        "session_end":             session.get("session_end"),
        "duration_minutes":        session.get("duration_minutes"),
        "session_type":            session.get("session_type", "chat"),
        "session_date":            session.get("session_date"),
        "month":                   session.get("month"),
        "language_code":           session.get("language_code"),
        "language_detected":       result.primary_language or session.get("language_detected"),
        "overall_verdict":         _VERDICT_MAP.get(result.final_severity, "CLEAN"),
        "confidence_score":        _CONF_SCORE.get(result.confidence_level, 0.3),
        "astrotalk_flagged":       session.get("astrotalk_flagged", 0),
        "astrotalk_flag_category": None,
        "astrotalk_severity":      None,
    }


def _apply_flag_verdict(session_data: dict, flags: list[dict]) -> dict:
    """
    Apply canonical flag-combination verdict rules to a session row.
    This is shared policy logic for automated LLM/REGEX outputs.
    """
    verdict = get_db_verdict_for_flags(
        f.get("category_code") for f in flags
    )
    session_data["overall_verdict"] = verdict
    session_data["confidence_score"] = get_db_confidence_for_verdict(verdict)
    return session_data


def _build_turns(messages: list[dict]) -> list[dict]:
    return [
        {
            "turn_id":           m.get("turn_id"),
            "speaker":           m.get("speaker", "UNKNOWN"),
            "message_text":      m.get("message_text", ""),
            "timestamp":         m.get("timestamp"),
            "language_detected": m.get("language_detected"),
            "is_automated":      m.get("is_automated", 0),
            "has_link":          m.get("has_link", 0),
        }
        for m in messages
    ]


def _build_flags(classification, profile) -> list[dict]:
    flags: list[dict] = []

    # Layer 2 — LLM intent matches, one row per (chunk, intent) occurrence
    for cr in classification.chunk_results:
        for im in cr.intents_triggered:
            flags.append({
                "turn_id":             None,
                "category_code":       im.intent_id,
                "detection_layer":     "LLM",
                "severity":            _FLAG_SEV.get(im.severity, "MEDIUM"),
                "confidence_score":    _CONF_SCORE.get(im.confidence, 0.3),
                "reasoning":           im.reason,
                "false_positive_risk": _FP_RISK.get(im.confidence, "MEDIUM"),
                "pattern_matched":     None,
            })

    # Layer 1 — rule-based flags from ConsultantAnalyser
    for flag in profile.red_flags:
        flags.append({
            "turn_id":             None,
            "category_code":       flag.flag_type,
            "detection_layer":     "REGEX",
            "severity":            _FLAG_SEV.get(flag.severity, "MEDIUM"),
            "confidence_score":    _CONF_SCORE.get(flag.severity, 0.3),
            "reasoning":           f"Consultant behaviour: {flag.flag_type}",
            "false_positive_risk": _FP_RISK.get(flag.severity, "MEDIUM"),
            "pattern_matched":     flag.flag_type,
        })

    return flags


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_unprocessed_session_ids() -> set[str]:
    """Returns session_ids from the DB where overall_verdict = 'UNPROCESSED'."""
    from store.db import get_connection
    with get_connection() as conn:
        cursor = conn.execute(
            "SELECT session_id FROM sessions WHERE overall_verdict = 'UNPROCESSED'"
        )
        return {row[0] for row in cursor.fetchall()}


# ---------------------------------------------------------------------------
# Public pipeline functions
# ---------------------------------------------------------------------------

def process_session(
    session: dict,
    logger,
    *,
    _analyser: ConsultantAnalyser | None = None,
    _clf: LLMClassifier | None = None,
    _aggregator: SessionAggregator | None = None,
) -> dict | None:
    """
    Runs a single session through the full engine pipeline.
    Returns a structured result dict or None on any failure.

    The *_analyser, _clf, _aggregator keyword arguments are internal and
    allow run_batch() to pass pre-constructed engine objects for reuse
    across the batch.  When called standalone the function instantiates
    its own engine objects.
    """
    session_id = str(session.get("session_id", "unknown"))
    try:
        analyser   = _analyser   or ConsultantAnalyser()
        clf        = _clf        or LLMClassifier()
        aggregator = _aggregator or SessionAggregator()

        legacy_session = _to_legacy_session(session)

        # Layer 1 — rule-based consultant behaviour analysis
        profile = analyser.analyse(legacy_session)

        # Layer 2 — LLM classification (also runs language detection + chunking internally)
        classification = clf.classify_session(legacy_session)

        # Verdict aggregation
        result = aggregator.aggregate(classification, profile, human_label=None)

        flags = _build_flags(classification, profile)
        session_data = _build_session_data(session, result, classification)
        session_data = _apply_flag_verdict(session_data, flags)

        return {
            "session_id":   session_id,
            "session_data": session_data,
            "turns":        _build_turns(session.get("messages", [])),
            "flags":        flags,
        }

    except Exception as exc:
        log_error(logger, session_id, exc)
        return None


def run_batch(data_path: str, fresh_run: bool = False, limit: int = None) -> None:
    logger = get_logger(__name__)

    initialise_db()

    if fresh_run and checkpoint_exists():
        confirm = input(
            "WARNING: This will clear the checkpoint and reprocess all sessions. "
            "Continue? [y/N]: "
        ).strip().lower()
        if confirm != "y":
            logger.info("Fresh run cancelled by user.")
            return
        clear_checkpoint()
        logger.info("Checkpoint cleared — fresh run in progress.")

    loader   = DataLoader(data_path)
    sessions = loader.load_sessions()

    processed_ids  = load_checkpoint()
    db_unprocessed = _get_unprocessed_session_ids()

    if db_unprocessed:
        pending = [s for s in sessions if str(s.get("session_id")) in db_unprocessed]
        print(f"  DB-based pending: {len(pending)} sessions with overall_verdict='UNPROCESSED'")
    else:
        pending = [s for s in sessions if str(s.get("session_id")) not in processed_ids]
        logger.info(
            "No UNPROCESSED sessions in DB — falling back to checkpoint "
            "(total=%d  already_done=%d  to_process=%d)",
            len(sessions), len(processed_ids), len(pending),
        )

    if limit is not None:
        pending = pending[:limit]
        print(f"  --limit applied: processing {len(pending)} of {len(sessions)} total sessions")

    logger.info("Sessions to process: %d", len(pending))

    # Instantiate engine objects once — avoids repeated API key checks and
    # handler setup on LLMClassifier for each session.
    analyser   = ConsultantAnalyser()
    clf        = LLMClassifier()
    aggregator = SessionAggregator()

    counts = {"CLEAN": 0, "FLAGGED": 0, "SEVERE": 0, "error": 0}
    batch_start     = time.time()
    processed_count = 0

    try:
        for session in pending:
            session_id = str(session.get("session_id", ""))
            t_start    = time.time()

            result = process_session(
                session, logger,
                _analyser=analyser, _clf=clf, _aggregator=aggregator,
            )

            if result is not None:
                write_session_complete(
                    result["session_id"],
                    result["session_data"],
                    result["turns"],
                    result["flags"],
                )
                verdict = result["session_data"]["overall_verdict"]
                counts[verdict] = counts.get(verdict, 0) + 1
                log_session_result(
                    logger, session_id, verdict,
                    len(result["flags"]), time.time() - t_start,
                )
            else:
                counts["error"] += 1

            processed_ids.add(session_id)
            processed_count += 1

            if processed_count % 50 == 0:
                save_checkpoint(processed_ids)

    except KeyboardInterrupt:
        logger.info("Run interrupted — saving checkpoint before exit.")
        save_checkpoint(processed_ids)
        return

    save_checkpoint(processed_ids)

    log_batch_summary(
        logger,
        total=processed_count,
        clean=counts.get("CLEAN", 0),
        flagged=counts.get("FLAGGED", 0),
        severe=counts.get("SEVERE", 0),
        errors=counts.get("error", 0),
        elapsed_minutes=(time.time() - batch_start) / 60,
    )


def ingest_only(data_path: str) -> None:
    """
    Loads sessions from data_path and writes them to the DB with
    overall_verdict='UNPROCESSED'. Skips sessions already in the
    checkpoint. No AI classification is run.
    """
    print("=" * 60)
    print("  AstroTalk Content Safety — Ingestion Only Mode")
    print("=" * 60)

    initialise_db()

    loader   = DataLoader(data_path)
    sessions = loader.load_sessions()

    processed_ids = load_checkpoint()
    pending = [s for s in sessions if str(s["session_id"]) not in processed_ids]
    print(
        f"Sessions: total={len(sessions)}  "
        f"already_ingested={len(processed_ids)}  "
        f"to_ingest={len(pending)}"
    )

    analyser  = ConsultantAnalyser()
    n_written = 0

    try:
        for session in pending:
            session_id = str(session["session_id"])

            session_data = {
                "session_id":              session_id,
                "astrologer_id":           session.get("astrologer_id"),
                "user_id":                 None,
                "session_start":           session.get("session_start"),
                "session_end":             session.get("session_end"),
                "duration_minutes":        session.get("duration_minutes"),
                "session_type":            session.get("session_type", "chat"),
                "session_date":            session.get("session_date"),
                "month":                   session.get("month"),
                "language_code":           session.get("language_code"),
                "language_detected":       session.get("language_detected"),
                "overall_verdict":         "UNPROCESSED",
                "confidence_score":        None,
                "astrotalk_flagged":       session.get("astrotalk_flagged", 0),
                "astrotalk_flag_category": None,
                "astrotalk_severity":      None,
                "review_status":           "PENDING",
            }

            turns = [
                {
                    "turn_id":           msg["turn_id"],
                    "speaker":           msg["speaker"],
                    "message_text":      msg["message_text"],
                    "timestamp":         msg["timestamp"],
                    "language_detected": msg.get("language_detected"),
                    "is_automated":      msg["is_automated"],
                }
                for msg in session.get("messages", [])
            ]

            write_session_complete(session_id, session_data, turns, [])

            re_engage_flags = analyser.detect_post_session_messages(
                session.get('messages', [])
            )
            link_flags = [
                {
                    'category_code':       'EXTERNAL_MEDIA_CONTENT',
                    'detection_layer':     'REGEX',
                    'severity':            'MEDIUM',
                    'confidence_score':    0.7,
                    'reasoning':           'Message contains an external link or media — requires manual verification',
                    'false_positive_risk': 'MEDIUM',
                    'pattern_matched':     (turn.get('message_text') or '')[:100],
                    'turn_id':             turn.get('turn_id'),
                }
                for turn in turns
                if turn.get('has_link') == 1
            ]

            all_auto_flags = re_engage_flags + link_flags
            if all_auto_flags:
                from store.writer import write_flags
                write_flags(session_id, all_auto_flags)
                from store.db import get_connection
                verdict = get_db_verdict_for_flags(
                    f.get("category_code") for f in all_auto_flags
                )
                with get_connection() as conn:
                    conn.execute(
                        """UPDATE sessions
                           SET overall_verdict = ?,
                               confidence_score = ?
                           WHERE session_id = ?
                           AND overall_verdict = 'UNPROCESSED'""",
                        (
                            verdict,
                            get_db_confidence_for_verdict(verdict),
                            session_id,
                        ),
                    )

            processed_ids.add(session_id)
            n_written += 1

            if n_written % 100 == 0:
                save_checkpoint(processed_ids)

    except KeyboardInterrupt:
        save_checkpoint(processed_ids)
        print(f"\nIngestion interrupted — {n_written} sessions written before interrupt.")
        return

    save_checkpoint(processed_ids)

    print()
    print("=" * 60)
    print(f"  Ingestion complete. {n_written} sessions written to {DB_PATH}")
    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AstroTalk Content Safety — Batch Pipeline",
    )
    parser.add_argument(
        "--data",
        required=True,
        metavar="FILE",
        help="Path to the input data file or folder",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore checkpoint and reprocess all sessions from scratch",
    )
    parser.add_argument(
        "--ingest-only",
        action="store_true",
        help="Load sessions into the DB without running AI classification",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process at most N sessions (useful for test runs)",
    )
    args = parser.parse_args()

    if args.ingest_only:
        ingest_only(args.data)
    else:
        print("=" * 60)
        print("  AstroTalk Content Safety — Batch Pipeline Starting")
        print("=" * 60)
        run_batch(args.data, fresh_run=args.fresh, limit=args.limit)
        print("=" * 60)
        print(f"  Batch run complete. Results written to {DB_PATH}")
        print("=" * 60)


if __name__ == "__main__":
    main()
