"""
rule_extraction.py — task #53: attempts to structure a freeform sentence
into a candidate Aristotle rule shape, using a local LLM (Ollama) rather
than a live worker or a cloud API - nothing leaves this machine. This is a
genuinely different discipline than the rest of this codebase: everywhere
else, "no LLM calls" is the standing rule (workgraph_socrates.py, workgraph_
nba.py, health_check.py, retention.py are all explicitly zero-LLM). This
module is the one deliberate exception, named as such rather than hidden -
turning a real sentence into structured fields needs actual language
understanding that no regex/keyword approach can honestly claim.

The model's output is NEVER trusted blindly. Every signal type it returns is
validated against workgraph_signals.known_signal_types() before anything
downstream sees it - a hallucinated signal type that doesn't exist gets
treated exactly like "couldn't structure it," not silently passed through.

Graceful degradation is a first-class requirement, not an afterthought:
Ollama not running, the model not pulled, a timeout, a malformed response -
all of these return None, and the caller (rule_teaching.py) falls back to
capturing the raw text with no structured guess. Confirmed necessary in
practice, not just in theory: at the time this was built, Ollama's own
model registry was unreachable from this network (a corporate TLS-
inspection issue, not fixable from this session) - so this module had to be
correct with ZERO successful live model calls to test against. Every test
here mocks the HTTP layer; if a model becomes pullable later, the real
behavior in production is exactly what those tests already exercise.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional

import workgraph_signals

OLLAMA_GENERATE_URL = "http://localhost:11434/api/generate"
OLLAMA_VERSION_URL = "http://localhost:11434/api/version"
MODEL_NAME = "qwen2.5:1.5b-instruct"
TIMEOUT_SECONDS = 20


def _build_prompt(explanation: str, known_types: list[str]) -> str:
    types_list = ", ".join(known_types)
    return (
        "You extract a structured prerequisite rule from a sentence about a business process.\n"
        f"Known signal types (choose ONLY from this exact list, or null if none fit): {types_list}\n\n"
        f'Sentence: "{explanation}"\n\n'
        "Output ONLY a JSON object with exactly these keys:\n"
        '{"trigger_signal_type": <one of the known types, or null>, '
        '"requires_signal_type": <one of the known types that must happen FIRST, or null>, '
        '"match_on": "project" or "supplier" or null, '
        '"confidence": "high" or "low", '
        '"reflection": <one plain-English sentence reflecting back what you understood>}'
    )


def _call_ollama(prompt: str) -> Optional[str]:
    """Raw HTTP call to the local Ollama server. Returns the model's raw
    text response, or None on ANY failure (connection refused, timeout,
    non-200, malformed response body). Blocking - callers must always run
    this through asyncio.to_thread (this server has one uvicorn worker;
    task #42's lesson about a blocking call anywhere freezing everything)."""
    payload = json.dumps({
        "model": MODEL_NAME, "prompt": prompt, "stream": False, "format": "json",
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_GENERATE_URL, data=payload, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            if resp.status != 200:
                return None
            body = json.loads(resp.read().decode("utf-8"))
            return body.get("response")
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return None


def extract_rule_candidate(explanation: str) -> Optional[dict]:
    """Returns {"trigger_signal_type","requires_signal_type","match_on",
    "reflection"} - every signal/match field already validated against the
    real known list - or None if the model is unavailable, timed out,
    returned something unparseable, or was explicitly low-confidence. Never
    raises; the caller treats None as "capture the raw text, no structured
    guess" rather than a failure to handle specially."""
    if not explanation or not explanation.strip():
        return None
    known_types = workgraph_signals.known_signal_types()
    raw = _call_ollama(_build_prompt(explanation, known_types))
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("confidence") != "high":
        return None

    trigger = parsed.get("trigger_signal_type")
    requires = parsed.get("requires_signal_type")
    match_on = parsed.get("match_on")
    if trigger not in known_types or requires not in known_types:
        return None
    if trigger == requires:
        return None
    if match_on not in ("project", "supplier"):
        return None

    reflection = parsed.get("reflection")
    if not isinstance(reflection, str) or not reflection.strip():
        reflection = f"{trigger} needs {requires} first, matched by {match_on}"
    return {
        "trigger_signal_type": trigger, "requires_signal_type": requires,
        "match_on": match_on, "reflection": reflection.strip(),
    }


def extract_rule_draft(explanation: str) -> Optional[dict]:
    """Task #62: like extract_rule_candidate, but does NOT require
    confidence=='high' - returns whatever structured guess the model made
    (even a low-confidence or only-partially-filled one), so rule_teaching.
    py's conversational clarification flow has a starting point to refine
    rather than a Q&A starting completely from scratch. Every non-null
    signal-type field is still independently validated against
    known_signal_types() - a hallucinated type is discarded (treated as
    unresolved), never passed through just because SOME field looked valid.
    A resolved trigger/requires pair that turned out identical is also
    discarded back to unresolved (can't require itself). Returns None only
    when the model itself was unreachable, timed out, or returned
    unparseable JSON - unlike extract_rule_candidate, a well-formed but
    low-confidence response DOES come back here."""
    if not explanation or not explanation.strip():
        return None
    known_types = workgraph_signals.known_signal_types()
    raw = _call_ollama(_build_prompt(explanation, known_types))
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None

    trigger = parsed.get("trigger_signal_type")
    if trigger not in known_types:
        trigger = None
    requires = parsed.get("requires_signal_type")
    if requires not in known_types:
        requires = None
    if trigger is not None and trigger == requires:
        requires = None
    match_on = parsed.get("match_on")
    if match_on not in ("project", "supplier"):
        match_on = None

    reflection = parsed.get("reflection")
    reflection = reflection.strip() if isinstance(reflection, str) and reflection.strip() else None
    return {
        "trigger_signal_type": trigger, "requires_signal_type": requires,
        "match_on": match_on, "reflection": reflection,
    }


def is_ollama_reachable() -> bool:
    """Cheap reachability check (not a full extraction attempt) - for a
    Settings/status indicator, distinct from actually trying to extract."""
    try:
        with urllib.request.urlopen(OLLAMA_VERSION_URL, timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False
