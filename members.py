"""
members.py — Member abstraction + ClaudeCLIMember impl.

A Member is an identity the substrate observes and can dispatch work to.
Members have:
  - id        (slug, stable)
  - short     (2-3 char display)
  - name      (display name)
  - role      (responsibility description)
  - kind      (impl type: claude_cli | slack_user | github_bot | …)
  - recovery_file (path, for claude_cli kind: appended as system prompt on dispatch)
  - active_file   (path, the member's mid-flight scratchpad)
  - session_id    (for claude_cli kind: their CC session UUID)

Members are loaded from config/members.json. Add/remove members by editing
that file (or via /api/members in v2 server).

Future kinds: SlackUserMember, GithubBotMember, etc. — same interface.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from paths import CONFIG_DIR, WORKSPACE_ROOT, CC_SESSIONS_ROOT

MEMBERS_PATH = CONFIG_DIR / "members.json"

_lock = threading.Lock()
_cache: list[dict[str, Any]] = []
_cache_mtime: float = 0.0


def _load() -> list[dict[str, Any]]:
    global _cache, _cache_mtime
    try:
        m = MEMBERS_PATH.stat().st_mtime
    except OSError:
        return _cache or []
    if m == _cache_mtime and _cache:
        return _cache
    try:
        data = json.loads(MEMBERS_PATH.read_text(encoding="utf-8"))
        _cache = data.get("members", [])
        _cache_mtime = m
    except Exception as e:
        # Same gap as config.py's _load (fixed alongside it, 2026-07-29): a bad
        # members.json used to silently keep serving the stale roster forever,
        # with nothing anywhere indicating the file on disk had gone bad.
        print(f"[members] failed to parse {MEMBERS_PATH}: {e!r} - using last-known-good cache", file=sys.stderr)
    return _cache or []


def list_members() -> list[dict[str, Any]]:
    with _lock:
        return [dict(m) for m in _load()]


def get_member(member_id: str) -> Optional[dict[str, Any]]:
    for m in list_members():
        if m.get("id") == member_id:
            return m
    return None


def member_ids() -> list[str]:
    return [m.get("id") for m in list_members() if m.get("id")]


def slug_to_short() -> dict[str, str]:
    return {m["id"]: m.get("short", m["id"][:2].upper()) for m in list_members() if m.get("id")}


def short_to_slug() -> dict[str, str]:
    """Map 'ab' -> 'aria_builder' AND 'abe' -> 'aria_builder'. Lowercase keys.

    Includes both the 2-letter short slug (canonical) and the human display_name
    (alias), so @-mention parsers route @abe and @ab to the same worker.
    Display-names added 2026-05-23 per George/cohort (Theo/Abe/Coby/Sage/Iris).
    """
    m_list = list_members()
    out: dict[str, str] = {}
    for m in m_list:
        slug = m.get("id")
        if not slug:
            continue
        short = m.get("short", "").lower()
        if short:
            out[short] = slug
        dn = m.get("display_name", "").lower()
        if dn:
            out[dn] = slug
    return out


# ---------------------------------------------------------------------------
# State (current "what is this member doing")
# ---------------------------------------------------------------------------
def member_state(member_id: str) -> dict[str, Any]:
    """Return current state of a member: liveness, active_file age, recent activity.

    Uses a combination of:
      - ~/.claude/sessions/<pid>.json for session metadata
      - active_file mtime
      - latest jsonl event for this member from the bus (TODO: when called)
    """
    m = get_member(member_id)
    if m is None: return {"id": member_id, "exists": False}

    state: dict[str, Any] = {
        "id": member_id,
        "exists": True,
        "name": m.get("name"),
        "short": m.get("short"),
        "role": m.get("role"),
        "kind": m.get("kind"),
    }

    # active_file mtime. active_file is roster config data (config/members.json),
    # not raw HTTP input, but a bad/malicious entry ("../../../../some/file") would
    # otherwise let member_state() read and excerpt anything reachable from
    # WORKSPACE_ROOT via traversal or an absolute-path override - same class of bug
    # fixed in inbox.py's _safe_member_path this session. Resolve and confirm the
    # path is still inside WORKSPACE_ROOT before touching it; skip (don't crash the
    # whole roster/status view over one bad entry) if not.
    active_path = (WORKSPACE_ROOT / m.get("active_file", "")).resolve()
    if not active_path.is_relative_to(WORKSPACE_ROOT.resolve()):
        active_path = None
    if active_path is not None and active_path.is_file():
        try:
            mt = active_path.stat().st_mtime
            state["active_md_mtime"] = mt
            state["active_md_age_s"] = int(time.time() - mt)
            state["active_md_path"] = str(active_path)
            # First non-empty line as "now" hint
            text = active_path.read_text(encoding="utf-8", errors="replace")[:5000]
            state["active_md_excerpt"] = text[:600]
            # Pull "Right now" or first H2/H3 section content
            state["now"] = _extract_now(text)
        except OSError:
            pass

    # liveness via CC session metadata
    sid = m.get("session_id")
    if sid and CC_SESSIONS_ROOT.is_dir():
        for jf in CC_SESSIONS_ROOT.glob("*.json"):
            try:
                d = json.loads(jf.read_text(encoding="utf-8"))
                if d.get("sessionId") == sid:
                    state["session_status"] = d.get("status")
                    state["session_kind"] = d.get("kind")
                    state["session_pid"] = d.get("pid")
                    if d.get("updatedAt"):
                        state["session_updated_at"] = d.get("updatedAt")
                    break
            except Exception: continue

    return state


def _extract_now(text: str) -> str:
    """Best-effort 'what is the worker doing right now' from active.md."""
    if not text: return ""
    # Look for "Right now" / "Right Now" / "Currently" section
    import re
    for label in ("Right now", "Right Now", "Currently", "Now", "RIGHT NOW"):
        m = re.search(rf"^\s*#+\s*{label}\b.*?$\n+(.+?)(?=\n#|\Z)",
                      text, re.MULTILINE | re.DOTALL)
        if m:
            return m.group(1).strip()[:400]
    # Fallback: first non-blank paragraph after any header
    paras = [p.strip() for p in text.split("\n\n") if p.strip() and not p.startswith("#")]
    return (paras[0][:400] if paras else "")
