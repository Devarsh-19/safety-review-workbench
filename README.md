# AstroTalk Safety Review Workbench

This project loads AstroTalk chat exports, reconstructs sessions, detects content-safety issues, writes results to SQLite, and exposes a review UI/API for human auditors.

The main production-style flow is:

```text
CSV files
  -> engine.data_loader.DataLoader
  -> pipeline.batch_runner.process_session
  -> engine.consultant_analyser.ConsultantAnalyser
  -> engine.classifier.LLMClassifier
       -> engine.language_detector.LanguageDetector
       -> engine.chunker.SessionChunker
       -> engine.intent_library.IntentLibrary
       -> Gemini / Claude / Ollama
  -> engine.aggregator.SessionAggregator
  -> store.writer.write_session_complete
  -> SQLite tables: sessions, turns, flags
  -> review_interface/api/main.py
  -> React review dashboard
```

## Repository Layout

```text
safety-review-workbench/
  config.py                         Runtime paths, LLM provider, retry settings
  main.py                           Legacy/pilot JSON-output runner
  pipeline/
    batch_runner.py                 Primary batch and ingestion pipeline
    checkpoint.py                   Resume checkpoint storage
    logger.py                       File and console logging
  engine/
    data_loader.py                  CSV -> normalized session dicts
    language_detector.py            Message/session language detection
    chunker.py                      Session cleanup and LLM chunk creation
    intent_library.py               12 AstroTalk NSFW intent definitions
    consultant_analyser.py          Rule-based consultant behaviour analysis
    classifier.py                   LLM prompt creation, model calls, JSON parsing
    aggregator.py                   Chunk results -> session verdict
    verdict_rules.py                Shared flag-combination verdict logic
  store/
    schema.sql                      SQLite schema
    db.py                           SQLite connection and query helpers
    writer.py                       Writes sessions, turns, flags, review actions
  review_interface/api/main.py      FastAPI backend for review workflow
  review_interface/frontend/        React frontend
  export/                           Export placeholders
  validation/                       Validation placeholders
```

## Primary Data Flow

1. `DataLoader(data_path).load_sessions()` reads one CSV file or every `.csv` file in a folder.
2. Rows are grouped by `session_id`. Automated rows are removed, duplicate message rows are dropped, timestamps are normalized, and language code/month fields are mapped to readable values.
3. `pipeline.batch_runner.process_session()` adapts each DataLoader session into the older engine message shape expected by `ConsultantAnalyser`, `SessionChunker`, and `LLMClassifier`.
4. `ConsultantAnalyser.analyse()` scans consultant messages for rule-based risk signals such as vulgar language, personal information sharing, erotic reading language, reciprocated flirting, deflection, or continuing after a user violation.
5. `LLMClassifier.classify_session()` detects session language, builds a consultant summary, chunks the session, injects the intent taxonomy, calls the configured LLM, and parses JSON into chunk-level results.
6. `SessionAggregator.aggregate()` combines chunk-level LLM matches and consultant behaviour into one engine severity: `Red`, `Amber`, or `Green`.
7. `engine.verdict_rules.get_db_verdict_for_flags()` converts active LLM/regex/manual flag codes into the stored DB verdict: `SEVERE`, `FLAGGED`, or `CLEAN`.
8. `write_session_complete()` writes one DB transaction containing the session row, turn rows, and flag rows.
9. The FastAPI app reads from SQLite and lets reviewers filter sessions, inspect turns/flags, confirm or override verdicts, add manual flags, dismiss/amend flags, lock sessions, and export reviewed rows.

## Input Data Format

`engine.data_loader.DataLoader` expects AstroTalk CSV data with one row per message.

Required columns:

