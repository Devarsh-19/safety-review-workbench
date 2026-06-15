"""
Central configuration for the AstroTalk NSFW Detection Engine.
All tunable parameters, model names, thresholds, and paths live here.
"""

import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent

DATA_RAW_DIR       = BASE_DIR / "data" / "raw"
EXCEL_FILE         = DATA_RAW_DIR / "NSFW_Cases_Categorization.xlsx"
DATA_PROCESSED_DIR = BASE_DIR / "data" / "processed"
OUTPUTS_DIR        = BASE_DIR / "outputs"

# ---------------------------------------------------------------------------
# LLM backend selection
# ---------------------------------------------------------------------------
LLM_PROVIDER   = os.environ.get("LLM_PROVIDER", "gemini")  # "claude" | "gemini" | "ollama"
USE_CLAUDE_API = LLM_PROVIDER == "claude"    # backward-compat alias
CLAUDE_MODEL   = "claude-sonnet-4-20250514"  # Claude model ID
GEMINI_MODEL   = "gemini-2.0-flash"          # Gemini model ID
# ANTHROPIC_API_KEY / GOOGLE_API_KEY must be set as environment variables

# ---------------------------------------------------------------------------
# Ollama / LLM settings (fallback when USE_CLAUDE_API = False)
# ---------------------------------------------------------------------------
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_URL      = OLLAMA_BASE_URL + "/api/generate"
OLLAMA_MODEL    = "llama3.2:3b"
OLLAMA_TIMEOUT  = 600               # 10 minutes — CPU needs more time
CPU_MODE        = True              # Set False if GPU available

MAX_RETRIES  = 3    # attempts before giving up on a single chunk
RETRY_DELAY  = 2    # seconds between retries

# ---------------------------------------------------------------------------
# Classification thresholds
# ---------------------------------------------------------------------------
NSFW_CONFIDENCE_THRESHOLD = 0.70    # minimum score to flag a chunk as NSFW
SESSION_FLAG_RATIO        = 0.30    # fraction of flagged chunks to flag a session

# ---------------------------------------------------------------------------
# Chunking settings
# ---------------------------------------------------------------------------
CHUNK_SIZE        = 10              # number of messages per chunk
CHUNK_OVERLAP     = 2               # overlapping messages between chunks

# ---------------------------------------------------------------------------
# Language settings
# ---------------------------------------------------------------------------
SUPPORTED_LANGUAGES = ["en", "hi"]  # ISO 639-1 codes
HINGLISH_THRESHOLD  = 0.4           # mixed-script ratio to treat as Hinglish

# ---------------------------------------------------------------------------
# Report settings
# ---------------------------------------------------------------------------
REPORT_OUTPUT_FORMAT = "json"       # "json" | "excel" | "both"
