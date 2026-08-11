"""
workgraph_sequences.py — task #322: mines recurring MULTI-STEP work
sequences ("request -> security review -> legal -> signature") across
closed/completed projects that share a category, and surfaces the best
match for a project's category as read-only, informational context.

Genuinely a different job than workgraph_aristotle.py, not a second copy of
it - read that module's own docstring first. Aristotle: PAIRWISE
correlations (does required signal X consistently precede trigger signal Y,
with ZERO exceptions), and once a person confirms a candidate it becomes an
ACTIVE, ENFORCED gate (workgraph_nba's has_unmet_prerequisite warning). This
module: sequences of THREE OR MORE stages at once, counted as "M of N
completed projects in this category showed this order" (majority signal,
not zero-exception), and NEVER promoted/confirmed/enforced - every row here
stays purely descriptive context, closer in spirit to personal_patterns.py's
citation_for_text ("you've referenced X N times before") than to a
prerequisite rule. The minimum-length-3 floor is deliberate: a length-2
finding is exactly Aristotle's own scope, and this module has no business
producing candidates that compete with or shadow that mechanism.

Signal source (researched before writing a line of detection logic):
workgraph_claims.py's claim_type is only 4 buckets (ask/decision/commitment/
date) and issue_state_history's to_state is only a handful of lifecycle
values (active/waiting/blocked/done/...) - both real, but too coarse to
ever produce a differentiated, category-specific STAGE NAME the way the
task's own example ("security review", "legal", "signature") implies.
raw_items.signal_type (workgraph_signals.py) is the one real, already-
recognized, deterministic vocabulary in this codebase that actually names
domain-specific stages (signature_requested, contractpodai_review_requested,
ariba_pr_fully_approved, ...) - the same field Aristotle already mines for
its own (different) purpose, confirmed via workgraph_signals.py and
workgraph_aristotle.detect_candidate_rules before reusing it here. Reusing
the same underlying field for a different analysis is not a conflict with
Aristotle's boundary; producing gate rules or auto-applying anything would
be.

Detection is fully deterministic (per this codebase's own stated
preference: no opaque scoring, prefer deterministic) - counting how many
real closed projects' own observed stage orderings contain a candidate
subsequence, nothing fuzzier. No LLM step was needed: plain order-preserving
subsequence counting, bounded to short (3-5 step) windows drawn only from
sequences that actually occurred in at least one real project, cleanly
answers "how many completed projects, in order, touched A then B then C" -
there was no real gap here that only a model could close.

Honest limitations (read before trusting this for anything beyond a nudge):
  - Only ever considers raw_items that got a real signal_type classification
    (workgraph_signals.py's finite, narrow rule table). A project whose
    real stages never triggered an automated-system notification - most
    plain email-only threads - contributes NOTHING to this analysis. This
    is deliberately not "every issue's lifecycle," only the automated-
    signal slice of it.
  - "Closed" here means project.status in ('done', 'archived') - a
    genuinely finished happy path. 'dismissed'/'noise-archived' projects
    are excluded on purpose: mining a "typical sequence" from killed work
    would misrepresent what a successful project actually looks like.
  - Order-preserving, not contiguous: a candidate sequence [A, B, C]
    matches a project whose real stage list was [A, X, B, Y, C] just as
    much as one that went [A, B, C] directly. This is a deliberate choice
    (an intervening, unrelated notification shouldn't break a real match)
    but it does mean "matched" is a looser claim than "happened back-to-
    back" - the note text says "have historically also involved," not
    "immediately followed by."
  - Small-N is real here: a single user's own history won't produce large
    sample sizes. MIN_MATCHING_PROJECTS=2 is a low bar, chosen so this
    produces SOMETHING real on realistic single-user data rather than
    staying empty forever - not a claim that 2 is statistically strong.
    Every stored row still carries its own real N/M and project ids so a
    reader can judge the strength themselves; nothing here hides behind an
    opaque score.
"""
from __future__ import annotations

import time
from itertools import combinations
from typing import Optional

import workgraph_store as ws

# 'done'/'archived' only - see module docstring for why 'dismissed' and
# 'noise-archived' projects are excluded from "completed happy path" mining.
_CLOSED_PROJECT_STATUSES = ("done", "archived")

