"""
watchers.py — observers that emit events into the bus.

  - FsWatcher: watchdog observer on workspace subdirs (signals/workers/threads/decisions/doctrine)
  - JsonlTailer: polls ~/.claude/projects/*/*.jsonl, tails new lines, runs intent classifier

Same shape as v1 but cleaner: no reader.py dependency, uses members.py for slug resolution,
filters OneDrive temp files, coalesces same-path-same-kind bursts within 0.5s.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Optional

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from bus import emit_event
from intent import classify_message, resolve_member
from paths import ARIA_SYNC, CC_PROJECTS_ROOT


# ---------------------------------------------------------------------------
# Filesystem watcher
# ---------------------------------------------------------------------------
WATCHED_SUBDIRS = ("signals", "workers", "threads", "decisions", "doctrine")

_TEMP_PATTERNS = (
    re.compile(r"\.tmp\.\d+\.\d+$"),
    re.compile(r"\.tmp$"),
    re.compile(r"~$"),
    re.compile(r"\.swp$"),
    re.compile(r"\.swo$"),
    re.compile(r"^\."),
    re.compile(r"\.lock$"),
    re.compile(r"\.crdownload$"),
)


def _is_temp_file(name: str) -> bool:
    return any(p.search(name) for p in _TEMP_PATTERNS)


def _tool_input_preview(name: str, input_dict: dict) -> str:
    if not isinstance(input_dict, dict): return ""
    n = (name or "").lower()
    def _trunc(s, lim=160):
        s = str(s or "").replace("\n", " ⏎ ").replace("\r", " ")
        return s if len(s) <= lim else s[:lim] + "…"
    if n == "bash": return _trunc(input_dict.get("command") or input_dict.get("description") or "", 200)
    if n in ("read", "edit", "multiedit", "write", "notebookedit"):
        path = input_dict.get("file_path") or input_dict.get("path") or ""
        return path[-160:] if len(path) > 160 else path
    if n == "grep":
        pat = input_dict.get("pattern") or ""
        path = input_dict.get("path") or ""
        glob = input_dict.get("glob") or ""
        bits = [f"/{pat}/" if pat else ""]
        if path: bits.append(f"in {path[-80:]}")
        if glob: bits.append(f"glob={glob}")
        return _trunc(" ".join(b for b in bits if b), 200)
    if n == "glob": return _trunc(input_dict.get("pattern") or "", 160)
    if n in ("task", "agent"):
        return _trunc(input_dict.get("description") or input_dict.get("prompt") or "", 160)
    if n == "todowrite":
        todos = input_dict.get("todos") or []
        if isinstance(todos, list) and todos:
            first = todos[0]
            label = first.get("subject") or first.get("content") or first.get("description") or "?"
            return f"{len(todos)} todo · first: {_trunc(label, 100)}"
        return f"{len(todos)} todos"
    if n == "webfetch": return _trunc(input_dict.get("url") or "", 160)
    if n == "websearch": return _trunc(input_dict.get("query") or "", 160)
    if n == "skill": return _trunc(input_dict.get("skill") or "", 80)
    for k, v in input_dict.items():
        if isinstance(v, str) and v: return f"{k}={_trunc(v, 140)}"
    return ""


class _FsHandler(FileSystemEventHandler):
    COALESCE_WINDOW_S = 0.5

    def __init__(self, root: Path):
        self.root = root
        self._last_emit: dict[str, tuple[float, str]] = {}
        self._coalesce_lock = threading.Lock()

    def _emit(self, kind: str, src_path: str, is_dir: bool):
        if is_dir: return
        name = Path(src_path).name
        if _is_temp_file(name): return
        try: rel = str(Path(src_path).relative_to(self.root))
        except ValueError: rel = src_path

        now = time.time()
        with self._coalesce_lock:
            last = self._last_emit.get(rel)
            if last and (now - last[0]) < self.COALESCE_WINDOW_S and last[1] == kind:
                return
            self._last_emit[rel] = (now, kind)
            if len(self._last_emit) > 200:
                cutoff = now - 5
                self._last_emit = {p: v for p, v in self._last_emit.items() if v[0] > cutoff}

        first = rel.split("/", 1)[0] if "/" in rel else rel
        emit_event(source="fs_watcher", kind=f"fs.{kind}", actor=None, target=rel,
                   payload={"path": src_path, "rel": rel, "subdir": first})

    def on_created(self, event): self._emit("created", event.src_path, event.is_directory)
    def on_modified(self, event): self._emit("modified", event.src_path, event.is_directory)
    def on_deleted(self, event): self._emit("deleted", event.src_path, event.is_directory)
    def on_moved(self, event): self._emit("moved", event.dest_path, event.is_directory)


_fs_observer: Optional[Observer] = None


def start_fs_watcher() -> None:
    global _fs_observer
    if _fs_observer is not None: return
    obs = Observer()
    handler = _FsHandler(ARIA_SYNC)
    for sub in WATCHED_SUBDIRS:
        path = ARIA_SYNC / sub
        if path.is_dir():
            obs.schedule(handler, str(path), recursive=True)
    obs.daemon = True
    obs.start()
    _fs_observer = obs
    emit_event(source="server", kind="watcher.started",
               payload={"watcher": "fs", "subdirs": list(WATCHED_SUBDIRS)})


def stop_fs_watcher() -> None:
    global _fs_observer
    if _fs_observer is not None:
        try:
            _fs_observer.stop()
            _fs_observer.join(timeout=2)
        except Exception: pass
        _fs_observer = None


# ---------------------------------------------------------------------------
# JSONL tailer
# ---------------------------------------------------------------------------
_offsets: dict[str, int] = {}
_offsets_lock = threading.Lock()
_tailer_thread: Optional[threading.Thread] = None
_tailer_stop = threading.Event()
TAIL_INTERVAL_S = 2.0


def _list_active_jsonls() -> list[Path]:
    if not CC_PROJECTS_ROOT.is_dir(): return []
    out = []
    cutoff = time.time() - 3 * 3600
    for pdir in CC_PROJECTS_ROOT.iterdir():
        if not pdir.is_dir(): continue
        for jp in pdir.glob("*.jsonl"):
            try:
                if jp.stat().st_mtime >= cutoff: out.append(jp)
            except OSError: pass
    return out


def _read_new_lines(path: Path) -> list[str]:
    key = str(path)
    try: size = path.stat().st_size
    except OSError: return []
    with _offsets_lock:
        prev = _offsets.get(key)
    if prev is None:
        with _offsets_lock: _offsets[key] = size
        return []
    if size < prev:
        with _offsets_lock: _offsets[key] = 0
        prev = 0
    if size == prev: return []
    try:
        with path.open("rb") as f:
            f.seek(prev)
            chunk = f.read(size - prev)
    except OSError: return []
    with _offsets_lock: _offsets[key] = size
    try: text = chunk.decode("utf-8", errors="replace")
    except Exception: return []
    return [ln for ln in text.split("\n") if ln.strip()]


def _process_line(jsonl_path: Path, line: str) -> None:
    try: rec = json.loads(line)
    except Exception: return
    rec_type = rec.get("type")
    if rec_type not in ("user", "assistant"): return
    msg = rec.get("message") or {}
    role = msg.get("role") or rec_type
    content = msg.get("content")
    text_parts: list[str] = []
    tool_calls: list[dict[str, str]] = []
    if isinstance(content, str): text_parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict): continue
            btype = block.get("type")
            if btype == "text": text_parts.append(block.get("text") or "")
            elif btype == "tool_use":
                tool_calls.append({
                    "name": block.get("name") or "tool",
                    "preview": _tool_input_preview(block.get("name") or "", block.get("input") or {}),
                })
    text = "\n".join(p for p in text_parts if p).strip()

    session_id = rec.get("sessionId") or msg.get("sessionId") or ""
    cwd = rec.get("cwd") or ""
    ts_iso = rec.get("timestamp") or msg.get("timestamp") or ""
    actor = resolve_member(session_id=session_id, cwd=cwd, jsonl_path=str(jsonl_path))

    # Extract token usage from assistant messages (CC writes this to JSONL)
    usage = msg.get("usage") if isinstance(msg, dict) else None
    if usage and isinstance(usage, dict):
        usage_clean = {
            "input": int(usage.get("input_tokens") or 0),
            "output": int(usage.get("output_tokens") or 0),
            "cache_create": int(usage.get("cache_creation_input_tokens") or 0),
            "cache_read": int(usage.get("cache_read_input_tokens") or 0),
        }
        usage_clean["total"] = sum(usage_clean.values())
        usage_clean["model"] = msg.get("model") or rec.get("model") or ""
    else:
        usage_clean = None

    payload = {
        "session_id": session_id,
        "session_short": session_id[:8] if session_id else "",
        "role": role,
        "text": text[:2000],
        "tool_calls": tool_calls,
        "ts_iso": ts_iso,
        "cwd": cwd,
        "jsonl_path": str(jsonl_path),
    }
    if usage_clean:
        payload["usage"] = usage_clean
    emit_event(source="jsonl_tailer", kind=f"jsonl.{role}", actor=actor, target=None, payload=payload)

    # Skip intent detection for actors flagged as meta-discussion sources
    # (e.g. team_builder writes prose ABOUT the substrate, polluting the bus).
    try:
        import config as _config
        excluded = _config.get("router", "intent_excluded_actors", default=[]) or []
    except Exception:
        excluded = []
    if role == "assistant" and text and actor not in excluded:
        intents = classify_message(text, actor=actor)
        for intent in intents:
            emit_event(
                source="intent", kind=f"intent.{intent['kind']}",
                actor=actor, target=intent.get("target"),
                payload={
                    "session_id": session_id, "session_short": session_id[:8] if session_id else "",
                    "snippet": intent.get("snippet", "")[:280],
                    "confidence": intent.get("confidence", 0.5),
                    "ts_iso": ts_iso,
                },
            )
    # Skip inbox-reply detection for excluded actors too
    if role == "assistant" and text and actor not in excluded:
        # Detect inbox replies. Strict: marker must be at the START of a line,
        # alone on its line, followed by the reply body until next marker or end.
        marker_re = re.compile(
            r"^\s*\[INBOX REPLY(?:\s+to:\s*(m_[a-z0-9]+))?\]\s*$\n+(.+?)(?=^\s*\[INBOX REPLY\b|\Z)",
            re.IGNORECASE | re.MULTILINE | re.DOTALL,
        )
        # Strip inline code spans before scanning so backticked syntax examples are ignored
        scan_text = re.sub(r"`[^`\n]*`", "", text)
        for m in marker_re.finditer(scan_text):
            in_reply_to = m.group(1)
            reply_body = (m.group(2) or "").strip()
            if len(reply_body) < 10:
                continue
            emit_event(
                source="inbox", kind="inbox.reply",
                actor=actor, target=in_reply_to,
                payload={
                    "from_member": actor,
                    "in_reply_to": in_reply_to,
                    "reply_preview": reply_body[:400],
                    "session_id": session_id,
                    "ts_iso": ts_iso,
                },
            )


def _tailer_loop() -> None:
    while not _tailer_stop.is_set():
        try:
            for jp in _list_active_jsonls():
                for ln in _read_new_lines(jp):
                    _process_line(jp, ln)
        except Exception as e:
            emit_event(source="jsonl_tailer", kind="watcher.error", payload={"error": str(e)})
        _tailer_stop.wait(TAIL_INTERVAL_S)


def start_jsonl_tailer() -> None:
    global _tailer_thread
    if _tailer_thread is not None and _tailer_thread.is_alive(): return
    _tailer_stop.clear()
    t = threading.Thread(target=_tailer_loop, name="jsonl-tailer", daemon=True)
    t.start()
    _tailer_thread = t
    emit_event(source="server", kind="watcher.started",
               payload={"watcher": "jsonl_tailer", "interval_s": TAIL_INTERVAL_S})


def stop_jsonl_tailer() -> None:
    global _tailer_thread
    _tailer_stop.set()
    if _tailer_thread is not None:
        _tailer_thread.join(timeout=3)
        _tailer_thread = None


# ---------------------------------------------------------------------------
# TR @-mention fan-out tailer REMOVED from the lean product body (TB 2026-07-14).
# It was DISABLED since the 2026-05-06 inbox-flood incident (started nowhere), and
# carried a hardcoded cohort mention-map (_TR_MENTION_MAP/_TR_ARCHIVED_WORKERS) —
# the born-body-cohort-scoping class. server_lean's members-driven F9 fanout handles
# TR @-mentions generically from members.json, so the dead tailer is gone entirely.
# ---------------------------------------------------------------------------


def start_watchers() -> None:
    start_fs_watcher()
    start_jsonl_tailer()
    # tr_fanout_tailer DISABLED post-INCIDENT 2026-05-06 20:24 ET ·
    # historical-message reprocessing flooded cohort inboxes · dedup-on-first-start
    # bug · needs proper offline testing before re-enable.


def stop_watchers() -> None:
    stop_jsonl_tailer()
    stop_fs_watcher()
    emit_event(source="server", kind="watcher.stopped", payload={})