| Column | Input type | Meaning |
| --- | --- | --- |
| `session_id` | string/integer | Unique chat session ID |
| `message_seq` | string/integer | Message order inside the session |
| `sender` | `USER`, `ASTROLOGER`, or `CONSULTANT` | Message author |
| `message_text` | string | Raw chat message |
| `is_automated_message` | `0` or `1` | Platform/system message marker |
| `sent_at_ist` | datetime string | Message timestamp |
| `has_link` | `0` or `1` | Whether message contains link/media |
| `month` | `1`-`12` | Month code |
| `flagged` | `yes` or `no` | Existing AstroTalk flag marker |
| `language` | comma-separated numeric codes | Example: `1`, `2`, `1,2,5` |

Optional column:

| Column | Output usage |
| --- | --- |
| `astrologer_id` | Stored in session metadata when present |

## Normalized Session Format

`DataLoader.load_sessions()` returns:

```python
list[{
    "session_id": str,
    "astrologer_id": str | None,
    "user_id": None,
    "session_date": str | None,          # YYYY-MM-DD
    "month": str | None,                 # e.g. "March"
    "language_code": str | None,         # e.g. "1,2"
    "language_detected": str | None,     # primary language name
    "session_type": "chat",
    "astrotalk_flagged": int,            # 0 or 1
    "astrotalk_flag_category": None,
    "astrotalk_severity": None,
    "session_start": str | None,         # ISO timestamp
    "session_end": str | None,           # ISO timestamp
    "duration_minutes": float | None,
    "messages": list[{
        "turn_id": int,
        "speaker": "USER" | "ASTROLOGER" | "UNKNOWN",
        "message_text": str,
        "is_automated": int,
        "timestamp": str | None,
        "language_detected": str | None,
        "has_link": int,
    }],
}]
```

The engine classifiers still expect a legacy in-memory shape:

```python
{
    "order_id": int,
    "consultant_name": str,
    "user_name": str,
    "category": str | None,
    "messages": [{
        "role": "USER" | "CONSULTANT",
        "message": str,
        "timestamp": str | None,
        "message_id": int,
    }],
}
```

`pipeline.batch_runner._to_legacy_session()` performs this adapter step.

## Core Function Reference

### `engine.data_loader.DataLoader`

`DataLoader(path: str | Path)`

Creates a loader for either one CSV file or a directory of CSV files.

`load_sessions() -> list[dict]`

Loads CSV rows, filters automated messages, deduplicates repeated rows, derives session metadata, builds message lists, prints a summary, and returns normalized session dicts.

Important private helpers:

| Function | Input | Output |
| --- | --- | --- |
| `_load_dataframe()` | loader path | `pandas.DataFrame` containing all CSV rows |
| `_build_sessions(df)` | flat message DataFrame | normalized session list |
| `_dedup_messages(group)` | one session DataFrame | `(deduped_df, removed_count)` |
| `_normalise_speaker(val)` | raw sender | `USER`, `ASTROLOGER`, or `UNKNOWN` |
| `_normalise_automated(val)` | raw auto flag | `0` or `1` |
| `_parse_language(val)` | code string such as `1,2` | `(all_codes, primary_language)` |
| `_parse_timestamp(val)` | raw datetime | ISO timestamp string or `None` |
| `_calc_duration(start, end)` | ISO timestamps | minutes as `float` or `None` |

### `engine.language_detector.LanguageDetector`

`detect(text: str) -> LanguageResult`

Detects one message's language. It first detects script, then uses Hinglish marker ratio for Latin-script Hindi/Hinglish, and falls back to `langdetect`.

```python
LanguageResult(
    primary_language="Hinglish",
    is_hinglish=True,
    hinglish_confidence=0.18,
    detected_by_langdetect="tl",
    script="Latin",
)
```

`analyse_session(messages: list[dict]) -> SessionLanguage`

Samples up to 30 messages and returns a session-level language profile.

```python
SessionLanguage(
    dominant_language="Hinglish",
    languages_detected={"Hinglish": 18, "English": 6},
    has_hinglish=True,
    has_devanagari=False,
    has_regional=False,
    sample_size=24,
)
```

