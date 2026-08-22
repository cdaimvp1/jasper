# Gates, Federation Commitments, and Mechanism Triage

**Status: DESIGN ONLY. Nothing here is built.** Written 2026-08-21 at Marc's
request, after reviewing the "Jasper Ingestion Flow & Federated Work Graph"
diagram and extracting the three ideas in it that are sharper than anything
currently recorded in this repo.

Marc's framing when commissioning this: *"can you design it keeping in mind my
rule around authority model... or whatever we discussed earlier that i shot
down."*

---

## 0. The rule this document is designed against

The standing constraint, restated so every section below can be checked
against it:

> **Never add a rule, table, map, or score that lets Jasper decide what is
> true, which source wins, or what a term means.**

Instead: **measure** ambiguity, **localize** gaps, **seek** the missing
context (search records, or ask the people involved), and where the gap cannot
be closed even after asking, **escalate the determination to the human with
the evidence assembled.**

Things already shot down under this rule, listed because two of the three
designs below have a short path back to them:

1. **The authority model** — declared precedence over evidence classes per
   claim type. Proposed in the Big Bet transcript; Marc's next message opened
   with "No..."
2. **`DEFAULT_SOURCE_TRUST`** — sharepoint 0.90 / calendar 0.80 /
   outlook_mail 0.60 / teams_chat 0.40. An authority model expressed in
   floats, justified with "executed document > stated intent > informal chat,"
   which is the precedence argument verbatim. Removed in `0c3de45`; two
   regression tests now assert it cannot return.
3. **GSAL / governance sufficiency** — compliance adjudication against a
   rulebook Jasper does not have and cannot author per domain.
4. **A sense-scoped glossary as a precondition** — backwards. "Neither Finance
   nor Commercial necessarily has to win. Context disambiguates meaning."
5. **A supplier+stakeholder correlation rule** (2026-08-21) — would have
   dropped 16 genuine siblings below the candidate gate. Measured, not
   theorised.

**The test for anything in this document:** does it let Jasper conclude
something a human did not, or rank one input above another? If yes, it is
wrong regardless of how reasonable it sounds.

---

## 1. The Two Gates convention (task #410)

### The trap, first

"Action **Authority** Gate" sounds exactly like the authority model. It is
not — but only because of a distinction that must be stated explicitly and
kept, or it will drift back:

