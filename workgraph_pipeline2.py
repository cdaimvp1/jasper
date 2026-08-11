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


_JUDGMENT_PROMPT_TEMPLATE = """You are judging the real relationship between two pieces of business communication.

ITEM A (already tracked):
{text_a}

ITEM B (new, being evaluated):
{text_b}

They already share {match_count} real data point(s): {matched_signals}.
{precedent_line}
Read both fully, then choose EXACTLY one of these three outcomes - decide only from the evidence above, using the historical note (if any) only as context, never as the answer itself:

- SAME_PROJECT: these describe the same underlying deal/workstream (even if they cover different individual transactions within one overall relationship).
- RELATED_DIFFERENT_PROJECT: same vendor/counterparty/relationship, but a genuinely distinct piece of work (a different SOW, a different renewal cycle, a different initiative).
- UNRELATED: the shared signal is coincidental; these are unrelated.

Respond with EXACTLY one line, nothing else:
VERDICT: same_project
or
VERDICT: related_different_project
or
VERDICT: unrelated
"""

_VALID_VERDICTS = ("same_project", "related_different_project", "unrelated")


def _parse_verdict(stdout: str) -> Optional[str]:
    """None (never a guess) when no parseable VERDICT line exists at
    all - a timeout, a crashed subprocess, or a malformed response all
    fall through to this, and the caller treats it exactly like
    "unrelated": move on, no permanent record either way."""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.upper().startswith("VERDICT:"):
            value = line.split(":", 1)[1].strip().lower()
            if value in _VALID_VERDICTS:
                return value
    return None


def judge_candidate(work_object_id: str, candidate_id: str, matched_signals: list,
                     *, model: Optional[str] = None, precedent_context: Optional[str] = None) -> Optional[str]:
    """The real, immediate LLM read of both sides' full text - step 4
    itself. Never raises on a timeout - treated the same as an
    unparseable response (None), so one slow/stuck judgment call can
    never crash the whole pipeline run.

    Returns one of _VALID_VERDICTS, or None on timeout/unparseable reply.
    Widened from a plain yes/no to a 3-way outcome (2026-08-11, Marc's
    own engineering-direction doc, Section 4 Step 4): a "related but
    different project" read used to have nowhere to go but the same bit
    as "unrelated," silently discarding a real relationship signal.

    precedent_context: optional one-line note injected into the prompt
    as CONTEXT ONLY, never a bypass of this call (2026-08-11, Section 5
    of the same doc: prior confirmed/rejected precedent must never skip
    this real evidence read - process_new_item now always calls this
    function, for every candidate, regardless of precedent).

    model: process_new_item's live call passes "sonnet" (2026-08-11,
    Marc's own direct instruction, after a live side-by-side comparison
    this session confirmed Sonnet matches Opus's judgment on both a real
    positive and a real negative merge pair) - kept as an optional
    override here, not a hardcoded default, so a future A/B test against
    a different tier stays just as easy."""
    text_a = full_text_for_work_object(candidate_id)
    text_b = full_text_for_work_object(work_object_id)
    precedent_line = f"\nHistorical note (context only - still judge THIS pair on the evidence above): {precedent_context}\n" if precedent_context else ""
    prompt = _JUDGMENT_PROMPT_TEMPLATE.format(
        text_a=text_a[:_MAX_TEXT_CHARS], text_b=text_b[:_MAX_TEXT_CHARS],
        match_count=len(matched_signals), matched_signals=", ".join(matched_signals),
        precedent_line=precedent_line,
    )
    try:
        proc = _run_headless_claude(prompt, timeout=_JUDGE_TIMEOUT_SECONDS, model=model)
    except subprocess.TimeoutExpired:
        return None
    return _parse_verdict(proc.stdout)


def _precedent_context_line(precedent: Optional[str], issue: dict) -> Optional[str]:
    category = issue.get("category") or "this type of"
    if precedent == "confirmed":
        return (f"similar {category} cases with this counterparty have previously turned out to be "
                f"real matches")
    if precedent == "rejected":
        return (f"similar {category} cases with this counterparty have previously turned out NOT "
                f"to match")
    return None


