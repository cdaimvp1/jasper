"""
workgraph_claims.py — Phase 3 (design doc Section 9): materializes the
ask/decision/commitment/date fields already sitting in
raw_item_extractions.extracted_json into real, typed, deduped,
actor-attributed rows in the `claims` table.

This is NOT a new extraction pass - curator's existing extraction (computed
once per raw_item, never re-extracted, per SYNTHESIS_ROUTINE.md) is the only
source of truth read here. materialize_claims_for_raw_item is the one
function that turns that blob into first-class rows; everything else in this
module is a reader.

Actor resolution (Section 9.4) is deterministic, never a keyword guess -
workgraph_commitments.py already found that only 5/79 real commitments even
mention Marc by name, so `author` comes from raw_items.direction instead:
outbound -> marc, inbound -> counterparty, internal/unknown -> unknown.
`owner` (who the resulting obligation falls on) is then DERIVED from
author + claim_type, not a second judgment:
    commitment -> owner = author        (the speaker owns doing it)
    ask        -> owner = other side    (an ask puts the obligation on the
                                          party being asked, not the asker)
    decision   -> owner = None          (a joint fact, not an obligation)
    date       -> owner = extraction's own 'whose' judgment (Section 9.7,
                                          task #57) - direction can't tell
                                          you whose deadline a sentence is
                                          about, only who happened to type it

Dedup (Section 9.3): consumes curator's own repeat_signals judgment (already
real, previously only displayed) rather than rebuilding text-similarity
matching. Applies to ask/decision/commitment claims alike (widened from
asks-only, a real gap found while reading the current extraction contract) -
any claim whose text repeat_signals names as a restatement of an existing
OPEN claim of the same type updates that claim in place instead of
inserting a second one.

Canonical-key dedup (2026-08-04, architecture-review follow-up P1): a real
fallback for when repeat_signals' byte-exact match fails, but the claim is
still genuinely a restatement - confirmed live: the SAME Ariba PR reminder
re-sent with slightly different wording around an identical reference ID
(PR1161567/PR1170816/PR1169904/PR854779-V4 all real live duplicate groups).
See canonical_key_for_claim's own docstring for the exact rule (structured
reference preferred; conservative text normalization only as a fallback;
deliberately no fuzzy/embedding similarity anywhere).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

_DOLLAR_RE = re.compile(r"\$\s?[\d,]+(?:\.\d+)?")


def _classify_refinement(old_claim: dict, new_spec: dict) -> str:
    """Fix 3 (task #310 follow-up, 2026-08-11, doc Section 7): which
    reconciliation sub-type a superseded-and-replaced claim represents.
    Only ever called on a genuinely unambiguous 1-old/1-new pairing (see
    _reconcile_extraction_correction) - never a guess among candidates,
    just a real diff between the one old claim and the one new spec that
    replaced it.

    owner_changed beats timing_changed beats monetary_changed beats the
    generic 'refined' fallback - a real change in who owns an obligation
    is the most consequential single fact to flag, ahead of a date or
    dollar-figure correction on the same claim."""
    if old_claim.get("owner") != new_spec.get("owner") and (old_claim.get("owner") or new_spec.get("owner")):
        return "owner_changed"
    if old_claim.get("claim_type") == "date":
        return "timing_changed"
    old_amounts = set(_DOLLAR_RE.findall(old_claim.get("text") or ""))
    new_amounts = set(_DOLLAR_RE.findall(new_spec.get("text") or ""))
    if old_amounts and new_amounts and old_amounts != new_amounts:
        return "monetary_changed"
    return "refined"

import workgraph_store as ws
import workgraph_lifecycle

_OTHER_SIDE = {"marc": "counterparty", "counterparty": "marc", "unknown": "unknown"}

# Finite action-family keyword table (2026-08-04) - deliberately small and
# closed, not a learned/fuzzy classifier: these are the verbs that actually
# recur across real procurement correspondence (approve/sign/pay a PO,
# review/confirm/respond-to a document, send a deliverable). Word-stem
# patterns, not bare substrings - "approval"/"approved"/"approving" must
# all match the SAME "approve" family as "approve" itself, or two real
# messages about the identical PR ("please approve PR1161567" vs "PR1161567
# still needs approval") would get DIFFERENT canonical keys and silently
# fail to dedup, defeating the whole point of preferring the structured-
# reference tier. A claim whose text matches none of these still gets a
# canonical_key (via the reference-only or conservative-normalization
# paths below) - this only sharpens the structured-reference key when a
# real action word is present.
_ACTION_FAMILY_PATTERNS = (
    ("approve", re.compile(r"\bapprov\w*\b")),
    ("review", re.compile(r"\breview\w*\b")),
    ("sign", re.compile(r"\bsign\w*\b")),
    ("send", re.compile(r"\bsen[dt]\w*\b")),
    ("pay", re.compile(r"\bpa(?:y|id|ying)\w*\b")),
    ("respond", re.compile(r"\brespon[ds]\w*\b")),
    ("confirm", re.compile(r"\bconfirm\w*\b")),
)


def _action_family_for_text(text: str) -> Optional[str]:
    lowered = text.lower()
    for family, pattern in _ACTION_FAMILY_PATTERNS:
        if pattern.search(lowered):
            return family
    return None


# Conservative boilerplate the normalization path strips - ONLY greeting/
# sign-off/reminder-framing phrases, never a word that could carry real
# content (negation, numbers, dates, amounts, supplier names, and the
# actual ask/decision/commitment text all survive this untouched).
_BOILERPLATE_RE = re.compile(
    r"\b(please\s+(?:be\s+)?(?:advised|note|find)|kind(?:ly)?\s+(?:note|reminder)|"
    r"this\s+is\s+a\s+(?:friendly\s+)?reminder|reminder\s*:?|"
    r"dear\s+\w+|hi\s+\w+|hello\s+\w+|thanks?(?:\s+(?:you|so\s+much))?|"
    r"regards|best\s+regards|sincerely)\b",
    re.IGNORECASE,
)
# Keeps word characters, whitespace, and the handful of symbols that carry
# real meaning in this domain - $/% for amounts, . and - for dates/decimals/
# negative numbers. Every other punctuation mark collapses to a space.
_NORMALIZE_STRIP_RE = re.compile(r"[^\w\s.$%-]")
_WHITESPACE_RE = re.compile(r"\s+")
# Below this length a normalized string is too generic to trust as a dedup
# key on its own (e.g. "ok" or "yes") - same trust-floor discipline as
# workgraph_projects.MIN_TOPIC_KEY_LEN for topic-key matching.
_MIN_NORMALIZED_LEN = 8


_SENTENCE_ENDING_PERIOD_RE = re.compile(r"\.(?=\s|$)")


def _conservative_normalize(text: str) -> str:
    """Unicode-normalize, lowercase, strip ONLY greeting/reminder
    boilerplate phrases and punctuation - never a fuzzy similarity
    transform. See this module's own docstring for why no embedding/
    fuzzy-match step exists anywhere in this pass."""
    normalized = unicodedata.normalize("NFKC", text).lower()
    normalized = _BOILERPLATE_RE.sub(" ", normalized)
    normalized = _NORMALIZE_STRIP_RE.sub(" ", normalized)
    # A period ending a sentence/word ("SOW." / "Thanks!") carries no
    # meaning worth preserving - unlike one BETWEEN digits (a decimal, e.g.
    # "2.5"), which _NORMALIZE_STRIP_RE already left untouched above.
    normalized = _SENTENCE_ENDING_PERIOD_RE.sub("", normalized)
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def canonical_key_for_claim(claim_type: str, text: str, owner: Optional[str],
                             reference_base: Optional[str]) -> Optional[str]:
    """The fallback dedup key checked after repeat_signals' byte-exact
    match fails (see materialize_claims_for_raw_item). Two tiers, most-
    trusted first:

    (1) A definitive reference (the producing raw_item's own real,
    structured PR/PO base - see reference_base's caller) is materially
    safer than any text comparison: "ask|approve|PR1161567|marc" ties the
    claim to a real identifier, not wording. Combined with the finite
    action-family keyword table above (falls back to "generic" if no
    keyword matches) and owner, scoped to claim_type by the caller's own
    (issue_id, claim_type) lookup.

    (2) Without a definitive reference, ONLY a conservative normalization
    of the claim text itself (Unicode-normalize/lowercase/strip boilerplate/
    collapse punctuation) - deliberately no fuzzy similarity or embeddings
    anywhere in this function, per this module's own docstring. Returns
    None (no fallback key at all) when the normalized text falls below
    _MIN_NORMALIZED_LEN - a key too generic to trust is worse than no key,
    since a false merge silently loses a real, distinct claim."""
    if reference_base:
        action_family = _action_family_for_text(text) or "generic"
        return f"{claim_type}|{action_family}|{reference_base}|{owner or 'unknown'}"
    normalized = _conservative_normalize(text)
    if len(normalized) < _MIN_NORMALIZED_LEN:
        return None
    return f"{claim_type}|text:{normalized}"


def _resolve_author(raw_item: dict) -> tuple[str, str]:
    direction = (raw_item or {}).get("direction")
    if direction == "outbound":
        return "marc", "direction"
    if direction == "inbound":
        return "counterparty", "direction"
    return "unknown", "unresolved"


def _derive_owner(claim_type: str, author: str, whose: Optional[str] = None) -> Optional[str]:
    if claim_type == "commitment":
        return author
    if claim_type == "ask":
        return _OTHER_SIDE.get(author, "unknown")
    if claim_type == "decision":
        return None
    if claim_type == "date":
        if whose in ("marc", "counterparty", "shared"):
            return whose if whose != "shared" else "unknown"
        return "unknown"
    return None


def _bump_for_key_facts(blob: dict, issue_id: str) -> None:
    """A key_fact is never a claim (there's no claim_type for it - see
    sync_checklist_action_to_claim's own docstring), so it's structurally
    invisible to every claims_revision bump insert_claim/touch_claim/
    update_claim_status already do. A real new key fact IS material new
    information though, and synthesis' staleness marker is entirely
    claims_revision-driven (workgraph_synthesis.compute_evidence_marker) -
    without this, an issue/project could accumulate genuinely new
    extracted content while reading as perfectly fresh forever. Shared by
    both the fresh-materialization and correction-reconciliation paths."""
    key_facts = blob.get("key_facts")
    if isinstance(key_facts, list) and any(isinstance(f, str) and f.strip() for f in key_facts):
        ws.bump_claims_revision(issue_id)


def _claim_specs_from_blob(blob: dict, author: str, reference_base: Optional[str]) -> list[dict]:
    """Pure: turns extracted_json into a flat list of claim specs (claim_
    type/text/owner/date_kind/canonical_key) - no DB access. Shared by the
    fresh-materialization path (which also runs the repeat_signals/
    canonical_key cross-issue dedup, see materialize_claims_for_raw_item)
    and the correction-reconciliation path (which diffs THIS raw_item's
    own old vs new specs directly - see _reconcile_extraction_correction),
    so the two paths can never compute a different claim set from the
    same extraction blob."""
    specs = []
    for field, claim_type in (("asks", "ask"), ("decisions", "decision"), ("commitments", "commitment")):
        values = blob.get(field)
        if not isinstance(values, list):
            continue
        for text in values:
            if not isinstance(text, str) or not text.strip():
                continue
            text = text.strip()
            owner = _derive_owner(claim_type, author)
            specs.append({
                "claim_type": claim_type, "text": text, "owner": owner, "date_kind": None,
                "canonical_key": canonical_key_for_claim(claim_type, text, owner, reference_base),
            })
    dates_mentioned = blob.get("dates_mentioned")
    for entry in (dates_mentioned if isinstance(dates_mentioned, list) else []):
        if not isinstance(entry, dict):
            continue
        text = (entry.get("text") or "").strip()
        if not text:
            continue
        date_kind = entry.get("kind") if entry.get("kind") in ("hard", "soft") else None
        owner = _derive_owner("date", author, whose=entry.get("whose"))
        specs.append({
            "claim_type": "date", "text": text, "owner": owner, "date_kind": date_kind,
            "canonical_key": canonical_key_for_claim("date", text, owner, reference_base),
        })
    return specs


def _suggest_reopen_if_matches_a_resolved_claim(issue_id: str, claim_type: str, text: str,
                                                 canonical_key: Optional[str], raw_item_id: int) -> None:
    """Task #304, item #5 (2026-08-11) - the 'reopen' claim suggestion.
    find_open_claim_by_text/find_open_claim_by_canonical_key have only
    ever searched OPEN claims, so a topic that comes back up again after
    its claim was already resolved (done/superseded/dismissed) always
    created a brand-new, disconnected claim with nothing linking it back
    to the fact this exact ask/decision/commitment was already marked
    closed once. Suggest-only, same discipline as every other mechanism
    in workgraph_reconcile.py: the fresh claim still gets inserted as
    usual (real new content is never silently withheld) - this only ADDS
    a suggestion for a human to review, never touches the resolved claim
    itself unless and until that suggestion is explicitly confirmed."""
    resolved = ws.find_resolved_claim_by_text(issue_id, claim_type, text)
    if resolved is None and canonical_key is not None:
        resolved = ws.find_resolved_claim_by_canonical_key(issue_id, claim_type, canonical_key)
    if resolved is None:
        return
    ws.create_claim_suggestion(
        claim_id=resolved["id"], suggestion_kind="reopen",
        evidence_type="resolved_claim_reoccurred",
        evidence_note=f"same {claim_type} text reoccurred after being marked {resolved['status']}: {text}",
        raw_item_id=raw_item_id,
    )


def _materialize_fresh(issue_id: str, raw_item_id: int, blob: dict, ts: Optional[float],
                        author: str, author_basis: str, reference_base: Optional[str]) -> int:
    """The FIRST-EVER materialization for a raw_item - no prior claims to
    reconcile against, so every spec either dedups against an existing
    OPEN claim elsewhere on the issue (repeat_signals, then canonical_key
    as a fallback - see canonical_key_for_claim's own docstring) or gets
    inserted fresh. Returns the count of newly inserted claim rows."""
    inserted = 0
    for spec in _claim_specs_from_blob(blob, author, reference_base):
        claim_type, text, owner, date_kind, canonical_key = (
            spec["claim_type"], spec["text"], spec["owner"], spec["date_kind"], spec["canonical_key"])

        # Section 9.3's real gap fix: repeat_signals now covers
        # commitments/decisions too, not just asks - same dedup rule,
        # widened scope, not a second mechanism.
        repeat = _matching_repeat_signal(blob, text) if claim_type != "date" else None
        existing = ws.find_open_claim_by_text(issue_id, claim_type, text) if repeat is not None else None
        # 2026-08-04 fallback: repeat_signals requires curator to have
        # volunteered an exact-text match AND find_open_claim_by_text to
        # confirm it byte-for-byte - real production duplicates (the
        # same Ariba PR reminder re-sent with slightly different
        # wording) pass through both checks untouched. canonical_key
        # (structured-reference-preferred, conservative-normalization
        # fallback - see canonical_key_for_claim's own docstring) is a
        # second, independent dedup path, checked only when the first
        # one didn't already find something.
        if existing is None and canonical_key is not None:
            existing = ws.find_open_claim_by_canonical_key(issue_id, claim_type, canonical_key)
        if existing is None:
            _suggest_reopen_if_matches_a_resolved_claim(issue_id, claim_type, text, canonical_key, raw_item_id)
        if existing is not None:
            # Only apply escalation state when repeat_signals actually
            # said something about it - escalated=None on touch_claim
            # means "just update last_seen_ts," never silently
            # downgrading a claim's existing escalated=1 back to 0 just
            # because THIS particular repeat came in via the
            # canonical_key fallback instead of repeat_signals.
            escalated = bool(repeat.get("escalated")) if repeat is not None else None
            escalation_note = repeat.get("escalation_note") if repeat is not None else None
            ws.touch_claim(existing["id"], ts=ts, escalated=escalated, escalation_note=escalation_note)
            # Section 12.3's right-sized event log: a repeat is real
            # signal either way - escalated means the same ask/
            # commitment/decision got worse (a new sender, more
            # senior), not-escalated means it's just still open and
            # someone said so again. Both are worth a real event, not
            # just the silent last_seen_ts bump touch_claim already
            # does. raw_item_id (2026-08-04): the ONE record of which
            # specific message caused THIS touch - claims.raw_item_id
            # only ever remembers the first one.
            ws.log_claim_event(
                existing["id"], "escalate" if escalated else "acknowledge",
                actor="curator", note=escalation_note, ts=ts, raw_item_id=raw_item_id,
            )
            continue

        claim_id = ws.insert_claim(
            issue_id=issue_id, raw_item_id=raw_item_id, claim_type=claim_type,
            text=text, author=author, author_basis=author_basis, owner=owner,
            date_kind=date_kind, ts=ts, canonical_key=canonical_key,
        )
        ws.log_claim_event(claim_id, "create", actor="curator", ts=ts, raw_item_id=raw_item_id)
        inserted += 1
    return inserted


def _reconcile_extraction_correction(issue_id: str, raw_item_id: int, blob: dict,
                                      author: str, author_basis: str, reference_base: Optional[str],
                                      new_content_hash: Optional[str]) -> int:
    """A raw_item's extraction was RE-EXTRACTED after already being
    materialized once - diffs the NEW spec set against the OLD one this
    SAME raw_item produced (never against the whole issue - that's the
    fresh path's job), scoped to claims still 'open' (a claim a real human
    action already resolved before the correction landed is left
    untouched either way, never silently re-opened or re-closed by an
    extraction diff).

    Deliberately no fuzzy/1:1 pairing between an old and new entry of the
    same claim_type - text present in both old and new is UNCHANGED (left
    alone); text only in the new set is ADDED; text only in the old set is
    REMOVED (superseded - "not completed real-world work," per this
    module's own docstring, so never status='done'). A wording correction
    therefore shows up as one remove + one add, not an in-place edit -
    the only deterministic way to represent it without guessing which old
    entry a changed one corresponds to.

    All the writes below happen in ONE transaction (see workgraph_store.
    reconcile_extraction_claims) - a corrected extraction's claims table
    update can never be left half-applied."""
    new_specs = _claim_specs_from_blob(blob, author, reference_base)
    old_claims = [c for c in ws.list_claims_for_raw_item(raw_item_id) if c["status"] == "open"]

    new_by_type: dict[str, list[dict]] = {}
    for spec in new_specs:
        new_by_type.setdefault(spec["claim_type"], []).append(spec)
    old_by_type: dict[str, list[dict]] = {}
    for claim in old_claims:
        old_by_type.setdefault(claim["claim_type"], []).append(claim)

    to_insert = []
    to_supersede = []
    all_added = []    # across every claim_type bucket - see the pairing note below
    all_removed = []
    for claim_type in set(new_by_type) | set(old_by_type):
        new_list = new_by_type.get(claim_type, [])
        old_list = old_by_type.get(claim_type, [])
        old_texts = {c["text"] for c in old_list}
        new_texts = {s["text"] for s in new_list}
        added = [spec for spec in new_list if spec["text"] not in old_texts]
        removed = [claim for claim in old_list if claim["text"] not in new_texts]
        to_insert.extend(added)
        to_supersede.extend(claim["id"] for claim in removed)
        all_added.extend(added)
        all_removed.extend(removed)

    # Fix 3 (2026-08-11, doc Section 7): classify the change ONLY when
    # there's EXACTLY one addition and one removal across the WHOLE
    # reconciliation pass (not just within one claim_type bucket - a real
    # owner/type correction, e.g. "decision" re-extracted as "ask" for the
    # same text, spans two buckets by construction). This is still the one
    # genuinely unambiguous pairing, never a guess among candidates - this
    # module's own established "no fuzzy 1:1 pairing" rule (see this
    # function's docstring above) is about genuinely ambiguous
    # multi-to-multi cases, not a single global candidate on each side.
    refinements = []  # (old_claim, new_spec, event_type) - see _classify_refinement
    if len(all_added) == 1 and len(all_removed) == 1:
        refinements.append((all_removed[0], all_added[0], _classify_refinement(all_removed[0], all_added[0])))

    if not to_insert and not to_supersede:
        # The extraction's content_hash changed (that's why we're here at
        # all - materialized_hash != content_hash), but the corrected
        # blob produces the IDENTICAL claim set (e.g. only key_facts or
        # unrelated metadata changed) - still mark materialized so this
        # exact content never gets re-diffed, but nothing to insert/
        # supersede means no reconcile_extraction_claims call needed.
        ws.mark_extraction_materialized(raw_item_id, new_content_hash)
        _bump_for_key_facts(blob, issue_id)
        return 0

    insert_specs = [{
        "claim_type": spec["claim_type"], "text": spec["text"], "owner": spec.get("owner"),
        "date_kind": spec.get("date_kind"), "canonical_key": spec.get("canonical_key"),
        "author": author, "author_basis": author_basis,
    } for spec in to_insert]
    ws.reconcile_extraction_claims(
        issue_id=issue_id, raw_item_id=raw_item_id, to_insert=insert_specs,
        to_supersede=to_supersede, new_materialized_hash=new_content_hash,
    )
    # Fix 3 (2026-08-11): log the real sub-type on the superseded claim -
    # separate from reconcile_extraction_claims' own transaction since a
    # missing/failed event log must never roll back a real claims write.
    for old_claim, new_spec, event_type in refinements:
        ws.log_claim_event(
            old_claim["id"], event_type, actor="curator",
            note=(new_spec.get("text") or "")[:160], raw_item_id=raw_item_id,
        )
    _bump_for_key_facts(blob, issue_id)
    return len(to_insert)


def materialize_claims_for_raw_item(raw_item_id: int) -> int:
    """Idempotent - safe to call more than once for the same raw_item
    (mirrors the never-re-extract discipline raw_item_extractions itself
    relies on for the extraction step). Returns the number of NEW claim
    rows inserted (touches to existing open claims, via repeat_signals/
    canonical_key dedup, don't count). No-ops (returns 0) if there's no
    extraction yet, no issue_id yet, or this exact extraction content has
    already been materialized.

    Corrected-extraction reconciliation (2026-08-04, architecture-review
    follow-up P1): create_extraction is an UPSERT - a re-extraction just
    overwrites extracted_json in place - but the OLD guard here
    (has_claims_for_raw_item, "does this raw_item have ANY claims at
    all") meant a corrected extraction never re-materialized once the
    first pass had run, silently letting the claims ledger and the
    extraction diverge forever. materialized_hash (on raw_item_
    extractions) now records which content_hash the claims table was
    last reconciled against; a mismatch means either this raw_item has
    never been materialized at all (materialized_hash is NULL - use the
    normal fresh-insert path) or a real correction landed since the last
    reconciliation (materialized_hash is set but differs - diff old vs
    new specs for THIS raw_item specifically, see
    _reconcile_extraction_correction).

    Design doc Section 12.10 (prompt-injection boundary, a standing
    constraint, not a one-time check): this function reads ONLY
    extraction.extracted_json's already-parsed asks/decisions/commitments/
    dates_mentioned fields - never raw_item's own subject/body text
    directly. Content read FROM evidence is structurally untrusted and can
    never itself become an operating instruction; any future field added
    here must stay on the extracted_json side of that line."""
    raw_item = ws.get_raw_item(raw_item_id)
    if not raw_item or not raw_item.get("issue_id"):
        return 0
    issue_id = raw_item["issue_id"]

    extraction = ws.get_extraction(raw_item_id)
    if not extraction:
        return 0
    content_hash = extraction.get("content_hash")
    materialized_hash = extraction.get("materialized_hash")
    if materialized_hash is not None and materialized_hash == content_hash:
        return 0  # this exact extraction content has already been reconciled - true no-op

    blob = extraction.get("extracted_json") or {}
    ts = extraction.get("extracted_ts")
    author, author_basis = _resolve_author(raw_item)
    reference_base = raw_item.get("pr_number_base")

    # Fix 4 (task #310 follow-up, 2026-08-11): real new evidence is about to
    # be materialized for this work_object - if it (or its parent project)
    # was dormant, wake it back up now. Dormant is never a permanent state.
    workgraph_lifecycle.revert_dormant_if_needed(issue_id)

    # Task #319: two more real, structured signals this same curator-
    # extraction step can carry, resolved/written exactly once per distinct
    # extraction content (same "already reconciled" guard above covers
    # both, since they read from this same already-fetched `blob`) - see
    # _resolve_explicit_completions and _write_project_link_signals for why
    # neither needs its own separate re-materialization guard.
    _resolve_explicit_completions(blob, issue_id, raw_item_id, reference_base)
    _write_project_link_signals(blob, issue_id, raw_item_id)

    if materialized_hash is None:
        inserted = _materialize_fresh(issue_id, raw_item_id, blob, ts, author, author_basis, reference_base)
        _bump_for_key_facts(blob, issue_id)
        ws.mark_extraction_materialized(raw_item_id, content_hash)
        return inserted

    return _reconcile_extraction_correction(issue_id, raw_item_id, blob, author, author_basis,
                                             reference_base, content_hash)


# --- completion + cross-project dependency detection (task #319) -----------
# Two real gaps confirmed before building either: (1) resolution_signals
# (task #155, SYNTHESIS_ROUTINE.md) already carries curator's judgment that
# THIS raw_item's content directly and unambiguously states a SPECIFIC
# earlier open claim was fulfilled, but the only existing consumer of that
# signal (workgraph_reconcile.generate_resolution_signal_suggestions) only
# ever matched it by byte-exact claim text - a resolution phrased
# differently than the original ask (but sharing a real structured
# reference) was invisible to it. _resolve_explicit_completions closes that
# specific gap with a wider match (see _find_claim_for_resolution_signal),
# staying suggest-only like every other claim-closing path in this module -
# see that function's own docstring for why an earlier auto-resolving
# version of this was scoped back after review. (2) project_links
# (workgraph_store.py) already has the right shape (from/to project id + a
# link_type enum covering depends_on/blocks/enables among others) but had
# zero writer anywhere - confirmed via grep before writing a line of this.

_COMPLETION_CLAIM_TYPES = ("ask", "decision", "commitment")


def _find_claim_for_resolution_signal(issue_id: str, claim_type: str, claim_text: str,
                                       reference_base: Optional[str]) -> Optional[dict]:
    """Same two-tier structural match this module's own dedup already
    trusts (see canonical_key_for_claim's docstring), applied to resolution
    matching instead of dedup - deliberately no fuzzy text comparison
    anywhere in this function:

    (1) Byte-exact text match among this issue's OPEN claims of the same
    type (find_open_claim_by_text) - the same "thread continuity" match
    resolution_signals has always used (curator is asked to reproduce the
    EARLIER claim's own text verbatim, per SYNTHESIS_ROUTINE.md).

    (2) Only when THIS raw_item carries a real structured reference
    (reference_base, e.g. a PR number) - a reference+action-family
    structural match (find_open_claim_by_canonical_key_prefix), owner-
    agnostic on purpose: the message that resolves an earlier ask
    ("PR1161567 approved") rarely carries the same author/owner framing
    the original ask did, so requiring an exact owner match too would
    silently miss genuine matches a real shared reference id already
    proves."""
    claim = ws.find_open_claim_by_text(issue_id, claim_type, claim_text)
    if claim is not None:
        return claim
    if not reference_base:
        return None
    action_family = _action_family_for_text(claim_text) or "generic"
    prefix = f"{claim_type}|{action_family}|{reference_base}|"
    return ws.find_open_claim_by_canonical_key_prefix(issue_id, claim_type, prefix)


def _resolve_explicit_completions(blob: dict, issue_id: str, raw_item_id: int,
                                   reference_base: Optional[str]) -> int:
    """Task #319 built this to auto-resolve directly (update_claim_status
    straight to 'done'), and Marc's own explicit call afterward
    (2026-08-11) was to scope it back to suggest-only: every OTHER
    claim-closing path in this codebase requires a human confirm first
    (see workgraph_reconcile.py's module docstring, repeated three times
    over - "no fuzzy/heuristic scoring," "never treated as evidence,"
    suggest-only discipline), and this was a real, deliberate departure
    from that, not just a gap-fill. Real business risk: a rare false
    structural match would have silently closed a genuine open
    commitment with no human ever seeing it happen.

    Still real, still worth keeping: the broader match this function's
    own _find_claim_for_resolution_signal added over what workgraph_
    reconcile.generate_resolution_signal_suggestions already does alone
    (byte-exact text only) - a reference/canonical-key fallback match for
    a resolution phrased differently than the original ask. That widened
    match now feeds ws.create_claim_suggestion the exact same way the
    existing byte-exact path does, so it's a suggestion a human confirms
    through the normal checklist action, never an automatic status change.

    create_claim_suggestion's own dedupe-then-insert (pending + same
    claim_id + evidence_type) means this is safe to call even when the
    byte-exact tier ALSO matches and generate_resolution_signal_
    suggestions (called separately, elsewhere in the pipeline) creates
    the identical suggestion first or second - whichever runs first wins,
    the other is a no-op reuse of the same pending row, never a
    duplicate. Returns the count of signals that produced a suggestion
    (new or already-pending)."""
    signals = blob.get("resolution_signals")
    if not isinstance(signals, list):
        return 0
    suggested = 0
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        claim_type = signal.get("claim_type")
        claim_text = signal.get("claim_text")
        if claim_type not in _COMPLETION_CLAIM_TYPES or not claim_text:
            continue
        claim = _find_claim_for_resolution_signal(issue_id, claim_type, claim_text, reference_base)
        if claim is None:
            continue
        ws.create_claim_suggestion(
            claim_id=claim["id"], suggestion_kind="resolve",
            evidence_type="explicit_resolution_signal",
            evidence_note=(signal.get("resolution_note") or "").strip()[:160] or None,
            raw_item_id=raw_item_id,
        )
        suggested += 1
    return suggested


_PROJECT_LINK_RELATIONSHIP_TYPES = ("depends_on", "blocks", "enables")


def _write_project_link_signals(blob: dict, issue_id: str, raw_item_id: int) -> int:
    """project_links already has the right shape (see workgraph_store.py's
    own CREATE TABLE comment) but, confirmed via grep before this was
    written, had zero writer anywhere. `dependency_signals` is a new
    curator-extraction field (SYNTHESIS_ROUTINE.md) - populated ONLY when a
    raw_item's content explicitly names a real, SPECIFIC other project
    curator has already confirmed exists (from her own already-fetched
    project list - never a topical-similarity guess) as depending on/
    blocking/enabling the CURRENT issue's own project.

    Project-scoped, same reasoning project_links' own CREATE TABLE comment
    gives for why this is project-level, not issue-level - skipped entirely
    (never queued, never guessed at) when this issue isn't grouped into a
    real project yet, or when the named target isn't a real, existing
    project row. Idempotent via create_project_link's own dedupe-then-
    insert (same (from, to, link_type) triple never gets a second row), so
    safe to call on every materialization pass, including a resend of the
    identical raw_item content. Returns the count of links written or
    already on record."""
    signals = blob.get("dependency_signals")
    if not isinstance(signals, list):
        return 0
    issue = ws.get_issue_or_cluster(issue_id)
    from_project_id = issue.get("project_id") if issue else None
    if not from_project_id:
        return 0
    written = 0
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        relationship = signal.get("relationship")
        target_project_id = signal.get("target_project_id")
        if relationship not in _PROJECT_LINK_RELATIONSHIP_TYPES or not target_project_id:
            continue
        if target_project_id == from_project_id:
            continue
        if ws.get_project(target_project_id) is None:
            continue  # never link to a project curator merely named, not a confirmed real one
        ws.create_project_link(
            from_project_id=from_project_id, to_project_id=target_project_id,
            link_type=relationship, reason=(signal.get("reason") or f"detected via raw_item {raw_item_id}"),
            created_by="claims_dependency_signal",
        )
        written += 1
    return written


def _matching_repeat_signal(blob: dict, ask_text: str) -> Optional[dict]:
    signals = blob.get("repeat_signals")
    if not isinstance(signals, list):
        return None
    for signal in signals:
        if isinstance(signal, dict) and signal.get("ask_text") == ask_text:
            return signal
    return None


_CHECKLIST_KIND_TO_CLAIM_TYPE = {"ask": "ask", "decision": "decision", "commitment": "commitment"}
_CHECKLIST_STATUS_TO_CLAIM_STATUS = {"done": "done", "dismissed": "dismissed"}
_CHECKLIST_STATUS_TO_EVENT = {"done": "complete", "dismissed": "dismiss"}


def sync_checklist_action_to_claim(*, issue_id: str, kind: str, text: str, status: str, actor: str) -> bool:
    """Design doc Section 12.3: closes a real gap found while building the
    event log - nothing before this ever moved a claim out of 'open', even
    though the checklist UI's own "Mark done"/"Dismiss" actions
    (workgraph_store.mark_checklist_item_done/dismiss_checklist_item) have
    represented exactly that outcome, on the SAME underlying text, since
    task #44/#59. Those two mechanisms were never connected - checklist_
    dismissals stays the authoritative record for the UI (unchanged, not
    replaced), this is purely additive: best-effort, silently a no-op
    (returns False) for a `kind` that isn't a real claim_type (e.g.
    key_facts, which were never claims) or when no matching OPEN claim is
    found (older data from before Phase 3, or an already-resolved claim) -
    never an error, since checklist_dismissals already succeeded by the
    time this runs and remains correct either way."""
    claim_type = _CHECKLIST_KIND_TO_CLAIM_TYPE.get(kind)
    new_status = _CHECKLIST_STATUS_TO_CLAIM_STATUS.get(status)
    if claim_type is None or new_status is None:
        return False
    claim = ws.find_open_claim_by_text(issue_id, claim_type, text.strip())
    if claim is None:
        return False
    ws.update_claim_status(claim["id"], new_status, actor=actor)
    ws.log_claim_event(claim["id"], _CHECKLIST_STATUS_TO_EVENT[status], actor=actor)
    return True


def list_open_claims_for_issue(issue_id: str, claim_type: Optional[str] = None) -> list[dict]:
    return ws.list_open_claims_for_issue(issue_id, claim_type=claim_type)


def list_open_claims_for_issues(issue_ids: list[str], claim_type: Optional[str] = None) -> dict[str, list[dict]]:
    return ws.list_open_claims_for_issues(issue_ids, claim_type=claim_type)


_CLOSED_ISSUE_STATES = ("done", "dismissed", "noise-archived")


def find_duplicate_or_conflicting_asks_across_project() -> list[dict]:
    """Enhancement idea panel #17 (Duplicate/conflicting-ask detector
    across project, worker capability): canonical-key dedup at
    materialization time is deliberately issue-scoped (see canonical_
    key_for_claim's own docstring - "the same PR, supplier, or action
    language can appear in separate cases... should not silently
    collapse claims across work objects"), which is correct for two
    issues that AREN'T related. But once two issues that both carry the
    same canonical_key get grouped into the same project, that's no
    longer two unrelated matters - it's the same ask/commitment/decision
    tracked twice, or (the more valuable catch) tracked twice with
    DIFFERENT details, e.g. two different dollar figures for the same PR
    approval on two issues under one project (the exact live PR854779-V4
    shape found while building this).

    Batched: one query across every open claim with a canonical_key
    (list_open_claims_with_canonical_key_and_project), grouped in Python
    by (project_id, claim_type, canonical_key) - never a per-project or
    per-issue query. Closed issues (done/dismissed/noise-archived) are
    excluded - same reasoning as find_all_reference_id_collisions: an
    unprompted alert about a matter Marc already closed isn't actionable
    right now.

    Returns one entry per (project_id, claim_type, canonical_key) group
    that spans 2+ DISTINCT issues: {project_id, claim_type, canonical_key,
    verdict, claims: [{claim_id, issue_id, text}, ...]}. verdict is
    'conflicting' when the claims disagree on text (a real discrepancy
    worth surfacing), 'duplicate' when every claim's text is byte-
    identical (the same thing tracked twice, nothing new to reconcile)."""
    rows = ws.list_open_claims_with_canonical_key_and_project()
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        if row["state"] in _CLOSED_ISSUE_STATES:
            continue
        key = (row["project_id"], row["claim_type"], row["canonical_key"])
        groups.setdefault(key, []).append(row)

    results = []
    for (project_id, claim_type, canonical_key), members in groups.items():
        issue_ids = {m["issue_id"] for m in members}
        if len(issue_ids) < 2:
            continue
        texts = {m["text"] for m in members}
        verdict = "duplicate" if len(texts) == 1 else "conflicting"
        results.append({
            "project_id": project_id, "claim_type": claim_type, "canonical_key": canonical_key,
            "verdict": verdict,
            "claims": [{"claim_id": m["id"], "issue_id": m["issue_id"], "text": m["text"]} for m in members],
        })
    return results