# Multi-step floor: length-2 is Aristotle's own scope (see module docstring).
MIN_PATTERN_LEN = 3
# Bounds candidate generation to a real, legible chain length - long enough
# to show a genuine multi-stage story, short enough to stay something a
# reader can actually take in as one sentence.
MAX_PATTERN_LEN = 5
# "One coincidental case proves nothing" - same reasoning workgraph_
# aristotle.MIN_SAMPLE_GROUPS already gives for pairwise correlation,
# applied here to a multi-step chain instead.
MIN_MATCHING_PROJECTS = 2
# A category with fewer closed projects than this can't support an honest
# "M of N" statement at all - there's no N worth reporting yet.
MIN_PROJECTS_FOR_CATEGORY = 2


def stage_sequence_for_project(project_id: str) -> list[str]:
    """The real, observed stage sequence for one project: every distinct
    raw_items.signal_type across every issue under this project, ordered by
    each type's OWN FIRST occurrence (occurred_ts) - same "first ts per
    group" convention workgraph_aristotle._occurrences_by_group already
    uses. A signal_type that fires more than once (e.g. multiple Ariba
    approvers) contributes ONE stage token, not one per occurrence - this
    is a STAGE list, not a raw event log."""
    issues = ws.list_issues_for_project(project_id)
    issue_ids = [i["id"] for i in issues]
    if not issue_ids:
        return []
    raw_items_by_issue = ws.get_raw_items_for_issues(issue_ids)

    first_ts_by_type: dict[str, float] = {}
    for raw_items in raw_items_by_issue.values():
        for item in raw_items:
            signal_type = item.get("signal_type")
            ts = item.get("occurred_ts")
            if not signal_type or ts is None:
                continue
            if signal_type not in first_ts_by_type or ts < first_ts_by_type[signal_type]:
                first_ts_by_type[signal_type] = ts
    return [s for s, _ts in sorted(first_ts_by_type.items(), key=lambda kv: (kv[1], kv[0]))]


def _candidate_windows(stage_list: list[str]) -> set[tuple]:
    """Contiguous windows of length MIN_PATTERN_LEN..MAX_PATTERN_LEN drawn
    from ONE project's own real stage list - candidates are never invented
    combinations, only slices of something that genuinely occurred in at
    least one real project. Contiguous (not combinatorial) generation keeps
    this cheap and deterministic regardless of how many distinct signal
    types one project happens to have touched."""
    windows: set[tuple] = set()
    n = len(stage_list)
    for length in range(MIN_PATTERN_LEN, min(MAX_PATTERN_LEN, n) + 1):
        for start in range(0, n - length + 1):
            windows.add(tuple(stage_list[start:start + length]))
    return windows


def _is_order_preserving_subsequence(candidate: tuple, full: list[str]) -> bool:
    """True if `candidate`'s stages all appear in `full`, in the same
    relative order - not necessarily adjacent (see module docstring's
    "order-preserving, not contiguous" limitation)."""
    it = iter(full)
    return all(any(stage == token for stage in it) for token in candidate)


def detect_sequence_patterns_for_category(category: str, closed_projects: Optional[list[dict]] = None) -> list[dict]:
    """Pure detection for one category - no writes. `closed_projects` lets
    a caller (or a test) pass in an already-fetched/filtered project list;
    when omitted, reads ws.list_projects(status=_CLOSED_PROJECT_STATUSES)
    and filters to this category itself. Returns [] (never a guess from too
    little data) when fewer than MIN_PROJECTS_FOR_CATEGORY closed projects
    exist for this category.

    Algorithm: gather each closed project's own real stage_sequence_for_
    project; pool every contiguous window (length 3-5) that actually
    occurred in ANY one of them as a candidate; for each candidate, count
    how many DISTINCT projects contain it as an order-preserving
    subsequence of their own full stage list; keep candidates with at least
    MIN_MATCHING_PROJECTS matches. Subsumption: when two qualifying
    candidates have the EXACT SAME matching project set, only the longer
    (more specific/informative) one is kept - a shorter window that adds no
    distinguishing evidence beyond what a longer one already covers is
    noise, not a second real finding."""
    if closed_projects is None:
        closed_projects = [p for p in ws.list_projects(status=list(_CLOSED_PROJECT_STATUSES))
                            if p.get("category") == category]
    else:
        closed_projects = [p for p in closed_projects if p.get("category") == category]
    if len(closed_projects) < MIN_PROJECTS_FOR_CATEGORY:
        return []

    stage_lists = {p["id"]: stage_sequence_for_project(p["id"]) for p in closed_projects}
    total = len(closed_projects)

    candidates: set[tuple] = set()
    for stages in stage_lists.values():
        candidates |= _candidate_windows(stages)

    qualifying: list[dict] = []
    for candidate in candidates:
        matching_ids = sorted(
            pid for pid, stages in stage_lists.items() if _is_order_preserving_subsequence(candidate, stages)
        )
        if len(matching_ids) >= MIN_MATCHING_PROJECTS:
            qualifying.append({
                "step_sequence": list(candidate),
                "total_projects_in_category": total,
                "matching_project_count": len(matching_ids),
                "matching_project_ids": matching_ids,
            })

    # Subsumption filter: drop a pattern if a STRICTLY LONGER qualifying
    # pattern shares the exact same evidence set - keep the longer, more
    # informative one only. Two patterns with the same length are never
    # subsets of one another by construction (a window can't equal another
    # same-length window unless they're literally the same tuple), so this
    # only ever drops a shorter one in favor of a longer superset.
    by_evidence: dict[tuple, list[dict]] = {}
    for q in qualifying:
        by_evidence.setdefault(tuple(q["matching_project_ids"]), []).append(q)
    kept: list[dict] = []
    for group in by_evidence.values():
        max_len = max(len(q["step_sequence"]) for q in group)
        kept.extend(q for q in group if len(q["step_sequence"]) == max_len)

    kept.sort(key=lambda q: (-q["matching_project_count"], -len(q["step_sequence"]), q["step_sequence"]))
    return kept


