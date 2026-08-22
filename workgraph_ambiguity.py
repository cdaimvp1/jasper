"""Deterministic ambiguity + context-confidence measurement (tasks #393, #394).

WHAT THIS IS
------------
A measurement instrument. Given a project, it reports how well-supported
Jasper's understanding of that project currently is, and - more usefully -
WHERE specifically the understanding is thin. Nothing here decides anything.

Sourced from the cd\\ai blueprint corpus, Appendix B (Ambiguity & Context
Engine), reviewed 2026-08-20. The math and the component decomposition are
the blueprint's; the data bindings are Jasper's. Deliberately NOT taken:
that corpus's governance machinery (Compliance Cortex, GSAL, the MCP cycle,
capability lifecycle). This is an epistemic instrument, not a governance
kernel - see task #406 and #411.

THE INVARIANT THAT MATTERS (task #410)
--------------------------------------
SIGNAL ONLY. This module measures; it never resolves, gates, routes, or
mutates. It takes no write path to the database. Callers may consume its
output; nothing here obliges them to. The reason is not ceremony: it is that
the part of a system which is unsure must never be the part that signs off,
and Jasper already reached this independently via suggest-only claim
resolution (#155/#319) and the no-write `ambiguous` verdict.

THE TWO GATES CONVENTION (task #410, adopted 2026-08-21)
--------------------------------------------------------
This module is GATE A - Contextual Sufficiency. There is a second,
INDEPENDENT gate: GATE B - Action Authority, which is
workgraph_store.resolve_required_approval() (task #317). Both must pass.
Neither can substitute for the other. Full statement, including why each
is a distinct kind of question, in
docs/design/GATES_FEDERATION_AND_MECHANISM_TRIAGE.md section 1.

The distinction that keeps Gate B from becoming an authority model: an
authority model adjudicates EVIDENCE ("which source wins"). Gate B
adjudicates JASPER'S OWN PERMISSIONS ("may I act unattended"). One is a
claim about the world; the other is a claim about what this software is
allowed to do on the user's behalf. Gate B must never acquire opinions
about the first, and this module must never acquire the power to do the
second.

Three rules that make the convention enforceable, not decorative:

  R1  NO SCORE-TO-ACTION MAPPING, EVER. There must be no threshold of the
      form `if ambiguity < X: auto_approve()`. That single line is how a
      measurement becomes an authority. This module's only outputs are a
      measurement and a set of NAMED GAPS. A gap is closed by seeking
      evidence or by asking a person - never by clearing a bar.
  R2  GATES SUBTRACT, NEVER ADD. Each gate may only block or escalate.
      Neither GRANTS permission; passing both is the minimum for an action
      already configured as permitted. No combination of measurements can
      promote an action to permitted.
  R3  ESCALATION CARRIES EVIDENCE, NOT A VERDICT. What reaches the human is
      the assembled evidence, the named gap, and the alternatives - not
      Jasper's recommendation of what is true.

Written down because today it is true BY ACCIDENT, not by construction:
this module is advisory only because nothing consumes it yet, and Gate B
ignores evidence only because nothing hands it any. The moment the two are
wired together the natural implementation violates R1 and R2 at once and
would read like a sensible refactor. This comment exists so that it reads
as a violation instead.

`low ambiguity != automatic action.`

Corollary, and the reason ABSTENTION is the centrepiece rather than a
footnote: a component with no data source MUST abstain, not return 0.0.
Returning 0.0 for contradiction would read as "no contradictions found"
when the truth is "we have never looked." The blueprint's own failure-mode
table says halt and emit telemetry on missing input, never guess. Jasper has
the same discipline elsewhere as "no silent caps."

WHAT IS ACTUALLY COMPUTABLE TODAY
---------------------------------
Verified against the live schema 2026-08-20, not assumed from the presence
of code:

  freshness              COMPUTABLE - raw_items.occurred_ts
  provenance_reliability ABSTAINS    - EXCLUDED BY DESIGN, not a data gap.
                                      An earlier draft scored per-source trust
                                      (sharepoint .90 / calendar .80 / mail .60
                                      / teams .40); that is an authority model
                                      in floats and was removed in 0c3de45.
                                      Provenance is CARRIED with the evidence,
                                      never scored. Two regression tests in
                                      tests/test_workgraph_ambiguity.py assert
                                      it cannot come back. (This line read
                                      "COMPUTABLE - raw_items.source + declared
                                      trust map" until 2026-08-21, describing
                                      the removed design and contradicting both
                                      _abstaining_components() and its own
                                      guard test.)
  referential_ambiguity  COMPUTABLE - identity_anchors(anchor_type='reference')
                                      + raw_items.pr_number
  context_coverage       CONDITIONAL - computes ONLY when Marc has written
                                      config/required_context.json declaring
                                      which data points a category must have.
                                      With no declaration it self-abstains, so
                                      the default behaviour is identical to
                                      before #394. Deliberately config, not
                                      code: even a completeness checklist is a
                                      domain judgement, and it is his. #394.
  contradiction          ABSTAINS    - claim_edges is EMPTY (0 rows): the #314
  internal_consistency   ABSTAINS      reconciliation taxonomy has never fired.
                                      See #412.
  semantic_polysemy      ABSTAINS    - needs embeddings; no sanctioned provider
  embedding_dispersion   ABSTAINS      (cdai-kernel's is OpenAI-only, which is
  relevance              ABSTAINS      not viable for Lilly content). See #405.

Three of ten always compute, a fourth (context_coverage) computes only once
Marc declares what complete means, and six abstain. That is a real answer, not
a partial failure: the blueprint's aggregation is equal-weighted, so an
aggregate over the computable subset is well-defined as long as the abstentions
travel with it - which they do, in `abstained`.

DETERMINISM
-----------
Same inputs -> same outputs, always. `now_ts` is an explicit parameter and is
echoed back in the result, so a stored result can be recomputed and compared
exactly. Nothing here samples, learns, adapts, or calls a model. There is no
LLM cost to running this.
"""
from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Optional

