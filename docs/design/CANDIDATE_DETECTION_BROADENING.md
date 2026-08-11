# Candidate Detection Broadening (task #324)

Design-only investigation. No `.py` file was touched to produce this — the
verification below was run against real code from a throwaway scratch
script, not a committed test.

## 1. The hypothesis, and the concrete answer

Task #324 was deferred until #323 (party/vendor linkage on the
light-synthesis path — `workgraph_synthesis_light.py` now calls
`workgraph_parties.run()`) landed, on the hypothesis that #323 "may turn out
to close most of the gap on its own."

**It does not.** #323 fixes a real, distinct bug (a light-synthesized
issue could have *zero* parties at all, so it never carried *any* company
signal). It does **not** fix the structural gap task #324 was opened for:
a prime/subcontractor pair with two *different* literal company names can
only ever earn **one** structured point (`supplier`, and only when a
second, independent signal happens to coincide) — never enough to clear
`find_candidates`'s 2-point gate on its own.

### Evidence: two runs against real code, zero mocking of the matching logic

Reproduced the real report's shape — Scriptly PV1 work order, a
Scriptly-Sodalis CO1-WO2 (the bridge/subcontract item), and a direct
Sodalis MSA — using the actual deterministic extraction path
`workgraph_parties.extract_and_link_parties_for_issue` (the exact function
`workgraph_parties.run()`, #323's new call site, invokes per-issue) and the
real `workgraph_pipeline2.find_candidates`. No LLM involved; this is pure
structured-signal detection.

**Run A** (bridge item's CC list happens to include the same literal
Scriptly contact as the PV1 item — a generous case):

```
PV1 parties:    [('jsmith@scriptly.com', 'Scriptly')]
BRIDGE parties: [('jsmith@scriptly.com', 'Scriptly'), ('mjones@sodalis.com', 'Sodalis')]
MSA parties:    [('klee@sodalis.com', 'Sodalis')]

find_candidates(PV1)    -> [(BRIDGE, ['supplier', 'stakeholder'])]   # clears the gate
find_candidates(BRIDGE) -> [(PV1,    ['supplier', 'stakeholder'])]   # clears the gate
find_candidates(MSA)    -> []                                        # does NOT
```

PV1↔BRIDGE only clears because of an incidental *second* signal (the same
named person on both threads). BRIDGE↔MSA share the company "Sodalis" —
one real structured point — but that's it: no shared named contact, no
title overlap, no shared reference/amount/document. One point is below
the gate, so the pair never even reaches `judge_candidate`.

**Run B** (more realistic: three genuinely separate rosters, no incidental
shared contact — matching Marc's actual report that these are three
distinct, never-touching threads):

```
PV1 parties:    [('jsmith@scriptly.com', 'Scriptly')]
BRIDGE parties: [('mjones@sodalis.com', 'Sodalis'), ('rpatel@scriptly.com', 'Scriptly')]
MSA parties:    [('klee@sodalis.com', 'Sodalis')]

find_candidates(PV1)    -> []
find_candidates(BRIDGE) -> []
find_candidates(MSA)    -> []
```

All three pairs fail to clear the gate. This is despite the bridge item's
own party extraction now correctly carrying *both* companies (proof #323's
fix is doing exactly what it was built to do), and despite the bridge
item's own extracted text literally reading "the existing Scriptly
subcontract" — a plain-English confirmation of the same relationship the
structured signal can't see. `supplier` fires between PV1↔BRIDGE (shared
"Scriptly") and between BRIDGE↔MSA (shared "Sodalis"), but each is only
**one** point; nothing else overlaps (different named contacts, disjoint
titles, no shared reference/amount/attachment lineage), so neither pair is
ever returned as a candidate, and PV1↔MSA share nothing at all.

**Conclusion:** #323 was necessary (a light-synthesized item with zero
parties couldn't clear the gate under any circumstance), but not
sufficient. The realistic prime/subcontractor shape needs a genuine second
signal source. Row 34's own extracted text already contains that signal —
it just never becomes something `find_candidates` can count.

One incidental finding worth flagging: the existing regression-corpus
fixtures for this exact Scriptly/Sodalis shape
(`tests/test_workgraph_regression_corpus.py` lines 189-211,
`tests/test_workgraph_pipeline2.py` lines 241-254) both *hand-construct* a
shared party + identical company string on both sides via the
`_link_party` test helper, rather than deriving it through real extraction
from two different domains. Those tests correctly cover the *downstream*
verdict/relationship-bookkeeping behavior once a candidate exists, but by
construction they assume the 2-point gate is already cleared — they don't
(and were never meant to) prove real extraction produces 2 points for a
genuine prime/sub pair. This report's probe is the first check against
that assumption, and it fails.

## 2. Options considered for a secondary candidate-generation path