| | Decides | Source of the decision | Allowed? |
|---|---|---|---|
| Authority model (**forbidden**) | what is TRUE / which source WINS | a precedence table Jasper consults | No |
| Action authority gate (**already built, #317**) | whether Jasper may ACT unattended | config Marc set | Yes |

An authority model adjudicates *evidence*. The authority gate adjudicates
*Jasper's own permissions*. One is a claim about the world; the other is a
claim about what this software is allowed to do on Marc's behalf. Never let
the second acquire opinions about the first.

### The convention to record

Two independent gates. Both must pass. Neither can substitute for the other.

**Gate A — Contextual Sufficiency.** *Is the available evidence enough to
support the conclusion this action rests on?*
- Implemented as **measurement only**: `workgraph_ambiguity.py`.
- Produces components that **abstain** (`value is None` + a reason) rather
  than defaulting to zero, plus **named, localized gaps** (`Gap.kind`,
  `Gap.what`, `Gap.fillable_by`).
- It is **advisory**. It never authorises anything.

**Gate B — Action Authority.** *Given policy, permissions, and consequence, is
this action permitted unattended, or must it escalate?*
- Implemented as a **policy check against configuration Marc controls**
  (#317's `required_approval` gate).
- It never inspects evidence quality. It does not care how confident anything
  is.

**The invariant, in Marc's phrasing from the diagram:**
`low ambiguity ≠ automatic action.`

### The three anti-drift rules

These are the load-bearing part. Without them this design becomes the
authority model within two iterations.

**R1 — No score-to-action mapping, ever.** There must be no threshold of the
form "if sufficiency > X then act." That single line is how a measurement
becomes an authority. Sufficiency's *only* outputs are: proceed to Gate B, or
name a gap. A gap is closed by **seeking evidence** or by **asking a person** —
never by clearing a bar.

**R2 — Gates subtract, never add.** Each gate may only **block** or
**escalate**. Neither can *grant* permission on its own; passing both is the
minimum for an action that is *already* configured as permitted. There is no
combination of measurements that promotes an action to permitted.

**R3 — Escalation carries evidence, not a verdict.** When either gate stops
something, what reaches Marc is the assembled evidence, the named gap, and the
alternatives — not Jasper's recommendation of what is true. This is the
existing "Escalate Prepared Decision" shape and it is the correct one.

### Why write it down if it's mostly already true

Because right now it is **true by accident**. `workgraph_ambiguity.py` is
advisory because it has no consumer; #317's gate ignores evidence because
nothing hands it any. The moment those two get wired together, the natural
implementation is `if ambiguity < threshold: auto_approve()` — which violates
R1 and R2 simultaneously and would look like a sensible refactor. The
convention exists to make that reviewable as a violation instead of an
improvement.

### Scope note

This is a **convention document plus, at most, docstring/comment amendments**
to the two existing modules. It is explicitly *not* a new module, table,
scoring function, or policy engine. If implementing #410 ever requires a new
table, the design has gone wrong.

---

## 2. Federation commitments (constrains #403 / #404, both unbuilt)

The diagram states three commitments more crisply than the ROADMAP does:
**Individualized by Design**, **Context Not Data Dumping**, **No Automatic
Propagation**. They are worth recording now precisely *because* federation is
unbuilt — they constrain the design space before anyone occupies it.

### The trap, first

Federation's authority-model failure mode is specific and severe:

> Two users' graphs disagree about the same shared artifact. Whose wins?

Any answer of the form "the more recent one," "the one from the more
authoritative source," "the owner's," or "the one with higher confidence" is
the authority model, re-derived from scratch in a new context. This is a
*harder* trap than the single-user case because disagreement between two
humans' graphs feels like it demands arbitration.

### C1 — Individualized by design

Each user's graph is theirs. There is no merged global graph, no canonical
cross-user record, and no "the" answer. Federation adds a **node's-eye view**,
never a shared substrate.

*Consequence:* the same artifact may be understood differently by two users,
indefinitely, and that is a correct end state — not an inconsistency to
reconcile.

### C2 — Context, not data dumping

What crosses a federation boundary is **evidence with provenance**, scoped to
the shared thing — not raw replication of one graph into another.

*Consequence:* the receiving graph gets "here is an artifact you also touch,
and here is what your counterpart's evidence says about it, attributed" — the
same shape as any other evidence row. It enters as **input to local
understanding**, with no elevated standing.

### C3 — No automatic propagation

Context flows only where there is **demonstrated shared work**: a shared
artifact, a shared meeting, or a shared tracked relationship. It does not flow
transitively. A↔B sharing a document does not give B anything about A↔C.

*Consequence:* the unit of federation is the **shared object**, not the user
pair. This also bounds blast radius, which matters for a system holding
procurement and legal material.

### C4 — Federation never reconciles disagreement (the one that enforces the rule)

This is the commitment that keeps C1–C3 from collapsing back into an authority
model. **Federation surfaces disagreement as disagreement.** It never picks a
winner, never merges conflicting claims, never scores which graph is more
current or more authoritative.

When two graphs disagree about a shared artifact, the correct behaviour is the
same as the single-user case: **measure the ambiguity, localize the gap, and
escalate to the human with both positions attributed.** Marc's own words from
the transcript are the whole design: *"federation informs local understanding;
it does not replace it."*

A conflicting federated claim is a **gap**, not a fact — and gaps go to people.

---

## 3. Mechanism triage (the genuinely new idea)

The diagram's most useful original contribution is a category most
architecture diagrams do not have:

> **LLM-mediated today, but not inherently an LLM problem.**

That is engineering triage, not presentation. It distinguishes *"this needs a
frontier model's judgment"* from *"we are using an LLM as a connector because
we have no API,"* and those two have completely different remedies.

### The four categories

| Category | Meaning | Remedy |
|---|---|---|
| **Deterministic** | rules, queries, indexes, exact matching, state machines | none needed |
| **Local-model-sufficient** | narrow, well-scoped language work a small open model handles with a structured prompt | cost optimisation |
| **LLM-required** | genuine judgment, nuance, or high-stakes comparison | keep the frontier model |
| **LLM-mediated, shouldn't be** | an LLM is standing in for missing integration | **replace with real integration** — this is a backlog item, not an architecture choice |

### Why the fourth category earns its keep

It converts a permanent-looking architectural fact into a tracked defect. It
also has a live worked example: **calendar was in this bucket this morning and
is not any more.** The relay was an LLM transcribing appointments into drop
files, and it silently lost data — 1,302 events reachable versus 77 ingested,
and dead-lettered payloads reading `"messages_raw": "21 messages fetched"`.
Replacing it with deterministic Outlook COM removed the LLM from the data path
entirely.

Current occupants: **Teams** and **SharePoint**. Teams has no credential-free
API path (no COM interface, and Graph is permanently unavailable at Lilly), so
it stays until that changes. SharePoint has a local OneDrive sync that is at
least partially substitutable.

### The trap, and it is subtle

Nothing in this taxonomy may leak into **evidence** evaluation. Classifying
*how a mechanism works* is fine and useful. Classifying *how much to believe a
fact based on which mechanism produced it* is the trust ladder in a new
costume — "LLM-derived claims are less reliable than deterministic ones" is
`DEFAULT_SOURCE_TRUST` with different labels.

**The line:** this taxonomy applies to **capabilities**, never to **facts**.
It answers "should this code path use a model?" It must never answer "should I
believe this row?"

### Bearing on task #375

#375 is *"add named epistemic-status tiers for evidence."* Read against
section 0, that is **the forbidden artifact**: a named ranking of evidence
believability is an authority model whose tiers happen to be words instead of
floats. Renaming `0.90 / 0.60 / 0.40` to `authoritative / reported / informal`
changes nothing about what it does.

**Recommendation: close #375 as should-not-build,** and note that the
legitimate need underneath it — *"how did we come to know this, and how solid
is the basis?"* — is already served correctly and non-rankingly by:
- `evidence.type` and the three-tier timeline (what kind of record this is),
- `identity_anchors.anchor_strength` (a property of an *identifier*, not a truth claim),
- `workgraph_ambiguity`'s components and gaps (measured properties of the data: freshness, referential ambiguity, state coherence),
- provenance carried, never scored — see `workgraph_ambiguity.py`'s own
  "NOTE ON SOURCE TRUST" block.

The distinction to keep: **describing evidence is fine; ordering it is not.**

---

## 4. What this document deliberately does not adopt

**The autonomous worker loop as drawn in box 5 of the diagram.** Assemble →
evaluate sufficiency → investigate gaps → re-evaluate → execute → escalate.

Not because the shape is wrong — it is a fair picture of the intent — but
because its two terminal steps are inert: `prepared_actions` = 0 rows,
`pending_actions` = 0 rows, `proactive_actions.enabled = False`. Task #400
already defers unifying autonomy gating on exactly this basis: there is
nothing consequential to gate yet. Building the loop before there is a real
action to govern would mean authoring policy against hypothetical
consequences, which is how a governance kernel gets in through the back door.

Revisit when a real, consequential, unattended action exists and needs
gating — not before.

---

## 5. Build order, if and when Marc wants any of it

1. **Section 1 (Two Gates convention)** — cheapest and highest value. A
   convention document plus comment amendments. No new tables, modules, or
   scores. Closes #410.
2. **Section 3 (mechanism triage)** — a ROADMAP section plus the #375 close.
   Documentation only.
3. **Section 2 (federation commitments)** — record now, constrains later.
   Cannot be validated until a second user exists (#404 gated).

None of the above requires touching `find_candidates`,
`_matched_data_points`, the `>= 2` threshold, or `judge_candidates` — so none
of it falls under the ROADMAP's standing grouping guardrail. If a proposed
implementation ever does touch those, it needs the explicit call-out plus
regression-corpus before/after and a live backtest that guardrail requires.
