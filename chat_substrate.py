"""Chat substrate — shared primitives for append-only message-log surfaces.

Used by: projects.py (refactored 2026-04-29). Future candidates: team_room.py,
inbox.py (deferred per risk-aversion; meetings.py per @george directive at
project bootstrap `pp_f47e620ff0` — *Let's leave meetings out of this.*)

The shape:
- Each surface stores messages in an append-only file with `---`-separated blocks
- Each block has a header (from / ts / message_id) followed by the body
- `parse_blocks` splits a file into structured records
- `append_block` writes a new block + emits a telemetry event
- `format_block` is the canonical block-string format

This module owns the LOAD-BEARING shape. Per-surface modules (projects.py etc.)
keep their own surface-specific logic (membership, lifecycle, routing) on top.
"""
from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from bus import emit_event

_META_RE = re.compile(
    r"^from:\s*(?P<from>[^\n]+?)\s*\nts:\s*(?P<ts>\S+)\s*\nmessage_id:\s*(?P<mid>\S+)(?:\s*\ngeorge_view:\s*(?P<gv>[^\n]+))?",
    re.DOTALL,
)

_TIME_ESTIMATE_RE = re.compile(r"~\d{1,2}:\d{2}(?:\s*(?:ET|AM|PM|am|pm))?")


_SMART_BREVITY_PROMPT = """You are writing a 2-line GLANCE-VIEW SUMMARY of a worker's message for the manager's dashboard. The full message is preserved in the expanded view; this summary is what the manager reads when deciding whether to expand. Return ONLY the summary, no preamble, no quotation marks.

Hard rules:
1. **Maximum 30 words.** Aim for 15-25. Two short sentences max, ideally one.
2. **Tease + Lede shape:** lead with the SINGLE most important fact or decision in the message. Cut secondary points entirely — they live in the expanded view.
3. **Selective bold:** bold 1-3 load-bearing keywords (a noun, a number, an action verb). Never bold a full sentence. Never bold for decoration.
4. **Cut all filler:** throat-clearing openers ("Just wanted to flag"), softeners ("hopefully", "perhaps"), adverbs, adjectives, passive voice, restating-the-obvious.
5. **Preserve critical refs:** every @-mention (@george, @tb, etc.), every message-ID (tr_xxx, m_xxx, pp_xxx, mm_xxx, desk_xxx), every tag bracket ([decision], [empirical], [take-the-L]). These are navigation anchors.
6. **No code blocks, no bullets, no headers** — this is a 2-line glance summary, not a structured doc.
7. **Stay truthful** — never fabricate facts or sharpen claims beyond what the original supports.
8. If the message is already 30 words or fewer AND already Smart-Brevity-shaped, return it unchanged.

Examples of GOOD summaries:
- "**Migration ETA:** 30-45 min. Rollback path intact at @cb's SHARP backup."
- "Take-the-L: **SSE consumer died** at server restart. Re-armed as `b2t2yn5ct`."
- "**v0.4 LIVE.** Submit instant; rewrite async; original preserved. Empirical at tr_403e68a63a."

Worker's original message:
---
{body}
---

2-line summary:"""


# Reference-extraction regex: matches @-mentions and substrate message-IDs that MUST survive rewrite.
# Per @george tr_bf6ce544d4 + tr_f492f4bb16 — verify-step ensures intent preservation on critical refs.
_PRESERVE_REF_RE = re.compile(
    r"(?:@[a-z][a-z0-9_-]{1,40})"            # @mentions (case-insensitive applied at compile)
    r"|(?:\b(?:tr|pp|mm?|desk)_[a-z0-9_-]{2,60}\b)",  # message-IDs
    re.IGNORECASE,
)
# Verbatim opt-out tag: body starting with [verbatim] (case-insensitive) skips rewrite entirely.
_VERBATIM_OPTOUT_RE = re.compile(r"^\s*\[verbatim\]\s*", re.IGNORECASE)


def _extract_refs(text: str) -> set[str]:
    """Lower-case set of all @mentions + message-IDs found in text."""
    return {m.group(0).lower() for m in _PRESERVE_REF_RE.finditer(text)}


