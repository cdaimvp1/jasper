"""
personal_patterns.py — Personal Response Learning (task #45 Phase 1, task
#49 Phase 2): mines Marc's own words for deterministic, zero-LLM co-
occurrence patterns - which systems/data sources he references, which
task-verbs he consistently reaches for. No attempt at "how he phrases
things" here - that's a genuinely fuzzier signal than keyword matching can
honestly claim to capture, and is deliberately deferred to a future bounded/
periodic LLM pass (the sentinel-worker concept discussed but not yet built)
rather than faked with a regex.

Two surfaces so far, each independently toggleable:
  app_chat  (#45) - Marc's own questions in socrates_retrieval_log. Zero new
             ingestion; this data already exists.
  sent_mail (#49) - Marc's own Sent Items, via a dedicated, minimal COM scan
             (ingest/outlook_scan_sent.ps1) - deliberately NOT routed through
             outlook_com_ingest.py's full pipeline, since sent mail isn't
             triage input and nothing here becomes a permanent document.

Gates: config.get("personal_learning", "enabled") is the master switch;
config.get("personal_learning", "surfaces", <name>) enables one specific
surface. run_daily_if_due() runs every enabled surface once/day and returns
a per-surface report - None only when the master switch is off, no surface
at all is enabled, or it already ran today (three distinct, real "did not
run" reasons, never a silent no-op). Everything below run_daily_if_due() is
pure/ungated on purpose (same shape as retention.py), so it stays directly
testable without fighting config state.

Sent Teams (task #50) is a separate, later module.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

import config
import workgraph_store as ws

_SENT_SCAN_SCRIPT = Path(__file__).resolve().parent / "ingest" / "outlook_scan_sent.ps1"

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
    """Every pattern_key whose regex matches `text` - one question/email can
    carry more than one (e.g. "check the Ariba PO status" matches both
    "ariba" and "check status")."""
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


def mine_sent_mail(since_ts: float) -> dict:
    """Scans Sent Items since since_ts via the dedicated outlook_scan_sent.ps1
    (not outlook_com_ingest.py's pipeline - see module docstring). Extracts
    patterns from subject + a body excerpt, upserts under
    source_surface='sent_mail'. Returns {"scanned","matched","cursor"} - same
    shape as mine_app_chat. A PowerShell failure (Outlook not running, etc.)
    is reported as scanned=0 with an "error" key rather than raised, so one
    bad scan never blocks the day's app_chat mining."""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-File", str(_SENT_SCAN_SCRIPT),
         "-SinceEpoch", str(since_ts), "-MaxItems", "500"],
        capture_output=True, encoding="utf-8", timeout=120,
    )
    scanned = 0
    matched = 0
    max_ts = since_ts
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue  # malformed line - skip, don't fail the whole batch
        scanned += 1
        ts = float(item.get("sent_epoch") or 0)
        if ts > max_ts:
            max_ts = ts
        text = f"{item.get('subject') or ''} {item.get('body_excerpt') or ''}"
        keys = extract_patterns(text)
        for key in keys:
            ws.upsert_response_pattern("sent_mail", key, item.get("subject") or "", ts)
        if keys:
            matched += 1
    result = {"scanned": scanned, "matched": matched, "cursor": max_ts}
    if proc.returncode != 0:
        result["error"] = proc.stderr.strip() or f"exit code {proc.returncode}"
    return result


def run_daily_if_due(now: Optional[float] = None) -> Optional[dict]:
    """Gate for scheduled_refresh.py - mirrors retention.run_daily_if_due's
    exact shape (same ingest_cursors mechanism, source='personal_learning').
    Runs every surface whose own toggle is on, once/day, and returns a
    per-surface report keyed by surface name (e.g. {"app_chat": {...},
    "sent_mail": {...}}) - only the enabled surfaces appear. Returns None -
    a real, checkable 'did not run' result, never a silent no-op - when the
    master toggle is off, no surface at all is enabled, or this already ran
    today."""
    if now is None:
        now = time.time()
    if not config.get("personal_learning", "enabled"):
        return None
    surfaces = config.get("personal_learning", "surfaces") or {}
    if not isinstance(surfaces, dict):
        surfaces = {}
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if ws.get_cursor("personal_learning", "last_run_date") == today:
        return None

    result: dict = {}
    if surfaces.get("app_chat"):
        since_ts = float(ws.get_cursor("personal_learning", "app_chat_cursor") or "0")
        r = mine_app_chat(since_ts)
        ws.set_cursor("personal_learning", "app_chat_cursor", str(r["cursor"]))
        result["app_chat"] = r
    if surfaces.get("sent_mail"):
        since_ts = float(ws.get_cursor("personal_learning", "sent_mail_cursor") or "0")
        r = mine_sent_mail(since_ts)
        ws.set_cursor("personal_learning", "sent_mail_cursor", str(r["cursor"]))
        result["sent_mail"] = r

    if not result:
        return None  # master on, but no individual surface enabled yet
    ws.set_cursor("personal_learning", "last_run_date", today)
    return result
