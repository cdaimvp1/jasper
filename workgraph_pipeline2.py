"""
workgraph_pipeline2.py - the NEW grouping-and-extraction pipeline
(2026-08-05, Marc's own exhaustive spec, replacing the retired
suggestion-queue/curator-reviewed model entirely). Deliberately self-
contained: does not import ingest/scheduled_refresh.py, workgraph_
lessons.py, or use the pending_project_suggestions/identity_constraints
tables for any decision. Marc's own words: "CURATOR OR ANY OTHER
PREVIOUSLY BUILT MECHANISM SHOULD NOT TOUCH THIS. BUILD NEW MECHANISMS
FOR IT. KEEP IT ENTIRELY SEPARATE."

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

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Optional

import json

import text_extract
import workgraph_store as ws
import workgraph_projects as wp
import workgraph_synthesis

_JUDGE_TIMEOUT_SECONDS = 300
_EXTRACTION_TIMEOUT_SECONDS = 600
_MAX_TEXT_CHARS = 12000


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
    call, so it doesn't need to pay for the default model."""
    env = os.environ.copy()
    args = ["claude", "-p", prompt, "--allowedTools", ""]
    if model:
        args += ["--model", model]
    proc = subprocess.Popen(
        args,
        cwd=str(Path(__file__).resolve().parent), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=15)
        except Exception:
            pass
        proc.communicate()
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def full_text_for_work_object(work_object_id: str) -> str:
    """Every linked raw_item's full resolved text (never a preview) -
    subject + real body - what steps 4/6 actually read. Newline-joined,
    one raw_item per block. Also includes each linked attachment's own
    extracted_text (2026-08-06, Kinaxis investigation) - real live example
    that motivated this: a signed Change Request PDF/DOCX carried the only
    copy of a real reference number nowhere present in any email body on
    the same issue. Same real, already-extracted text source
    reference_base_ids_for_issue (workgraph_projects.py) now also scans -
    kept as a separate read here rather than sharing code, since that
    function returns a normalized id set and this one needs raw prose."""
    parts = []
    for item in ws.get_raw_items_for_issue(work_object_id):
        subject = item.get("subject") or ""
        body = text_extract.resolve_item_text(item)
        parts.append(f"Subject: {subject}\n{body}")
    for att in ws.list_attachments_for_issue(work_object_id):
        text = att.get("extracted_text")
        if text:
            parts.append(f"Attachment ({att.get('filename') or 'unnamed'}):\n{text}")
    return "\n\n---\n\n".join(parts)


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
    exempt from LLM review, not a shared business reference number."""
    if issue is None:
        issue = ws.get_issue_or_cluster(work_object_id)
    my_sig = wp.get_or_compute_work_object_signature(work_object_id, issue)
    my_topic_key = wp._topic_key_for_signature(issue, my_sig)
    my_project_id = issue.get("project_id")
    candidates = []
    for other in ws.list_issues(states=None, limit=10000) + ws.list_clusters(limit=10000):
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


_COMPANY_BACKFILL_TIMEOUT_SECONDS = 60
_COMPANY_BACKFILL_MODEL = "haiku"

_COMPANY_BACKFILL_PROMPT_TEMPLATE = """Read this real business communication.

TEXT:
{text}