import paths
import workgraph_store as ws

# ---------------------------------------------------------------- constants --

#: Blueprint Appendix B 5.11.5 canonical decay constant, in days. Note this
#: happens to suit Jasper's corpus well - the live evidence window is roughly
#: 100 days (2026-05-12..2026-08-20 at the 5th percentile), so a 90-day decay
#: puts the bulk of real evidence on the informative part of the curve rather
#: than saturated at either end.
FRESHNESS_TAU_DAYS = 90.0

#: Blueprint Appendix B 5.12.2 canonical volatility saturation.
VOLATILITY_V_MAX = 0.25

#: NOTE ON SOURCE TRUST - deliberately absent, and this is a design decision
#: rather than a missing feature.
#:
#: The blueprint's provenance_reliability component is a mean over externally
#: ASSIGNED per-source trust scores. An earlier draft of this module shipped a
#: default map (sharepoint 0.90 / calendar 0.80 / outlook_mail 0.60 /
#: teams_chat 0.40). That was wrong on three counts and has been removed:
#:
#:   1. It is an authority model - a declared precedence over evidence classes,
#:      expressed per channel instead of per claim type. Marc explicitly
#:      abandoned that design: Jasper does not arbitrate which source wins. It
#:      measures what is unclear, seeks the missing context, and where it still
#:      cannot be settled it hands the determination to the human WITH the
#:      evidence assembled.
#:   2. It grades the transport, not the content. A signed contract attached to
#:      an email is 'outlook_mail'; an offhand remark in a SharePoint comment is
#:      'sharepoint'. The channel carries almost no evidentiary weight.
#:   3. It violated this module's own abstention rule - inventing values for a
#:      component with no real data source is exactly what abstention exists to
#:      prevent. Empirically it also produced a flat 0.6 on every project
#:      measured, contributing a uniform constant to every score.
#:
#: In this design provenance is CARRIED, not SCORED: it travels with evidence
#: and with gaps so a human can weigh it. Weighing is the human's job.
#:
#: If Lilly ever declares real per-source trust as policy, this component can
#: be revived - it would then be a genuine external input rather than a number
#: invented here.


# ------------------------------------------------------------------ results --

@dataclass(frozen=True)
class Component:
    """One measured component, or a recorded abstention.

    `value` is None exactly when `abstained_reason` is set. There is no third
    state and no default-to-zero.
    """
    name: str
    value: Optional[float]
    abstained_reason: Optional[str] = None
    detail: dict = field(default_factory=dict)

    @property
    def abstained(self) -> bool:
        return self.value is None

    @classmethod
    def measured(cls, name: str, value: float, **detail) -> "Component":
        return cls(name=name, value=_clamp(value), detail=detail)

    @classmethod
    def abstain(cls, name: str, reason: str, **detail) -> "Component":
        return cls(name=name, value=None, abstained_reason=reason, detail=detail)


@dataclass(frozen=True)
class Gap:
    """A specific, named hole in the understanding - the useful output.

    `kind` is a stable machine token. `what` says what is missing in Marc's
    terms. `fillable_by` names where the answer could come from, which is what
    turns a measurement into either a targeted question (#397) or a line in an
    escalation package (#399). It is deliberately NOT a decision about truth.
    """
    kind: str
    what: str
    fillable_by: str
    ref: Optional[str] = None


