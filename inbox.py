"""
inbox.py — two-way messaging hub for the team.

Every member has an inbox file: <workspace>/aria_sync/inboxes/<member_id>.md
The manager has one too: <workspace>/aria_sync/inboxes/<manager_id>.md

Anyone can write to anyone's inbox:
  - George via the dashboard
  - The substrate router (auto-routes detected intents)
  - One worker writing to another worker's inbox
  - A scheduler job posting digests

Each message becomes an append to the recipient's inbox, with structured frontmatter
so the recipient (or a hook) can parse and archive cleanly.

Format on disk (one message per block):
  ---
  from: george
  to: aria_builder
  ts: 2026-04-27T03:14:00
  message_id: m_abc123
  in_reply_to: null | m_xyz789
  ---
  Hey AB, the v17 build looks great. One question on the keychain wiring…

Replies: when a worker writes "[INBOX REPLY to: m_abc123]" in their live JSONL,
the tailer (with light extension) emits an inbox.reply event. The dashboard
threads them.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from bus import emit_event
from paths import ARIA_SYNC

INBOX_DIR = ARIA_SYNC / "inboxes"
ARCHIVE_DIR = INBOX_DIR / ".archive"

_lock = threading.Lock()


def _ensure_dirs() -> None:
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)


def _safe_member_path(base_dir: Path, member_id: str) -> Path:
    """Resolve member_id into a path INSIDE base_dir, or raise ValueError.

    Confirmed exploitable 2026-07-29: member_id/recipient/sender values reach
    here from an unvalidated HTTP field (POST /api/team_room/messages'
    `sender`, chained through a reaction -> inbox.send_message), and a bare
    f-string join let "../../evil" escape INBOX_DIR entirely, or an absolute
    path like "C:/Windows/Temp/pwned" discard it completely. Resolving the
    candidate and checking it's still relative to base_dir catches both -
    '..' traversal and an absolute-path override - regardless of what the
    caller passes."""
    candidate = (base_dir / f"{member_id}.md").resolve()
    if not candidate.is_relative_to(base_dir.resolve()):
        raise ValueError(f"invalid member id: {member_id!r}")
    return candidate


def _inbox_path(member_id: str) -> Path:
    return _safe_member_path(INBOX_DIR, member_id)


def _archive_path(member_id: str) -> Path:
    return _safe_member_path(ARCHIVE_DIR, member_id)


def _new_message_id() -> str:
    return "m_" + uuid.uuid4().hex[:10]


def _format_message(
    sender: str,
    recipient: str,
    body: str,
    message_id: str,
    in_reply_to: Optional[str] = None,
    cc: Optional[list[str]] = None,
    george_view: Optional[str] = None,
) -> str:
    ts = datetime.now().isoformat(timespec="seconds")
    cc_str = json.dumps(cc) if cc else "[]"
    gv_line = ""
    if george_view and george_view.strip():
        gv = " ".join(george_view.strip().split())
        gv_line = f"george_view: {gv}\n"
    return (
        f"\n---\n"
        f"from: {sender}\n"
        f"to: {recipient}\n"
        f"ts: {ts}\n"
        f"message_id: {message_id}\n"
        f"in_reply_to: {in_reply_to or 'null'}\n"
        f"cc: {cc_str}\n"
        f"{gv_line}"
        f"---\n"
        f"{body.strip()}\n"
    )


def send_message(
    sender: str,
    recipient: str,
    body: str,
    in_reply_to: Optional[str] = None,
    cc: Optional[list[str]] = None,
    george_view: Optional[str] = None,
) -> dict[str, Any]:
    """Append a message to recipient's inbox file. Emit bus event.
    Optional `george_view` adds the collapsed-view summary header."""
    _ensure_dirs()
    if not body or not body.strip():
        raise ValueError("body required")
    msg_id = _new_message_id()
    block = _format_message(sender, recipient, body, msg_id, in_reply_to, cc, george_view)
    path = _inbox_path(recipient)
    with _lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(block)
    emit_event(
        source="inbox", kind="inbox.sent",
        actor=sender, target=recipient,
        payload={
            "message_id": msg_id,
            "recipient": recipient,
            "sender": sender,
            "body_preview": body[:2000],
            "in_reply_to": in_reply_to,
            "cc": cc or [],
            "path": str(path),
        },
    )
    # If cc'd, also append to those inboxes (with cc marker)
    for cc_member in cc or []:
        if cc_member == recipient: continue
        cc_block = _format_message(sender, cc_member, f"[CC of message to {recipient}]\n\n{body}",
                                    _new_message_id(), in_reply_to=msg_id, cc=cc)
        with _lock:
            with _inbox_path(cc_member).open("a", encoding="utf-8") as f:
                f.write(cc_block)
    return {"ok": True, "message_id": msg_id, "recipient": recipient}


def broadcast(sender: str, recipients: list[str], body: str) -> dict[str, Any]:
    """Send the same message to multiple inboxes at once. Each gets its own message_id."""
    results = []
    for r in recipients:
        try:
            results.append(send_message(sender, r, body))
        except Exception as e:
            results.append({"ok": False, "recipient": r, "error": str(e)})
    emit_event(source="inbox", kind="inbox.broadcast",
               actor=sender, payload={"recipients": recipients, "ok_count": sum(1 for x in results if x.get("ok"))})
    return {"ok": all(r.get("ok") for r in results), "results": results}


def read_inbox(member_id: str) -> str:
    """Return raw contents of a member's inbox file."""
    p = _inbox_path(member_id)
    if not p.is_file(): return ""
    try: return p.read_text(encoding="utf-8")
    except OSError: return ""


