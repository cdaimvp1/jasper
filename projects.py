"""Projects — multi-party threaded persistent work surface.

v0.01 minimum-viable: one file per project; markdown format; append-only thread.
Distinct from team_room (broadcast / ambient) and meetings (regimented / time-bounded).

Storage: aria_sync/projects/<id>_<slug>.md
Format:
    # <title>
    members: <comma-separated worker ids>
    status: active|paused|done|archived
    created: <iso>
    ---
    from: <sender>
    ts: <iso>
    message_id: <pp_xxx>
    ---
    <body>
    ---
    from: <sender>
    ...
"""
from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import chat_substrate as cs
import config
import members as members_mod

from paths import ARIA_SYNC as SYNC_ROOT  # de-hardcoded onto shared root (TEAM_WORKSPACE_ROOT env); rides joint bounce
PROJ_DIR = SYNC_ROOT / "projects"

VALID_STATUSES = {"active", "paused", "done", "archived"}

_lock = threading.Lock()


def _slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    return s[:40] or "project"


def _project_file(project_id: str) -> Optional[Path]:
    """Find the file for a project_id (file pattern: <id>_<slug>.md)."""
    PROJ_DIR.mkdir(parents=True, exist_ok=True)
    matches = list(PROJ_DIR.glob(f"{project_id}_*.md"))
    return matches[0] if matches else None


def _normalize_member_id(m: str) -> str:
    """Normalize a member identifier to canonical worker slug shape.

    Maps hyphens → underscores; lowercases; trims whitespace. Catches common typos
    like `canon-builder` → `canon_builder`. Worker slugs in this substrate are
    underscore_separated by convention.

    Scar (2026-07-17): this lowercasing used to be the ONLY normalization applied to
    project membership, while members.json/F9-fanout key off the worker's CASE-PRESERVED
    id (e.g. a composed worker named "Ledger", not "ledger"). That mismatch silently
    dropped every project notification to any mixed-case-named worker (F9's
    `recipient not in members` check is case-sensitive) — a real instance, not a
    hypothetical: a fresh CFIB cohort's "Ledger"/"Quill" workers never got notified of
    project posts, and even got wrongly told "you're not a member" they already were.
    Fixed by re-mapping through `_canonical_member_id` at read/fan-out time below,
    so a project's stored (lowercased) roster still resolves to the exact id F9 expects.
    """
    return m.strip().lower().replace("-", "_")


def _canonical_member_id(m: str) -> str:
    """Resolve a (possibly lowercased/normalized) member token to the EXACT, case-preserved
    worker id F9-fanout and members.json key on. Falls back to the input unchanged if no
    live member matches (keeps `george`/`manager`/an unregistered id passing through as-is)."""
    if not m:
        return m
    for mem in members_mod.list_members():
        mid = mem.get("id") or ""
        if mid.lower() == m.strip().lower():
            return mid
    return m


# Public alias — server_lean.py's membership-gate + FYI-mention checks need the same
# case-insensitive resolution (same root-cause class); exposed non-underscored for cross-module use.
canonical_member_id = _canonical_member_id


def create_project(*, title: str, members: list[str], creator: str = "george", status: str = "active",
                   source_message_id: Optional[str] = None) -> dict[str, Any]:
    """Create a new project file. Returns {project_id, file, ...}.
    If `source_message_id` is provided (e.g. boost-from-room flow), it's recorded in the header so
    repeat 🚀 reactions on the same source can navigate to the existing project (Option II semantics).
    """
    if not title.strip():
        raise ValueError("title required")
    if not members:
        raise ValueError("at least one member required")
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}")
    # Normalize member IDs (hyphen→underscore; lowercase) so notification routing matches inbox slugs
    members = [_normalize_member_id(m) for m in members if m.strip()]
    PROJ_DIR.mkdir(parents=True, exist_ok=True)
    project_id = "proj_" + uuid.uuid4().hex[:10]
    slug = _slugify(title)
    f = PROJ_DIR / f"{project_id}_{slug}.md"
    members_str = ",".join(sorted(set(members)))
    header_lines = [
        f"# {title.strip()}",
        f"members: {members_str}",
        f"status: {status}",
        f"created: {cs.now_iso()}",
        f"creator: {creator}",
    ]
    if source_message_id:
        header_lines.append(f"source_message_id: {source_message_id}")
    header = "\n".join(header_lines) + "\n"
    with _lock:
        f.write_text(header, encoding="utf-8")
    from bus import emit_event
    emit_event(source="projects", kind="project.created", actor=creator, target="projects",
               payload={"project_id": project_id, "title": title, "members": members_str,
                        "source_message_id": source_message_id or ""})
    return {"ok": True, "project_id": project_id, "file": str(f.relative_to(SYNC_ROOT.parent))}


