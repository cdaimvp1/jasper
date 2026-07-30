"""server_lean.py — Symphony lean product spaceship (new-minimal · TB 2026-07-14).

The SYMPHONY PRODUCT server a new user installs. Registers ONLY the Symphony keep-routes
(Team Room + Projects + core plumbing + the multi-cohort dashboard) and REUSES the existing
tested logic modules — the Cockpit + ~110 dev/instrumentation routes are removed.

Per George (pp_497a92e6ab): a simple dashboard to monitor cohorts + comms; nav = Team Room +
Projects (Docs/Settings later). Multi-cohort per pp_0b2af44b5d. Product-lean; our live :8675
dev server stays full/separate.

DESIGN NOTES (cohort-converged 2026-07-14, proj_c8f384b8f5):
- Mention resolution is MEMBERS-DRIVEN, not hardcoded. The full server bakes cohort slugs into
  _ALL_COHORT / _MENTION_SHORT_TO_SLUG / _MENTION_GROUPS — a born cohort body would fan @all to
  phantom cross-cohort workers ("born-body-cohort-scoping" class, N=3: substrate_watcher F9 roster,
  server mention-maps, members). Cure: resolve @-tags from members.list_members() (config/members.json,
  seeded per-cohort from cohort_roster.yaml at install). Cohort-scoped by construction.
- Notification fanout (F9) runs IN-SERVER here (the product doesn't run the full scheduler/watcher
  suite). It's members-driven too — same members.json single-source. worker_notifications is what
  every poller reads; without this the product's cohort comms are dead.

Import-closure of THIS file = the installer body-manifest (Abe). Every import must resolve inside the
product venv's 9 deps (fastapi·uvicorn·duckdb·watchdog·markupsafe·sniffio·jinja2·python-multipart·pyyaml;
pydantic ships transitively with fastapi) + stdlib + the local team modules it reuses.
"""
from __future__ import annotations

import os
import re
import json
import time
import hashlib
import sqlite3
import threading
import asyncio
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from markupsafe import Markup, escape
from datetime import datetime, timedelta, timezone

import config
import team_room
import projects
import members as members_mod
import inbox
import retention
import backup
import paths
from bus import init_bus, emit_event, query_events, latest_id, event_count

from watchers import start_watchers, stop_watchers

import workgraph_store as wg
import workgraph_classify
import workgraph_nba
import workgraph_recommend
import deep_links
import outlook_actions
import workgraph_signals
import rule_teaching
import personal_patterns
import workgraph_alerts
import workgraph_synthesis
import workgraph_projects
import workgraph_lessons
import workgraph_socrates
import workgraph_deadlines
import workgraph_signal_trends
import workgraph_aristotle
import workgraph_export
import workgraph_commitments
import workgraph_asks_decisions
import workgraph_key_facts
import health_check
import workgraph_suppliers
import workgraph_digest
import workgraph_party_review


PORT = int(os.environ.get("TEAM_PORT", "8700"))  # born-local default (Tia's live-review catch, 2026-07-23):
# this IS the lean PRODUCT server (see module docstring - "our live :8675 dev server stays full/separate"),
# so its own fallback contradicted its own stated design intent. Every real Symphony install always sets
# TEAM_PORT explicitly to 8700 anyway, so this only ever mattered as a defensive fallback - but 8675 is
# the wrong fallback for a product server (risks colliding with/pointing at the live dev instance if
# TEAM_PORT is ever unset). 8700 is the correct born-local default, matching every other install-side reference.
HERE = Path(__file__).parent

import sys as _sys
_sys.path.insert(0, str(HERE / "ingest"))
import outlook_com_ingest  # cockpit "Refresh" route - synchronous mail re-ingest


# ── serve-boundary surrogate scrub (kept from full server — lone-surrogate guard,
#    AB 2026-06-18: an unpaired UTF-16 surrogate in served content 400s the worker's
#    CC request and loops it with no self-heal).
_LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def strip_lone_surrogates(s):
    if not isinstance(s, str) or not s:
        return s
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return _LONE_SURROGATE_RE.sub("�", s)


def sanitize_surrogates(o):
    """Recursively scrub str values inside dicts/lists (e.g. notification items)."""
    if isinstance(o, str):
        return strip_lone_surrogates(o)
    if isinstance(o, list):
        return [sanitize_surrogates(x) for x in o]
    if isinstance(o, dict):
        return {k: sanitize_surrogates(v) for k, v in o.items()}
    return o


# Universal serve-side backstop (TB · from full server): a JSONResponse subclass that
# scrubs EVERY outgoing payload once, at the single render() chokepoint. Rebinding the
# module name means every JSONResponse(...) call site is covered — no endpoint can leak
# a lone surrogate into a polling worker's context.
class SafeJSONResponse(JSONResponse):
    def render(self, content):
        return super().render(sanitize_surrogates(content))


JSONResponse = SafeJSONResponse  # all JSONResponse(...) below scrub on render

app = FastAPI(title="Symphony", docs_url=None, redoc_url=None, openapi_url=None,
              default_response_class=SafeJSONResponse)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))


# ── jinja helpers the kept templates (base/team_room/projects) require. All stdlib +
#    markupsafe (in the product venv's 9 deps) — no markdown lib (those live in stripped routes).
def md_inline(text: str) -> Markup:
    """Minimal inline markdown → HTML: `code`, **bold**, *italic*. Escapes first (XSS-safe)."""
    if not text:
        return Markup("")
    s = str(escape(text))
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)
    return Markup(s)


def fmt_age(seconds) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def static_version(filename: str) -> str:
    """Cache-buster: static file mtime as a version string."""
    p = HERE / "static" / filename
    try:
        return str(int(p.stat().st_mtime))
    except OSError:
        return "0"


templates.env.filters["md_inline"] = md_inline
templates.env.filters["fmt_age"] = fmt_age
templates.env.globals["static_version"] = static_version


# ═══════════════════════════════════════════════════════════════════════════
# Members-driven mention resolution (generic · per-cohort · no hardcoded slugs)
# ═══════════════════════════════════════════════════════════════════════════
# Replaces the full server's hardcoded _ALL_COHORT / _MENTION_SHORT_TO_SLUG /
# _MENTION_GROUPS. Resolves @-tags against the INSTALLED cohort's members
# (config/members.json, shape {id, name, archetype} — seeded per-cohort from
# cohort_roster.yaml at install). Cohort-scoped BY CONSTRUCTION: a born ir_cohort
# body resolves @all to ONLY {quinn,atlas,mira}, zero cross-cohort phantoms.
_MENTION_TOKEN_RE = re.compile(r"@([\w-]+)")


def _mention_index() -> dict:
    """Build {tag -> member_id} from the live members list every call (cheap; mtime-cached
    upstream). Tags = the member id (slug) + the display name + its first token, lowercased.
    The human manager is resolvable via config.manager (not a roster worker → not in members)."""
    idx: dict = {}
    for m in members_mod.list_members():
        mid = (m.get("id") or "").strip()
        if not mid:
            continue
        idx[mid.lower()] = mid
        # Index BOTH display_name (the shown/renamed name — PRIMARY, so @Tia resolves after a rename)
        # and name (archetype/legacy — so old references keep working). display_name iterated LAST so it
        # wins the full-name key on any collision. (Bug fix: was `name or display_name` → a rename never
        # entered the @-index because the stale archetype `name` won; every other surface prefers display_name.)
        for nm in (m.get("name"), m.get("display_name")):
            nm = (nm or "").strip()
            if nm:
                idx[nm.lower()] = mid
                idx.setdefault(nm.split()[0].lower(), mid)
    mgr = (config.get("manager", "id") or "manager")
    idx.setdefault(mgr.lower(), mgr)
    return idx


def _all_member_ids(exclude=()) -> list:
    out = []
    for m in members_mod.list_members():
        mid = (m.get("id") or "").strip()
        if mid and mid not in exclude:
            out.append(mid)
    return out


def _resolve_mention_targets(text: str, sender: Optional[str] = None) -> set:
    """@-tags in `text` → set of member ids. @all/@everyone → all members. Excludes sender."""
    if not text:
        return set()
    idx = _mention_index()
    targets: set = set()
    for m in _MENTION_TOKEN_RE.finditer(text):
        raw = m.group(1).lower()
        tag = raw.replace("-", "_")
        if tag in ("all", "everyone"):
            targets.update(_all_member_ids())
            continue
        slug = idx.get(tag) or idx.get(raw)
        if slug:
            targets.add(slug)
    if sender:
        targets.discard(sender)
    return targets


def _parse_audience(to_field: Optional[str]) -> tuple:
    """(specific_recipients, is_broadcast) from the `to` field ONLY (not body — scar
    tr_19a5a71266: scanning the body for @all silently promoted DMs to TR)."""
    is_broadcast = False
    recipients: set = set()
    idx = _mention_index()
    for m in _MENTION_TOKEN_RE.finditer(to_field or ""):
        raw = m.group(1).lower()
        tag = raw.replace("-", "_")
        if tag in ("all", "everyone"):
            is_broadcast = True
            continue
        slug = idx.get(tag) or idx.get(raw)
        if slug:
            recipients.add(slug)
    return recipients, is_broadcast


def _resolve_project_from_reply(reply_to: Optional[str]) -> Optional[str]:
    """If reply_to is pp_*, find the project it belongs to. Raises on lookup error
    (never masks a real error as not-found — the reverse-routing scar)."""
    if not reply_to or not reply_to.startswith("pp_"):
        return None
    try:
        for p in (projects.list_projects() or []):
            pid = p.get("id") or p.get("project_id")
            if not pid:
                continue
            full = projects.get_project(pid) or {}
            for post in full.get("posts", []) or []:
                if post.get("message_id") == reply_to:
                    return pid
    except Exception as e:
        raise RuntimeError(f"PP_LOOKUP_ERROR · resolving project for {reply_to} raised "
                           f"{type(e).__name__}: {e}") from e
    return None


# ═══════════════════════════════════════════════════════════════════════════
# In-server F9 notification fanout (members-driven)
# ═══════════════════════════════════════════════════════════════════════════
# Populates worker_notifications (what every poller reads) from bus events. The full
# server does this via scheduler→substrate_watcher.tick_function_9, whose roster is
# hardcoded (the original born-body-cohort-scoping instance). This is the lean,
# members-driven equivalent: recipients resolve from members.list_members(), so a born
# cohort body fans out to ONLY its own workers. Idempotent via UNIQUE(recipient,event_id,kind).
from paths import BUS_DB, CONFIG_DIR  # local module · env-driven bus.db + config paths
from paths import DOCUMENTS_DIR, DOCUMENTS_ISSUES_DIR, DOCUMENTS_PROJECTS_DIR, DOCUMENTS_CHAT_DIR, DOCUMENTS_RAW_ITEMS_DIR

_F9_INTERVAL_S = float(os.environ.get("TEAM_F9_INTERVAL_S", "2.0"))
_f9_thread: Optional[threading.Thread] = None
_f9_stop = threading.Event()
_F9_CURSOR_KEY = "lean_f9_cursor"


def _f9_cursor_load(conn) -> int:
    try:
        row = conn.execute("SELECT value FROM substrate_state WHERE key=?", (_F9_CURSOR_KEY,)).fetchone()
        if row and row[0] is not None:
            return int(row[0])
    except Exception:
        pass
    return -1


def _f9_cursor_save(conn, cursor: int) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS substrate_state (key TEXT PRIMARY KEY, value TEXT, ts REAL)")
    conn.execute(
        "INSERT INTO substrate_state(key, value, ts) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
        (_F9_CURSOR_KEY, str(cursor), time.time()),
    )


def _f9_targets_for_event(kind: str, actor, payload: dict) -> list:
    """Return [(recipient, notif_kind, summary), ...] for one bus event — members-driven.

    Handles the two fanout classes the product needs:
      inbox.sent       → per-recipient DM (covers direct DMs, project-member fan-out, FYI-mentions)
      team_room.message→ @-mention / @all fan-out to cohort members (tr_mention)
    """
    members = {m.get("id") for m in members_mod.list_members() if m.get("id")}

    if kind == "inbox.sent":
        recipient = payload.get("recipient")
        # F9-belt (2026-07-17 CFIB case-fix): resolve recipient to its canonical case-preserved
        # id before this case-sensitive membership check. Tia's fix canonicalizes at the known
        # producers (project fan-out); this self-defends the CONSUMER so ANY future emitter that
        # forgets to canonicalize can't silently drop a DM again (closes the root-cause class here).
        if projects.canonical_member_id(recipient) not in members:
            return []
        sender = payload.get("sender") or actor or "?"
        if sender == recipient:  # never notify a worker of their own DM
            return []
        body = str(payload.get("body") or payload.get("body_preview") or "")[:2000]
        if payload.get("f10_test"):
            return [(recipient, "comms_test", f"{sender}: {body[:160]}")]
        return [(recipient, "dm", f"{sender}: {body}")]

    if kind == "team_room.message":
        body = str(payload.get("body_preview") or payload.get("body") or "")
        sender = payload.get("sender") or actor or "?"
        targets = _resolve_mention_targets(body, sender=sender)
        if not targets:
            return []
        msg_id = payload.get("message_id", "")
        summary = f"{sender} [{msg_id}]: {body[:140]}"
        return [(w, "tr_mention", summary) for w in targets]

    return []


