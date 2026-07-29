"""Team Room — ambient culture surface.

Distinct from meetings: unbounded artifact, no close lifecycle, no moderator,
no participant management. File-only at aria_sync/team_room/<YYYY_MM>.md with
current.md symlink for Monitor-tailability. Monthly rotation handled lazily
on first write of each month.

Per design doc: team_lab/concept/room_vs_meeting_2026_04_27.md (closed 5-of-5
2026-04-27). Path C: file-only module sharing only the file-append + Monitor-tail
primitive with meetings.py — not a kind-column fork.
"""
from __future__ import annotations

import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from bus import emit_event
from paths import ARIA_SYNC  # single env-driven (TEAM_WORKSPACE_ROOT) shared root

SYNC_ROOT = ARIA_SYNC
ROOM_DIR = SYNC_ROOT / "team_room"
CURRENT_LINK = ROOM_DIR / "current.md"

# Reactions: append-only JSONL event log per month; state derived by replay.
# Per @george sig tr_5553c13f27 (2026-05-08) — TASK row + CULTURE row, anti-complacency rules-of-use.
#  TASK    👍 ack · ✅ approved · 👀 thinking · 🎉 celebrate · ❌ disagree
#  CULTURE 🙏 thank_you · 🌟 model · 🤔 more_info · 🛠 capability · 📍 anchor
#  WORKFLOW (artifact-creating, secondary menu) 🚀 project · 🗓 meeting · 🔖 bookmark (private)
# DROPPED: clarify (3 uses · empirically dead · was the "react instead of asking" trap).
REACTION_KINDS = {
    # task
    "ack", "approved", "thinking", "celebrate", "disagree",
    # culture
    "thank_you", "model", "more_info", "capability", "anchor",
    # workflow / artifact-creating
    "project", "meeting", "bookmark",
}
PRIVATE_KINDS = {"bookmark"}  # only visible to the actor who reacted

_lock = threading.Lock()
_reactions_lock = threading.Lock()


def _month_key(t: Optional[float] = None) -> str:
    d = datetime.fromtimestamp(t) if t else datetime.now()
    return d.strftime("%Y_%m")


def current_file() -> Path:
    """Path of the file for the current month. Lazily creates + maintains symlink."""
    ROOM_DIR.mkdir(parents=True, exist_ok=True)
    f = ROOM_DIR / f"{_month_key()}.md"
    if not f.exists():
        f.write_text(
            f"# Team Room — {_month_key()}\n\n"
            f"Ambient culture surface. No moderation. No close.\n"
            f"Per design doc: `team_lab/concept/room_vs_meeting_2026_04_27.md`.\n\n",
            encoding="utf-8",
        )
    # Re-point symlink if it's stale (or absent)
    try:
        target_name = f.name
        need_relink = True
        if CURRENT_LINK.is_symlink():
            try: need_relink = os.readlink(CURRENT_LINK) != target_name
            except OSError: need_relink = True
        if need_relink:
            if CURRENT_LINK.exists() or CURRENT_LINK.is_symlink():
                try: CURRENT_LINK.unlink()
                except OSError: pass
            CURRENT_LINK.symlink_to(target_name)
    except OSError:
        pass  # symlink failures are non-fatal — direct file path still works
    return f


# Block-level regex used by edit/delete to find a specific message in-file.
# Matches the canonical room post shape: leading `\n---\nfrom:...message_id:..\n---\nbody`.
_POST_BLOCK_RE = re.compile(
    r"\n---\nfrom:\s*(?P<from>[^\n]+?)\s*\nts:\s*(?P<ts>\S+)\s*\nmessage_id:\s*(?P<mid>\S+)\s*\n---\n(?P<body>.*?)(?=\n---\n|\Z)",
    re.DOTALL,
)


def _find_file_containing(message_id: str) -> Optional[Path]:
    """Locate the month file that contains a given message_id. Searches current month first,
    then walks back through prior month files. Returns None if not found."""
    ROOM_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(ROOM_DIR.glob("20*_*.md"), reverse=True)  # most recent first
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        if f"message_id: {message_id}\n" in text:
            return f
    return None