@dataclass(frozen=True)
class AmbiguitySignal:
    project_id: str
    ambiguity_score: Optional[float]      # None when everything abstained
    components: tuple[Component, ...]
    gaps: tuple[Gap, ...]
    now_ts: float
    n_claims: int
    n_raw_items: int
    #: Provenance CARRIED, not scored - counts of evidence per source, so a
    #: human can weigh where this understanding came from. Deliberately not
    #: folded into ambiguity; see the source-trust note at the top of this file.
    source_mix: dict = field(default_factory=dict)

    @property
    def abstained(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.components if c.abstained)

    @property
    def measured(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.components if not c.abstained)

    def as_dict(self) -> dict:
        """Plain-dict form for storage/telemetry. Round-trippable."""
        return {
            "project_id": self.project_id,
            "ambiguity_score": self.ambiguity_score,
            "now_ts": self.now_ts,
            "n_claims": self.n_claims,
            "n_raw_items": self.n_raw_items,
            "source_mix": dict(self.source_mix),
            "measured": list(self.measured),
            "abstained": list(self.abstained),
            "components": [
                {"name": c.name, "value": c.value,
                 "abstained_reason": c.abstained_reason, "detail": c.detail}
                for c in self.components
            ],
            "gaps": [
                {"kind": g.kind, "what": g.what, "fillable_by": g.fillable_by, "ref": g.ref}
                for g in self.gaps
            ],
        }


# ------------------------------------------------------------------ helpers --

def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _read_project_evidence(project_id: str) -> tuple[list, list]:
    """Read-only. Returns (claims, raw_items) for every member of the project.

    Narrow SQL lives here rather than in workgraph_store.py deliberately: this
    module is additive and self-contained so it can be reviewed and reverted as
    one unit, and it takes no write path. If this becomes load-bearing these
    two queries should migrate into workgraph_store.py alongside the other
    readers.
    """
    conn = ws._connect()
    try:
        member_ids = [
            r["id"] for r in conn.execute(
                "SELECT id FROM work_objects WHERE parent_id = ?", (project_id,)
            ).fetchall()
        ]
        if not member_ids:
            return [], []
        q = ",".join("?" for _ in member_ids)
        claims = conn.execute(
            f"SELECT id, raw_item_id, claim_type, status, text FROM claims WHERE issue_id IN ({q})",
            member_ids,
        ).fetchall()
        raw_ids = sorted({c["raw_item_id"] for c in claims if c["raw_item_id"]})
        raw_items = []
        if raw_ids:
            rq = ",".join("?" for _ in raw_ids)
            raw_items = conn.execute(
                f"SELECT id, source, occurred_ts, pr_number FROM raw_items WHERE id IN ({rq})",
                raw_ids,
            ).fetchall()
        return list(claims), list(raw_items)
    finally:
        conn.close()


#: Task #394. Where Marc's required-context declaration lives. Deliberately in
#: CONFIG_DIR, not in this repo: it is HIS statement about what a complete
#: record looks like for his work, not a rule Jasper authored. `config/` is
#: gitignored, so the real file never ships; the committed
#: `config/required_context.example.json` shows the shape.
REQUIRED_CONTEXT_FILENAME = "required_context.json"


def load_required_context() -> dict:
    """Task #394. Marc's declaration: per project category, which data points
    must be present for the record to count as complete.

    Returns {} when the file is absent or unreadable, and {} means
    context_coverage KEEPS ABSTAINING exactly as it does today. That default is
    the whole safety property - this feature is inert until Marc writes the
    file, so shipping it changes nothing until he decides what "complete" means.

    WHY THIS IS NOT AN AUTHORED RESOLVER. The standing rule is that Jasper never
    decides what is TRUE or which source WINS. A required-context declaration
    makes neither kind of claim: it says which FIELDS a category of work should
    have on file, so a missing one can be NAMED as a gap and gone looking for.
    It is a completeness checklist, not a truth arbiter - the same posture as
    localize_gaps itself. The reason it is config rather than code is that even
    a completeness checklist is a domain judgement, and that judgement is
    Marc's. This module's own note says inventing one SILENTLY would be worse
    than abstaining; an explicit, human-owned, reviewable file is the answer to
    that, not a constant in this file.
    """
    path = paths.CONFIG_DIR / REQUIRED_CONTEXT_FILENAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    # Normalise: category -> list[str]. Anything malformed is dropped rather
    # than guessed at, and a dropped entry simply means that category keeps
    # abstaining.
    out = {}
    for category, fields in raw.items():
        if isinstance(fields, list) and all(isinstance(f, str) for f in fields):
            out[str(category)] = [f for f in fields if f.strip()]
    return out


