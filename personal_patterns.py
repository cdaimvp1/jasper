"""
personal_patterns.py — Personal Response Learning (task #45 Phase 1, task
#49 Phase 2): mines Marc's own words for deterministic, zero-LLM co-
occurrence patterns - which systems/data sources he references, which
task-verbs he consistently reaches for. No attempt at "how he phrases
things" here - that's a genuinely fuzzier signal than keyword matching can
honestly claim to capture, and is deliberately deferred to a future bounded/
periodic LLM pass (the sentinel-worker concept discussed but not yet built)
rather than faked with a regex.

Three surfaces so far, each independently toggleable:
  app_chat   (#45) - Marc's own questions in socrates_retrieval_log. Zero new
              ingestion; this data already exists.
  sent_mail  (#49) - Marc's own Sent Items, via a dedicated, minimal COM scan
              (ingest/outlook_scan_sent.ps1) - deliberately NOT routed
              through outlook_com_ingest.py's full pipeline, since sent mail
              isn't triage input and nothing here becomes a permanent
              document.
  sent_teams (#50) - Marc's own messages within Teams chats already
              ingested by the existing pipeline (ingest/normalize.py's
              _process_teams_chat captures both directions of every chat
              already) - zero new ingestion here either, just identifying
              which already-stored rows are his (see
              workgraph_store.get_teams_messages_from_actor_since's own
              caveat on how that match works and its limits).

Gates: config.get("personal_learning", "enabled") is the master switch;
config.get("personal_learning", "surfaces", <name>) enables one specific
surface. run_daily_if_due() runs every enabled surface once/day and returns
a per-surface report - None only when the master switch is off, no surface
at all is enabled, or it already ran today (three distinct, real "did not
run" reasons, never a silent no-op). Everything below run_daily_if_due() is
pure/ungated on purpose (same shape as retention.py), so it stays directly
testable without fighting config state.

Task #59: attach_citations() is the one honest visible consumer of what's
been learned. It ONLY ever cites - "you've referenced X in similar
situations N times before" - the same "cited as precedent" framing Total
Recall already uses for issue reasoning. It never rewrites a draft or any
other content; that would be a real overreach past what a keyword-matched
pattern can honestly support.
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


def mine_sent_teams(since_ts: float) -> dict:
    """Phase 3 (task #50): identifies which already-ingested teams_chat
    raw_items are Marc's own sent messages (matched against
    config.manager.id) and mines them the same way as the other two
    surfaces. No new ingestion - see module docstring. Returns
    {"scanned","matched","cursor"} - if manager.id isn't set (shouldn't
    happen in practice, but this must never crash a daily scheduled job over
    it), scanned/matched are 0 and cursor is unchanged."""
    manager_id = config.get("manager", "id") or ""
    if not manager_id:
        return {"scanned": 0, "matched": 0, "cursor": since_ts}
    rows = ws.get_teams_messages_from_actor_since(manager_id, since_ts)
    matched = 0
    max_ts = since_ts
    for row in rows:
        ts = row["occurred_ts"]
        if ts > max_ts:
            max_ts = ts
        text = f"{row.get('subject') or ''} {row.get('body_preview') or ''}"
        keys = extract_patterns(text)
        for key in keys:
            ws.upsert_response_pattern("sent_teams", key, row.get("body_preview") or "", ts)
        if keys:
            matched += 1
    return {"scanned": len(rows), "matched": matched, "cursor": max_ts}


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
    if surfaces.get("sent_teams"):
        since_ts = float(ws.get_cursor("personal_learning", "sent_teams_cursor") or "0")
        r = mine_sent_teams(since_ts)
        ws.set_cursor("personal_learning", "sent_teams_cursor", str(r["cursor"]))
        result["sent_teams"] = r

    if not result:
        return None  # master on, but no individual surface enabled yet
    ws.set_cursor("personal_learning", "last_run_date", today)
    return result


# --- task #59: the one visible consumer -------------------------------------

# Below this, a pattern is too thin to honestly say "you usually..." - one or
# two hits could just be coincidence, not a real habit worth citing back.
MIN_CITATION_HIT_COUNT = 3


def citation_for_text(text: str) -> Optional[dict]:
    """Given a piece of evidence text, returns the highest-hit_count learned
    pattern it matches (from ANY surface), as long as that pattern has been
    seen at least MIN_CITATION_HIT_COUNT times - or None. This only ever
    cites; it never rewrites a draft or any other content."""
    keys = set(extract_patterns(text))
    if not keys:
        return None
    for pattern in ws.list_response_patterns():  # already ORDER BY hit_count DESC
        if pattern["pattern_key"] in keys and pattern["hit_count"] >= MIN_CITATION_HIT_COUNT:
            return {
                "pattern_key": pattern["pattern_key"], "hit_count": pattern["hit_count"],
                "note": f'You\'ve referenced "{pattern["pattern_key"]}" in similar '
                        f'situations {pattern["hit_count"]} times before.',
            }
    return None


def attach_citations(evidence: list[dict]) -> list[dict]:
    """Mutates and returns `evidence`: adds a "learned_citation" key ({
    "pattern_key","hit_count","note"} or None) to each row. A no-op (every
    row gets None) when Personal Response Learning is off - there's no
    learned data to cite in that case anyway, but the explicit check keeps
    the gate visible in code rather than relying on an empty table."""
    if not config.get("personal_learning", "enabled"):
        for ev in evidence:
            ev["learned_citation"] = None
        return evidence
    for ev in evidence:
        ev["learned_citation"] = citation_for_text(ev.get("summary") or "")
    return evidence