def edit_message(*, message_id: str, new_body: str, actor: str) -> dict[str, Any]:
    """In-place rewrite of a team_room message body. Only the original sender can edit.
    Tracks edit count via leading `<!--edits:N-->\\n` marker (same shape as projects)."""
    if not (new_body or "").strip():
        raise ValueError("body required")
    f = _find_file_containing(message_id)
    if not f:
        raise ValueError(f"message not found: {message_id}")
    edited = False
    edit_count = 0
    with _lock:
        text = f.read_text(encoding="utf-8")
        def replacer(m):
            nonlocal edited, edit_count
            if m.group("mid") != message_id:
                return m.group(0)
            if m.group("from") != actor:
                raise PermissionError(f"only the sender ({m.group('from')}) can edit this message")
            body = m.group("body").rstrip()
            ec_match = re.match(r"^<!--edits:(\d+)-->\n", body)
            edit_count = (int(ec_match.group(1)) + 1) if ec_match else 1
            new_body_with_marker = f"<!--edits:{edit_count}-->\n{new_body.strip()}"
            edited = True
            return f"\n---\nfrom: {m.group('from')}\nts: {m.group('ts')}\nmessage_id: {m.group('mid')}\n---\n{new_body_with_marker}\n"
        new_text = _POST_BLOCK_RE.sub(replacer, text)
        if not edited:
            raise ValueError(f"message not found in file: {message_id}")
        f.write_text(new_text, encoding="utf-8")
    # Invalidate translation cache (per @george sluggish-fix · cache by message_id assumes immutability;
    # explicit edit invalidates per Liveness Floor empirical-self-verify discipline).
    try:
        from translator import invalidate_cache
        invalidate_cache(message_id)
    except Exception:
        pass
    # Smart Brevity rewriter DISABLED per @george tr_8a1f2fbb1c — invalidate-only on edit.
    try:
        from smart_preview import invalidate as _sp_invalidate
        _sp_invalidate(message_id)
    except Exception:
        pass
    emit_event(
        source="team_room", kind="team_room.message_edited", actor=actor, target="team_room",
        payload={"message_id": message_id, "edit_count": edit_count},
    )
    return {"ok": True, "message_id": message_id, "edit_count": edit_count}


def delete_message(*, message_id: str, actor: str) -> dict[str, Any]:
    """Soft-delete: replace body with deletion marker. Only the original sender can delete."""
    f = _find_file_containing(message_id)
    if not f:
        raise ValueError(f"message not found: {message_id}")
    ts = datetime.now().isoformat(timespec="seconds")
    deleted = False
    with _lock:
        text = f.read_text(encoding="utf-8")
        def replacer(m):
            nonlocal deleted
            if m.group("mid") != message_id:
                return m.group(0)
            if m.group("from") != actor:
                raise PermissionError(f"only the sender ({m.group('from')}) can delete this message")
            deleted = True
            new_body = f"<!--deleted-->\n_(deleted by @{actor} at {ts})_"
            return f"\n---\nfrom: {m.group('from')}\nts: {m.group('ts')}\nmessage_id: {m.group('mid')}\n---\n{new_body}\n"
        new_text = _POST_BLOCK_RE.sub(replacer, text)
        if not deleted:
            raise ValueError(f"message not found in file: {message_id}")
        f.write_text(new_text, encoding="utf-8")
    emit_event(
        source="team_room", kind="team_room.message_deleted", actor=actor, target="team_room",
        payload={"message_id": message_id},
    )
    return {"ok": True, "message_id": message_id, "deleted": True}


def post_message(sender: str, body: str, *, george_view: Optional[str] = None) -> dict[str, Any]:
    if not (body or "").strip():
        raise ValueError("body required")
    f = current_file()
    msg_id = "tr_" + uuid.uuid4().hex[:10]
    ts = datetime.now().isoformat(timespec="seconds")
    # Liveness Floor v0.2 substrate-enforcement: shared with projects.py via chat_substrate
    # Phase 1 of "delight George in George's view" substrate (TB tr_38c55b8899 2026-05-03):
    # optional george_view single-sentence summary for collapsed-view rendering. When omitted,
    # collapsed view falls back to body-preview as before. Phase 2 will add render-time
    # translation via translator.py + cohort_glossary.json (zero author burden path).
    from chat_substrate import audit_time_claims, auto_george_view as _auto_gv
    body_audited = audit_time_claims(body.strip(), ts)
    meta_lines = [f"from: {sender}", f"ts: {ts}", f"message_id: {msg_id}"]
    if george_view and george_view.strip():
        # Single-line; collapse internal newlines for header safety
        gv = " ".join(george_view.strip().split())
        meta_lines.append(f"george_view: {gv}")
    block = "\n---\n" + "\n".join(meta_lines) + "\n---\n" + body_audited + "\n"
    with _lock:
        with f.open("a", encoding="utf-8") as fh:
            fh.write(block)
    # Phase A of Team-tab redesign (TB pp_59b871f489 2026-05-03 per @george delight-me directive):
    # attach george_view to the event payload so /team home renders the summary, not the raw body.
    # Author-provided wins; auto-heuristic fills the rest.
    effective_gv = (george_view or "").strip() or (_auto_gv(body) or "")
    emit_event(
        source="team_room",
        kind="team_room.message",
        actor=sender,
        target="team_room",
        payload={
            "message_id": msg_id,
            "sender": sender,
            "body_preview": body[:16000],  # 2026-05-31 George pp_6a57a66b7e: 2000→16000 (matches chat_substrate · most posts >2K · readers go through the event)
            "george_view": effective_gv or None,
            "month": _month_key(),
        },
    )
    # Smart Brevity rewriter DISABLED per @george tr_8a1f2fbb1c 2026-05-03 — issue flagged,
    # turning off pending fix. Posts now land verbatim with no async rewrite spawned.
    return {"ok": True, "message_id": msg_id}


