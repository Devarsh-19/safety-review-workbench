"""
aggregator.py
Rolls up chunk-level LLM classification results and consultant behaviour
analysis into a single session-level verdict (SessionResult).

Pipeline position:
  LLMClassifier.classify_session()  →  SessionAggregator.aggregate()
                                     →  SessionResult
"""

from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Allow running this file directly
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class IntentSummary:
    intent_id:         str
    intent_name:       str
    severity:          str
    occurrence_count:  int
    max_confidence:    str           # "High" | "Medium" | "Low"
    speakers_involved: list[str]     # ["USER", "CONSULTANT"]
    sample_trigger:    str           # first trigger message seen


@dataclass
class SessionResult:
    session_id:                  int
    final_severity:              str          # "Red" | "Amber" | "Green"
    intents_triggered:           list[IntentSummary]
    consultant_response_pattern: str
    severity_modifier_applied:   bool
    original_severity:           str          # before modifier
    primary_language:            str
    total_messages:              int
    total_chunks:                int
    successful_chunks:           int
    flagged_messages:            list[dict]   # trigger messages with context
    confidence_level:            str          # "High" | "Medium" | "Low"
    summary:                     str          # 2-3 sentence plain English
    recommended_action:          str
    mismatch_flag:               bool         # GT vs existing engine disagree


# ---------------------------------------------------------------------------
# Confidence ordering helper
# ---------------------------------------------------------------------------

_CONF_RANK = {"High": 3, "Medium": 2, "Low": 1}


