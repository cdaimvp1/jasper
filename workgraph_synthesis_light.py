"""
workgraph_synthesis_light.py - the light half of task #247's hybrid
synthesis routing.

Real per-wake data (gathered live against workgraph.db before this was
built) showed the large majority of stale entities on any given wake
carry well under 100KB of genuinely NEW evidence (the delta since their
last synthesis - see workgraph_synthesis.list_stale_entities's own
"delta, not the whole history" discipline, which this module reuses
rather than re-deriving). Waking a full curator subprocess - a real
`claude -p` agentic session with Bash-tool curl round-trips, minutes of
overhead - for a couple of short emails is real, measurable overkill.
Anything AT OR ABOVE the threshold still goes through ingest/
scheduled_refresh.py's existing run_synthesis_oneshot() unchanged; that
path is deliberately untouched, since its richer judgment is worth the
cost for a genuinely large evidence delta.

Scope, a deliberate, stated tradeoff: the light path covers the same
core fields SYNTHESIS_ROUTINE.md's step 3 always requires (asks,
decisions, dates_mentioned w/ kind+whose, commitments, key_facts) plus
step 4's synthesis write (summary, derived_title, next_steps,
suggested_actions) - but deliberately OMITS repeat_signals/
resolution_signals (both require reading this issue's own prior claims
first and judging a restatement/completion match - a second, harder
judgment layered on top of the first extraction) and
estimated_completion/per-step duration estimates (requires grounding
against the sourcing-process knowledge base doc, a second read this
path doesn't do). All four are individually optional per SYNTHESIS_
ROUTINE.md itself ("omitting is a normal, correct outcome") - a
light-path entity just gets the leaner, still-real treatment its actual
size warrants. Nothing is permanently lost: routing is re-decided fresh
on every wake, so an entity that grows past the threshold next time
gets the full curator treatment then.

No queue, no separate storage shape: this writes through the exact same
ws.create_extraction / ws.upsert_synthesis primitives the real curator
routine (via server_lean.py's own routes) and workgraph_pipeline2.py's
run_project_extraction already use - never a new, parallel synthesis
representation.

Party (vendor/stakeholder) linking (task #323, 2026-08-11): run_light_
synthesis also calls workgraph_parties.run over this entity's own
members on every invocation - the same deterministic, no-LLM domain-
heuristic mechanism workgraph_classify.cluster_and_link already calls
for the live ingest path. Before this fix, a light-synthesized project/
issue could get real claims materialized with zero party ever linked
underneath (confirmed live: 25 of 39 rows, 64%, on the Workload Status
Update Report had no vendor shown), since cluster_and_link's own party
pass is scoped to the raw_items a CLASSIFY batch touches, not to
whatever this module later synthesizes - most visibly for backfill_
stale_marker_projects.py, which calls run_light_synthesis directly and
deliberately bypasses cluster_and_link/list_stale_entities entirely.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

import text_extract
import workgraph_store as ws
import workgraph_claims
import workgraph_parties
import workgraph_reconcile
import workgraph_synthesis

LIGHT_PATH_MAX_BYTES = 100_000  # task #247's own "<~100KB evidence" cutoff
_PROMPT_MAX_CHARS = 90_000  # defensive cap on what actually goes in the prompt, independent of the routing threshold above
_TIMEOUT_SECONDS = 240


def _run_headless_claude(prompt: str, *, timeout: int, model: str | None = None) -> subprocess.CompletedProcess:
    """Self-contained tree-kill subprocess primitive, deliberately NOT
    imported from workgraph_pipeline2.py or ingest/scheduled_refresh.py -
    same reasoning workgraph_pipeline2.py's own copy documents: each
    mechanism gets its own copy of this real safety technique rather than
    a shared import, so no future change to one path can silently affect
    another's timeout/tree-kill behavior.

    model (2026-08-11, backfill cost investigation): optional override,
    e.g. "haiku" - unset stays on this account's default model (opus, per
    ~/.claude/settings.json), which is what the live scheduled-refresh
    forward path keeps using. Only the one-time stale-marker backfill
    script passes an override, so this cost question stays scoped to that
    backfill and never silently changes the live path's quality bar.

    Prompt goes over STDIN, never as a command-line argument (task #304
    backfill-sizing investigation, 2026-08-11, real bug reproduced live):
    Windows' CreateProcess has a hard ~32K character total-command-line
    limit, and this module's own _PROMPT_MAX_CHARS (90,000) routinely
    exceeds it for any project with real evidence past roughly 30-90KB -
    confirmed live as WinError 206 ("The filename or extension is too
    long") the first time this function was ever exercised against a
    project with genuinely large new evidence. `claude -p` (no positional
    prompt argument) reads the prompt from stdin instead - confirmed
    directly (`echo ... | claude -p`) - which has no such length ceiling.

    encoding="utf-8" is explicit, not incidental: text=True alone lets
    Python fall back to the Windows locale codepage (cp1252 here) for the
    stdin write, which crashed with UnicodeEncodeError on the very next
    real project tried (a stray BOM/\\ufeff character from real email
    content, un-representable in cp1252) - real evidence can carry any
    Unicode character, so the encoding can't be locale-dependent.
    errors="replace" is a deliberate last-resort safety net on top of
    that, not a substitute for it - one character Python still can't
    round-trip should never crash a whole backfill pass over it."""
    env = os.environ.copy()
    args = ["claude", "-p", "--allowedTools", ""]
    if model:
        args += ["--model", model]
    proc = subprocess.Popen(
        args,
        cwd=str(Path(__file__).resolve().parent), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=15)
        except Exception:
            pass
        proc.communicate()
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def _member_ids_for_entity(entity_type: str, entity_id: str) -> list[str]:
    if entity_type == "issue":
        return [entity_id]
    if entity_type == "project":
        return (
            [c["id"] for c in ws.list_clusters_for_project(entity_id)]
            + [i["id"] for i in ws.list_issues_for_project(entity_id)]
        )
    raise ValueError(f"unknown entity_type: {entity_type}")


def _gather_new_evidence(entity_type: str, entity_id: str) -> tuple[list[dict], str]:
    """Returns (new_raw_items, full_text_blob) - the exact same set of
    genuinely-new raw_items SYNTHESIS_ROUTINE.md step 2 defines (no
    extraction row yet), across every member (a project's clusters +
    issues; a standalone issue is its own sole member), plus each
    member's attachment extracted_text (not extraction-gated the same
    way - matches workgraph_pipeline2.full_text_for_work_object's own
    convention of including every linked attachment regardless).
    full_text_blob is exactly what compute_new_evidence_bytes measures
    AND what the prompt is built from - one computation, not two
    parallel ones that could quietly drift apart."""
    extracted_ids = set(ws.list_raw_item_ids_with_extractions())
    member_ids = _member_ids_for_entity(entity_type, entity_id)

    new_items: list[dict] = []
    parts: list[str] = []
    for member_id in member_ids:
        for item in ws.get_raw_items_for_issue(member_id):
            if item["id"] in extracted_ids:
                continue
            body = text_extract.resolve_item_text(item)
            subject = item.get("subject") or ""
            parts.append(f"RAW_ITEM_ID: {item['id']}\nSubject: {subject}\n{body}")
            new_items.append(item)
    for member_id in member_ids:
        for att in ws.list_attachments_for_issue(member_id):
            text = att.get("extracted_text")
            if text:
                parts.append(f"Attachment ({att.get('filename') or 'unnamed'}):\n{text}")

    return new_items, "\n\n---\n\n".join(parts)


def compute_new_evidence_bytes(entity_type: str, entity_id: str) -> int:
    """The routing measure task #247 calls a light-vs-heavy decision on -
    len() of the exact text a light-path run would actually read, never a
    separately-estimated proxy."""
    _, full_text = _gather_new_evidence(entity_type, entity_id)
    return len(full_text)


_LIGHT_SYNTHESIS_PROMPT_TEMPLATE = """You are doing lightweight synthesis maintenance for one real business {entity_type} ("{entity_name}") in a procurement/vendor-negotiation tracker.

