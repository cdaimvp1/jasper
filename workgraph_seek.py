"""workgraph_seek.py - "seek to understand": turn a localized gap into the
concrete sources that could close it, and into a question worth asking a real
person. Tasks #396 (source enumeration) and #397 (question generation).

WHERE THIS SITS
---------------
`workgraph_ambiguity.py` measures and localizes: it produces `Gap` objects that
each say what is missing (`what`) and, in prose, where an answer might come from
(`fillable_by`). This module makes that second half machine-usable:

    measure (#393/#394)  ->  localize gaps (#393)  ->  ENUMERATE SOURCES (#396)
                                                   ->  GENERATE QUESTIONS (#397)
                                                   ->  persist answers (#398)
                                                   ->  escalation package (#399)

That is Marc's own stated sequence - measure, localize, seek, ask, escalate -
and it exists specifically so Jasper closes gaps by FINDING OUT rather than by
deciding. Nothing here concludes anything about the world.

THE THREE RULES THIS MODULE KEEPS
---------------------------------
1. NEVER INVENT A RECIPIENT. A question is only generated when there is a real
   party on the project to ask. No party -> no question, and the gap says so.
   Guessing who to ask is how an assistant emails the wrong person about a
   contract.
2. NEVER RANK THE SOURCES. `enumerate_sources` returns options with an honest
   `available` flag and a reason when unavailable. It does not score them or
   pick a winner - that would be the authority model applied to methods instead
   of facts. Which source to use is the human's call, or the caller's.
3. NO LLM, NO WRITES. Pure functions over a Gap plus context the caller already
   read. Deterministic: same gap in, same options and questions out. Callers may
   consume the output; nothing obliges them to (the Gate A rule from
   docs/design/GATES_FEDERATION_AND_MECHANISM_TRIAGE.md s1).

WHAT IS ACTUALLY REACHABLE, MEASURED NOT ASSUMED (2026-08-22)
-------------------------------------------------------------
`available=True` is claimed only where a real, credential-free, unattended path
exists today:

  search_records   YES - evidence_fts is populated and searchable
                         (ws.search_evidence_fts)
  ask_person       YES - parties carries real emails; Outlook COM can draft.
                         Available only when the project HAS an external party.
  read_document    PARTIAL - the local OneDrive sync reaches 29 of 157
                         SharePoint items (ingest/sharepoint_local_sync.py).
                         Marked available only when the gap names a document
                         Jasper already staged text for.
  check_system     NO  - Ariba/SAP/S2P is reachable ONLY through an interactive
                         ARIA MCP session, never from unattended code. Recorded
                         as an option with available=False and the reason, so
                         the gap is honest about what a human COULD do that
                         Jasper cannot.

That last row is the point of the `available` flag: an unreachable source is
still worth NAMING, because a human reading an escalation package can act on it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import workgraph_store as ws

# --------------------------------------------------------------- #396 types --

#: Deliberately a small, closed vocabulary. These are KINDS OF ACTION, not a
#: ranking, and they map to how a person actually closes a gap.
SOURCE_KINDS = ("search_records", "ask_person", "read_document", "check_system")


@dataclass(frozen=True)
class SourceOption:
    """One concrete way a gap could be closed.

    `available` is about JASPER's reach right now, unattended - not about
    whether the answer exists. An option with available=False is still
    returned, with `why_unavailable` populated, because naming it is useful to
    a human even when Jasper cannot act on it.
    """
    kind: str
    what: str
    target: Optional[str] = None
    available: bool = False
    why_unavailable: Optional[str] = None

    def as_dict(self) -> dict:
        return {"kind": self.kind, "what": self.what, "target": self.target,
                "available": self.available, "why_unavailable": self.why_unavailable}


#: The one genuinely unreachable source, factored out so its reason is stated
#: once and stays accurate. See the module docstring's measurement table.
_SYSTEM_OF_RECORD_UNAVAILABLE = (
    "Ariba/SAP purchase records are reachable only through an interactive ARIA "
    "MCP session (S2P Purchase Order Product, confirmed live 2026-08-21). "
    "Jasper's ingestion is unattended, so it cannot query them; a human can."
)


def enumerate_sources(gap, *, parties: Optional[list] = None,
                      staged_documents: Optional[set] = None) -> list[SourceOption]:
    """#396. Which concrete sources could close THIS gap? Deterministic.

    `parties` - already-read party rows for the project (never fetched here, so
    this stays a pure function and the caller controls DB access).
    `staged_documents` - filenames Jasper has already extracted text for, used
    to decide whether `read_document` is genuinely available.

    Returns [] for an unrecognized gap kind rather than a generic guess: a
    made-up source is worse than an admitted blank.
    """
    parties = parties or []
    staged_documents = staged_documents or set()
    externals = [p for p in parties
                 if (p.get("affiliation") or "") != "internal" and p.get("primary_email")]

    out: list[SourceOption] = []
    kind = getattr(gap, "kind", None)

    if kind == "unresolved_reference":
        token = getattr(gap, "ref", None) or "the reference"
        out.append(SourceOption(
            kind="search_records",
            what=f"full-text search the graph's own evidence for {token} - it may "
                 f"appear in an item that was never linked to this project",
            target="evidence_fts",
            available=True,
        ))
        out.append(SourceOption(
            kind="check_system",
            what=f"look {token} up in the purchasing system of record to confirm it "
                 f"exists and read its supplier, amount and status",
            target="Ariba/SAP (S2P)",
            available=False,
            why_unavailable=_SYSTEM_OF_RECORD_UNAVAILABLE,
        ))
        if externals:
            out.append(SourceOption(
                kind="ask_person",
                what=f"ask whoever cited {token} what it refers to",
                target=externals[0]["primary_email"],
                available=True,
            ))

    elif kind == "stale_evidence":
        if externals:
            out.append(SourceOption(
                kind="ask_person",
                what="ask the counterparty whether anything has changed since the "
                     "last update on record",
                target=externals[0]["primary_email"],
                available=True,
            ))
        else:
            out.append(SourceOption(
                kind="ask_person",
                what="ask the counterparty for current status",
                target=None,
                available=False,
                why_unavailable="no external party is recorded on this project, so "
                                "there is nobody to ask - Jasper will not guess a "
                                "recipient",
            ))
        out.append(SourceOption(
            kind="search_records",
            what="check for newer items that were ingested but never linked here",
            target="evidence_fts",
            available=True,
        ))

    elif kind == "closed_issue_with_open_claims":
        issue_id = getattr(gap, "ref", None) or "the issue"
        out.append(SourceOption(
            kind="search_records",
            what=f"re-read the evidence on {issue_id} to see whether the open claims "
                 f"were in fact completed before it was closed",
            target=issue_id,
            available=True,
        ))
        if externals:
            out.append(SourceOption(
                kind="ask_person",
                what="confirm with the counterparty that the outstanding items are done",
                target=externals[0]["primary_email"],
                available=True,
            ))

    elif kind == "claims_without_evidence":
        out.append(SourceOption(
            kind="search_records",
            what="re-check ingestion: the source items for these claims are not "
                 "retrievable, which is an ingestion fault rather than a knowledge gap",
            target="raw_items / dead-letter log",
            available=True,
        ))

    elif kind == "missing_required_context":
        # Emitted by #394's required-context check. The named field is what to
        # go and find.
        field_name = getattr(gap, "ref", None) or "a required field"
        out.append(SourceOption(
            kind="search_records",
            what=f"search this project's own evidence for {field_name} - it may be "
                 f"present in text but never extracted",
            target="evidence_fts",
            available=True,
        ))
        doc_hit = any(field_name.lower() in d.lower() for d in staged_documents)
        out.append(SourceOption(
            kind="read_document",
            what=f"read the attached documents for {field_name}",
            target="local OneDrive sync",
            available=bool(doc_hit),
            why_unavailable=None if doc_hit else (
                "no staged document on this project mentions that field; only 29 of "
                "157 SharePoint items have locally-synced text (see "
                "ingest/sharepoint_local_sync.py)"),
        ))
        if externals:
            out.append(SourceOption(
                kind="ask_person",
                what=f"ask the counterparty for {field_name}",
                target=externals[0]["primary_email"],
                available=True,
            ))

    return out


# --------------------------------------------------------------- #397 types --

@dataclass(frozen=True)
class Question:
    """A question worth asking a real person, derived from one gap.

    `answer_would_close` is the honesty check: if a caller cannot say what the
    answer would resolve, the question should not be asked. It is also what
    #398 uses to attach the eventual answer back to the right gap.
    """
    gap_kind: str
    text: str
    asked_of: str
    answer_would_close: str
    ref: Optional[str] = None

    def as_dict(self) -> dict:
        return {"gap_kind": self.gap_kind, "text": self.text,
                "asked_of": self.asked_of,
                "answer_would_close": self.answer_would_close, "ref": self.ref}


#: One template per gap kind. Deterministic strings, no model call - the same
#: discipline workgraph_classify and cross_mention_match already use, and it
#: keeps every question auditable before it is ever sent.
#:
#: Phrasing rule: ask for a FACT the recipient owns. Never ask them to confirm
#: Jasper's guess ("is this the Kinaxis renewal?"), because a yes to a leading
#: question is not evidence - it is the recipient being agreeable.
_QUESTION_TEMPLATES = {
    "unresolved_reference": (
        "Quick question - I have {ref} referenced against this work but nothing on "
        "file that matches it. What does {ref} cover?",
        "identifies what {ref} refers to",
    ),
    "stale_evidence": (
        "Checking in on this one - the last update I have is from a while back. "
        "Where does it stand now?",
        "establishes current state where the record is stale",
    ),
    "closed_issue_with_open_claims": (
        "I have this marked as closed, but a few items on it still look "
        "outstanding on my side. Were those wrapped up?",
        "resolves whether the recorded closed state is correct",
    ),
    "missing_required_context": (
        "One gap on my side - I do not have {ref} recorded for this. Could you "
        "point me to it?",
        "supplies the missing {ref}",
    ),
}


def generate_questions(gaps: list, *, parties: Optional[list] = None,
                       max_questions: int = 5) -> list[Question]:
    """#397. Turn gaps into questions for real people. Deterministic, no LLM.

    Skips a gap entirely when:
      * its kind has no template (an unrecognized gap gets no invented question)
      * there is no external party to ask (RULE 1 - never invent a recipient)
      * the gap is `claims_without_evidence`, which is an INGESTION FAULT, not
        something to bother a counterparty about. That one belongs in the
        escalation package to Marc, not in an email to a supplier.

    `max_questions` bounds the output, and when it truncates it is the caller's
    job to say so - this returns the first N in gap order, and the count of
    what it dropped is recoverable as len(eligible) - len(returned) by the
    caller. No silent caps.
    """
    parties = parties or []
    externals = [p for p in parties
                 if (p.get("affiliation") or "") != "internal" and p.get("primary_email")]
    if not externals:
        return []
    recipient = externals[0]["primary_email"]

    out: list[Question] = []
    for gap in gaps:
        kind = getattr(gap, "kind", None)
        if kind == "claims_without_evidence":
            continue  # our bug, not their question
        tpl = _QUESTION_TEMPLATES.get(kind)
        if tpl is None:
            continue
        text_tpl, closes_tpl = tpl
        ref = getattr(gap, "ref", None) or ""
        out.append(Question(
            gap_kind=kind,
            text=text_tpl.format(ref=ref) if "{ref}" in text_tpl else text_tpl,
            asked_of=recipient,
            answer_would_close=(closes_tpl.format(ref=ref)
                                if "{ref}" in closes_tpl else closes_tpl),
            ref=getattr(gap, "ref", None),
        ))
        if len(out) >= max_questions:
            break
    return out


# ------------------------------------------------------------------ helpers --

def parties_for_project(project_id: str) -> list[dict]:
    """Convenience read so callers do not have to know the store shape. Kept
    OUT of the pure functions above deliberately - they take already-read rows
    so they stay testable with no DB and cannot surprise a caller with I/O."""
    return ws.list_parties_for_issue(project_id)


def seek_for_signal(signal, *, project_id: Optional[str] = None) -> dict:
    """Convenience: gaps -> sources + questions for one AmbiguitySignal.

    Returns a plain dict, advisory only. Writes nothing, decides nothing, and
    - per the Two Gates convention - does not authorize any action.
    """
    pid = project_id or getattr(signal, "project_id", None)
    parties = parties_for_project(pid) if pid else []
    gaps = list(getattr(signal, "gaps", ()) or ())
    per_gap = []
    for g in gaps:
        per_gap.append({
            "gap": {"kind": g.kind, "what": g.what, "fillable_by": g.fillable_by,
                    "ref": g.ref},
            "sources": [s.as_dict() for s in enumerate_sources(g, parties=parties)],
        })
    questions = generate_questions(gaps, parties=parties)
    return {
        "project_id": pid,
        "n_gaps": len(gaps),
        "gaps": per_gap,
        "questions": [q.as_dict() for q in questions],
        "n_external_parties": len([p for p in parties
                                   if (p.get("affiliation") or "") != "internal"]),
        "advisory_only": True,
    }