# Parser shared shape with meetings.py — segment-walker on `\n---\n` boundaries.
_META_RE = re.compile(
    # Substrate hardening (per @george tr_7418063bb9 2026-05-04 21:34 ET):
    # AB's worker emits ts with literal TZ abbreviation suffix ("2026-05-04T21:14:00 EDT")
    # which broke the previous `(?P<ts>\S+)` requirement → 2 most-recent AB posts dropped silently.
    # Now ts allows optional " EDT"/" PDT"/etc. trailing TZ abbrev (2-5 uppercase chars).
    r"^from:\s*(?P<from>[^\n]+?)\s*\nts:\s*(?P<ts>\S+(?:\s+[A-Z]{2,5})?)\s*\nmessage_id:\s*(?P<mid>\S+)(?:\s*\ngeorge_view:\s*(?P<gv>[^\n]+))?",
    re.DOTALL,
)
_SYSTEM_TS_RE = re.compile(r"^_System\s*·\s*([\d\-T:]+)_?:")


def _parse_file(text: str) -> list[dict[str, Any]]:
    """Parse team_room file. Per substrate-self-reference scar 2026-05-01 (`tr_a7871b0e6c`):
    `---` lines within a message body are preserved — segments after a META block that
    don't themselves parse as META get treated as body continuation, joined back with
    `\\n---\\n`. Block boundary discipline: `\\n---\\n` followed by a `from:` line is the
    only real boundary."""
    out: list[dict[str, Any]] = []
    segments = re.split(r"\n---\n", text)
    i = 1
    while i < len(segments):
        seg = segments[i].strip()
        if not seg:
            i += 1; continue
        m = _META_RE.match(seg)
        if m and i + 1 < len(segments):
            # Body absorbs subsequent segments that aren't META-headers (markdown `---` in body)
            body_parts = [segments[i + 1]]
            j = i + 2
            while j < len(segments):
                if _META_RE.match(segments[j].strip()):
                    break
                body_parts.append(segments[j])
                j += 1
            body = "\n---\n".join(body_parts).strip()
            edit_count = 0
            deleted = False
            ec = re.match(r"^<!--edits:(\d+)-->\n", body)
            if ec:
                edit_count = int(ec.group(1))
                body = body[ec.end():]
            dm = re.match(r"^<!--deleted-->\n", body)
            if dm:
                deleted = True
                body = body[dm.end():]
            gv = m.group("gv") if "gv" in (m.groupdict() or {}) else None
            out.append({
                "kind": "message",
                "from": m.group("from"),
                "ts": m.group("ts"),
                "message_id": m.group("mid"),
                "body": body,
                "george_view": (gv.strip() if gv else None),
                "edit_count": edit_count,
                "deleted": deleted,
            })
            i = j; continue
        if seg.startswith("_System") or seg.startswith("_MEETING "):
            ts = None
            ts_match = _SYSTEM_TS_RE.match(seg)
            if ts_match: ts = ts_match.group(1)
            out.append({
                "kind": "system",
                "from": "_system",
                "ts": ts,
                "message_id": None,
                "body": seg,
            })
            i += 1; continue
        i += 1
    return out


def _visible_len(s: str) -> int:
    """Count visible characters in a markdown-formatted string — excludes the format markers
    themselves so length budgets reflect what George actually sees, not the raw bytes."""
    stripped = s
    stripped = re.sub(r"\*\*([^\*]+)\*\*", r"\1", stripped)  # bold
    stripped = re.sub(r"\*([^\*]+)\*", r"\1", stripped)      # italic
    stripped = re.sub(r"`([^`]+)`", r"\1", stripped)         # inline code
    stripped = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", stripped)  # markdown links — visible text only
    return len(stripped)