Marc's explicit constraint, carried through this session's `git log`
(`dfaa87c Task #184: replace weighted/tiered scored-grouping with plain
data-point count`, and the whole retired `scored_grouping_decision` history
in `docs/design/CONFIDENCE_AND_IDENTITY_REDESIGN.md`): no opaque, weighted,
un-inspectable score. Every option below is evaluated against that first.

### (a) Generic text-similarity / keyword-overlap secondary signal

A broad "shared distinctive terms" or TF-IDF/embedding-style overlap check
between two items' full text.

- **Inspectability:** fails if implemented as a similarity *score*
  (cosine distance, weighted term overlap) — that's exactly the kind of
  number Marc narrowed away from twice already (the D4 party-weight bug,
  then the whole scored-model retirement). Passable only if scoped down to
  a plain, nameable overlap (e.g., a literal shared distinctive noun
  phrase) rather than a numeric similarity threshold.
- **Risk:** unscoped keyword overlap is noisy — common business vocabulary
  ("agreement," "invoice," "renewal") would fire constantly unless
  filtered hard, and building/maintaining that filter is its own ongoing
  cost (`workgraph_discovery.py`'s `_BOILERPLATE_LABELS` shows how much
  hand-tuning a generic text signal already needs, and that's for a much
  narrower "Label: value" shape).
- **Verdict:** too broad to adopt as-is; the narrow version of this idea
  (matching only *known* company vocabulary, not arbitrary terms) is
  folded into the recommendation below.

### (b) Discovered-vocabulary-driven path (`workgraph_discovery.py`)

Lean on the existing tiered discovery pipeline — let a not-yet-promoted
`candidate_pattern_observations` row, or even a `proposed` (not yet
human-confirmed) `data_point_definitions` row, contribute a point.

- **Wrong shape for this evidence.** `workgraph_discovery.derive_pattern_
  signatures`/`_LABELED_FIELD_RE` specifically look for `Label: value`
  structured lines — Ariba/ContractPodAI/Workday-style notifications. Row
  34's sentence ("the existing Scriptly subcontract") is free-flowing
  prose, not a labeled field; it would never be extracted as a pattern
  signature at all under the current regex.
- **Wrong cadence.** Even for a shape it *could* catch, the significance
  bar (`_SIGNIFICANCE_OCCURRENCE_MIN = 5`, across 2+ threads, within 60
  days) is designed for recurring per-installation vocabulary discovery,
  not a specific one-off relationship between two named companies. A
  genuine prime/sub pairing might occur once or twice total — it would
  never cross the bar, and even if some *other* generic pattern did, using
  it requires a human to confirm the proposal first (`matched_discovered_
  points` only reads `status='confirmed'` definitions) — a multi-week
  latency for a signal that's already sitting in already-extracted text
  today.
- **Verdict:** reject as the primary mechanism. It's a legitimate,
  complementary slow-lane for genuinely recurring structured fields
  (e.g., if "Prime Contract Number:" turned out to recur 5+ times), but it
  cannot be the fix for this specific gap.

### (c) Raw extracted text as a lower-confidence LLM-judgment trigger

Skip structured-datapoint matching for a sparse pair and hand the raw text
straight to `judge_candidate` (or a cheaper triage call) whenever *any*
textual signal suggests a relationship, without first requiring 2
structured points.

- **Inspectability:** fine in principle (the trigger condition can be
  named and shown), but "any textual signal" as stated is unbounded — it
  either means running an LLM call over every otherwise-uncandidatable
  pair (cost/noise explosion: today's gate exists specifically so
  `judge_candidate`, a real Sonnet call, is only ever invoked against a
  short, structurally-justified candidate list) or it collapses back into
  option (a)'s scoping problem (some deterministic rule has to decide
  which pairs are worth a look).
- **The useful version of this idea is not "any text," it's "text that
  names a company we already structurally know about."** That's a
  narrow, cheap, deterministic check — not a new LLM tier at all.