# Parse messages out of an inbox file
_BLOCK_RE = re.compile(r"\n?---\n(.*?)---\n(.+?)(?=\n---\nfrom:|\Z)", re.DOTALL)
_FIELD_RE = re.compile(r"^(\w+):\s*(.+)$", re.MULTILINE)


def _parse_messages(text: str) -> list[dict[str, Any]]:
    if not text: return []
    out = []
    for m in _BLOCK_RE.finditer(text):
        meta_block, body = m.group(1), m.group(2).strip()
        fields = {f.group(1): f.group(2).strip() for f in _FIELD_RE.finditer(meta_block)}
        # Parse cc as json
        cc = []
        if fields.get("cc"):
            try: cc = json.loads(fields["cc"])
            except Exception: cc = []
        out.append({
            "from": fields.get("from"),
            "to": fields.get("to"),
            "ts": fields.get("ts"),
            "message_id": fields.get("message_id"),
            "in_reply_to": (fields.get("in_reply_to") if fields.get("in_reply_to") and fields.get("in_reply_to") != "null" else None),
            "cc": cc,
            "george_view": fields.get("george_view"),
            "body": body,
        })
    return out


def list_messages(member_id: str) -> list[dict[str, Any]]:
    """Parse the inbox file into structured messages, newest first."""
    out = _parse_messages(read_inbox(member_id))
    # Phase 2 of "delight George" substrate (TB tr_7008a84b3f 2026-05-03):
    # auto-fill george_view from heuristic when not author-provided.
    try:
        import chat_substrate as _cs
        _cs.attach_george_view(out)
    except Exception:
        pass
    return list(reversed(out))


def archive_inbox(member_id: str) -> int:
    """Move all current messages to the archive file, atomically. Return
    count moved.

    Fixed 2026-07-29: the read used to happen BEFORE acquiring the lock, with
    only the archive-append + truncate inside it - a message appended by a
    concurrent send_message() in that unlocked window was captured in
    NEITHER the archive nor the now-truncated live inbox, permanently lost.
    Reproduced with two threads in one process. Now the read is inside the
    same lock as the append+truncate, so the whole operation is one atomic
    step."""
    with _lock:
        text = read_inbox(member_id)
        if not text:
            return 0
        with _archive_path(member_id).open("a", encoding="utf-8") as f:
            f.write(text)
        _inbox_path(member_id).write_text("", encoding="utf-8")
    count = len(_parse_messages(text))
    emit_event(source="inbox", kind="inbox.archived",
               actor=member_id, payload={"count": count})
    return count


def inbox_counts() -> dict[str, int]:
    """Number of messages waiting in each inbox."""
    if not INBOX_DIR.is_dir(): return {}
    out = {}
    for p in INBOX_DIR.glob("*.md"):
        if p.name.startswith("."): continue
        member_id = p.stem
        out[member_id] = len(list_messages(member_id))
    return out