def compute_context_coverage(category: Optional[str], present_points: set,
                             declaration: dict) -> Component:
    """Task #394. What fraction of the declared-required context is on file?

    Reported as AMBIGUITY (badness), consistent with referential_ambiguity: 0.0
    means everything required is present, 1.0 means none of it is.

    Abstains - never returns 0.0 - when there is no declaration at all, or none
    for this category, or the category is unknown. A 0.0 would read as "nothing
    is missing", when the truth is "nobody has said what should be here."
    """
    if not declaration:
        return Component.abstain(
            "context_coverage",
            "no required-context declaration exists (config/required_context.json "
            "is absent or empty), so 'complete' is undefined - see #394",
            unblocked_by="Marc writing config/required_context.json",
        )
    if not category:
        return Component.abstain(
            "context_coverage",
            "this work object has no category, so no required-context list applies",
            unblocked_by="categorising the work object",
        )
    required = declaration.get(category)
    if not required:
        return Component.abstain(
            "context_coverage",
            f"the declaration names no required context for category {category!r}",
            unblocked_by=f"adding a {category!r} entry to config/required_context.json",
        )
    missing = [f for f in required if f not in present_points]
    return Component(
        name="context_coverage",
        value=_clamp(len(missing) / len(required)),
        detail={"category": category, "required": list(required),
                "present": sorted(present_points & set(required)),
                "missing": missing},
    )


def _read_present_data_points(project_id: str) -> set:
    """Which confirmed data-point NAMES have a value on this project or any of
    its members? Names, not ids, so the declaration can be written in Marc's
    words rather than in `dp-fasttrack-*` internal keys."""
    conn = ws._connect()
    try:
        ids = [project_id] + [r["id"] for r in conn.execute(
            "SELECT id FROM work_objects WHERE parent_id = ?", (project_id,)).fetchall()]
        q = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"""SELECT DISTINCT d.name FROM data_point_values v
                JOIN data_point_definitions d ON d.id = v.definition_id
                WHERE v.work_object_id IN ({q})""", ids).fetchall()
        return {r["name"] for r in rows}
    finally:
        conn.close()


