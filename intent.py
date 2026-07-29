"""
intent.py — regex-based intent classifier for assistant messages.

Each `classify_message(text, actor)` call returns a list of detected intents.
Intent kinds:
  - asking_question     : worker is asking another worker something
  - blocked_on          : worker is stuck / waiting
  - needs_george        : worker is escalating to manager (configurable manager id)
  - proposing_decision  : worker is proposing a decision/change
  - completed           : worker just finished a task
  - handoff             : explicit task assignment to another worker

Worker resolution (session_id → member id) uses the members.json roster.
This is a v2 simplification — no more reader.py soup, no separate session_map.json.
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

import config
import members as members_mod
from paths import CC_SESSIONS_ROOT


# ---------------------------------------------------------------------------
# Worker resolution (cached — TTL 60s)
# ---------------------------------------------------------------------------
_resolution_cache: dict[str, str] = {}
_cache_built_at: float = 0.0
_CACHE_TTL_S = 60.0


def _rebuild_cache() -> None:
    """Build session_id → member_id map from members.json + CC session metadata."""
    global _resolution_cache, _cache_built_at
    cache: dict[str, str] = {}
    # 1. Direct mapping from members.json (member.session_id → member.id)
    for m in members_mod.list_members():
        sid = m.get("session_id")
        if sid:
            cache[sid] = m["id"]
    # 2. CC session metadata fallback: any session whose name matches a member's name
    if CC_SESSIONS_ROOT.is_dir():
        import json as _json
        for jf in CC_SESSIONS_ROOT.glob("*.json"):
            try:
                d = _json.loads(jf.read_text(encoding="utf-8"))
                sid = d.get("sessionId")
                if not sid or sid in cache: continue
                name = (d.get("name") or "").lower().replace("_", " ").replace("-", " ")
                for m in members_mod.list_members():
                    short = (m.get("short") or "").lower()
                    full = (m.get("name") or "").lower()
                    if not name: continue
                    if full and full in name:
                        cache[sid] = m["id"]; break
                    if short and (f" {short} " in f" {name} " or name.endswith(short)):
                        cache[sid] = m["id"]; break
            except Exception: continue
    _resolution_cache = cache
    _cache_built_at = time.time()


def resolve_member(session_id: str = "", cwd: str = "", jsonl_path: str = "") -> Optional[str]:
    """Resolve a session/file to a member id. Best-effort, never raises."""
    if (time.time() - _cache_built_at) > _CACHE_TTL_S or not _resolution_cache:
        _rebuild_cache()
    if session_id and session_id in _resolution_cache:
        return _resolution_cache[session_id]
    return None


def reset_caches() -> None:
    global _resolution_cache, _cache_built_at
    _resolution_cache = {}
    _cache_built_at = 0.0


# ---------------------------------------------------------------------------
# Intent patterns
# ---------------------------------------------------------------------------
def _find_target_member(text: str, exclude: Optional[str] = None) -> Optional[str]:
    """Find a member shortname (e.g. AB, CB) mentioned in text. Returns member id."""
    lower = text.lower()
    short_map = members_mod.short_to_slug()  # {'ab': 'aria_builder', ...}

    # @-mentions are most explicit
    for short, slug in short_map.items():
        if slug == exclude: continue
        if re.search(rf"@{short}\b", lower): return slug

    # Address patterns
    for short, slug in short_map.items():
        if slug == exclude: continue
        patterns = [
            rf"\b(ask|tell|ping|notify|hand off to|loop in|cc) {short}\b",
            rf"\b{short}: ",
            rf"\b{short},\s+",
            rf"\b{short} (will|can|should|needs to|please|owns)",
            rf"\bfor {short}\b",
        ]
        for pat in patterns:
            if re.search(pat, lower): return slug
    return None


QUESTION_PATTERNS = [
    re.compile(r"\?\s*$", re.MULTILINE),
    re.compile(r"^(can|could|would|should|will|do|does|is|are)\s.+\?", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bwhat (do|does|is|are|would|should|about)\b.+\?", re.IGNORECASE),
]
BLOCKED_PATTERNS = [
    re.compile(r"\b(blocked on|waiting for|stuck on|can'?t (proceed|continue|ship)|need(s)? .+ before)\b", re.IGNORECASE),
    re.compile(r"\b(unable to|prevented from)\s+\w+", re.IGNORECASE),
]
PROPOSAL_PATTERNS = [
    re.compile(r"^\s*(proposal|recommendation|recommend|suggest|let'?s|i propose|propose to)\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\bdecision:\s+", re.IGNORECASE),
]
HANDOFF_PATTERNS = [
    # Per scar 2026-05-01 #18 (`tr_a7871b0e6c` family): bare `action` matched word-internal `action-button`.
    # Tightened to `action item|take action|action for` — explicit handoff verb-shapes only.
    re.compile(r"\b(ab|cb|cs|tb|oc)[:,]\s+(please|can you|could you|need you to|own this|take this|action item|take action|action for)\b", re.IGNORECASE),
    re.compile(r"\b(handing off to|handoff to|over to|action item for|ownership goes to)\s+(ab|cb|cs|tb|oc)\b", re.IGNORECASE),
]
COMPLETED_PATTERNS = [
    re.compile(r"\b(done|shipped|landed|merged|completed|finished|wrote|built|deployed|published)\b.{0,80}\b(it|that|this|the .+|to .+)\b", re.IGNORECASE),
    re.compile(r"^\s*(✓|✅|done\.|shipped\.|complete\.|finished\.)", re.IGNORECASE | re.MULTILINE),
]


def _needs_manager_patterns():
    mgr_id = (config.get("manager", "id") or "manager").lower()
    return [
        re.compile(rf"@{re.escape(mgr_id)}\b", re.IGNORECASE),
        re.compile(rf"\bneeds? {re.escape(mgr_id)}\b", re.IGNORECASE),
        re.compile(rf"\bescalat(e|ing) to {re.escape(mgr_id)}\b", re.IGNORECASE),
        re.compile(rf"\b{re.escape(mgr_id)}.{{0,30}}(decision|approval|sign[- ]?off|input|to (decide|approve|review))", re.IGNORECASE),
    ]


def _snippet_around(text: str, match: re.Match, span: int = 80) -> str:
    s, e = match.span()
    start, end = max(0, s - span), min(len(text), e + span)
    snip = text[start:end].replace("\n", " ").strip()
    if start > 0: snip = "…" + snip
    if end < len(text): snip = snip + "…"
    return snip


def classify_message(text: str, actor: Optional[str] = None) -> list[dict[str, Any]]:
    intents: list[dict[str, Any]] = []
    if not text or len(text.strip()) < 3: return intents

    # needs_manager (highest priority)
    for pat in _needs_manager_patterns():
        m = pat.search(text)
        if m:
            mgr_id = config.get("manager", "id") or "manager"
            intents.append({"kind": "needs_george", "snippet": _snippet_around(text, m),
                            "target": mgr_id, "confidence": 0.85})
            break

    for pat in BLOCKED_PATTERNS:
        m = pat.search(text)
        if m:
            intents.append({"kind": "blocked_on", "snippet": _snippet_around(text, m),
                            "target": _find_target_member(text, exclude=actor), "confidence": 0.7})
            break

    target = _find_target_member(text, exclude=actor)
    if target:
        for pat in QUESTION_PATTERNS:
            m = pat.search(text)
            if m:
                intents.append({"kind": "asking_question", "snippet": _snippet_around(text, m),
                                "target": target, "confidence": 0.6})
                break

    for pat in HANDOFF_PATTERNS:
        m = pat.search(text)
        if m:
            intents.append({"kind": "handoff", "snippet": _snippet_around(text, m),
                            "target": _find_target_member(text, exclude=actor), "confidence": 0.7})
            break

    for pat in PROPOSAL_PATTERNS:
        m = pat.search(text)
        if m:
            intents.append({"kind": "proposing_decision", "snippet": _snippet_around(text, m),
                            "confidence": 0.55})
            break

    for pat in COMPLETED_PATTERNS:
        m = pat.search(text)
        if m:
            intents.append({"kind": "completed", "snippet": _snippet_around(text, m), "confidence": 0.5})
            break

    return intents