def process_new_item(work_object_id: str, *, model: Optional[str] = "sonnet") -> dict:
    """The real step 3->4 pipeline for ONE freshly-classified item that
    step 2's exact-match check already failed to link anywhere. Finds
    every 2+-point candidate and judges ALL of them with a real LLM read
    of full text before deciding anything - no permanent veto on a
    non-match. If nothing matches, this item becomes its own new project
    immediately - per Marc's own words, every item ends up in SOME
    project, never left dangling.

    Rewritten 2026-08-11 per Marc's own engineering-direction doc,
    Sections 4 (Step 4) and 5, after this session's own audit confirmed
    two real gaps against it:

    (1) USED TO merge on the first candidate that returned a "yes" and
    return immediately, never comparing the remaining candidates. Now
    every candidate is judged first (judge_candidate returns one of
    same_project/related_different_project/unrelated/None), and the
    verdicts are evaluated together:
      - exactly one DISTINCT target project among same_project verdicts
        -> merge (unchanged real-world outcome for the common case).
      - MULTIPLE distinct target projects both say same_project -> this
        is the doc's real "ambiguous" outcome. Never picked arbitrarily -
        returned as action="ambiguous" with every conflicting project id,
        and the item is left ungrouped (no permanent decision either way,
        same "no veto" philosophy this pipeline already applies to a
        plain non-match - it will naturally be re-evaluated next cycle by
        run_pipeline_for_ungrouped_items, which only pulls project_id IS
        NULL items).
      - a related_different_project verdict, on any candidate, now
        writes a real signal instead of vanishing: ws.upsert_work_object_
        relationship(..., relationship_type="rejected", ...) - the exact
        same table workgraph_relationships.run_relationship_sweep()
        already reads to build durable, named Relationship rows. This is
        that mechanism's first live production writer; previously only
        tests ever called it.

    (2) USED TO let a "confirmed"/"rejected" Total Recall precedent skip
    judge_candidate (the LLM) entirely - an unconditional bypass of
    current-evidence inspection the doc's Section 5 explicitly names as
    unsafe. Precedent is now ALWAYS just one line of context injected
    into the judgment prompt (_precedent_context_line) - judge_candidate
    is called for every real candidate, every time, regardless of
    precedent. record_confirmed_or_rejected still logs the real,
    genuine verdict afterward exactly as before - precedent only ever
    informs the read, never replaces it.

    model: "sonnet" (2026-08-11, Marc's own direct instruction after a
    live side-by-side test this session confirmed Sonnet matches Opus's
    judgment on a real positive AND a real negative merge pair) - passed
    through as an explicit default here, not hardcoded inside judge_
    candidate itself, so a future re-test against a different tier only
    ever requires changing this one call site.

    Haiku backfill runs here, once, right before candidate search -
    between the deterministic extraction and the 2+-point matching gate.
    Genuinely PLURAL (design doc §5.2, task #215) - fills EVERY confirmed
    data point still missing a value for this item in one call, not just
    company. Only runs for THIS item, never for the existing candidates -
    each of those already went through this same step when IT was the
    new item being processed. invalidate_work_object_signature is
    required here, not optional: get_or_compute_work_object_signature is
    cache-first, so without busting the cache, find_candidates below
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
    precedent = workgraph_lessons.precedent_prefilter(issue)
    precedent_context = _precedent_context_line(precedent, issue)

    judged = []  # list of (candidate, verdict)
    for candidate in candidates:
        verdict = judge_candidate(work_object_id, candidate["candidate_id"], candidate["matched_signals"],
                                   model=model, precedent_context=precedent_context)
        judged.append((candidate, verdict))

    for candidate, verdict in judged:
        if verdict == "related_different_project":
            try:
                ws.upsert_work_object_relationship(
                    a_id=work_object_id, b_id=candidate["candidate_id"], relationship_type="rejected",
                    match_count=len(candidate["matched_signals"]), matched_signals=candidate["matched_signals"],
                )
            except Exception:
                pass  # relationship bookkeeping must never break the real grouping decision

    same_project = [(c, v) for c, v in judged if v == "same_project"]
    target_projects: dict[str, list[dict]] = {}
    for candidate, _ in same_project:
        other = ws.get_issue_or_cluster(candidate["candidate_id"])
        target = (other or {}).get("project_id") or candidate["candidate_id"]
        target_projects.setdefault(target, []).append(candidate)

    if len(target_projects) > 1:
        # Real ambiguity (doc Section 4, Step 4): more than one existing
        # project both read as "same project." Never guess - park it,
        # exactly like a plain non-match, so it's re-evaluated next cycle
        # rather than silently mis-filed.
        return {"work_object_id": work_object_id, "action": "ambiguous",
                "candidate_project_ids": list(target_projects.keys())}

    if len(target_projects) == 1:
        candidates_for_target = next(iter(target_projects.values()))
        for candidate in candidates_for_target:
            result = wp.merge_issues(
                work_object_id, candidate["candidate_id"],
                reason_label=f"pipeline2: LLM-confirmed match ({','.join(candidate['matched_signals'])})",
            )
            if result["status"] == "merged":
                project_id = result["project_id"]
                workgraph_lessons.record_confirmed_or_rejected(issue_id_a=work_object_id, status="confirmed")
                run_project_extraction(project_id, model="haiku")
                return {"work_object_id": work_object_id, "action": "merged",
                        "project_id": project_id, "candidate_id": candidate["candidate_id"]}
            # "deferred" - a rare two-already-established-projects collision,
            # merge_issues' own existing safety net. Try the next candidate
            # for this same target rather than treat this as a final answer.

    # Task #334 fix (2026-08-11, found while building the regression corpus
    # for #333): only a real "unrelated" verdict counts as grounds to write
    # a "rejected" Total Recall precedent. `judged` also holds None entries
    # (judge_candidate's own timeout/unparseable case) and
    # "related_different_project" entries (a distinct outcome, already
    # captured above via the relationship signal) - neither is a genuine
    # LLM read confirming this situation doesn't match, so neither should
    # seed a false precedent that then biases every future judgment for the
    # same category+company situation_key via _precedent_context_line.
    if any(verdict == "unrelated" for _, verdict in judged):
        workgraph_lessons.record_confirmed_or_rejected(issue_id_a=work_object_id, status="rejected")

    project_id = ws.create_project_with_new_id(
        name=issue.get("title") or "Untitled", category=issue.get("category"),
    )
    ws.assign_issue_to_project(work_object_id, project_id, reason="pipeline2: no real match found, new project")
    run_project_extraction(project_id, model="haiku")
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