def _read_state_incoherences(project_id: str) -> list:
    """Read-only. Claims on this project flagged as inconsistent with their own
    issue's recorded state - currently the 'issue is closed but this claim is
    still open' case produced by the existing reconciliation sweep.

    NOTE this is NOT the blueprint's contradiction signal. See the long note on
    compute_state_coherence for why they are different measurements.
    """
    conn = ws._connect()
    try:
        rows = conn.execute(
            """SELECT p.claim_id, p.evidence_type, p.evidence_note, c.issue_id
                 FROM pending_claim_suggestions p
                 JOIN claims c ON c.id = p.claim_id
                 JOIN work_objects w ON w.id = c.issue_id
                WHERE w.parent_id = ?
                  AND p.suggestion_kind = 'contradiction'
                  AND p.status = 'pending'""",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _read_reference_anchors(project_id: str) -> set:
    """Active reference-type identity anchors reachable from this project."""
    conn = ws._connect()
    try:
        rows = conn.execute(
            """SELECT DISTINCT a.normalized_value
                 FROM identity_anchors a
                 JOIN work_objects w ON a.issue_id = w.id
                WHERE w.parent_id = ?
                  AND a.anchor_type = 'reference'
                  AND a.status = 'active'""",
            (project_id,),
        ).fetchall()
        return {r["normalized_value"] for r in rows if r["normalized_value"]}
    finally:
        conn.close()


# --------------------------------------------------------------- components --

def compute_freshness(raw_items: list, now_ts: float) -> Component:
    """Blueprint B.5.11.5: freshness_i = exp(-dt_i / tau); aggregate = mean.

    Reported as FRESHNESS (1.0 = fresh). The aggregate below inverts it, since
    ambiguity rises as evidence goes stale.
    """
    ages = [
        (now_ts - r["occurred_ts"]) / 86400.0
        for r in raw_items
        if r["occurred_ts"] is not None
    ]
    ages = [a for a in ages if a >= 0]  # future-dated rows are not "fresh", they're bad data
    if not ages:
        return Component.abstain(
            "freshness", "no raw_items with a usable occurred_ts",
            n_considered=len(raw_items),
        )
    vals = [math.exp(-a / FRESHNESS_TAU_DAYS) for a in ages]
    return Component.measured(
        "freshness", sum(vals) / len(vals),
        n=len(vals), median_age_days=round(sorted(ages)[len(ages) // 2], 1),
    )


def compute_source_mix(raw_items: list) -> dict:
    """NOT a score. Plain provenance, carried so a human can weigh it.

    Returns the count of evidence items per source. This is descriptive
    metadata attached to the signal - it is deliberately not folded into
    ambiguity, because ranking sources against each other is a judgment about
    authority and Jasper does not make those (see the source-trust note above).
    """
    mix: dict = {}
    for r in raw_items:
        src = r["source"] or "unknown"
        mix[src] = mix.get(src, 0) + 1
    return mix


def compute_referential_ambiguity(raw_items: list, resolved_refs: set) -> Component:
    """Blueprint B.8: U / max(R, 1) over references appearing on this project.

    R = distinct reference tokens mentioned. U = those with no active
    reference-type identity anchor, i.e. mentioned but never resolved to
    anything Jasper tracks.
    """
    mentioned = {str(r["pr_number"]).strip() for r in raw_items if r["pr_number"]}
    if not mentioned:
        # Per spec this is 0.0 ambiguity, but say so explicitly so a consumer
        # can tell "0 of 0" from "0 of many".
        return Component.measured("referential_ambiguity", 0.0, n_referenced=0, n_unresolved=0)
    normalized_resolved = {str(v).strip() for v in resolved_refs}
    unresolved = sorted(m for m in mentioned if m not in normalized_resolved)
    return Component.measured(
        "referential_ambiguity", len(unresolved) / max(len(mentioned), 1),
        n_referenced=len(mentioned), n_unresolved=len(unresolved),
        unresolved=unresolved[:20],
    )


def compute_state_coherence(claims: list, incoherences: list) -> Component:
    """Task #412. NOT a blueprint component - a tenth, Jasper-specific one,
    named honestly rather than borrowing a blueprint label it does not match.

    WHY IT IS SEPARATE. The blueprint's `contradiction` evaluates all
    declarative statement PAIRS and asks whether two statements conflict
    semantically ("the contract renews Oct 31" vs "we're covered through
    December"). Jasper has no source for that: claim_edges is empty, and
    deliberately so - the store's own schema comment records that none of its
    four edge types has a production writer, following an explicit "don't build
    a producer nothing calls yet" discipline, with Evidence Assembly's conflict
    detection named as the intended future producer. That has not changed, so
    `contradiction` and `internal_consistency` still abstain.

    WHAT IS REAL. A different inconsistency IS already detected, populated, and
    deterministic: an issue recorded as closed while claims on it remain open.
    43 such rows exist live, all evidence_type='issue_closed_with_open_claims'.
    That is a genuine incoherence in the graph's own recorded state - and it is
    directly actionable, because each one is a precise question ("this issue is
    closed but three commitments on it are still open - did they complete, or
    should the issue reopen?").

    Reported as COHERENCE (1.0 = coherent), inverted by the aggregate like
    freshness. Denominator is claims, not pairs - stated plainly here so nobody
    later mistakes this for the blueprint's C/P ratio.
    """
    if not claims:
        return Component.abstain("state_coherence", "no claims on this project")
    flagged = {r["claim_id"] for r in incoherences}
    issues = sorted({r["issue_id"] for r in incoherences})
    return Component.measured(
        "state_coherence", 1.0 - (len(flagged) / len(claims)),
        n_claims=len(claims), n_flagged=len(flagged), issues=issues[:20],
    )


def _abstaining_components(exclude: Optional[set] = None) -> list[Component]:
    """The components with no viable data source today - seven by default, six
    when context_coverage is supplied by the caller (task #394).

    `exclude` (task #394): names the caller is supplying itself because a real
    source now exists. Only context_coverage can be excluded today, and only
    when Marc has written a required-context declaration.

    Each records WHY, and which task would unblock it. They are returned as
    real components rather than omitted so that a consumer sees the full
    ten-component contract and cannot mistake a partial score for a whole one.
    """
    exclude = exclude or set()
    return [c for c in [
        Component.abstain(
            "provenance_reliability",
            "EXCLUDED BY DESIGN, not a data gap. Scoring per-source trust is an "
            "authority model - it would have Jasper rank which source wins, and "
            "Jasper does not arbitrate truth. Provenance is carried with the "
            "evidence and the gaps so the human can weigh it.",
            unblocked_by="design decision, not data - revive only if Lilly declares real per-source trust as policy",
        ),
        Component.abstain(
            "context_coverage",
            "no required-context declaration exists; data_point_definitions has "
            "no category->required mapping. Would have to be invented. See #394.",
            unblocked_by="#394 required-context declaration",
        ),
        Component.abstain(
            "contradiction",
            "no source for pairwise SEMANTIC contradiction. claim_edges is empty "
            "BY DESIGN (the store's schema comment records that none of its four "
            "edge types has a production writer, deliberately, with Evidence "
            "Assembly conflict detection as the intended future producer), so 0.0 "
            "would falsely mean 'no contradictions found'. The 43 live "
            "'contradiction' claim-suggestions are a DIFFERENT thing - lifecycle "
            "state incoherence - and are measured as state_coherence instead of "
            "being mislabelled as this.",
            unblocked_by="Evidence Assembly conflict detection (Section 8.1)",
        ),
        Component.abstain(
            "internal_consistency",
            "blueprint-defined as 1 - contradiction(context only); inherits "
            "contradiction's missing source. See state_coherence for the real, "
            "narrower signal Jasper does have.",
            unblocked_by="Evidence Assembly conflict detection (Section 8.1)",
        ),
        Component.abstain(
            "semantic_polysemy", "requires embeddings; no sanctioned provider.",
            unblocked_by="#405",
        ),
        Component.abstain(
            "embedding_dispersion", "requires embeddings; no sanctioned provider.",
            unblocked_by="#405",
        ),
        Component.abstain(
            "relevance", "requires embeddings; no sanctioned provider.",
            unblocked_by="#405",
        ),
    ] if c.name not in exclude]


# --------------------------------------------------------- gap localization --

def localize_gaps(components: list, raw_items: list, claims: list) -> list:
    """Task #393 - the actually useful output.

    Turns measurements into named, addressable holes. Each gap says what is
    missing and where the answer could come from; it never says what is true.
    Deliberately conservative: only emits a gap it can point at concretely.
    """
    gaps: list[Gap] = []
    by_name = {c.name: c for c in components}

    ref = by_name.get("referential_ambiguity")
    if ref and not ref.abstained:
        for token in ref.detail.get("unresolved", []):
            gaps.append(Gap(
                kind="unresolved_reference",
                what=f"reference {token} is mentioned but resolves to nothing Jasper tracks",
                fillable_by="search the source systems for this reference, or ask the sender",
                ref=token,
            ))

    fresh = by_name.get("freshness")
    if fresh and not fresh.abstained and fresh.value < 0.25:
        gaps.append(Gap(
            kind="stale_evidence",
            what=(f"the newest evidence here is old (median age "
                  f"{fresh.detail.get('median_age_days')} days) - current state may have moved"),
            fillable_by="ask the people involved whether anything has changed",
        ))

    coh = by_name.get("state_coherence")
    if coh and not coh.abstained and coh.detail.get("n_flagged"):
        for issue_id in coh.detail.get("issues", []):
            gaps.append(Gap(
                kind="closed_issue_with_open_claims",
                what=(f"issue {issue_id} is recorded as closed while claims on it are "
                      f"still open - its recorded state contradicts its own contents"),
                fillable_by="confirm whether those claims completed, or reopen the issue",
                ref=issue_id,
            ))

    # Task #394: each declared-but-absent field becomes its own named gap, so
    # workgraph_seek can enumerate sources and ask for that specific thing
    # rather than for "more context" in the abstract.
    cov = by_name.get("context_coverage")
    if cov and not cov.abstained:
        for field_name in cov.detail.get("missing", []):
            gaps.append(Gap(
                kind="missing_required_context",
                what=(f"{field_name} is declared required for "
                      f"{cov.detail.get('category')!r} work but is not on file here"),
                fillable_by=("search this project's evidence, read its documents, "
                             "or ask the counterparty"),
                ref=field_name,
            ))

    if claims and not raw_items:
        gaps.append(Gap(
            kind="claims_without_evidence",
            what=f"{len(claims)} claim(s) here trace to no retrievable source item",
            fillable_by="re-check ingestion for the underlying items",
        ))

    return gaps


# ------------------------------------------------------------------ measure --

#: Below this absolute change, an acquisition is treated as having taught us
#: nothing. Blueprint Appendix B does not name a dead-band (it emits the raw
#: delta), but Jasper needs one to tell "source exhausted" apart from "moved a
#: little", which is the whole point of the three-way split below.
TREND_DEADBAND = 0.02


@dataclass(frozen=True)
class Trend:
    """Task #395. NOT just the blueprint's scalar delta.

    Blueprint Appendix B 5.12.1 emits uncertainty_trend = clamp(A_t - A_t-1)
    and stops there. That is sufficient for cd\\ai, where the artifact is fixed
    and rising ambiguity means the remediation loop is diverging - a problem.
    It is NOT sufficient for Jasper, where the only way to reduce ambiguity is
    to ACQUIRE more context, and where three situations a scalar conflates
    demand opposite responses:

      improving  - ambiguity fell: the right context arrived. Continue.
      conflict   - ambiguity ROSE: contradictory context arrived. This is a
                   real disagreement DISCOVERED, not a failure. Surface it;
                   do NOT keep grinding. Under cd\\ai's semantics this would
                   read as divergence and trigger a halt-as-failure, which
                   would suppress exactly the finding Jasper most wants.
      exhausted  - ambiguity unchanged within the dead-band: that source had
                   nothing to add. Switch modality or stop.
      unknown    - fewer than 2 observations, or a null score in the pair.

    `delta` is still reported raw so the blueprint-conformant number is
    available; `state` is the Jasper-specific interpretation.
    """
    state: str                      # improving | conflict | exhausted | unknown
    delta: Optional[float]          # A_t - A_t-1, clamped to [-1, 1]
    volatility: Optional[float]     # [0,1], None with < 3 observations
    n_observations: int


def compute_uncertainty_trend(history: list, current_score: Optional[float]) -> Trend:
    """`history` is newest-first (as returned by ws.list_ambiguity_observations)
    and is the history BEFORE `current_score`. Deterministic; no I/O.
    """
    scores = [h["ambiguity_score"] for h in history if h.get("ambiguity_score") is not None]

    # Blueprint missing-history rule: fewer than 2 points -> no trend. A null
    # current score also yields unknown rather than a fabricated delta.
    if current_score is None or not scores:
        return Trend(state="unknown", delta=None,
                     volatility=_volatility(scores), n_observations=len(scores))

    delta = _clamp(current_score - scores[0], -1.0, 1.0)
    if delta < -TREND_DEADBAND:
        state = "improving"
    elif delta > TREND_DEADBAND:
        state = "conflict"
    else:
        state = "exhausted"
    return Trend(state=state, delta=round(delta, 4),
                 volatility=_volatility([current_score] + scores),
                 n_observations=len(scores))


def _volatility(scores: list) -> Optional[float]:
    """Blueprint 5.12.2: stddev over the last min(5, N), normalised by V_max.
    Missing-history rule: fewer than 3 observations -> None (not 0.0, which
    would read as 'measured, and stable')."""
    window = scores[:5]
    if len(window) < 3:
        return None
    return _clamp(statistics.stdev(window) / VOLATILITY_V_MAX)


#: How many consecutive no-movement passes before we call it spinning. Two is
#: deliberately conservative: one flat pass can happen because the ONE source
#: consulted had nothing, which is not the same as nothing being left to learn.
SPINNING_AFTER_FLAT_PASSES = 2

#: Ambiguity below this is treated as "good enough to act on" for the purpose
#: of projecting passes remaining. Not a gate - #400 is deferred and this
#: module gates nothing; it is only the target the projection aims at.
CONVERGENCE_TARGET = 0.15


@dataclass(frozen=True)
class Forecast:
    """Task #409, the PCM pattern (cd\\ai blueprint Appendix G).

    The property that makes this worth having is Appendix G's own line: "No
    model invocation required." This is pure arithmetic over a bounded history
    window, so asking "is another pass likely to pay?" costs nothing, while the
    pass it might save costs a real LLM call. Given how much of Jasper's
    backlog is LLM-cost-gated, a free forecast is worth real money.

    ADVISORY ONLY, exactly as PCM specifies (Theorem G.9.1: "PCM cannot
    authorize or execute"). This returns a recommendation and a reason. It
    does not gate, does not skip, does not decide. A caller is free to spend
    anyway - and should, if it has a reason this does not know about.
    """
    recommendation: str        # spend | spinning | surface_conflict | no_history
    reason: str
    contraction_ratio: Optional[float]   # <1.0 = converging, >=1.0 = not
    projected_passes: Optional[int]      # to reach CONVERGENCE_TARGET
    n_observations: int


def forecast_next_pass(history: list, current_score: Optional[float]) -> Forecast:
    """`history` newest-first, as ws.list_ambiguity_observations returns it.
    Deterministic, no I/O, no model call.
    """
    scores = [h["ambiguity_score"] for h in history if h.get("ambiguity_score") is not None]
    n = len(scores)

    if current_score is None or n == 0:
        # Never looked, or nothing measurable. You must look at least once -
        # you cannot forecast from no data. Labelled honestly rather than
        # silently defaulting to "spend".
        return Forecast(recommendation="no_history",
                        reason="no prior observations to forecast from - a first pass is warranted",
                        contraction_ratio=None, projected_passes=None, n_observations=n)

    trend = compute_uncertainty_trend(history, current_score)

    if trend.state == "conflict":
        # Rising ambiguity means contradictory context arrived. More passes
        # over the same contradiction will not resolve it - a person will.
        return Forecast(recommendation="surface_conflict",
                        reason=(f"ambiguity rose by {trend.delta} - contradictory context was "
                                f"acquired, which is a finding to surface rather than grind on"),
                        contraction_ratio=None, projected_passes=None, n_observations=n)

    # Spinning: the last N transitions all sat inside the dead-band.
    series = [current_score] + scores
    flat_run = 0
    for newer, older in zip(series, series[1:]):
        if abs(newer - older) <= TREND_DEADBAND:
            flat_run += 1
        else:
            break
    if flat_run >= SPINNING_AFTER_FLAT_PASSES:
        return Forecast(recommendation="spinning",
                        reason=(f"{flat_run} consecutive passes moved ambiguity by no more than "
                                f"{TREND_DEADBAND} - the sources being consulted have nothing left "
                                f"to add; change modality or stop"),
                        contraction_ratio=1.0, projected_passes=None, n_observations=n)

    # Appendix G.2.1 contraction estimate, over the same bounded window.
    ratios = [newer / older for newer, older in zip(series, series[1:]) if older > 0]
    if not ratios:
        return Forecast(recommendation="spend",
                        reason="no usable contraction estimate; one more pass is reasonable",
                        contraction_ratio=None, projected_passes=None, n_observations=n)
    r = sum(ratios) / len(ratios)

    if r >= 1.0:
        # Same minimum-evidence bar as the flat-run check above, deliberately.
        # Without it the two paths contradict each other: the flat-run guard
        # says one non-moving pass is not enough to conclude anything, and then
        # this would immediately conclude it anyway from that same single data
        # point. Erring toward one more pass is the cheaper mistake - wrongly
        # abandoning a project costs more than one call.
        if len(ratios) < SPINNING_AFTER_FLAT_PASSES:
            return Forecast(recommendation="spend",
                            reason=(f"contraction ratio {round(r, 3)} is not yet shrinking, but "
                                    f"{len(ratios)} observation(s) is too little to call it spinning"),
                            contraction_ratio=round(r, 3), projected_passes=None, n_observations=n)
        return Forecast(recommendation="spinning",
                        reason=f"contraction ratio {round(r, 3)} >= 1.0 - ambiguity is not shrinking",
                        contraction_ratio=round(r, 3), projected_passes=None, n_observations=n)

    projected = None
    if current_score > CONVERGENCE_TARGET and 0 < r < 1.0:
        projected = max(1, math.ceil(math.log(CONVERGENCE_TARGET / current_score) / math.log(r)))
    return Forecast(recommendation="spend",
                    reason=(f"contraction ratio {round(r, 3)} - ambiguity is shrinking"
                            + (f", ~{projected} pass(es) to reach {CONVERGENCE_TARGET}"
                               if projected else ", already at or below target")),
                    contraction_ratio=round(r, 3), projected_passes=projected, n_observations=n)


def measure_project(
    project_id: str,
    *,
    now_ts: Optional[float] = None,
) -> AmbiguitySignal:
    """Measure one project. Read-only, deterministic, no LLM, no writes.

    `ambiguity_score` aggregates the COMPUTABLE components with equal weight
    (blueprint B.9 keeps all components equally weighted so that none can
    dominate or suppress another), and is None if every component abstained.
    Freshness and state_coherence are inverted on the way in, since both are
    reported as goodness while the aggregate is badness. (This said "Freshness
    and provenance" until 2026-08-21 - a leftover from the per-source trust
    model removed in 0c3de45; provenance_reliability abstains and so is never
    inverted. The live set is `inverted` below, which is the authority.)
    """
    if now_ts is None:
        now_ts = time.time()

    claims, raw_items = _read_project_evidence(project_id)
    resolved_refs = _read_reference_anchors(project_id)

    # Task #394: context_coverage becomes REAL only when Marc has declared what
    # complete means for this category. With no declaration the call abstains
    # and the component set is byte-identical to what it was before #394.
    declaration = load_required_context()
    proj = ws.get_issue_or_cluster(project_id) or {}
    coverage = compute_context_coverage(
        proj.get("category"),
        _read_present_data_points(project_id) if declaration else set(),
        declaration)

    components = [
        compute_freshness(raw_items, now_ts),
        compute_referential_ambiguity(raw_items, resolved_refs),
        compute_state_coherence(claims, _read_state_incoherences(project_id)),
        coverage,
    ] + _abstaining_components(exclude={"context_coverage"})

    # Components reported as goodness must be inverted to contribute to an
    # ambiguity (badness) aggregate.
    inverted = {"freshness", "state_coherence"}
    contributions = [
        (1.0 - c.value) if c.name in inverted else c.value
        for c in components
        if not c.abstained
    ]
    score = round(sum(contributions) / len(contributions), 4) if contributions else None

    return AmbiguitySignal(
        project_id=project_id,
        ambiguity_score=score,
        components=tuple(components),
        gaps=tuple(localize_gaps(components, raw_items, claims)),
        now_ts=now_ts,
        n_claims=len(claims),
        n_raw_items=len(raw_items),
        source_mix=compute_source_mix(raw_items),
    )
