"""
parser.py
=========
Response parsing for the Gemini content-moderation runner.

Extracts and normalizes the JSON object returned by the model. Handles
<think> blocks, markdown fences, double-escaped quotes, trailing commas,
and truncated JSON. Supports both the compact (s/f) and verbose
(intents_triggered) output formats.

Usage:
    from parser import parse_llm_response
"""

from __future__ import annotations

import json
import re


def _normalize_compact_format(parsed: dict) -> dict:
    """Convert compact format → standard format for downstream use.

    Compact:   {"id": 123, "s": "Red", "f": [[12, "NSFW", 0.95], ...]}
    Standard:  {"session_id": 123, "session_severity": "Red",
                "intents_triggered": [{"turn_id": 12, ...}, ...]}
    """
    if "intents_triggered" in parsed:
        return parsed

    result = {
        "session_id": parsed.get("id", parsed.get("session_id", 0)),
        "session_severity": parsed.get("s", parsed.get("session_severity", "Green")),
    }

    flags = parsed.get("f", parsed.get("flags", []))
    intents = []
    for flag in flags:
        if isinstance(flag, list) and len(flag) >= 2:
            raw_conf = flag[2] if len(flag) > 2 else 1.0
            intents.append({
                "turn_id": flag[0],
                "intent_id": str(flag[1]),
                "confidence": raw_conf,
            })
        elif isinstance(flag, dict):
            intents.append(flag)

    result["intents_triggered"] = intents
    result["notes"] = parsed.get("n", parsed.get("notes", ""))
    return result


def parse_llm_response(raw: str) -> dict:
    """Extract and parse JSON from the LLM response.

    Handles: <think> blocks, markdown fences, truncated JSON, trailing commas.
    Supports both compact (s/f) and verbose (intents_triggered) formats.
    """
    text = raw.strip()

    # Strip Qwen3-style <think>...</think> blocks
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)

    # Strip markdown code fences
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```\s*', '', text)
    text = text.strip()

    # Fix double-escaped quotes
    if '\\"' in text:
        text = text.replace('\\"', '"')

    # Find the JSON object
    start = text.find('{')
    if start == -1:
        if len(raw.strip()) == 0:
            raise ValueError("Empty response — likely content blocked by model safety filters")
        raise ValueError(f"No JSON object found (len={len(raw)}): {raw[:100]!r}")

    json_text = text[start:]

    # Fast path: full parse
    end = json_text.rfind('}')
    if end != -1:
        candidate = json_text[:end + 1]
        candidate = re.sub(r',\s*}', '}', candidate)
        candidate = re.sub(r',\s*]', ']', candidate)
        try:
            return _normalize_compact_format(json.loads(candidate))
        except json.JSONDecodeError:
            pass

    # Aggressive truncation recovery
    lines = json_text.split('\n')
    for trim_count in range(1, min(len(lines), 10)):
        trimmed = '\n'.join(lines[:-trim_count])
        last_comma = trimmed.rfind(',')
        last_bracket = trimmed.rfind(']')
        last_brace = trimmed.rfind('}')
        cut_point = max(last_comma, last_bracket, last_brace)
        if cut_point <= 0:
            continue
        attempt = trimmed[:cut_point + 1]
        attempt = re.sub(r',\s*$', '', attempt)
        open_brackets = attempt.count('[') - attempt.count(']')
        open_braces = attempt.count('{') - attempt.count('}')
        attempt += ']' * max(open_brackets, 0)
        attempt += '}' * max(open_braces, 0)
        attempt = re.sub(r',\s*}', '}', attempt)
        attempt = re.sub(r',\s*]', ']', attempt)
        try:
            return _normalize_compact_format(json.loads(attempt))
        except json.JSONDecodeError:
            continue

    # Last resort
    if '}' not in json_text:
        last_good = max(json_text.rfind(','), json_text.rfind('['), json_text.rfind('{'))
        if last_good > 0:
            json_text = json_text[:last_good]
        open_braces = json_text.count('{') - json_text.count('}')
        open_brackets = json_text.count('[') - json_text.count(']')
        json_text += ']' * max(open_brackets, 0)
        json_text += '}' * max(open_braces, 0)
    else:
        end = json_text.rfind('}')
        json_text = json_text[:end + 1]

    json_text = re.sub(r',\s*}', '}', json_text)
    json_text = re.sub(r',\s*]', ']', json_text)
    json_text = re.sub(r'"\s*"(?=[a-zA-Z_])', '", "', json_text)

    try:
        return _normalize_compact_format(json.loads(json_text))
    except json.JSONDecodeError:
        try:
            fixed = re.sub(r'"\s+"', '", "', json_text)
            return _normalize_compact_format(json.loads(fixed))
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Parse error: {e}\nAttempted JSON: {json_text[:200]}..."
            ) from e
