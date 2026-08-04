# Backlog / deferred items

Items intentionally NOT on the active task list, with the real reason each was set
aside. Nothing here is abandoned — it's parked, with the condition that would bring it
back named explicitly. Moved out of the tracker on 2026-08-04 at Marc's request so the
active list only shows work actually in flight.

## Deferred by Marc directly

**Sender seniority/title as an NBA signal.** Explicitly excluded from the "build all of
it" standing instruction from the start of this session — one of two named exceptions.
No condition given for revisiting; raise it with Marc directly if it should come back.

**Live external-system integration (Ariba/SAP/DocuSign).** The second of the two named
exceptions from the same standing instruction. Real API/auth work with a different risk
profile than everything else on the list (touches live external systems, not just
Jasper's own data) — needs its own explicit go-ahead, not a default "yes."

**Tenant/multi-user scope.** Marc was explicit that Jasper stays single-user (him) for
now. Revisit only as part of the generalization phase below, not before.

## Future-phase (generalization beyond Marc/procurement)

Per [[jasper-generalization-roadmap]] (Marc's stated intent: stabilize via weeks of real
use, then generalize Jasper beyond procurement and beyond himself) — all of these are
premature until that phase actually starts, since building them now means designing
against zero other real users/installs:

- **Extract domain vocabulary** (`workgraph_signals.py`/`workgraph_classify.py`'s
  keyword rules) into a swappable config, so a non-procurement install doesn't need a
  code change to retarget its own domain.
- **Per-install config** for Aristotle prerequisite rules, the knowledge base, and the
  cockpit's cast of workers - today these assume Marc's specific setup.
- **Auto-triggered first-run backfill + onboarding wizard** - only matters once there's
  a second real install to onboard.
- **Role-weighted NBA** for management/leadership vs. individual contributor - nothing
  to weight against with one user.
- **Wire the real 99-skill Lilly Claude Skills Hub catalog** into `skills_registry.json`
  - only the handful Marc directly needs have been registered so far (task #46); the
  rest is future-phase breadth, not current need.
- **Skill-gap detection** (suggest + design + export spec for a missing skill) -
  explicitly scoped by Marc to suggest/design only, never build or run automatically,
  and not worth building even that much until there's a real gap-reporting need beyond
  Marc's own procurement work.

## Needs clarification before any build (not simply deferred)

**Manual cross-reference confirmation for circumstantial issue matches.** Original
scope was ambiguous enough that building it risked guessing. Likely substantially
superseded by later work in the same session - the meeting-grouping design's
`project_links`/`suggestion_kind='link'` mechanism already gives a manual confirm step
for exactly "weak/circumstantial match, don't auto-merge." Before this comes back:
confirm with Marc whether that existing flow already covers the real ask, or whether
something genuinely different was intended.