`get_language_instruction(session_language) -> str`

Builds prompt text telling the LLM how to interpret the session language.

`detect_from_code(language_code) -> str | None`

Maps AstroTalk numeric language codes to names.

### `engine.chunker.SessionChunker`

`clean_messages(messages: list[dict]) -> list[ChunkMessage]`

Removes automated/system messages and birth-detail headers. Input is the legacy message format with `role`, `message`, `timestamp`, and `message_id`.

```python
ChunkMessage(
    role="USER",
    message="message text",
    timestamp="2026-03-19T04:02:25+00:00",
    message_id=12,
)
```

`chunk_messages(cleaned_messages, chunk_size=15, overlap=3) -> list[Chunk]`

Creates overlapping sliding windows. Final chunks with fewer than 3 messages are dropped.

```python
Chunk(
    chunk_index=1,
    total_chunks=4,
    messages=[...],
    formatted_text="[CHUNK 1 of 4 - Messages 1-15]\nUSER: ...",
    message_count=15,
    has_user_messages=True,
    has_consultant_messages=True,
)
```

`get_session_context(cleaned_messages) -> str`

Builds a short non-LLM context summary based on first messages and topic keywords.

`process_session(session: dict) -> SessionChunkResult`

Runs clean, dynamic chunk-size selection, chunking, and context creation.

```python
SessionChunkResult(
    session_id=123,
    total_messages_raw=80,
    total_messages_cleaned=72,
    automated_removed=8,
    chunks=[...],
    session_context="Session appears to be about ...",
    user_message_count=36,
    consultant_message_count=36,
    chunk_size_used=15,
)
```

### `engine.intent_library.IntentLibrary`

Stores the 12 NSFW intent categories used in prompts. Each `Intent` has:

```python
Intent(
    intent_id="INT-01",
    name="Explicit Sexual Description Directed at Consultant",
    description="...",
    severity="Red",
    examples=[...],
    counter_examples=[...],
    annexure_category="Category A",
    detection_notes="...",
)
```

Public functions:

| Function | Input | Output |
| --- | --- | --- |
| `format_for_prompt(third_party_names=None)` | optional list of known third-party names | full plain-text taxonomy block for LLM prompt |
| `get_intent(intent_id)` | `INT-01` to `INT-12` | matching `Intent`, or raises `KeyError` |
| `get_red_intents()` | none | list of Red intents |
| `get_amber_intents()` | none | list of Amber intents |
| `get_intent_ids()` | none | ordered list of all IDs |

### `engine.consultant_analyser.ConsultantAnalyser`

`analyse(session: dict) -> ConsultantProfile`

Runs rule-based analysis over consultant messages. Input is legacy session format.

```python
ConsultantProfile(
    session_id=123,
    response_pattern="ENGAGED",
    engagement_score=5.0,
    red_flags=[...],
    severity_modifier="ESCALATE",
    modifier_reason="...",
    consultant_message_count=40,
    flagged_message_count=2,
    engagement_ratio=0.05,
)
```

Rule flag output:

```python
ConsultantRedFlag(
    message_id=20,
    timestamp="...",
    message="...",
    flag_type="VULGAR_LANGUAGE",
    severity="High",
)
```

Other public functions:

| Function | Input | Output |
| --- | --- | --- |
| `format_for_prompt(profile)` | `ConsultantProfile` | compact prompt summary |
| `get_summary_stats(profiles)` | list of profiles | aggregate distribution and score stats |
| `detect_post_session_messages(turns)` | DataLoader-format turns | DB-style flags for astrologer messages after session end |

### `engine.classifier.LLMClassifier`

`LLMClassifier()`

Initializes `IntentLibrary`, `LanguageDetector`, `SessionChunker`, `ConsultantAnalyser`, logging, and the configured model backend. The backend is selected by `LLM_PROVIDER` in `config.py` or environment:

