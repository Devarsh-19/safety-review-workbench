# AstroTalk Safety Review Workbench — Technical Reference

## 1. System Architecture

The AstroTalk Safety Review Workbench comprises a data processing pipeline, a SQLite data store, and a web-based review interface (FastAPI + React). 

### Core Components
1. **Engine (`engine/`)**: Core logic for data parsing, intent resolution, and aggregation.
   - `data_loader.py`: Reconstructs chat sessions from flattened CSV formats.
   - `classifier.py`: Wraps LLM interactions (now deprecated in favor of `llm_call/batch.py` for scale).
   - `verdict_rules.py`: Contains flag normalization, escalation mapping, and conflict resolution logic.
2. **LLM Runner (`llm_call/`)**: High-throughput inference tools.
   - `batch.py`: Async concurrency runner that pushes sessions to Gemini 3 Flash Preview.
   - `moderate.py`: Single-session execution flow for testing or debugging.
   - `prompts.py`: Taxonomy definitions and prompt structure.
   - `merge.py`: Re-associates LLM classifications back onto the original input dataset for validation.
   - **Cost Estimation**: The batch runners estimate Gemini costs based on:
     - `non_cached_text_tokens  * 0.50 / 1M`
     - `non_cached_audio_tokens * 1.00 / 1M`
     - `cached_text_tokens      * 0.05 / 1M`
     - `cached_audio_tokens     * 0.10 / 1M`
     - `billable_output_tokens  * 3.00 / 1M`
3. **Data Store (`store/`)**: SQLite schemas and connection managers.
   - `astrotalk.db` (Chat): Controlled by `schema.sql`.
   - `audio_review.db` (Audio): Controlled by `audio_schema.sql`.
4. **Pipeline / Scripts (`scripts/`, `pipeline/`)**: Scripts for ingestion, state changes, exports, and analytics.
5. **Review Interface (`review_interface/`)**: 
   - **Backend**: FastAPI (`api/main.py`) running on `localhost:8000`.
   - **Frontend**: React SPA (`frontend/src/`) running on `localhost:3000`.

## 2. Database Schema

Both chat and audio databases share the fundamental concept of a hierarchical review entity:
`Sessions -> Turns (or Segments) -> Flags -> Audit Log`

### Chat DB (`astrotalk.db`)
- `sessions`: High-level session metadata, `overall_verdict` (CLEAN, FLAGGED, SEVERE), and `review_status`.
- `turns`: Sequential messages sent by either `USER` or `ASTROLOGER`.
- `flags`: Contains specific violations. Columns: `source` (LLM/REGEX/MANUAL), `status` (ACTIVE/CONFIRMED), `parent_flag_id` (used for amended flags pointing back to the original).
- `review_log`: Audit trail of all reviewer actions (locks, submissions, flag edits).

### Audio DB (`audio_review.db`)
- `audio_sessions`: Contains media URLs, overall verdicts. Reviewers must assign speaker identities (`speaker1_role`).
- `audio_segments`: Sequential timed segments of audio with raw diarization labels (`SPEAKER_1`, `SPEAKER_2`).
- `audio_flags`: Follows the identical source/status paradigm as chat.
- `audio_review_log`: Follows the identical audit paradigm as chat.

## 3. Review Interface API Reference

The backend operates entirely inside `main.py` using FastAPI. 
All endpoints are available natively under `http://localhost:8000/docs`.

### Key Endpoints:
- `GET /sessions`: Retrieves a paginated list of chat sessions. Can filter by status, verdict, language, or assignment.
- `GET /sessions/{session_id}`: Retrieves full payload (turns, flags, history) for the Chat Viewer.
- `POST /sessions/{session_id}/flags`: Add a manual flag to a chat session.
- `PUT /sessions/{session_id}/flags/{flag_id}`: Amend an existing flag (updates category/severity; creates audit trail).
- `DELETE /sessions/{session_id}/flags/{flag_id}`: Dismiss a flag (sets status to DISMISSED, recalculates verdict).
- `POST /sessions/{session_id}/submit`: L1 action. Moves session from `PENDING` to `SUBMITTED_FOR_REVIEW`.
- `POST /sessions/{session_id}/lock`: L2 action. Moves session to `LOCKED` (final state).
- `GET /audio/sessions`: Paginated list of audio sessions.
- `GET /audio/sessions/{s_id}`: Full payload for the Audio Viewer.
- `PUT /audio/sessions/{s_id}/roles`: Link raw diarization tags (`SPEAKER_1`) to roles (`ASTROLOGER`).
- (Analogous POST/PUT/DELETE endpoints exist for `/audio/sessions/.../flags` and `/audio/sessions/.../submit`).

## 4. Operational Scripts (`scripts/`)

The workbench uses Python scripts heavily for operational tasks outside of the web UI.

### Ingestion
- `ingest_llm_sessions.py`: Ingests flat CSVs containing pre-classified LLM flags into the chat database. Uses `engine.data_loader` to rebuild structures.
- `ingest_audio_results.py`: Ingests JSON files (diarized segments + LLM analysis) into the audio database.

### Review Workflow Operations
- `assign_sessions.py` / `assign_audio_sessions.py`: Distribute workload to reviewer buckets.
- `auto_process_clean_sessions.py`: Moves `CLEAN` sessions automatically to `SUBMITTED_FOR_REVIEW` using the `LLM` reviewer persona.
- `auto_lock_clean_submitted.py`: L2 script to batch lock all LLM-approved clean sessions.
- `dismiss_low_confidence_flags.py`: Cleans up borderline flags below arbitrary thresholds.

### Exports & Reporting
- `export_submitted_flags.py`: Extracts the active state of all flags for finalized delivery.
- `generate_detection_logs.py`: End-to-end report showing agreement between existing platforms flags and LLM flags for Chat.
- `generate_audio_detection_logs.py`: Equivalent end-to-end detection logs for Audio.
- `session_report.py`: Comprehensive CLI console printout of the entire queue health, verdict distribution, and reviewer workload.

## 5. Environment & Dependencies

Dependencies are managed via `requirements.txt` (backend) and `package.json` (frontend).

### Configuration (`.env`)
Required variables across backend/scripts:
- `GOOGLE_API_KEY`: For Gemini API calls (if using `llm_call`).
- `DB_PATH`: Path to the SQLite DB (default `store/astrotalk.db`).
- `L2_REVIEWERS`: Comma-separated list of reviewer IDs permitted to lock sessions (e.g. `Amogh,Devarsh`).
- `READONLY_AUDIO_REVIEWERS`: IDs permitted only to view, not edit, audio sessions.