PRIOR SUMMARY (may be empty if this is the first synthesis):
{previous_summary}

EXISTING TRACKED NEXT STEPS (for context only, do not just repeat these):
{previous_next_steps}

NEW COMMUNICATIONS SINCE THE PRIOR SUMMARY (read all of it):
{new_evidence}

Do two things.

1. For EACH raw item above (keyed by its RAW_ITEM_ID line), extract what is genuinely present - never invent anything not actually stated:
   - asks: direct requests made in this item
   - decisions: decisions stated in this item
   - dates_mentioned: each as {{"text": "...", "kind": "hard"|"soft", "whose": "marc"|"counterparty"|"shared"|"unclear"}} - "hard" only for a real binding deadline with a nameable consequence for missing it, "soft" for an aspirational/target date; "whose" is who the date actually binds per the sentence, never who sent the message
   - commitments: commitments made in this item
   - key_facts: other material facts worth remembering
   Any list may be empty - never pad with something not actually there.

2. Write one synthesis for the WHOLE {entity_type}, informed by the prior summary plus everything new:
   - summary: 2-4 sentences - who asked what, what's happened, where it stands now
   - derived_title: a short (5-10 words), specific, real title for what this is actually about - never a restatement of a raw email subject line
   - next_steps: each {{"step": "...", "current": true}}
   - suggested_actions: each {{"task_id": null, "label": "2-5 word imperative", "rationale": "the why, with specifics (PR/PO number, supplier, dollar amount) where you have them"}}