```text
gemini -> GOOGLE_API_KEY
claude -> ANTHROPIC_API_KEY
ollama -> local Ollama server
```

`classify_session(session: dict) -> SessionClassification`

Runs the full classification path for one legacy session:

```text
LanguageDetector.analyse_session
ConsultantAnalyser.analyse
SessionChunker.process_session
classify_chunk for each chunk
```

Output:

```python
SessionClassification(
    session_id=123,
    chunk_results=[...],
    consultant_profile_summary="CONSULTANT BEHAVIOUR ANALYSIS: ...",
    primary_language="Hinglish",
    total_chunks=5,
    successful_chunks=5,
    failed_chunks=0,
)
```

`classify_chunk(chunk, language_instruction, session_context, consultant_summary, third_party_names=None) -> ChunkResult`

Builds the LLM prompt, calls the configured model, parses JSON, and returns:

```python
ChunkResult(
    chunk_index=1,
    total_chunks=5,
    intents_triggered=[
        IntentMatch(
            intent_id="INT-06",
            intent_name="Off-Platform Solicitation",
            confidence="High",
            severity="Red",
            trigger_message="WhatsApp pe aao",
            speaker="USER",
            reason="Requests off-platform contact",
            english_translation="Come on WhatsApp",
        )
    ],
    chunk_severity="Red",
    notes="...",
    raw_response="{...}",
    parse_success=True,
)
```

Important private helpers:

| Function | Input | Output |
| --- | --- | --- |
| `_call_model(prompt)` | prompt string | raw model response text |
| `_call_gemini_api(prompt)` | prompt string | Gemini response text |
| `_call_claude_api(prompt)` | prompt string | Claude response text |
| `_call_ollama(prompt)` | prompt string | Ollama response text |
| `_parse_llm_response(raw)` | raw model response | parsed JSON dict |
| `_build_intent_matches(raw_intents)` | list of JSON intent dicts | list of `IntentMatch` |
| `_get_compact_intent_library(third_party_names)` | optional names | compact taxonomy for CPU/Ollama mode |

### `engine.aggregator.SessionAggregator`

`aggregate(classification, consultant_profile, human_label=None, existing_engine_severity=None) -> SessionResult`

Combines all chunk results and consultant behaviour into a session verdict.

Severity logic:

| Condition | Raw severity |
| --- | --- |
| Any parsed chunk is `Red` | `Red` |
| More than 30% parsed chunks are `Amber` | `Amber` |
| Any parsed chunk is `Amber` | `Amber` |
| Otherwise | `Green` |

Consultant modifier:

| Modifier | Effect |
| --- | --- |
| `ESCALATE` | `Amber -> Red` |
| `REDUCE` | `Red -> Amber`, `Amber -> Green` |
| `MAINTAIN` | No change |

Output:

```python
SessionResult(
    session_id=123,
    final_severity="Red",
    intents_triggered=[...],
    consultant_response_pattern="ENGAGED",
    severity_modifier_applied=True,
    original_severity="Amber",
    primary_language="Hinglish",
    total_messages=5,
    total_chunks=5,
    successful_chunks=5,
    flagged_messages=[...],
    confidence_level="High",
    summary="...",
    recommended_action="Immediate Review",
    mismatch_flag=False,
)
```

Other public functions:

| Function | Input | Output |
| --- | --- | --- |
| `map_label_to_severity(label)` | `Explicit`, `Borderline`, `Moderate`, `False Positives` | `Red`, `Amber`, `Green`, or `Unknown` |
| `to_dict(result)` | `SessionResult` | JSON-serializable dict |

## Batch Pipeline Functions

### `pipeline.batch_runner`

`run_batch(data_path: str, fresh_run=False, limit=None) -> None`

Primary AI processing command. Loads data, skips checkpointed sessions, processes each session, writes SQLite rows, and logs summary counts.

`process_session(session, logger, _analyser=None, _clf=None, _aggregator=None) -> dict | None`