def _auto_george_view(body: str, *, max_chars: int = 220) -> Optional[str]:
    """Phase 2 of "delight George" substrate (TB tr_13e95bdf15 2026-05-03 · refined per
    @george tr_d9afefc525): heuristic extractive george_view auto-generation that PRESERVES
    strategic formatting (bold/italic/code) from the source. Pure deterministic — no LLM.

    Strategy:
      1. Take the first non-empty paragraph
      2. Preserve `**bold**` `*italic*` `` `code` `` markdown — strategic formatting carries scan-affordance
      3. Strip markdown link syntax to visible-text only (URLs are noise in collapsed view)
      4. Compute length on VISIBLE chars (excluding markdown markers); truncate at word boundary
      5. Be safe near format-marker boundaries (don't truncate inside an open `**...`)
      6. Return None if body is empty or paragraphs contain no usable text

    Workers always win — explicit `george_view` field overrides this auto-derivation.
    """
    if not body or not body.strip():
        return None
    text = body.strip()
    # Take first paragraph (split on blank-line)
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paras:
        return None
    first_para = paras[0]
    # Per @george tr_328ec8bece 2026-05-03 — preserve markdown link syntax verbatim. The
    # team_room renderer now handles `[text](url)` → clickable anchor; auto-george_view used
    # to strip URLs (kept text only), but that broke "links must be clickable in collapsed view."
    # Keep `[text](url)` intact so the renderer makes it clickable.
    formatted = first_para
    # Collapse internal whitespace
    formatted = re.sub(r"\s+", " ", formatted).strip()
    if not formatted:
        return None
    # Truncate by visible-char budget while preserving markdown markers
    if _visible_len(formatted) <= max_chars:
        return formatted
    # Walk forward, accumulating visible chars until budget hit, then truncate at last word boundary
    out = []
    visible_count = 0
    i = 0
    in_bold = False
    in_italic = False
    in_code = False
    while i < len(formatted) and visible_count < max_chars:
        c = formatted[i]
        # Markdown markers don't count toward visible length
        if c == '*' and i + 1 < len(formatted) and formatted[i + 1] == '*':
            out.append("**")
            in_bold = not in_bold
            i += 2
            continue
        if c == '*':
            out.append("*")
            in_italic = not in_italic
            i += 1
            continue
        if c == '`':
            out.append("`")
            in_code = not in_code
            i += 1
            continue
        out.append(c)
        visible_count += 1
        i += 1
    truncated = "".join(out)
    # Close any unclosed format spans before truncating further
    if in_bold:
        truncated += "**"
    if in_italic:
        truncated += "*"
    if in_code:
        truncated += "`"
    # Truncate at last word boundary
    last_space = truncated.rfind(" ")
    if last_space > 0 and last_space > len(truncated) * 0.7:
        truncated = truncated[:last_space]
    return truncated.rstrip(",;:.!?-— ") + "…"


def list_messages(months: int = 1, *, viewer: Optional[str] = None) -> list[dict[str, Any]]:
    """Return parsed messages from the most-recent N months (default: current month only).

    If `viewer` is provided, attaches `reactions` field to each message (reaction-state replayed
    from the JSONL log; PRIVATE_KINDS filtered for non-actor viewers).

    Phase 2 (TB tr_13e95bdf15 2026-05-03): when `george_view` is missing on a message, attach
    a heuristic-generated one via `_auto_george_view()`. Explicit author-provided george_view
    always wins. The auto-generated value is marked via `george_view_auto: True` so the
    dashboard can optionally render it with weaker styling than author-provided.
    """
    ROOM_DIR.mkdir(parents=True, exist_ok=True)
    months = max(1, months)
    files = sorted(ROOM_DIR.glob("*.md"))[-months:]
    out: list[dict[str, Any]] = []
    for f in files:
        try: text = f.read_text(encoding="utf-8")
        except OSError: continue
        out.extend(_parse_file(text))
    # Phase 2 auto-fill: heuristic george_view when author didn't provide one
    for m in out:
        if m.get("kind") == "message" and not m.get("george_view") and m.get("body"):
            auto = _auto_george_view(m["body"])
            if auto:
                m["george_view"] = auto
                m["george_view_auto"] = True
    if viewer is not None:
        mids = [m["message_id"] for m in out if m.get("message_id")]
        rxn_state = get_reactions_for_messages(mids, viewer=viewer)
        for m in out:
            mid = m.get("message_id")
            if mid:
                m["reactions"] = rxn_state.get(mid, [])
    return out


def init_team_room() -> None:
    """Ensure the room directory + current month file + symlink exist."""
    current_file()


# ─── Reactions (v0 — append-only JSONL event log; state replayed) ────────────