Output EXACTLY one JSON object, nothing before or after it, in this shape:
{{"extractions": {{"<raw_item_id>": {{"asks": [...], "decisions": [...], "dates_mentioned": [...], "commitments": [...], "key_facts": [...]}}, ...}},
  "synthesis": {{"summary": "...", "derived_title": "...", "next_steps": [...], "suggested_actions": [...]}}}}
"""


def _parse_light_output(stdout: str) -> Optional[dict]:
    """Lenient JSON extraction - a real `claude -p` reply can carry stray
    prose around the JSON object despite the prompt's instruction not to;
    slicing to the outermost braces before parsing is the same
    defensiveness this codebase already applies to every other headless-
    subprocess parser (_parse_verdict, _parse_extraction_output) rather
    than a strict json.loads that fails on the first stray character."""
    text = stdout or ""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict) or "synthesis" not in parsed:
        return None
    return parsed


def run_light_synthesis(entity_type: str, entity_id: str, *, model: str | None = None) -> dict:
    """The light path itself - one real LLM call, no subprocess-per-item,
    no agentic session. Writes through the same primitives the heavy
    curator path uses (create_extraction + its full live-wiring side
    effects, upsert_synthesis), so a light-path write is indistinguishable
    downstream from a curator-written one except in richness (see module
    docstring for the deliberately-omitted fields).

    model: optional cheap-model override, see _run_headless_claude."""
    if entity_type == "issue":
        entity = ws.get_issue(entity_id)
    elif entity_type == "project":
        entity = ws.get_project(entity_id)
    else:
        return {"entity_type": entity_type, "entity_id": entity_id, "action": "unknown_entity_type"}
    if entity is None:
        return {"entity_type": entity_type, "entity_id": entity_id, "action": "not_found"}

    # Task #323 fix (2026-08-11, real bug confirmed by Marc's direct report
    # review of the Workload Status Update Report - 25 of 39 rows, 64%, had
    # no vendor/party shown): party (vendor/stakeholder) linking used to
    # happen ONLY via workgraph_classify.cluster_and_link's own call to
    # workgraph_parties.run(touched_issues) during live ingest - this light-
    # synthesis path never called it, so any project/issue synthesized here
    # (including via backfill_stale_marker_projects.py, which calls this
    # function directly and deliberately bypasses cluster_and_link/list_
    # stale_entities entirely) could get real claims materialized with zero
    # party ever linked underneath. workgraph_parties.run is the exact same
    # deterministic, no-LLM domain-heuristic mechanism the curator/classify
    # path already uses for this - reused here verbatim rather than
    # re-derived, and it is exactly the "cheap, already-available signal"
    # this path's own design principle calls for (see module docstring).
    # It is also safe/idempotent to call on every light-synthesis run
    # regardless of whether THIS run finds new evidence (upsert_party/
    # link_party_to_issue are both no-ops on a repeat) - a party's presence
    # is a fact about who's actually on the thread, not something scoped to
    # this run's own newly-extracted claims.
    member_ids = _member_ids_for_entity(entity_type, entity_id)
    party_result = workgraph_parties.run(member_ids)

    new_items, full_text = _gather_new_evidence(entity_type, entity_id)
    if not new_items:
        # Shouldn't happen given list_stale_entities' own staleness gate
        # (a revision bump implies at least one new material claim), but
        # never crash a scheduled run over a race against that gate.
        return {"entity_type": entity_type, "entity_id": entity_id, "action": "no_new_evidence",
                "parties": party_result}

    existing = ws.get_synthesis(entity_type, entity_id)
    previous_summary = (existing or {}).get("summary") or "(none yet - first synthesis)"
    previous_next_steps = json.dumps((existing or {}).get("next_steps") or [])

    prompt = _LIGHT_SYNTHESIS_PROMPT_TEMPLATE.format(
        entity_type=entity_type, entity_name=entity.get("title") or entity.get("name") or entity_id,
        previous_summary=previous_summary, previous_next_steps=previous_next_steps,
        new_evidence=full_text[:_PROMPT_MAX_CHARS],
    )
    try:
        proc = _run_headless_claude(prompt, timeout=_TIMEOUT_SECONDS, model=model)
    except subprocess.TimeoutExpired:
        return {"entity_type": entity_type, "entity_id": entity_id, "action": "timeout",
                "parties": party_result}

    parsed = _parse_light_output(proc.stdout)
    if parsed is None:
        return {"entity_type": entity_type, "entity_id": entity_id, "action": "unparseable",
                "parties": party_result}

    extractions = parsed.get("extractions") or {}
    extracted_count = 0
    for item in new_items:
        entry = extractions.get(str(item["id"]))
        if not isinstance(entry, dict):
            continue
        extracted_json = json.dumps({
            "asks": entry.get("asks") or [], "decisions": entry.get("decisions") or [],
            "dates_mentioned": entry.get("dates_mentioned") or [],
            "commitments": entry.get("commitments") or [], "key_facts": entry.get("key_facts") or [],
        })
        ws.create_extraction(item["id"], extracted_json)
        # Same live-wiring side effects api_raw_item_extraction_write applies
        # (server_lean.py) - claims materialization and FTS indexing must
        # stay current for a light-path write exactly as for a curator one.
        workgraph_claims.materialize_claims_for_raw_item(item["id"])
        workgraph_reconcile.generate_resolution_signal_suggestions(item["id"])
        body_text = text_extract.resolve_item_text(item)
        if body_text and body_text.strip():
            ws.index_evidence_fts(item["id"], item.get("issue_id"), body_text)
        extracted_count += 1

    synthesis = parsed.get("synthesis") or {}
    marker = workgraph_synthesis.compute_evidence_marker(entity_type, entity_id)
    ws.upsert_synthesis(
        entity_type=entity_type, entity_id=entity_id,
        summary=synthesis.get("summary"), derived_title=synthesis.get("derived_title"),
        next_steps_json=json.dumps(synthesis.get("next_steps") or []),
        suggested_actions_json=json.dumps(synthesis.get("suggested_actions") or []),
        synthesized_from_marker=marker,
    )
    return {"entity_type": entity_type, "entity_id": entity_id, "action": "synthesized_light",
            "new_raw_items": len(new_items), "extracted": extracted_count, "parties": party_result}