Runs one normalized DataLoader session through adapter, consultant analysis, LLM classification, aggregation, and DB-shape builders.

Output:

```python
{
    "session_id": "123",
    "session_data": {...},   # one row for sessions
    "turns": [...],          # rows for turns
    "flags": [...],          # rows for flags
}
```

`ingest_only(data_path: str) -> None`

Loads sessions into SQLite with `overall_verdict='UNPROCESSED'`, without AI classification. It still adds automatic regex flags for post-session re-engagement and external link/media messages.

Internal mappers:

| Function | Purpose |
| --- | --- |
| `_to_legacy_session(session)` | Converts DataLoader shape to engine legacy shape |
| `_build_session_data(session, result, classification)` | Builds DB `sessions` row |
| `_build_turns(messages)` | Builds DB `turns` rows |
| `_build_flags(classification, profile)` | Builds DB `flags` rows from LLM and regex detections |

### `pipeline.checkpoint`

Stores processed session IDs in `logs/checkpoint.json`.

| Function | Output |
| --- | --- |
| `load_checkpoint()` | `set` of processed IDs |
| `save_checkpoint(processed_ids)` | writes checkpoint JSON |
| `clear_checkpoint()` | deletes checkpoint file |
| `checkpoint_exists()` | `True` when checkpoint has IDs |

## Shared Flag Verdict Logic

`engine.verdict_rules.py` contains the final flag-combination rules used for both automated LLM/regex output and manual reviewer flags.

Canonical severe flags:

```python
abusive_language
financial_solicitation
hate_speech
identity_fraud
nsfw
fake_remedies
unauthorized_medical_advice
```

Canonical flagged flags:

```python
off_platform_solicitation
personal_data_collection
fear_manipulation
competitor_promotion
other
```

Severe combinations:

```python
off_platform_solicitation + personal_data_collection
off_platform_solicitation + fear_manipulation
personal_data_collection + fear_manipulation
```

Main functions:

| Function | Input | Output |
| --- | --- | --- |
| `normalize_flag(flag)` | raw flag text | lowercase snake_case |
| `to_canonical_flag(flag)` | engine/manual code | canonical policy flag |
| `get_final_verdict(flags)` | list of flag codes/names | `severe`, `flagged`, or `clean` |
| `get_db_verdict_for_flags(flags)` | list of flag codes/names | `SEVERE`, `FLAGGED`, or `CLEAN` |
| `get_active_flag_codes(flag_rows)` | DB flag rows | active category codes, with `DISMISSED` rows suppressing matching originals |

Current engine mappings include examples such as `INT-06 -> off_platform_solicitation`, `INT-07 -> abusive_language`, `VULGAR_LANGUAGE -> abusive_language`, and `PERSONAL_INFO_SHARED -> personal_data_collection`. Manual flags should use canonical names where possible, but known engine codes are also accepted.

## SQLite Output Format

`store/schema.sql` defines four tables.

### `sessions`

One row per session. Important columns:

| Column | Values |
| --- | --- |
| `session_id` | source session ID |
| `overall_verdict` | `UNPROCESSED`, `CLEAN`, `FLAGGED`, `SEVERE` |
| `confidence_score` | numeric score, usually `0.3`, `0.6`, or `0.9` |
| `review_status` | `PENDING`, `REVIEWED`, `CONFIRMED`, `OVERRIDDEN`, `NEEDS_FINAL_REVIEW`, `LOCKED` |
| `language_detected` | session language |
| `session_note`, `reviewer_note` | human review notes |

### `turns`

One row per message.

```python
{
    "session_id": str,
    "turn_id": int,
    "speaker": "USER" | "ASTROLOGER" | "UNKNOWN",
    "message_text": str,
    "is_automated": int,
    "timestamp": str | None,
    "language_detected": str | None,
    "has_link": int,
}
```

### `flags`

One row per engine/manual flag.

