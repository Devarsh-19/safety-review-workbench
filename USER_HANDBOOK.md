# AstroTalk Safety Review Workbench — User Handbook

## 1. Getting Started

The Safety Review Workbench is a specialized tool for ingesting, reviewing, and exporting policy-violating sessions identified by LLM inference.

### Prerequisites
- Python 3.10+
- Node.js 18+
- SQLite3
- Gemini API Key (if running fresh inference)

### Setup
1. Clone the repository and setup your virtual environment:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and fill in necessary values. Ensure you have `DB_PATH` set (default `store/astrotalk.db`).

## 2. Running the Workbench

The workbench is divided into a backend API and a frontend dashboard. Both must be running simultaneously.

### Starting the Backend (FastAPI)
```bash
cd review_interface/api
uvicorn main:app --reload --port 8000
```
*The API runs at `http://localhost:8000`. You can view the interactive documentation at `http://localhost:8000/docs`.*

### Starting the Frontend (React)
Open a new terminal window:
```bash
cd review_interface/frontend
npm install
npm start
```
*The Dashboard runs at `http://localhost:3000`.*

## 3. Review Workflow

The workbench supports a two-tier review process designed for maximum throughput and accuracy.

1. **L1 Reviewer (Auditor)**
   - Logs into the Dashboard.
   - Views their assigned `PENDING` queue.
   - For Chat: Reads session turns. For Audio: Listens to the session playback and assigns Speaker identities.
   - Validates flagged segments/turns. Reviewers can:
     - **Add** manual flags if the LLM missed a violation.
     - **Amend** existing flags (changing severity or category).
     - **Dismiss** false-positive flags. *(Note: Dismissing a flag completely removes it from the active review set).*
   - Submits the session. The status becomes `SUBMITTED_FOR_REVIEW`.

2. **L2 Reviewer (Manager/QA)**
   - Logs into the Dashboard (must be listed in the `L2_REVIEWERS` env variable). *Note: Adding a new L2 reviewer also requires updating the React `LoginScreen.jsx` roster.*
   - Views the `SUBMITTED_FOR_REVIEW` queue.
   - Performs a final spot-check on the L1 reviewer's decisions.
   - Locks the session. The status becomes `LOCKED`.
   - *Locked sessions cannot be edited and are ready for final export.*

### Fast-Tracking CLEAN Sessions
Sessions that the LLM flagged as entirely `CLEAN` do not require manual L1 review.
- Run `python scripts/auto_process_clean_sessions.py` to automatically submit all clean sessions.
- Run `python scripts/auto_lock_clean_submitted.py` (as L2) to automatically lock them.

## 4. Running LLM Inference

### Chat Inference
If you have raw chat data and need to run it through the LLM for the first time, use the `llm_call` module.

```bash
cd llm_call
python batch.py --input path/to/raw_chat.csv
```
This runs the sessions concurrently against Gemini 3 Flash Preview. The output will be a JSON file.

To merge the raw LLM output back onto the original CSV structure for analysis:
```bash
python merge.py --input path/to/raw_chat.csv --results raw_chat_moderation.json
```

### Audio Inference
The primary script for audio inference is `test_gemini_multi/gemini_audio_batch.py` (which uses `gemini-3-flash-preview`). It manages FFmpeg concurrency (default 4) and API concurrency (default 16).

```bash
python test_gemini_multi/gemini_audio_batch.py
```
*(Use `--session-id` or `--limit 1` for testing individual sessions before running large batches).*

## 5. Data Ingestion

Once you have LLM-processed files, you must ingest them into the SQLite database to make them visible in the Workbench.

**Ingest Chat:**
```bash
python scripts/ingest_llm_sessions.py --input path/to/merged_results.csv
```

**Ingest Audio:**
```bash
python scripts/ingest_audio_results.py path/to/audio_results_folder/
```

*Note: Ingestion scripts contain duplicate-protection via a checkpoint file (`logs/checkpoint.json` for chat, `logs/audio_ingest_checkpoint.json` for audio). They will skip sessions already in the database. Use `--force` to re-ingest checkpointed sessions or `--reset-checkpoint` to start completely fresh.*

## 6. Generating Deliverable Logs

The platform requires formalized detection logs to be delivered upon completion of review batches.

**Chat Detection Logs:**
```bash
python scripts/generate_detection_logs.py
```
Outputs `exports/chat_detection_logs_YYYYMMDD.csv` (session summary) and `chat_detection_logs_detailed_YYYYMMDD.csv` (turn-by-turn).

**Audio Detection Logs:**
```bash
python scripts/generate_audio_detection_logs.py
```
Outputs similar session and detailed logs for Audio directly from the audio database.

## 7. Generating Reports

To check the health of your review queues, backlog size, and reviewer velocity, use the reporting script.

```bash
python scripts/session_report.py
```
This outputs a comprehensive console report detailing Verdict breakdowns, signal agreement matrices, False Positive rates, and reviewer workloads.

*Add `--docx report.docx` to save the output as a Word document for sharing.*
