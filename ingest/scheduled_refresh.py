"""
scheduled_refresh.py — the 5x/day cockpit refresh, run by a Windows Scheduled
Task (6am/8am/noon/5pm/midnight ET), independent of any Claude session or the
server being up.

Deterministic parts (mail ingest, classify, NBA re-score) call the same
modules directly - no dependency on server_lean.py running, no network hop.
The one part that genuinely needs an agent (Teams/Calendar/SharePoint, since
those MCP tools only work inside a live Claude Code session, confirmed this
session) runs as a SCOPED ONE-SHOT headless `claude -p` invocation for
`relay`, scoped to Bash only - same safety pattern already used for curator's
action-bridge proof, not a persistent unsupervised agent left running.

Logs one summary line per run to DATA_DIR/scheduled_refresh.log (paths.DATA_DIR,
i.e. TEAM_DATA_DIR - this file's own LOG_PATH already resolves it correctly in
code below; only the GRAPH_INGEST_ROUTINE.md instructions relay follows had a
hardcoded-relative-path version of this mistake, fixed 2026-07-29) so Marc can
check status without watching it happen live.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BODY = HERE.parent
sys.path.insert(0, str(BODY))

import workgraph_store as ws
import workgraph_classify
import workgraph_nba
import workgraph_alerts
import workgraph_synthesis
import workgraph_projects
import outlook_com_ingest
import retention
import health_check
import personal_patterns
import workgraph_aristotle

from paths import DATA_DIR

LOG_PATH = DATA_DIR / "scheduled_refresh.log"


def _run_headless_with_tree_kill(args: list, *, cwd: str, env: dict, timeout: int) -> subprocess.CompletedProcess:
    """Like subprocess.run(..., capture_output=True, text=True, timeout=...),
    but kills the WHOLE process tree on timeout, not just the immediate
    child. Confirmed gap, 2026-07-29: subprocess.run()'s own timeout handling
    only terminates the `claude` process itself - any grandchild it spawned
    via its own Bash tool calls (this session's own work is a live example of
    that pattern) survives as an orphan past the timeout, potentially still
    running and racing the very NEXT scheduled_refresh pass against the same
    workgraph.db. CREATE_NEW_PROCESS_GROUP (so taskkill's /T can find the
    whole tree by process-group, not just the one PID) + taskkill /T /F on
    timeout closes that. Re-raises TimeoutExpired after killing, same as
    subprocess.run() would have - every call site's existing try/except
    around this call needs no change."""
    proc = subprocess.Popen(
        args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=15)
        except Exception:
            pass
        proc.communicate()  # drain pipes so the now-dying process can fully exit
        raise
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)

RELAY_PROMPT = (
    "You are relay, a Symphony worker (scout archetype) for this cohort. "
    "Follow the routine in ingest/GRAPH_INGEST_ROUTINE.md exactly, steps 1-8 "
    "(read cursors, pull Teams chats capped at 3 per this wake, pull Calendar, "
    "pull SharePoint per the routine's derive-from-open-issues query scope "
    "(check the enabled flag first, it should be on), write the raw drop "
    "files in the envelope shapes the routine specifies, run normalize.py, "
    "emit the bus event, report your status). IMPORTANT: if any Teams or "
    "SharePoint call returns a 429/rate-limit error, STOP pulling that source "
    "immediately for this wake - do NOT wait out the retry-after and do NOT "
    "retry the same call. Move on to the remaining steps with whatever you "
    "already have. "
    "CRITICAL HONESTY REQUIREMENT (found violated 2026-07-29 - a prior headless "
    "run reported '5 chats, 81 messages, 25 events' in confident detail while "
    "writing zero files and advancing zero cursors): if teams_list_chats, "
    "outlook_calendar_search, sharepoint_search, or read_resource are missing, "
    "unavailable, or fail for ANY reason (including an auth/permission error "
    "you cannot resolve), STOP immediately and say so plainly in your final "
    "message - name exactly which tool call failed and how. Do NOT report a "
    "step as complete, do NOT state a file was written, and do NOT state a "
    "count of chats/messages/events pulled unless you have directly, actually "
    "called that tool and it returned real data this wake. A fabricated success "
    "report is a serious violation of this routine, worse than an honest "
    "failure - Marc would rather see 'relay failed, here is why' than a "
    "plausible-sounding summary that turns out to describe nothing that "
    "actually happened. Do not do anything else - no classification, no "
    "team_room posts beyond what the routine itself calls for."
)


SYNTHESIS_PROMPT = (
    "You are curator (Colleen), a Symphony worker (planner-analyst archetype) for this cohort, "
    "doing SYNTHESIS work this wake - not ingestion (relay's job) and not routine classification "
    "(your own classify pass already ran separately). Follow the routine in "
    "ingest/SYNTHESIS_ROUTINE.md exactly: run `python workgraph_synthesis.py --list-stale` to get "
    "your work list, then for each stale entity gather its prior synthesis (if any) and only the "
    "NEW evidence/raw_items since its previous marker - never re-read an entity's whole history. "
    "Extract any newly-seen raw_item that lacks a raw_item_extractions row yet (real judgment: "
    "asks/decisions/dates_mentioned/commitments/key_facts) via POST "
    "/api/workgraph/raw_items/{raw_item_id}/extraction, then write the updated synthesis via POST "
    "/api/workgraph/{entity_type}/{entity_id}/synthesis, INCLUDING duration estimates where the "
    "knowledge base actually supports one: each next_steps item may carry estimate_days_low/high + "
    "estimate_confidence (\"documented\"|\"model\"|\"unknown\") + estimate_note, and there is a "
    "top-level estimated_completion {note, confidence} summarizing the whole remaining timeline. "
    "Ground next_steps and every duration figure in "
    "$TEAM_DATA_DIR/documents/reference/sourcing_process_knowledge_base.md "
    "(the shared document library, readable by any worker) and "
    "map its own [DOCUMENTED]/[MARC'S MODEL]/[UNKNOWN] labels onto estimate_confidence exactly - "
    "never state a MARC'S MODEL estimate or an UNKNOWN gap as if it were documented Lilly policy. "
    "Omitting estimate_* fields (or the whole estimated_completion) is correct and expected when "
    "the knowledge base gives you nothing to ground even a model-tier guess in - never invent a "
    "number to fill the field. Do not do anything "
    "else this wake - no ingestion, no re-classification, no team_room posts beyond what the "
    "routine itself calls for."
)


PROJECT_GROUPING_PROMPT = (
    "You are curator (Colleen), a Symphony worker (planner-analyst archetype) for this cohort, "
    "judging weak-signal PROJECT-GROUPING suggestions this wake - not ingestion, not synthesis, not "
    "routine classification (each is a separate wake). Follow the routine in "
    "ingest/PROJECT_GROUPING_ROUTINE.md exactly: fetch GET /api/workgraph/project-suggestions, and "
    "for each pending pair read both issues' real content (GET /api/workgraph/issues/{issue_id} for "
    "each - evidence, synthesis, parties) before judging whether they're plausibly the same "
    "underlying deal or just coincidentally similar. Three verdicts, not two: confident same -> "
    "POST .../resolve {\"status\": \"confirmed\"} (this actually merges them now); confident "
    "unrelated -> POST .../resolve {\"status\": \"rejected\"}; genuinely unsure -> make no call at "
    "all and leave it pending. Abstaining is a correct, expected outcome for a real fraction of "
    "these - do not force a verdict on a pair you can't actually judge from the evidence given. Do "
    "not do anything else this wake - no ingestion, no synthesis, no re-classification, no "
    "team_room posts beyond what the routine itself calls for."
)


def _log(line: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S %Z')} {line}\n")


def run_relay_oneshot() -> dict:
    """Scoped, one-shot headless relay wake - exits after this one ingest
    pass, never left running unsupervised.

    `ok` is deliberately NOT the subprocess's exit code alone. Confirmed live
    2026-07-29: a headless run can exit 0 and report a confident, detailed,
    entirely FABRICATED success ("5 chats, 81 messages, 25 events") while
    writing zero files and advancing zero cursors - almost certainly because
    the interactive Microsoft 365 connector's auth doesn't reliably carry into
    a headless `claude -p` process, and nothing forced the model to notice
    and say so. The real check is whether the CALENDAR cursor moved: the
    routine's own step 3 sets it to THIS WAKE'S run timestamp unconditionally
    (GRAPH_INGEST_ROUTINE.md - "low volume, no pacing concern"), regardless of
    whether any new events exist. That makes it the one deterministic,
    code-verifiable proof relay's routine actually executed that far, wholly
    independent of anything relay claims about itself.

    Extended 2026-07-29: calendar alone is blind to a SharePoint-specific
    failure while Teams/Calendar succeed - and SharePoint had ZERO code-
    verifiable success signal of its own, meaning an auth failure or an
    accidentally-cleared 'enabled' flag could persist forever with nothing
    noticing. SharePoint's cursor gets the SAME unconditional
    this-wake's-timestamp update as calendar's, per the routine's own step 5
    ("only if enabled+ran") - so when the enabled flag is on, checking it
    the same way is just as reliable a proxy, at no extra cost."""
    calendar_cursor_before = ws.get_cursor("calendar", "default")
    sharepoint_enabled = ws.get_cursor("sharepoint", "enabled") == "1"
    sharepoint_cursor_before = ws.get_cursor("sharepoint", "default") if sharepoint_enabled else None

    env_prefix = {
        "SYMPHONY_WORKER": "relay",
        "TEAM_HOME": str(BODY),
        "TEAM_SCRIPTS_ROOT": str(BODY / "setup"),
        "TEAM_DATA_DIR": str(DATA_DIR),
        "COHORT_BASE": "http://localhost:8700",
        "TEAM_PORT": "8700",
    }
    import os
    env = os.environ.copy()
    env.update(env_prefix)
    try:
        proc = _run_headless_with_tree_kill(
            ["claude", "-p", RELAY_PROMPT, "--allowedTools", "Bash",
             "--add-dir", str(BODY), "--model", "claude-haiku-4-5-20251001"],
            cwd=str(BODY), env=env, timeout=900,
        )
        calendar_cursor_after = ws.get_cursor("calendar", "default")
        cursor_advanced = (calendar_cursor_after is not None
                            and calendar_cursor_after != calendar_cursor_before)

        sharepoint_advanced = True  # vacuously true when not enabled - nothing to check
        if sharepoint_enabled:
            sharepoint_cursor_after = ws.get_cursor("sharepoint", "default")
            sharepoint_advanced = (sharepoint_cursor_after is not None
                                    and sharepoint_cursor_after != sharepoint_cursor_before)

        return {
            "ok": proc.returncode == 0 and cursor_advanced and sharepoint_advanced,
            "returncode": proc.returncode,
            "cursor_advanced": cursor_advanced,
            "sharepoint_enabled": sharepoint_enabled,
            "sharepoint_advanced": sharepoint_advanced,
            "calendar_cursor_before": calendar_cursor_before,
            "calendar_cursor_after": calendar_cursor_after,
            "stdout_tail": proc.stdout[-1000:], "stderr_tail": proc.stderr[-1000:],
        }
    except Exception as e:
        return {"ok": False, "cursor_advanced": False, "error": str(e)}


def run_synthesis_oneshot() -> dict:
    """Scoped, one-shot headless curator wake for per-entity synthesis - same
    safety pattern as run_relay_oneshot() (SYMPHONY_WORKER pinned, scoped to
    Bash only, exits when done, never a persistent unsupervised agent).
    Skipped entirely (no subprocess spawned) when list_stale_entities() is
    already empty - no reason to wake a worker with nothing to do. Timeout is
    longer than relay's (900s): synthesizing several stale entities means
    real reading/extraction/writing work across possibly many raw_items, not
    a single mechanical ingest pass. No --model pin (unlike relay's haiku) -
    this is genuine judgment work (facts extraction + narrative synthesis),
    not mechanical ingestion, so it gets the CLI's default model rather than
    the cheapest one.

    list_stale_entities() is itself capped (DEFAULT_SYNTHESIS_LIMIT) and
    materiality-filtered - see workgraph_synthesis.py - so a backlog after
    being offline a while can't turn this one subprocess into an unbounded
    session. `deferred`/`skipped_immaterial` below report what that gate did
    this wake (never silently - just not acted on this cycle)."""
    stats: dict = {}
    stale = workgraph_synthesis.list_stale_entities(stats=stats)
    if not stale:
        return {"ok": True, "skipped": True, "reason": "no stale entities", **stats}

    env_prefix = {
        "SYMPHONY_WORKER": "curator",
        "TEAM_HOME": str(BODY),
        "TEAM_SCRIPTS_ROOT": str(BODY / "setup"),
        "TEAM_DATA_DIR": str(DATA_DIR),
        "COHORT_BASE": "http://localhost:8700",
        "TEAM_PORT": "8700",
    }
    import os
    env = os.environ.copy()
    env.update(env_prefix)
    try:
        proc = _run_headless_with_tree_kill(
            ["claude", "-p", SYNTHESIS_PROMPT, "--allowedTools", "Bash", "--add-dir", str(BODY)],
            cwd=str(BODY), env=env, timeout=1500,
        )
        return {"ok": proc.returncode == 0, "returncode": proc.returncode,
                "stale_count": len(stale), **stats,
                "stdout_tail": proc.stdout[-1000:], "stderr_tail": proc.stderr[-1000:]}
    except Exception as e:
        return {"ok": False, "stale_count": len(stale), **stats, "error": str(e)}


def run_project_grouping_oneshot() -> dict:
    """Scoped, one-shot headless curator wake to judge weak-signal project-
    suggestion residue (the pairs the deterministic auto-grouper - shared
    party/company/subject-core - couldn't resolve on its own). Same safety
    pattern as the other one-shots: skipped entirely when there are no
    pending suggestions, scoped to Bash only, exits when done. Runs AFTER
    classification (so this cycle's newly-created suggestions are in scope)
    and BEFORE synthesis (so synthesis operates on final, settled project
    boundaries rather than pre-merge ones)."""
    pending = ws.list_project_suggestions(status="pending")
    if not pending:
        return {"ok": True, "skipped": True, "reason": "no pending suggestions"}

    env_prefix = {
        "SYMPHONY_WORKER": "curator",
        "TEAM_HOME": str(BODY),
        "TEAM_SCRIPTS_ROOT": str(BODY / "setup"),
        "TEAM_DATA_DIR": str(DATA_DIR),
        "COHORT_BASE": "http://localhost:8700",
        "TEAM_PORT": "8700",
    }
    import os
    env = os.environ.copy()
    env.update(env_prefix)
    try:
        proc = _run_headless_with_tree_kill(
            ["claude", "-p", PROJECT_GROUPING_PROMPT, "--allowedTools", "Bash", "--add-dir", str(BODY)],
            cwd=str(BODY), env=env, timeout=1200,
        )
        return {"ok": proc.returncode == 0, "returncode": proc.returncode,
                "pending_count": len(pending),
                "stdout_tail": proc.stdout[-1000:], "stderr_tail": proc.stderr[-1000:]}
    except Exception as e:
        return {"ok": False, "pending_count": len(pending), "error": str(e)}


def run() -> dict:
    ws.init_workgraph()

    # 1. Mail - pure deterministic, no agent needed.
    try:
        mail_result = outlook_com_ingest.run()
    except Exception as e:
        mail_result = {"ok": False, "error": str(e)}

    # 2. First classify+NBA pass (covers whatever mail just brought in).
    classify_result_1 = workgraph_classify.run()
    nba_result_1 = workgraph_nba.recompute_all()
    alerts_result_1 = workgraph_alerts.run()

    # 3. Relay's one-shot for Teams/Calendar/SharePoint.
    relay_result = run_relay_oneshot()

    # 4. Second classify+NBA pass (covers whatever relay just normalized -
    #    relay's own routine only ingests+normalizes, it doesn't classify).
    classify_result_2 = workgraph_classify.run()
    nba_result_2 = workgraph_nba.recompute_all()
    alerts_result_2 = workgraph_alerts.run()

    # 5. Judge weak-signal project-suggestion residue - AFTER classification
    #    (this cycle's suggestions exist by now) and BEFORE synthesis (so
    #    synthesis groups issues by their final, settled project boundaries).
    grouping_result = run_project_grouping_oneshot()

    # 6. Synthesis, once per refresh cycle (not duplicated per classify pass
    #    like step 2/4 above - comparatively expensive, and doesn't need to
    #    run twice in one cycle the way classify does).
    synthesis_result = run_synthesis_oneshot()

    # 7. Retention + DB snapshotting - gated to once/day internally (see
    #    retention.run_daily_if_due), so calling it every one of the 5x/day
    #    cycles is safe; it's a no-op on 4 of them. Never lets a retention
    #    failure block the rest of an already-completed refresh cycle.
    try:
        retention_result = retention.run_daily_if_due()
    except Exception as e:
        retention_result = {"error": str(e)}

    # 8. Deterministic daily health check (task #41) - same once/day gate,
    # same never-block-the-rest-of-the-cycle guard. Report-only: this never
    # takes any corrective action itself, just surfaces findings.
    try:
        health_check_result = health_check.run_daily_if_due()
    except Exception as e:
        health_check_result = {"error": str(e)}

    # 9. Personal Response Learning, app-chat phase (task #45) - same once/day
    # gate, same never-block guard. Off by default (two config toggles inside
    # run_daily_if_due itself) - a no-op on every cycle until Marc turns it on.
    try:
        personal_learning_result = personal_patterns.run_daily_if_due()
    except Exception as e:
        personal_learning_result = {"error": str(e)}

    # 10. Aristotle candidate-rule detection (task #52) - same once/day gate,
    # same never-block guard. Only ever PROPOSES (pending_prerequisite_
    # suggestions) - never activates a rule on its own.
    try:
        aristotle_detection_result = workgraph_aristotle.detect_and_log_candidates_daily_if_due()
    except Exception as e:
        aristotle_detection_result = {"error": str(e)}

    # 11. Phase 0 fix (D2) - expire stale pending merge suggestions, once/day,
    # same never-block guard. Structural backstop against the queue
    # accumulating again regardless of the same_category_proximity_
    # suggestions_enabled flag's setting.
    try:
        suggestion_expiry_result = workgraph_projects.run_suggestion_expiry_daily_if_due()
    except Exception as e:
        suggestion_expiry_result = {"error": str(e)}

    # 12. Phase 0 fix (D12) - same once/day gate, expires stale 'offered'
    # nba_choice_log rows nothing ever resolved before.
    try:
        choice_log_expiry_result = workgraph_nba.run_choice_log_expiry_daily_if_due()
    except Exception as e:
        choice_log_expiry_result = {"error": str(e)}

    summary = {
        "mail": mail_result,
        "classify_after_mail": classify_result_1,
        "alerts_after_mail": alerts_result_1,
        "relay": relay_result,
        "classify_after_relay": classify_result_2,
        "nba_final": nba_result_2,
        "alerts_final": alerts_result_2,
        "project_grouping": grouping_result,
        "synthesis": synthesis_result,
        "retention": retention_result,
        "health_check": health_check_result,
        "personal_learning": personal_learning_result,
        "aristotle_detection": aristotle_detection_result,
        "suggestion_expiry": suggestion_expiry_result,
        "choice_log_expiry": choice_log_expiry_result,
    }
    _log(f"REFRESH ok mail_inserted={mail_result.get('inserted', '?')} "
        f"relay_ok={relay_result.get('ok')} relay_calendar_advanced={relay_result.get('cursor_advanced')} "
        f"relay_sharepoint_enabled={relay_result.get('sharepoint_enabled')} relay_sharepoint_advanced={relay_result.get('sharepoint_advanced')} "
        f"classified_total={classify_result_1.get('classify', {}).get('classified', 0) + classify_result_2.get('classify', {}).get('classified', 0)} "
        f"grouping_ok={grouping_result.get('ok')} grouping_skipped={grouping_result.get('skipped', False)} "
        f"synthesis_ok={synthesis_result.get('ok')} synthesis_skipped={synthesis_result.get('skipped', False)} "
        f"synthesis_deferred={synthesis_result.get('deferred', 0)} synthesis_skipped_immaterial={synthesis_result.get('skipped_immaterial', 0)} "
        f"retention_ran={retention_result is not None and 'error' not in retention_result} "
        f"health_check_ok={health_check_result.get('ok') if health_check_result else 'not-due'} "
        f"personal_learning_ran={personal_learning_result is not None and 'error' not in personal_learning_result} "
        f"aristotle_detection_ran={aristotle_detection_result is not None and 'error' not in aristotle_detection_result}")
    return summary


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))