def _f9_tick() -> int:
    """Scan bus events since cursor, fan out to worker_notifications. Returns rows written."""
    fired = 0
    conn = sqlite3.connect(str(BUS_DB), timeout=5)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS substrate_state (key TEXT PRIMARY KEY, value TEXT, ts REAL)")
        cursor = _f9_cursor_load(conn)
        if cursor < 0:
            # First tick: skip historical backlog, start at the current tip.
            row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
            cursor = int(row[0]) if row else 0
            _f9_cursor_save(conn, cursor)
            conn.commit()
            return 0
        rows = conn.execute(
            "SELECT id, ts, source, kind, actor, target, payload FROM events "
            "WHERE id > ? ORDER BY id ASC LIMIT 500", (cursor,)
        ).fetchall()
        max_seen = cursor
        for eid, ts, source, kind, actor, target, payload_json in rows:
            max_seen = max(max_seen, eid)
            try:
                payload = json.loads(payload_json) if payload_json else {}
            except Exception:
                payload = {}
            for recipient, notif_kind, summary in _f9_targets_for_event(kind, actor, payload or {}):
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO worker_notifications "
                        "(ts, recipient, kind, source, summary, event_id) VALUES (?, ?, ?, ?, ?, ?)",
                        (time.time(), recipient, notif_kind, source or "bus",
                         strip_lone_surrogates(summary), eid),
                    )
                    if conn.total_changes:
                        fired += 1
                except Exception:
                    pass
        if max_seen != cursor:
            _f9_cursor_save(conn, max_seen)
        conn.commit()
    finally:
        conn.close()
    return fired


def _f9_loop() -> None:
    while not _f9_stop.is_set():
        try:
            _f9_tick()
        except Exception as e:
            try:
                emit_event(source="lean_f9", kind="watcher.error", payload={"error": str(e)})
            except Exception:
                pass
        _f9_stop.wait(_F9_INTERVAL_S)


def start_f9_fanout() -> None:
    global _f9_thread
    if _f9_thread is not None and _f9_thread.is_alive():
        return
    _f9_stop.clear()
    t = threading.Thread(target=_f9_loop, name="lean-f9-fanout", daemon=True)
    t.start()
    _f9_thread = t


def stop_f9_fanout() -> None:
    _f9_stop.set()


# ═══════════════════════════════════════════════════════════════════════════
# Lifecycle
# ═══════════════════════════════════════════════════════════════════════════
@app.on_event("startup")
async def _startup():
    init_bus()
    team_room.init_team_room()
    projects.init_projects()
    wg.init_workgraph()
    start_watchers()
    start_f9_fanout()
    emit_event(source="server", kind="server.started",
               payload={"port": PORT, "flavor": "symphony-lean"})


@app.on_event("shutdown")
async def _shutdown():
    stop_f9_fanout()
    stop_watchers()
    emit_event(source="server", kind="server.stopped", payload={})


# ═══════════════════════════════════════════════════════════════════════════
# Core plumbing
# ═══════════════════════════════════════════════════════════════════════════
@app.get("/healthz")
async def healthz():
    return {"ok": True, "flavor": "symphony-lean"}


@app.get("/api/members")
async def api_members():
    return JSONResponse(members_mod.list_members())


@app.get("/api/notifications/{worker}")
async def api_worker_notifications(worker: str, since_id: int = 0, limit: int = 200):
    """Notifications for `worker`, id > since_id, oldest first. Records the worker
    poll-heartbeat in substrate_state (alive-signal); the F9 fanout populates the stream."""
    limit = min(max(limit, 1), 500)
    try:
        con = sqlite3.connect(str(BUS_DB), timeout=3)
        con.row_factory = sqlite3.Row
        try:
            con.execute("CREATE TABLE IF NOT EXISTS substrate_state (key TEXT PRIMARY KEY, value TEXT, ts REAL)")
            con.execute(
                "INSERT INTO substrate_state(key, value, ts) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
                (f"f9_poll:{worker}", str(time.time()), time.time()),
            )
            con.commit()
        except Exception:
            pass  # heartbeat best-effort, never fail the poll
        rows = con.execute(
            "SELECT id, ts, recipient, kind, source, summary, event_id FROM worker_notifications "
            "WHERE recipient = ? AND id > ? ORDER BY id ASC LIMIT ?",
            (worker, since_id, limit),
        ).fetchall()
        items = [sanitize_surrogates(dict(r)) for r in rows]
        max_id_row = con.execute(
            "SELECT MAX(id) FROM worker_notifications WHERE recipient = ?", (worker,)
        ).fetchone()
        latest = max_id_row[0] if max_id_row and max_id_row[0] is not None else since_id
        con.close()
        cursor = items[-1]["id"] if items else since_id
        return JSONResponse({"notifications": items, "cursor": cursor, "latest_id": latest, "worker": worker})
    except Exception as e:
        return JSONResponse({"error": str(e), "notifications": [], "cursor": since_id}, status_code=500)


@app.get("/api/events")
async def api_events(since_id: Optional[int] = None, kind_prefix: Optional[str] = None,
                     actor: Optional[str] = None, target: Optional[str] = None, limit: int = 100):
    rows = query_events(since_id=since_id, kind_prefix=kind_prefix, actor=actor,
                        target=target, limit=min(max(limit, 1), 500))
    rows = sanitize_surrogates(rows)
    return JSONResponse({"events": rows, "latest_id": latest_id(), "count": event_count()})


# ═══════════════════════════════════════════════════════════════════════════
# Request models
# ═══════════════════════════════════════════════════════════════════════════
class TeamRoomMessageBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    body: str
    sender: Optional[str] = Field(None, alias="from")
    george_view: Optional[str] = None


class TeamRoomEditBody(BaseModel):
    body: str
    actor: Optional[str] = None


class ReactionBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    message_id: str
    kind: str
    actor: Optional[str] = Field(None, alias="from")


class ProjectPostBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    body: str
    sender: Optional[str] = Field(None, alias="from")
    george_view: Optional[str] = None


class ProjectPostEditBody(BaseModel):
    body: str
    actor: Optional[str] = None


class ProjectPostDeleteBody(BaseModel):
    actor: Optional[str] = None


class ProjectCreateBody(BaseModel):
    title: str
    members: list[str]
    creator: Optional[str] = None
    status: Optional[str] = "active"
    source_message_id: Optional[str] = None


class UnifiedPostBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    sender: Optional[str] = Field(None, alias="from")
    to: Optional[str] = None
    body: str
    reply_to: Optional[str] = None
    project: Optional[str] = None
    george_view: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════
