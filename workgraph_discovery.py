"""
workgraph_discovery.py — personalized data-point discovery (design doc:
docs/design/PERSONALIZED_DATA_POINT_DISCOVERY.md, all open questions
resolved 2026-08-06). Two layers, matching the doc's §3 "two-tier
mechanism, not a single trigger":

  1. Deterministic, no-LLM-cost pattern observation (record_observations_
     for_item) - meant to run on EVERY new item as it's ingested, cheap
     enough to never gate on. Pure counting into candidate_pattern_
     observations/pattern_observation_threads.
  2. LLM-driven proposal drafting (propose_from_observation), triggered
     only once a pattern crosses the real significance bar Marc set: 5
     occurrences, across 2+ genuinely distinct threads, within a 60-day
     window. Never auto-activates anything - every proposal lands in
     data_point_definitions with status='proposed', requiring a real
     human confirm (server_lean.py's /api/discovery/* routes, task #214)
     before the retrofitted pipeline (matched_discovered_points below,
     wired into workgraph_projects._matched_data_points, #215/#216)
     ever reads it.

run_setup_discovery() is the one-time bulk form of (1)+(2) together, run
over a real historical window (90 days, Marc's number) for a fresh
installation. run_monthly_sweep() is the periodic complement (2) alone:
re-checks observations for anything the continuous per-item hook missed,
and separately flags confirmed data points that have gone stale (no real
match in a long stretch) for human review - never auto-removes anything.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workgraph_store as ws
import text_extract
import workgraph_parties

_SIGNIFICANCE_OCCURRENCE_MIN = 5
_SIGNIFICANCE_THREAD_MIN = 2
_SIGNIFICANCE_WINDOW_SECONDS = 60 * 86400  # Marc's own number: 5 occurrences
# across 2+ threads WITHIN 60 days, not ever. The observation row only
# tracks first_seen_ts/last_seen_ts (aggregate, not per-occurrence), so
# last_seen - first_seen <= 60d is the honest proxy available from this
# data shape - conservative in the direction Marc already chose (5 over
# 3): a pattern whose occurrences are spread thinner than 60 days doesn't
# cross the bar here even if it eventually would with per-occurrence
# timestamps, rather than risk the opposite (crossing too easily).

_VOCABULARY_CAP = 20  # Marc's own number (§3 of the design doc) - enforced
# at CONFIRM time (server_lean.py), not here - a proposal can still be
# drafted and queued for review past the cap, per the doc's own wording.

_STALENESS_SECONDS = 90 * 86400  # 3 months with zero real match - the
# job-change signal design doc §3 describes ("hasn't come up in months,
# still relevant?"). No hard-coded universal answer exists for "how
# stale is too stale"; 90 days mirrors the setup window itself as the
# most defensible default without inventing a new unrelated number.

# --- deterministic pattern-signature derivation (no LLM, cheap) -------------

# Generic "Label: value" line - the same shape Ariba/DocuSign-style
# structured notifications already use ("PR Number: PR1234567",
# "Requestor: Jane Doe"), but written to match ANY domain's own labeled
# fields, not just procurement's (a manufacturing "Batch Number:", a
# research "Protocol ID:", a finance "Cost Center:" all match the same
# shape) - this genericness is the whole point of this module existing.
_LABELED_FIELD_RE = re.compile(r"^([A-Z][A-Za-z0-9 /&_-]{2,40}):[ \t]+\S", re.MULTILINE)

# Same shape as _LABELED_FIELD_RE, but captures the VALUE too (group 2) -
# used by the retrofit (#216) to compare two items' actual values for a
# shared labeled field, not just detect the label's presence.
_LABELED_FIELD_VALUE_RE = re.compile(r"^([A-Z][A-Za-z0-9 /&_-]{2,40}):[ \t]+(\S.*?)\s*$", re.MULTILINE)

_EMAIL_DOMAIN_RE = re.compile(r"@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def _normalize_label(label: str) -> str:
    return re.sub(r"\s+", " ", label.strip().lower())


# Cheap, deterministic pre-filter - confirmed live against this
# installation's real corpus (2026-08-06, top-40 signature scan before
# this filter existed): the single most common "labeled_field" hit by far
# was "confidentiality notice" (782 occurrences - would have crossed the
# significance bar almost immediately), alongside email-signature/
# structural boilerplate ("email", "mobile", "phone", "subject", "date",
# "from", etc.) that's universal to email as a medium, not a genuine
# per-installation data point. Filtering these here (never even recorded
# as an observation) is strictly better than letting them reach the LLM
# proposal step and rejecting them there - saves a real LLM call once they
# inevitably cross the bar, AND keeps them out of the human-review queue
# entirely rather than showing up as an obvious "reject this" chore.
_BOILERPLATE_LABELS = frozenset({
    "confidentiality notice", "disclaimer", "truly human notice", "privacy policy",
    "terms of service", "unsubscribe",
    "email", "mobile", "phone", "fax", "cell", "tel", "telephone", "address",
    "from", "to", "cc", "bcc", "subject", "date", "sent", "sent by",
    "office", "note", "description", "reason",
    "upcoming out of office dates", "out of office", "away from office",
})


def derive_pattern_signatures(raw_item: dict) -> list[str]:
    """Every candidate pattern signature this ONE item exhibits - a sender
    domain and/or any labeled-field names found in its resolved text.
    Deduped within the item (a thread that mentions "PO Number:" three
    times in one message still counts as one occurrence of that pattern
    for THIS item, matching occurrence_count's own "per real recurrence,
    not per line" semantics)."""
    signatures: set[str] = set()

    from_actor = raw_item.get("from_actor") or ""
    m = _EMAIL_DOMAIN_RE.search(from_actor)
    if m:
        domain = m.group(1).lower()
        # The exact root internal domain (this installation's own employer,
        # e.g. "lilly.com") is on nearly every internal email and is never
        # a useful grouping signal on its own - confirmed live, 1567 of
        # 3174 raw_items. A genuine internal SUBDOMAIN (e.g.
        # "network.lilly.com" - the internal IT/network team) stays in;
        # it's narrow enough to represent a real, specific internal system.
        if domain != workgraph_parties.INTERNAL_DOMAIN:
            signatures.add(f"sender_domain:{domain}")

    text = text_extract.resolve_item_text(raw_item)
    for label_match in _LABELED_FIELD_RE.finditer(text):
        label = _normalize_label(label_match.group(1))
        if label in _BOILERPLATE_LABELS:
            continue
        signatures.add(f"labeled_field:{label}")

    return sorted(signatures)


def record_observations_for_item(raw_item: dict) -> list[dict]:
    """The continuous, always-on, no-LLM-cost tracker (design doc §3,
    mechanism 1) - call this once per newly-classified/linked raw_item.
    Returns the updated observation row for each signature this item
    exhibited, so a caller (the setup bulk pass, or a live per-item hook)
    can immediately check crosses_significance_bar without a second
    DB round trip."""
    thread_key = raw_item.get("thread_key") or raw_item.get("stable_key") or ""
    rows = []
    for signature in derive_pattern_signatures(raw_item):
        is_new_thread = ws.record_pattern_observation_thread(signature, thread_key) if thread_key else False
        rows.append(ws.observe_candidate_pattern(signature, is_new_thread=is_new_thread))
    return rows


def crosses_significance_bar(observation_row: dict) -> bool:
    """Marc's own real numbers (design doc §3): 5+ occurrences, 2+
    distinct threads, within a 60-day window. Already-promoted patterns
    (a proposal already drafted, still pending review) never cross again
    - re-proposing the same pattern while its first proposal sits in the
    review queue would just clutter that queue, not add real signal."""
    if observation_row.get("promoted_to_definition_id"):
        return False
    if observation_row["occurrence_count"] < _SIGNIFICANCE_OCCURRENCE_MIN:
        return False
    if observation_row["distinct_thread_count"] < _SIGNIFICANCE_THREAD_MIN:
        return False
    span = observation_row["last_seen_ts"] - observation_row["first_seen_ts"]
    return span <= _SIGNIFICANCE_WINDOW_SECONDS


# --- LLM-driven proposal drafting (only once a pattern is significant) -----

_PROPOSAL_TIMEOUT_SECONDS = 60
_PROPOSAL_MODEL = "haiku"  # a characterization/naming task, not a judgment
# call - same cost tier as llm_backfill_company (workgraph_pipeline2.py),
# not the heavier default model judge_candidate/curator synthesis use.
_MAX_EXAMPLE_CHARS = 2500
_MAX_EXAMPLES = 5

_POINT_TYPES = ("entity", "reference", "amount", "person", "date", "freetext")

_PROPOSAL_PROMPT_TEMPLATE = """A pattern has recurred {occurrence_count} times across {distinct_thread_count}+ distinct email threads in this person's real mailbox: "{signature}".

Here are up to {max_examples} real examples where this pattern appeared:

{examples}
{role_hint_line}
Decide whether this represents a genuine, useful DATA POINT worth tracking for grouping/matching related work together (e.g. a supplier name, a reference number, a dollar amount, a named contact, a deadline/date, or some other recurring structured or semi-structured value). Do NOT propose something that is just a greeting, a signature block, a disclaimer, or generic boilerplate.

If it IS a genuine data point, output exactly one line in this format:
PROPOSAL: <short user-facing name> | <point_type> | <one-sentence description> | <deterministic_rule or NONE>

point_type must be exactly one of: entity, reference, amount, person, date, freetext
- entity: a named organization/thing with a real tracked identity (e.g. a supplier)
- reference: a unique lookup ID that, if two messages share it, definitely means the same real-world thing (e.g. a PO number)
- amount: a dollar/numeric value
- person: a named individual
- date: a date or deadline
- freetext: a real but not identity-backed value (a product name, a topic)

deterministic_rule should be a short description of a reliable extraction rule if one clearly exists (e.g. "text after 'Batch Number:'"), or exactly NONE if the value only shows up as free-flowing prose that would need an LLM to extract reliably.

If this is NOT a genuine, useful data point, output exactly:
PROPOSAL: NONE

Output nothing else.
"""


def _format_examples(sample_raw_items: list[dict]) -> str:
    blocks = []
    for item in sample_raw_items[:_MAX_EXAMPLES]:
        subject = item.get("subject") or ""
        body = text_extract.resolve_item_text(item)[:_MAX_EXAMPLE_CHARS]
        blocks.append(f"---\nSubject: {subject}\n{body}")
    return "\n".join(blocks)


def _parse_proposal(stdout: str) -> Optional[dict]:
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.upper().startswith("PROPOSAL:"):
            continue
        rest = line.split(":", 1)[1].strip()
        if rest.upper() == "NONE":
            return None
        parts = [p.strip() for p in rest.split("|")]
        if len(parts) != 4:
            return None
        name, point_type, description, rule = parts
        point_type = point_type.lower()
        if point_type not in _POINT_TYPES or not name or not description:
            return None
        return {
            "name": name, "point_type": point_type, "description": description,
            "deterministic_rule": None if rule.upper() == "NONE" else rule,
        }
    return None


def _run_headless_claude(prompt: str, *, timeout: int, model: Optional[str] = None) -> subprocess.CompletedProcess:
    """Same self-contained subprocess-safety primitive as workgraph_
    pipeline2._run_headless_claude (CREATE_NEW_PROCESS_GROUP + taskkill
    /T /F on timeout) - deliberately a separate copy, not an import,
    matching Marc's own standing instruction for this pipeline family
    ("build new mechanisms for it, keep it entirely separate")."""
    env = os.environ.copy()
    args = ["claude", "-p", prompt, "--allowedTools", ""]
    if model:
        args += ["--model", model]
    proc = subprocess.Popen(
        args, cwd=str(Path(__file__).resolve().parent), env=env,
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


def propose_from_observation(
    observation_row: dict, *, sample_raw_items: list[dict], role_hint: Optional[str] = None,
) -> Optional[dict]:
    """One real LLM call, only once crosses_significance_bar is already
    True (the caller's job to check first - this function trusts it was
    called correctly, same discipline as workgraph_pipeline2.judge_
    candidate trusting its own caller's 2+-point gate). Drafts a real
    'proposed' data_point_definitions row grounded in the actual real
    examples that surfaced it (discovered_from), never auto-confirmed."""
    if not sample_raw_items:
        return None
    role_hint_line = f'\nThe person who owns this mailbox described their role as: "{role_hint}"\n' if role_hint else ""
    prompt = _PROPOSAL_PROMPT_TEMPLATE.format(
        occurrence_count=observation_row["occurrence_count"],
        distinct_thread_count=observation_row["distinct_thread_count"],
        signature=observation_row["pattern_signature"],
        max_examples=min(_MAX_EXAMPLES, len(sample_raw_items)),
        examples=_format_examples(sample_raw_items),
        role_hint_line=role_hint_line,
    )
    try:
        proc = _run_headless_claude(prompt, timeout=_PROPOSAL_TIMEOUT_SECONDS, model=_PROPOSAL_MODEL)
    except subprocess.TimeoutExpired:
        return None
    parsed = _parse_proposal(proc.stdout)
    if parsed is None:
        return None

    definition_id = f"dp-{observation_row['pattern_signature']}".replace(":", "-").replace(" ", "_")[:64]
    if ws.get_data_point_definition(definition_id) is not None:
        return None  # already proposed under this id - don't duplicate
    discovered_from = "; ".join(
        f"raw_item#{item['id']}" for item in sample_raw_items[:_MAX_EXAMPLES]
    )
    ws.create_data_point_definition(
        id=definition_id, name=parsed["name"], description=parsed["description"],
        point_type=parsed["point_type"], deterministic_rule=parsed["deterministic_rule"],
        discovered_from=discovered_from, status="proposed",
    )
    ws.mark_candidate_pattern_promoted(observation_row["pattern_signature"], definition_id)
    return ws.get_data_point_definition(definition_id)


# --- bulk setup pass + monthly sweep ----------------------------------------

def _sample_raw_items_for_signature(signature: str, raw_items: list[dict], limit: int = _MAX_EXAMPLES) -> list[dict]:
    matches = [item for item in raw_items if signature in derive_pattern_signatures(item)]
    return matches[:limit]


def check_and_propose_for_signatures(
    signatures, *, raw_items_pool: Optional[list[dict]] = None, role_hint: Optional[str] = None,
) -> list[dict]:
    """Shared significance-check + LLM-proposal step, factored out so both
    the live per-item hook (workgraph_classify.py, called once per BATCH
    with only that batch's touched signatures - never per item, since an
    LLM call is not cheap enough to run on every single classified email)
    and the setup bulk pass (below, over a whole 90-day window at once)
    go through identical logic. raw_items_pool lets a caller that already
    has the relevant raw_items in memory (a classify batch, a setup scan)
    avoid a redundant DB read for sample-gathering; omitted, this falls
    back to a fresh 180-day lookup per signature (same as the monthly
    sweep uses)."""
    proposals = []
    for signature in sorted(set(signatures)):
        row = ws.get_candidate_pattern_observation(signature)
        if row is None or not crosses_significance_bar(row):
            continue
        samples = (
            _sample_raw_items_for_signature(signature, raw_items_pool) if raw_items_pool is not None
            else _raw_items_matching_signature(signature)
        )
        proposal = propose_from_observation(row, sample_raw_items=samples, role_hint=role_hint)
        if proposal:
            proposals.append(proposal)
    return proposals


def run_setup_discovery(*, role_hint: Optional[str] = None, window_days: int = 90) -> dict:
    """The one-time bulk pass over a fresh (or freshly-requested) window
    of real mail (design doc §2, window locked at 90 days). Scans every
    item in the window through the SAME deterministic tracker the
    continuous per-item hook uses (no separate "setup-only" counting
    logic to maintain), then immediately checks every touched pattern
    against the significance bar and drafts real proposals for whichever
    already cross it - so setup doesn't just silently wait another 60
    days for a pattern that already had enough real history sitting in
    the backlog. role_hint (freeform text, §7.2) is passed straight
    through as extra context to the LLM's characterization call - it
    never gates or pre-filters which patterns get checked; every
    candidate is tested against real recurrence exactly the same way with
    or without a role hint."""
    cutoff = time.time() - (window_days * 86400)
    raw_items = ws.list_raw_items_since(cutoff)

    touched_signatures: set[str] = set()
    for item in raw_items:
        for row in record_observations_for_item(item):
            touched_signatures.add(row["pattern_signature"])

    proposals = check_and_propose_for_signatures(touched_signatures, raw_items_pool=raw_items, role_hint=role_hint)

    return {
        "items_scanned": len(raw_items), "patterns_touched": len(touched_signatures),
        "proposals_drafted": [p["id"] for p in proposals],
    }


def run_monthly_sweep() -> dict:
    """Design doc §3's periodic complement - NOT a repeat of the
    continuous tracker's job. Two things only the monthly sweep does:
    (a) catch any pattern that crossed the significance bar without ever
    getting checked (e.g. the live per-item hook was down, or missed a
    batch) by re-scanning every observation row already in the table;
    (b) flag confirmed data points that have gone stale - no real match
    in _STALENESS_SECONDS - for human review. Proposes/flags only, never
    auto-commits or auto-removes anything, same as everywhere else in
    this design."""
    all_signatures = [row["pattern_signature"] for row in ws.list_candidate_pattern_observations()]
    proposals = check_and_propose_for_signatures(all_signatures)

    now = time.time()
    stale = [
        d for d in ws.list_data_point_definitions(status="confirmed")
        if (d.get("last_matched_ts") is None and (now - d["created_ts"]) > _STALENESS_SECONDS)
        or (d.get("last_matched_ts") is not None and (now - d["last_matched_ts"]) > _STALENESS_SECONDS)
    ]

    return {
        "proposals_drafted": [p["id"] for p in proposals],
        "stale_definition_ids": [d["id"] for d in stale],
    }


def _raw_items_matching_signature(signature: str, limit: int = _MAX_EXAMPLES) -> list[dict]:
    """Monthly sweep's sample-gathering path - unlike the setup bulk pass
    (which already has the whole window's raw_items in memory), this has
    to go find real examples for a signature that may have been observed
    long before this sweep run. Scoped to a reasonable recent slice
    (last 180 days) rather than the whole corpus, since this only needs
    a handful of representative examples, not exhaustive recall."""
    cutoff = time.time() - (180 * 86400)
    matches = []
    for item in ws.list_raw_items_since(cutoff):
        if signature in derive_pattern_signatures(item):
            matches.append(item)
            if len(matches) >= limit:
                break
    return matches


# --- fast-track: Marc's own already-proven vocabulary (task #217) ----------

# Design doc §7.3: re-discovering fields already proven correct across 90+
# days of real use would be pure cost with no signal - these become this
# installation's initial CONFIRMED vocabulary directly, the one legitimate
# bypass of the propose-then-confirm gate. Each maps one of today's
# hardcoded content categories (workgraph_projects._matched_data_points'
# own docstring: reference/supplier/stakeholder/product_service/amount/
# document/subject_entity) onto the new 6-type structural taxonomy - see
# §7.4 for the real mapping evidence. `document` (shared attachment-hash
# lineage, artifact_lineages) deliberately has no row here - it isn't a
# discovered "value" at all (§7.4), so it isn't part of this vocabulary.
_FASTTRACK_DEFINITIONS = [
    {
        "id": "dp-fasttrack-supplier", "name": "Supplier / external company", "point_type": "entity",
        "description": "The external organization a thread is about - tracked via parties.company "
                        "(workgraph_parties.classify_affiliation).",
        "deterministic_rule": "parties table: company field on an external-affiliation party linked to the issue",
    },
    {
        "id": "dp-fasttrack-stakeholder", "name": "Stakeholder / named contact", "point_type": "person",
        "description": "A named internal or external person genuinely involved in a thread - tracked via "
                        "parties.display_name and issue_parties.",
        "deterministic_rule": "parties/issue_parties tables (workgraph_parties.extract_and_link_parties_for_issue)",
    },
    {
        "id": "dp-fasttrack-reference", "name": "PR/PO reference number", "point_type": "reference",
        "description": "A definitive, unique transaction identifier (Ariba-style Purchase Requisition/Order "
                        "number) - two threads sharing this number are the same real transaction.",
        # Literal text of workgraph_signals.REFERENCE_ID_RE - cited here for
        # audit/display, not re-compiled (the real pipeline still applies
        # the actual compiled pattern directly, see #215's retrofit).
        "deterministic_rule": r"\b(?:PR|PO)\d{4,}(?:-V\d+)?\b",
    },
    {
        "id": "dp-fasttrack-amount", "name": "Dollar amount", "point_type": "amount",
        "description": "A real dollar figure mentioned in a thread - the deal/contract/PR value.",
        "deterministic_rule": "workgraph_nba._extract_value_amount's currency-pattern scan",
    },
    {
        "id": "dp-fasttrack-product-service", "name": "Product/service description", "point_type": "freetext",
        "description": "An Ariba-extracted product or service description - real content, but not "
                        "identity-backed the way a tracked party or reference number is.",
        "deterministic_rule": None,
    },
    {
        "id": "dp-fasttrack-subject-entity", "name": "Subject/topic core", "point_type": "freetext",
        "description": "The normalized, boilerplate-stripped core of a thread's subject line - a soft "
                        "content-overlap signal, never auto-merge-worthy alone.",
        "deterministic_rule": None,
    },
    {
        "id": "dp-fasttrack-deadline", "name": "Deadline / mentioned date", "point_type": "date",
        "description": "A date or deadline mentioned in a thread, already classified hard (a real, binding "
                        "date with a named consequence) or soft (aspirational) by curator's synthesis routine.",
        "deterministic_rule": "raw_item_extractions.dates_mentioned + workgraph_deadlines.py's hard/soft classification",
    },
]


def seed_fasttrack_vocabulary(*, confirmed_by: str = "marc") -> list[str]:
    """Task #217 - creates each of Marc's own already-proven procurement
    fields directly as status='confirmed' data_point_definitions rows (the
    one legitimate bypass of the normal propose-then-confirm gate - see
    module docstring above). Idempotent: skips any id that already exists,
    safe to call more than once (e.g. re-run after a fresh install of this
    same codebase, not just this literal database)."""
    created = []
    for d in _FASTTRACK_DEFINITIONS:
        if ws.get_data_point_definition(d["id"]) is not None:
            continue
        ws.create_data_point_definition(
            id=d["id"], name=d["name"], description=d["description"], point_type=d["point_type"],
            deterministic_rule=d["deterministic_rule"], discovered_from="fast-tracked from existing hardcoded logic",
            status="confirmed",
        )
        ws.confirm_data_point_definition(d["id"], confirmed_by=confirmed_by)
        created.append(d["id"])
    return created


# --- retrofit: wiring discovered vocabulary into grouping (#215/#216) ------

_FASTTRACK_PREFIX = "dp-fasttrack-"


def _extract_labeled_field_value(text: str, label: str) -> Optional[str]:
    for m in _LABELED_FIELD_VALUE_RE.finditer(text):
        if _normalize_label(m.group(1)) == label:
            return m.group(2).strip()
    return None


def value_for_signature(signature: str, raw_item: dict) -> Optional[str]:
    """The real, comparable VALUE this ONE item carries for a given
    pattern signature, or None if the item doesn't actually exhibit it.
    sender_domain's "value" is just the domain itself (two items from the
    same vendor domain trivially share a value - real signal for a vendor
    the existing party/company extraction doesn't recognize yet);
    labeled_field's value is whatever real text follows the label on that
    specific item."""
    if signature.startswith("sender_domain:"):
        from_actor = raw_item.get("from_actor") or ""
        m = _EMAIL_DOMAIN_RE.search(from_actor)
        domain = m.group(1).lower() if m else None
        return domain if domain and domain == signature.split(":", 1)[1] else None
    if signature.startswith("labeled_field:"):
        label = signature.split(":", 1)[1]
        return _extract_labeled_field_value(text_extract.resolve_item_text(raw_item), label)
    return None


def matched_discovered_points(work_object_id_a: str, work_object_id_b: str) -> list[str]:
    """Task #215/#216's real retrofit into grouping - purely ADDITIVE to
    workgraph_projects._matched_data_points' existing, proven hardcoded
    checks (never replaces or touches them - Marc's own fast-tracked
    fields, task #217, stay exactly as they already work today). Every
    CONFIRMED, non-fast-tracked data point (one that actually went through
    real discovery + human confirm, not one of the 7 seeded directly) gets
    a real chance to contribute a matched point here.

    Short-circuits to [] immediately, with zero raw-text reads, whenever
    no such definitions exist yet (today's real state, before Marc has
    confirmed his first genuine discovery) - a forward-looking capability
    wired all the way through, not dead weight on every grouping call.

    Known, accepted limitation: record_data_point_value is called on
    every pairwise match this fires for, so a data point matching across
    many candidate pairs in one grouping pass can end up with duplicate
    rows for the same (definition, work_object) pair - acceptable for a
    first build since data_point_values is an audit/trust-tracking trail,
    not a uniqueness-constrained ledger; real dedup would be a reasonable
    follow-up once this is seeing real confirmed non-fast-track data."""
    non_fasttrack = [
        d for d in ws.list_data_point_definitions(status="confirmed")
        if not d["id"].startswith(_FASTTRACK_PREFIX)
    ]
    if not non_fasttrack:
        return []

    items_a = ws.get_raw_items_for_issue(work_object_id_a)
    items_b = ws.get_raw_items_for_issue(work_object_id_b)
    if not items_a or not items_b:
        return []

    points = []
    for definition in non_fasttrack:
        signature = ws.get_pattern_signature_for_definition(definition["id"])
        if not signature:
            continue
        values_a = {v for v in (value_for_signature(signature, item) for item in items_a) if v}
        values_b = {v for v in (value_for_signature(signature, item) for item in items_b) if v}
        shared = values_a & values_b
        if not shared:
            continue
        value = next(iter(shared))
        ws.record_data_point_value(definition_id=definition["id"], work_object_id=work_object_id_a,
                                    value=value, extraction_source="deterministic")
        ws.record_data_point_value(definition_id=definition["id"], work_object_id=work_object_id_b,
                                    value=value, extraction_source="deterministic")
        points.append(f"discovered:{definition['id']}")
    return points


# --- tier 2: Haiku backfill for confirmed data points the deterministic
# tier left empty (design doc §5.2) -------------------------------------

_BACKFILL_TIMEOUT_SECONDS = 60
_BACKFILL_MODEL = "haiku"

_BACKFILL_PROMPT_TEMPLATE = """Read this real business communication.

KNOWN PARTICIPANTS (real email addresses/names already seen on this thread - \
use these when relevant, never invent an email address that isn't in this list \
or in the text below):
{participants}

TEXT:
{text}

For EACH of the following data points, try to find its real value in the text \
or known participants above. Do not guess or invent a value - if you cannot \
find a confident, real value, skip it entirely.

{field_list}

For each one you CAN confidently fill, output one line in exactly this format:
VALUE: <data point id> | <value>

Output nothing else - no preamble, no explanation, skip any you can't fill.
"""


def _known_participants_text(raw_items: list[dict]) -> str:
    """The exact fix for the concrete bug found live 2026-08-06: the OLD
    single-field company backfill asked Haiku for an email address that
    was never actually in resolve_item_text's output (that function
    returns body text, not headers) - Haiku correctly said "none" every
    time. from_actor/participants ARE the real, already-known addresses;
    handing them over as structured input means Haiku is never asked to
    invent what it can't see."""
    import json as _json
    seen = set()
    lines = []
    for item in raw_items:
        for raw in [item.get("from_actor")] + _json.loads(item.get("participants") or "[]"):
            raw = (raw or "").strip()
            if raw and raw not in seen:
                seen.add(raw)
                lines.append(f"- {raw}")
    return "\n".join(lines) if lines else "(none known)"


def llm_backfill_missing_values(work_object_id: str) -> list[dict]:
    """Task #215's tier 2 - genuinely PLURAL (every confirmed data point
    missing a value for this work object, one Haiku call, not the single-
    field version built and reverted earlier tonight). Only ever reads
    CONFIRMED definitions (never 'proposed' - the human-confirm gate is
    absolute, workgraph_lessons-style discipline applies here too).
    Writes results tagged extraction_source='llm_backfill', auditable and
    reversible, never indistinguishable from a deterministic hit."""
    items = ws.get_raw_items_for_issue(work_object_id)
    if not items:
        return []

    confirmed = ws.list_data_point_definitions(status="confirmed")
    already_have = {v["definition_id"] for v in ws.list_data_point_values_for_work_object(work_object_id)}
    missing = [d for d in confirmed if d["id"] not in already_have]
    if not missing:
        return []

    parts = []
    for item in items:
        subject = item.get("subject") or ""
        body = text_extract.resolve_item_text(item)
        parts.append(f"Subject: {subject}\n{body}")
    text = "\n\n---\n\n".join(parts)[:_MAX_EXAMPLE_CHARS * 3]
    if not text.strip():
        return []

    field_list = "\n".join(f"- {d['id']}: {d['name']} ({d['description']})" for d in missing)
    prompt = _BACKFILL_PROMPT_TEMPLATE.format(
        participants=_known_participants_text(items), text=text, field_list=field_list,
    )
    try:
        proc = _run_headless_claude(prompt, timeout=_BACKFILL_TIMEOUT_SECONDS, model=_BACKFILL_MODEL)
    except subprocess.TimeoutExpired:
        return []

    missing_ids = {d["id"] for d in missing}
    applied = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.upper().startswith("VALUE:"):
            continue
        rest = line.split(":", 1)[1].strip()
        parts2 = [p.strip() for p in rest.split("|", 1)]
        if len(parts2) != 2 or parts2[0] not in missing_ids or not parts2[1]:
            continue
        definition_id, value = parts2
        ws.record_data_point_value(definition_id=definition_id, work_object_id=work_object_id,
                                    value=value, extraction_source="llm_backfill")
        applied.append({"definition_id": definition_id, "value": value})
    return applied