def _run_rewrite_subprocess(prompt: str, timeout_s: int) -> tuple[str, str]:
    """Send prompt to the persistent CC writer (per @george tr_ce57788504 2026-05-03).

    Falls back to the one-shot subprocess path if the persistent writer fails.
    Persistent writer uses stream-JSON mode — one CC process serves all rewrites with
    warm session-cache (~2-3s per call after first warmup vs ~5s for fresh subprocess).
    """
    try:
        from persistent_writer import rewrite as _persistent_rewrite
        result, status = _persistent_rewrite(prompt, timeout_s=timeout_s)
        if status == "ok":
            return result, ""
        # Fall through to one-shot subprocess on persistent-writer error
    except Exception:
        pass

    # Fallback: one-shot subprocess (slower, but always works)
    import shutil
    import subprocess
    claude_bin = shutil.which("claude") or "/Users/DA37243/.local/bin/claude"
    try:
        proc = subprocess.run(
            [claude_bin, "-p", prompt, "--model", "haiku", "--no-session-persistence"],
            capture_output=True, text=True, timeout=timeout_s,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return "", "error:timeout"
    except Exception as e:
        return "", f"error:{type(e).__name__}"
    if proc.returncode != 0:
        return "", f"error:rc{proc.returncode}"
    output = (proc.stdout or "").strip()
    if output.startswith("```") and output.endswith("```"):
        lines = output.split("\n")
        output = "\n".join(lines[1:-1]).strip()
    return output, ""


def smart_brevity_rewrite(body: str, *, timeout_s: int = 25) -> tuple[str, str]:
    """LLM-based Smart Brevity rewrite (per @george tr_fa89a7dfe9 anti-deterministic-translator scar).

    v0.3 additions per @george tr_f492f4bb16:
      - [verbatim] opt-out tag: body starting with `[verbatim]` (case-insensitive) skips rewrite,
        tag is stripped before persisting.
      - Reference-preservation verify step: extracts @-mentions + message-IDs from input, checks
        each appears in rewritten output. If any missing, retries rewrite once with stronger
        preserve-instruction; if still missing, falls back to original body (safe default).

    Returns (final_body, status). Status is one of:
      "verbatim", "rewritten", "rewritten:retry", "fallback:refs_dropped",
      "unchanged", "skipped:short", "skipped:special", "error:<reason>"
    """
    if not body:
        return body, "skipped:short"

    # [verbatim] opt-out: strip the tag and return body unchanged.
    if _VERBATIM_OPTOUT_RE.match(body):
        stripped = _VERBATIM_OPTOUT_RE.sub("", body, count=1).lstrip()
        return stripped, "verbatim"

    if len(body) < 30:
        return body, "skipped:short"
    # Code fences: skip — risk of LLM mangling code is real.
    if "```" in body:
        return body, "skipped:special"

    input_refs = _extract_refs(body)
    # System prompt holds the Smart Brevity rules (cached by Haiku across calls).
    # Per-call user message is just the body + a MUST-PRESERVE ref list.
    if input_refs:
        ref_list = ", ".join(sorted(input_refs))
        prompt = f"MUST-PRESERVE: {ref_list}\n\n{body}"
    else:
        prompt = body
    rewritten, err = _run_rewrite_subprocess(prompt, timeout_s)
    if err:
        return body, err
    if not rewritten:
        return body, "error:empty"
    if rewritten == body.strip():
        return body, "unchanged"

    # Verify: every input ref must appear in rewritten output.
    output_refs = _extract_refs(rewritten)
    missing = input_refs - output_refs
    if not missing:
        return rewritten, "rewritten"

    # Retry once with explicit preserve-instruction for the missing refs.
    retry_prompt = (
        prompt
        + f"\n\nIMPORTANT: your previous rewrite attempt dropped these references that MUST be preserved verbatim: "
        + ", ".join(sorted(missing))
        + "\nRewrite again, keeping ALL @-mentions and message-IDs from the original."
    )
    retry_rewritten, err2 = _run_rewrite_subprocess(retry_prompt, timeout_s)
    if err2 or not retry_rewritten:
        # Fallback to original body — better safe than dropping refs.
        return body, "fallback:refs_dropped"
    retry_refs = _extract_refs(retry_rewritten)
    retry_missing = input_refs - retry_refs
    if retry_missing:
        # Still dropping refs after retry — fall back to original body.
        return body, "fallback:refs_dropped"
    return retry_rewritten, "rewritten:retry"

# Smart Brevity v0.1 substrate-enforcement (per @george tr_39d2e0299f path-of-least-resistance
# + @oc tr_815c9f4d25 research). Strip pure-throat-clearing prefixes only; never consume content.
# Each pattern matches the throat-clearing prefix UP TO AND INCLUDING the link-word/punctuation
# that bridges to substantive content — leaves the actual message intact.
_BANLIST_PREFIX_RE = re.compile(
    r"^\s*(?:"
    # "Just wanted to flag," / "Just wanted to flag that" / "Just want to share —"
    r"Just\s+(?:wanted\s+to\s+|want\s+to\s+|going\s+to\s+)(?:flag|share|note|update|let\s+you\s+know|reach\s+out|say|mention|highlight)(?:\s+that)?\s*[,:—\-]?\s*"
    # "As a reminder," / "As a reminder that"
    r"|As\s+a\s+(?:quick\s+|brief\s+)?reminder(?:\s+that)?\s*[,:—\-]\s*"
    # "Please be advised that" / "Please be advised:"
    r"|Please\s+be\s+advised(?:\s+that)?\s*[,:—\-]?\s*"
    # "FYI:" / "FYI —" / "FYI, just wanted to share that"
    r"|FYI\s*[,:—\-]+\s*(?:just\s+(?:wanted\s+to\s+|letting\s+you\s+know\s+)(?:flag|share|note|let\s+you\s+know|that)\s+)?"
    # "Hey team, just wanted to flag," / "Hey all, just want to share that"
    r"|Hey\s+(?:team|all)\s*[,—\-]\s*just\s+(?:wanted\s+to\s+|want\s+to\s+)(?:flag|share|note|let\s+you\s+know|say|mention|update)(?:\s+that)?\s*[,:—\-]?\s*"
    r")",
    re.IGNORECASE,
)
# Detect a fully-bolded leading sentence (40-300 chars between ** **) at start of body or after blank line.
# Threshold 40 leaves short impactful headlines (<40 chars) untouched per Smart Brevity *Tease* convention.
_FULL_BOLD_LEAD_RE = re.compile(r"(?:^|\n)\*\*([^*\n]{40,300}?)\*\*(?=\s|$)", re.MULTILINE)


def apply_smart_brevity(body: str) -> tuple[str, list[str]]:
    """v0.1 deterministic auto-fix DEPRECATED per @george tr_fa89a7dfe9 — translator scar applies.

    The deterministic ban-list + un-bold rules were disabled because deterministic text-transform
    cannot capture context (same scar as the deterministic-translator passthrough revert at
    `tr_56f60b00b9`). LLM-based rewrite path replaces this — see `smart_brevity_rewrite` below
    + `team_room.post_message` integration.

    This function now returns the body unchanged (no-op) to preserve the existing call sites
    in team_room.py and projects.py without raising. Will be removed in a follow-up sweep.
    """
    return body, []


def audit_time_claims(body: str, server_ts: str) -> str:
    """
    Liveness Floor v0.2 substrate-enforcement candidate · muscle-memory-via-mechanism:
    when post body contains `~HH:MM` estimation-marked time-claims, append the empirical
    server-ts footer. Citations (e.g., `per tr_xxx 23:38 ET`) don't match the `~` marker,
    so they pass through unmodified. Self-claimed-current-time estimates always get the
    ground-truth alongside, preventing narrative-vs-empirical-time drift at write-time.

    Per @george muscle-memory framing (`tr_ca8fe22171`): doctrine-as-substrate, not
    doctrine-as-text. No worker discipline required — substrate makes the right thing
    visible by default. Lifted from team_room.py (2026-05-02 ~10:08 ET) so both TR + project
    surfaces inherit the same discipline.
    """
    if _TIME_ESTIMATE_RE.search(body):
        return body.rstrip() + f"\n\n_[server-ts: {server_ts}]_"
    return body


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_message_id(prefix: str) -> str:
    """Generate a message_id with the given prefix (e.g., 'tr', 'pp', 'mm', 'm')."""
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def format_block(*, sender: str, ts: str, message_id: str, body: str,
                 george_view: Optional[str] = None) -> str:
    """Canonical message block format. Begins with newline so it appends cleanly.

    Optional `george_view` adds a header line — collapsed-view summary George reads
    without expanding (Phase 1 of "delight George" substrate, TB tr_38c55b8899 2026-05-03).
    """
    meta_lines = [f"from: {sender}", f"ts: {ts}", f"message_id: {message_id}"]
    if george_view and george_view.strip():
        gv = " ".join(george_view.strip().split())
        meta_lines.append(f"george_view: {gv}")
    return "\n---\n" + "\n".join(meta_lines) + "\n---\n" + body.strip() + "\n"


def visible_len(s: str) -> int:
    """Count visible characters in a markdown-formatted string — excludes the format
    markers themselves so length budgets reflect what George actually sees."""
    stripped = s
    stripped = re.sub(r"\*\*([^\*]+)\*\*", r"\1", stripped)  # bold
    stripped = re.sub(r"\*([^\*]+)\*", r"\1", stripped)      # italic
    stripped = re.sub(r"`([^`]+)`", r"\1", stripped)         # inline code
    stripped = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", stripped)  # markdown links
    return len(stripped)


def auto_george_view(body: str, *, max_chars: int = 220) -> Optional[str]:
    """Heuristic-extractive george_view auto-generation. Pure deterministic — no LLM.

    Phase 2 of "delight George" substrate (TB tr_13e95bdf15 + tr_d9afefc525 2026-05-03):
    take the first non-empty paragraph, preserve `**bold**` `*italic*` `` `code` `` markdown,
    truncate at word boundary on visible-char budget, auto-close any open format span before
    the ellipsis. Markdown link `[text](url)` syntax preserved verbatim — the renderer makes
    it clickable. Workers always win — explicit `george_view` overrides this auto-derivation.
    """
    if not body or not body.strip():
        return None
    text = body.strip()
    # Item 1 of @george tr_71ca04c5e8 cohort-comms ship · 2026-05-08:
    # Surface ACTION NEEDED line + @-mention(s) into george_view so summary triage
    # always shows who's on the hook. Pattern: any line containing both "ACTION NEEDED"
    # (case-insensitive) and at least one @-mention takes priority over first paragraph.
    action_re = re.compile(r"(?i)action[\s_-]*needed\s*[:\-—]?\s*(.{1,300})")
    for line in text.splitlines():
        line_s = line.strip().lstrip("*_-• ").rstrip("*_- ")
        if not line_s:
            continue
        m = action_re.search(line_s)
        if m and re.search(r"@\w+", line_s):
            # Strip the leading "**ACTION NEEDED:**" markdown-wrapped tag · 🔴 prefix replaces it
            ask = re.sub(r"(?i)^[*_\s]*action[\s_-]*needed\s*[:\-—]?\*?\*?\s*", "", line_s).strip().lstrip("*_ ").rstrip("*_ ")
            return ("🔴 ACTION: " + ask)[:max_chars + 12].rstrip(" ,.;:") + ("…" if len(ask) > max_chars else "")
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paras:
        return None
    first_para = paras[0]
    formatted = re.sub(r"\s+", " ", first_para).strip()
    if not formatted:
        return None
    if visible_len(formatted) <= max_chars:
        return formatted
    out = []
    visible_count = 0
    i = 0
    in_bold = False
    in_italic = False
    in_code = False
    while i < len(formatted) and visible_count < max_chars:
        c = formatted[i]
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
    if in_bold:
        truncated += "**"
    if in_italic:
        truncated += "*"
    if in_code:
        truncated += "`"
    last_space = truncated.rfind(" ")
    if last_space > 0 and last_space > len(truncated) * 0.7:
        truncated = truncated[:last_space]
    return truncated.rstrip(",;:.!?-— ") + "…"


def attach_george_view(messages: list[dict[str, Any]]) -> None:
    """Mutate-in-place: for every message dict missing `george_view`, attach one via the
    heuristic. Marks auto-generated entries with `george_view_auto: True` flag.
    Idempotent — already-populated views are left alone."""
    for m in messages:
        if not m.get("george_view") and m.get("body"):
            auto = auto_george_view(m["body"])
            if auto:
                m["george_view"] = auto
                m["george_view_auto"] = True


def parse_blocks(text: str, *, header_segments: int = 1) -> list[dict[str, Any]]:
    """Parse a file into message records.

    `header_segments` is how many top-of-file segments to skip before message blocks
    begin (1 = standard surfaces with single header; 0 = no header). Returns list of
    {from, ts, message_id, body} dicts.

    `---` lines within message body are preserved (per OC scar 2026-05-01 substrate-
    self-reference family · `tr_a7871b0e6c`): if a segment after a META block doesn't
    itself parse as META, it's treated as continuation of the body, joined back with
    `\\n---\\n`. So users can write markdown horizontal rules in body without breaking
    the parser. Boundary discipline: `\\n---\\n` followed by a `from:` line is the only
    real block boundary.
    """
    out: list[dict[str, Any]] = []
    segments = re.split(r"\n---\n", text)
    i = header_segments
    while i < len(segments):
        seg = segments[i].strip()
        if not seg:
            i += 1
            continue
        m = _META_RE.match(seg)
        if m and i + 1 < len(segments):
            # Body starts at segment i+1; walk forward absorbing segments that aren't
            # themselves META-headers (those are body content with stray `---` lines).
            body_parts = [segments[i + 1]]
            j = i + 2
            while j < len(segments):
                if _META_RE.match(segments[j].strip()):
                    break  # next real block
                body_parts.append(segments[j])
                j += 1
            body = "\n---\n".join(body_parts).strip()
            gv_raw = m.group("gv") if "gv" in (m.groupdict() or {}) else None
            out.append({
                "from": m.group("from"),
                "ts": m.group("ts"),
                "message_id": m.group("mid"),
                "george_view": (gv_raw.strip() if gv_raw else None),
                "body": body,
            })
            i = j
            continue
        i += 1
    return out


def append_block(
    *,
    file: Path,
    sender: str,
    body: str,
    message_id_prefix: str,
    lock: threading.Lock,
    event_source: str,
    event_kind: str,
    event_payload_extra: Optional[dict[str, Any]] = None,
    george_view: Optional[str] = None,
) -> dict[str, Any]:
    """Append a message block to `file` + emit telemetry event.

    Optional `george_view` adds the collapsed-view summary header (Phase 1 of "delight
    George" substrate, TB tr_38c55b8899 + tr_7008a84b3f 2026-05-03).

    Returns {ok, message_id, ts}.
    """
    if not (body or "").strip():
        raise ValueError("body required")
    if not file.exists():
        raise ValueError(f"file does not exist: {file}")
    msg_id = new_message_id(message_id_prefix)
    ts = now_iso()
    body_audited = audit_time_claims(body.strip(), ts)
    block = format_block(sender=sender, ts=ts, message_id=msg_id, body=body_audited,
                         george_view=george_view)
    with lock:
        with file.open("a", encoding="utf-8") as fh:
            fh.write(block)
    # Phase A team-tab redesign (TB pp_59b871f489 2026-05-03): attach george_view to event
    # payload so /team home renders summary, not raw body. Author > auto-heuristic.
    effective_gv = (george_view or "").strip() or (auto_george_view(body) or "")
    # Read-in-full per @george pp_61aa5d0e3a (2026-05-23): 200-char cap on project-post
    # body_preview was hiding substantive content from workers + George's own re-read.
    # Match team_room.py:217 (16000) so events carry the full author intent.
    # 2026-05-31 (George pp_6a57a66b7e): raised 2000→16000 — most cohort posts exceed 2K and the
    # event-read path (monitors/inbox) is how workers READ posts, so the 2K cap was truncating real
    # content for readers (the full body was always on disk in the .md file, but readers go through
    # the event). 16K covers all real posts; monitors truncate on their own end so no flood.
    payload = {"message_id": msg_id, "sender": sender, "body_preview": body[:16000],
               "george_view": effective_gv or None}
    if event_payload_extra:
        payload.update(event_payload_extra)
    emit_event(source=event_source, kind=event_kind, actor=sender, target=event_source, payload=payload)
    return {"ok": True, "message_id": msg_id, "ts": ts}
