#!/usr/bin/env python3
"""symphony_precompact_digest.py — PreCompact hook for BORN Symphony workers (layer-5, Mechanism B).

WHAT: at every compact boundary, write a small local "what was I doing / what's next"
digest so the §0 wake-protocol can resume the SAME task on next wake, instead of
waking clean and losing the mid-task thread. This is the born-worker equivalent of
our own active.md pre-compaction stow — local-only, no cloud mirror (unlike
precompact_soul_mirror.py, which this borrows its PreCompact-hook SHAPE from, not
its Fabric-upload machinery — a born worker doesn't need or have that).

V1 SAFETY CONTRACT (same invariant class as precompact_soul_mirror.py):
- ALWAYS exits 0. NEVER blocks the compaction (exit 2 / {"decision":"block"} would).
- Any failure (unreadable transcript, unresolvable worker, write error) → skip
  quietly, never crash the wake.
- Digest is BOUNDED (size-capped) and OVERWRITES (rolling window, not an
  accumulating journal — HOMEOSTASIS dim-2 discipline: an active file is
  current state, not a log).

Registered in a born worker's body/.claude/settings.json → hooks.PreCompact,
AFTER Coby's hook-registration entry (his file, his lane) — this script is what
that entry points at.

Hook input (verified against CC docs, 2026-07-18): session_id, transcript_path,
cwd, permission_mode, hook_event_name. NOTE: no "trigger" field (auto vs manual
isn't distinguishable at this hook) — digest content doesn't depend on it.
Transcript is written ASYNCHRONOUSLY and may lag the in-memory conversation —
treat it as best-effort, not authoritative-complete.
"""
import json
import os
import sys
import time
from pathlib import Path

MAX_DIGEST_CHARS = 4000          # bounded — a resume-cue, not a transcript dump
TAIL_LINES_TO_SCAN = 60          # tail-read only, never the whole transcript


def _worker() -> str:
    """Resolve the born worker's identity. SYMPHONY_WORKER is the born-install
    convention (set by symphony_wake.sh before exec claude)."""
    return os.environ.get("SYMPHONY_WORKER", "").strip()


def _team_home() -> Path:
    env = os.environ.get("TEAM_HOME", "").strip()
    if env and Path(env).is_dir():
        return Path(env)
    # fall back to cwd's expected body-parent layout (body/.. == TEAM_HOME)
    return Path.cwd().parent


def _tail_transcript_text(transcript_path: str, max_lines: int) -> str:
    """Best-effort tail-read of the transcript JSONL — last N assistant/user
    text turns, not the whole file. Returns '' on any failure (never raises)."""
    p = Path(transcript_path) if transcript_path else None
    if not p or not p.is_file():
        return ""
    try:
        # bounded tail read: read the file's last ~64KB, not the whole thing,
        # then take the last N complete JSON lines from that window.
        size = p.stat().st_size
        read_from = max(0, size - 65536)
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            if read_from:
                f.seek(read_from)
                f.readline()  # discard partial line from the seek point
            lines = f.readlines()
    except Exception:
        return ""

    texts = []
    for line in lines[-max_lines:]:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        msg = rec.get("message") or {}
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            snippet = content
        elif isinstance(content, list):
            parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            snippet = " ".join(parts)
        else:
            snippet = ""
        snippet = snippet.strip()
        if snippet:
            texts.append("{}: {}".format(role, snippet[:400]))
    return "\n".join(texts[-10:])  # last few turns is plenty for a resume-cue


def _write_digest(team_home: Path, worker: str, body: str) -> bool:
    workers_dir = team_home / "workers"
    try:
        workers_dir.mkdir(parents=True, exist_ok=True)
        target = workers_dir / "{}_active.md".format(worker)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        content = (
            "# {} — pre-compaction digest\n\n"
            "> Written by symphony_precompact_digest.py at {} (PreCompact hook, "
            "layer-5 resume-cue). Rolling — this OVERWRITES on every compaction, "
            "not a journal.\n\n"
            "{}\n"
        ).format(worker, stamp, body[:MAX_DIGEST_CHARS])
        target.write_text(content, encoding="utf-8")
        return True
    except Exception:
        return False


def main() -> int:
    # hook input (stdin JSON); never let a parse failure crash the wake.
    transcript_path, cwd = "", os.getcwd()
    try:
        hin = json.load(sys.stdin)
        transcript_path = hin.get("transcript_path", "") or ""
        cwd = hin.get("cwd", "") or cwd
    except Exception:
        pass

    worker = _worker()
    if not worker:
        print("[precompact-digest] SYMPHONY_WORKER unresolved — skip (exit 0)")
        return 0

    tail = _tail_transcript_text(transcript_path, TAIL_LINES_TO_SCAN)
    if not tail:
        print("[precompact-digest] no readable transcript tail — skip (exit 0, non-fatal)")
        return 0

    body = (
        "## Recent activity (best-effort tail, not exhaustive)\n\n"
        "{}\n\n"
        "## On next wake\n\n"
        "Read this digest FIRST (§0 resume-branch) before treating this as a fresh "
        "wake — resume the task above rather than re-asking the operator what you "
        "were doing.\n"
    ).format(tail)

    team_home = _team_home()
    ok = _write_digest(team_home, worker, body)
    print("[precompact-digest] {} digest {} at {}".format(
        worker, "written" if ok else "FAILED (non-fatal)", team_home / "workers"))
    return 0  # ALWAYS 0 — never block compaction


if __name__ == "__main__":
    sys.exit(main())