```python
{
    "session_id": str,
    "turn_id": int | None,
    "category_code": str,          # e.g. INT-06, VULGAR_LANGUAGE
    "detection_layer": "LLM" | "REGEX" | "MANUAL" | "AMENDED" | "DISMISSED",
    "severity": "LOW" | "MEDIUM" | "HIGH",
    "confidence_score": float,
    "reasoning": str,
    "false_positive_risk": "LOW" | "MEDIUM" | "HIGH",
    "pattern_matched": str | None,
}
```

### `review_log`

Audit log for reviewer actions such as `CONFIRM`, `FALSE_POSITIVE`, `CLEAR`, `MANUAL_FLAG`, and `FLAG_DISMISSED`.

## Review API

Run the API:

```bash
cd review_interface/api
uvicorn main:app --reload --port 8000
```

Key endpoints:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | API and DB health |
| `GET` | `/stats` | total, pending, reviewed, verdict counts |
| `GET` | `/stats/reviewer` | per-reviewer activity |
| `GET` | `/stats/violations` | flag counts by category |
| `GET` | `/sessions` | list sessions with optional `verdict`, `status`, `language` filters |
| `GET` | `/sessions/pending` | pending review queue |
| `GET` | `/sessions/{session_id}` | session detail with turns and flags |
| `GET` | `/sessions/{session_id}/flags` | flags for one session |
| `POST` | `/sessions/{session_id}/review` | submit reviewer verdict action |
| `POST` | `/sessions/{session_id}/manual-flag` | add manual flag |
| `POST` | `/sessions/{session_id}/lock` | lock session for reviewer |
| `POST` | `/sessions/{session_id}/unlock` | unlock session |
| `POST` | `/sessions/{session_id}/session-note` | save session-level note |
| `POST` | `/flags/{flag_id}/amend` | add amended flag |
| `POST` | `/flags/{flag_id}/dismiss` | add dismissed marker |
| `GET` | `/export/csv` | export reviewed sessions |

## Running the Project

Install Python dependencies:

```bash
pip install -r requirements.txt
```

Set environment variables:

```bash
# PowerShell examples
$env:LLM_PROVIDER="gemini"
$env:GOOGLE_API_KEY="your-key"
$env:DB_PATH="store/results.db"
```

For Claude:

```bash
$env:LLM_PROVIDER="claude"
$env:ANTHROPIC_API_KEY="your-key"
```

For Ollama:

```bash
$env:LLM_PROVIDER="ollama"
ollama serve
ollama pull llama3.2:3b
```

Initialize or migrate the DB:

```bash
python -c "from store.db import initialise_db; initialise_db()"
```

Ingest only, without AI:

```bash
python -m pipeline.batch_runner --data data/raw --ingest-only
```

Run AI classification:

```bash
python -m pipeline.batch_runner --data data/raw --limit 10
```

Fresh run, clearing checkpoint after confirmation:

```bash
python -m pipeline.batch_runner --data data/raw --fresh
```

Start review API:

```bash
cd review_interface/api
uvicorn main:app --reload --port 8000
```

Start frontend:

```bash
cd review_interface/frontend
npm install
npm run dev
```

## Current Caveats

- `pipeline/batch_runner.py` is the primary path for CSV input and SQLite output.
- Root `main.py` appears to be a legacy/pilot runner. It imports `DataLoader()` with no path and calls `loader.load()` / `loader.save_processed()`, but the current `engine.data_loader.DataLoader` requires a path and exposes `load_sessions()`.
- Some self-test blocks inside engine modules also use the older loader interface. Treat them as historical test scaffolding unless updated.
- `engine.classifier._call_gemini_api()` currently hardcodes `"gemini-3.5-flash"` even though `config.py` also defines `GEMINI_MODEL`.
- `store.writer.write_turns()` and `write_session_complete()` do not currently persist `is_automated` or `has_link` even though the schema has those columns.
- `export/` and `validation/` modules are placeholders with docstrings only.
