"""
config.py
=========
Shared configuration for the Gemini content-moderation runner.

Single hardcoded model (Gemini 3 Flash). Imported by the API caller,
the caching helpers, and the runner.
"""

# Hardcoded model + key.
# WARNING: do NOT commit a real key — this file is tracked in git.
MODEL_ID = "gemini-3-flash-preview"            # API model ID (hardcoded)
GOOGLE_API_KEY = "PASTE_YOUR_GOOGLE_API_KEY_HERE"  # hardcoded API key
MAX_OUTPUT_TOKENS = 16384                      # generation cap
MAX_RETRIES = 2                                # API call retries per session