import json as _json


def _reactions_file() -> Path:
    """Path of reactions log for current month."""
    ROOM_DIR.mkdir(parents=True, exist_ok=True)
    return ROOM_DIR / f"reactions_{_month_key()}.jsonl"


def _load_reactions_state(months: int = 1) -> dict[str, list[dict[str, Any]]]:
    """Replay reaction events; return {message_id: [{actor, kind, ts}, ...]} of currently-active reactions.

    Active = (actor, message_id, kind) tuple has more *add* events than *remove* events (idempotent).
    """
    ROOM_DIR.mkdir(parents=True, exist_ok=True)
    months = max(1, months)
    files = sorted(ROOM_DIR.glob("reactions_*.jsonl"))[-months:]
    # Track add-count per (actor, message_id, kind)
    counts: dict[tuple[str, str, str], int] = {}
    last_ts: dict[tuple[str, str, str], str] = {}
    for f in files:
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                key = (ev.get("actor", ""), ev.get("message_id", ""), ev.get("kind", ""))
                action = ev.get("action", "add")
                if action == "add":
                    counts[key] = counts.get(key, 0) + 1
                elif action == "remove":
                    counts[key] = counts.get(key, 0) - 1
                last_ts[key] = ev.get("ts", "")
        except OSError:
            continue
    out: dict[str, list[dict[str, Any]]] = {}
    for (actor, mid, kind), n in counts.items():
        if n <= 0:
            continue
        out.setdefault(mid, []).append({"actor": actor, "kind": kind, "ts": last_ts.get((actor, mid, kind), "")})
    return out


def add_reaction(*, message_id: str, kind: str, actor: str) -> dict[str, Any]:
    """Append an *add* reaction event to the log. Idempotent — replay collapses duplicates.

    Accepts message_ids from team_room (tr_*), projects (pp_*), or DMs (m_*) — single shared
    reactions log; surface-disambiguation happens via message_id prefix on read.
    """
    if not message_id or not re.match(r"^(tr|pp|mm?|desk)_[a-z0-9_]{2,60}$", message_id):
        raise ValueError("message_id must be a tr_*, pp_*, mm_*, m_*, or desk_* identifier")
    if kind not in REACTION_KINDS:
        raise ValueError(f"kind must be one of {sorted(REACTION_KINDS)}")
    if not actor:
        raise ValueError("actor required")
    ts = datetime.now().isoformat(timespec="seconds")
    ev = {"ts": ts, "actor": actor, "message_id": message_id, "kind": kind, "action": "add"}
    with _reactions_lock:
        with _reactions_file().open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps(ev) + "\n")
    emit_event(
        source="team_room", kind="team_room.reaction.added", actor=actor, target="team_room",
        payload={"message_id": message_id, "reaction_kind": kind},
    )
    return {"ok": True, "event": ev}


def remove_reaction(*, message_id: str, kind: str, actor: str) -> dict[str, Any]:
    """Append a *remove* reaction event (toggle-off). Idempotent."""
    if not message_id or not re.match(r"^(tr|pp|mm?|desk)_[a-z0-9_]{2,60}$", message_id):
        raise ValueError("message_id must be a tr_*, pp_*, mm_*, m_*, or desk_* identifier")
    if kind not in REACTION_KINDS:
        raise ValueError(f"kind must be one of {sorted(REACTION_KINDS)}")
    if not actor:
        raise ValueError("actor required")
    ts = datetime.now().isoformat(timespec="seconds")
    ev = {"ts": ts, "actor": actor, "message_id": message_id, "kind": kind, "action": "remove"}
    with _reactions_lock:
        with _reactions_file().open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps(ev) + "\n")
    emit_event(
        source="team_room", kind="team_room.reaction.removed", actor=actor, target="team_room",
        payload={"message_id": message_id, "reaction_kind": kind},
    )
    return {"ok": True, "event": ev}


def get_reactions_for_messages(message_ids: list[str], *, viewer: Optional[str] = None) -> dict[str, list[dict[str, Any]]]:
    """Return active reactions for the given message_ids.

    If `viewer` is provided, filters PRIVATE_KINDS (e.g., bookmark) to only show reactions
    where viewer == actor. All other kinds are visible to all viewers.
    """
    state = _load_reactions_state()
    out: dict[str, list[dict[str, Any]]] = {}
    for mid in message_ids:
        rxns = state.get(mid, [])
        if viewer is not None:
            rxns = [r for r in rxns if r["kind"] not in PRIVATE_KINDS or r["actor"] == viewer]
        if rxns:
            out[mid] = rxns
    return out