The deterministic extraction found no external company/organization for this item. Look for a real external company genuinely referenced in this text (not Marc Lane's own employer, not an internal team), together with the specific email address of a person at that company who is a sender or participant here.

For each confident (email, company) pair you find, output one line in exactly this format:
COMPANY_MATCH: <email> | <company name>

If you cannot confidently identify any, output exactly:
COMPANY_MATCH: NONE

Output nothing else.
"""


def _parse_company_matches(stdout: str) -> list[dict]:
    matches = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.upper().startswith("COMPANY_MATCH:"):
            continue
        rest = line.split(":", 1)[1].strip()
        if rest.upper() == "NONE":
            continue
        parts = [p.strip() for p in rest.split("|", 1)]
        if len(parts) == 2 and "@" in parts[0] and parts[1]:
            matches.append({"email": parts[0].lower(), "company": parts[1]})
    return matches


def llm_backfill_company(work_object_id: str) -> list[dict]:
    """Haiku mid-step (Marc's own design ask, 2026-08-06 Kinaxis
    investigation): when the deterministic pass leaves external_orgs
    empty, a cheap/fast model reads the SAME text (full_text_for_work_
    object, which now also includes attachment text) once, targeted at
    exactly the one missing field - not a full re-extraction, not the
    judgment-call model tier judge_candidate uses.

    Writes any confident finding as a REAL party record, tagged
    affiliation_source='llm_backfill' so it stays auditable/reversible,
    never indistinguishable from a regex-confirmed extraction. This is
    what makes it flow into compute_work_object_signature's external_orgs
    the same way any other party already does - no separate signature
    concept to maintain.

    Deliberately narrow: only creates a party where NONE exists yet for
    that email (upsert_party's own existing-row path never touches
    company - by design, so a real extraction is never silently
    overwritten - which means this can't safely correct an existing
    party's blank company field, only fill a genuine gap). A real live
    example this narrower scope still covers: Jasmine Joseph
    (jjoseph@kinaxis.com) and DocuSign's own notification address had NO
    party record at all for the Kinaxis Maestro thread that motivated
    this - confirmed live against the real DB, not assumed."""
    issue = ws.get_issue_or_cluster(work_object_id)
    if issue is None:
        return []
    sig = wp.get_or_compute_work_object_signature(work_object_id, issue)
    if sig["external_orgs"]:
        return []  # deterministic pass already found a real company - nothing to backfill
    text = full_text_for_work_object(work_object_id)
    if not text.strip():
        return []
    prompt = _COMPANY_BACKFILL_PROMPT_TEMPLATE.format(text=text[:_MAX_TEXT_CHARS])
    try:
        proc = _run_headless_claude(prompt, timeout=_COMPANY_BACKFILL_TIMEOUT_SECONDS, model=_COMPANY_BACKFILL_MODEL)
    except subprocess.TimeoutExpired:
        return []
    applied = []
    for match in _parse_company_matches(proc.stdout):
        email = match["email"]
        if ws.get_party_by_email(email) is not None:
            continue  # a party already exists for this email - not this function's job to correct it
        party_id = f"llm-{hashlib.sha256(email.encode()).hexdigest()[:12]}"
        ws.upsert_party(
            id=party_id, primary_email=email, display_name=None,
            affiliation="external", affiliation_confidence="L", affiliation_source="llm_backfill",
            company=match["company"],
        )
        ws.link_party_to_issue(work_object_id, party_id)
        applied.append(match)
    return applied


_JUDGMENT_PROMPT_TEMPLATE = """You are judging whether two real pieces of business communication describe the SAME underlying deal/project, or two different ones.

ITEM A (already tracked):
{text_a}

ITEM B (new, being evaluated):
{text_b}

They already share {match_count} real data point(s): {matched_signals}.

Read both fully. Decide: are these genuinely the same underlying deal or vendor relationship (even if they cover different individual transactions within one overall relationship), or are they unrelated/different deals that merely share a coincidental signal?

Respond with EXACTLY one line, nothing else:
VERDICT: yes
or
VERDICT: no
"""


def _parse_verdict(stdout: str) -> Optional[bool]:
    """None (never a guess) when no parseable VERDICT line exists at
    all - a timeout, a crashed subprocess, or a malformed response all
    fall through to this, and the caller treats it exactly like "no":
    move on, no permanent record either way."""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.upper().startswith("VERDICT:"):
            value = line.split(":", 1)[1].strip().lower()
            if value == "yes":
                return True
            if value == "no":
                return False
    return None


def judge_candidate(work_object_id: str, candidate_id: str, matched_signals: list) -> Optional[bool]:
    """The real, immediate LLM read of both sides' full text - step 4
    itself. Never raises on a timeout - treated the same as an
    unparseable response (None), so one slow/stuck judgment call can
    never crash the whole pipeline run."""
    text_a = full_text_for_work_object(candidate_id)
    text_b = full_text_for_work_object(work_object_id)
    prompt = _JUDGMENT_PROMPT_TEMPLATE.format(
        text_a=text_a[:_MAX_TEXT_CHARS], text_b=text_b[:_MAX_TEXT_CHARS],
        match_count=len(matched_signals), matched_signals=", ".join(matched_signals),
    )
    try:
        proc = _run_headless_claude(prompt, timeout=_JUDGE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None
    return _parse_verdict(proc.stdout)


def process_new_item(work_object_id: str) -> dict:
    """The real step 3->4 pipeline for ONE freshly-classified item that
    step 2's exact-match check already failed to link anywhere. Finds
    every 2+-point candidate, judges each with a real LLM read of full
    text, merges immediately on the first "yes" - no queue, no permanent
    veto on a "no". If nothing matches (or every candidate says no),
    this item becomes its own new project immediately - per Marc's own
    words, every item ends up in SOME project, never left dangling.

    Haiku company backfill (2026-08-06, Marc's own design ask) runs here,
    once, right before candidate search - between the deterministic
    extraction and the 2+-point matching gate, exactly the order Marc
    asked to confirm. Only runs for THIS item, never for the existing
    candidates it gets compared against below - each of those already
    went through this same step when IT was the new item being
    processed, so there's nothing to re-backfill. invalidate_work_object_
    signature is required here, not optional: get_or_compute_work_object_
    signature is cache-first, so without busting the cache, find_
    candidates below would read the STALE pre-backfill signature and the
    new party would never reach the point-matching gate at all this run."""
    issue = ws.get_issue_or_cluster(work_object_id)
    if issue is None:
        return {"work_object_id": work_object_id, "action": "not_found"}
    if issue.get("project_id"):
        return {"work_object_id": work_object_id, "action": "already_grouped",
                "project_id": issue["project_id"]}

    if llm_backfill_company(work_object_id):
        ws.invalidate_work_object_signature(work_object_id)

    candidates = find_candidates(work_object_id, issue)
    for candidate in candidates:
        verdict = judge_candidate(work_object_id, candidate["candidate_id"], candidate["matched_signals"])
        if verdict is not True:
            continue
        result = wp.merge_issues(
            work_object_id, candidate["candidate_id"],
            reason_label=f"pipeline2: LLM-confirmed match ({','.join(candidate['matched_signals'])})",
        )
        if result["status"] == "merged":
            project_id = result["project_id"]
            run_project_extraction(project_id)
            return {"work_object_id": work_object_id, "action": "merged",
                    "project_id": project_id, "candidate_id": candidate["candidate_id"]}
        # "deferred" - a rare two-already-established-projects collision,
        # merge_issues' own existing safety net. Try the next candidate
        # rather than treat this as a final answer.

    project_id = ws.create_project_with_new_id(
        name=issue.get("title") or "Untitled", category=issue.get("category"),
    )
    ws.assign_issue_to_project(work_object_id, project_id, reason="pipeline2: no real match found, new project")
    run_project_extraction(project_id)
    return {"work_object_id": work_object_id, "action": "new_project", "project_id": project_id}


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
    for work_object in ungrouped[:limit]:
        results.append(process_new_item(work_object["id"]))
    return {"checked": len(results), "results": results}


_EXTRACTION_PROMPT_TEMPLATE = """You are reviewing the full communication history of ONE real business project to keep its tracked checklist current.

PROJECT FULL TEXT (every linked communication):
{project_text}

EXISTING TRACKED ITEMS (may be empty for a brand-new project):
{existing_items}

Read the project text and decide what checklist items (asks/issues/requests/deliverables) are genuinely present. For each one that is NEW (not already in the existing tracked items list) or whose STATUS has changed, output one line in exactly this format:

ITEM: <short title> | STATUS: <active|done|blocked> | NOTE: <one-sentence grounding in the actual text>

Then, on its own final line, output a one-sentence project summary in exactly this format:

SUMMARY: <one sentence describing the real current state of this project>

Output nothing else.
"""


def _parse_extraction_output(stdout: str) -> dict:
    items = []
    summary = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.upper().startswith("ITEM:"):
            rest = line.split(":", 1)[1]
            fields = [f.strip() for f in rest.split("|")]
            entry = {"title": fields[0] if fields else ""}
            for field in fields[1:]:
                if field.upper().startswith("STATUS:"):
                    entry["status"] = field.split(":", 1)[1].strip().lower()
                elif field.upper().startswith("NOTE:"):
                    entry["note"] = field.split(":", 1)[1].strip()
            if entry["title"]:
                items.append(entry)
        elif line.upper().startswith("SUMMARY:"):
            summary = line.split(":", 1)[1].strip()
    return {"items": items, "summary": summary}


def run_project_extraction(project_id: str) -> dict:
    """Step 6 - fires immediately after every successful group (a merge
    or a new-project creation), never on a separate schedule. Reads the
    WHOLE project's full-text corpus (every member cluster/issue's own
    raw_items), asks the LLM what checklist items are genuinely present
    and what changed, and writes real issues + a project summary via the
    same deterministic store functions synthesis already uses elsewhere
    in this codebase - never a new, separate storage shape."""
    project = ws.get_project(project_id)
    if project is None:
        return {"project_id": project_id, "action": "not_found"}

    member_ids = (
        [c["id"] for c in ws.list_clusters_for_project(project_id)]
        + [i["id"] for i in ws.list_issues_for_project(project_id)]
    )
    project_text = "\n\n===\n\n".join(full_text_for_work_object(mid) for mid in member_ids)
    existing = ws.list_issues_for_project(project_id)
    existing_text = "\n".join(f"- {i['title']}" for i in existing) or "(none yet)"

    prompt = _EXTRACTION_PROMPT_TEMPLATE.format(
        project_text=project_text[:_MAX_TEXT_CHARS], existing_items=existing_text,
    )
    try:
        proc = _run_headless_claude(prompt, timeout=_EXTRACTION_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return {"project_id": project_id, "action": "timeout"}

    parsed = _parse_extraction_output(proc.stdout)
    existing_titles = {i["title"] for i in existing}
    created = []
    for entry in parsed["items"]:
        if entry["title"] in existing_titles:
            continue
        new_issue_id = ws.create_issue_with_new_id(
            title=entry["title"], category=project.get("category"),
            state="done" if entry.get("status") == "done" else "active",
        )
        ws.assign_issue_to_project(new_issue_id, project_id, reason="pipeline2: extracted from project full text")
        created.append(new_issue_id)

    if parsed["summary"]:
        marker = workgraph_synthesis.compute_evidence_marker("project", project_id)
        ws.upsert_synthesis(
            entity_type="project", entity_id=project_id, summary=parsed["summary"],
            next_steps_json=json.dumps([]), suggested_actions_json=json.dumps([]),
            synthesized_from_marker=marker,
        )

    return {"project_id": project_id, "action": "extracted",
            "created_issue_ids": created, "summary": parsed["summary"]}
