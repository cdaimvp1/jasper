"""
personal_patterns.py — Phase 1 of Personal Response Learning (task #45):
mines Marc's own in-app questions (socrates_retrieval_log) for deterministic,
zero-LLM co-occurrence patterns - which systems/data sources he references,
which task-verbs he consistently reaches for. No attempt at "how he phrases
things" here - that's a genuinely fuzzier signal than keyword matching can
honestly claim to capture, and is deliberately deferred to a future bounded/
periodic LLM pass (the sentinel-worker concept discussed but not yet built)
rather than faked with a regex.

Off by default, two independent gates:
  config.get("personal_learning", "enabled")             - master switch
  config.get("personal_learning", "surfaces", "app_chat") - this surface

Both must be true for run_daily_if_due() to do anything. Everything below
run_daily_if_due() is pure/ungated on purpose (same shape as retention.py:
the gate lives at the one real entry point, not scattered through the
module), so it stays directly testable without fighting config state.

Sent mail (task #49) and sent Teams (task #50) are separate, later modules -
this file is app_chat only.
"""
from __future__ import annotations

import re
import time
from typing import Optional

import config
import workgraph_store as ws

# Cheap, explainable keyword matching over fancier NLP - same house style as
# workgraph_recommend.py's _APPROVAL_RE and workgraph_nba.py's value-extraction
# regex. Meant to grow with real usage, not be exhaustive on day one - add a
# line here when a real recurring system/verb shows up in practice.
_SYSTEM_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("ariba", re.compile(r"\bariba\b", re.IGNORECASE)),
    ("sap", re.compile(r"\bsap\b", re.IGNORECASE)),
    ("docusign", re.compile(r"\bdocusign\b", re.IGNORECASE)),
    ("adobe sign", re.compile(r"\badobe\s*sign\b", re.IGNORECASE)),
    ("sharepoint", re.compile(r"\bsharepoint\b", re.IGNORECASE)),
    ("teams", re.compile(r"\bteams\b", re.IGNORECASE)),
    ("outlook", re.compile(r"\boutlook\b", re.IGNORECASE)),
    ("contractpodai", re.compile(r"\bcontractpod\w*\b", re.IGNORECASE)),
    ("aravo", re.compile(r"\baravo\b", re.IGNORECASE)),
    ("servicenow", re.compile(r"\bservicenow\b", re.IGNORECASE)),
]
_TASK_VERB_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("check status", re.compile(r"\bstatus\b", re.IGNORECASE)),
    ("draft reply", re.compile(r"\bdraft\w*\b", re.IGNORECASE)),
    ("summarize", re.compile(r"\bsummar\w*\b", re.IGNORECASE)),
    ("find/locate", re.compile(r"\b(find|locate|where\s?'?s|where is)\b", re.IGNORECASE)),
]
_ALL_PATTERNS = _SYSTEM_PATTERNS + _TASK_VERB_PATTERNS


def extract_patterns(text: str) -> list[str]:
    """Every pattern_key whose regex matches `text` - one question can carry
    more than one (e.g. "check the Ariba PO status" matches both "ariba" and
    "check status")."""
    if not text:
        return []
    return [key for key, rx in _ALL_PATTERNS if rx.search(text)]


def mine_app_chat(since_ts: float) -> dict:
    """Reads every distinct question in socrates_retrieval_log strictly after
    since_ts, extracts patterns, upserts each into response_patterns under
    source_surface='app_chat'. Returns {"scanned","matched","cursor"} -
    cursor is the highest asked_ts actually seen (unchanged from since_ts if
    nothing new came in), for the caller to persist forward."""
    rows = ws.get_socrates_log_since(since_ts)
    matched = 0
    max_ts = since_ts
    for row in rows:
        ts = row["asked_ts"]
        if ts > max_ts:
            max_ts = ts
        question = row.get("question") or ""
        keys = extract_patterns(question)
        for key in keys:
            ws.upsert_response_pattern("app_chat", key, question, ts)
        if keys:
            matched += 1
    return {"scanned": len(rows), "matched": matched, "cursor": max_ts}


def run_daily_if_due(now: Optional[float] = None) -> Optional[dict]:
    """Gate for scheduled_refresh.py - mirrors retention.run_daily_if_due's
    exact shape (same ingest_cursors mechanism, source='personal_learning').
    Returns None - a real, checkable 'did not run' result, not a silent
    no-op - when the master toggle or the app_chat sub-toggle is off, or
    this has already run today."""
    if now is None:
        now = time.time()
    if not config.get("personal_learning", "enabled"):
        return None
    surfaces = config.get("personal_learning", "surfaces") or {}
    if not (isinstance(surfaces, dict) and surfaces.get("app_chat")):
        return None
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if ws.get_cursor("personal_learning", "last_run_date") == today:
        return None
    since_ts = float(ws.get_cursor("personal_learning", "app_chat_cursor") or "0")
    result = mine_app_chat(since_ts)
    ws.set_cursor("personal_learning", "app_chat_cursor", str(result["cursor"]))
    ws.set_cursor("personal_learning", "last_run_date", today)
    return result