def detect_sequence_patterns() -> dict[str, list[dict]]:
    """Every category with any closed project, mapped to its own detected
    patterns (possibly []). Real categories only - never invents one."""
    closed_projects = ws.list_projects(status=list(_CLOSED_PROJECT_STATUSES))
    categories = sorted({p.get("category") for p in closed_projects if p.get("category")})
    return {
        category: detect_sequence_patterns_for_category(category, closed_projects=closed_projects)
        for category in categories
    }


def recompute_and_store() -> dict:
    """Writes the current detection result for every real category,
    wholesale-replacing each category's stored rows (workgraph_store.
    replace_sequence_patterns) so a pattern that stops qualifying actually
    disappears, not just never-updated stale data. Returns a small summary,
    never raises on an empty result (an empty category list is real,
    honest output when there simply isn't enough closed-project history
    yet, not an error)."""
    by_category = detect_sequence_patterns()
    patterns_stored = 0
    for category, patterns in by_category.items():
        ws.replace_sequence_patterns(category, patterns)
        patterns_stored += len(patterns)
    return {"categories_scanned": len(by_category), "patterns_stored": patterns_stored}


def recompute_daily_if_due(now: Optional[float] = None) -> Optional[dict]:
    """Gate for scheduled_refresh.py - same once/day ingest_cursors pattern
    as retention/health_check/personal_learning/aristotle_detection
    (source='sequence_patterns_detection'). Returns None - a real 'did not
    run' result, never a silent no-op - when this already ran today."""
    if now is None:
        now = time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if not ws.claim_daily_run("sequence_patterns_detection", today):
        return None
    return recompute_and_store()


# --- the one consumer: predictive context for a project's own category ----

def top_pattern_for_category(category: Optional[str], exclude_project_id: Optional[str] = None) -> Optional[dict]:
    """The single strongest stored pattern for `category` (list_sequence_
    patterns is already ordered strongest-evidence-first), formatted as a
    read-only informational note - never a gate, never auto-applied.
    `exclude_project_id` drops that one project from the evidence count/list
    if present (the project currently being viewed should never cite itself
    as its own historical precedent); a pattern whose evidence would fall
    below MIN_MATCHING_PROJECTS after that exclusion is not returned - re-
    counted, not just filtered from display, so the note's own M/N numbers
    stay honest. Returns None when there's no category, or nothing stored
    for it yet (a real, common 'not enough history yet' case, not an
    error)."""
    if not category:
        return None
    for pattern in ws.list_sequence_patterns(category=category):
        matching_ids = [pid for pid in pattern["matching_project_ids"] if pid != exclude_project_id]
        if len(matching_ids) < MIN_MATCHING_PROJECTS:
            continue
        total = pattern["total_projects_in_category"]
        if exclude_project_id in pattern["matching_project_ids"]:
            total = max(total - 1, len(matching_ids))
        sequence_text = " → ".join(pattern["step_sequence"])
        return {
            "category": category,
            "step_sequence": pattern["step_sequence"],
            "matching_project_count": len(matching_ids),
            "total_projects_in_category": total,
            "matching_project_ids": matching_ids,
            "note": (f'Projects in "{category}" have historically also involved this sequence: '
                     f'{sequence_text} — seen in {len(matching_ids)} of {total} completed '
                     f'"{category}" projects so far.'),
        }
    return None
