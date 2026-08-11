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
import sys
import json
import time
import hashlib
import sqlite3
import subprocess
import threading
import asyncio
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
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

import workgraph_claims
import workgraph_reconcile
import workgraph_deepdive
import workgraph_store as wg
import workgraph_classify
import workgraph_nba
import workgraph_recommend
import workgraph_proactive
import skills_registry
import deep_links
import outlook_actions
import workgraph_assistant
import workgraph_signals
import rule_teaching
import personal_patterns
import workgraph_alerts
import workgraph_synthesis
import workgraph_projects
import workgraph_lessons
import workgraph_socrates
import workgraph_deadlines
import workgraph_redline
import workgraph_meetingprep
import workgraph_signal_trends
import workgraph_aristotle
import workgraph_export
import workgraph_commitments
import workgraph_asks_decisions
import workgraph_key_facts
import workgraph_repeat_signals
import health_check
import workgraph_suppliers
import workgraph_todo
import workgraph_focus
import workgraph_digest
import workgraph_party_review
import workgraph_relationships
import text_extract
import workgraph_discovery


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
# Task #211/M365 plugin (2026-08-06): the Outlook add-in task pane is served
# from https://localhost:3000 (a separate origin from this server, even
# though both are loopback - "localhost" and "127.0.0.1" are different
# hostnames to a browser) and needs to fetch this API's real data. Scoped to
# that one specific origin, not "*" - this server has no auth of its own
# (§2.4 of docs/design/M365_PLUGIN_INTEGRATION.md), so a wildcard would let
# any webpage Marc's browser visits read Jasper's data via JS, not just the
# add-in pane.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://localhost:3000"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
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
    # Real gap found 2026-08-03 while investigating Marc's repeat report that
    # project-panel buttons "still don't work" after multiple server-side
    # fixes had already landed and been verified working via direct API
    # calls: this route sent no Cache-Control header at all, while every
    # single fetch() call inside cockpit.html's own JS already uses
    # {cache: "no-store"} for API calls - the ~700KB HTML+JS page itself
    # (the thing that actually contains the button-wiring code) had no such
    # protection. A browser (or an already-open tab never re-fetching at
    # all) could keep running JS from before any given fix indefinitely,
    # which would look exactly like "still broken" no matter how many real
    # server-side fixes land. Same no-store discipline this page's own JS
    # already applies to its data, now applied to the page itself.
    response = templates.TemplateResponse(request, "cockpit.html", {
        "active_page": "cockpit",
        "manager_tag": config.get("manager", "tag") or config.get("manager", "id") or "manager",
        "now_iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    response.headers["Cache-Control"] = "no-store"
    return response


class WorkgraphIssueStatusBody(BaseModel):
    state: Optional[str] = None
    priority: Optional[str] = None
    actor: Optional[str] = None
    reason: Optional[str] = None


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
async def api_workgraph_issue_detail(issue_id: str, log_choice: bool = False):
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
    # 2026-07-29: populate evidence[].recommendations (list, task #15) +
    # .attachment (see workgraph_recommend.py) — the Detail pane's Progress
    # column already rendered these when present, but nothing ever produced
    # them.
    workgraph_recommend.attach_recommendations(evidence, attachments, time.time())
    deep_links.attach_deep_links(evidence)
    personal_patterns.attach_citations(evidence)
    workgraph_lessons.attach_learned([issue])
    workgraph_deadlines.attach_deadline_info([issue])
    workgraph_suppliers.attach_supplier_precedent(issue)
    # Enhancement #86: real, persisted PR#/PO# reference IDs for this issue -
    # same set workgraph_projects' own grouping veto already reads, not a
    # second live rescan.
    issue["reference_ids"] = sorted(workgraph_projects.reference_ids_for_issue(issue_id))
    # Enhancement #88: dollar value in play, already computed for Value-at-
    # risk/the Supplier Dashboard, just never attached to the issue itself.
    issue["value_found"] = workgraph_nba.value_amount_for_issue(issue_id)
    # Enhancement idea panel #1: real back-and-forth activity from data
    # already captured at ingest (raw_items.direction/occurred_ts) - never
    # read back out until now.
    issue["reply_latency"] = wg.compute_reply_latency_for_issue(issue_id)
    # Enhancement idea panel #2: same PR/PO number on 2 NOT-yet-grouped
    # issues - either a merge lagging behind, or a real cannot_merge veto
    # (v2.4) Marc should be able to see, not just infer.
    issue["reference_id_collisions"] = workgraph_projects.find_reference_id_collisions_for_issue(issue_id, issue)
    # Task #176: surface whether this issue's own grouping was ever human-
    # confirmed (wg.confirm_work_object_membership, called from
    # confirm_suggestion) vs still 'provisional' (an auto-merge/bridge-merge
    # the deterministic matcher made with no human review yet) - schema and
    # write-path already existed from task #121, just never read back out
    # anywhere until now, so Marc had no way to see which project
    # assignments are still unreviewed guesses.
    membership = wg.get_work_object_membership_exposure(issue_id) if issue.get("project_id") else None
    issue["grouping_membership_state"] = membership["membership_state"] if membership else None
    # Enhancement idea panel #4: show when this issue's classification
    # reflects Marc's own override, not the code default - previously only
    # visible in Settings' audit view, never on the issue it affects.
    issue["active_signal_overrides"] = wg.find_active_signal_overrides_for_issue(issue_id)
    # Enhancement idea panel #7: real calendar/meeting data (location,
    # agenda, attendee response status) for issues that came from or
    # reference a calendar event - previously discarded at ingest.
    issue["calendar_meetings"] = wg.list_calendar_meetings_for_issue(issue_id)
    # Enhancement idea panel #8: how many DIFFERENT people have pushed on
    # this, not just whether it's been re-raised at all (claims.escalated
    # is a flat boolean - see workgraph_nba.distinct_escalation_sender_count's
    # own docstring for why this is computed at the issue level).
    issue["distinct_escalation_senders"] = workgraph_nba.distinct_escalation_sender_count(
        wg.get_raw_items_for_issue(issue_id)
    )
    # Enhancement idea panel #10: a to='waiting' transition with a real
    # actor recorded (a deliberate snooze) vs an organic wait the automated
    # classifier set with no actor - list_issue_state_history is already a
    # single-issue, cheap query (not a DB-wide scan like #9's), safe here.
    issue["snooze_history"] = workgraph_nba.snooze_history_from_state_history(wg.list_issue_state_history(issue_id))
    # Enhancement idea panel #12: how many distinct open asks are currently
    # stacked on this one issue - list_open_claims_for_issue is a cheap
    # single-issue query, same as #10's above.
    issue["ask_density"] = workgraph_nba.ask_density_for_issue(
        wg.list_open_claims_for_issue(issue_id, claim_type="ask")
    )
    # Enhancement idea panel #13: does this issue's chosen deal-value figure
    # also show up, independently, in a real attachment's extracted text
    # (E6) - not just somewhere in the same candidate pool the regex already
    # draws from. Reuses raw_items already fetched above; value_found was
    # just computed a few lines up.
    issue["value_corroborated_by_attachment"] = workgraph_nba.attachment_corroborates_value(
        wg.get_raw_items_for_issue(issue_id), issue["value_found"]
    )
    # Enhancement idea panel #16: distinct preferred-tier dollar figures
    # across this issue's own messages - len() >= 2 means two messages
    # disagree about the deal's own value, not just "many numbers appear
    # somewhere in the thread."
    issue["conflicting_value_figures"] = workgraph_nba.conflicting_value_figures_for_issue(
        wg.get_raw_items_for_issue(issue_id)
    )
    # Part E1 (2026-07-30): unified ranked candidate actions - read-time
    # only, see workgraph_nba.candidate_actions' own docstring. Falls back
    # to project_synthesis's suggested_actions when this issue's own
    # synthesis has none of its own - task #21 moved the actual fallback
    # decision (prefer synthesis only when it has real suggested_actions,
    # not just when the dict itself is truthy) into candidate_actions()
    # itself so it's unit-tested there instead of as inline call-site logic.
    issue["candidate_actions"] = workgraph_nba.candidate_actions(issue, evidence, synthesis, project_synthesis)
    # Part E2 (2026-07-30): log what was offered - ONLY when the caller is
    # a real, intentional detail-pane view (log_choice=true), never the
    # Inbox/Project-detail background bucketing prefetch, which calls this
    # exact endpoint for every issue in the list on every 20s poll. Fixed
    # same-day after catching it live: without this guard, one poll cycle
    # would have written a choice-log row for every issue in the database
    # regardless of whether Marc ever looked at it - diluting the whole
    # point of this table (what was offered when a real decision was
    # actually being made). Once per open (not-yet-chosen) window even
    # when log_choice is true - avoids spamming a new row on every repeat
    # view of the same issue.
    if log_choice and wg.get_most_recent_open_choice_log(issue_id) is None:
        wg.create_nba_choice_log(
            issue_id=issue_id,
            offered_json=json.dumps(issue["candidate_actions"], default=str),
            scoring_inputs_json=json.dumps({
                "state": issue.get("state"), "category": issue.get("category"),
                "priority_score": issue.get("priority_score"), "value_found": issue.get("value_found"),
                "confidence_tier": issue.get("confidence_tier"),
                "has_unmet_prerequisite": issue.get("has_unmet_prerequisite"),
                "lesson_id_cited": issue.get("lesson_id_cited"),
            }, default=str),
        )
    # Checklist rework (2026-08-01): asks/decisions/commitments/repeat_signals
    # now carry a real raw_item_id (see each module's own docstring) - attach
    # the same real deep link mechanism the Progress-zone evidence rows
    # already use (Ariba/Adobe Sign/Outlook), so a checklist item is never
    # floating free of the email that produced it. attach_deep_links mutates
    # in place and only needs a "raw_item_id" key on each dict, regardless of
    # what else the dict carries.
    checklist_asks = workgraph_asks_decisions.list_asks_for_issue(issue_id)
    checklist_decisions = workgraph_asks_decisions.list_decisions_for_issue(issue_id)
    checklist_commitments = workgraph_commitments.list_commitments_for_issue(issue_id)
    checklist_repeat_signals = workgraph_repeat_signals.list_repeat_signals_for_issue(issue_id)
    # Real duplicate caught by Marc's live screenshot: curator's own synthesis
    # suggested_actions has no raw_item_id (candidate_actions' own docstring -
    # "synthesis is project/issue-aggregate"), so a synthesis candidate that
    # just restates "approve PR<n>" ends up as its own unscoped "general"
    # checklist row, duplicating the real ask that already says the same
    # thing with the actual requester/amount. Suppress a synthesis candidate
    # whose label references the SAME PR/PO (version-insensitive) as an
    # already-real ask/decision/commitment/repeat-signal row - the real row
    # already tells Marc what to do; the restatement adds nothing.
    def _reference_bases_from(items: list, text_field: str) -> set:
        bases = set()
        for it in items:
            m = workgraph_signals.REFERENCE_ID_RE.search(it.get(text_field) or "")
            if m:
                bases.add(workgraph_signals.reference_base(m.group(0).upper()))
        return bases
    covered_reference_bases = (
        _reference_bases_from(checklist_asks, "text")
        | _reference_bases_from(checklist_decisions, "text")
        | _reference_bases_from(checklist_commitments, "text")
        | _reference_bases_from(checklist_repeat_signals, "ask_text")
    )
    if covered_reference_bases:
        def _duplicates_existing_reference(c: dict) -> bool:
            if c.get("source_surface") != "synthesis" or c.get("raw_item_id"):
                return False
            m = workgraph_signals.REFERENCE_ID_RE.search(c.get("label") or "")
            return bool(m) and workgraph_signals.reference_base(m.group(0).upper()) in covered_reference_bases
        issue["candidate_actions"] = [c for c in issue["candidate_actions"] if not _duplicates_existing_reference(c)]
    # Task #44: drop any row a dismissed item_key still matches, so a real
    # dismissal actually stops the item from reappearing (the "negative
    # signal" the recommend/checklist engine needs to respect) rather than
    # just fading it out client-side for the current page view only.
    dismissed_keys = wg.list_dismissed_checklist_keys(issue_id)
    if dismissed_keys:
        def _not_dismissed(items: list, kind: str, text_field: str) -> list:
            return [
                it for it in items
                if wg.checklist_item_key(kind, it.get("raw_item_id"), it.get(text_field, "")) not in dismissed_keys
            ]
        checklist_asks = _not_dismissed(checklist_asks, "ask", "text")
        checklist_decisions = _not_dismissed(checklist_decisions, "decision", "text")
        checklist_commitments = _not_dismissed(checklist_commitments, "commitment", "text")
        checklist_repeat_signals = _not_dismissed(checklist_repeat_signals, "repeat", "ask_text")
    deep_links.attach_deep_links(checklist_asks)
    deep_links.attach_deep_links(checklist_decisions)
    deep_links.attach_deep_links(checklist_commitments)
    deep_links.attach_deep_links(checklist_repeat_signals)
    # Task #49 (per-checklist-item Aristotle gating, docs/design/
    # ARISTOTLE_PER_ROW_GATING.md): every unsatisfied prerequisite for this
    # issue, keyed by the raw_item_id that raised it - a map instead of a
    # field on each of the four checklist dicts above, so this needed one
    # extra lookup here rather than touching four separate fetch call sites.
    # issue["has_unmet_prerequisite"] (set by recompute_all, unchanged)
    # stays the aggregate "is anything gated" signal; this is the added
    # per-row "which thing" signal - one doesn't replace the other.
    gated_raw_items = {
        g["raw_item_id"]: g for g in workgraph_aristotle.check_prerequisites_all(
            issue_id, wg.get_raw_items_for_issue(issue_id))
        if g.get("raw_item_id") is not None
    }
    return JSONResponse({"issue": sanitize_surrogates(issue), "evidence": sanitize_surrogates(evidence),
                        "gated_raw_items": sanitize_surrogates(gated_raw_items),
                        "pending_actions": sanitize_surrogates(pending), "tasks": sanitize_surrogates(tasks),
                        "state_history": sanitize_surrogates(state_history),
                        "parties": sanitize_surrogates(parties), "project": sanitize_surrogates(project),
                        "synthesis": sanitize_surrogates(synthesis),
                        "project_synthesis": sanitize_surrogates(project_synthesis),
                        "attachments": sanitize_surrogates(attachments),
                        # Enhancement #87: this issue's own asks/decisions/key
                        # facts - the same real extraction fields the global
                        # rollup cards already surface, scoped to just this
                        # issue rather than every open issue.
                        "asks": sanitize_surrogates(checklist_asks),
                        "decisions": sanitize_surrogates(checklist_decisions),
                        "key_facts": sanitize_surrogates(workgraph_key_facts.list_key_facts_for_issue(issue_id)),
                        "commitments": sanitize_surrogates(checklist_commitments),
                        # Part D (2026-07-30): repeat-ask/escalation signals
                        # curator recorded for this issue - see
                        # workgraph_repeat_signals.py.
                        "repeat_signals": sanitize_surrogates(checklist_repeat_signals),
                        # Checklist rework: other open issues sharing a real
                        # PR/PO reference with this one - a relationship
                        # fact, not a claim about which blocks which (see
                        # workgraph_projects.related_open_issues_by_reference).
                        "related_by_reference": sanitize_surrogates(
                            workgraph_projects.related_open_issues_by_reference(issue_id))})


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
    # Task #36: real issue_id already resolved above - "JW-<issue-id>" is
    # the same reference-tag format the stakeholder mailto: compose already
    # uses; None (no tag at all) when this raw_item isn't linked to an
    # issue yet, never a fabricated placeholder.
    ref_tag = f"JW-{raw_item['issue_id']}" if raw_item.get("issue_id") else None
    try:
        result = await asyncio.to_thread(outlook_actions.draft_reply, entry_id, body.reply_all, ref_tag)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return JSONResponse(result)


class HeroDraftReplyBody(BaseModel):
    raw_item_id: int


@app.post("/api/addin/hero-draft-reply")
async def api_addin_hero_draft_reply(body: HeroDraftReplyBody):
    """Marc's own request: the drawer hero's "draft a reply" button should
    open a REAL, review-ready draft, not a blank one - reuses workgraph_
    proactive's already-deterministic _draft_status_update_body (curator's
    own synthesis summary + this issue's own open asks - the same body-
    drafting logic task #287's automatic proactive path already uses,
    just triggered on demand here instead). reply_all=True is Marc's own
    framing ("include those who the user should reply to") - the whole
    original thread's recipients, not just whoever happened to send this
    one email."""
    raw_item = wg.get_raw_item(body.raw_item_id)
    if raw_item is None:
        raise HTTPException(404, f"no such raw_item: {body.raw_item_id}")
    entry_id = raw_item.get("entry_id")
    if not entry_id:
        raise HTTPException(400, "this item has no stored EntryID (ingested before task #43, or not a mail item)")
    issue_id = raw_item.get("issue_id")
    if issue_id is None:
        raise HTTPException(400, "this item isn't linked to a tracked issue yet")
    ref_tag = f"JW-{issue_id}"
    draft_body = workgraph_proactive._draft_status_update_body(issue_id)
    try:
        result = await asyncio.to_thread(
            outlook_actions.draft_reply, entry_id, True, ref_tag, draft_body, False,
        )
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return JSONResponse(result)


class DraftForwardBody(BaseModel):
    raw_item_id: int


@app.post("/api/action/draft-forward")
async def api_action_draft_forward(body: DraftForwardBody):
    """Task #16 - mirrors draft-reply above (outlook_actions.draft_forward:
    Forward() + Display(), never Send()), same asyncio.to_thread guard for
    the same reason - a blocking COM round-trip in an async def handler
    freezes every request on this single-worker server."""
    raw_item = wg.get_raw_item(body.raw_item_id)
    if raw_item is None:
        raise HTTPException(404, f"no such raw_item: {body.raw_item_id}")
    entry_id = raw_item.get("entry_id")
    if not entry_id:
        raise HTTPException(400, "this item has no stored EntryID (ingested before task #43, or not a mail item)")
    ref_tag = f"JW-{raw_item['issue_id']}" if raw_item.get("issue_id") else None  # task #36, see draft-reply above
    try:
        result = await asyncio.to_thread(outlook_actions.draft_forward, entry_id, ref_tag)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return JSONResponse(result)


class AssistantMessageBody(BaseModel):
    message: str
    session_id: Optional[str] = None
    reset: bool = False


@app.post("/api/assistant/message")
async def api_assistant_message(body: AssistantMessageBody):
    """The Outlook pane's live chat box. Spawns a real `claude -p` turn
    (workgraph_assistant.ask) with tools into Jasper's own API plus the
    already-authorized M365 connector - a genuine conversational turn, not
    the flat FTS5 search this route replaces. asyncio.to_thread for the
    same reason as the action routes above: this blocks on a subprocess
    for up to ~2 minutes, and would otherwise freeze every other request
    on this single-worker server.

    Task #232: session_id is now optional in a real sense, not just in
    the type signature - omitting it (the normal case now) continues
    whatever conversation is persisted server-side rather than always
    starting fresh. reset=True explicitly starts a new one."""
    if not body.message or not body.message.strip():
        raise HTTPException(400, "message is required")
    # Reset before, read after: whichever of jasper_focus_email/
    # jasper_focus_party/jasper_focus_project this turn's tool calls hit
    # (if any) will have called _record_referenced_project - reading the
    # cursor back here is how "show me the Kinaxis drawer" link gets a
    # real project_id instead of the client guessing one out of the reply
    # text.
    wg.set_cursor(*_REFERENCED_PROJECTS_CURSOR, "[]")
    result = await asyncio.to_thread(
        workgraph_assistant.ask, body.message.strip(), body.session_id, reset=body.reset,
    )
    referenced_raw = wg.get_cursor(*_REFERENCED_PROJECTS_CURSOR)
    result["related_projects"] = json.loads(referenced_raw) if referenced_raw else []
    return JSONResponse(result)


@app.get("/api/assistant/session")
async def api_assistant_session():
    """Task #232 - lets the task pane check whether an ongoing persisted
    conversation exists (e.g. right after a reload) without needing to
    send a throwaway message just to find out.

    Task #271: also returns the visible chat turns logged so far
    (workgraph_store.list_assistant_chat_turns) - the actual fix for the
    New Outlook pane-reload bug. A --resume'd session_id alone only kept
    Claude's own reasoning context alive; the task pane's rendered
    bubbles lived in that page's DOM and vanished on the process restart
    New Outlook does on a security-context change. On load, the client
    calls this route and, if turns exist, re-renders them instead of
    silently falling back to the default view with no sign a
    conversation was ever in progress."""
    return JSONResponse({
        "session_id": wg.get_assistant_session_id(),
        "turns": wg.list_assistant_chat_turns(),
    })


@app.post("/api/assistant/reset")
async def api_assistant_reset():
    """A real "start a new conversation" action that costs nothing - pure
    store-layer state clear, no claude -p subprocess spawned (the
    message-route's own reset=True field also does this, but only as a
    side effect of an actual paid turn; this is the free, direct path for
    a UI "New conversation" control). Clears the visible chat log too
    (task #271) - a "new conversation" that still showed the old
    transcript on the next reload would defeat the point of resetting."""
    wg.clear_assistant_session_id()
    wg.clear_assistant_chat_turns()
    return JSONResponse({"ok": True})


class ComposeNewBody(BaseModel):
    issue_id: str
    to_emails: list[str]
    body: str = ""
    attachment_paths: list[str] = Field(default_factory=list)


@app.post("/api/action/compose-new")
async def api_action_compose_new(body: ComposeNewBody):
    """Task #35 - creates a REAL Outlook draft new-mail item addressed to
    the selected stakeholders (outlook_actions.compose_new: CreateItem(0) +
    Display(), never Send()) - replaces the interim client-only mailto:
    link the cockpit UI used while this wasn't built yet. Same
    asyncio.to_thread guard as the other Outlook actions above, same
    reason - this blocks for a full COM round-trip on a single-worker
    server."""
    issue = wg.get_issue(body.issue_id)
    if issue is None:
        raise HTTPException(404, f"no such issue: {body.issue_id}")
    if not body.to_emails:
        raise HTTPException(400, "to_emails is required")
    # Same "JW-<issue-id>" tag format draft-reply/draft-forward already use
    # (task #36) - display_title (task #52's derived_title, when curator or
    # the deterministic backfill has set one) preferred over the raw
    # subject line, same precedence the rest of the UI already applies
    # everywhere (wg.get_issue() alone only returns the issues table row,
    # not the synthesis-joined display_title - fetched separately here).
    synthesis = wg.get_synthesis("issue", body.issue_id)
    display_title = (synthesis or {}).get("derived_title") or issue.get("title") or ""
    subject = f"{display_title} - Ref: JW-{body.issue_id}"
    try:
        result = await asyncio.to_thread(
            outlook_actions.compose_new, body.to_emails, subject, body.body, body.attachment_paths,
        )
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return JSONResponse(result)


class DraftReviewRequestBody(BaseModel):
    issue_id: str
    to_emails: list[str]
    attachment_id: int
    message: str = ""


@app.post("/api/action/draft-review-request")
async def api_action_draft_review_request(body: DraftReviewRequestBody):
    """Task #35 follow-on (2026-08-08): the assistant-facing 'share this
    output and ask them to review' action - real, but honestly scoped: a
    real Outlook draft with the file ATTACHED (same COM path as compose-
    new above, no new M365/Graph permission), not native SharePoint
    co-authoring (see task #285 - that needs a real, separate permission
    grant this route can't manufacture).

    Takes attachment_id rather than a raw filesystem path on purpose - an
    LLM-driven caller should never need to know or guess a real path on
    this machine. Resolved and ownership-checked against the issue's own
    attachments here, server-side, same discipline as every other route
    that turns an id into a real action rather than trusting client input
    directly."""
    if wg.get_issue(body.issue_id) is None:
        raise HTTPException(404, f"no such issue: {body.issue_id}")
    if not body.to_emails:
        raise HTTPException(400, "to_emails is required")
    owned_ids = {a["id"] for a in wg.list_attachments_for_issue(body.issue_id)}
    if body.attachment_id not in owned_ids:
        raise HTTPException(404, f"attachment {body.attachment_id} is not attached to issue {body.issue_id}")
    attachment = wg.get_attachment(body.attachment_id)
    abs_path = paths.DATA_DIR / attachment["stored_path"]
    if not abs_path.is_file():
        raise HTTPException(404, f"attachment file missing on disk: {abs_path}")

    synthesis = wg.get_synthesis("issue", body.issue_id)
    issue = wg.get_issue(body.issue_id)
    display_title = (synthesis or {}).get("derived_title") or issue.get("title") or ""
    subject = f"Please review: {display_title} - Ref: JW-{body.issue_id}"
    review_body = body.message or f"Hi,\n\nCould you take a look at the attached {attachment['filename']} and share your thoughts?\n\nThanks,\nMarc"
    try:
        result = await asyncio.to_thread(
            outlook_actions.compose_new, body.to_emails, subject, review_body, [str(abs_path)],
        )
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


def _closing_evidence_warning(issue_id: str, new_state: str) -> Optional[str]:
    """Real incident (2026-08-01): two financial issues (marc-014 $53.7M,
    marc-185 $111.7M) were manually flipped to 'done' with zero closing
    evidence - Jasper's own evidence-derived state for both was still
    'active'. Manually overriding the derived state is legitimate and
    intentional by design (recompute_issue_state's own docstring: "a human's
    manual close/archive is... not silently reverted" - Marc sees real
    approvals Jasper never does), so this never blocks the close. It only
    returns an advisory string when the evidence disagrees, so the caller
    (and eventually the UI) has something to show/log instead of a silent,
    unattributed, unexplained state flip being the only trace it happened."""
    if new_state not in ("done", "noise-archived", "dismissed"):
        return None
    derived = workgraph_classify.derive_target_state(issue_id)
    if derived == "active":
        return "Jasper's own evidence still shows an unresolved ask on this issue - closing it anyway."
    if derived == "waiting":
        return "Jasper's own evidence shows this is still waiting on someone else - closing it anyway."
    return None


def _mark_issue_emails_read_best_effort(issue_id: str) -> None:
    """Task #275 - fires only on a genuine 'done' close (not noise-archived/
    dismissed, which don't mean the work actually got resolved), and only
    from the manual close routes below, deliberately NOT from the
    automatic recompute_issue_state path that runs on every classify pass -
    that runs 2x per scheduled_refresh cycle across the whole corpus, and
    piling more live Outlook COM calls onto an already-fragile automated
    path is exactly the kind of thing that contributed to a real stuck-
    pipeline incident (task #278). Best-effort and silent on failure - a
    missing/stale entry_id or a COM error here must never block or fail
    the actual state-change response; the issue is closed either way."""
    for item in wg.get_raw_items_for_issue(issue_id):
        entry_id = item.get("entry_id")
        if not entry_id:
            continue
        try:
            outlook_actions.mark_read(entry_id)
        except Exception:
            pass


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
    warning = _closing_evidence_warning(issue_id, body.state) if body.state else None
    actor = body.actor or (config.get("manager", "id") or "unknown")
    wg.update_issue(issue_id, actor=actor, **fields)
    # Part E2 (2026-07-30): a real, deterministic action was just taken
    # against this issue - resolve whichever candidate list was most
    # recently offered, if any.
    # Phase 0 fix (D12, 2026-08-03): a bare state change used to mark
    # whichever offer was most recently open "chosen" unconditionally - a
    # generic Issue state change (e.g. Marc archiving something for an
    # unrelated administrative reason) is NOT the same as accepting one of
    # curator's offered candidates, and doing so corrupted nba_choice_log's
    # whole purpose (a real record of what was offered vs. actually acted
    # on). Only mark chosen when the new state actually names one of this
    # offer's real candidate kinds - otherwise the offer is left untouched
    # (still 'offered', to be resolved later by a real matching action or
    # the expiry sweep below).
    open_log = wg.get_most_recent_open_choice_log(issue_id)
    if open_log is not None and body.state is not None:
        offered = json.loads(open_log["offered_json"] or "[]")
        offered_kinds = {c.get("kind") for c in offered if isinstance(c, dict)}
        if body.state in offered_kinds:
            wg.mark_choice_log_chosen(open_log["id"], chosen_action_kind=body.state)
    if body.state == "done":
        # Fire-and-forget (task #275) - never awaited, so a slow/cold-start
        # Outlook COM call can't delay this response or risk timing the
        # route out, same reasoning as the automatic-path exclusion above.
        asyncio.create_task(asyncio.to_thread(_mark_issue_emails_read_best_effort, issue_id))
    result = {"ok": True, "issue": sanitize_surrogates(wg.get_issue(issue_id))}
    if warning:
        result["warning"] = warning
    return JSONResponse(result)


class ChecklistItemDismissBody(BaseModel):
    kind: str
    raw_item_id: Optional[int] = None
    text: str
    actor: Optional[str] = None


@app.post("/api/workgraph/issues/{issue_id}/checklist/dismiss")
async def api_workgraph_checklist_dismiss(issue_id: str, body: ChecklistItemDismissBody):
    """Task #44: a real, persisted 'this checklist item was wrong/not needed'
    outcome for one ask/decision/commitment/repeat-signal row, distinct from
    marking the whole issue done. Server derives the item_key itself (never
    trusts a client-computed key) so the dismissal is keyed exactly the same
    way the issue-detail endpoint re-derives keys to filter dismissed rows
    back out on the next read - see workgraph_store.checklist_item_key's
    docstring for the "stable enough, not a real id" caveat."""
    if wg.get_issue(issue_id) is None:
        raise HTTPException(404, f"no such issue: {issue_id}")
    actor = body.actor or (config.get("manager", "id") or "unknown")
    item_key = wg.dismiss_checklist_item(
        issue_id=issue_id, kind=body.kind, raw_item_id=body.raw_item_id,
        text=body.text, actor=actor,
    )
    # Section 12.3: best-effort sync to the claims ledger, if this checklist
    # row corresponds to a real open claim - checklist_dismissals above
    # stays the authoritative record either way, this closes a real gap
    # (nothing before this ever moved a claim out of 'open').
    workgraph_claims.sync_checklist_action_to_claim(
        issue_id=issue_id, kind=body.kind, text=body.text, status="dismissed", actor=actor,
    )
    return JSONResponse({"ok": True, "item_key": item_key})


class ChecklistItemDoneBody(BaseModel):
    kind: str
    raw_item_id: Optional[int] = None
    text: str
    actor: Optional[str] = None


@app.post("/api/workgraph/issues/{issue_id}/checklist/done")
async def api_workgraph_checklist_done(issue_id: str, body: ChecklistItemDoneBody):
    """Task #59: real persistence for the checklist row's "Mark done" icon -
    same mechanics as the /checklist/dismiss endpoint above, distinct
    outcome (see workgraph_store.mark_checklist_item_done)."""
    if wg.get_issue(issue_id) is None:
        raise HTTPException(404, f"no such issue: {issue_id}")
    actor = body.actor or (config.get("manager", "id") or "unknown")
    item_key = wg.mark_checklist_item_done(
        issue_id=issue_id, kind=body.kind, raw_item_id=body.raw_item_id,
        text=body.text, actor=actor,
    )
    # Section 12.3: same best-effort claims-ledger sync as the dismiss route.
    workgraph_claims.sync_checklist_action_to_claim(
        issue_id=issue_id, kind=body.kind, text=body.text, status="done", actor=actor,
    )
    return JSONResponse({"ok": True, "item_key": item_key})


class BulkIssueStatusBody(BaseModel):
    issue_ids: list[str]
    state: str
    actor: Optional[str] = None


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
    if body.state not in ("done", "noise-archived", "waiting", "dismissed"):
        raise HTTPException(400, f"unsupported bulk triage state: {body.state!r}")
    if not body.issue_ids:
        raise HTTPException(400, "issue_ids must be a non-empty list")
    actor = body.actor or (config.get("manager", "id") or "unknown")
    updated, missing, warnings = [], [], {}
    for issue_id in body.issue_ids:
        if wg.get_issue(issue_id) is None:
            missing.append(issue_id)
            continue
        warning = _closing_evidence_warning(issue_id, body.state)
        if warning:
            warnings[issue_id] = warning
        wg.update_issue(issue_id, state=body.state, actor=actor)
        updated.append(issue_id)
        if body.state == "done":
            asyncio.create_task(asyncio.to_thread(_mark_issue_emails_read_best_effort, issue_id))
    result = {"ok": True, "updated": updated, "missing": missing}
    if warnings:
        result["warnings"] = warnings
    return JSONResponse(result)


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


@app.get("/api/workgraph/actions/ranked")
async def api_actions_ranked(limit: int = workgraph_nba.DEFAULT_RANK_ACTIONS_LIMIT):
    """Design doc Section 11 (Phase 4/NBA v2): every open ask/commitment
    claim owned by Marc, ranked globally across every open issue - not one
    action per issue, the real gap Section 11.1 found in the existing
    per-issue candidate_actions. New, additive, read-only - does NOT
    change issues.priority_score or the existing Inbox sort (Section
    11.5): this is a new surface pending Marc's own review before
    anything wires into the primary worklist view."""
    return JSONResponse({"actions": sanitize_surrogates(workgraph_nba.rank_actions(limit=limit))})


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
    issue_ids = [i["id"] for i in issues]

    # Corrected pipeline Phase D (2026-08-05): raw cluster membership,
    # additive to `issues` above - never rendered by cockpit.html (clusters
    # stay invisible to Marc's UI by construction, same as every other
    # issue-only reader in this file), but this is the ONE route curator's
    # SYNTHESIS_ROUTINE already reads for "every issue in this project" -
    # without a real source for cluster content here, a Phase-C-promoted
    # project made up entirely of clusters would look completely empty to
    # curator, with nothing to extract real issues from at all.
    clusters = wg.list_clusters_for_project(project_id)
    cluster_ids = [c["id"] for c in clusters]
    cluster_evidence_by_id = wg.list_evidence_for_issues(cluster_ids)
    cluster_raw_items_by_id = {cid: wg.get_raw_items_for_issue(cid) for cid in cluster_ids}
    for c in clusters:
        c["evidence"] = cluster_evidence_by_id.get(c["id"], [])
        c["raw_item_ids"] = [r["id"] for r in cluster_raw_items_by_id.get(c["id"], [])]
    has_confirmed_grouping = wg.project_has_confirmed_grouping(project_id)
    # Design doc Section 12.8: this GET is the real render point - the
    # project itself and every member issue are genuinely shown to Marc
    # here, unlike wg.list_issues_for_project's OTHER callers (deep_dive
    # picker, aristotle detection, internal aggregation), which never
    # display anything and must not advance exposure_state.
    wg.advance_work_object_exposure_state(project_id, "shown_in_project")
    for iid in issue_ids:
        wg.advance_work_object_exposure_state(iid, "shown_in_project")
    open_issues = [i for i in issues if i["state"] in ("active", "waiting", "blocked")]
    open_ids = [i["id"] for i in open_issues]

    # Project-detail redesign (2026-07-31, Marc's own design brief):
    # real rollups across every member issue, reusing the exact same
    # per-issue readers already built this session - no new extraction,
    # just aggregation at a different scope. Each per-issue reader returns
    # bare strings (correct for the issue-level panel, where "which issue"
    # is already implied by context) - at project scope that context is
    # gone the moment a project has 2+ issues, so every entry gets tagged
    # with issue_id/issue_title here at the aggregation point rather than
    # changing the per-issue readers' own return shape for their existing
    # (correctly-scoped) callers.
    asks, decisions, key_facts, commitments, repeat_signals = [], [], [], [], []
    title_by_id = {i["id"]: (i.get("display_title") or i["title"]) for i in issues}
    # Fixed 2026-08-02 (Marc's direct report: project-detail buttons "reacting
    # very slowly") - this loop used to call list_asks_for_issue/
    # list_decisions_for_issue/list_key_facts_for_issue/
    # list_commitments_for_issue/list_repeat_signals_for_issue ONE ISSUE AT A
    # TIME, each independently re-querying the same underlying
    # raw_item_extractions table via list_extractions_for_issues([iid]) -
    # despite that function already being the batched-safe primitive its own
    # docstring advertises. For a 4-issue project that was 20 separate DB
    # round-trips instead of 4. Confirmed live via curl timing before this
    # fix: GET /api/workgraph/projects/{id} alone took ~3.1s, and every
    # button click on this page chains a POST + this same GET behind it -
    # a real multi-second delay per click, not a missing click handler.
    # Batched *_for_issues() siblings (same modules) now do exactly one
    # extractions fetch per field across the WHOLE project.
    asks_by_issue = workgraph_asks_decisions.list_asks_for_issues(issue_ids)
    decisions_by_issue = workgraph_asks_decisions.list_decisions_for_issues(issue_ids)
    key_facts_by_issue = workgraph_key_facts.list_key_facts_for_issues(issue_ids)
    commitments_by_issue = workgraph_commitments.list_commitments_for_issues(issue_ids)
    repeat_signals_by_issue = workgraph_repeat_signals.list_repeat_signals_for_issues(issue_ids)
    for iid in issue_ids:
        issue_title = title_by_id.get(iid, iid)
        # Checklist rework (2026-08-01): asks/decisions/commitments now carry
        # a real raw_item_id (see each module's own docstring) - kept through
        # here too so project-scoped items can get the same real deep link
        # the issue-detail endpoint already attaches, not just the text.
        asks.extend({"issue_id": iid, "issue_title": issue_title, "text": t["text"], "raw_item_id": t["raw_item_id"]}
                    for t in asks_by_issue.get(iid, []))
        decisions.extend({"issue_id": iid, "issue_title": issue_title, "text": t["text"], "raw_item_id": t["raw_item_id"]}
                    for t in decisions_by_issue.get(iid, []))
        key_facts.extend({"issue_id": iid, "issue_title": issue_title, "text": t}
                    for t in key_facts_by_issue.get(iid, []))
        commitments.extend({"issue_id": iid, "issue_title": issue_title, "text": t["text"], "raw_item_id": t["raw_item_id"]}
                    for t in commitments_by_issue.get(iid, []))
        for rs in repeat_signals_by_issue.get(iid, []):
            rs["issue_id"] = iid
            rs["issue_title"] = issue_title
            repeat_signals.append(rs)
    deep_links.attach_deep_links(asks)
    deep_links.attach_deep_links(decisions)
    deep_links.attach_deep_links(commitments)
    deep_links.attach_deep_links(repeat_signals)
    parties = workgraph_projects.aggregate_parties_for_project(project_id)
    value_by_issue = workgraph_nba.value_amounts_for_issues(open_ids)
    parties_by_issue = wg.list_parties_for_issues(issue_ids)
    # Detail Panel Refined (task #124 follow-on, 2026-08-01): the per-issue
    # values were already computed above just to sum them - attaching them
    # back onto each issue costs nothing extra and is what the Project tab's
    # "issues, priority order" list needs to show a real dollar figure per row.
    # Fixed 2026-08-02 (Marc's direct report: project-detail buttons
    # "reacting very slowly"): each issue's own parties (for its row's party
    # chips) used to require the CLIENT to fetch the full
    # GET /api/workgraph/issues/{id} payload separately, once per member
    # issue - real, measured cost on a server where every request currently
    # blocks the same single event loop (see list_parties_for_issues' own
    # docstring). Attaching them here means the one project-detail response
    # already has everything the issue-row list needs.
    for i in issues:
        i["value_found"] = value_by_issue.get(i["id"])
        i["parties"] = sanitize_surrogates(parties_by_issue.get(i["id"], []))
    # "What the next owner must know" needs one real headline action, not
    # a full per-issue candidate_actions() call for every member (that
    # would mean N extra full detail fetches) - the highest-priority open
    # issue's own already-computed nba_reason is the cheap, honest answer.
    top_issue = max(open_issues, key=lambda i: i.get("priority_score") or 0, default=None)
    # Thread & Web (task #124 follow-on): every real evidence row across every
    # OPEN member issue (closed/noise-archived issues stay out, same scope as
    # value_found/gated/hard-deadline counts above), tagged with issue_id/
    # issue_title so a merged cross-issue feed can show which issue each item
    # belongs to. Deep links attached the same way the single-issue detail
    # panel already does (deep_links.attach_deep_links) - no separate logic.
    evidence_by_issue = wg.list_evidence_for_issues(open_ids)
    thread_feed: list[dict] = []
    for iid in open_ids:
        rows = evidence_by_issue.get(iid, [])
        issue_title = title_by_id.get(iid, iid)
        for ev in rows:
            ev["issue_id"] = iid
            ev["issue_title"] = issue_title
        thread_feed.extend(rows)
    deep_links.attach_deep_links(thread_feed)
    thread_feed.sort(key=lambda ev: ev.get("ts") or 0, reverse=True)
    return JSONResponse({"project": sanitize_surrogates(project), "issues": sanitize_surrogates(issues),
                        "clusters": sanitize_surrogates(clusters),
                        "has_confirmed_grouping": has_confirmed_grouping,
                        "thread_feed": sanitize_surrogates(thread_feed),
                        "synthesis": sanitize_surrogates(synthesis),
                        "attachments": sanitize_surrogates(attachments),
                        "parties": sanitize_surrogates(parties),
                        "asks": sanitize_surrogates(asks), "decisions": sanitize_surrogates(decisions),
                        "key_facts": sanitize_surrogates(key_facts),
                        "commitments": sanitize_surrogates(commitments),
                        "repeat_signals": sanitize_surrogates(repeat_signals),
                        "value_found": sum(value_by_issue.values()),
                        "gated_open_issue_count": sum(1 for i in open_issues if i.get("has_unmet_prerequisite")),
                        "hard_deadline_open_issue_count": sum(1 for i in open_issues if i.get("has_hard_deadline")),
                        "top_action": sanitize_surrogates({
                            "issue_id": top_issue["id"], "title": top_issue.get("display_title") or top_issue["title"],
                            "reason": top_issue.get("nba_reason"),
                        }) if top_issue and top_issue.get("nba_reason") else None})


class ProjectExtractIssueBody(BaseModel):
    title: str
    category: Optional[str] = None
    claim_ids: list[int] = Field(default_factory=list)


@app.post("/api/workgraph/projects/{project_id}/issues")
async def api_project_extract_issue(project_id: str, body: ProjectExtractIssueBody):
    """Corrected pipeline Phase D (2026-08-05): curator's real content-
    extraction route - see workgraph_projects.extract_issue_from_project's
    own docstring for the full design. Curator calls this once it's read a
    confirmed project's aggregated evidence (GET /api/workgraph/projects/
    {id} now returns `clusters`/`has_confirmed_grouping` alongside the
    existing `issues`, for exactly this purpose) and judged that a specific
    set of already-materialized claims genuinely belong together as one
    real, separately-trackable issue."""
    if not body.claim_ids:
        raise HTTPException(400, "claim_ids must be non-empty - this route creates an issue FROM cited claims")
    try:
        result = workgraph_projects.extract_issue_from_project(
            project_id, title=body.title, category=body.category, claim_ids=body.claim_ids,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse({"ok": True, "result": result})


def _pending_review_items_for_issues(issue_ids: list[str]) -> list[dict]:
    """Task #272: pending claim-resolution/issue-state suggestions scoped
    to THIS project's own open issues, surfaced directly inside the
    add-in's project card - the real fix for the "queue" Marc explicitly
    rejected ("there is no current queue for me to view this shit...if a
    user MUST take an action then it needs to ask the user in the card").
    Same underlying suggest-only rows the cockpit/chat paths already
    resolve (workgraph_reconcile.confirm_claim_suggestion,
    /api/workgraph/issue-state-suggestions/{id}/resolve) - this is a new
    SURFACE for them, not a new mechanism, and definitely not a second
    place to go looking. Deliberately excludes prerequisite-rule
    suggestions - those are a Settings-level new-gating-RULE proposal, not
    tied to one issue's own checklist, so they don't belong in a project
    card the way a claim/issue-state suggestion does."""
    if not issue_ids:
        return []
    issue_id_set = set(issue_ids)
    items: list[dict] = []
    for s in wg.list_pending_claim_suggestions():
        claim = wg.get_claim(s["claim_id"])
        if not claim or claim.get("issue_id") not in issue_id_set:
            continue
        verb = "Mark resolved" if s["suggestion_kind"] == "resolve" else "Contradiction on"
        description = f'{verb}: "{claim.get("text") or "(claim text unavailable)"}"'
        if s.get("evidence_note"):
            description += f" — {s['evidence_note']}"
        items.append({
            "kind": "claim_suggestion", "id": s["id"], "issue_id": claim.get("issue_id"),
            "description": description,
        })
    for s in wg.list_issue_state_suggestions(status="pending"):
        if s.get("issue_id") not in issue_id_set:
            continue
        items.append({
            "kind": "issue_state_suggestion", "id": s["id"], "issue_id": s.get("issue_id"),
            "description": s.get("evidence_note") or "This issue looks resolved to Jasper - close it?",
        })
    return items


def _build_addin_focus_card(project_id: str) -> Optional[dict]:
    """Lean, single-project card for the Outlook/Teams add-in (tasks #240-
    243): "focus on this email/supplier/person" - reuses the exact same
    real readers the full cockpit project-detail route does (synthesis,
    per-issue candidate_actions with real evidence-row recommendations
    like an Ariba approval deep link or a contract_review offer), but
    scoped to ONE project and with candidate_actions computed per open
    issue - something GET /api/workgraph/projects/{id} deliberately skips
    for its own perf reasons (that route renders every project in a list;
    the add-in only ever renders one at a time, so the same N-issue
    candidate_actions cost that route avoids is cheap here)."""
    project = wg.get_project(project_id)
    if project is None:
        return None
    issues = wg.list_issues_for_project(project_id)
    workgraph_deadlines.attach_deadline_info(issues)
    synthesis = wg.get_synthesis("project", project_id)
    open_issues = [i for i in issues if i["state"] in ("active", "waiting", "blocked")]
    open_ids = [i["id"] for i in open_issues]
    evidence_by_issue = wg.list_evidence_for_issues(open_ids)
    attachments = wg.list_attachments_for_project(project_id)
    all_evidence = [ev for iid in open_ids for ev in evidence_by_issue.get(iid, [])]
    workgraph_recommend.attach_recommendations(all_evidence, attachments, time.time())
    deep_links.attach_deep_links(all_evidence)

    raw_item_ids = {ev["raw_item_id"] for ev in all_evidence if ev.get("raw_item_id")}
    raw_items_by_id = wg.get_raw_items_by_ids(list(raw_item_ids)) if raw_item_ids else {}
    open_claims_by_issue = wg.list_open_claims_for_issues(open_ids)
    # Drawer redesign follow-on (Marc's own request): "Everything else"
    # lane items should DO something, not just show text. Each evidence
    # row already carries real deep_links (deep_links.attach_deep_links -
    # a vendor URL like "Open in Ariba"/"Open in DocuSign" when the
    # underlying email is a live signature/approval request) and real
    # recommendations (workgraph_recommend.attach_recommendations - e.g.
    # a contract_review offer). Indexed by raw_item_id here so a lane
    # item (built from claims, not evidence) can look up whichever real
    # action its own raw_item_id already has, rather than a second
    # invented action-detection pass.
    evidence_by_raw_item_id = {ev["raw_item_id"]: ev for ev in all_evidence if ev.get("raw_item_id")}

    issue_cards = []
    hero = None  # first (highest-priority) issue's own top-scored action wins - open_issues is
    # already priority_score-sorted below, so the loop's first non-empty actions list is it.
    for issue in sorted(open_issues, key=lambda i: i.get("priority_score") or 0, reverse=True):
        actions = workgraph_nba.candidate_actions(issue, evidence_by_issue.get(issue["id"], []), None, synthesis)
        for a in actions:
            rid = a.get("raw_item_id")
            raw_item = raw_items_by_id.get(rid) if rid else None
            if raw_item:
                a["open_email"] = deep_links.open_email_action(raw_item)
                a["draft_reply"] = deep_links.draft_reply_action(raw_item)
                a["draft_forward"] = deep_links.draft_forward_action(raw_item)
        # Task #237/#238/#239: this issue's own real reference ID(s) (so a
        # project card can show distinct PRs/POs as distinct sub-cards
        # instead of one flat blob), nearest open date claim(s) (real
        # curator-extracted text/hard-or-soft tier - never a parsed
        # calendar date, see workgraph_nba.DEFAULT_CLAIM_WEIGHTS' own
        # docstring on why no such parsing exists in this codebase), and
        # this issue's own attachments so a document can be opened directly
        # from its card rather than requiring a trip to the full project
        # view first.
        issue_cards.append({
            "id": issue["id"], "title": issue.get("display_title") or issue["title"],
            "state": issue["state"], "category": issue.get("category"),
            "nba_reason": issue.get("nba_reason"), "actions": actions,
            "reference_ids": sorted(workgraph_projects.reference_ids_for_issue(issue["id"])),
            "dates": [{"text": c["text"], "date_kind": c.get("date_kind")}
                      for c in wg.list_open_claims_for_issue(issue["id"], claim_type="date")],
            "attachments": wg.list_attachments_for_issue(issue["id"]),
        })
        # Drawer redesign (task #276/#293's own follow-on): the single "your
        # move" spotlight, real not invented - the top-scored real
        # candidate_actions() entry off the highest-priority open issue,
        # same ranking this card already computed above, just surfaced
        # once instead of buried per-issue. First issue with any action at
        # all wins (open_issues is already sorted highest-priority-first).
        if hero is None and actions:
            top = max(actions, key=lambda a: a.get("score") or 0)
            open_email_act = top.get("open_email")
            draft_reply_act = top.get("draft_reply")
            draft_forward_act = top.get("draft_forward")
            # Marc's report ("I do not know what to click on"): the
            # top-scored action can be synthesis-derived with no
            # raw_item_id of its own, or point at a raw_item that isn't a
            # real outlook_mail entry - either way the label says "Draft a
            # reply" but no button renders. Fall back to this issue's own
            # most recent real outlook_mail evidence (evidence_by_issue is
            # already ts DESC, see list_evidence_for_issues) so the hero
            # always has something real to anchor a button to when one of
            # these three is missing.
            if not (open_email_act or draft_reply_act or draft_forward_act):
                for ev in evidence_by_issue.get(issue["id"], []):
                    rid = ev.get("raw_item_id")
                    raw_item = raw_items_by_id.get(rid) if rid else None
                    if raw_item is None and rid:
                        raw_item = wg.get_raw_item(rid)
                    if raw_item and raw_item.get("source") == "outlook_mail" and raw_item.get("entry_id"):
                        open_email_act = deep_links.open_email_action(raw_item)
                        draft_reply_act = deep_links.draft_reply_action(raw_item)
                        draft_forward_act = deep_links.draft_forward_action(raw_item)
                        break
            hero = {
                "issue_id": issue["id"],
                "issue_title": issue.get("display_title") or issue["title"],
                "reference_ids": sorted(workgraph_projects.reference_ids_for_issue(issue["id"])),
                "kind": top.get("kind"), "label": top.get("label"),
                "rationale": top.get("rationale"),
                "open_email": open_email_act, "draft_reply": draft_reply_act,
                "draft_forward": draft_forward_act,
            }

    # "Everything else": every other open ask/decision/commitment across
    # ALL this project's open issues (not just the hero's issue - a project
    # is the unit here, task #237), split into you/them by the claim's own
    # real author field ('marc' vs 'counterparty'/'unknown' - never guessed
    # from phrasing). "settled" is a bounded, real lookback (not the whole
    # claims history) so a long-lived project doesn't dump its entire
    # archive into one lane.
    issue_title_by_id = {i["id"]: (i.get("display_title") or i["title"]) for i in open_issues}
    lane_you: list = []
    lane_them: list = []
    lane_settled: list = []
    for iid in open_ids:
        for c in open_claims_by_issue.get(iid, []):
            if c.get("claim_type") not in ("ask", "decision", "commitment"):
                continue
            item = {
                "issue_id": iid, "issue_title": issue_title_by_id.get(iid),
                "claim_type": c["claim_type"], "text": c.get("text"),
                "raw_item_id": c.get("raw_item_id"),
            }
            ev = evidence_by_raw_item_id.get(c.get("raw_item_id"))
            if ev:
                vendor_link = next((d for d in ev.get("deep_links", []) if d.get("kind") == "url"), None)
                if vendor_link:
                    item["action_label"] = vendor_link["label"]
                    item["action_url"] = vendor_link["url"]
                elif any(r.get("kind") == "contract_review" for r in ev.get("recommendations", [])):
                    item["action_label"] = "Run Contract Review Skill"
                    item["action_kind"] = "contract_review"
            (lane_you if c.get("author") == "marc" else lane_them).append(item)
        recent_resolved = [
            c for c in wg.list_claims_for_issue(iid)
            if c.get("claim_type") in ("ask", "decision", "commitment")
            and c.get("status") in ("done", "dismissed")
            and (time.time() - (c.get("last_seen_ts") or 0)) < 14 * 86400
        ]
        recent_resolved.sort(key=lambda c: c.get("last_seen_ts") or 0, reverse=True)
        for c in recent_resolved[:3]:
            lane_settled.append({
                "issue_id": iid, "issue_title": issue_title_by_id.get(iid),
                "claim_type": c["claim_type"], "text": c.get("text"), "status": c.get("status"),
            })

    # get_project() (singular) doesn't do list_projects()'s synthesis join -
    # derived_title lives on the synthesis row already fetched above, name
    # is the raw fallback (same precedence list_projects uses).
    project_title = (synthesis or {}).get("derived_title") or project.get("name")
    return {
        "project": {"id": project["id"], "title": project_title},
        "summary": (synthesis or {}).get("summary"),
        "issues": issue_cards,
        "hero": hero,
        "lanes": {"you": lane_you, "them": lane_them, "settled": lane_settled},
        "attachments": attachments,
        "parties": workgraph_projects.aggregate_parties_for_project(project_id),
        "pending_review": _pending_review_items_for_issues(open_ids),
    }


_REFERENCED_PROJECTS_CURSOR = ("addin_chat", "last_referenced_projects")


def _record_referenced_project(project_id: str, title: Optional[str]) -> None:
    """Marc's own request: when Jasper's chat reply discusses a specific
    project ("show me where we're at with Kinaxis"), the add-in should be
    able to offer a real "open the drawer" link for it - not guessed from
    the reply text. Every route that resolves a real project (focus-email/
    focus-party/focus-project) records it here via the existing generic
    ingest_cursors key-value primitive (no new table); /api/assistant/
    message resets this before each turn and reads it back after, so a
    stale reference from an earlier turn can never leak into this one."""
    existing_raw = wg.get_cursor(*_REFERENCED_PROJECTS_CURSOR)
    existing = json.loads(existing_raw) if existing_raw else []
    if not any(p.get("id") == project_id for p in existing):
        existing.append({"id": project_id, "title": title})
    wg.set_cursor(*_REFERENCED_PROJECTS_CURSOR, json.dumps(existing))


@app.get("/api/addin/focus-email")
async def api_addin_focus_email(conversation_id: str):
    """Task #240: "focus on the email I have open" - Office.js's
    conversationId is the same Exchange conversation-thread GUID Outlook
    COM writes into raw_items.stable_key (ingest/outlook_scan.ps1),
    so this is a direct, ground-truth lookup, not a guess."""
    project_id = wg.project_id_for_conversation_id(conversation_id)
    if project_id is None:
        return JSONResponse({"matched": False})
    card = _build_addin_focus_card(project_id)
    if card is None:
        return JSONResponse({"matched": False})
    _record_referenced_project(project_id, card["project"]["title"])
    return SafeJSONResponse({"matched": True, "card": card})


_DISCOVERY_VOCABULARY_CAP = workgraph_discovery._VOCABULARY_CAP


def _data_point_with_staleness(d: dict) -> dict:
    now = time.time()
    last = d.get("last_matched_ts")
    reference_ts = last if last is not None else d.get("created_ts")
    d = dict(d)
    d["days_since_last_matched"] = round((now - reference_ts) / 86400, 1) if reference_ts else None
    d["stale"] = bool(reference_ts and (now - reference_ts) > workgraph_discovery._STALENESS_SECONDS)
    return d


@app.get("/api/discovery/proposed")
async def api_discovery_proposed():
    """Task #214 - every data point discovery has drafted but a human
    hasn't acted on yet. Never auto-activated (design doc §2.4) - this is
    the one real surface where that human confirm/reject actually happens."""
    return SafeJSONResponse({"proposed": wg.list_data_point_definitions(status="proposed")})


@app.get("/api/discovery/confirmed")
async def api_discovery_confirmed():
    """This installation's real active vocabulary, annotated with
    staleness (design doc §3's job-change signal) so the review surface
    can show "hasn't come up in N months, still relevant?" without the
    caller needing to re-derive that math itself."""
    confirmed = [_data_point_with_staleness(d) for d in wg.list_data_point_definitions(status="confirmed")]
    return SafeJSONResponse({"confirmed": confirmed, "cap": _DISCOVERY_VOCABULARY_CAP})


class DiscoveryConfirmBody(BaseModel):
    confirmed_by: str = "marc"
    retire_definition_id: Optional[str] = None


@app.post("/api/discovery/{definition_id}/confirm")
async def api_discovery_confirm(definition_id: str, body: DiscoveryConfirmBody):
    """Design doc §3's 20-item vocabulary cap is enforced HERE, not at
    proposal-drafting time (a proposal can still be drafted and queued
    past the cap) - confirming a NEW data point when already at cap fails
    with 409 unless the caller also names an existing confirmed
    definition to retire in the same call, forcing a real choice rather
    than silently growing past 20."""
    definition = wg.get_data_point_definition(definition_id)
    if definition is None:
        raise HTTPException(404, f"no such data point: {definition_id}")
    if definition["status"] != "proposed":
        raise HTTPException(400, f"data point {definition_id} is not pending review (status={definition['status']})")

    confirmed = wg.list_data_point_definitions(status="confirmed")
    if len(confirmed) >= _DISCOVERY_VOCABULARY_CAP:
        if not body.retire_definition_id:
            raise HTTPException(409, {
                "error": f"already at the {_DISCOVERY_VOCABULARY_CAP}-item vocabulary cap - "
                         "retire an existing confirmed data point to make room (pass retire_definition_id)",
                "confirmed": confirmed,
            })
        retire = wg.get_data_point_definition(body.retire_definition_id)
        if retire is None or retire["status"] != "confirmed":
            raise HTTPException(400, f"no confirmed data point to retire: {body.retire_definition_id}")
        wg.reject_data_point_definition(body.retire_definition_id)

    wg.confirm_data_point_definition(definition_id, confirmed_by=body.confirmed_by)
    return JSONResponse({"ok": True, "definition": wg.get_data_point_definition(definition_id)})


@app.post("/api/discovery/{definition_id}/reject")
async def api_discovery_reject(definition_id: str):
    """Covers both real rejections of a fresh proposal AND retiring an
    already-confirmed data point directly (e.g. from the staleness list,
    without needing to go through the confirm route's retire-to-make-room
    path) - same underlying status transition either way."""
    if wg.get_data_point_definition(definition_id) is None:
        raise HTTPException(404, f"no such data point: {definition_id}")
    wg.reject_data_point_definition(definition_id)
    return JSONResponse({"ok": True})


class DiscoverySetupBody(BaseModel):
    role_hint: Optional[str] = None
    window_days: int = 90


@app.post("/api/discovery/setup")
async def api_discovery_setup(body: DiscoverySetupBody):
    """Task #213's one-time bulk pass - real LLM calls happen inline here
    (one per pattern that already crosses the significance bar in the
    window), so this can take a while on a fresh corpus; called from a
    real setup flow, not on every page load."""
    result = await asyncio.to_thread(
        workgraph_discovery.run_setup_discovery, role_hint=body.role_hint, window_days=body.window_days,
    )
    return JSONResponse({"ok": True, "result": result})


@app.post("/api/discovery/sweep")
async def api_discovery_sweep():
    """Manual trigger for design doc §3's monthly sweep - real scheduling
    (a cron-style periodic call) is deliberately out of scope here, same
    as every other *_oneshot()-style routine in this codebase that a
    scheduler calls into (see scheduled_refresh.py's own pattern)."""
    result = await asyncio.to_thread(workgraph_discovery.run_monthly_sweep)
    return JSONResponse({"ok": True, "result": result})


@app.get("/api/discovery/system-table-proposals")
async def api_discovery_system_table_proposals(status: Optional[str] = None):
    """Task #266's review surface - mirrors /api/discovery/proposed's own
    shape for the generalized "this domain looks like a whole system"
    proposals. Confirming one here is a real go-ahead decision for a
    human/dev pass to actually build the table + extraction function
    (see proposed_system_tables' own CREATE TABLE comment) - this route
    never executes DDL or writes code itself."""
    return SafeJSONResponse({"proposals": wg.list_system_table_proposals(status=status)})


class SystemTableProposalResolveBody(BaseModel):
    action: str  # "confirm" | "reject"
    resolved_by: str = "marc"


@app.post("/api/discovery/system-table-proposals/{proposal_id}/resolve")
async def api_discovery_system_table_proposal_resolve(proposal_id: str, body: SystemTableProposalResolveBody):
    proposal = wg.get_system_table_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(404, f"no such system-table proposal: {proposal_id}")
    if proposal["status"] != "proposed":
        raise HTTPException(400, f"proposal {proposal_id} is not pending review (status={proposal['status']})")
    if body.action not in ("confirm", "reject"):
        raise HTTPException(400, f'action must be "confirm" or "reject", got {body.action!r}')
    wg.resolve_system_table_proposal(
        proposal_id, "confirmed" if body.action == "confirm" else "rejected", resolved_by=body.resolved_by,
    )
    return JSONResponse({"ok": True, "proposal": wg.get_system_table_proposal(proposal_id)})


@app.get("/api/addin/focus-project/{project_id}")
async def api_addin_focus_project(project_id: str):
    """Same rich card shape as focus-email/focus-party, addressed directly
    by project_id - lets the add-in's "View full project" drill-down (from
    the top-actions list) render the identical real-actions card instead of
    a second, flatter view."""
    card = _build_addin_focus_card(project_id)
    if card is None:
        raise HTTPException(404, f"no such project: {project_id}")
    _record_referenced_project(project_id, card["project"]["title"])
    return SafeJSONResponse({"card": card})


@app.get("/api/addin/top-projects")
async def api_addin_top_projects(limit: int = 5):
    """Task #237: the add-in's home view, redesigned around PROJECT as the
    primary unit instead of a flat list of individual asks that read as N
    unrelated things even when several were the same deal. Reuses the
    exact real card _build_addin_focus_card already builds for focus-
    email/focus-party/focus-project (synthesis, type-aware actions,
    attachments, and now each issue's reference_ids/dates - see that
    function's own docstring) - not a second, thinner rendering built from
    raw claims.

    Which projects make the list is still workgraph_nba.rank_actions' own
    real global ranking (claim urgency/staleness/value/escalation/
    confidence) - this only changes the UNIT rendered (project, not
    individual claim), never the ranking logic. Distinct project_ids are
    taken in ranked-claim order, so a project's single best-ranked claim
    determines its position - a project with 3 loud asks doesn't crowd out
    4 other projects each with one. Over-fetches claims (rank_actions
    already scores every open issue regardless of its own limit param,
    only truncating the final sorted list, so a bigger request here is a
    cheap Python slice, not more DB work) since several top claims can
    easily collapse into fewer distinct projects than `limit`."""
    ranked = workgraph_nba.rank_actions(limit=200)
    project_ids: list[str] = []
    seen: set[str] = set()
    for claim in ranked:
        pid = claim.get("project_id")
        if pid and pid not in seen:
            seen.add(pid)
            project_ids.append(pid)
        if len(project_ids) >= limit:
            break
    cards = [c for c in (_build_addin_focus_card(pid) for pid in project_ids) if c is not None]
    return SafeJSONResponse({"projects": cards})


@app.get("/api/addin/focus-party")
async def api_addin_focus_party(q: str):
    """Task #241: "focus on a supplier or person" even when it's not the
    currently open email - fuzzy party name/company match -> every
    project those parties touch, most-recently-active first."""
    project_ids = wg.project_ids_for_party_query(q)
    cards = [c for c in (_build_addin_focus_card(pid) for pid in project_ids) if c is not None]
    for c in cards:
        _record_referenced_project(c["project"]["id"], c["project"]["title"])
    return SafeJSONResponse({"matched": bool(cards), "cards": cards})


class WorkgraphProjectStatusBody(BaseModel):
    status: str
    actor: Optional[str] = None


@app.post("/api/workgraph/projects/{project_id}/status")
async def api_workgraph_project_status(project_id: str, body: WorkgraphProjectStatusBody):
    """Task #62: real project-level Mark done/Dismiss/Archive - there was no
    endpoint at all for this before (the generic Symphony /api/projects/
    {id}/status a couple routes above belongs to a completely different,
    unrelated `projects` module - George's cohort-wide tracker, not this
    workgraph). Same shape as the issue-level /status endpoint."""
    if wg.get_project(project_id) is None:
        raise HTTPException(404, f"no such project: {project_id}")
    try:
        wg.set_project_status(project_id, body.status)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if body.status == "done":
        # Task #275 - cascades to every member issue's own raw_items, same
        # fire-and-forget discipline as the issue-level route above.
        for member in wg.list_issues_for_project(project_id):
            asyncio.create_task(asyncio.to_thread(_mark_issue_emails_read_best_effort, member["id"]))
    return JSONResponse({"ok": True, "project": sanitize_surrogates(wg.get_project(project_id))})


@app.get("/api/workgraph/held-aside-teams")
async def api_held_aside_teams_list():
    """Task #54/#55 (Marc's direct report: "not every individual message...
    in Teams should go into the system"): the real, previously-invisible
    pile of unlinked, unreviewed Teams raw_items - see cluster_and_link's
    own comment for how an item lands here instead of always spawning an
    Issue, and list_held_aside_teams_items for the query itself."""
    return JSONResponse({"items": sanitize_surrogates(wg.list_held_aside_teams_items())})


@app.post("/api/workgraph/held-aside-teams/{raw_item_id}/track")
async def api_held_aside_teams_track(raw_item_id: int):
    """A human's explicit override: yes, actually track this one as a real
    Issue - see workgraph_classify.track_held_aside_item's own docstring."""
    try:
        issue_id = workgraph_classify.track_held_aside_item(raw_item_id)
    except workgraph_classify.HeldAsideItemError as e:
        raise HTTPException(400, str(e))
    return JSONResponse({"ok": True, "issue_id": issue_id})


@app.post("/api/workgraph/held-aside-teams/{raw_item_id}/dismiss")
async def api_held_aside_teams_dismiss(raw_item_id: int):
    """Reviewed, confirmed not worth tracking - see workgraph_classify.
    dismiss_held_aside_item's own docstring."""
    try:
        workgraph_classify.dismiss_held_aside_item(raw_item_id)
    except workgraph_classify.HeldAsideItemError as e:
        raise HTTPException(400, str(e))
    return JSONResponse({"ok": True})


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


class IssueSplitBody(BaseModel):
    reason: Optional[str] = None


@app.post("/api/workgraph/issues/{issue_id}/split")
async def api_issue_split_from_project(issue_id: str, body: IssueSplitBody):
    """Task #178 - the safety-valve counterpart to the more aggressive
    matching model this grouping-v3 phase builds. Unlike the bare /project
    route above (which the auto-grouper itself also calls), this is the
    Marc-facing 'this grouping was wrong, undo it' action: detaches the
    issue AND durably vetoes it from auto-re-merging with the same project's
    current members (workgraph_projects.split_issue_from_project)."""
    if wg.get_issue(issue_id) is None:
        raise HTTPException(404, f"no such issue: {issue_id}")
    result = workgraph_projects.split_issue_from_project(issue_id, actor="marc", reason=body.reason)
    return JSONResponse({"ok": True, "result": result})


@app.get("/api/workgraph/issues/{issue_id}/claim-suggestions")
async def api_claim_suggestions_list(issue_id: str):
    """Task #155: pending claim-resolution suggestions (both evidence
    types - 'resolve' and 'contradiction') for one issue's checklist
    view - a human confirms/rejects each explicitly, same review-then-
    confirm shape as project suggestions above."""
    if wg.get_issue(issue_id) is None:
        raise HTTPException(404, f"no such issue: {issue_id}")
    suggestions = workgraph_reconcile.list_pending_claim_suggestions_for_issue(issue_id)
    return JSONResponse({"suggestions": sanitize_surrogates(suggestions)})


class ClaimSuggestionResolveBody(BaseModel):
    status: str  # confirmed | rejected
    actor: str = "marc"


@app.post("/api/workgraph/claim-suggestions/{suggestion_id}/resolve")
async def api_claim_suggestion_resolve(suggestion_id: int, body: ClaimSuggestionResolveBody):
    """Confirming a 'resolve' suggestion marks the named claim done;
    confirming a 'contradiction' suggestion only acknowledges the
    mismatch and never touches the claim (see workgraph_reconcile's
    module docstring - an issue closing is not evidence a claim was
    fulfilled). Either way, nothing here ever auto-closes anything
    without this explicit human call."""
    if body.status not in ("confirmed", "rejected"):
        raise HTTPException(400, "status must be 'confirmed' or 'rejected'")
    if wg.get_claim_suggestion(suggestion_id) is None:
        raise HTTPException(404, f"no such suggestion: {suggestion_id}")
    if body.status == "confirmed":
        ok = workgraph_reconcile.confirm_claim_suggestion(suggestion_id, actor=body.actor)
    else:
        ok = workgraph_reconcile.reject_claim_suggestion(suggestion_id, actor=body.actor)
    if not ok:
        raise HTTPException(409, "suggestion already resolved")
    return JSONResponse({"ok": True})


class IssueStateSuggestionResolveBody(BaseModel):
    action: str  # "confirm" | "reject"
    actor: str = "marc"


@app.post("/api/workgraph/issue-state-suggestions/{suggestion_id}/resolve")
async def api_issue_state_suggestion_resolve(suggestion_id: int, body: IssueStateSuggestionResolveBody):
    """Task #272: the real action side of detect_issues_appear_resolved_
    but_still_open (task #273) - that sweep only ever creates a suggestion,
    never touches the issue itself (suggest-only discipline). Confirming
    here is the human call it's designed to wait for: marks the issue
    'done' through the SAME path (wg.update_issue + the closure-triggered
    mark-as-read fire-and-forget) the normal status route already uses,
    rather than a second, narrower state-flip that would skip those side
    effects. Rejecting just acknowledges the mismatch, same as rejecting a
    claim-resolution suggestion never touches the claim."""
    if body.action not in ("confirm", "reject"):
        raise HTTPException(400, "action must be 'confirm' or 'reject'")
    suggestions = wg.list_issue_state_suggestions(status="pending")
    suggestion = next((s for s in suggestions if s["id"] == suggestion_id), None)
    if suggestion is None:
        raise HTTPException(404, f"no such pending suggestion: {suggestion_id}")
    if body.action == "confirm":
        issue_id = suggestion["issue_id"]
        if wg.get_issue(issue_id) is None:
            raise HTTPException(404, f"no such issue: {issue_id}")
        wg.update_issue(issue_id, actor=body.actor, state="done")
        asyncio.create_task(asyncio.to_thread(_mark_issue_emails_read_best_effort, issue_id))
        wg.resolve_issue_state_suggestion(suggestion_id, "confirmed")
    else:
        wg.resolve_issue_state_suggestion(suggestion_id, "rejected")
    return JSONResponse({"ok": True})


@app.get("/api/workgraph/review-queue")
async def api_review_queue():
    """Task #260/#262: one aggregated, chat-friendly view across every LIVE
    review queue (claim suggestions + prerequisite rule suggestions) - the
    same two queues Marc already resolves one-by-one via the cockpit's
    per-issue checklist / Settings page, surfaced here with a plain-language
    description so the assistant can describe and resolve them
    conversationally (jasper_list_review_queue/jasper_resolve_review_item),
    mirroring Aristotle's existing #addrule chat-teaching flow (task #236)
    rather than inventing a second interaction style. Deliberately excludes
    the retired pending_project_suggestions/work_object_relationships
    queues (task #269 found them dead - zero live consumer since the
    2026-08-05 grouping-mechanism cutover)."""
    claim_suggestions = wg.list_pending_claim_suggestions()
    prereq_suggestions = wg.list_prerequisite_suggestions("pending")
    items = []
    for s in claim_suggestions:
        claim = wg.get_claim(s["claim_id"])
        claim_text = (claim or {}).get("text") or "(claim text unavailable)"
        verb = "Resolve" if s["suggestion_kind"] == "resolve" else "Contradiction on"
        description = f'{verb} claim "{claim_text}"'
        if s.get("evidence_note"):
            description += f": {s['evidence_note']}"
        items.append({
            "kind": "claim_suggestion", "id": s["id"],
            "issue_id": (claim or {}).get("issue_id"),
            "description": description, "created_ts": s["created_ts"],
        })
    for s in prereq_suggestions:
        description = s.get("raw_explanation") or s.get("reason") or (
            f"{s.get('trigger_signal_type')} shouldn't be treated as actionable until "
            f"{s.get('requires_signal_type')} has happened"
        )
        items.append({
            "kind": "prerequisite_suggestion", "id": s["id"], "issue_id": None,
            "description": description, "created_ts": s["created_ts"],
        })
    items.sort(key=lambda i: i["created_ts"])
    return JSONResponse({"items": sanitize_surrogates(items), "count": len(items)})


@app.get("/api/workgraph/relationship-audit")
async def api_relationship_audit():
    """Task #304, item #2 (2026-08-11, Marc's own scoping call: chat/MCP
    tool only, no cockpit UI). Every active Relationship spanning 2+
    projects - the "these share a real relationship signal but are
    separate projects, should they be?" question, on demand rather than
    something Marc has to stumble into. Deliberately NOT folded into
    /api/workgraph/review-queue above - a relationship-spans-projects
    finding isn't a proposal with a clean confirmed/rejected verdict the
    way a claim suggestion is, so it doesn't fit that route's resolve
    contract. Read-only, computed fresh every call."""
    return JSONResponse({"relationships": sanitize_surrogates(workgraph_relationships.list_relationships_needing_review())})


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


@app.get("/api/workgraph/suppliers/{company}/weekly_scorecard")
async def api_supplier_weekly_scorecard(company: str):
    """Enhancement idea panel #15: a real, on-demand draft - generated
    fresh on every call (cheap, same batched queries supplier_detail
    already uses), never auto-sent anywhere."""
    draft = workgraph_suppliers.weekly_scorecard_draft(company, time.time())
    if draft is None:
        raise HTTPException(404, f"no such supplier: {company}")
    return JSONResponse(sanitize_surrogates(draft))


@app.get("/api/workgraph/todo-summary")
async def api_workgraph_todo_summary():
    """Task #281: real data behind "what's my to-do list?" - see
    workgraph_todo.build_todo_summary's own docstring for the three
    sections (outputs_waiting/open_claims/by_supplier). Raw structured
    data, not prose - jasper_todo_list (jasper_mcp_server.py) hands this
    straight to the assistant to render conversationally."""
    return JSONResponse(sanitize_surrogates(workgraph_todo.build_todo_summary()))


@app.get("/api/workgraph/focus-today")
async def api_workgraph_focus_today():
    """Task #283: real data behind "what should I focus on today?" - see
    workgraph_focus.build_focus_today_summary's own docstring for the
    three sections (top_actions/meetings_today/deliverables_due_soon).
    Raw structured data - jasper_focus_today hands this to the assistant
    to render conversationally."""
    return JSONResponse(sanitize_surrogates(workgraph_focus.build_focus_today_summary()))


@app.get("/api/workgraph/renewal-outreach-candidates")
async def api_renewal_outreach_candidates():
    """Enhancement idea panel #18: every open issue with a real, curator-
    resolved renewal/expiration date landing in the outreach window - a
    'Renewal Radar' list, computed fresh on every call."""
    candidates = workgraph_deadlines.find_renewal_outreach_candidates(now=time.time())
    return JSONResponse({"candidates": sanitize_surrogates(candidates)})


@app.get("/api/workgraph/issues/{issue_id}/renewal_outreach_draft")
async def api_renewal_outreach_draft(issue_id: str):
    """Enhancement idea panel #18: the actual draft content (recipient,
    subject, body) for one issue's renewal candidate - a genuine DRAFT,
    same posture as the weekly scorecard route above. Never sends
    anything; no live Outlook 'compose new mail' action exists yet
    (task #35) to wire this into."""
    if wg.get_issue(issue_id) is None:
        raise HTTPException(404, f"no such issue: {issue_id}")
    draft = workgraph_deadlines.renewal_outreach_draft(issue_id, now=time.time())
    if draft is None:
        raise HTTPException(404, f"no renewal outreach candidate for issue: {issue_id}")
    return JSONResponse(sanitize_surrogates(draft))


@app.get("/api/workgraph/attachments/{attachment_id_a}/compare/{attachment_id_b}")
async def api_attachment_compare(attachment_id_a: int, attachment_id_b: int):
    """Enhancement idea panel #19: a deterministic, zero-LLM paragraph-
    level text diff between two attachments the caller has explicitly
    named (no relationship-discovery guess about which two are
    'versions' of each other - see workgraph_redline.py's own module
    docstring for why)."""
    try:
        result = workgraph_redline.compare_attachments(attachment_id_a, attachment_id_b)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return JSONResponse(sanitize_surrogates(result))


@app.get("/api/workgraph/meeting-prep-candidates")
async def api_meeting_prep_candidates():
    """Enhancement idea panel #20: every open issue with a real upcoming
    calendar meeting within the default lookahead window."""
    candidates = workgraph_meetingprep.find_upcoming_meeting_prep_candidates(now=time.time())
    return JSONResponse({"candidates": sanitize_surrogates(candidates)})


@app.get("/api/workgraph/issues/{issue_id}/meeting_prep_draft")
async def api_meeting_prep_draft(issue_id: str):
    """Enhancement idea panel #20: the actual prep narrative for one
    issue's nearest upcoming meeting - a genuine draft, same posture as
    the weekly scorecard/renewal outreach drafts above."""
    if wg.get_issue(issue_id) is None:
        raise HTTPException(404, f"no such issue: {issue_id}")
    draft = workgraph_meetingprep.meeting_prep_draft(issue_id, now=time.time())
    if draft is None:
        raise HTTPException(404, f"no upcoming meeting for issue: {issue_id}")
    return JSONResponse(sanitize_surrogates(draft))


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

@app.get("/api/workgraph/raw_items/{raw_item_id}")
async def api_raw_item_detail(raw_item_id: int):
    """The one place curator's synthesis routine (task #33) can actually read
    a communication's real content. evidence.summary (returned by the issue
    detail endpoint) is subject-line-only by design - fine for the Progress
    timeline's compact list, useless for real extraction judgment. Before
    this endpoint existed there was no GET route for raw_items at all, so
    curator had no way to read a body or an attachment even though
    text_extract.resolve_item_text and attachment_extract's stored
    extracted_text (task #29) already had the real content sitting in the
    DB/filesystem, unused for this purpose."""
    item = wg.get_raw_item(raw_item_id)
    if item is None:
        raise HTTPException(404, f"no such raw_item: {raw_item_id}")
    attachments = wg.list_attachments("raw_item", str(raw_item_id))
    return JSONResponse({
        "raw_item": sanitize_surrogates({
            "id": item["id"], "source": item.get("source"), "subject": item.get("subject"),
            "from_actor": item.get("from_actor"), "occurred_ts": item.get("occurred_ts"),
            "participants": item.get("participants_json"),
        }),
        "full_text": sanitize_surrogates(text_extract.resolve_item_text(item)),
        "attachments": sanitize_surrogates([
            {"id": a["id"], "filename": a.get("filename"), "extracted_text": a.get("extracted_text")}
            for a in attachments
        ]),
        "extraction": sanitize_surrogates(wg.get_extraction(raw_item_id)),
    })


class ExtractionBody(BaseModel):
    extracted_json: dict


@app.post("/api/workgraph/raw_items/{raw_item_id}/extraction")
async def api_raw_item_extraction_write(raw_item_id: int, body: ExtractionBody):
    item = wg.get_raw_item(raw_item_id)
    if item is None:
        raise HTTPException(404, f"no such raw_item: {raw_item_id}")
    wg.create_extraction(raw_item_id, json.dumps(body.extracted_json))
    # Phase 3 (design doc Section 9): materialize claims from this extraction
    # the moment it's written - the live-wiring point claims_revision (Section
    # 9.5) needs to stay current for real, not just at backfill time. Also
    # index evidence_fts (Section 9.6) here, same reasoning: this is the one
    # place a raw_item's real text+extraction are both freshly available.
    workgraph_claims.materialize_claims_for_raw_item(raw_item_id)
    # Task #155: resolution_signals (this extraction's curator-judged
    # completion evidence, if any) become suggest-only claim-resolution
    # suggestions the moment they're written - same live-wiring reasoning
    # as materialize_claims_for_raw_item above, never an auto-close.
    workgraph_reconcile.generate_resolution_signal_suggestions(raw_item_id)
    body_text = text_extract.resolve_item_text(item)
    if body_text and body_text.strip():
        wg.index_evidence_fts(raw_item_id, item.get("issue_id"), body_text)
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


@app.get("/api/workgraph/evidence-search")
async def api_evidence_search(q: str, limit: int = 50):
    """Design doc Section 9.6/10.5 step 2: full-text search over
    text_extract.resolve_item_text() output, already indexed at extraction-
    write time (and by the one-time backfill). Unscoped by design - the
    caller (Project Deep-Dive) cross-references hits against a project's
    own issue list itself rather than this route doing that filtering."""
    return JSONResponse({"results": sanitize_surrogates(wg.search_evidence_fts(q, limit=limit))})


@app.get("/api/workgraph/deep-dive/next")
async def api_deep_dive_next():
    """Design doc Section 10.3/10.5: the ONE project (never-deep-dived
    first, then oldest-last_deep_dive_ts first, active/waiting projects
    only) the Deep-Dive routine should work on this wake, plus its derived
    search seeds (Section 10.3) - the project's own name and every real
    identity anchor across its member issues. Empty `project` means there's
    nothing eligible right now."""
    candidates = workgraph_deepdive.list_deepdive_candidates(limit=1)
    if not candidates:
        return JSONResponse({"project": None, "seeds": None})
    project = candidates[0]
    seeds = workgraph_deepdive.derive_seeds_for_project(project["id"])
    return JSONResponse({"project": sanitize_surrogates(project), "seeds": sanitize_surrogates(seeds)})


class DeepDiveCompleteBody(BaseModel):
    note: str


@app.post("/api/workgraph/projects/{project_id}/deep_dive_complete")
async def api_deep_dive_complete(project_id: str, body: DeepDiveCompleteBody):
    """Design doc Section 10.4: the ONE place last_deep_dive_ts/note ever
    changes - a deterministic, code-verifiable act the routine calls when
    it finishes a wake, never inferred from the model's own prose. `note`
    is a short, honest account of what was actually searched and found (or
    why the run stopped early) - a genuine "found nothing new" is a normal,
    expected value here, not a failure."""
    if wg.get_project(project_id) is None:
        raise HTTPException(404, f"no such project: {project_id}")
    wg.mark_project_deep_dived(project_id, body.note)
    return JSONResponse({"ok": True, "project": sanitize_surrogates(wg.get_project(project_id))})


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


# --- Task #303: SharePoint/OneDrive link fetch queue - relay's own
# worklist for links workgraph_classify.run_classification() found in an
# ordinary message's body and can't fetch itself (that pass is
# deterministic, no live SharePoint access). Resolving one writes a real
# attachments row the exact same way a native email attachment does
# (entity_type='raw_item'), so the drawer/synthesis/claims never need to
# know this queue exists. ---------------------------------------------
@app.get("/api/workgraph/pending-link-fetches")
async def api_pending_link_fetches():
    return JSONResponse({"fetches": sanitize_surrogates(wg.list_pending_link_fetches())})


class ResolveLinkFetchBody(BaseModel):
    status: str  # 'fetched' | 'failed'
    filename: Optional[str] = None
    extracted_text: Optional[str] = None
    note: Optional[str] = None


@app.post("/api/workgraph/pending-link-fetches/{fetch_id}/resolve")
async def api_resolve_link_fetch(fetch_id: int, body: ResolveLinkFetchBody):
    if body.status not in ("fetched", "failed"):
        raise HTTPException(400, "status must be 'fetched' or 'failed'")
    pending = [f for f in wg.list_pending_link_fetches() if f["id"] == fetch_id]
    if not pending:
        raise HTTPException(404, f"no pending fetch with id {fetch_id}")
    fetch = pending[0]

    attachment = None
    if body.status == "fetched":
        if not body.extracted_text:
            raise HTTPException(400, "extracted_text is required when status is 'fetched'")
        raw_item_id = fetch["raw_item_id"]
        sub_dir = DOCUMENTS_RAW_ITEMS_DIR / str(raw_item_id)
        sub_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_filename(body.filename or "shared_document.txt")
        text_bytes = body.extracted_text.encode("utf-8")
        digest = hashlib.sha256(text_bytes).hexdigest()
        dest = sub_dir / f"{digest[:16]}_{safe_name}"
        dest.write_bytes(text_bytes)
        attachment_id = wg.create_attachment(
            entity_type="raw_item", entity_id=str(raw_item_id), kind="reference",
            filename=body.filename or safe_name, stored_path=str(dest.relative_to(DOCUMENTS_DIR)),
            content_type="text/plain", size_bytes=len(text_bytes),
            sha256_hex=digest, uploaded_by="relay", extracted_text=body.extracted_text,
        )
        attachment = wg.get_attachment(attachment_id)

    ok = wg.resolve_pending_link_fetch(fetch_id, status=body.status, note=body.note)
    if not ok:
        raise HTTPException(409, f"fetch {fetch_id} was already resolved")
    return JSONResponse({"ok": True, "attachment": sanitize_surrogates(attachment)})


@app.get("/api/workgraph/issues/{issue_id}/attachments")
async def api_issue_attachments(issue_id: str):
    """Direct issue attachments + email attachments inherited from any raw_item
    already linked to this issue (see wg.list_attachments_for_issue)."""
    if wg.get_issue(issue_id) is None:
        raise HTTPException(404, f"no such issue: {issue_id}")
    return JSONResponse({"attachments": sanitize_surrogates(wg.list_attachments_for_issue(issue_id))})


@app.get("/api/workgraph/issues/{issue_id}/timeline")
async def api_issue_timeline(issue_id: str, tier: str = "milestone"):
    """Design doc Section 12.9: three read-time views over evidence_units/
    claims/claim_events/artifact_versions/prepared_actions - never a new
    stored table. tier defaults to 'milestone' (deterministically filtered
    - the one Marc should actually see by default); 'complete' and
    'activity' are the other two."""
    if wg.get_issue(issue_id) is None:
        raise HTTPException(404, f"no such issue: {issue_id}")
    if tier == "complete":
        entries = wg.list_complete_timeline_for_issue(issue_id)
    elif tier == "activity":
        entries = wg.list_activity_stream_for_issue(issue_id)
    elif tier == "milestone":
        entries = wg.list_milestone_timeline_for_issue(issue_id)
    else:
        raise HTTPException(400, f"unknown tier: {tier} (expected complete|milestone|activity)")
    # Task #9 follow-through (Marc's own request): "who sent/requested
    # this" per timeline entry - claim_events.actor is usually just
    # 'curator'/'system' (the pipeline that wrote the event), never a
    # real person, so the real signal is the underlying raw_item's own
    # sender/direction, resolved here in one batched lookup rather than
    # guessed from actor.
    raw_item_ids = [e["raw_item_id"] for e in entries if e.get("raw_item_id")]
    if raw_item_ids:
        raw_items_by_id = wg.get_raw_items_by_ids(raw_item_ids)
        for e in entries:
            raw_item = raw_items_by_id.get(e.get("raw_item_id"))
            if raw_item:
                e["sender"] = raw_item.get("from_actor")
                e["direction"] = raw_item.get("direction")
    return JSONResponse({"tier": tier, "entries": sanitize_surrogates(entries)})


@app.get("/api/attachments/{attachment_id}/lineage")
async def api_attachment_lineage(attachment_id: int):
    """Design doc Section 12.5's own motivating signal, finally surfaced -
    v2.6 built artifact_lineages/artifact_versions and the live linking
    producer but never a caller that shows 'this document also appears
    on N other threads'. occurrences is [] (not 404) for an attachment
    with no confirmed duplicate - a real, common, correct answer, not a
    missing-data error."""
    if wg.get_attachment(attachment_id) is None:
        raise HTTPException(404, "no such attachment")
    occurrences = wg.list_other_occurrences_for_attachment(attachment_id)
    return JSONResponse({"occurrences": sanitize_surrogates(occurrences)})


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


@app.post("/api/attachments/{attachment_id}/reviewed")
async def api_attachment_mark_reviewed(attachment_id: int):
    """Task #280: the ✓ icon on an "Outputs waiting on you" row - a direct,
    explicit human action, so it's safe to mutate immediately rather than
    queue a suggestion (same reasoning as checklist done/dismiss)."""
    if wg.get_attachment(attachment_id) is None:
        raise HTTPException(404, "no such attachment")
    wg.mark_attachment_reviewed(attachment_id)
    return JSONResponse({"ok": True})


@app.get("/api/addin/output-badge")
async def api_addin_output_badge():
    """Task #280/#287: the add-in's persistent-header amber count chip -
    real source per the locked mockup's own note ('count of skill/worker
    outputs delivered since Marc last opened this project's card'),
    generalized here to a global count since the header chrome is shown
    regardless of which project (if any) is focused. Task #287 adds
    proactively-dispatched actions Marc hasn't acknowledged yet to the same
    count - both are "something Jasper did that's waiting on your eyes,"
    the same underlying concept."""
    count = wg.count_unreviewed_worker_outputs() + wg.count_unacknowledged_proactive_actions()
    return JSONResponse({"count": count})


class ProactiveActionsSettingsBody(BaseModel):
    enabled: bool


@app.get("/api/settings/user-name")
async def api_settings_user_name():
    """The add-in's chat labels use the real user's name instead of a
    generic "You" (Marc's own request) - config.manager.id is the same
    real identity value already used as the actor on every deterministic
    action elsewhere in this codebase, not a second, separate name field.
    Falls back to "You" only if this installation's config genuinely has
    no manager id set."""
    return JSONResponse({"name": config.get("manager", "id") or "You"})


@app.get("/api/settings/proactive-actions")
async def api_settings_proactive_actions_get():
    """Task #287: the add-in header's settings toggle. Off by default, same
    as every other autonomous-leaning capability in this codebase - turning
    this on IS Marc's standing approval for the two narrow action types
    workgraph_proactive.py handles (see its own module docstring for why
    that's a real approval and not content self-approving)."""
    return JSONResponse({"enabled": bool(config.get("proactive_actions", "enabled", default=False))})


@app.post("/api/settings/proactive-actions")
async def api_settings_proactive_actions_set(body: ProactiveActionsSettingsBody):
    config.set_value(body.enabled, "proactive_actions", "enabled")
    return JSONResponse({"ok": True, "enabled": body.enabled})


@app.post("/api/prepared-actions/{prepared_action_id}/acknowledge")
async def api_prepared_action_acknowledge(prepared_action_id: int):
    """Task #287: Marc's explicit 'I've seen this' on a proactively-
    dispatched action - drops it from the output-badge count."""
    if wg.get_prepared_action(prepared_action_id) is None:
        raise HTTPException(404, f"no such prepared action: {prepared_action_id}")
    wg.mark_prepared_action_acknowledged(prepared_action_id)
    return JSONResponse({"ok": True})


# Design doc Section 12.4: long enough to catch a genuine double-click or a
# browser/network retry of the SAME request, short enough that a legitimate
# second request for the same issue+action_kind a few minutes later is never
# blocked. Dispatch itself (team_room.post_message) is near-instant, so this
# has nothing to do with how long the WORKER takes to actually do the work.
COCKPIT_ACTION_IDEMPOTENCY_WINDOW_SECONDS = 300


def _cockpit_action_idempotency_key(issue_id: str, action_kind: str, instructions: Optional[str]) -> str:
    return hashlib.sha256(f"{issue_id}|{action_kind}|{instructions or ''}".encode("utf-8")).hexdigest()


@app.get("/api/skills")
async def api_skills_list():
    """Every real, runnable skill (task #112) - the UI's 'Run a skill' picker
    reads this to offer ALL of them, not just the handful with a dedicated
    button, so a skill installed later is immediately pickable with no
    frontend change. skill_dir is a filesystem Path (worker-side detail,
    not JSON-safe and not useful to the browser) - dropped here, never
    exposed over the wire."""
    registry = skills_registry.list_all()
    skills = [
        {"action_kind": action_kind, "display_name": entry.get("display_name"),
         "label": entry.get("label"), "produces": entry.get("produces")}
        for action_kind, entry in sorted(registry.items())
    ]
    return JSONResponse({"skills": skills})


@app.post("/api/cockpit/actions")
async def api_cockpit_action(body: CockpitActionBody):
    """Generative actions (draft, review, summarize) — wakes a worker via the
    PROVEN action-bridge (team_room @mention -> F9 fanout -> worker_notifications
    -> the worker's armed Monitor poller), empirically confirmed working this
    session. The message is a thin wake-trigger + pointer; the worker pulls full
    Issue context from workgraph.db itself, not from the message body.

    Design doc Section 12.4: this is the one real dispatch point in the
    codebase (every action here is human-click-initiated, never autonomous)
    - prepared_actions is the real execution-safety layer wired in here, an
    idempotency_key blocking an actual double-dispatch (a double-click, a
    browser retry) rather than sending a second team_room message and
    creating a second pending_action for the identical request."""
    if wg.get_issue(body.issue_id) is None:
        raise HTTPException(404, f"no such issue: {body.issue_id}")

    idempotency_key = _cockpit_action_idempotency_key(body.issue_id, body.action_kind, body.instructions)
    existing = wg.find_prepared_action_by_idempotency_key(idempotency_key)
    if (existing is not None and existing["state"] not in wg.PREPARED_ACTION_TERMINAL_STATES
            and (time.time() - existing["created_ts"]) < COCKPIT_ACTION_IDEMPOTENCY_WINDOW_SECONDS):
        return JSONResponse({"ok": True, "duplicate": True, "prepared_action_id": existing["id"],
                              "state": existing["state"]})

    prepared_id = wg.create_prepared_action(
        claim_id=None,  # an issue-level cockpit action isn't tied to one specific claim - honest, not guessed
        action_type=body.action_kind,
        proposed_parameters_json=json.dumps({"issue_id": body.issue_id, "action_kind": body.action_kind,
                                              "worker": body.worker, "instructions": body.instructions},
                                             ensure_ascii=False),
        evidence_refs_json="[]",  # no claim-level evidence linkage at this call site - named gap, not fabricated
        rationale=body.instructions or f"cockpit action: {body.action_kind}",
        risk_class="low",  # every current action_kind requires a further human step before any real-world effect
        idempotency_key=idempotency_key,
        state="approved",  # the human click that reached this route IS the approval - no separate policy gate exists yet
    )

    sender = config.get("manager", "id") or "marc"
    envelope = "@{worker} [COCKPIT-ACTION] {payload}".format(
        worker=body.worker,
        payload=json.dumps({"type": body.action_kind, "issue_id": body.issue_id,
                           "instructions": body.instructions}, ensure_ascii=False),
    )
    wg.update_prepared_action_state(prepared_id, "executing")
    try:
        result = team_room.post_message(sender=sender, body=envelope)
    except ValueError as e:
        wg.update_prepared_action_state(prepared_id, "failed", policy_result=str(e))
        raise HTTPException(400, str(e))
    pending_id = wg.create_pending_action(
        issue_id=body.issue_id, action_kind=body.action_kind, worker=body.worker,
        instructions=body.instructions, message_id=result.get("message_id"),
    )
    # Design doc Section 12.8: a real action was just dispatched for this
    # issue - the strongest of the three exposure states (highest rank).
    wg.advance_work_object_exposure_state(body.issue_id, "used_for_action")
    # Part E2 (2026-07-30): a real generative action was just requested -
    # resolve whichever candidate list was most recently offered, if any,
    # and link it to the real pending_action this produced.
    open_log = wg.get_most_recent_open_choice_log(body.issue_id)
    if open_log is not None:
        wg.mark_choice_log_chosen(open_log["id"], chosen_action_kind=body.action_kind,
                                   resulting_pending_action_id=pending_id)
    return JSONResponse({"ok": True, "pending_action_id": pending_id, "message_id": result.get("message_id"),
                          "prepared_action_id": prepared_id})


@app.get("/api/mail/freshness")
async def api_mail_freshness():
    """Task #274: the assistant's own first check before answering anything
    scoped to "today"/"this morning"/"just now" - cheap and read-only
    (outlook_com_ingest.freshness_status just reads a cursor's updated_ts,
    no COM call), distinct from the heavier /api/cockpit/refresh below."""
    return JSONResponse(outlook_com_ingest.freshness_status())


@app.post("/api/mail/refresh-now")
async def api_mail_refresh_now():
    """Task #274: the assistant's on-demand "fill the gap" call, once
    /api/mail/freshness says data is stale - a real but bounded mail pull
    (outlook_com_ingest.refresh_now's own 75s cap, chosen to leave headroom
    inside the assistant's 120s per-turn budget), NOT the full mail+
    classify+nba+alerts cascade /api/cockpit/refresh runs - the assistant
    needs fresh raw_items to search, not a full corpus re-score, and a
    lighter call is less likely to blow the turn's own timeout."""
    result = await asyncio.to_thread(outlook_com_ingest.refresh_now)
    return JSONResponse({"refresh": result, "freshness": outlook_com_ingest.freshness_status()})


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


@app.post("/api/workers/{worker_id}/wake")
async def api_worker_wake(worker_id: str):
    """The one-click "wake" button (2026-07-31, Marc's direct request, in
    place of any auto-spawn): launches setup/conductor_runbook.md's own
    documented manual wake step (symphony_wake.ps1 <worker>) - the exact
    same action a human would run in a terminal, just without having to
    open one. Deliberately NOT automatic anywhere in this codebase - a
    live worker session is a real, acting, real-cost agent, and this
    cohort's whole governance model (conductor_runbook.md's "propose then
    wait for approval") assumes a human decides when one starts. This
    endpoint IS that human decision, expressed as a button click.

    Launched in a NEW, VISIBLE console window (CREATE_NEW_CONSOLE) - never
    hidden/detached like poller_autostart's background poller relaunch.
    The whole point is a supervisable session Marc can see and talk to,
    not one more silent background process."""
    known = set(_all_member_ids())
    if worker_id not in known:
        raise HTTPException(404, f"no such worker: {worker_id}")
    if sys.platform != "win32":
        raise HTTPException(501, "wake is only implemented for the Windows install this body runs on")
    wake_script = paths.HERE / "symphony_wake.ps1"
    if not wake_script.exists():
        raise HTTPException(500, f"wake script not found at {wake_script}")
    try:
        subprocess.Popen(
            ["powershell.exe", "-NoExit", "-File", str(wake_script), worker_id],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    except OSError as e:
        raise HTTPException(500, f"failed to launch wake console: {e}")
    return JSONResponse({"ok": True, "worker": worker_id})


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
    role: Optional[str] = None    # optional role/title label for the cockpit header


@app.get("/api/manager")
async def api_manager_get():
    """Read the current operator (the human running this Symphony) — powers the
    Settings 'You' field + the cockpit header identity (2026-07-31: replaced a
    hardcoded 3-person demo cast there - this is now the only source). config.manager
    is the single source. `role` is optional and unset by default (never a guessed
    fallback) - the header simply omits the role line until it's genuinely set."""
    mid = (config.get("manager", "id") or "").strip()
    tag = (config.get("manager", "tag") or "").strip()
    role = (config.get("manager", "role") or "").strip()
    return JSONResponse({"id": mid or None, "tag": tag or None, "role": role or None, "display_name": mid or None})


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
    """Set the operator's name (+ optional @-tag, + optional role/title) — the SHARED
    runtime write called by BOTH the compose naming step and the Settings 'You' field
    (last-confirm-wins). Writes config.manager.id/.tag/.role (settings.json,
    hot-reloaded). No slot-id and no born discriminator is touched — the operator is
    not a worker; this is display identity."""
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
    role = (body.role or "").strip()
    if len(role) > 60:
        raise HTTPException(400, "role too long (max 60 characters)")
    try:
        config.set_value(name, "manager", "id")
        config.set_value(tag, "manager", "tag")
        config.set_value(role, "manager", "role")
    except Exception as e:
        raise HTTPException(500, f"failed writing operator identity: {e}")
    return JSONResponse({"ok": True, "id": name, "tag": tag, "role": role or None, "display_name": name})


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
