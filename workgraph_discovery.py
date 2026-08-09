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

Generalized system-table detection (task #266, 2026-08-07). contractpodai_
requests/ariba_requisitions (workgraph_store.py) were both built by hand:
Marc's own direct correction was that a field like "Request ID" means
something different depending on which SYSTEM produced it, so those two
got real per-system typed tables instead of generic data_point_
definitions rows. check_and_propose_system_table below generalizes the
RECOGNITION half of that judgment call - when a sender_domain crosses the
ordinary significance bar AND has 3+ genuinely co-occurring structured
labeled fields (_labels_cooccurring_with_domain), that shape (one
automated sender, several fields that always show up together) is what a
whole system's notification format looks like, not one more isolated
vocabulary field. It drafts a real proposal into proposed_system_tables
(sender_domain, system_name, suggested columns with sample values) for
human review via GET/POST /api/discovery/system-table-proposals.

Deliberately does NOT generalize the BUILD half. Confirming a proposal
never executes DDL, never writes a Python extraction function, and never
touches the live schema - it is a real go-ahead decision recorded for a
human/dev pass to actually implement, the same way ContractPodAI/Ariba
themselves were built. Auto-generating and auto-applying schema or
extraction code from an LLM's own characterization would be a materially
riskier class of automation than anything else in this module (every
other output here is a row in an already-generic, safe table); this
mechanism stops at the proposal, on purpose.
"""
from __future__ import annotations

import hashlib
import json
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
    """Hardened 2026-08-06 against two real adversarial cases found while
    testing this against deliberately malformed model output: (1) a
    literal "|" inside the description or rule text (e.g. an example
    value like "BN-1234 | Lot 9") used to break parsing entirely via a
    plain split("|") expecting exactly 4 parts - fixed with maxsplit=3, so
    only the first 3 pipes are treated as field separators and anything
    after the 3rd stays intact as the rule text. (2) a stray "PROPOSAL:
    NONE" line before a real one used to make this return None
    prematurely - fixed by continuing the scan past a NONE line instead
    of returning immediately, so a later real line still gets picked up."""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.upper().startswith("PROPOSAL:"):
            continue
        rest = line.split(":", 1)[1].strip()
        if rest.upper() == "NONE":
            continue
        parts = [p.strip() for p in rest.split("|", 3)]
        if len(parts) != 4:
            continue
        name, point_type, description, rule = parts
        point_type = point_type.lower()
        if point_type not in _POINT_TYPES or not name or not description:
            continue
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
    sweep uses).

    For a sender_domain: signature specifically, also tries the
    generalized system-table check (task #266) - non-exclusive with the
    per-field proposal path below: a domain can get both a system-table
    proposal AND individual field proposals for labels that happen to
    cross their OWN significance bar independently. Known, accepted gap:
    no dedup between the two yet if both fire for the same domain: a
    genuinely useful first cut, not a claim this is the final word on
    queue tidiness."""
    proposals = []
    for signature in sorted(set(signatures)):
        row = ws.get_candidate_pattern_observation(signature)
        if row is None or not crosses_significance_bar(row):
            continue
        samples = (
            _sample_raw_items_for_signature(signature, raw_items_pool) if raw_items_pool is not None
            else _raw_items_matching_signature(signature)
        )
        if signature.startswith("sender_domain:"):
            system_proposal = check_and_propose_system_table(signature, raw_items_pool=raw_items_pool)
            if system_proposal:
                proposals.append(system_proposal)
        proposal = propose_from_observation(row, sample_raw_items=samples, role_hint=role_hint)
        if proposal:
            proposals.append(proposal)
    return proposals


# --- generalized system-table detection (task #266) -------------------------

_SYSTEM_TABLE_MIN_DISTINCT_LABELS = 3  # Marc's own precedent, generalized:
# ContractPodAI (7 fields) and Ariba (3 fields: requester/descriptor/
# amount) both had several real structured fields recurring TOGETHER -
# a domain with only 1-2 co-occurring labels is still just "a couple of
# generic fields," not "a whole system's format," so it stays on the
# ordinary per-field data_point_definitions path instead.

_SYSTEM_TABLE_PROPOSAL_TIMEOUT_SECONDS = 90


def _labels_cooccurring_with_domain(
    domain: str, *, raw_items_pool: Optional[list[dict]] = None,
) -> dict[str, list[str]]:
    """Every distinct labeled_field label seen (with up to 3 real sample
    values each) across every raw_item actually sent from `domain` -
    the co-occurrence signal that distinguishes "one sender with several
    structured fields" from isolated single-field patterns, computed on
    demand from the same raw_items a per-field proposal would sample
    from rather than a separately-tracked table (nothing here is lost by
    not persisting it: this is cheap local text-regex work, no LLM call)."""
    matches = (
        [item for item in raw_items_pool if f"sender_domain:{domain}" in derive_pattern_signatures(item)]
        if raw_items_pool is not None else _raw_items_matching_signature(f"sender_domain:{domain}", limit=50)
    )
    labels: dict[str, list[str]] = {}
    for item in matches:
        text = text_extract.resolve_item_text(item)
        for label_match in _LABELED_FIELD_VALUE_RE.finditer(text):
            label = _normalize_label(label_match.group(1))
            if label in _BOILERPLATE_LABELS:
                continue
            value = label_match.group(2).strip()
            samples = labels.setdefault(label, [])
            if value and value not in samples and len(samples) < 3:
                samples.append(value)
    return labels


_SYSTEM_TABLE_PROPOSAL_PROMPT_TEMPLATE = """Real emails keep arriving from the domain "{domain}", and they consistently carry several structured labeled fields together - this looks like one automated system's notification format.

Fields seen, with real sample values:
{fields_block}

Real example emails from this domain:
{examples}

Decide: is this genuinely ONE coherent automated system worth its own dedicated tracking table (like a procurement/contract/ticketing/HR system's own structured notifications), or is it a coincidence of unrelated fields that happen to share a domain?

If it IS one coherent system, output:
SYSTEM: <short real name for this system, e.g. "Ariba" or "Workday" - infer from the domain/content, never invent a name unrelated to what's actually there>
Then one line per field actually worth tracking (skip any that are just noise), in exactly this format:
FIELD: <the exact label as given above> | <point_type> | <one-sentence description>

point_type must be exactly one of: entity, reference, amount, person, date, freetext (same meanings as: entity=named org/thing, reference=unique lookup ID, amount=dollar/numeric value, person=named individual, date=a date/deadline, freetext=real value with no fixed identity)

If this is NOT one coherent system, output exactly:
SYSTEM: NONE

Output nothing else.
"""


def _parse_system_table_proposal(stdout: str) -> Optional[dict]:
    """Same defensive line-scanning discipline as _parse_proposal - a
    stray SYSTEM: NONE before a real line must not short-circuit the
    scan, and a malformed FIELD line is skipped rather than aborting the
    whole parse."""
    system_name = None
    fields = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if line.upper().startswith("SYSTEM:"):
            value = line.split(":", 1)[1].strip()
            if value.upper() != "NONE":
                system_name = value
        elif line.upper().startswith("FIELD:") and system_name:
            parts = [p.strip() for p in line.split(":", 1)[1].split("|", 2)]
            if len(parts) != 3:
                continue
            label, point_type, description = parts
            point_type = point_type.lower()
            if point_type not in _POINT_TYPES or not label or not description:
                continue
            fields.append({"label": label, "point_type": point_type, "description": description})
    if system_name is None or not fields:
        return None
    return {"system_name": system_name, "fields": fields}


def check_and_propose_system_table(
    domain_signature: str, *, raw_items_pool: Optional[list[dict]] = None,
) -> Optional[dict]:
    """domain_signature is the full "sender_domain:x.com" signature string
    (the caller already has this from check_and_propose_for_signatures'
    own loop). Returns the created proposal, or None for every honest
    non-match: bar not crossed, too few co-occurring labels, already
    proposed for this domain, or the LLM itself decided it's not really
    one coherent system."""
    domain = domain_signature.split(":", 1)[1] if ":" in domain_signature else domain_signature
    row = ws.get_candidate_pattern_observation(domain_signature)
    if row is None or not crosses_significance_bar(row):
        return None
    if ws.get_system_table_proposal_by_domain(domain) is not None:
        return None

    labels = _labels_cooccurring_with_domain(domain, raw_items_pool=raw_items_pool)
    if len(labels) < _SYSTEM_TABLE_MIN_DISTINCT_LABELS:
        return None

    samples = (
        _sample_raw_items_for_signature(domain_signature, raw_items_pool) if raw_items_pool is not None
        else _raw_items_matching_signature(domain_signature)
    )
    if not samples:
        return None

    fields_block = "\n".join(
        f"- {label}: {', '.join(values) if values else '(no sample captured)'}"
        for label, values in sorted(labels.items())
    )
    prompt = _SYSTEM_TABLE_PROPOSAL_PROMPT_TEMPLATE.format(
        domain=domain, fields_block=fields_block, examples=_format_examples(samples),
    )
    try:
        proc = _run_headless_claude(prompt, timeout=_SYSTEM_TABLE_PROPOSAL_TIMEOUT_SECONDS, model=_PROPOSAL_MODEL)
    except subprocess.TimeoutExpired:
        return None
    parsed = _parse_system_table_proposal(proc.stdout)
    if parsed is None:
        return None

    suggested_columns = [
        {**field, "sample_values": labels.get(_normalize_label(field["label"]), [])}
        for field in parsed["fields"]
    ]
    proposal_id = f"systbl-{domain}".replace(".", "-").replace(":", "-")[:64]
    if ws.get_system_table_proposal(proposal_id) is not None:
        return None
    ws.create_system_table_proposal(
        id=proposal_id, sender_domain=domain, system_name=parsed["system_name"],
        suggested_columns_json=json.dumps(suggested_columns),
        sample_raw_item_ids_json=json.dumps([item["id"] for item in samples[:_MAX_EXAMPLES]]),
        status="proposed",
    )
    return ws.get_system_table_proposal(proposal_id)


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
    this design.

    Real bug fixed the first time this ever ran live (2026-08-08, task
    #249's own first scheduled fire): without passing raw_items_pool,
    every sender_domain signature's system-table check (task #266,
    check_and_propose_system_table) fell back to its own fresh 180-day
    _raw_items_matching_signature scan - TWICE per domain (once for
    _labels_cooccurring_with_domain, once for the actual sample-gathering)
    - turning what should be one corpus scan into O(domains x corpus).
    Against a real installation's accumulated signature history, that's
    the exact shape of a long, subprocess-silent hang (most domains never
    cross the label-count bar, so most of that time spends no LLM call
    at all - nothing to see in a process list, nothing in the log until
    it finally finishes). Building the pool ONCE here and passing it
    through is exactly what raw_items_pool's own docstring already says
    it's for - this sweep just never actually took the discount before
    today, and #266 made not taking it far more expensive."""
    all_signatures = [row["pattern_signature"] for row in ws.list_candidate_pattern_observations()]
    cutoff = time.time() - (180 * 86400)  # same window _raw_items_matching_signature's fallback path used
    raw_items_pool = ws.list_raw_items_since(cutoff)
    proposals = check_and_propose_for_signatures(all_signatures, raw_items_pool=raw_items_pool)

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


def run_monthly_sweep_if_due(now: float | None = None) -> dict | None:
    """Real schedule for run_monthly_sweep() (task #249) - same once-per-
    period atomic-claim gate every other periodic sweep in this codebase
    uses (ws.claim_daily_run), just keyed by a "YYYY-MM" string instead of
    a day - the gate itself is period-agnostic (a plain string-equality
    UPSERT, see claim_daily_run's own docstring), so reusing it for a
    monthly cadence needs no new mechanism or schema. Piggybacks the 5x/day
    scheduled_refresh cycle without redoing this sweep's real work (a scan
    of every candidate_pattern_observations row) on all but one call a
    month."""
    if now is None:
        now = time.time()
    month = time.strftime("%Y-%m", time.localtime(now))
    if not ws.claim_daily_run("discovery_monthly_sweep", month):
        return None
    return run_monthly_sweep()


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

If "dp-fasttrack-supplier" is one of the data points above AND you found a \
real company name for it, ALSO check whether a specific real email address \
for a sender/participant at that company is in the known participants or \
text above. If so, output one more line:
SUPPLIER_EMAIL: <email> | <company name>
Skip this line entirely if you can't confidently pair an email with the company.

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
        try:
            participants = _json.loads(item.get("participants") or "[]")
        except (TypeError, ValueError):
            participants = []
        # Found live 2026-08-08 (229 real raw_items rows): a single-
        # participant PowerShell array can serialize as a bare JSON
        # string rather than a 1-element array (ConvertTo-Json's own
        # single-item-array collapsing behavior) - this was crashing
        # workgraph_pipeline2's whole grouping batch on `list + str`
        # whenever any item in it carried one of these rows, not just
        # skipping the bad row.
        if isinstance(participants, str):
            participants = [participants]
        elif not isinstance(participants, list):
            participants = []
        for raw in [item.get("from_actor")] + participants:
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
        upper = line.upper()
        if upper.startswith("VALUE:"):
            rest = line.split(":", 1)[1].strip()
            parts2 = [p.strip() for p in rest.split("|", 1)]
            if len(parts2) != 2 or parts2[0] not in missing_ids or not parts2[1]:
                continue
            definition_id, value = parts2
            ws.record_data_point_value(definition_id=definition_id, work_object_id=work_object_id,
                                        value=value, extraction_source="llm_backfill")
            applied.append({"definition_id": definition_id, "value": value})
        elif upper.startswith("SUPPLIER_EMAIL:"):
            # Restores the retired llm_backfill_company's real effect: a
            # data_point_values row alone is invisible to workgraph_
            # projects._matched_data_points' EXISTING hardcoded "supplier"
            # check, which reads parties/external_orgs, not data_point_
            # values - found live while verifying #219 against the real
            # Kinaxis cluster (marc-714 etc. all had empty external_orgs
            # even after a successful data_point_values backfill). Same
            # narrow, non-destructive rule as before: only creates a party
            # where none exists yet for that email, never corrects one.
            rest = line.split(":", 1)[1].strip()
            parts2 = [p.strip() for p in rest.split("|", 1)]
            if len(parts2) != 2 or "@" not in parts2[0] or not parts2[1]:
                continue
            email, company = parts2[0].lower(), parts2[1]
            if ws.get_party_by_email(email) is not None:
                continue
            party_id = f"llm-{hashlib.sha256(email.encode()).hexdigest()[:12]}"
            ws.upsert_party(
                id=party_id, primary_email=email, display_name=None,
                affiliation="external", affiliation_confidence="L", affiliation_source="llm_backfill",
                company=company,
            )
            ws.link_party_to_issue(work_object_id, party_id)
            applied.append({"definition_id": "dp-fasttrack-supplier", "value": f"{email} ({company})",
                             "bridged_to_party": party_id})
    return applied