def find_post_by_message_id(message_id: str) -> Optional[dict[str, Any]]:
    """Locate a project post by its message_id (e.g., pp_abc123).
    Used by reaction-notify routing to find the post author for `pp_*` reactions
    (server.py:1081 family) — team_room.list_messages doesn't see project posts."""
    if not message_id:
        return None
    PROJ_DIR.mkdir(parents=True, exist_ok=True)
    for f in sorted(PROJ_DIR.glob("proj_*.md")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        for p in _parse_posts(text):
            if p.get("message_id") == message_id and not p.get("deleted"):
                m = re.match(r"^(proj_[a-f0-9]{10})_(.+)\.md$", f.name)
                return {
                    "project_id": m.group(1) if m else f.stem,
                    "from": p.get("from", ""),
                    "body": p.get("body", "") or "",
                    "ts": p.get("ts", ""),
                }
    return None


def find_project_by_source(source_message_id: str) -> Optional[dict[str, Any]]:
    """Return the first project whose header `source_message_id` matches, else None.
    Used by the 🚀 reaction Option II path: repeat 🚀s on the same source post navigate to
    the existing project rather than re-opening the boost modal.
    """
    if not source_message_id:
        return None
    PROJ_DIR.mkdir(parents=True, exist_ok=True)
    for f in sorted(PROJ_DIR.glob("proj_*.md")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = _parse_header(text)
        if meta.get("source_message_id") == source_message_id:
            m = re.match(r"^(proj_[a-f0-9]{10})_(.+)\.md$", f.name)
            return {
                "project_id": m.group(1) if m else f.stem,
                "title": meta.get("title", f.stem),
                "members": meta.get("members", "").split(",") if meta.get("members") else [],
                "status": meta.get("status", "active"),
            }
    return None


def list_projects(*, include_archived: bool = False) -> list[dict[str, Any]]:
    """Return summary of all projects (parsed headers + post counts + last activity).
    By default excludes projects with status=archived (per Tier A3 @george directive); pass
    include_archived=True to include them (used by the Show archive toggle)."""
    PROJ_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for f in sorted(PROJ_DIR.glob("proj_*.md")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = _parse_header(text)
        if not include_archived and meta.get("status") == "archived":
            continue
        posts = _parse_posts(text)
        last_post = posts[-1] if posts else None
        # project_id from filename: proj_<10char>_<slug>.md
        m = re.match(r"^(proj_[a-f0-9]{10})_(.+)\.md$", f.name)
        project_id = m.group(1) if m else f.stem
        out.append({
            "project_id": project_id,
            "title": meta.get("title", f.stem),
            "members": meta.get("members", "").split(",") if meta.get("members") else [],
            "status": meta.get("status", "active"),
            "created": meta.get("created", ""),
            "creator": meta.get("creator", ""),
            "post_count": len(posts),
            "last_activity": last_post["ts"] if last_post else meta.get("created", ""),
            "last_post_from": last_post["from"] if last_post else "",
        })
    return out


def _parse_header(text: str) -> dict[str, str]:
    """Header is everything before the first `---` separator."""
    parts = re.split(r"\n---\n", text, maxsplit=1)
    head = parts[0]
    meta = {}
    title_match = re.match(r"^#\s+(.+)$", head, re.MULTILINE)
    if title_match:
        meta["title"] = title_match.group(1).strip()
    for line in head.splitlines():
        m = re.match(r"^(\w+):\s*(.+)$", line)
        if m and m.group(1) in ("members", "status", "created", "creator", "source_message_id"):
            meta[m.group(1)] = m.group(2).strip()
    return meta


def _parse_posts(text: str) -> list[dict[str, Any]]:
    """Posts are `---`-separated blocks after the header. Delegates to chat_substrate.

    Post-processes body for markers:
    - `<!--edits:N-->` prefix → strips marker, sets `edit_count = N`
    - `<!--deleted-->` prefix → strips marker, sets `deleted = True`
    - `<!--parent:pp_X-->` prefix → strips marker, sets `parent_message_id = pp_X`
    Markers may appear in any order at the start of the body.
    """
    posts = cs.parse_blocks(text, header_segments=1)
    for p in posts:
        body = p.get("body", "")
        # Strip up to 3 markers in any order
        for _ in range(3):
            em = re.match(r"^<!--edits:(\d+)-->\n", body)
            if em:
                p["edit_count"] = int(em.group(1))
                body = body[em.end():]
                continue
            dm = re.match(r"^<!--deleted-->\n", body)
            if dm:
                p["deleted"] = True
                body = body[dm.end():]
                continue
            pm = re.match(r"^<!--parent:(pp_[a-z0-9]{8,16})-->\n", body)
            if pm:
                p["parent_message_id"] = pm.group(1)
                body = body[pm.end():]
                continue
            break
        p["body"] = body
    # Phase 1+2 of "delight George" substrate (TB tr_7008a84b3f 2026-05-03):
    # auto-fill george_view from heuristic when not explicitly set by author.
    cs.attach_george_view(posts)
    return posts


def get_project(project_id: str, *, viewer: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Return project metadata + full thread. If `viewer` provided, attaches reactions per post."""
    f = _project_file(project_id)
    if not f:
        return None
    try:
        text = f.read_text(encoding="utf-8")
    except OSError:
        return None
    meta = _parse_header(text)
    posts = _parse_posts(text)
    if viewer is not None:
        # Reactions live in team_room.py reactions log (single shared log for all surfaces;
        # privacy filter applies — bookmark only visible to actor).
        import team_room as _tr
        mids = [p.get("message_id") for p in posts if p.get("message_id")]
        rxn_state = _tr.get_reactions_for_messages(mids, viewer=viewer)
        for p in posts:
            mid = p.get("message_id")
            if mid:
                p["reactions"] = rxn_state.get(mid, [])
    return {
        "project_id": project_id,
        "title": meta.get("title", project_id),
        "members": meta.get("members", "").split(",") if meta.get("members") else [],
        "status": meta.get("status", "active"),
        "created": meta.get("created", ""),
        "creator": meta.get("creator", ""),
        "source_message_id": meta.get("source_message_id", ""),
        "posts": posts,
        "post_count": len(posts),
    }


def rename_project(*, project_id: str, new_title: str, actor: str = "george") -> dict[str, Any]:
    """Rewrite the `# <title>` line in the project header. Renames the file slug to match the new title.
    Project_id is preserved (deep-links remain valid). Returns {ok, project_id, title}.
    """
    new_title = (new_title or "").strip()
    if not new_title:
        raise ValueError("title required")
    f = _project_file(project_id)
    if not f:
        raise ValueError(f"project not found: {project_id}")
    new_slug = _slugify(new_title)
    new_path = PROJ_DIR / f"{project_id}_{new_slug}.md"
    with _lock:
        text = f.read_text(encoding="utf-8")
        # Rewrite the first markdown heading line `# <old>` → `# <new>`. Header is everything before
        # the first `\n---\n` separator; only touch the title line.
        new_text = re.sub(r"^#\s+.+$", f"# {new_title}", text, count=1, flags=re.MULTILINE)
        if new_path != f:
            f.write_text(new_text, encoding="utf-8")
            f.rename(new_path)
        else:
            f.write_text(new_text, encoding="utf-8")
    from bus import emit_event
    emit_event(source="projects", kind="project.renamed", actor=actor, target="projects",
               payload={"project_id": project_id, "new_title": new_title})
    return {"ok": True, "project_id": project_id, "title": new_title}


def list_archived() -> list[dict[str, Any]]:
    """Return archived items: BOTH (a) files in `_archive/` (soft-deleted via 🗑) AND (b) projects
    in active dir with status=archived (via 📦 Archive overflow). Each carries an `archive_kind`
    flag so the UI can offer the right restore action (Restore from _archive/ vs Unarchive flag)."""
    out = []
    # (a) _archive/ directory — soft-deleted files
    archive_dir = PROJ_DIR / "_archive"
    if archive_dir.exists():
        for f in sorted(archive_dir.glob("proj_*.md")):
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            meta = _parse_header(text)
            posts = _parse_posts(text)
            last_post = posts[-1] if posts else None
            m = re.match(r"^(proj_[a-f0-9]{10})_(.+)\.md$", f.name)
            project_id = m.group(1) if m else f.stem
            out.append({
                "project_id": project_id,
                "title": meta.get("title", f.stem),
                "members": meta.get("members", "").split(",") if meta.get("members") else [],
                "status": meta.get("status", "active"),
                "created": meta.get("created", ""),
                "creator": meta.get("creator", ""),
                "post_count": len(posts),
                "last_activity": last_post["ts"] if last_post else meta.get("created", ""),
                "last_post_from": last_post["from"] if last_post else "",
                "archived": True,
                "archive_kind": "deleted",  # file moved; restore via /restore endpoint
            })
    # (b) active dir with status=archived — flagged but not deleted
    PROJ_DIR.mkdir(parents=True, exist_ok=True)
    for f in sorted(PROJ_DIR.glob("proj_*.md")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = _parse_header(text)
        if meta.get("status") != "archived":
            continue
        posts = _parse_posts(text)
        last_post = posts[-1] if posts else None
        m = re.match(r"^(proj_[a-f0-9]{10})_(.+)\.md$", f.name)
        project_id = m.group(1) if m else f.stem
        out.append({
            "project_id": project_id,
            "title": meta.get("title", f.stem),
            "members": meta.get("members", "").split(",") if meta.get("members") else [],
            "status": meta.get("status", "active"),
            "created": meta.get("created", ""),
            "creator": meta.get("creator", ""),
            "post_count": len(posts),
            "last_activity": last_post["ts"] if last_post else meta.get("created", ""),
            "last_post_from": last_post["from"] if last_post else "",
            "archived": True,
            "archive_kind": "flagged",  # status=archived; restore via /status endpoint (set to active)
        })
    return out


def restore_project(*, project_id: str, actor: str = "george") -> dict[str, Any]:
    """Move project file from `_archive/` back to active dir. Sets status to `active`.
    Returns {ok, project_id}."""
    archive_dir = PROJ_DIR / "_archive"
    matches = list(archive_dir.glob(f"{project_id}_*.md")) if archive_dir.exists() else []
    if not matches:
        raise ValueError(f"archived project not found: {project_id}")
    f = matches[0]
    new_path = PROJ_DIR / f.name
    with _lock:
        text = f.read_text(encoding="utf-8")
        # Ensure status is active (in case the file was archived with status=archived)
        new_text = re.sub(r"^status:\s*\S+\s*$", "status: active", text, count=1, flags=re.MULTILINE)
        f.rename(new_path)
        new_path.write_text(new_text, encoding="utf-8")
    from bus import emit_event
    emit_event(source="projects", kind="project.restored", actor=actor, target="projects",
               payload={"project_id": project_id})
    return {"ok": True, "project_id": project_id}


def delete_project(*, project_id: str, actor: str = "george") -> dict[str, Any]:
    """Soft-delete: move project file to `aria_sync/projects/_archive/`. Preserves audit trail;
    can be restored manually. Returns {ok, project_id, archived_path}.
    """
    f = _project_file(project_id)
    if not f:
        raise ValueError(f"project not found: {project_id}")
    archive_dir = PROJ_DIR / "_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived_path = archive_dir / f.name
    with _lock:
        f.rename(archived_path)
    from bus import emit_event
    emit_event(source="projects", kind="project.deleted", actor=actor, target="projects",
               payload={"project_id": project_id, "archived_path": str(archived_path.relative_to(SYNC_ROOT.parent))})
    return {"ok": True, "project_id": project_id, "archived_path": str(archived_path.relative_to(SYNC_ROOT.parent))}


def add_members(*, project_id: str, new_members: list[str], actor: str = "george") -> dict[str, Any]:
    """Add one or more members to an existing project. Rewrites the `members:` header line.
    De-dups against current members. Returns {ok, project_id, added: [...], members: [...]}.
    Used by post_to_project's @-mention pull-in flow (Tier A1 per George pp_5fb0a0abcc origin)."""
    f = _project_file(project_id)
    if not f:
        raise ValueError(f"project not found: {project_id}")
    new_members = [_normalize_member_id(m) for m in new_members if m and m.strip()]
    if not new_members:
        return {"ok": True, "project_id": project_id, "added": [], "members": []}
    with _lock:
        text = f.read_text(encoding="utf-8")
        meta = _parse_header(text)
        existing = set(meta.get("members", "").split(",")) if meta.get("members") else set()
        existing.discard("")
        added = [m for m in new_members if m not in existing]
        if not added:
            return {"ok": True, "project_id": project_id, "added": [], "members": sorted(existing)}
        merged = sorted(existing | set(added))
        members_str = ",".join(merged)
        # Rewrite the `members:` line in-place
        new_text = re.sub(r"^members:\s*.+$", f"members: {members_str}", text, count=1, flags=re.MULTILINE)
        f.write_text(new_text, encoding="utf-8")
    from bus import emit_event
    emit_event(source="projects", kind="project.members_added", actor=actor, target="projects",
               payload={"project_id": project_id, "added": added, "members": merged})
    return {"ok": True, "project_id": project_id, "added": added, "members": merged}


def remove_members(*, project_id: str, members_to_remove: list[str], actor: str = "george") -> dict[str, Any]:
    """Remove one or more members from an existing project. Rewrites the `members:` header line.
    Refuses to remove the creator (project always retains its creator). Returns
    {ok, project_id, removed: [...], members: [...]}."""
    f = _project_file(project_id)
    if not f:
        raise ValueError(f"project not found: {project_id}")
    targets = [_normalize_member_id(m) for m in members_to_remove if m and m.strip()]
    if not targets:
        return {"ok": True, "project_id": project_id, "removed": [], "members": []}
    with _lock:
        text = f.read_text(encoding="utf-8")
        meta = _parse_header(text)
        existing = set(meta.get("members", "").split(",")) if meta.get("members") else set()
        existing.discard("")
        creator = meta.get("creator", "").strip()
        # Refuse to remove the creator (project always retains its creator)
        targets = [t for t in targets if t != creator]
        removed = [t for t in targets if t in existing]
        if not removed:
            return {"ok": True, "project_id": project_id, "removed": [], "members": sorted(existing)}
        merged = sorted(existing - set(removed))
        members_str = ",".join(merged) if merged else ""
        new_text = re.sub(r"^members:\s*.+$", f"members: {members_str}", text, count=1, flags=re.MULTILINE)
        f.write_text(new_text, encoding="utf-8")
    from bus import emit_event
    emit_event(source="projects", kind="project.members_removed", actor=actor, target="projects",
               payload={"project_id": project_id, "removed": removed, "members": merged})
    return {"ok": True, "project_id": project_id, "removed": removed, "members": merged}


def update_status(*, project_id: str, new_status: str, actor: str = "george") -> dict[str, Any]:
    """Update a project's status. Rewrites the header in-place; preserves all posts."""
    if new_status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}")
    f = _project_file(project_id)
    if not f:
        raise ValueError(f"project not found: {project_id}")
    with _lock:
        text = f.read_text(encoding="utf-8")
        # Replace status: line in header (everything before first `\n---\n`)
        parts = re.split(r"(\n---\n)", text, maxsplit=1)
        if len(parts) >= 1:
            head = parts[0]
            new_head = re.sub(r"^status:\s*\S+\s*$", f"status: {new_status}", head, count=1, flags=re.MULTILINE)
            new_text = new_head + ("".join(parts[1:]) if len(parts) > 1 else "")
            f.write_text(new_text, encoding="utf-8")
    from bus import emit_event
    emit_event(source="projects", kind="project.status_changed", actor=actor, target="projects",
               payload={"project_id": project_id, "new_status": new_status})
    return {"ok": True, "project_id": project_id, "status": new_status}


_POST_BLOCK_RE = re.compile(
    # Per Phase 1 george_view substrate (TB tr_38c55b8899) — optional `george_view:` line
    # between message_id and the closing `---`. Without this, edit_post + delete_post fail
    # on any post that carries a george_view header (substrate-self-reference scar #20).
    r"\n---\nfrom:\s*(?P<from>[^\n]+?)\s*\nts:\s*(?P<ts>\S+)\s*\nmessage_id:\s*(?P<mid>\S+)(?:\s*\ngeorge_view:\s*(?P<gv>[^\n]+))?\s*\n---\n(?P<body>.*?)(?=\n---\n|\Z)",
    re.DOTALL,
)


def edit_post(*, project_id: str, message_id: str, new_body: str, actor: str) -> dict[str, Any]:
    """In-place rewrite of a post's body. Only the actor (sender of original post) can edit.
    Tracks edit count via leading `<!--edits:N-->\\n` marker in body.
    """
    if not new_body.strip():
        raise ValueError("body required")
    f = _project_file(project_id)
    if not f:
        raise ValueError(f"project not found: {project_id}")
    with _lock:
        text = f.read_text(encoding="utf-8")
        # Find the post block matching message_id; rewrite body
        edited = False
        edit_count = 0
        def replacer(m):
            nonlocal edited, edit_count
            if m.group("mid") != message_id:
                return m.group(0)
            if m.group("from") != actor:
                raise PermissionError(f"only the sender ({m.group('from')}) can edit this post")
            # Parse existing edit count from body
            body = m.group("body").rstrip()
            ec_match = re.match(r"^<!--edits:(\d+)-->\n", body)
            if ec_match:
                edit_count = int(ec_match.group(1)) + 1
                body_stripped = body[ec_match.end():]
            else:
                edit_count = 1
                body_stripped = body
            edited = True
            new_body_with_marker = f"<!--edits:{edit_count}-->\n{new_body.strip()}"
            return f"\n---\nfrom: {m.group('from')}\nts: {m.group('ts')}\nmessage_id: {m.group('mid')}\n---\n{new_body_with_marker}\n"
        new_text = _POST_BLOCK_RE.sub(replacer, text)
        if not edited:
            raise ValueError(f"post not found: {message_id}")
        # Trim trailing extra newlines that may have been introduced
        f.write_text(new_text, encoding="utf-8")
    from bus import emit_event
    emit_event(source="projects", kind="project.post_edited", actor=actor, target="projects",
               payload={"project_id": project_id, "message_id": message_id, "edit_count": edit_count})
    return {"ok": True, "message_id": message_id, "edit_count": edit_count}


def delete_post(*, project_id: str, message_id: str, actor: str) -> dict[str, Any]:
    """Soft-delete: replace body with deletion marker. Only the actor can delete.
    The post block stays in the file; body becomes `<!--deleted-->\\n_(deleted by ACTOR at TS)_`.
    """
    f = _project_file(project_id)
    if not f:
        raise ValueError(f"project not found: {project_id}")
    ts = cs.now_iso()
    with _lock:
        text = f.read_text(encoding="utf-8")
        deleted = False
        def replacer(m):
            nonlocal deleted
            if m.group("mid") != message_id:
                return m.group(0)
            # Per @george tr_250dc5fab1 (2026-05-09) — non-member stray-post cleanup.
            # Allow project creator + manager (george) to delete posts they didn't author.
            # Sender always retains delete-self.
            proj = get_project(project_id) or {}
            creator = proj.get("creator")
            mgr = config.get("manager", "id") or "manager"
            allowed = {m.group("from"), creator, mgr}
            if actor not in allowed:
                raise PermissionError(
                    f"only sender ({m.group('from')}), creator ({creator}), or manager ({mgr}) can delete · actor={actor}")
            deleted = True
            new_body = f"<!--deleted-->\n_(deleted by @{actor} at {ts})_"
            return f"\n---\nfrom: {m.group('from')}\nts: {m.group('ts')}\nmessage_id: {m.group('mid')}\n---\n{new_body}\n"
        new_text = _POST_BLOCK_RE.sub(replacer, text)
        if not deleted:
            raise ValueError(f"post not found: {message_id}")
        f.write_text(new_text, encoding="utf-8")
    from bus import emit_event
    emit_event(source="projects", kind="project.post_deleted", actor=actor, target="projects",
               payload={"project_id": project_id, "message_id": message_id})
    return {"ok": True, "message_id": message_id, "deleted": True}


def post_to_project(*, project_id: str, sender: str, body: str,
                    parent_message_id: Optional[str] = None,
                    george_view: Optional[str] = None) -> dict[str, Any]:
    """Append a post to the project thread. Delegates to chat_substrate.append_block.
    If `parent_message_id` is provided, prepends a `<!--parent:pp_X-->` marker so
    the post renders as a threaded reply under its parent.
    Optional `george_view` adds the collapsed-view summary George reads."""
    f = _project_file(project_id)
    if not f:
        raise ValueError(f"project not found: {project_id}")
    write_body = body
    if parent_message_id:
        if not re.match(r"^pp_[a-z0-9]{8,16}$", parent_message_id):
            raise ValueError(f"invalid parent_message_id: {parent_message_id}")
        write_body = f"<!--parent:{parent_message_id}-->\n{body}"
    payload_extra: dict[str, Any] = {"project_id": project_id}
    if parent_message_id:
        payload_extra["parent_message_id"] = parent_message_id
    result = cs.append_block(
        file=f,
        sender=sender,
        body=write_body,
        message_id_prefix="pp",
        lock=_lock,
        event_source="projects",
        event_kind="project.post",
        event_payload_extra=payload_extra,
        george_view=george_view,
    )
    # Auto-archive related desk items when manager responds in a project (per @george pp_xxx)
    try:
        manager_id = config.get("manager", "id") or "manager"
        if sender == manager_id:
            import desk as _desk
            _desk.auto_addressed_for_project(project_id, actor=sender)
    except Exception:
        pass
    # Inbox fan-out to other project members. Moved here from server.api_project_post
    # 2026-05-30 per @george tr_3b3a7d2447 + @tb diagnosis pp_f14231be19: /api/post path
    # (api_unified_post) was calling post_to_project but skipping the fan-out that lived
    # only in the legacy /api/projects/{id}/posts handler. Caused Quinn-poller silent-miss
    # on all team_builder→Symphony Comms posts (#190). Single source of truth: the fan-out
    # belongs to the projects module since it owns the routing. Fail-loud: any inbox.send_message
    # failure logs but doesn't fail the post (the post itself already succeeded).
    new_pp_id = result.get("message_id")
    if new_pp_id:
        try:
            import inbox as _inbox
            proj_meta = get_project(project_id)
            if proj_meta:
                # canonicalize each stored (lowercased) member token to the exact worker id
                # F9-fanout matches on — else a mixed-case worker id silently drops (scar above).
                recipients = [_canonical_member_id(m) for m in proj_meta.get("members", []) if m]
                recipients = [r for r in recipients if r != sender]
                title = proj_meta.get("title", "") or "(no-title)"
                preview = body  # full body per @ab tr_543ee39e37 + @george tr_cddf88ef03
                reply_hint = (f"\n\n[REPLY HINT · proj=*{title}* · pp={new_pp_id}] "
                              f"post(sender=\"<you>\", to=\"@{sender}\", "
                              f"reply_to=\"{new_pp_id}\", body=\"...\")  · reply_to auto-routes to project")
                for recipient in recipients:
                    try:
                        _inbox.send_message(
                            sender=sender,
                            recipient=recipient,
                            body=f"💬 New post in project *{title}* (`{project_id}`) by @{sender}: {preview}{reply_hint}",
                        )
                    except Exception:
                        pass  # per-recipient best-effort; don't fail post on routing
        except Exception:
            pass  # fan-out is best-effort; substrate must not gate post-write on it
    return {"ok": True, "project_id": project_id, "message_id": new_pp_id}


def search_posts(query: str, *, limit: int = 50, include_archived: bool = False) -> list[dict[str, Any]]:
    """Substring-match posts across all projects (case-insensitive).
    Returns most-recent-first list, capped at `limit`. Each hit carries
    project_id + title for UI grouping; body is the full post body so the
    UI can highlight matches in context."""
    q = (query or "").strip()
    if not q:
        return []
    needle = q.lower()
    PROJ_DIR.mkdir(parents=True, exist_ok=True)
    hits: list[dict[str, Any]] = []
    for f in sorted(PROJ_DIR.glob("proj_*.md")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = _parse_header(text)
        if not include_archived and meta.get("status") == "archived":
            continue
        m = re.match(r"^(proj_[a-f0-9]{10})_(.+)\.md$", f.name)
        project_id = m.group(1) if m else f.stem
        title = meta.get("title", f.stem)
        for p in _parse_posts(text):
            body = p.get("body", "") or ""
            if p.get("deleted"):
                continue
            if needle in body.lower():
                hits.append({
                    "project_id": project_id,
                    "project_title": title,
                    "message_id": p.get("message_id", ""),
                    "from": p.get("from", ""),
                    "ts": p.get("ts", ""),
                    "body": body,
                })
    hits.sort(key=lambda h: h.get("ts", ""), reverse=True)
    return hits[:limit]


def init_projects() -> None:
    """Ensure the projects directory exists."""
    PROJ_DIR.mkdir(parents=True, exist_ok=True)
