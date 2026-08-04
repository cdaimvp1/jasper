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
"""
from __future__ import annotations

from typing import Optional

import workgraph_store as ws

_OTHER_SIDE = {"marc": "counterparty", "counterparty": "marc", "unknown": "unknown"}


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


def materialize_claims_for_raw_item(raw_item_id: int) -> int:
    """Idempotent - safe to call more than once for the same raw_item (mirrors
    the never-re-extract discipline raw_item_extractions itself relies on).
    Returns the number of NEW claim rows inserted (touches to existing open
    claims, via repeat_signals dedup, don't count). No-ops (returns 0) if
    there's no extraction yet, no issue_id yet, or claims already exist for
    this raw_item.

    Design doc Section 12.10 (prompt-injection boundary, a standing
    constraint, not a one-time check): this function reads ONLY
    extraction.extracted_json's already-parsed asks/decisions/commitments/
    dates_mentioned fields (below) - never raw_item's own subject/body text
    directly. Content read FROM evidence is structurally untrusted and can
    never itself become an operating instruction; any future field added
    here must stay on the extracted_json side of that line."""
    if ws.has_claims_for_raw_item(raw_item_id):
        return 0

    raw_item = ws.get_raw_item(raw_item_id)
    if not raw_item or not raw_item.get("issue_id"):
        return 0
    issue_id = raw_item["issue_id"]

    extraction = ws.get_extraction(raw_item_id)
    if not extraction:
        return 0
    blob = extraction.get("extracted_json") or {}
    ts = extraction.get("extracted_ts")

    author, author_basis = _resolve_author(raw_item)
    inserted = 0

    for field, claim_type in (("asks", "ask"), ("decisions", "decision"), ("commitments", "commitment")):
        values = blob.get(field)
        if not isinstance(values, list):
            continue
        for text in values:
            if not isinstance(text, str) or not text.strip():
                continue
            text = text.strip()
            owner = _derive_owner(claim_type, author)

            # Section 9.3's real gap fix: repeat_signals now covers
            # commitments/decisions too, not just asks - same dedup rule,
            # widened scope, not a second mechanism.
            repeat = _matching_repeat_signal(blob, text)
            if repeat is not None:
                existing = ws.find_open_claim_by_text(issue_id, claim_type, text)
                if existing is not None:
                    escalated = bool(repeat.get("escalated"))
                    ws.touch_claim(
                        existing["id"], ts=ts, escalated=escalated,
                        escalation_note=repeat.get("escalation_note"),
                    )
                    # Section 12.3's right-sized event log: a repeat is real
                    # signal either way - escalated means the same ask/
                    # commitment/decision got worse (a new sender, more
                    # senior), not-escalated means it's just still open and
                    # someone said so again. Both are worth a real event,
                    # not just the silent last_seen_ts bump touch_claim
                    # already does.
                    ws.log_claim_event(
                        existing["id"], "escalate" if escalated else "acknowledge",
                        actor="curator", note=repeat.get("escalation_note"), ts=ts,
                    )
                    continue

            claim_id = ws.insert_claim(
                issue_id=issue_id, raw_item_id=raw_item_id, claim_type=claim_type,
                text=text, author=author, author_basis=author_basis, owner=owner, ts=ts,
            )
            ws.log_claim_event(claim_id, "create", actor="curator", ts=ts)
            inserted += 1

    dates_mentioned = blob.get("dates_mentioned")
    for entry in (dates_mentioned if isinstance(dates_mentioned, list) else []):
        if not isinstance(entry, dict):
            continue
        text = (entry.get("text") or "").strip()
        if not text:
            continue
        date_kind = entry.get("kind") if entry.get("kind") in ("hard", "soft") else None
        owner = _derive_owner("date", author, whose=entry.get("whose"))
        claim_id = ws.insert_claim(
            issue_id=issue_id, raw_item_id=raw_item_id, claim_type="date",
            text=text, author=author, author_basis=author_basis, owner=owner,
            date_kind=date_kind, ts=ts,
        )
        ws.log_claim_event(claim_id, "create", actor="curator", ts=ts)
        inserted += 1

    # Fixed 2026-08-04 (architecture-review follow-up, P1): a key_fact is
    # never a claim (there's no claim_type for it - see sync_checklist_
    # action_to_claim's own docstring), so it was structurally invisible
    # to every claims_revision bump above. A real new key fact IS material
    # new information though, and synthesis' staleness marker is entirely
    # claims_revision-driven (workgraph_synthesis.compute_evidence_marker)
    # - without this, an issue/project could accumulate genuinely new
    # extracted content while reading as perfectly fresh forever.
    key_facts = blob.get("key_facts")
    if isinstance(key_facts, list) and any(isinstance(f, str) and f.strip() for f in key_facts):
        ws.bump_claims_revision(issue_id)

    return inserted


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
