# AstroTalk Content Safety Detection Workbench

A content safety detection and review pipeline for the AstroTalk astrology platform. The system analyses consultant-user sessions (both text chat and audio calls) to detect NSFW, harmful, or policy-violating content, aggregates risk signals across multiple LLM classifiers, and exposes a human-review interface for auditors.

---

## Project Documentation

For comprehensive guides, please refer to the markdown documentation:
- [User Handbook](docs/USER_HANDBOOK.md) — complete guide on setting up, running, and using the workbench
- [Technical Reference](docs/TECHNICAL_REFERENCE.md) — architecture, database schema, API documentation, and scripts overview
- [Approach Notes](docs/APPROACH_NOTES.md) — methodology, intent taxonomy, and system assumptions

---

## Project Structure

```
safety-review-workbench/
├── engine/                   # Core detection logic & data structures
├── pipeline/                 # Batch orchestration
├── store/                    # Persistence layer (SQLite databases for chat & audio)
├── review_interface/
│   ├── api/                  # FastAPI backend for the review UI
│   └── frontend/src/         # React review dashboard
├── export/                   # Export utilities
├── scripts/                  # Operations, reporting, log generation, and database ingestion
├── llm_call/                 # High-throughput async Gemini LLM runners
├── docs/                     # Project documentation
├── main.py
├── config.py
└── requirements.txt
```

---

## Quick Start

### 1. Clone and create a virtual environment

```bash
git clone <repo-url>
cd safety-review-workbench

python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

```bash
cp .env.example .env
```
Ensure you set your `GOOGLE_API_KEY` for LLM inference (if applicable), and confirm the `DB_PATH`.

---

## Starting the Review Interface

The review interface consists of a FastAPI backend and a frontend dashboard.

### API backend

```bash
cd review_interface/api
uvicorn main:app --reload --port 8000
```
Available at `http://localhost:8000`. Interactive docs at `http://localhost:8000/docs`.

### Frontend dashboard

Open a separate terminal:
```bash
cd review_interface/frontend
npm install
npm run dev
```
Available at `http://localhost:3000`.

---

## Generating Deliverables

To generate the final detection logs required for formal handover:

**Chat Logs:**
```bash
python scripts/generate_detection_logs.py
```

**Audio Logs:**
```bash
python scripts/generate_audio_detection_logs.py
```
Output files will be generated in the `exports/` directory.

---

## Notes

- Raw data, processed outputs, and ground-truth files are **git-ignored** — never commit session data.
- The `llm_call/` module handles all heavy model interactions.
- All human review workflow is managed natively within the Workbench application.