- **Verdict:** reject the unscoped form (cost/noise, and it reintroduces
  an implicit, unstated scoring judgment about which pairs "seem worth
  it"). Adopt the narrow form as the actual recommendation.

## 3. Recommendation: a new, narrow, deterministic point type — "cross-mention"

Add **one new point type** to `workgraph_projects._matched_data_points`
(same list, same `points.append(...)` pattern every existing point type
already uses — `reference`, `supplier`, `stakeholder`, `subject_entity`,
`product_service`, `amount`, `document`). No new architecture, no new
decision layer, no weight, no score.

**Rule (plain, inspectable, stated in one sentence):** if work object A's
raw text contains, as a literal substring (case-insensitive), a company
name that work object B's own parties/positive_vocabulary already know
about (or vice versa) *and* that mention sits near one of a short, curated
list of relationship-language keywords (`subcontract`, `sub-contract`,
`subcontractor`, `flow-down`, `flowdown`, `prime contract`, `teaming
agreement`, `change order under`, `work order under`), that counts as one
point: `cross_mention`.

Why the keyword co-occurrence, not a bare substring match: a bare company
substring match is exactly the kind of thing that's already covered when
it matters (it's the `supplier` point, once parties are linked at all —
confirmed by Run B, where a bare shared-company match still only produces
1 point). What's missing is a way to *corroborate* that weak, single
company-name signal with something that says "and this text is explicitly
describing a relationship between them," so the pair earns its second
point honestly rather than by coincidence (an incidentally cc'd contact,
as in Run A). This is precisely what row 34's own text already supplies —
"the existing Scriptly subcontract" is a company name plus relationship
language, sitting in already-extracted text, unused today.

Every hit is fully explainable: the returned `matched_signals` entry can
carry the literal phrase and company name matched (e.g.
`"cross_mention:Scriptly (subcontract)"`), exactly as auditable as today's
`"supplier"`/`"reference"` strings — never a number, never a weight.

**What this does NOT change:** the 2-point gate itself, `judge_candidate`,
the `same_project`/`related_different_project`/`unrelated` three-way
verdict, or the relationship-bookkeeping (`upsert_work_object_relationship`)
that already exists specifically for the prime/sub outcome. A pair that
newly clears the gate this way still only ever reaches a real Sonnet
judgment call before anything merges — this only fixes *candidate
generation*, never auto-merge, matching the same non-negotiable boundary
`_matched_data_points`'s own docstring already states for every other
point type ("a real candidate is 2 or more of these... this function only
reports what actually matched").

Concretely, for the real case: BRIDGE's text ("the existing Scriptly
subcontract") would pick up `cross_mention` against PV1 (whose party
carries "Scriptly"), stacking with the existing `supplier` point once
BRIDGE's own Scriptly-domain party is linked → 2 points → PV1↔BRIDGE
becomes a candidate without needing an incidental shared contact. If
BRIDGE's text also names Sodalis in relationship-language proximity (or if
a Sodalis-side document ever does), the same mechanism gets BRIDGE↔MSA to
2 points too. The direct PV1↔MSA pair may still never become a candidate
on its own — and that's fine; the system doesn't need to force a direct
edge between the two endpoints. Once BRIDGE is correctly linked to both,
the existing `related_different_project` relationship-writing (already
built, already tested in the regression corpus) is exactly the mechanism
that should represent "these are related through a subcontract, not one
project" — which is Marc's own already-established design intent for this
shape, not something new to build.

### Why not the alternatives, restated briefly

- Generic keyword/similarity overlap (a) — too broad, reintroduces exactly
  the tuning/false-positive burden the retired scored model had, unless
  scoped to known company vocabulary the way this recommendation already
  is (at which point it *is* this recommendation, not a separate option).
- Discovered-vocabulary path (b) — wrong extraction shape (labeled fields,
  not prose) and wrong cadence (a 5-occurrence/60-day bar, plus a human
  confirm) for a signal that's already sitting in text today. Worth
  building separately, later, for actually-recurring structured fields —
  not a substitute here.
- Unscoped raw-text LLM trigger (c) — cost and noise, and it just moves
  the "which pairs deserve a look" scoring decision into an implicit,
  unstated place instead of a named, inspectable rule.

## 4. Build size, model tier, and sequencing

- **Model tier: none (deterministic).** This is a regex/substring check
  over already-extracted text and already-known company vocabulary
  (`external_orgs`/`positive_vocabulary.system_party`, the same fields
  `_matched_data_points` already reads for `supplier`) — zero LLM calls,
  runs at the same point in the pipeline `_matched_data_points` already
  runs today. `judge_candidate` (Sonnet) is unchanged and remains the only
  LLM read in this path, exactly as it is now.
- **Build size: small.** One new function (natural home: `workgraph_
  signals.py`, alongside `extract_labeled_party_field`/`normalize_company_
  name`, which `_matched_data_points` already imports from there) plus one
  new `points.append(...)` line in `workgraph_projects._matched_data_
  points`, wired the same way `matched_discovered_points` already is. The
  curated relationship-keyword list is short and hand-written, not learned.
  Test additions extend the *existing* Scriptly/Sodalis fixtures already
  in `test_workgraph_regression_corpus.py`/`test_workgraph_pipeline2.py` —
  add a case that builds parties via real `workgraph_parties.
  extract_and_link_parties_for_issue` (not the `_link_party` test helper's
  shortcut) so the regression corpus actually exercises real extraction
  against two different domains, closing the gap this investigation found
  in the existing tests themselves. Rough sizing: well under a day of
  focused build + test time, given the point-type list pattern, the
  company-normalization helper, and the Scriptly/Sodalis test fixtures are
  all already in place.
- **Sequencing:** a follow-up build task (next available task number),
  scoped narrowly to this one point type. Does not block on, or require
  changes to, `judge_candidate`, the verdict handling in `process_new_
  item`, or the relationship-bookkeeping — all of that already does the
  right thing once a candidate exists.
