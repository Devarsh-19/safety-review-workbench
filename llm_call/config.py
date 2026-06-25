"""
config.py
=========
Shared configuration for the Gemini content-moderation runner.

Single hardcoded model (Gemini 3 Flash). Imported by the API caller,
the caching helpers, and the runner.
"""

MODEL_ID = "gemini-3-flash-preview"   # API model ID
API_KEY_ENV = "GOOGLE_API_KEY"        # env var holding the API key
MAX_OUTPUT_TOKENS = 16384             # generation cap
MAX_RETRIES = 2                       # API call retries per session