def _higher_confidence(a: str, b: str) -> str:
    """Return the higher of two confidence strings."""
    return a if _CONF_RANK.get(a, 0) >= _CONF_RANK.get(b, 0) else b


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class SessionAggregator:
    """
    Aggregates chunk-level classification results into a single
    session-level SessionResult.

    Usage
    -----
    aggregator = SessionAggregator()
    result = aggregator.aggregate(
        classification,
        consultant_profile,
        human_label="Moderate",
        existing_engine_severity="Amber",
    )
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def aggregate(
        self,
        classification:           Any,   # SessionClassification
        consultant_profile:       Any,   # ConsultantProfile
        human_label:              str | None = None,
        existing_engine_severity: str | None = None,
    ) -> SessionResult:
        """
        Master aggregation method.

        Parameters
        ----------
        classification          : SessionClassification from LLMClassifier
        consultant_profile      : ConsultantProfile from ConsultantAnalyser
        human_label             : category label from the GT dataset
                                  ("Explicit", "Borderline", "Moderate",
                                   "False Positives")
        existing_engine_severity: severity string the existing AstroTalk engine
                                  assigned (for mismatch detection)
        """
        chunk_results = classification.chunk_results

        # ── CSAM_RISK override — immediately SEVERE ──────────────────────
        # Must run before any other aggregation logic.
        for cr in chunk_results:
            for im in cr.intents_triggered:
                if im.intent_id == 'CSAM_RISK':
                    return SessionResult(
                        session_id=                  classification.session_id,
                        final_severity=              'Red',
                        intents_triggered=[
                            IntentSummary(
                                intent_id=         'CSAM_RISK',
                                intent_name=       im.intent_name,
                                severity=          'Red',
                                occurrence_count=  1,
                                max_confidence=    'High',
                                speakers_involved= [im.speaker] if im.speaker else [],
                                sample_trigger=    im.trigger_message,
                            )
                        ],
                        consultant_response_pattern= consultant_profile.response_pattern,
                        severity_modifier_applied=   False,
                        original_severity=           'Red',
                        primary_language=            classification.primary_language,
                        total_messages=              0,
                        total_chunks=                classification.total_chunks,
                        successful_chunks=           classification.successful_chunks,
                        flagged_messages=[{
                            'intent_id':           im.intent_id,
                            'trigger_message':     im.trigger_message,
                            'speaker':             im.speaker,
                            'english_translation': im.english_translation,
                            'chunk_index':         cr.chunk_index,
                            'confidence':          'High',
                        }],
                        confidence_level=            'High',
                        summary=(
                            'CSAM_RISK detected — verdict overridden to SEVERE '
                            'regardless of other session content.'
                        ),
                        recommended_action=          'Immediate Review',
                        mismatch_flag=               False,
                    )

        # ── STEP 1 — Collect intents across all chunks ──────────────────
        intent_map: dict[str, IntentSummary] = {}

        for cr in chunk_results:
            for im in cr.intents_triggered:
                iid = im.intent_id
                if iid not in intent_map:
                    intent_map[iid] = IntentSummary(
                        intent_id=        iid,
                        intent_name=      im.intent_name,
                        severity=         im.severity,
                        occurrence_count= 1,
                        max_confidence=   im.confidence,
                        speakers_involved= [im.speaker] if im.speaker else [],
                        sample_trigger=   im.trigger_message,
                    )
                else:
                    s = intent_map[iid]
                    s.occurrence_count += 1
                    s.max_confidence    = _higher_confidence(
                        s.max_confidence, im.confidence
                    )
                    if im.speaker and im.speaker not in s.speakers_involved:
                        s.speakers_involved.append(im.speaker)

        intents_list = list(intent_map.values())

        # ── STEP 2 — Raw severity ────────────────────────────────────────
        severities = [cr.chunk_severity for cr in chunk_results if cr.parse_success]
        total      = len(severities) or 1

        if "Red" in severities:
            raw_severity = "Red"
        elif severities.count("Amber") / total > 0.30:
            raw_severity = "Amber"
        elif "Amber" in severities:
            raw_severity = "Amber"
        else:
            raw_severity = "Green"

        # ── STEP 3 — Apply consultant modifier ──────────────────────────
        original_severity = raw_severity
        modifier_applied  = False
        modifier          = consultant_profile.severity_modifier

        if modifier == "ESCALATE":
            if raw_severity == "Amber":
                final_severity   = "Red"
                modifier_applied = True
            else:
                final_severity = raw_severity

        elif modifier == "REDUCE":
            if raw_severity == "Red":
                final_severity   = "Amber"
                modifier_applied = True
            elif raw_severity == "Amber":
                final_severity   = "Green"
                modifier_applied = True
            else:
                final_severity = raw_severity

        else:  # MAINTAIN
            final_severity = raw_severity

        # ── STEP 4 — Confidence level ────────────────────────────────────
        high_count = sum(
            1 for cr in chunk_results
            for im in cr.intents_triggered
            if im.confidence == "High"
        )
        if high_count >= 2:
            confidence_level = "High"
        elif high_count == 1:
            confidence_level = "Medium"
        else:
            confidence_level = "Low"

        # ── STEP 5 — Recommended action ──────────────────────────────────
        recommended_action = self._recommend_action(
            final_severity, confidence_level, modifier
        )

        # ── STEP 6 — Summary ─────────────────────────────────────────────
        summary = self._build_summary(
            final_severity,
            original_severity,
            modifier_applied,
            intents_list,
            classification.total_chunks,
            consultant_profile,
            recommended_action,
        )

        # ── STEP 7 — Mismatch flag ───────────────────────────────────────
        mismatch_flag = False
        if existing_engine_severity is not None:
            mapped = self.map_label_to_severity(human_label or "")
            if mapped != "Unknown" and final_severity != existing_engine_severity:
                mismatch_flag = True
        if human_label is not None and existing_engine_severity is None:
            mapped = self.map_label_to_severity(human_label)
            # Only flag mismatch when we have a known mapping
            if mapped != "Unknown" and final_severity != mapped:
                mismatch_flag = True

        # ── STEP 8 — Flagged messages ────────────────────────────────────
        flagged_messages = self._collect_flagged_messages(chunk_results)

        return SessionResult(
            session_id=                  classification.session_id,
            final_severity=              final_severity,
            intents_triggered=           intents_list,
            consultant_response_pattern= consultant_profile.response_pattern,
            severity_modifier_applied=   modifier_applied,
            original_severity=           original_severity,
            primary_language=            classification.primary_language,
            total_messages=              sum(
                                             cr.total_chunks  # proxy — real count
                                             for cr in chunk_results[:1]
                                         ) if chunk_results else 0,
            total_chunks=                classification.total_chunks,
            successful_chunks=           classification.successful_chunks,
            flagged_messages=            flagged_messages,
            confidence_level=            confidence_level,
            summary=                     summary,
            recommended_action=          recommended_action,
            mismatch_flag=               mismatch_flag,
        )

    def map_label_to_severity(self, label: str) -> str:
        """Map human category labels to severity strings."""
        return {
            "Explicit":       "Red",
            "Borderline":     "Red",
            "Moderate":       "Amber",
            "False Positives": "Green",
        }.get(label, "Unknown")

    def to_dict(self, result: SessionResult) -> dict:
        """Convert SessionResult to a JSON-serialisable dict."""
        return {
            "session_id":                  result.session_id,
            "final_severity":              result.final_severity,
            "original_severity":           result.original_severity,
            "severity_modifier_applied":   result.severity_modifier_applied,
            "consultant_response_pattern": result.consultant_response_pattern,
            "primary_language":            result.primary_language,
            "total_chunks":                result.total_chunks,
            "successful_chunks":           result.successful_chunks,
            "confidence_level":            result.confidence_level,
            "recommended_action":          result.recommended_action,
            "mismatch_flag":               result.mismatch_flag,
            "summary":                     result.summary,
            "intents_triggered": [
                {
                    "intent_id":         s.intent_id,
                    "intent_name":       s.intent_name,
                    "severity":          s.severity,
                    "occurrence_count":  s.occurrence_count,
                    "max_confidence":    s.max_confidence,
                    "speakers_involved": s.speakers_involved,
                    "sample_trigger":    s.sample_trigger,
                }
                for s in result.intents_triggered
            ],
            "flagged_messages": result.flagged_messages,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _recommend_action(
        self, severity: str, confidence: str, modifier: str
    ) -> str:
        if severity == "Red":
            if confidence == "High":
                return "Immediate Review"
            return "Scheduled Review"
        if severity == "Amber":
            if modifier == "ESCALATE" or confidence == "High":
                return "Scheduled Review"
            return "Monitor"
        return "No Action"

    def _build_summary(
        self,
        final_severity:   str,
        original_severity: str,
        modifier_applied:  bool,
        intents_list:      list[IntentSummary],
        total_chunks:      int,
        profile:           Any,
        recommended_action: str,
    ) -> str:
        if final_severity == "Green":
            base = (
                f"No policy violations detected. "
                f"Session appears compliant across all "
                f"{total_chunks} chunk(s) analysed."
            )
        elif final_severity == "Amber":
            names = ", ".join(s.intent_name for s in intents_list) or "unknown"
            base = (
                f"Session flagged at Amber severity. "
                f"{len(intents_list)} intent(s) triggered: {names}. "
                f"Consultant response pattern: "
                f"{profile.response_pattern}."
            )
        else:  # Red
            top = intents_list[0].intent_name if intents_list else "unknown violation"
            pattern_desc = {
                "ENGAGED":                 "actively engaged with inappropriate content",
                "CONTINUED_WITHOUT_ENDING": "continued without ending the session",
                "DENIED_CONTINUED":        "denied but continued the session",
                "DEFLECTED":               "attempted to deflect",
                "DENIED_AND_ENDED":        "denied and ended the session",
            }.get(profile.response_pattern, profile.response_pattern)
            base = (
                f"Session flagged at Red severity requiring "
                f"{recommended_action}. "
                f"{len(intents_list)} intent(s) triggered including "
                f"{top}. "
                f"Consultant {pattern_desc}."
            )

        if modifier_applied:
            direction = "escalated" if final_severity > original_severity else "reduced"
            # String comparison works: Red > Green alphabetically is wrong,
            # use explicit check instead
            direction = (
                "escalated" if final_severity == "Red" and original_severity != "Red"
                else "reduced"
            )
            base += (
                f" Severity {direction} based on consultant behaviour "
                f"(score: {profile.engagement_score}/10)."
            )

        return base

    def _collect_flagged_messages(
        self, chunk_results: list[Any]
    ) -> list[dict]:
        """
        Extract High/Medium confidence trigger messages from all chunks.
        Deduplicated by trigger text; High confidence first; max 10.
        """
        seen:  set[str]   = set()
        high:  list[dict] = []
        medium: list[dict] = []

        for cr in chunk_results:
            for im in cr.intents_triggered:
                if im.confidence not in ("High", "Medium"):
                    continue
                key = im.trigger_message.strip()
                if key in seen:
                    continue
                seen.add(key)
                entry = {
                    "intent_id":          im.intent_id,
                    "trigger_message":    im.trigger_message,
                    "speaker":            im.speaker,
                    "english_translation": im.english_translation,
                    "chunk_index":        cr.chunk_index,
                    "confidence":         im.confidence,
                }
                if im.confidence == "High":
                    high.append(entry)
                else:
                    medium.append(entry)

        return (high + medium)[:10]


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import importlib.util
    import os

    sys.stdout.reconfigure(encoding="utf-8")

    def _load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        mod  = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    base = Path(__file__).resolve().parents[1]

    dl_mod  = _load("engine.data_loader",       str(base / "engine/data_loader.py"))
    ca_mod  = _load("engine.consultant_analyser", str(base / "engine/consultant_analyser.py"))

    # LLMClassifier requires ANTHROPIC_API_KEY if USE_CLAUDE_API=True
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: Set ANTHROPIC_API_KEY environment variable before running tests.")
        sys.exit(1)

    from engine.classifier import LLMClassifier
    from engine.consultant_analyser import ConsultantAnalyser

    loader   = dl_mod.DataLoader()
    sessions = loader.load()
    index    = {s["order_id"]: s for s in sessions}

    clf        = LLMClassifier()
    analyser   = ConsultantAnalyser()
    aggregator = SessionAggregator()

    def _run_test(oid, human_label, existing_sev, label):
        print("\n" + "=" * 64)
        print(f"  {label}  —  Session {oid}")
        print("=" * 64)
        session  = index[oid]
        profile  = analyser.analyse(session)
        classif  = clf.classify_session(session)
        result   = aggregator.aggregate(
            classif, profile,
            human_label=human_label,
            existing_engine_severity=existing_sev,
        )
        print(f"  final_severity           : {result.final_severity}")
        print(f"  original_severity        : {result.original_severity}")
        print(f"  severity_modifier_applied: {result.severity_modifier_applied}")
        print(f"  confidence_level         : {result.confidence_level}")
        print(f"  recommended_action       : {result.recommended_action}")
        print(f"  mismatch_flag            : {result.mismatch_flag}")
        print(f"  summary                  : {result.summary}")
        print(f"  intents ({len(result.intents_triggered)}):")
        for s in result.intents_triggered:
            print(f"    [{s.intent_id}] x{s.occurrence_count} "
                  f"({s.max_confidence}) — {s.intent_name}")
        return result

    # ── Test 1 ─────────────────────────────────────────────────────────
    r1 = _run_test(
        294055364,
        human_label="Moderate",
        existing_sev="Amber",
        label="TEST 1 — Moderate / Action Taken",
    )
    t1_pass = (
        r1.final_severity in ("Red", "Amber")
        and r1.recommended_action != "No Action"
    )
    print(f"\n  [{'PASS' if t1_pass else 'FAIL'}] severity in (Red, Amber) "
          f"and action != 'No Action'")

    # ── Test 2 ─────────────────────────────────────────────────────────
    r2 = _run_test(
        296029912,
        human_label="False Positives",
        existing_sev="Red",
        label="TEST 2 — False Positive",
    )
    t2_pass = (
        r2.final_severity == "Green"
        and r2.recommended_action == "No Action"
        and r2.mismatch_flag is True
    )
    print(f"\n  [{'PASS' if t2_pass else 'FAIL'}] Green, No Action, mismatch_flag=True")

    # ── Summary ────────────────────────────────────────────────────────
    print("\n" + "=" * 64)
    all_pass = t1_pass and t2_pass
    print(f"  Overall: {'ALL PASS' if all_pass else 'SOME TESTS FAILED'}")
    print("=" * 64)
