# Jasper Roadmap

This is the persistent record of work that's been deliberately identified
and deliberately **not** built yet — as opposed to work that's simply
unplanned. Everything here was scoped from a real gap (found in the code,
or in Marc's own live feedback), then explicitly deferred rather than
queued, for a stated reason. Nothing on this list should be started without
Marc's explicit go-ahead at the time.

This file exists because task-list items get deleted once they're recognized
as roadmap (not active-queue) work, so the roadmap content would otherwise
only survive in conversation history and commit messages. This is the
durable version.

---

## UI / integration parity items (2026-08-11)

Deferred during the Phase 1 build-queue pass. Each is a real, scoped gap;
none blocks anything currently on the active queue.

- **Generalize UI grouping/labels to read from per-role vocabulary**
  (post-demo). Cockpit's grouping labels and category vocabulary are
  currently procurement-specific; this is the UI-facing half of the broader
  generalization-beyond-procurement track below.
- **Teams auto-respond to status/process questions after timeout** —
  deferred pending the new Symphony (Teams-enabled workers) install.
- **Native M365/SharePoint file-sharing (Share button parity)** — matching
  the real Share button behavior users already expect from M365 apps,
  rather than Jasper's current ad hoc link handling.

Explicitly NOT on this roadmap, per Marc's own instruction: "Run multiple
adversarial testing passes against Jasper" was removed entirely, not
deferred — it is not a future task, it was withdrawn.

---

## Phase 4 — deeper authority/policy model (beyond task #317)

Task #317 (queued/building now) covers the one Phase 4 piece that's real
and single-user-relevant: a deterministic dispatch table deciding
`prepared_actions.required_approval` per `action_kind`.

Everything deeper than that — delegation of authority, per-actor role
separation, an audit/appeal flow, a configurable per-org policy layer — is
inherently about **multiple actors** checking or delegating authority to
each other. With exactly one user on one machine, there is no second actor
to gate against or delegate to. Building this now would be speculative
infrastructure with nothing real to exercise it — precisely the anti-
pattern the engineering-direction doc warns against.

**Trigger to revisit:** a second real user/actor exists, or Jasper moves
off Marc's local-only install.

---

## Phase 5 — third learning domain (behavioral adaptation loop)

Of the 5 learning domains named in the original audit, 2 were already real
(grouping/precedent via `workgraph_lessons.py`, keyword mining via
`personal_patterns.py`) and 2 more are single-user-valid and now queued:

- Task #318 — NBA outcome/behavioral learning (accept/dismiss/rewrite
  tracking)
- Task #322 — process learning (recurring work-sequence mining)

The third missing domain is a broader feedback loop that adapts *how*
Jasper behaves based on the accumulated pattern of how Marc actually
responds over time — distinct from #318's narrower per-suggestion signal.
This doesn't strictly require multi-machine or multi-user, but Marc's own
stated plan sequences it last on purpose: it needs weeks of real
accumulated usage data to have anything real to learn from, not more
infrastructure. See `[[jasper-generalization-roadmap]]` (memory), point 3,
for the fuller framing, including Marc's own concrete example (correcting
a suggested action so Jasper remembers the precondition — which maps onto
extending `workgraph_aristotle.py`'s existing gating with a natural-
language rule-teaching path).

**Trigger to revisit:** #318 and #322 have been running long enough to
produce real accumulated signal to learn from.

---

## Phase 6 — multi-install / multi-user (zero code today)

Confirmed zero code exists for this phase. By definition, Phase 6 is about
running Jasper for more than one person or one install — it has no
meaning against a single local machine, so there is nothing "doable today"
to extract from it.

**Trigger to revisit:** Marc's own stated gate — run Jasper stably for
weeks on his own machine first, then give explicit go-ahead to generalize.
See `[[jasper-generalization-roadmap]]` (memory) for the concrete 5-point
scope already identified for that phase (domain vocabulary as config,
de-hardcoding the UI's fixed cast, onboarding friction, a data-driven
skills registry, a real per-org policy layer) and its recommended
sequencing.

---

## Longer-term generalization track (separate from the above)

Marc's stated plan: stabilize Jasper via real day-to-day use first, then
decide whether/how to make it portable to other people and systems, still
starting from procurement as the base case. Full gap analysis and
recommended sequencing lives in the `jasper-generalization-roadmap` memory
file — not duplicated here since it's already detailed there and this file
is meant to index, not fork, that content. Overlaps with Phase 6 above by
design (Phase 6 is the code-level expression of this same intent).

**Trigger to revisit:** Marc's explicit signal that the stabilization
period is over.