# Team Room
# ═══════════════════════════════════════════════════════════════════════════
@app.get("/team_room", response_class=HTMLResponse)
async def team_room_page(request: Request):
    # NEW starlette signature: (request, name, context) — the product venv's starlette is
    # newer than dev, where the legacy ("name", {"request": ...}) form raises
    # "unhashable type: dict". Caught by the against-product-venv boot-test (TB 2026-07-14).
    return templates.TemplateResponse(request, "team_room.html", {
        "active_page": "team_room",
        "manager_id": config.get("manager", "id") or "manager",
        "manager_tag": config.get("manager", "tag") or config.get("manager", "id") or "manager",
        "members": members_mod.list_members(),
        "now_iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.get("/api/team_room")
async def api_team_room_list(months: int = 1, viewer: Optional[str] = None, limit: Optional[int] = None):
    """Parsed TR messages. If `viewer` set, reactions are attached per message."""
    msgs = team_room.list_messages(months=months, viewer=viewer)
    if limit is not None and limit > 0:
        msgs = msgs[-limit:]
    return JSONResponse({"messages": msgs})


@app.post("/api/team_room/messages")
async def api_team_room_post(body: TeamRoomMessageBody):
    sender = body.sender
    if not sender or not str(sender).strip():
        raise HTTPException(400, "sender (or 'from') field required for team_room posts")
    try:
        # post_message emits the team_room.message bus event; the F9 fanout notifies
        # @-mentioned members (members-driven) — no inline endpoint fan-out needed.
        result = team_room.post_message(sender=sender, body=body.body, george_view=body.george_view)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse(result)


@app.patch("/api/team_room/messages/{message_id}")
async def api_team_room_edit(message_id: str, body: TeamRoomEditBody):
    actor = body.actor or (config.get("manager", "id") or "manager")
    try:
        return JSONResponse(team_room.edit_message(message_id=message_id, new_body=body.body, actor=actor))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/team_room/messages/{message_id}")
async def api_team_room_delete(message_id: str, actor: Optional[str] = None):
    actor = actor or (config.get("manager", "id") or "manager")
    try:
        return JSONResponse(team_room.delete_message(message_id=message_id, actor=actor))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/team_room/reactions")
async def api_team_room_reaction_add(body: ReactionBody):
    actor = body.actor
    if not actor:
        raise HTTPException(400, "actor (or 'from') required — no silent default-to-manager")
    try:
        result = team_room.add_reaction(message_id=body.message_id, kind=body.kind, actor=actor)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Notify the message author (if not self) for the notify-class reaction kinds.
    # Tia's audit (2026-07-23): this list is independently maintained from team_room.py's
    # REACTION_KINDS (13 total) and NOT every kind is meant to be here — "ack"/"thinking" are
    # deliberately low-signal ("saw it, no reply needed") and excluded on purpose (George
    # confirmed 2026-07-23, no change wanted); "bookmark" is excluded because it's PRIVATE_KINDS
    # (self-only, nothing to notify). If a new reaction kind is ever added to REACTION_KINDS,
    # decide explicitly whether it belongs here too — don't assume silence means "add it."
    NOTIFY_KINDS = {"approved", "project", "meeting", "disagree", "celebrate",
                    "thank_you", "model", "more_info", "capability", "anchor"}
    KIND_LABELS = {
        "approved": "✅ Approved", "disagree": "❌ Disagree", "celebrate": "\U0001f389 Celebrate",
        "thank_you": "\U0001f64f Thank you", "model": "\U0001f31f Model this behavior",
        "more_info": "\U0001f914 More info please", "capability": "\U0001f6e0 Capability-honest",
        "anchor": "\U0001f4cd Anchor this", "project": "\U0001f680 Start Project", "meeting": "\U0001f5d3 Meeting",
    }
    if body.kind in NOTIFY_KINDS:
        try:
            msg_author = None
            if body.message_id.startswith("pp_"):
                hit = projects.find_post_by_message_id(body.message_id)
                if hit:
                    msg_author = hit.get("from")
                surface = "project post"
            else:
                surface = "room post"
                for m in team_room.list_messages(months=2):
                    if m.get("message_id") == body.message_id:
                        msg_author = m.get("from")
                        break
            if msg_author and msg_author != actor:
                inbox.send_message(
                    sender=actor, recipient=msg_author,
                    body=f"\U0001f4ac {KIND_LABELS.get(body.kind, body.kind)} reaction on your "
                         f"{surface} `{body.message_id}` by @{actor}.")
        except Exception:
            pass  # routing best-effort; never fail the reaction
    return JSONResponse(result)


@app.delete("/api/team_room/reactions")
async def api_team_room_reaction_remove(body: ReactionBody):
    actor = body.actor
    if not actor:
        raise HTTPException(400, "actor (or 'from') required — no silent default-to-manager")
    try:
        return JSONResponse(team_room.remove_reaction(message_id=body.message_id, kind=body.kind, actor=actor))
    except ValueError as e:
        raise HTTPException(400, str(e))


# ═══════════════════════════════════════════════════════════════════════════
# Projects
# ═══════════════════════════════════════════════════════════════════════════
@app.get("/projects", response_class=HTMLResponse)
async def projects_page(request: Request):
    return templates.TemplateResponse(request, "projects.html", {
        "active_page": "projects",
        "manager_id": config.get("manager", "id") or "manager",
        "manager_tag": config.get("manager", "tag") or config.get("manager", "id") or "manager",
        "members": [{"id": m["id"], "name": m["name"], "display_name": m.get("display_name", "")}
                    for m in members_mod.list_members()],
    })


@app.get("/api/projects")
async def api_projects_list_legacy():
    return JSONResponse({"projects": projects.list_projects()})


@app.get("/api/projects/archived")
async def api_projects_archived():
    """Listed BEFORE the parameterized /{project_id} route so 'archived' isn't matched as an id."""
    return JSONResponse({"projects": projects.list_archived()})


@app.get("/api/projects/search")
async def api_projects_search(q: str = "", limit: int = 50, include_archived: bool = False):
    hits = projects.search_posts(q, limit=limit, include_archived=include_archived)
    return JSONResponse({"query": q, "hits": hits, "count": len(hits)})


@app.get("/api/projects/{project_id}")
async def api_project_get(project_id: str, viewer: Optional[str] = None, limit: int = 100):
    """Project + posts. Default limit=100 most-recent posts (payload-size guard); limit=0 = full."""
    p = projects.get_project(project_id, viewer=viewer)
    if not p:
        raise HTTPException(404, f"project not found: {project_id}")
    posts_all = p.get("posts") or []
    total = len(posts_all)
    if limit and limit > 0 and total > limit:
        p["posts"] = posts_all[-limit:]
        p["posts_truncated"] = True
    else:
        p["posts_truncated"] = False
    p["posts_total"] = total
    p["posts_returned"] = len(p.get("posts") or [])
    return JSONResponse(p)


class ProjectStatusBody(BaseModel):
    status: str
    actor: Optional[str] = None


@app.post("/api/projects/{project_id}/status")
async def api_project_status(project_id: str, body: ProjectStatusBody):
    """George's catch 2026-07-23: this endpoint (backing the ⋯ menu's Pause/Resume/Mark
    Done/Archive) existed in server.py but was never ported to server_lean.py — the 3-dot
    menu 404'd silently against the shipped Symphony server. Ported verbatim (same body,
    same projects.py function both servers already share)."""
    actor = body.actor or (config.get("manager", "id") or "george")
    try:
        return JSONResponse(projects.update_status(project_id=project_id, new_status=body.status, actor=actor))
    except ValueError as e:
        raise HTTPException(400, str(e))


class ProjectRenameBody(BaseModel):
    title: str
    actor: Optional[str] = None


@app.patch("/api/projects/{project_id}")
async def api_project_rename(project_id: str, body: ProjectRenameBody):
    """Ported from server.py, same missing-in-lean class as the status endpoint above."""
    actor = body.actor or (config.get("manager", "id") or "george")
    try:
        return JSONResponse(projects.rename_project(project_id=project_id, new_title=body.title, actor=actor))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/projects/{project_id}")
async def api_project_delete(project_id: str, actor: Optional[str] = None):
    """Ported from server.py, same missing-in-lean class as the status endpoint above."""
    actor = actor or (config.get("manager", "id") or "george")
    try:
        return JSONResponse(projects.delete_project(project_id=project_id, actor=actor))
    except ValueError as e:
        raise HTTPException(400, str(e))


class ProjectMembersBody(BaseModel):
    members: list[str]
    actor: Optional[str] = None


@app.post("/api/projects/{project_id}/add_members")
async def api_project_add_members(project_id: str, body: ProjectMembersBody):
    """George's catch 2026-07-23: the project-chat add/remove-worker buttons 404'd silently —
    same missing-in-lean class as status/rename/delete above. projects.add_members already
    existed with a matching payload shape; only the route was missing."""
    actor = body.actor or (config.get("manager", "id") or "george")
    try:
        return JSONResponse(projects.add_members(project_id=project_id, new_members=body.members, actor=actor))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/projects/{project_id}/remove_members")
async def api_project_remove_members(project_id: str, body: ProjectMembersBody):
    """Same missing-in-lean class as add_members above."""
    actor = body.actor or (config.get("manager", "id") or "george")
    try:
        return JSONResponse(projects.remove_members(project_id=project_id, members_to_remove=body.members, actor=actor))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/projects/{project_id}/posts")
async def api_project_post(project_id: str, body: ProjectPostBody):
    sender = body.sender
    if not sender or not str(sender).strip():
        raise HTTPException(400, "sender (or 'from') field required for project posts")
    # Block non-member posts (allowed: project members + manager + _substrate).
    # Case-insensitive: stored membership is lowercase-normalized (projects._normalize_member_id)
    # while `sender` arrives case-preserved (e.g. "Ledger") — compare via canonical resolution,
    # not raw string equality, or a mixed-case worker gets wrongly 403'd from its own project.
    try:
        proj_check = projects.get_project(project_id) or {}
        members_check = {projects.canonical_member_id(m) for m in (proj_check.get("members", []) or [])}
        mgr = config.get("manager", "id") or "manager"
        if members_check and sender not in members_check and sender != mgr and sender != "_substrate":
            raise HTTPException(403, f"@{sender} is not a member of project {project_id}")
    except HTTPException:
        raise
    except Exception:
        pass
    try:
        # post_to_project owns project-member inbox fan-out (single source of truth);
        # F9 turns those inbox.sent events into worker_notifications.
        result = projects.post_to_project(project_id=project_id, sender=sender, body=body.body,
                                          george_view=body.george_view)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # FYI-mention path: @-mentioned NON-members get a heads-up (they aren't auto-joined).
    # Same case-insensitive resolution as the membership gate above — `mention_targets` comes
    # back case-preserved (via _mention_index's canonical values) while stored membership is
    # lowercased, so compare through canonical_member_id or a real member gets a false
    # "you're not a member" FYI (2026-07-17 scar, CFIB Ledger/Quill).
    try:
        proj = projects.get_project(project_id)
        if proj:
            current_members = {projects.canonical_member_id(m) for m in proj.get("members", []) if m}
            mention_targets = _resolve_mention_targets(body.body or "", sender=sender)
            fyi = [w for w in mention_targets if w not in current_members]
            new_pp_id = result.get("message_id") if isinstance(result, dict) else None
            for w in fyi:
                try:
                    inbox.send_message(
                        sender=sender, recipient=w,
                        body=f"\U0001f44b FYI: you were @-mentioned by @{sender} in project "
                             f"*{proj.get('title','')}* (`{project_id}`) but you're not a member. "
                             f"Preview: {(body.body or '')[:400]}")
                except Exception:
                    pass
    except Exception:
        pass
    return JSONResponse(result)


@app.patch("/api/projects/{project_id}/posts/{message_id}")
async def api_project_post_edit(project_id: str, message_id: str, body: ProjectPostEditBody):
    actor = body.actor or (config.get("manager", "id") or "manager")
    try:
        return JSONResponse(projects.edit_post(project_id=project_id, message_id=message_id,
                                               new_body=body.body, actor=actor))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/projects/{project_id}/posts/{message_id}")
async def api_project_post_delete(project_id: str, message_id: str, body: Optional[ProjectPostDeleteBody] = None):
    actor = (body.actor if body else None) or (config.get("manager", "id") or "manager")
    try:
        return JSONResponse(projects.delete_post(project_id=project_id, message_id=message_id, actor=actor))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/projects")
async def api_project_create(body: ProjectCreateBody):
    creator = body.creator or (config.get("manager", "id") or "manager")
    try:
        return JSONResponse(projects.create_project(
            title=body.title, members=body.members, creator=creator,
            status=body.status or "active", source_message_id=body.source_message_id,
        ))
    except ValueError as e:
        raise HTTPException(400, str(e))


# ═══════════════════════════════════════════════════════════════════════════
# Declarative-routing post (workers declare audience+body · substrate routes)
# ═══════════════════════════════════════════════════════════════════════════
@app.post("/api/post")
async def api_unified_post(body: UnifiedPostBody):
    """Workers declare WHAT (audience + body); substrate decides WHERE (TR / project / DM)."""
    sender = body.sender
    if not sender or not str(sender).strip():
        raise HTTPException(400, "sender (or 'from') field required")
    if not (body.body or "").strip():
        raise HTTPException(400, "EMPTY_BODY · refusing a contentless post. Include a body, or if a "
                                 "backtick got shell-substituted to empty, re-send via --body-file.")
    text = body.body or ""
    recipients, is_broadcast = _parse_audience(body.to)

    try:
        project_id = body.project or _resolve_project_from_reply(body.reply_to)
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    if body.reply_to and body.reply_to.startswith("pp_") and not project_id:
        raise HTTPException(400, f"PP_UNRESOLVED · reply_to={body.reply_to!r} looks like a project post "
                                 f"but no project contains it. Pass an explicit project=, or check the id.")

    # reply-to inheritance: m_ → DM back to the original author (when `to` omitted)
    inferred_recipient = None
    if body.reply_to and not body.to and not body.project and not project_id:
        if body.reply_to.startswith("m_"):
            try:
                mgr = config.get("manager", "id") or "manager"
                for wid in _all_member_ids() + [mgr]:
                    for msg in inbox.list_messages(wid):
                        if msg.get("message_id") == body.reply_to:
                            orig = msg.get("from")
                            if orig and orig != sender:
                                inferred_recipient = orig
                                recipients = {orig}
                            break
                    if inferred_recipient:
                        break
            except Exception:
                pass

    if (body.to and str(body.to).strip() and not recipients and not is_broadcast
            and not project_id and not inferred_recipient):
        raise HTTPException(400, f"UNRESOLVED_AUDIENCE · to={body.to!r} matched no known recipient. "
                                 f"Check the slug, or use @all for a broadcast.")

    # Reply-room authority: a reply belongs in the room of the thing it answers.
    if project_id:
        destination = "project"
    elif body.reply_to and body.reply_to.startswith("tr_") and not is_broadcast and recipients:
        destination = "team_room"
    elif is_broadcast:
        destination = "team_room"
    elif recipients:
        destination = "dm"
    elif body.reply_to and body.reply_to.startswith("tr_"):
        destination = "team_room"
    else:
        destination = "team_room"

    if destination == "project":
        try:
            result = projects.post_to_project(project_id=project_id, sender=sender,
                                              body=text, george_view=body.george_view)
            mid = result.get("message_id") if isinstance(result, dict) else None
            return JSONResponse({"ok": True, "destination": "project", "project_id": project_id,
                                 "canonical_id": mid, "routed_via": "audience+project_context"})
        except ValueError as e:
            raise HTTPException(400, str(e))
    elif destination == "team_room":
        try:
            result = team_room.post_message(sender=sender, body=text, george_view=body.george_view)
            mid = result.get("message_id") if isinstance(result, dict) else None
            return JSONResponse({"ok": True, "destination": "team_room", "canonical_id": mid,
                                 "routed_via": "broadcast" if is_broadcast else "default"})
        except ValueError as e:
            raise HTTPException(400, str(e))
    else:  # dm
        last_mid = None
        for r in recipients:
            try:
                msg = inbox.send_message(sender=sender, recipient=r, body=text,
                                         in_reply_to=body.reply_to, george_view=body.george_view)
                last_mid = (msg or {}).get("message_id")
            except Exception:
                pass
        return JSONResponse({"ok": True, "destination": "dm", "recipients": sorted(recipients),
                             "canonical_id": last_mid, "routed_via": "specific_audience"})


# ═══════════════════════════════════════════════════════════════════════════
# Stubs for removed-feature calls the kept templates still make (200-empty so
# trimmed nav never 500s the page). Real features (meetings, cockpit) are gone.
# ═══════════════════════════════════════════════════════════════════════════
@app.get("/api/meetings")
async def api_meetings_stub(status: Optional[str] = None):
    return JSONResponse([])


@app.get("/api/cockpit/dm-summary")
async def api_cockpit_dm_summary_stub():
    return JSONResponse({"channels": []})


# ═══════════════════════════════════════════════════════════════════════════
# Multi-cohort dashboard data (Sage's data-contract v0.1) — REAL FEED (Abe, cure-A ii)
# ═══════════════════════════════════════════════════════════════════════════
# Was a hardcoded aria_canon+ir_cohort STUB → a BORN box's dashboard showed OUR dev cohorts,
# never its own (class instance #7 of "born body carries a live/DEV default"). Now derived
# from THIS body's own state: cohort_id + worker→archetype from config.roles
# (config/symphony_identity.json — the same file resolve_symphony_identity/L3/verify read),
# status from the substrate_state f9_poll heartbeat (born-local bus), display names from
# members.json. Born box → ONE cohort (its own); a live/dev body with no archetype config →
# members-derived single cohort (never the old baked pair). SHAPE = Sage's locked contract v0.1.
_CONFIG_PATH = CONFIG_DIR / "symphony_identity.json"  # env-driven (TEAM_CONFIG_DIR), consistent with members.py


def _worker_heartbeats() -> dict:
    """worker → last f9-poll unix-ts from substrate_state (born-local bus). Best-effort:
    no heartbeats yet (fresh cohort) → {} → workers report 'unknown', never an error."""
    hb = {}
    try:
        con = sqlite3.connect(str(BUS_DB), timeout=3)
        try:
            for key, ts in con.execute(
                    "SELECT key, ts FROM substrate_state WHERE key LIKE 'f9_poll:%'"):
                if ts is not None:
                    hb[str(key).split(":", 1)[1]] = float(ts)
        finally:
            con.close()
    except Exception:
        pass
    return hb


def _cohorts_feed():
    """Per-cohort dashboard data (Sage contract v0.1) derived from THIS body's own state —
    never a baked cohort list. A born box reports its one cohort; shape is the locked contract."""
    cohort_id, roles = None, {}
    try:
        if _CONFIG_PATH.is_file():
            _c = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            cohort_id = _c.get("cohort")
            roles = _c.get("roles") or {}
    except Exception:
        pass  # malformed/absent config → fall through to members-derived (below)
    dn = {m.get("id"): (m.get("display_name") or m.get("name") or m.get("id"))
          for m in members_mod.list_members() if m.get("id")}
    # worker set: config.roles (archetype-body) else members.json (agnostic/live body)
    if roles:
        worker_src = {w: (v.get("archetype", "") if isinstance(v, dict) else str(v))
                      for w, v in roles.items()}
    else:
        worker_src = {m["id"]: "" for m in members_mod.list_members() if m.get("id")}
    hb = _worker_heartbeats()
    now = time.time()
    workers = []
    for w, arch in worker_src.items():
        ts = hb.get(w)
        if ts is None:
            status, last = "unknown", None
        else:
            status = "active" if (now - ts) < 120 else "idle"
            last = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        workers.append({"worker": w, "display_name": dn.get(w, w), "archetype": arch,
                        "status": status, "last_activity": last})
    if not cohort_id:
        cohort_id = os.environ.get("SYMPHONY_COHORT") or os.environ.get("TEAM_COHORT") or "cohort"
    cohort_status = ("healthy" if any(x["status"] == "active" for x in workers)
                     else "degraded" if any(x["status"] == "idle" for x in workers)
                     else "offline")
    last_activity = max([x["last_activity"] for x in workers if x["last_activity"]], default=None)
    # display_name is the MUTABLE label (Settings #7); cohort_id stays the STABLE internal key.
    display_name = (config.get("cohort_display_name") or "").strip() or cohort_id
    return [{"cohort_id": cohort_id, "display_name": display_name, "cohort_status": cohort_status,
             "last_activity": last_activity, "workers": workers}]


@app.get("/api/cohorts")
async def api_cohorts():
    """Per-cohort dashboard data (Sage contract v0.1) — REAL feed, derived from this body's
    config.roles + members + born-bus heartbeats (no baked cohort list)."""
    return JSONResponse({"cohorts": _cohorts_feed(), "stub": False})


# ═══════════════════════════════════════════════════════════════════════════
# Cockpit — work graph + roster control (new, Symphony Cockpit build)
# ═══════════════════════════════════════════════════════════════════════════
@app.get("/cockpit", response_class=HTMLResponse)
async def cockpit_page(request: Request):
    return templates.TemplateResponse(request, "cockpit.html", {
        "active_page": "cockpit",
        "manager_tag": config.get("manager", "tag") or config.get("manager", "id") or "manager",
        "now_iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


class WorkgraphIssueStatusBody(BaseModel):
    state: Optional[str] = None
    priority: Optional[str] = None


class CockpitActionBody(BaseModel):
    issue_id: str
    action_kind: str  # draft_reply | review_contract | summarize | custom
    worker: str = "bridge"
    instructions: Optional[str] = None


class WorkerStatusBody(BaseModel):
    state: str  # working | idle | blocked
    current_task: Optional[str] = None
    detail: Optional[str] = None


@app.get("/api/workgraph/issues")
async def api_workgraph_issues(state: Optional[str] = None, limit: int = 200):
    """Issue list, server-sorted by priority_score DESC (no client resort needed).
    `state` is a comma-separated filter, e.g. 'active,waiting'; omitted = the
    three open states (active/waiting/blocked), never done/noise-archived."""
    states = [s.strip() for s in state.split(",")] if state else ["active", "waiting", "blocked"]
    issues = wg.list_issues(states=states, limit=min(max(limit, 1), 1000))
    workgraph_lessons.attach_learned(issues)
    workgraph_deadlines.attach_deadline_info(issues)
    return JSONResponse({"issues": sanitize_surrogates(issues)})


@app.get("/api/workgraph/issues/export.csv")
async def api_export_issues_csv(start: str, end: str, state: Optional[str] = None):
    """Task #68: CSV export of issues whose updated_at falls within
    [start, end] (both YYYY-MM-DD, UTC, inclusive on both ends - `end` is
    treated as through end-of-day). `state` is an optional comma-separated
    filter, same convention as GET /api/workgraph/issues; omitted means
    every state (workgraph_export.issues_csv's own "no filter" default).
    MUST be registered before /api/workgraph/issues/{issue_id} below - a
    path-param route registered first would otherwise greedily match
    "export.csv" as an issue_id (confirmed live: this 404'd with "no such
    issue: export.csv" until moved here)."""
    try:
        start_ts = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        end_ts = (datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                  + timedelta(days=1, seconds=-1)).timestamp()
    except ValueError:
        raise HTTPException(400, "start/end must be YYYY-MM-DD")
    if start_ts > end_ts:
        raise HTTPException(400, "start must be at or before end")
    states = [s.strip() for s in state.split(",")] if state else None
    csv_text = workgraph_export.issues_csv(start_ts, end_ts, states=states)
    filename = f"jasper-issues-{start}-to-{end}.csv"
    return Response(content=csv_text, media_type="text/csv",
                     headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/workgraph/issues/{issue_id}")
async def api_workgraph_issue_detail(issue_id: str):
    issue = wg.get_issue(issue_id)
    if issue is None:
        raise HTTPException(404, f"no such issue: {issue_id}")
    evidence = wg.list_evidence(issue_id)
    pending = wg.list_pending_actions(issue_id)
    tasks = wg.list_tasks(issue_id)
    state_history = wg.list_issue_state_history(issue_id)
    parties = wg.list_parties_for_issue(issue_id)
    project = wg.get_project(issue["project_id"]) if issue.get("project_id") else None
    synthesis = wg.get_synthesis("issue", issue_id)
    project_synthesis = wg.get_synthesis("project", issue["project_id"]) if issue.get("project_id") else None
    attachments = wg.list_attachments_for_issue(issue_id)
    # 2026-07-29: populate evidence[].recommendation + .attachment (see
    # workgraph_recommend.py) — the Detail pane's Progress column already
    # rendered these when present, but nothing ever produced them.
    workgraph_recommend.attach_recommendations(evidence, attachments, time.time())
    deep_links.attach_deep_links(evidence)
    personal_patterns.attach_citations(evidence)
    workgraph_lessons.attach_learned([issue])
    workgraph_deadlines.attach_deadline_info([issue])
    workgraph_suppliers.attach_supplier_precedent(issue)
    return JSONResponse({"issue": sanitize_surrogates(issue), "evidence": sanitize_surrogates(evidence),
                        "pending_actions": sanitize_surrogates(pending), "tasks": sanitize_surrogates(tasks),
                        "state_history": sanitize_surrogates(state_history),
                        "parties": sanitize_surrogates(parties), "project": sanitize_surrogates(project),
                        "synthesis": sanitize_surrogates(synthesis),
                        "project_synthesis": sanitize_surrogates(project_synthesis),
                        "attachments": sanitize_surrogates(attachments)})


class OpenEmailBody(BaseModel):
    raw_item_id: int


@app.post("/api/action/open-email")
async def api_action_open_email(body: OpenEmailBody):
    """Task #46 - opens the exact source email in a real Outlook window via
    COM's Display() (outlook_actions.py). Wrapped in asyncio.to_thread: this
    shells out to PowerShell and blocks for the whole COM round-trip, and
    this server runs a single uvicorn worker with no --workers flag - a
    blocking call directly in an async def handler freezes EVERY request,
    not just this one (the exact bug found and fixed for /api/cockpit/refresh
    in task #42 - not repeating it here)."""
    raw_item = wg.get_raw_item(body.raw_item_id)
    if raw_item is None:
        raise HTTPException(404, f"no such raw_item: {body.raw_item_id}")
    entry_id = raw_item.get("entry_id")
    if not entry_id:
        raise HTTPException(400, "this item has no stored EntryID (ingested before task #43, or not a mail item)")
    try:
        result = await asyncio.to_thread(outlook_actions.open_email, entry_id)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return JSONResponse(result)


class DraftReplyBody(BaseModel):
    raw_item_id: int
    reply_all: bool = False


@app.post("/api/action/draft-reply")
async def api_action_draft_reply(body: DraftReplyBody):
    """Task #47 - creates a REAL Outlook draft reply to the exact source
    email (outlook_actions.draft_reply: Reply()/ReplyAll() + Display(), never
    Send()). Same asyncio.to_thread guard as open-email above, same reason -
    this blocks for a full COM round-trip on a single-worker server."""
    raw_item = wg.get_raw_item(body.raw_item_id)
    if raw_item is None:
        raise HTTPException(404, f"no such raw_item: {body.raw_item_id}")
    entry_id = raw_item.get("entry_id")
    if not entry_id:
        raise HTTPException(400, "this item has no stored EntryID (ingested before task #43, or not a mail item)")
    try:
        result = await asyncio.to_thread(outlook_actions.draft_reply, entry_id, body.reply_all)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return JSONResponse(result)


class TaskCreateBody(BaseModel):
    label: str
    action: Optional[str] = None
    due: Optional[str] = None


class TaskStatusBody(BaseModel):
    state: str


@app.post("/api/workgraph/issues/{issue_id}/tasks")
async def api_workgraph_task_create(issue_id: str, body: TaskCreateBody):
    if wg.get_issue(issue_id) is None:
        raise HTTPException(404, f"no such issue: {issue_id}")
    task_id = wg.create_task(issue_id=issue_id, label=body.label, action=body.action, due=body.due)
    return JSONResponse({"ok": True, "task": sanitize_surrogates(wg.get_task(task_id))})


@app.post("/api/workgraph/tasks/{task_id}/status")
async def api_workgraph_task_status(task_id: str, body: TaskStatusBody):
    if wg.get_task(task_id) is None:
        raise HTTPException(404, f"no such task: {task_id}")
    wg.update_task(task_id, state=body.state)
    return JSONResponse({"ok": True, "task": sanitize_surrogates(wg.get_task(task_id))})


@app.post("/api/workgraph/issues/{issue_id}/status")
async def api_workgraph_issue_status(issue_id: str, body: WorkgraphIssueStatusBody):
    """Deterministic actions (mark done, snooze, reprioritize, archive) — applied
    immediately, no worker wake, per the cockpit's deterministic/generative action split."""
    if wg.get_issue(issue_id) is None:
        raise HTTPException(404, f"no such issue: {issue_id}")
    fields = {}
    if body.state is not None:
        fields["state"] = body.state
    if body.priority is not None:
        fields["priority"] = body.priority
    if not fields:
        raise HTTPException(400, "at least one of state/priority required")
    wg.update_issue(issue_id, **fields)
    return JSONResponse({"ok": True, "issue": sanitize_surrogates(wg.get_issue(issue_id))})


class BulkIssueStatusBody(BaseModel):
    issue_ids: list[str]
    state: str


@app.post("/api/workgraph/issues/bulk-status")
async def api_workgraph_issues_bulk_status(body: BulkIssueStatusBody):
    """Task #63 (bulk triage): the same deterministic state change
    pccRunDeterministic already applies one issue at a time (Mark done /
    Snooze / Archive as noise), applied to many issues from a single
    request - for clearing a pile of low-priority items from the Morning
    Queue list without opening each one individually. Restricted to the
    same 3 states the single-issue action buttons already use - this isn't
    a general-purpose bulk field editor, just bulk triage. Unknown issue
    ids are skipped and reported back in `missing`, never silently dropped
    or allowed to fail the whole batch."""
    if body.state not in ("done", "noise-archived", "waiting"):
        raise HTTPException(400, f"unsupported bulk triage state: {body.state!r}")
    if not body.issue_ids:
        raise HTTPException(400, "issue_ids must be a non-empty list")
    updated, missing = [], []
    for issue_id in body.issue_ids:
        if wg.get_issue(issue_id) is None:
            missing.append(issue_id)
            continue
        wg.update_issue(issue_id, state=body.state)
        updated.append(issue_id)
    return JSONResponse({"ok": True, "updated": updated, "missing": missing})


@app.get("/api/workgraph/gate-board")
async def api_gate_board():
    """Task #67 (Gate Board): a portfolio view of every Aristotle
    prerequisite rule - see workgraph_aristotle.gate_board's docstring for
    why "currently_gating" is computed live rather than a stored counter."""
    return JSONResponse(sanitize_surrogates(workgraph_aristotle.gate_board()))


@app.get("/api/workgraph/party-review-queue")
async def api_party_review_queue():
    """Task #79 (Party resolution confidence review queue). See
    workgraph_party_review.py's docstring for why this is scoped to
    "external, no identified company, not a system sender" rather than a
    literal confidence-tier filter - real data showed every external
    party is already at the highest confidence tier."""
    return JSONResponse({"parties": sanitize_surrogates(workgraph_party_review.list_parties_needing_review())})


@app.get("/api/workgraph/weekly-digest")
async def api_weekly_digest():
    """Task #76 (Weekly Digest). Zero LLM, zero new extraction - a
    7-day-scoped rollup of numbers this app already computes elsewhere
    (workgraph_nba's scoring/value, workgraph_deadlines' classification)."""
    return JSONResponse(sanitize_surrogates(workgraph_digest.build_digest(time.time())))


@app.get("/api/workgraph/signal-trends")
async def api_signal_trends(months: int = 6):
    """Task #66 (month-over-month signal trend view). Pure aggregation over
    raw_items.signal_type (workgraph_signals.classify_signal's output at
    ingest) - zero LLM, no interpretation, so unlike Deadline Radar's
    `mentioned` tier this data is safe to chart directly."""
    if months < 1 or months > 24:
        raise HTTPException(400, "months must be between 1 and 24")
    return JSONResponse(sanitize_surrogates(workgraph_signal_trends.monthly_signal_trends(time.time(), months)))


@app.get("/api/workgraph/value-at-risk")
async def api_value_at_risk():
    """Task #65 (Value-at-risk rollup banner). Reuses workgraph_nba's own
    deterministic, already-scored dollar-value extraction (the same figure
    each issue's own nba_reason cites, e.g. "$111.7M") - summed across
    every open issue. Same known failure mode as that per-issue signal: an
    unrelated large figure quoted in passing inflates the total. That's why
    the frontend must present this as "value found in open threads," not
    as a certain "at risk" claim."""
    return JSONResponse(sanitize_surrogates(workgraph_nba.value_at_risk_rollup()))


@app.get("/api/workgraph/commitments")
async def api_commitments():
    """Task #73 (Commitments Tracker). See workgraph_commitments.py's
    docstring for why this isn't scoped to "Marc's own" commitments -
    the underlying extraction doesn't attribute who made each one, and a
    keyword guess would repeat the Ariba expiration-date mistake."""
    return JSONResponse(sanitize_surrogates({"commitments": workgraph_commitments.list_open_commitments()}))


@app.get("/api/workgraph/asks-decisions")
async def api_asks_decisions():
    """Enhancement #2 (Asks & Decisions Tracker). Curator already extracts
    both fields on every wake (same pass as commitments/dates_mentioned) -
    grepping the whole codebase found neither ever displayed anywhere,
    only checked for truthiness in workgraph_synthesis.py's staleness
    logic. Zero new extraction - a plain reflect-only rollup."""
    return JSONResponse(sanitize_surrogates({
        "asks": workgraph_asks_decisions.list_open_asks(),
        "decisions": workgraph_asks_decisions.list_open_decisions(),
    }))


@app.get("/api/workgraph/key-facts")
async def api_key_facts():
    """Enhancement #2 (Key Facts panel). Same rationale as asks-decisions
    above - curator already extracts this on every wake, nothing anywhere
    reads it back."""
    return JSONResponse(sanitize_surrogates({"key_facts": workgraph_key_facts.list_open_key_facts()}))


_ALERT_SEVERITY_ORDER = {"critical": 0, "warn": 1, "info": 2}


@app.get("/api/alerts")
async def api_alerts_list():
    """Undismissed alerts, most-attention-worthy first: severity (critical >
    warn > info), then newest within a severity."""
    alerts = wg.list_alerts(dismissed=False)
    alerts.sort(key=lambda a: (_ALERT_SEVERITY_ORDER.get(a["severity"], 3), -a["created_ts"]))
    return JSONResponse({"alerts": sanitize_surrogates(alerts)})


@app.post("/api/alerts/{alert_id}/dismiss")
async def api_alert_dismiss(alert_id: int):
    if wg.get_alert(alert_id) is None:
        raise HTTPException(404, f"no such alert: {alert_id}")
    wg.dismiss_alert(alert_id)
    return JSONResponse({"ok": True})


# --- Projects / Parties / Ownership rules --------------------------------
# Every mutation here is a plain callable REST endpoint a worker can invoke
# directly (via curl/Bash) when Marc corrects an auto-grouping or an
# ownership guess through conversation - there is deliberately no separate
# "approval workflow" mechanism beyond that, per Marc's explicit call that
# correction should happen conversationally, not through a required review
# queue.

@app.get("/api/workgraph/last-refresh")
async def api_last_refresh():
    """Feeds the footer's data-freshness line. `next_scheduled` is computed
    from the known 6/8/12/17/24 ET cadence (SymphonyCockpitRefresh), not
    read from anywhere - there's no live "next run" API, this is just the
    fixed schedule projected forward from now."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/New_York")
    now = datetime.now(tz)
    slots = [6, 8, 12, 17, 24]  # 24 == midnight of the FOLLOWING day
    next_hour = next((h for h in slots if h > now.hour), None)
    if next_hour is None:
        next_dt = (now + timedelta(days=1)).replace(hour=slots[0], minute=0, second=0, microsecond=0)
    elif next_hour == 24:
        next_dt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        next_dt = now.replace(hour=next_hour, minute=0, second=0, microsecond=0)
    def _fmt(dt):
        # %-d/%-I (no leading zero) are Unix-only strftime flags - this runs on
        # Windows, so strip the zero manually instead of relying on them.
        return dt.strftime("%b %d, %I:%M %p").replace(" 0", " ")
    last_ts = wg.get_last_refresh_ts()
    last_str = _fmt(datetime.fromtimestamp(last_ts, tz)) if last_ts else None
    return JSONResponse({"last_refresh": last_str, "next_scheduled": _fmt(next_dt).split(", ")[-1]})


@app.get("/api/workgraph/projects")
async def api_projects_list(status: Optional[str] = None):
    statuses = [s.strip() for s in status.split(",")] if status else None
    projects = wg.list_projects(status=statuses)
    return JSONResponse({"projects": sanitize_surrogates(projects)})


@app.get("/api/workgraph/projects/{project_id}")
async def api_project_detail(project_id: str):
    project = wg.get_project(project_id)
    if project is None:
        raise HTTPException(404, f"no such project: {project_id}")
    issues = wg.list_issues_for_project(project_id)
    workgraph_deadlines.attach_deadline_info(issues)
    synthesis = wg.get_synthesis("project", project_id)
    attachments = wg.list_attachments_for_project(project_id)
    return JSONResponse({"project": sanitize_surrogates(project), "issues": sanitize_surrogates(issues),
                        "synthesis": sanitize_surrogates(synthesis),
                        "attachments": sanitize_surrogates(attachments)})


class IssueProjectBody(BaseModel):
    project_id: Optional[str] = None  # null = detach from any project
    reason: Optional[str] = None


@app.post("/api/workgraph/issues/{issue_id}/project")
async def api_issue_assign_project(issue_id: str, body: IssueProjectBody):
    """Reassign/detach an issue's project - the one endpoint both the
    auto-grouper (workgraph_projects.py) and a worker correcting it on
    Marc's say-so both call."""
    if wg.get_issue(issue_id) is None:
        raise HTTPException(404, f"no such issue: {issue_id}")
    if body.project_id is not None and wg.get_project(body.project_id) is None:
        raise HTTPException(404, f"no such project: {body.project_id}")
    wg.assign_issue_to_project(issue_id, body.project_id, reason=body.reason)
    return JSONResponse({"ok": True, "issue": sanitize_surrogates(wg.get_issue(issue_id))})


MAX_GROUPING_SUGGESTIONS_PER_WAKE = 12


@app.get("/api/workgraph/project-suggestions")
async def api_project_suggestions_list(limit: int = MAX_GROUPING_SUGGESTIONS_PER_WAKE):
    """Capped + oldest-first (Design-1 budget governor): curator's
    PROJECT_GROUPING wake reads this route directly, so the cap has to live
    here rather than in the Python-side skip-if-empty gate in
    scheduled_refresh.py, which never sees the actual list. Oldest-first so a
    backlog doesn't starve the suggestions that have been pending longest;
    anything past the cap is reported via `deferred`, not silently dropped -
    it's still 'pending' in the DB and shows up again next wake."""
    all_pending = wg.list_project_suggestions(status="pending")
    all_pending.sort(key=lambda s: s["created_ts"])
    capped = all_pending[:limit]
    deferred = max(0, len(all_pending) - limit)
    return JSONResponse({"suggestions": sanitize_surrogates(capped), "deferred": deferred})


class ProjectSuggestionResolveBody(BaseModel):
    status: str  # confirmed | rejected


@app.post("/api/workgraph/project-suggestions/{suggestion_id}/resolve")
async def api_project_suggestion_resolve(suggestion_id: int, body: ProjectSuggestionResolveBody):
    """Confirming now actually merges the two issues into a shared project
    (was previously a no-op beyond marking the row reviewed) - called by a
    human clicking Confirm/Reject in the cockpit, and by curator's LLM
    judgment pass on the weak-signal residue (see workgraph_projects.
    confirm_suggestion/reject_suggestion)."""
    if body.status not in ("confirmed", "rejected"):
        raise HTTPException(400, "status must be 'confirmed' or 'rejected'")
    if wg.get_project_suggestion(suggestion_id) is None:
        raise HTTPException(404, f"no such suggestion: {suggestion_id}")
    if body.status == "confirmed":
        result = workgraph_projects.confirm_suggestion(suggestion_id)
    else:
        result = workgraph_projects.reject_suggestion(suggestion_id)
    return JSONResponse({"ok": True, "result": result})


@app.get("/api/workgraph/parties")
async def api_parties_list(affiliation: Optional[str] = None):
    return JSONResponse({"parties": sanitize_surrogates(wg.list_parties(affiliation=affiliation))})


@app.get("/api/workgraph/suppliers")
async def api_suppliers_list():
    """Task #75 (Supplier Relationship Dashboard). Grouped by external
    party company - see workgraph_suppliers.py's docstring. Zero LLM,
    zero new data; everything here is grouped/summed from parties/issues/
    workgraph_nba's own value extraction/workgraph_deadlines' own
    hard/soft classification."""
    return JSONResponse({"suppliers": sanitize_surrogates(workgraph_suppliers.list_suppliers())})


@app.get("/api/workgraph/suppliers/{company}")
async def api_supplier_detail(company: str):
    detail = workgraph_suppliers.supplier_detail(company)
    if detail is None:
        raise HTTPException(404, f"no such supplier: {company}")
    return JSONResponse(sanitize_surrogates(detail))


class PartyCorrectionBody(BaseModel):
    affiliation: str  # internal | external
    company: Optional[str] = None
    reason: Optional[str] = None


@app.post("/api/workgraph/parties/{party_id}/correct")
async def api_party_correct(party_id: str, body: PartyCorrectionBody):
    if body.affiliation not in ("internal", "external"):
        raise HTTPException(400, "affiliation must be 'internal' or 'external'")
    wg.correct_party_affiliation(party_id, affiliation=body.affiliation, company=body.company, reason=body.reason)
    return JSONResponse({"ok": True})


class TaskOwnerBody(BaseModel):
    owner: Optional[str] = None  # 'marc', a parties.id, or null (unknown)
    reason: Optional[str] = None
    generalize: bool = False  # if true, also creates an ownership_rule from match_field/match_value below
    match_field: Optional[str] = None
    match_value: Optional[str] = None


@app.post("/api/workgraph/tasks/{task_id}/owner")
async def api_task_owner_correct(task_id: str, body: TaskOwnerBody):
    if wg.get_task(task_id) is None:
        raise HTTPException(404, f"no such task: {task_id}")
    wg.correct_task_owner(task_id, owner=body.owner, reason=body.reason)
    if body.generalize:
        if not body.match_field or not body.match_value:
            raise HTTPException(400, "generalize=true requires match_field and match_value")
        wg.create_ownership_rule(match_field=body.match_field, match_value=body.match_value,
                                  default_owner=body.owner or "unknown", created_reason=body.reason)
    return JSONResponse({"ok": True, "task": sanitize_surrogates(wg.get_task(task_id))})


@app.get("/api/workgraph/ownership-rules")
async def api_ownership_rules_list():
    return JSONResponse({"rules": sanitize_surrogates(wg.list_ownership_rules())})


@app.delete("/api/workgraph/ownership-rules/{rule_id}")
async def api_ownership_rule_delete(rule_id: int):
    wg.delete_ownership_rule(rule_id)
    return JSONResponse({"ok": True})


# --- Aristotle: taught prerequisite/gate rules (task #51) ------------------
# Rules are only ever created here, by explicit Settings input - never
# inferred from mail patterns (see workgraph_aristotle.py's own docstring).

class PrerequisiteRuleBody(BaseModel):
    trigger_signal_type: str
    requires_signal_type: str
    match_on: str
    reason: Optional[str] = None


@app.get("/api/settings/prerequisite-rules")
async def api_prerequisite_rules_list():
    return JSONResponse({
        "rules": sanitize_surrogates(wg.list_prerequisite_rules()),
        "known_signal_types": workgraph_signals.known_signal_types(),
    })


@app.post("/api/settings/prerequisite-rules")
async def api_prerequisite_rule_create(body: PrerequisiteRuleBody):
    if body.match_on not in ("project", "supplier"):
        raise HTTPException(400, "match_on must be 'project' or 'supplier'")
    known = workgraph_signals.known_signal_types()
    if body.trigger_signal_type not in known:
        raise HTTPException(400, f"unknown trigger_signal_type: {body.trigger_signal_type!r}")
    if body.requires_signal_type not in known:
        raise HTTPException(400, f"unknown requires_signal_type: {body.requires_signal_type!r}")
    if body.trigger_signal_type == body.requires_signal_type:
        raise HTTPException(400, "trigger_signal_type and requires_signal_type must differ")
    rule_id = wg.create_prerequisite_rule(
        trigger_signal_type=body.trigger_signal_type, requires_signal_type=body.requires_signal_type,
        match_on=body.match_on, reason=body.reason or "",
        created_by=config.get("manager", "id") or "marc",
    )
    return JSONResponse({"ok": True, "rule_id": rule_id})


class PrerequisiteRuleActiveBody(BaseModel):
    active: bool


@app.post("/api/settings/prerequisite-rules/{rule_id}/active")
async def api_prerequisite_rule_set_active(rule_id: int, body: PrerequisiteRuleActiveBody):
    wg.set_prerequisite_rule_active(rule_id, body.active)
    return JSONResponse({"ok": True})


@app.delete("/api/settings/prerequisite-rules/{rule_id}")
async def api_prerequisite_rule_delete(rule_id: int):
    wg.delete_prerequisite_rule(rule_id)
    return JSONResponse({"ok": True})


@app.get("/api/settings/prerequisite-rule-suggestions")
async def api_prerequisite_rule_suggestions_list():
    """Shared review queue for BOTH origins (task #52 auto-detected, task
    #54 chat-taught) - one place to confirm or reject, regardless of where a
    candidate came from."""
    return JSONResponse({
        "suggestions": sanitize_surrogates(wg.list_prerequisite_suggestions("pending")),
    })


class PrerequisiteSuggestionResolveBody(BaseModel):
    action: str  # "confirm" | "reject"


@app.post("/api/settings/prerequisite-rule-suggestions/{suggestion_id}/resolve")
async def api_prerequisite_rule_suggestion_resolve(suggestion_id: int, body: PrerequisiteSuggestionResolveBody):
    if body.action not in ("confirm", "reject"):
        raise HTTPException(400, "action must be 'confirm' or 'reject'")
    suggestion = wg.get_prerequisite_suggestion(suggestion_id)
    if suggestion is None:
        raise HTTPException(404, f"no such suggestion: {suggestion_id}")
    if suggestion["status"] != "pending":
        raise HTTPException(400, f"suggestion already {suggestion['status']}")
    if body.action == "confirm":
        if not (suggestion.get("trigger_signal_type") and suggestion.get("requires_signal_type")
                and suggestion.get("match_on")):
            raise HTTPException(400, "this suggestion isn't structured enough to confirm yet - "
                                      "add it as a real rule above using the dropdowns instead")
        wg.create_prerequisite_rule(
            trigger_signal_type=suggestion["trigger_signal_type"],
            requires_signal_type=suggestion["requires_signal_type"],
            match_on=suggestion["match_on"], reason=suggestion.get("reason") or "",
            created_by=config.get("manager", "id") or "marc",
        )
        wg.resolve_prerequisite_suggestion(suggestion_id, "confirmed")
    else:
        wg.resolve_prerequisite_suggestion(suggestion_id, "rejected")
    return JSONResponse({"ok": True})


# --- Per-communication extraction / per-entity synthesis ------------------
# Extraction is real LLM judgment (asks/decisions/dates/commitments/facts),
# written once per raw_item by curator's synthesis routine and never
# recomputed wholesale (see SYNTHESIS_ROUTINE.md, workgraph_synthesis.py).
# Synthesis applies to Projects (aggregating every constituent issue's
# evidence) and standalone Issues alike via the same entity_type/entity_id
# mechanism - no separate routes per entity type, per the build constraint.

class ExtractionBody(BaseModel):
    extracted_json: dict


@app.post("/api/workgraph/raw_items/{raw_item_id}/extraction")
async def api_raw_item_extraction_write(raw_item_id: int, body: ExtractionBody):
    if wg.get_raw_item(raw_item_id) is None:
        raise HTTPException(404, f"no such raw_item: {raw_item_id}")
    wg.create_extraction(raw_item_id, json.dumps(body.extracted_json))
    return JSONResponse({"ok": True, "extraction": sanitize_surrogates(wg.get_extraction(raw_item_id))})


class SynthesisBody(BaseModel):
    summary: Optional[str] = None
    next_steps: list = Field(default_factory=list)
    suggested_actions: list = Field(default_factory=list)
    derived_title: Optional[str] = None
    estimated_completion: Optional[dict] = None  # {"note": "...", "confidence": "documented"|"model"|"unknown"}
    closing_lesson: Optional[dict] = None  # {"statement": "..."} - optional Total Recall lesson,
    # written only when curator's synthesis pass has a real one-line takeaway worth remembering.
    # Never a standalone LLM call - piggybacks on a synthesis write curator was already making.


@app.get("/api/workgraph/{entity_type}/{entity_id}/synthesis")
async def api_synthesis_get(entity_type: str, entity_id: str):
    if entity_type not in ("issue", "project"):
        raise HTTPException(404, f"unknown entity_type: {entity_type}")
    return JSONResponse({"synthesis": sanitize_surrogates(wg.get_synthesis(entity_type, entity_id))})


@app.post("/api/workgraph/{entity_type}/{entity_id}/synthesis")
async def api_synthesis_write(entity_type: str, entity_id: str, body: SynthesisBody):
    """Curator's one-shot synthesis routine writes here. The evidence marker
    is computed server-side from the CURRENT state of the DB at write time -
    a worker never supplies synthesized_from_marker itself, so a write can't
    mark itself fresh against stale/fabricated input."""
    if entity_type not in ("issue", "project"):
        raise HTTPException(404, f"unknown entity_type: {entity_type}")
    if entity_type == "issue" and wg.get_issue(entity_id) is None:
        raise HTTPException(404, f"no such issue: {entity_id}")
    if entity_type == "project" and wg.get_project(entity_id) is None:
        raise HTTPException(404, f"no such project: {entity_id}")
    marker = workgraph_synthesis.compute_evidence_marker(entity_type, entity_id)
    wg.upsert_synthesis(
        entity_type=entity_type, entity_id=entity_id, summary=body.summary,
        next_steps_json=json.dumps(body.next_steps),
        suggested_actions_json=json.dumps(body.suggested_actions),
        synthesized_from_marker=marker, derived_title=body.derived_title,
        estimated_completion_json=json.dumps(body.estimated_completion) if body.estimated_completion is not None else None,
    )
    # Optional Total Recall lesson, piggybacked on this synthesis write - only
    # meaningful for a standalone issue (a lesson's situation_key is built
    # from ONE issue's category+company; a project spans several). A missing
    # category/company signal, or an entity_type of 'project', is a silent
    # no-op here - same abstain-don't-force discipline as the rest of this
    # feature, never an error surfaced back to curator.
    if entity_type == "issue" and body.closing_lesson and body.closing_lesson.get("statement"):
        key = workgraph_lessons.situation_key_for_issue(wg.get_issue(entity_id))
        if key:
            workgraph_lessons.record_lesson(
                situation_key_val=key, statement=body.closing_lesson["statement"],
                outcome="resolved", source_issue_id=entity_id,
            )
    return JSONResponse({"ok": True, "synthesis": sanitize_surrogates(wg.get_synthesis(entity_type, entity_id))})


class SignalTreatmentBody(BaseModel):
    treatment: str  # 'noise' | 'fyi' | 'actionable' | 'closure'
    reason: Optional[str] = None
    set_by: Optional[str] = None


@app.get("/api/workgraph/signal-rules")
async def api_signal_rules_list():
    """Every known-signal-type treatment override currently in effect (see
    workgraph_signals.py) - an audit view of what's been corrected away from
    the code default. known_signal_types included so Settings (task #58) can
    populate its dropdown from real, confirmed values - never freeform text."""
    return JSONResponse({
        "overrides": sanitize_surrogates(wg.list_signal_treatments()),
        "known_signal_types": workgraph_signals.known_signal_types(),
    })


@app.post("/api/workgraph/signal-rules/{signal_type}")
async def api_signal_rule_set(signal_type: str, body: SignalTreatmentBody):
    """Correct a known-automated-signal type's treatment (noise/fyi/
    actionable/closure) without a code change - e.g. Marc telling a worker
    'mark ContractPodAI's obligation-update emails as noise' persists here.
    Sticks until changed again; the next classify pass picks it up."""
    if body.treatment not in ("noise", "fyi", "actionable", "closure"):
        raise HTTPException(400, f"unknown treatment: {body.treatment!r}")
    wg.set_signal_treatment(signal_type, body.treatment, reason=body.reason, set_by=body.set_by)
    return JSONResponse({"ok": True, "signal_type": signal_type, "treatment": body.treatment})


class CapabilitySuggestionBody(BaseModel):
    origin: str
    observation: str
    suggestion: str
    rationale: Optional[str] = None


@app.post("/api/workgraph/capability-suggestions")
async def api_capability_suggestion_create(body: CapabilitySuggestionBody):
    """Any worker logs a NOTE here when it notices a real Jasper gap during
    normal work - never a code change on its own. Sits pending until Marc
    reviews it (or a chat request greenlights it directly)."""
    if not body.observation.strip() or not body.suggestion.strip():
        raise HTTPException(400, "observation and suggestion are both required")
    sid = wg.create_capability_suggestion(
        origin=body.origin, observation=body.observation,
        suggestion=body.suggestion, rationale=body.rationale,
    )
    return JSONResponse({"ok": True, "id": sid})


@app.get("/api/workgraph/capability-suggestions")
async def api_capability_suggestions_list(status: str = "pending"):
    if status not in ("pending", "confirmed", "rejected"):
        raise HTTPException(400, f"unknown status: {status!r}")
    return JSONResponse({"suggestions": sanitize_surrogates(wg.list_capability_suggestions(status=status))})


class CapabilitySuggestionResolveBody(BaseModel):
    status: str  # confirmed | rejected
    resolution_note: Optional[str] = None


@app.post("/api/workgraph/capability-suggestions/{suggestion_id}/resolve")
async def api_capability_suggestion_resolve(suggestion_id: int, body: CapabilitySuggestionResolveBody):
    """Marc's call, always - confirming here means 'worth building,' not a
    trigger for anything automatic. The actual build is still a separate,
    explicit step."""
    if body.status not in ("confirmed", "rejected"):
        raise HTTPException(400, "status must be 'confirmed' or 'rejected'")
    if wg.get_capability_suggestion(suggestion_id) is None:
        raise HTTPException(404, f"no such suggestion: {suggestion_id}")
    wg.resolve_capability_suggestion(suggestion_id, body.status, resolution_note=body.resolution_note)
    return JSONResponse({"ok": True})


class SocratesAskBody(BaseModel):
    question: str
    issue_id: Optional[str] = None
    asker: Optional[str] = None
    depth: Optional[str] = None  # 'lookup' | 'standard' | 'deep' - a request, not a guarantee (safety floor applies)


@app.post("/api/socrates/ask")
async def api_socrates_ask(body: SocratesAskBody):
    """Ask Jasper a free-text question; answered from precedent (Total
    Recall), the relevant synthesis, and linked evidence - no LLM call, no
    fabrication. See workgraph_socrates.py.

    Task #54 - checked BEFORE that zero-LLM path, not inside it:
    1. A "#addrule ..." message (optionally still carrying a leading
       @mention, if the frontend also posted it to a worker) always gets
       captured as a pending prerequisite-rule suggestion here, then
       best-effort structured by rule_extraction.py's local LLM. Never
       reaches workgraph_socrates.answer() - it isn't a question.
    2. A short "confirm"/"yes"/"reject"/"no" reply resolves the asker's most
       recent still-pending taught-via-chat suggestion, if one exists within
       the recency window. If it doesn't look like an answer to something
       pending, this is a no-op and falls through to the normal path below -
       workgraph_socrates.answer() itself is completely untouched."""
    if not (body.question or "").strip():
        raise HTTPException(400, "question required")
    if body.issue_id and wg.get_issue(body.issue_id) is None:
        raise HTTPException(404, f"no such issue: {body.issue_id}")

    if rule_teaching.is_addrule_message(body.question):
        result = await asyncio.to_thread(rule_teaching.teach_from_chat, body.question, body.asker or "")
        return JSONResponse({"answer": result["reply"], "outcome": "rule_captured",
                             "suggestion_id": result["suggestion_id"]})

    # Task #62 - checked BEFORE the plain confirm/reject resolver below: a
    # bare "yes" while a clarification conversation is active means "yes,
    # let's walk through it" (or an answer to one of its questions), not a
    # confirm/reject answer for some unrelated already-structured suggestion.
    clarification = rule_teaching.try_continue_clarification(body.question, body.asker or "")
    if clarification is not None:
        return JSONResponse({"answer": clarification["reply"], "outcome": "rule_clarifying"})

    resolution = rule_teaching.try_resolve_pending_confirmation(body.question, body.asker or "")
    if resolution is not None:
        return JSONResponse({"answer": resolution["reply"], "outcome": "rule_resolved"})

    result = workgraph_socrates.answer(
        question=body.question, issue_id=body.issue_id, asker=body.asker, explicit_depth=body.depth,
    )
    return JSONResponse(sanitize_surrogates(result))


# --- Attachments: real files on disk, related to a specific issue/project/
# chat, not just a filename mentioned in a message. ------------------------
ATTACHMENT_ENTITY_DIRS = {
    "issue": DOCUMENTS_ISSUES_DIR,
    "project": DOCUMENTS_PROJECTS_DIR,
    "chat": DOCUMENTS_CHAT_DIR,
    "raw_item": DOCUMENTS_RAW_ITEMS_DIR,  # email attachments, before classification assigns an issue
}
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


def _safe_path_segment(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]", "_", s or "")
    s = s.strip(". ")
    return s if s and s not in (".", "..") else "_"


def _safe_filename(name: str) -> str:
    name = os.path.basename(name or "")
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name)
    return name[:180] or "upload.bin"


@app.post("/api/attachments")
async def api_attachment_upload(
    file: UploadFile = File(...),
    entity_type: str = Form(...),
    entity_id: Optional[str] = Form(None),
    kind: str = Form("upload"),
    uploaded_by: str = Form("marc"),
):
    if entity_type not in ATTACHMENT_ENTITY_DIRS:
        raise HTTPException(400, f"unknown entity_type: {entity_type}")
    if entity_type == "issue" and entity_id and wg.get_issue(entity_id) is None:
        raise HTTPException(404, f"no such issue: {entity_id}")
    if entity_type == "project" and entity_id and wg.get_project(entity_id) is None:
        raise HTTPException(404, f"no such project: {entity_id}")

    raw = await file.read()
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(413, f"file too large (max {MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB)")

    base_dir = ATTACHMENT_ENTITY_DIRS[entity_type]
    sub_dir = base_dir / (_safe_path_segment(entity_id) if entity_id else "_unscoped")
    sub_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(raw).hexdigest()
    safe_name = _safe_filename(file.filename)
    dest = sub_dir / f"{digest[:16]}_{safe_name}"
    dest.write_bytes(raw)

    attachment_id = wg.create_attachment(
        entity_type=entity_type, entity_id=entity_id, kind=kind,
        filename=file.filename or safe_name, stored_path=str(dest.relative_to(DOCUMENTS_DIR)),
        content_type=file.content_type, size_bytes=len(raw),
        sha256_hex=digest, uploaded_by=uploaded_by,
    )
    return JSONResponse({"ok": True, "attachment": sanitize_surrogates(wg.get_attachment(attachment_id))})


@app.get("/api/attachments")
async def api_attachment_list(entity_type: str, entity_id: str):
    return JSONResponse({"attachments": sanitize_surrogates(wg.list_attachments(entity_type, entity_id))})


@app.get("/api/workgraph/issues/{issue_id}/attachments")
async def api_issue_attachments(issue_id: str):
    """Direct issue attachments + email attachments inherited from any raw_item
    already linked to this issue (see wg.list_attachments_for_issue)."""
    if wg.get_issue(issue_id) is None:
        raise HTTPException(404, f"no such issue: {issue_id}")
    return JSONResponse({"attachments": sanitize_surrogates(wg.list_attachments_for_issue(issue_id))})


@app.get("/api/attachments/{attachment_id}/download")
async def api_attachment_download(attachment_id: int):
    att = wg.get_attachment(attachment_id)
    if att is None:
        raise HTTPException(404, "no such attachment")
    full_path = DOCUMENTS_DIR / att["stored_path"]
    if not full_path.is_file():
        raise HTTPException(404, "file missing on disk")
    return FileResponse(str(full_path), filename=att["filename"], media_type=att.get("content_type") or "application/octet-stream")


@app.delete("/api/attachments/{attachment_id}")
async def api_attachment_delete(attachment_id: int):
    att = wg.get_attachment(attachment_id)
    if att is None:
        raise HTTPException(404, "no such attachment")
    wg.delete_attachment(attachment_id)
    try:
        full_path = DOCUMENTS_DIR / att["stored_path"]
        if full_path.is_file():
            full_path.unlink()
    except OSError:
        pass
    return JSONResponse({"ok": True})


@app.post("/api/cockpit/actions")
async def api_cockpit_action(body: CockpitActionBody):
    """Generative actions (draft, review, summarize) — wakes a worker via the
    PROVEN action-bridge (team_room @mention -> F9 fanout -> worker_notifications
    -> the worker's armed Monitor poller), empirically confirmed working this
    session. The message is a thin wake-trigger + pointer; the worker pulls full
    Issue context from workgraph.db itself, not from the message body."""
    if wg.get_issue(body.issue_id) is None:
        raise HTTPException(404, f"no such issue: {body.issue_id}")
    sender = config.get("manager", "id") or "marc"
    envelope = "@{worker} [COCKPIT-ACTION] {payload}".format(
        worker=body.worker,
        payload=json.dumps({"type": body.action_kind, "issue_id": body.issue_id,
                           "instructions": body.instructions}, ensure_ascii=False),
    )
    try:
        result = team_room.post_message(sender=sender, body=envelope)
    except ValueError as e:
        raise HTTPException(400, str(e))
    pending_id = wg.create_pending_action(
        issue_id=body.issue_id, action_kind=body.action_kind, worker=body.worker,
        instructions=body.instructions, message_id=result.get("message_id"),
    )
    return JSONResponse({"ok": True, "pending_action_id": pending_id, "message_id": result.get("message_id")})


_cockpit_refresh_in_flight = False


def _run_cockpit_refresh_sync() -> dict:
    """The actual blocking work, unchanged from before - split out so it can
    run on a worker thread (see api_cockpit_refresh below) instead of
    directly on the event loop."""
    try:
        ingest_result = outlook_com_ingest.run()
    except Exception as e:
        ingest_result = {"ok": False, "error": str(e)}
    classify_result = workgraph_classify.run()
    nba_result = workgraph_nba.recompute_all()
    alerts_result = workgraph_alerts.run()
    return {"ingest": ingest_result, "classify": classify_result, "nba": nba_result,
            "alerts": alerts_result}


@app.post("/api/cockpit/refresh")
async def api_cockpit_refresh():
    """Synchronous mail re-ingest + classify + re-score, for the cockpit's
    'Refresh' button — mail doesn't need a worker wake (pure COM automation),
    so this returns fresh results immediately rather than waiting on relay's
    own scheduled cadence. Teams/Calendar/SharePoint still need relay's wake
    (the MCP tools aren't reachable from this plain server process).

    In-flight guard (added 2026-07-29): this endpoint existed but was never
    actually wired to a UI button - the cockpit's "Refresh now" control only
    re-rendered already-loaded client-side data, never called this. Now that
    it's wired for real (see mqRefresh in cockpit.html), a real ingest pass
    can take several seconds; refusing a second concurrent call (409, not a
    silent queue or a second overlapping ingest) is cheap insurance against
    mashing the button twice.

    asyncio.to_thread (task #42, fixed 2026-07-29): this used to run
    _run_cockpit_refresh_sync's blocking work directly on the event loop -
    confirmed by direct measurement that this froze the ENTIRE server (not
    just this endpoint) for the whole duration: an unrelated /api/manager
    call fired 0.3s into a 15.7s refresh took 15.2s to respond, i.e. it was
    genuinely blocked, not just slow. Running the same code via
    asyncio.to_thread keeps it exactly as correct (workgraph_store/bus's own
    threading.Lock + WAL/busy_timeout already make this safe from a worker
    thread - independently confirmed under real cross-PROCESS concurrency,
    a strictly stronger guarantee than one extra thread needs), while
    freeing the event loop to keep serving every other request normally
    while a refresh runs."""
    global _cockpit_refresh_in_flight
    if _cockpit_refresh_in_flight:
        raise HTTPException(409, "a refresh is already in progress")
    _cockpit_refresh_in_flight = True
    try:
        result = await asyncio.to_thread(_run_cockpit_refresh_sync)
    finally:
        _cockpit_refresh_in_flight = False
    return JSONResponse(result)


@app.get("/api/workers/status")
async def api_workers_status():
    """Merges the existing binary active/idle/unknown heartbeat (_cohorts_feed)
    with the new rich worker_status table (current_task/detail), keyed by worker id."""
    cohorts = _cohorts_feed()
    rich = wg.get_all_worker_status()
    workers = []
    for cohort in cohorts:
        for w in cohort.get("workers", []):
            merged = dict(w)
            extra = rich.get(w["worker"])
            if extra:
                merged["current_task"] = extra.get("current_task")
                merged["detail"] = extra.get("detail")
                merged["rich_state"] = extra.get("state")
            workers.append(merged)
    return JSONResponse({"workers": sanitize_surrogates(workers)})


@app.post("/api/workers/{worker_id}/status")
async def api_worker_status_set(worker_id: str, body: WorkerStatusBody):
    """A worker reports its own rich status (state/current_task/detail) —
    best-effort, worker-written, per its routine doc's status-report step."""
    wg.set_worker_status(worker_id, state=body.state, current_task=body.current_task, detail=body.detail)
    return JSONResponse({"ok": True})


@app.get("/api/cockpit/chat/{worker_id}")
async def api_cockpit_chat(worker_id: str):
    """Two-way DM thread with one worker, merged from both sides' inbox files
    (inbox.py already has send/list/counts — no new messaging primitive
    needed, just a merged read). Marc's side of the thread lives in the
    manager's own inbox (messages FROM worker_id); the worker's side lives in
    worker_id's inbox (messages addressed TO worker_id, i.e. everything Marc
    or others sent it). Sending reuses the existing /api/post DM path
    (to=worker_id) verbatim - no new send route."""
    manager_id = config.get("manager", "tag") or config.get("manager", "id") or "manager"
    to_worker = [m for m in inbox.list_messages(worker_id) if m.get("to") == worker_id]
    from_worker = [m for m in inbox.list_messages(manager_id) if m.get("from") == worker_id]
    thread = sorted(to_worker + from_worker, key=lambda m: m.get("ts") or "")
    return JSONResponse({"worker": worker_id, "messages": sanitize_surrogates(thread)})


# ═══════════════════════════════════════════════════════════════════════════
# Help + Settings pages (the 5 shipped tabs · George pp 2026-07-16)
# ═══════════════════════════════════════════════════════════════════════════
@app.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    return templates.TemplateResponse(request, "help.html", {
        "active_page": "help",
        "now_iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse(request, "settings.html", {
        "active_page": "settings",
        "now_iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


class WorkerRenameBody(BaseModel):
    display_name: str


@app.post("/api/workers/{worker_id}/rename")
async def api_worker_rename(worker_id: str, body: WorkerRenameBody):
    """Rename a worker's DISPLAY name (George #3 · let users name their workers).
    The stable slot-id (worker_id = roster key = marker key) NEVER changes — only the
    display label. Writes the model's single source config.roles[slot-id].display_name
    AND the runtime read members.json (kept in sync → no drift; members.json mtime-bump
    invalidates its cache so the new name shows immediately). Rename-safe by construction:
    slot-id unchanged → markers / refs / resolver untouched (Sage rail #3, Mira C1)."""
    new_name = (body.display_name or "").strip()
    if not new_name:
        raise HTTPException(400, "display_name cannot be empty")
    if len(new_name) > 60:
        raise HTTPException(400, "display_name too long (max 60 characters)")
    members = members_mod.list_members()
    if not any(m.get("id") == worker_id for m in members):
        raise HTTPException(404, f"no such worker: {worker_id}")
    # Uniqueness for @-mention clarity (Quinn's compose-loop contract): no OTHER worker
    # may already carry this display name.
    for m in members:
        if m.get("id") != worker_id and \
                (m.get("display_name") or m.get("name") or "").strip().lower() == new_name.lower():
            raise HTTPException(409, f'"{new_name}" is already used by another worker — pick a distinct name')
    # 1. config.roles[slot-id].display_name — the model's single source (if this body has one)
    try:
        if _CONFIG_PATH.is_file():
            cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            roles = cfg.get("roles")
            if isinstance(roles, dict) and isinstance(roles.get(worker_id), dict):
                roles[worker_id]["display_name"] = new_name
                config.write_json_atomic(_CONFIG_PATH, cfg)
    except Exception as e:
        raise HTTPException(500, f"failed writing config.roles display_name: {e}")
    # 2. members.json display_name — the runtime read (mtime-bump invalidates members cache)
    try:
        mp = members_mod.MEMBERS_PATH
        data = json.loads(mp.read_text(encoding="utf-8"))
        changed = False
        for m in data.get("members", []):
            if m.get("id") == worker_id:
                m["display_name"] = new_name
                changed = True
        if not changed:
            raise HTTPException(404, f"worker {worker_id} not present in members.json")
        config.write_json_atomic(mp, data)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"failed writing members.json display_name: {e}")
    return JSONResponse({"ok": True, "worker_id": worker_id, "display_name": new_name})


# ═══════════════════════════════════════════════════════════════════════════
# You (the operator) + cohort name + restart — runtime identity (George Settings
# #5d / #7 / #6, pp_e336…). Three small self-serve controls a new user needs and
# won't find in any technical detail.
# ═══════════════════════════════════════════════════════════════════════════
# The OPERATOR is the installing human (config.manager) — NOT a roster worker, NOT
# the §2 served-audience "principal". Seeded at install (SSO-prefill default, #5b)
# and editable here. This ONE endpoint is the shared runtime write-fn: the compose
# naming step AND this Settings field both POST here → config.manager is always
# latest (last-confirm-wins). Consumers read display_name → name → id (robust).

class ManagerBody(BaseModel):
    id: str                       # what the operator wants to be called
    tag: Optional[str] = None     # optional @-handle; defaults to a slug of id


@app.get("/api/manager")
async def api_manager_get():
    """Read the current operator (the human running this Symphony) — powers the
    Settings 'You' field + the top-bar @you. config.manager is the single source."""
    mid = (config.get("manager", "id") or "").strip()
    tag = (config.get("manager", "tag") or "").strip()
    return JSONResponse({"id": mid or None, "tag": tag or None, "display_name": mid or None})


class CockpitSettingsBody(BaseModel):
    chat_history_window: int


@app.get("/api/cockpit/settings")
async def api_cockpit_settings_get():
    """Cockpit-specific settings, currently just the chat history window (how
    many messages of each worker-thread the conversation panel keeps visible -
    was hardcoded to 20 in cockpit.html, now a real Settings control)."""
    window = config.get("cockpit", "chat_history_window")
    return JSONResponse({"chat_history_window": int(window) if window else 20})


class RetentionCategoryBody(BaseModel):
    days: Optional[int] = None
    keep_days: Optional[int] = None


class RetentionSnapshotsBody(BaseModel):
    daily_keep: Optional[int] = None
    weekly_keep: Optional[int] = None
    monthly_keep: Optional[int] = None


class RetentionSettingsBody(BaseModel):
    enforcement_enabled: Optional[bool] = None
    bus_worker_notifications: Optional[RetentionCategoryBody] = None
    bus_events: Optional[RetentionCategoryBody] = None
    socrates_retrieval_log: Optional[RetentionCategoryBody] = None
    logs: Optional[RetentionCategoryBody] = None
    raw_ingest_processed: Optional[RetentionCategoryBody] = None
    db_snapshots: Optional[RetentionSnapshotsBody] = None


@app.get("/api/settings/retention")
async def api_settings_retention_get():
    """Data & Retention Settings section - current policy config (merged with
    defaults for anything not yet customized), plus the disk-usage dashboard
    and last-run status, so the numbers on this page are always real, never
    stale placeholders."""
    def _cat(name: str, fallback: dict) -> dict:
        val = config.get("retention", name)
        return val if isinstance(val, dict) else fallback

    snap_fallback = {"daily_keep": backup.DEFAULT_DAILY_KEEP, "weekly_keep": backup.DEFAULT_WEEKLY_KEEP,
                      "monthly_keep": backup.DEFAULT_MONTHLY_KEEP}
    return JSONResponse({
        "enforcement_enabled": bool(config.get("retention", "enforcement_enabled")),
        "categories": {
            "bus_worker_notifications": _cat("bus_worker_notifications", {"days": 60}),
            "bus_events": _cat("bus_events", {"days": 270}),
            "socrates_retrieval_log": _cat("socrates_retrieval_log", {"days": 90}),
            "logs": _cat("logs", {"keep_days": 60}),
            "raw_ingest_processed": _cat("raw_ingest_processed", {"days": 730}),
        },
        "db_snapshots": _cat("db_snapshots", snap_fallback),
        "disk_usage": retention.disk_usage_report(),
        "last_retention_run": wg.get_cursor("retention", "last_run_date"),
    })


@app.post("/api/settings/retention")
async def api_settings_retention_set(body: RetentionSettingsBody):
    """Partial update - only fields actually present in the request body are
    written; everything else keeps its current value. enforcement_enabled
    defaults to False and is the ONE toggle that turns any of this from
    report-only into an actual delete/archive - see retention.py's module
    docstring for why that default matters."""
    if body.enforcement_enabled is not None:
        config.set_value(body.enforcement_enabled, "retention", "enforcement_enabled")
    for field_name in ("bus_worker_notifications", "bus_events", "socrates_retrieval_log",
                       "logs", "raw_ingest_processed"):
        cat = getattr(body, field_name)
        if cat is not None:
            existing = config.get("retention", field_name) or {}
            merged = dict(existing) if isinstance(existing, dict) else {}
            if cat.days is not None:
                if cat.days < 1:
                    raise HTTPException(400, f"{field_name}.days must be at least 1")
                merged["days"] = cat.days
            if cat.keep_days is not None:
                if cat.keep_days < 1:
                    raise HTTPException(400, f"{field_name}.keep_days must be at least 1")
                merged["keep_days"] = cat.keep_days
            config.set_value(merged, "retention", field_name)
    if body.db_snapshots is not None:
        existing = config.get("retention", "db_snapshots") or {}
        merged = dict(existing) if isinstance(existing, dict) else {}
        for key in ("daily_keep", "weekly_keep", "monthly_keep"):
            val = getattr(body.db_snapshots, key)
            if val is not None:
                if val < 1:
                    raise HTTPException(400, f"db_snapshots.{key} must be at least 1")
                merged[key] = val
        config.set_value(merged, "retention", "db_snapshots")
    return JSONResponse({"ok": True})


@app.get("/api/settings/health-check")
async def api_settings_health_check_get():
    """Task #74 (Health Check panel). Returns the LAST daily result
    (health_check.get_last_result(), a plain read) rather than running the
    checks fresh - health_check.run() persists today's disk/process counts
    as the baseline for TOMORROW's day-over-day comparison, so re-running
    it every time this panel loads would corrupt that comparison (each
    open would overwrite "yesterday" with "just now"). null means it
    hasn't run yet in this install (a fresh install, or before the first
    scheduled_refresh.py cycle completes)."""
    return JSONResponse({"result": sanitize_surrogates(health_check.get_last_result())})


class PersonalLearningSurfacesBody(BaseModel):
    app_chat: Optional[bool] = None
    sent_mail: Optional[bool] = None
    sent_teams: Optional[bool] = None


class PersonalLearningSettingsBody(BaseModel):
    enabled: Optional[bool] = None
    surfaces: Optional[PersonalLearningSurfacesBody] = None


@app.get("/api/settings/personal-learning")
async def api_settings_personal_learning_get():
    """Personal Response Learning Settings section (task #45) - off by
    default. patterns_learned/last_run make the toggle's real effect visible
    rather than a black box; "Forget what's been learned" (the POST /forget
    route below) is the explicit, reversible undo."""
    surfaces = config.get("personal_learning", "surfaces") or {}
    return JSONResponse({
        "enabled": bool(config.get("personal_learning", "enabled")),
        "surfaces": {
            "app_chat": bool(surfaces.get("app_chat")) if isinstance(surfaces, dict) else False,
            "sent_mail": bool(surfaces.get("sent_mail")) if isinstance(surfaces, dict) else False,
            "sent_teams": bool(surfaces.get("sent_teams")) if isinstance(surfaces, dict) else False,
        },
        "patterns_learned": len(wg.list_response_patterns()),
        "last_run": wg.get_cursor("personal_learning", "last_run_date"),
        # task #59: was just a bare count before - shows WHAT's been learned,
        # per surface, top 5 by hit_count (list_response_patterns already
        # returns ORDER BY hit_count DESC).
        "top_patterns": {
            surface: [
                {"pattern_key": p["pattern_key"], "hit_count": p["hit_count"], "example_text": p["example_text"]}
                for p in wg.list_response_patterns(surface)[:5]
            ]
            for surface in ("app_chat", "sent_mail", "sent_teams")
        },
    })


@app.post("/api/settings/personal-learning")
async def api_settings_personal_learning_set(body: PersonalLearningSettingsBody):
    if body.enabled is not None:
        config.set_value(body.enabled, "personal_learning", "enabled")
    if body.surfaces is not None:
        existing = config.get("personal_learning", "surfaces") or {}
        merged = dict(existing) if isinstance(existing, dict) else {}
        for key in ("app_chat", "sent_mail", "sent_teams"):
            val = getattr(body.surfaces, key)
            if val is not None:
                merged[key] = val
        config.set_value(merged, "personal_learning", "surfaces")
    return JSONResponse({"ok": True})


@app.post("/api/settings/personal-learning/forget")
async def api_settings_personal_learning_forget():
    """Explicit, reversible-by-nature-of-being-obvious undo: clears every
    accumulated pattern. Does NOT touch the enabled/surfaces toggles - turning
    learning off and forgetting what's been learned are two separate actions,
    on purpose (matching the copy already shown in Settings)."""
    cleared = wg.clear_response_patterns()
    return JSONResponse({"ok": True, "cleared": cleared})


@app.post("/api/cockpit/settings")
async def api_cockpit_settings_set(body: CockpitSettingsBody):
    if body.chat_history_window < 5 or body.chat_history_window > 200:
        raise HTTPException(400, "chat_history_window must be between 5 and 200")
    config.set_value(body.chat_history_window, "cockpit", "chat_history_window")
    return JSONResponse({"ok": True, "chat_history_window": body.chat_history_window})


@app.post("/api/manager")
async def api_manager_set(body: ManagerBody):
    """Set the operator's name (+ optional @-tag) — the SHARED runtime write called by
    BOTH the compose naming step and the Settings 'You' field (last-confirm-wins).
    Writes config.manager.id/.tag (settings.json, hot-reloaded). No slot-id and no born
    discriminator is touched — the operator is not a worker; this is display identity."""
    name = (body.id or "").strip()
    if not name:
        raise HTTPException(400, "your name cannot be empty")
    if len(name) > 60:
        raise HTTPException(400, "name too long (max 60 characters)")
    tag = (body.tag or "").strip()
    if not tag:  # derive a sensible @-handle from the first token
        tag = re.sub(r"[^\w-]", "", name.split()[0].lower()) or "you"
    if len(tag) > 40:
        raise HTTPException(400, "tag too long (max 40 characters)")
    try:
        config.set_value(name, "manager", "id")
        config.set_value(tag, "manager", "tag")
    except Exception as e:
        raise HTTPException(500, f"failed writing operator identity: {e}")
    return JSONResponse({"ok": True, "id": name, "tag": tag, "display_name": name})


class CohortRenameBody(BaseModel):
    display_name: str


@app.get("/api/cohort")
async def api_cohort_get():
    """Read the cohort's stable internal id + its mutable display-name (Settings 'Team name')."""
    cid = None
    try:
        if _CONFIG_PATH.is_file():
            cid = json.loads(_CONFIG_PATH.read_text(encoding="utf-8")).get("cohort")
    except Exception:
        pass
    cid = cid or os.environ.get("SYMPHONY_COHORT") or os.environ.get("TEAM_COHORT") or "cohort"
    dn = (config.get("cohort_display_name") or "").strip() or cid
    return JSONResponse({"cohort_id": cid, "display_name": dn})


@app.post("/api/cohort/rename")
async def api_cohort_rename(body: CohortRenameBody):
    """Rename the cohort's DISPLAY name only. The stable internal identity (config.cohort —
    the install-time key that keys isolation, born markers, and the born-L3 / born-@all /
    fires-check discriminators) is NEVER changed by this, exactly as a worker rename moves
    the display-name but never the slot-id. The new label lands in settings.json
    (config.cohort_display_name, hot-reloaded); the dashboard picks it up immediately.
    (George Settings #7; naming-model §3 v1.6 invariant per Quinn/Mira/Atlas.)"""
    name = (body.display_name or "").strip()
    if not name:
        raise HTTPException(400, "cohort name cannot be empty")
    if len(name) > 80:
        raise HTTPException(400, "cohort name too long (max 80 characters)")
    try:
        config.set_value(name, "cohort_display_name")
    except Exception as e:
        raise HTTPException(500, f"failed writing cohort display name: {e}")
    return JSONResponse({"ok": True, "display_name": name})


def _restart_server():
    """Re-exec this server in place — marker-SAFE: touches NO ~/.cache markers, no worker
    sessions, no bus.db, no live ports; just replaces the process image on a short delay so
    the HTTP response flushes first. If the re-exec raises, the current process stays up (no
    silent death). SERVER-ONLY (Sage's constraint): worker/TO pids stay alive → their markers
    stay valid → zero orphans; they reconnect on next poll.

    Mechanism CONFIRMED (Abe, born launcher .command L308 + live pid, no supervisor):
      "$VENV_PY" -m uvicorn server_lean:app --host 127.0.0.1 --port $PORT   (backgrounded)
    Re-exec the SAME `python -m uvicorn` form so the reloaded process re-binds identically.
    execv INHERITS the current env (symphony_env.sh: SYMPHONY_HOME/COHORT_BASE/PORT/…) + cwd,
    so no re-source is needed. The `if sys.argv[1:]` guard falls back to a plain re-exec for
    any non-`-m` (e.g. future CLI-`uvicorn`) launch. No bus.db belt needed: bus writes are
    per-call open/close in autocommit (isolation_level=None) — no persistent handle or open
    txn survives to lose (verified bus.py:emit_event)."""
    import sys
    import threading

    def _go():
        time.sleep(0.4)  # let the HTTP response flush
        argv = ([sys.executable, "-m", "uvicorn", *sys.argv[1:]]
                if sys.argv[1:] else [sys.executable, *sys.argv])
        os.execv(sys.executable, argv)

    threading.Thread(target=_go, daemon=True).start()


@app.post("/api/server/restart")
async def api_server_restart():
    """Restart the Symphony server (Settings #6) — for the rare case a change needs a fresh
    boot to take hold. Marker-safe (see _restart_server). Returns before the process re-execs;
    the UI polls '/' until it's back."""
    _restart_server()
    return JSONResponse({"ok": True, "restarting": True})


# ═══════════════════════════════════════════════════════════════════════════
# Multi-cohort dashboard (root) — first version, UI iterated with George
# ═══════════════════════════════════════════════════════════════════════════
_DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Symphony</title>
<style>
:root{--bg:#f7f8fa;--card:#fff;--ink:#1a1f2b;--muted:#6b7280;--line:#e5e7eb;--accent:#3b5bdb;
 --ok:#2f9e44;--warn:#f08c00;--off:#e03131;}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{display:flex;align-items:center;gap:20px;padding:14px 22px;border-bottom:1px solid var(--line);background:var(--card)}
header h1{font-size:18px;margin:0;letter-spacing:.3px}
nav a{color:var(--muted);text-decoration:none;margin-right:16px;font-weight:500}
nav a:hover{color:var(--accent)}
nav a.active{color:var(--accent);font-weight:700}
.spacer{flex:1}
button.new{background:var(--accent);color:#fff;border:0;border-radius:7px;padding:8px 14px;font-weight:600;cursor:pointer}
main{padding:22px;display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;max-width:1100px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.card h2{font-size:16px;margin:0 0 2px}
.chead{display:flex;align-items:center;gap:8px;margin-bottom:12px}
.badge{font-size:11px;font-weight:700;padding:2px 8px;border-radius:20px;text-transform:uppercase;letter-spacing:.4px}
.badge.healthy{background:color-mix(in srgb,var(--ok) 18%,transparent);color:var(--ok)}
.badge.degraded{background:color-mix(in srgb,var(--warn) 18%,transparent);color:var(--warn)}
.badge.offline{background:color-mix(in srgb,var(--off) 18%,transparent);color:var(--off)}
.w{display:flex;align-items:center;gap:9px;padding:6px 0;border-top:1px solid var(--line)}
.dot{width:9px;height:9px;border-radius:50%;flex:none}
.dot.active,.dot.alive{background:var(--ok)}.dot.idle{background:var(--warn)}.dot.offline,.dot.unknown{background:var(--muted)}
.wname{font-weight:600}.warch{color:var(--muted);font-size:13px}
.muted{color:var(--muted);font-size:12px;margin-top:10px}
.stub{max-width:1100px;margin:0 auto 0;padding:6px 22px;color:var(--warn);font-size:12px}
</style></head><body>
<header>
  <h1>\U0001f6f8 Symphony</h1>
  <nav><a href="/" class="active">Dashboard</a><a href="/team_room">Team Room</a><a href="/projects">Projects</a><a href="/getting-started">Getting Started</a><a href="/help">Help</a><a href="/settings">Settings</a></nav>
  <span class="spacer"></span>
  <a class="new" href="/getting-started" style="text-decoration:none">+ Add a worker</a>
</header>
<div class="stub" id="stub"></div>
<main id="grid"></main>
<script>
async function load(){
  const r = await fetch('/api/cohorts'); const d = await r.json();
  document.getElementById('stub').textContent = d.stub ? 'Showing sample data (live cohort feed wires in at step-4).' : '';
  const g = document.getElementById('grid'); g.innerHTML='';
  const cohorts = d.cohorts||[];
  const totalWorkers = cohorts.reduce((s,c)=>s+((c.workers||[]).length),0);
  if(totalWorkers <= 1){
    const wc=document.createElement('div'); wc.className='card';
    wc.style.cssText='grid-column:1/-1;text-align:center;padding:34px 20px';
    wc.innerHTML='<div style="font-size:34px;margin-bottom:8px">&#128075;</div>'+
      '<h2 style="font-size:19px;margin:0 0 6px">Your team is just getting started</h2>'+
      '<div class="muted" style="margin:0 0 16px;font-size:14px">Only your coordinator so far &mdash; add your first worker to start building your team.</div>'+
      '<a class="new" href="/getting-started" style="text-decoration:none;display:inline-block">+ Add your first worker</a>';
    g.appendChild(wc);
  }
  for(const c of cohorts){
    const card=document.createElement('div'); card.className='card';
    const cs=(c.cohort_status||'unknown');
    let h=`<div class="chead"><h2>${c.display_name||c.cohort_id}</h2><span class="badge ${cs}">${cs}</span></div>`;
    for(const w of (c.workers||[])){
      const nm = w.display_name || w.worker;
      const role = w.archetype ? (' · ' + w.archetype) : '';
      h+=`<div class="w"><span class="dot ${w.status||'unknown'}"></span>`+
         `<span class="wname">${nm}</span><span class="warch">${role}</span></div>`;
    }
    h+=`<div class="muted">${(c.workers||[]).length} workers · id: ${c.cohort_id}</div>`;
    card.innerHTML=h; g.appendChild(card);
  }
}
load(); setInterval(load, 15000);
</script></body></html>"""


def _getting_started_html() -> str:
    # Live per-request fill of the operator's name from config.manager.id — the SAME
    # authoritative source the worker itself resolves (/api/manager), read at SERVE-time
    # (never a baked-in snapshot → no #6 name-drift). George 2026-07-20 pp_344554caf8.
    # welcome.html carries the token "{operator_intro}" at the start of the first-message
    # opener; the server owns the intro-clause construction so wording stays here, not
    # duplicated. Empty-case (bare/pre-onboarding install where manager.id isn't set yet):
    # graceful generic opener — same don't-guess/leak doctrine as the §0 empty-manager case.
    html = (HERE / "static" / "welcome.html").read_text(encoding="utf-8")
    name = (config.get("manager", "id") or "").strip()
    # Non-time-dependent greeting (George 2026-07-20): a served page can't know the reader's
    # local time, so avoid "Good morning" — use a neutral opener that reads right any hour.
    intro = f"This is {name} and " if name else "Hello — "
    return html.replace("{operator_intro}", intro)


@app.get("/getting-started", response_class=HTMLResponse)
async def getting_started():
    """The new-user onboarding on-ramp — walks setup of the first worker (the Team
    Builder), then hands off to the team-room conversation. Always reachable."""
    return HTMLResponse(_getting_started_html())


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    # The Dashboard tab ALWAYS shows the dashboard (George option-b, pp_ba3565511e): the
    # dashboard renders a friendly welcome/empty-state when only the coordinator is present,
    # guiding the user to compose their team; getting-started stays the guided "+ Add a worker"
    # flow. Only a truly-empty body (no coordinator at all — shouldn't happen post-install)
    # falls back to the onboarding on-ramp.
    try:
        n_workers = len([m for m in members_mod.list_members() if m.get("id")])
    except Exception:
        n_workers = 1
    if n_workers == 0:
        return HTMLResponse(_getting_started_html())
    return HTMLResponse(_DASHBOARD_HTML)
