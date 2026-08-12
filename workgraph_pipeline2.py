"""
workgraph_pipeline2.py - the NEW grouping-and-extraction pipeline
(2026-08-05, Marc's own exhaustive spec, replacing the retired
suggestion-queue/curator-reviewed model entirely). Originally deliberately
self-contained: did not import ingest/scheduled_refresh.py, workgraph_
lessons.py, or use the pending_project_suggestions/identity_constraints
tables for any decision. Marc's own words at the time: "CURATOR OR ANY
OTHER PREVIOUSLY BUILT MECHANISM SHOULD NOT TOUCH THIS. BUILD NEW
MECHANISMS FOR IT. KEEP IT ENTIRELY SEPARATE."

workgraph_lessons.py (Total Recall) revisited 2026-08-07 at Marc's own
explicit later request, once #269's cleanup retired confirm_suggestion/
reject_suggestion - the only writers record_confirmed_or_rejected ever
had. See process_new_item's own docstring for exactly how the precedent
fast-path is wired in (read-only skip of the LLM call on a strong prior,
never itself writing a lesson) and how this pipeline's own genuine LLM
verdicts now keep the lesson store current instead. This is the one,
deliberate exception to "keep it entirely separate" - added by Marc's own
direct request, not a drift back toward the old orchestration.

Marc's exact spec, steps 3-6 (steps 1-2 already run live and were
confirmed working this same day, not rebuilt here: outlook_com_ingest.py
already captures full body text at ingestion for fresh mail - live-
tested directly against real Outlook COM this session - and workgraph_
classify.cluster_and_link already does deterministic exact-match linking
via thread_key, which already covers Outlook conversationId, Teams
chat_id, and a calendar seriesMasterId):

  3. For an item that cleared step 2 with no exact match, compare it
     against every existing project AND every not-yet-grouped item for
     2+ matched data points. Reuses workgraph_projects'
     compute_work_object_signature/_matched_data_points as bare, pure
     extraction utilities ONLY - never its group_issue()/
     precedent_prefilter/suggestion-queue orchestration, which stays
     retired for this path.
  4. Each candidate gets an immediate, real LLM read of BOTH sides' full
     text, right then - no queue, no scheduled wake. A "yes" merges
     immediately (reuses workgraph_projects.merge_issues - pure
     transactional mechanics, not a decision-making mechanism). A "no"
     means it stands as (or becomes) its own project - no permanent veto
     is ever recorded either way; a "no" is a judgment about today's
     evidence, not a promise about all future evidence.
  5. System-generated senders: already handled by step 3's own point-
     counting - is_automated_sender already excludes the sender address
     from every signal, so 2+ OTHER real points are required before a
     candidate can exist at all. No separate mechanism needed.
  6. Once grouped, an LLM reviews the project's full-text corpus and
     adds/updates checklist items plus the project's own summary/status -
     fires immediately after every successful group, not on a schedule.
     See run_project_extraction below.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

import json

import text_extract
import workgraph_store as ws
import workgraph_projects as wp
import workgraph_synthesis
import workgraph_discovery
import workgraph_lessons

_JUDGE_TIMEOUT_SECONDS = 300
_EXTRACTION_TIMEOUT_SECONDS = 600
_MAX_TEXT_CHARS = 12000

# Task #368: CREATE_NEW_PROCESS_GROUP only exists on Windows - a bare
# `subprocess.CREATE_NEW_PROCESS_GROUP` attribute access raises AttributeError
# on any other platform, before Popen is even called (this app is Windows-only
# in practice, but a test run off-Windows would crash here regardless of
# mocking). getattr(..., 0) is a real no-op value there, not just a test
# workaround - subprocess.Popen accepts creationflags=0 on every platform.
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def _run_headless_claude(prompt: str, *, timeout: int, model: Optional[str] = None) -> subprocess.CompletedProcess:
    """New, self-contained headless-claude subprocess primitive for this
    pipeline only - deliberately NOT imported from ingest/scheduled_
    refresh.py's own _run_headless_with_tree_kill (Marc's own words:
    "BUILD NEW MECHANISMS FOR IT. KEEP IT ENTIRELY SEPARATE") - same real
    safety technique independently implemented, since it's a genuine
    correctness requirement (a `claude -p` subprocess can spawn its own
    Bash-tool grandchildren that survive a naive subprocess.run timeout
    as orphans, racing the next call against the same workgraph.db), not
    "reusing curator's mechanism" in the sense Marc was objecting to.

    model (2026-08-06, company-backfill addition): optional cheap-model
    override (e.g. "haiku") for calls that don't need this pipeline's
    default model - judge_candidate/run_project_extraction stay on the
    default (unset) since they're real judgment calls this pipeline
    already trusts at that quality bar; _llm_backfill_company is
    deliberately cheap/fast, a narrow single-field lookup, not a judgment
    call, so it doesn't need to pay for the default model.

    Prompt goes over STDIN, never as a command-line argument (task #304
    backfill-sizing investigation, 2026-08-11): Windows' CreateProcess has
    a hard ~32K character total-command-line limit - workgraph_synthesis_
    light.py's own copy of this same pattern hit it live (WinError 206)
    the first time it ran against a project with real, large evidence.
    This module's own _MAX_TEXT_CHARS (12,000, doubled for judge_
    candidate's two-issue comparison) stays under that ceiling today, but
    fixing this the same way here too closes the same real fragility
    rather than leaving one copy patched and the other still latent.

    encoding="utf-8" is explicit for the same reason as the stdin fix
    above: text=True alone falls back to the Windows locale codepage
    (cp1252), which can't represent every character real evidence text
    can carry (confirmed live via the same sibling bug in workgraph_
    synthesis_light.py - a stray BOM character). errors="replace" is a
    deliberate last-resort safety net on top of that, never a substitute
    for it."""
    env = os.environ.copy()
    args = ["claude", "-p", "--allowedTools", ""]
    if model:
        args += ["--model", model]
    proc = subprocess.Popen(
        args,
        cwd=str(Path(__file__).resolve().parent), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        creationflags=_CREATE_NEW_PROCESS_GROUP,
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


def _raw_item_parts(work_object_id: str) -> list[str]:
    """Every linked raw_item's full resolved text (never a preview) -
    subject + real body - in occurred_ts ASC order (oldest first), same
    order ws.get_raw_items_for_issue itself returns them in."""
    parts = []
    for item in ws.get_raw_items_for_issue(work_object_id):
        subject = item.get("subject") or ""
        body = text_extract.resolve_item_text(item)
        parts.append(f"Subject: {subject}\n{body}")
    return parts


def _attachment_parts(work_object_id: str) -> list[str]:
    """Every linked attachment's own extracted_text (2026-08-06, Kinaxis
    investigation) - real live example that motivated this: a signed
    Change Request PDF/DOCX carried the only copy of a real reference
    number nowhere present in any email body on the same issue."""
    parts = []
    for att in ws.list_attachments_for_issue(work_object_id):
        text = att.get("extracted_text")
        if text:
            parts.append(f"Attachment ({att.get('filename') or 'unnamed'}):\n{text}")
    return parts


def full_text_for_work_object(work_object_id: str) -> str:
    """Every linked raw_item's full resolved text (never a preview),
    oldest-first, followed by every linked attachment's extracted text.
    Same real, already-extracted text source reference_base_ids_for_issue
    (workgraph_projects.py) now also scans - kept as a separate read here
    rather than sharing code, since that function returns a normalized id
    set and this one needs raw prose.

    NOT what judge_candidate reads for its actual judgment prompt as of
    review point #6/#9 - see build_identity_packet below. Kept as the
    plain, untruncated "give me everything" primitive: _evidence_hash_
    for_pair uses this (not the packet) precisely because the cache
    fingerprint should reflect every real change to a work object's
    evidence, not just the slice that happened to survive a budget."""
    return "\n\n---\n\n".join(_raw_item_parts(work_object_id) + _attachment_parts(work_object_id))


_IDENTITY_PACKET_ATTACHMENT_BUDGET = 2000
_SECTION_SEPARATOR = "\n\n---\n\n"


def build_identity_packet(work_object_id: str, char_budget: int = _MAX_TEXT_CHARS) -> str:
    """The actual evidence judge_candidate reads for its judgment prompt -
    a purpose-built, budget-aware assembly, NOT a plain oldest-first
    concat-then-slice (review point #6/#9, confirmed live via this
    session's own code read: get_raw_items_for_issue is occurred_ts ASC,
    and judge_candidate truncated via text[:_MAX_TEXT_CHARS]). On a long-
    running work object whose combined text exceeds the budget, that
    ordering silently dropped the NEWEST evidence and any attachment
    entirely - exactly backwards for a system whose value depends on
    staying current.

    Composed instead as, in this priority order:
      1. attachments - own fixed sub-budget (_IDENTITY_PACKET_ATTACHMENT_
         BUDGET), always included regardless of message volume. Real
         identity-critical content per full_text_for_work_object's own
         Kinaxis-investigation history has shown up ONLY in an attachment.
      2. the EARLIEST raw item, in full - origin/identity context (who/
         what this started as).
      3. as many of the MOST RECENT raw items as still fit the remaining
         budget, filled backward from newest toward oldest. If everything
         can't fit, it's the MIDDLE (oldest-after-the-first) that gets
         dropped - the newest activity is never the part silently cut.

    Falls back to full_text_for_work_object's exact same oldest-first
    join whenever the combined content is already under budget - byte-
    identical output in the common (short) case; this only changes
    behavior once real truncation would otherwise occur."""
    item_parts = _raw_item_parts(work_object_id)
    attachment_parts = _attachment_parts(work_object_id)
    attachment_block = _SECTION_SEPARATOR.join(attachment_parts)[:_IDENTITY_PACKET_ATTACHMENT_BUDGET]

    full_joined = _SECTION_SEPARATOR.join(item_parts + ([attachment_block] if attachment_block else []))
    if len(full_joined) <= char_budget:
        return full_joined

    earliest_part = item_parts[0] if item_parts else None
    remaining_budget = char_budget - len(attachment_block) - len(earliest_part or "")

    kept_recent_newest_first = []
    used = 0
    for part in reversed(item_parts[1:]):
        cost = len(part) + len(_SECTION_SEPARATOR)
        if used + cost > remaining_budget:
            break
        kept_recent_newest_first.append(part)
        used += cost
    recent_parts_chronological = list(reversed(kept_recent_newest_first))

    ordered_sections = [p for p in (earliest_part,) if p] + \
        ([attachment_block] if attachment_block else []) + recent_parts_chronological
    return _SECTION_SEPARATOR.join(ordered_sections)


def find_candidates(work_object_id: str, issue: Optional[dict] = None) -> list[dict]:
    """Every existing project/ungrouped item sharing 2+ real data points
    with work_object_id - pure detection, no side effects, no suggestion
    row, no precedent check. Reuses workgraph_projects' own pure
    signature/matching functions as bare utilities - not its retired
    group_issue() orchestration. Deliberately does NOT call
    workgraph_projects._shared_reference_id's separate auto-merge
    shortcut - a shared PR/PO reference is just one more point in the
    same 2+-point count here (via _matched_data_points' own "reference"
    point type), never a silent bypass of the LLM read in step 4 - Marc's
    own spec names only thread/message/meeting-series identity as
    exempt from LLM review, not a shared business reference number.

    Rewired 2026-08-11 (task #331, Marc's own engineering-direction doc,
    Section 16): used to unconditionally scan EVERY issue/cluster in the
    database (ws.list_issues(limit=10000) + ws.list_clusters(limit=10000))
    and run the real matching computation against each one - a genuine,
    already-live O(20k) cost on every new ungrouped item, every cycle, at
    ~1500+ projects. `others` is now sourced from workgraph_projects.
    candidate_pool_via_data_point_index - a real datapoint_value ->
    work_object_ids lookup (workgraph_store.data_point_values, one new
    index) instead of a full scan - EXCEPT the one case that index can't
    yet safely serve (see workgraph_discovery.
    has_confirmed_non_fasttrack_definitions' own docstring: a genuinely
    discovered, non-fast-track data point actually confirmed), where this
    still falls back to the exact original full scan rather than risk
    ever silently dropping a real candidate. The inner loop below - the
    real matching decision - is completely unchanged either way; only
    where `others` comes from differs, so the two paths are guaranteed to
    produce IDENTICAL candidate results (see
    tests/test_workgraph_pipeline2.py's own index-vs-full-scan
    equivalence tests)."""
    if issue is None:
        issue = ws.get_issue_or_cluster(work_object_id)
    my_sig = wp.get_or_compute_work_object_signature(work_object_id, issue)
    my_topic_key = wp._topic_key_for_signature(issue, my_sig)
    my_project_id = issue.get("project_id")

    if workgraph_discovery.has_confirmed_non_fasttrack_definitions():
        others = ws.list_issues(states=None, limit=10000) + ws.list_clusters(limit=10000)
    else:
        wp.ensure_fasttrack_index_backfilled()
        pool_ids = wp.candidate_pool_via_data_point_index(work_object_id, my_sig, my_topic_key)
        others = [o for o in (ws.get_issue_or_cluster(oid) for oid in pool_ids) if o is not None]

    candidates = []
    for other in others:
        if other["id"] == work_object_id:
            continue
        if my_project_id and my_project_id == other.get("project_id"):
            continue
        other_sig = wp.get_or_compute_work_object_signature(other["id"], other)
        other_topic_key = wp._topic_key_for_signature(other, other_sig)
        points = wp._matched_data_points(
            work_object_id, my_sig, my_topic_key, other["id"], other_sig, other_topic_key,
        )
        if len(points) >= 2:
            candidates.append({"candidate_id": other["id"], "matched_signals": points})
    return candidates


_MAX_COMPARATIVE_CANDIDATES = 8


def _aggregate_candidates_by_project(candidates: list) -> list:
    """Task #364: buckets candidates that already share the same parent
    Project into a single comparative-prompt slot before ranking/capping,
    so N sibling Issues under one already-established Project don't each
    independently consume one of the _MAX_COMPARATIVE_CANDIDATES slots -
    which could both push a real candidate from a DIFFERENT project out of
    the capped set and waste tokens showing the model near-duplicate
    evidence for what is, from the "which Project is this" question's own
    point of view, a single answer.

    GUARDRAIL (ROADMAP.md "Standing guardrail: the 2-point candidate-
    detection gate is load-bearing"): every candidate passed in here has
    ALREADY passed find_candidates' >=2-matched-point gate - this function
    never re-examines or loosens that gate, it only changes how many
    prompt slots already-gated candidates consume.

    Candidates with no project_id (a raw cluster, or another Issue that
    hasn't been grouped yet) are never bucketed with anything - there is
    no parent Project to aggregate them under, so they pass through
    unchanged, one slot each, exactly as before this task.

    Representative per project bucket = the single member with the most
    matched real data points (ties broken by candidate_id for determinism)
    - the richest single piece of evidence for that project is what gets
    read in full by judge_candidates. matched_signals on the returned
    candidate is the UNION of every bucketed member's matched signals, so
    the comparative prompt's own match-count line reflects the full
    strength of the project's evidence, not just the one representative
    Issue's slice of it. Whichever Issue ends up as the representative,
    process_new_item's merge_issues call still resolves to the SAME
    Project either way (merge_issues joins whichever side already has a
    project_id) - this never changes which Project a "same_project"
    verdict lands in, only which slot/evidence the model saw to get there."""
    by_project: dict = {}
    standalone = []
    for c in candidates:
        obj = ws.get_issue_or_cluster(c["candidate_id"])
        project_id = (obj or {}).get("project_id")
        if project_id:
            by_project.setdefault(project_id, []).append(c)
        else:
            standalone.append(c)

    aggregated = list(standalone)
    for members in by_project.values():
        representative = max(
            sorted(members, key=lambda m: m["candidate_id"]),
            key=lambda m: len(m["matched_signals"]),
        )
        union_signals = sorted(set().union(*(set(m["matched_signals"]) for m in members)))
        aggregated.append({**representative, "matched_signals": union_signals})
    return aggregated


_CANDIDATE_BLOCK_TEMPLATE = """CANDIDATE {index} (already tracked - shares {match_count} real data point(s): {matched_signals}):
{text}"""

_COMPARATIVE_JUDGMENT_PROMPT_TEMPLATE = """You are judging whether a NEW piece of business communication is the same real project/workstream as any of several ALREADY-TRACKED candidates, or related to one of them under a different project.

NEW ITEM (being evaluated):
{text_b}

{candidate_blocks}
{precedent_line}
Read everything fully. At most ONE candidate can be the same underlying project as the new item - if more than one looks plausible, choose the SINGLE best match. If you genuinely cannot tell which (if any) of several plausible candidates is right, say so rather than guessing.

For whichever candidate you choose (if any), decide the real relationship:
- SAME_PROJECT: the new item concerns the same underlying project/workstream as that candidate, even if it appears in a different thread, channel, phase, or sub-issue.
- RELATED_DIFFERENT_PROJECT: the same relationship/counterparty is involved, but the new item concerns a distinct transaction, SOW, renewal cycle, initiative, implementation, engagement, or other independently trackable body of work.

Respond with EXACTLY these lines, nothing else:
MATCH: <candidate number, or NONE if none of them match, or UNCERTAIN if you genuinely cannot tell>
VERDICT: <same_project or related_different_project - whichever real relationship you decided above>
(include the VERDICT line only when MATCH is a candidate number - omit it entirely for NONE or UNCERTAIN)
"""
# External-review finding #354 (2026-08-13): the example line above used to
# hardcode "VERDICT: same_project" verbatim, one line after defining
# SAME_PROJECT and RELATED_DIFFERENT_PROJECT as two equally valid verdicts -
# a real contradiction that could suppress the Project-vs-Relationship
# distinction Track B.5 exists to make. Very likely a regression: task #341
# fixed this exact contradiction in the OLD pairwise prompt template, which
# Track B.5 then deleted and rebuilt from scratch, silently reintroducing
# it. The parser (_parse_comparative_verdict) already accepted both values
# via _COMPARATIVE_VALID_VERDICTS - this was a prompt-text bug only, never
# a parsing bug, and does NOT touch the 2-point candidate-detection gate
# (see ROADMAP.md's standing guardrail) - only the wording of the example
# shown to the model for an already-gated candidate's judgment.

_COMPARATIVE_VALID_VERDICTS = ("same_project", "related_different_project")


def _parse_comparative_verdict(stdout: str, n: int) -> dict:
    """Returns one of:
      {"status": "match", "index": 0-based int, "verdict": one of
       _COMPARATIVE_VALID_VERDICTS}
      {"status": "none", "index": None, "verdict": None} - a confident
       "no real match" read (functionally what the old 3-way "unrelated"
       verdict meant).
      {"status": "uncertain", "index": None, "verdict": None} - the model
       was genuinely torn between plausible candidates and said so rather
       than guessing; this is the direct replacement for the old
       "ambiguous" outcome (review point #8) - previously derived only by
       noticing two INDEPENDENT pairwise calls both said same_project for
       different projects, which could never happen here since exactly one
       comparative call makes one joint decision.
      {"status": "unparseable", "index": None, "verdict": None} - a
       timeout, a crashed subprocess, or a malformed reply. Never a
       guess - the caller treats this exactly like the old None-verdict
       case: move on, no permanent record, retried next cycle."""
    match_line = None
    verdict_line = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        upper = line.upper()
        if match_line is None and upper.startswith("MATCH:"):
            match_line = line.split(":", 1)[1].strip()
        elif verdict_line is None and upper.startswith("VERDICT:"):
            verdict_line = line.split(":", 1)[1].strip().lower()
    if match_line is None:
        return {"status": "unparseable", "index": None, "verdict": None}
    normalized = match_line.upper()
    if normalized == "NONE":
        return {"status": "none", "index": None, "verdict": None}
    if normalized == "UNCERTAIN":
        return {"status": "uncertain", "index": None, "verdict": None}
    try:
        idx = int(match_line)
    except ValueError:
        return {"status": "unparseable", "index": None, "verdict": None}
    if not (1 <= idx <= n):
        return {"status": "unparseable", "index": None, "verdict": None}
    if verdict_line not in _COMPARATIVE_VALID_VERDICTS:
        return {"status": "unparseable", "index": None, "verdict": None}
    return {"status": "match", "index": idx - 1, "verdict": verdict_line}


def judge_candidates(work_object_id: str, candidates: list, *, model: Optional[str] = None,
                      precedent_context: Optional[str] = None) -> dict:
    """ONE comparative LLM call across every qualifying candidate, replacing
    the old N-independent-pairwise-calls design (2026-08-11, review point
    #8): calling judge_candidate once per candidate meant the model was
    never actually asked to choose between them, so two candidates could
    each independently come back "same_project" for two DIFFERENT existing
    projects - a logically incoherent pair of verdicts the old code could
    only detect after the fact (process_new_item's own "ambiguous"
    handling) and never actually resolve. This function asks the real
    question directly: given all the real candidates at once, which one
    (if any) is the match?

    Ranks candidates by matched-signal count and caps at
    _MAX_COMPARATIVE_CANDIDATES for prompt-size sanity - real candidate
    lists are rarely anywhere near that size, but a silent drop past the
    cap would violate the "no silent caps" discipline, so it's logged.

    Returns _parse_comparative_verdict's dict, plus (on a "match") the
    resolved candidate dict itself under "candidate" - the caller never
    needs to know about the ranking/capping done here to map an index back
    to a real candidate_id.

    Reads via build_identity_packet for every side, not a plain full-text
    slice (review point #6/#9) - see that function's own docstring.

    Task #364: candidates are bucketed by parent Project (_aggregate_
    candidates_by_project) BEFORE ranking/capping - see that function's
    own docstring for why and its guardrail note."""
    candidates = _aggregate_candidates_by_project(candidates)
    ranked = sorted(candidates, key=lambda c: -len(c["matched_signals"]))
    capped = ranked[:_MAX_COMPARATIVE_CANDIDATES]
    skipped = len(ranked) - len(capped)
    if skipped > 0:
        print(f"[workgraph_pipeline2] judge_candidates: {skipped} candidate(s) for {work_object_id} "
              f"skipped past the {_MAX_COMPARATIVE_CANDIDATES}-candidate comparative-prompt cap")
    text_b = build_identity_packet(work_object_id)
    blocks = [
        _CANDIDATE_BLOCK_TEMPLATE.format(
            index=i, text=build_identity_packet(c["candidate_id"]),
            match_count=len(c["matched_signals"]), matched_signals=", ".join(c["matched_signals"]),
        )
        for i, c in enumerate(capped, start=1)
    ]
    precedent_line = (f"\nHistorical note (context only - still judge on the evidence above): "
                       f"{precedent_context}\n") if precedent_context else ""
    prompt = _COMPARATIVE_JUDGMENT_PROMPT_TEMPLATE.format(
        text_b=text_b, candidate_blocks="\n\n".join(blocks), precedent_line=precedent_line,
    )
    try:
        proc = _run_headless_claude(prompt, timeout=_JUDGE_TIMEOUT_SECONDS, model=model)
    except subprocess.TimeoutExpired:
        return {"status": "unparseable", "index": None, "verdict": None}
    parsed = _parse_comparative_verdict(proc.stdout, n=len(capped))
    if parsed["status"] == "match":
        parsed["candidate"] = capped[parsed["index"]]
    return parsed


def _candidate_set_hash(work_object_id: str, candidates: list) -> str:
    """Cheap (no LLM call) fingerprint of exactly what judge_candidates
    would read right now for this whole candidate set - review point #4's
    fingerprint cache, rescoped from a per-pair key to a per-work-object
    key (2026-08-11, review point #8) since the comparative call makes one
    joint decision over the full set, not N independent ones. The moment
    the new item's own evidence, any candidate's evidence, or the
    candidate SET ITSELF changes (a candidate appears/disappears), this
    hash changes too, and the item is judged fresh rather than reusing a
    stale answer."""
    payload = {
        "b": full_text_for_work_object(work_object_id),
        "candidates": sorted(
            [{"id": c["candidate_id"], "a": full_text_for_work_object(c["candidate_id"]),
              "matched_signals": sorted(c["matched_signals"])} for c in candidates],
            key=lambda x: x["id"],
        ),
    }
    return ws.canonical_json_hash(payload)


def _precedent_context_line(precedent: Optional[str], issue: dict) -> Optional[str]:
    category = issue.get("category") or "this type of"
    if precedent == "confirmed":
        return (f"similar {category} cases with this counterparty have previously turned out to be "
                f"real matches")
    if precedent == "rejected":
        return (f"similar {category} cases with this counterparty have previously turned out NOT "
                f"to match")
    return None


def _finalize_as_new_project(work_object_id: str, issue: dict) -> dict:
    project_id = ws.create_project_with_new_id(
        name=issue.get("title") or "Untitled", category=issue.get("category"),
    )
    ws.assign_issue_to_project(work_object_id, project_id, reason="pipeline2: no real match found, new project")
    run_project_extraction(project_id, model="haiku")
    return {"work_object_id": work_object_id, "action": "new_project", "project_id": project_id}


def process_new_item(work_object_id: str, *, model: Optional[str] = "sonnet") -> dict:
    """The real step 3->4 pipeline for ONE freshly-classified item that
    step 2's exact-match check already failed to link anywhere. Finds
    every 2+-point candidate and, if any exist, asks ONE real comparative
    LLM question (judge_candidates) before deciding anything - no
    permanent veto on a non-match. If nothing matches, this item becomes
    its own new project immediately - per Marc's own words, every item
    ends up in SOME project, never left dangling.

    Rewritten again 2026-08-11 (review point #8, this session's own
    architectural review of the 2026-08-11 rewrite below): the previous
    version judged every candidate with an INDEPENDENT pairwise call, then
    reconciled the verdicts after the fact - which meant the model was
    never actually asked to choose between candidates, and two candidates
    could each independently come back "same_project" for two DIFFERENT
    existing projects, a logically incoherent pair of verdicts only ever
    caught downstream as "ambiguous." judge_candidates now asks the real
    question directly in one call: given every real candidate at once,
    which one (if any) is the match? See its own and _parse_comparative_
    verdict's docstrings for the full "match / none / uncertain /
    unparseable" outcome shape - "uncertain" is the direct replacement for
    the old multi-project "ambiguous" outcome, now driven by the model
    saying so rather than derived from two calls colliding.

    Original 2026-08-11 rewrite's second gap - a "confirmed"/"rejected"
    Total Recall precedent must never skip the real LLM read - is
    unchanged here: precedent is still only ever one line of context
    (_precedent_context_line) inside the comparative prompt, never a
    bypass. record_confirmed_or_rejected still logs the real, genuine
    verdict afterward exactly as before.

    Review point #4's fingerprint cache is rescoped from per-pair to
    per-work-object (_candidate_set_hash) to match the one-call-per-item
    shape - see that function's own docstring.

    model: "sonnet" (2026-08-11, Marc's own direct instruction after a
    live side-by-side test this session confirmed Sonnet matches Opus's
    judgment on a real positive AND a real negative merge pair) - passed
    through as an explicit default here, not hardcoded inside judge_
    candidates itself, so a future re-test against a different tier only
    ever requires changing this one call site.

    Haiku backfill runs here, once, right before candidate search -
    between the deterministic extraction and the 2+-point matching gate.
    Genuinely PLURAL (design doc Section 5.2, task #215) - fills EVERY
    confirmed data point still missing a value for this item in one call,
    not just company. Only runs for THIS item, never for the existing
    candidates - each of those already went through this same step when
    IT was the new item being processed. invalidate_work_object_signature
    is required here, not optional: get_or_compute_work_object_signature
    is cache-first, so without busting the cache, find_candidates below
    would read the STALE pre-backfill signature and the new party would
    never reach the point-matching gate at all this run."""
    issue = ws.get_issue_or_cluster(work_object_id)
    if issue is None:
        return {"work_object_id": work_object_id, "action": "not_found"}
    if issue.get("project_id"):
        return {"work_object_id": work_object_id, "action": "already_grouped",
                "project_id": issue["project_id"]}

    if workgraph_discovery.llm_backfill_missing_values(work_object_id):
        ws.invalidate_work_object_signature(work_object_id)

    candidates = find_candidates(work_object_id, issue)
    if not candidates:
        return _finalize_as_new_project(work_object_id, issue)

    precedent = workgraph_lessons.precedent_prefilter(issue)
    precedent_context = _precedent_context_line(precedent, issue)

    # Review point #4: skip the LLM call entirely when this exact
    # candidate SET was already judged against evidence that hasn't
    # changed since - see _candidate_set_hash's own docstring. A cache
    # miss (a genuinely new/changed set) always falls through to the real
    # call below.
    cache_key = work_object_id
    evidence_hash = _candidate_set_hash(work_object_id, candidates)
    cached = ws.get_cached_judgment(cache_key)
    if cached and cached["evidence_hash"] == evidence_hash:
        decision = json.loads(cached["verdict"])
    else:
        parsed = judge_candidates(work_object_id, candidates, model=model, precedent_context=precedent_context)
        if parsed["status"] == "unparseable":
            # A timeout/crash/malformed reply is never cached - it's not a
            # real answer worth remembering, and caching it would block a
            # retry that might succeed next time.
            decision = {"status": "unparseable", "candidate_id": None, "verdict": None}
        else:
            candidate_id = parsed["candidate"]["candidate_id"] if parsed["status"] == "match" else None
            decision = {"status": parsed["status"], "candidate_id": candidate_id, "verdict": parsed.get("verdict")}
            ws.upsert_cached_judgment(cache_key, evidence_hash, json.dumps(decision), model=model)

    status = decision["status"]
    chosen_candidate_id = decision["candidate_id"]
    verdict = decision["verdict"]

    if status == "uncertain":
        # Direct replacement for the old multi-project "ambiguous" outcome
        # (review point #8) - the model itself said it can't tell, rather
        # than this being derived after the fact from two independent
        # calls disagreeing. Never guess - park it, exactly like a plain
        # non-match, so it's re-evaluated next cycle (or served from cache
        # if nothing has actually changed) rather than silently mis-filed.
        return {"work_object_id": work_object_id, "action": "ambiguous"}

    chosen_candidate = None
    if chosen_candidate_id:
        chosen_candidate = next((c for c in candidates if c["candidate_id"] == chosen_candidate_id), None)

    if status == "match" and verdict == "related_different_project" and chosen_candidate:
        try:
            ws.upsert_work_object_relationship(
                a_id=work_object_id, b_id=chosen_candidate_id, relationship_type="rejected",
                match_count=len(chosen_candidate["matched_signals"]), matched_signals=chosen_candidate["matched_signals"],
            )
        except Exception:
            pass  # relationship bookkeeping must never break the real grouping decision

    if status == "match" and verdict == "same_project" and chosen_candidate:
        result = wp.merge_issues(
            work_object_id, chosen_candidate_id,
            reason_label=f"pipeline2: LLM-confirmed match ({','.join(chosen_candidate['matched_signals'])})",
        )
        if result["status"] == "merged":
            project_id = result["project_id"]
            workgraph_lessons.record_confirmed_or_rejected(issue_id_a=work_object_id, status="confirmed")
            run_project_extraction(project_id, model="haiku")
            return {"work_object_id": work_object_id, "action": "merged",
                    "project_id": project_id, "candidate_id": chosen_candidate_id}
        # "deferred" - a rare two-already-established-projects collision,
        # merge_issues' own existing safety net. Under the old N-
        # independent-calls design there could be another same_project
        # candidate to try next; under this comparative design there is
        # only ever one chosen candidate, so this falls through to a new
        # project below, same as if the verdict had been "none".

    # Task #334's fix (only a real "no real match" read counts as grounds
    # to write a "rejected" Total Recall precedent) still applies here:
    # "unparseable" (no genuine LLM judgment happened) and a "match" whose
    # verdict is related_different_project (a distinct outcome, already
    # captured above via the relationship signal) must neither seed a
    # false precedent that would then bias every future judgment for the
    # same category+company situation_key via _precedent_context_line.
    # Only a confident "none" is a genuine "doesn't match" read.
    if status == "none":
        workgraph_lessons.record_confirmed_or_rejected(issue_id_a=work_object_id, status="rejected")

    return _finalize_as_new_project(work_object_id, issue)


def run_pipeline_for_ungrouped_items(limit: int = 500) -> dict:
    """Step 3's real entry point - every issue/cluster with no project_id
    yet, oldest first, run through process_new_item. This is what the
    live scheduled loop calls in place of the retired group_issue()/
    run_project_grouping_oneshot pairing."""
    ungrouped = [
        w for w in (ws.list_issues(states=None, limit=10000) + ws.list_clusters(limit=10000))
        if not w.get("project_id")
    ]
    ungrouped.sort(key=lambda w: w.get("opened_at") or w.get("created_ts") or 0)
    results = []
    # Found live 2026-08-08: one bad item (e.g. the participants_json-as-
    # bare-string bug fixed in outlook_com_ingest.py the same day) used to
    # raise out of process_new_item() with no per-item guard, aborting this
    # WHOLE function - scheduled_refresh.py's own try/except around the
    # pipeline2 call then turned an entire cycle's grouping into "0 items
    # processed," not just "1 item skipped." Confirmed via the live corpus:
    # a permanent backlog of ~70 ungrouped items sitting behind whichever
    # bad row was oldest, since the same crash recurs every cycle until the
    # oldest item is fixed or removed. Isolated per item instead, same
    # never-let-one-failure-block-the-rest discipline as every other sweep
    # in scheduled_refresh.py - a failed item is reported, not silent, and
    # every item after it still gets its real chance this cycle.
    for work_object in ungrouped[:limit]:
        try:
            results.append(process_new_item(work_object["id"]))
        except Exception as e:
            results.append({"work_object_id": work_object["id"], "action": "error", "error": str(e)})
    return {"checked": len(results), "results": results}


_EXTRACTION_PROMPT_TEMPLATE = """You are reviewing the already-extracted claims (asks/decisions/commitments/dates) tracked against ONE real business project, to decide which genuinely belong together as separate trackable issues.

CLAIMS (id | type | status | text):
{claims_text}

EXISTING TRACKED ISSUES (may be empty for a brand-new project):
{existing_items}

Decide which claims above are not yet covered by an existing tracked issue and genuinely belong together as one real, separately trackable issue - a pricing-negotiation ask and a separate onboarding-scope ask living in the same communications are TWO issues, not one. For each new issue, output one line in exactly this format:

ISSUE: <short title> | CLAIM_IDS: <comma-separated claim ids from the list above>

Only cite claim ids that appear in the CLAIMS list above - never invent one, and never cite the same claim id under two different ISSUE lines.

Then, on its own final line, output a one-sentence project summary in exactly this format:

SUMMARY: <one sentence describing the real current state of this project, grounded only in the claims above>

Output nothing else.
"""


def _parse_extraction_output(stdout: str) -> dict:
    issues = []
    summary = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.upper().startswith("ISSUE:"):
            rest = line.split(":", 1)[1]
            fields = [f.strip() for f in rest.split("|")]
            title = fields[0] if fields else ""
            claim_ids = []
            for field in fields[1:]:
                if field.upper().startswith("CLAIM_IDS:"):
                    for part in field.split(":", 1)[1].split(","):
                        part = part.strip()
                        if part.isdigit():
                            claim_ids.append(int(part))
            if title and claim_ids:
                issues.append({"title": title, "claim_ids": claim_ids})
        elif line.upper().startswith("SUMMARY:"):
            summary = line.split(":", 1)[1].strip()
    return {"issues": issues, "summary": summary}


def run_project_extraction(project_id: str, *, model: Optional[str] = None) -> dict:
    """Step 6 - fires immediately after every successful group (a merge
    or a new-project creation), never on a separate schedule.

    model: optional cheap-model override (see _run_headless_claude's own
    docstring) - the live forward-path caller (process_new_item) never
    passes this, so it stays on this pipeline's trusted default (opus);
    only the one-time stale-marker backfill script (task #304/#310,
    2026-08-11 cost investigation) passes an override, deliberately
    scoped to that backfill alone.

    Consolidated onto the claim-grounded path (task #304, item #3,
    2026-08-11, Marc's own explicit build authorization and his own
    "correct fix for a company-wide rollout" call): previously read the
    project's WHOLE raw-text corpus and created issues directly via
    ws.create_issue_with_new_id, with zero claim/evidence/party linkage -
    a real, separate mechanism from curator's claim-grounded extract_
    issue_from_project (SYNTHESIS_ROUTINE.md step 4a). The two disagreeing
    is exactly the live inconsistency Marc flagged; worse, this function's
    own upsert_synthesis call stamped synthesized_from_marker to the SAME
    marker curator's staleness check reads, so a project could permanently
    read "already synthesized" the moment this ungrounded pass ran,
    starving curator's real pass from ever correcting it.

    Now reads the project's already-materialized claims (list_claims_for_
    issues over every member cluster/issue - the same claims ledger both
    real materialize_claims_for_raw_item producers write to at extraction
    time, normally well before grouping runs: server_lean.py's POST
    /api/workgraph/raw_items/{id}/extraction route for curator's full
    synthesis wake, and workgraph_synthesis_light.py's hybrid-routing
    light path for small evidence deltas - this reads whichever already
    ran, agnostic to which one it was),
    asks the LLM which claims belong together as a real issue, and creates
    each one through workgraph_projects.extract_issue_from_project - the
    exact same deterministic mechanics (claim reassignment, evidence,
    parties) curator's own routine uses. One mechanism, always
    claim-grounded, from the moment an issue is born.

    Deliberately reads claims from every member (cluster OR already-real
    issue), not just clusters - the "new_project" caller in
    process_new_item assigns the triggering work_object (which can itself
    already be a real, is_raw_cluster=0 issue, not only a cluster) to the
    project BEFORE calling this, so its claims must stay eligible on this
    very first pass. Two dedup layers, matching exactly the level of trust
    extract_issue_from_project's own callers already extend elsewhere in
    this codebase (curator's own SYNTHESIS_ROUTINE step 4a has the same
    shape and no stronger guarantee): already_cited below is a genuine,
    new, code-level guard against the SAME claim id appearing under two
    different ISSUE: lines within one LLM response; avoiding re-citation
    of a claim an EARLIER run_project_extraction call already moved onto
    a real issue relies on the EXISTING TRACKED ISSUES list in the prompt
    below, the same "don't re-extract what's already tracked" discipline
    curator's own claim-grounded flow already runs on."""
    project = ws.get_project(project_id)
    if project is None:
        return {"project_id": project_id, "action": "not_found"}

    member_ids = (
        [c["id"] for c in ws.list_clusters_for_project(project_id)]
        + [i["id"] for i in ws.list_issues_for_project(project_id)]
    )
    claims_by_member = ws.list_claims_for_issues(member_ids)
    all_claims = [c for claims in claims_by_member.values() for c in claims]
    if not all_claims:
        return {"project_id": project_id, "action": "no_claims_yet"}

    claims_text = "\n".join(f"{c['id']} | {c['claim_type']} | {c['status']} | {c['text']}" for c in all_claims)
    existing = ws.list_issues_for_project(project_id)
    existing_text = "\n".join(f"- {i['title']}" for i in existing) or "(none yet)"

    prompt = _EXTRACTION_PROMPT_TEMPLATE.format(
        claims_text=claims_text[:_MAX_TEXT_CHARS], existing_items=existing_text,
    )
    try:
        proc = _run_headless_claude(prompt, timeout=_EXTRACTION_TIMEOUT_SECONDS, model=model)
    except subprocess.TimeoutExpired:
        return {"project_id": project_id, "action": "timeout"}

    parsed = _parse_extraction_output(proc.stdout)
    valid_claim_ids = {c["id"] for c in all_claims}
    already_cited: set = set()
    created = []
    for entry in parsed["issues"]:
        claim_ids = [cid for cid in entry["claim_ids"] if cid in valid_claim_ids and cid not in already_cited]
        if not claim_ids:
            continue
        try:
            result = wp.extract_issue_from_project(
                project_id, title=entry["title"], category=project.get("category"), claim_ids=claim_ids,
            )
        except ValueError:
            continue  # never let one malformed LLM line abort the whole extraction pass
        already_cited.update(claim_ids)
        created.append(result["issue_id"])

    if parsed["summary"]:
        marker = workgraph_synthesis.compute_evidence_marker("project", project_id)
        ws.upsert_synthesis(
            entity_type="project", entity_id=project_id, summary=parsed["summary"],
            next_steps_json=json.dumps([]), suggested_actions_json=json.dumps([]),
            synthesized_from_marker=marker,
        )

    return {"project_id": project_id, "action": "extracted",
            "created_issue_ids": created, "summary": parsed["summary"]}
