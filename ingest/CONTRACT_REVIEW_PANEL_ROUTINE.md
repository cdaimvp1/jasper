# Contract review Pass 3 SME panel — worker protocol

**What this is for:** per docs/design/CONTRACT_REVIEW_SME_PANEL.md (task #50), the
contract_review skill's Pass 3 (`PASS_3_ANALYSIS`) is the one pass where a single
generalist model juggling 19+ SME domains' worth of trigger phrases is most likely to
miss something a domain-specific pass wouldn't. This doc is what a worker (per
ACTION_BRIDGE_ROUTINE.md's registry-driven skill-execution step) actually does at Pass
3 instead of running it as one generalist pass — Passes 1, 2, and 4 are unchanged,
sequential, exactly as the skill's own SKILL.md already specifies.

**Nothing here is invented ad hoc; this is the design doc's own plan, made executable.**

## When this applies

You reach this doc because `skills_registry.get_skill_for_action('contract_review')`'s
entry carries a `panel_protocol` field naming this file (see ACTION_BRIDGE_ROUTINE.md's
generic step 4 — that check is registry-driven and never hardcodes "contract_review" by
name; a future skill could opt into the same treatment for a different pass just by
adding the field to its own registry entry). If you're running contract_review and this
field is absent, run Pass 3 exactly as SKILL.md itself describes — this panelization is
an enhancement to HOW Pass 3 gets executed, not a required step.

## Steps

1. **Complete Pass 1 and Pass 2 exactly as SKILL.md specifies**, producing the real
   `PASS_1_STRUCTURE` and `PASS_2_COVERAGE` artifacts. The panel below reads both
   read-only — it never re-derives coverage status itself.

2. **Run the keyword pre-filter** (pure Python, no LLM call, no cost):
   ```
   python -c "
   import workgraph_contract_panel as wcp
   triggered = wcp.identify_triggered_smes(open('<path to the contract text>').read())
   for t in triggered:
       print(t['sme_name'], '|', t['email'], '|', t['triggers_matched'])
   "
   ```
   This tells you which SMEs' domains are even plausibly relevant to THIS document —
   a simple NDA should trigger zero or one SME (the catch-all generalist below), not a
   flat panel of 19+ regardless of content. `identify_triggered_smes` only checks
   whether a trigger keyword literally appears — it says nothing about whether the hit
   is actually that SME's domain (see step 4's own caution about that).

3. **Always include the generalist member too**, regardless of what step 2 found — it
   covers `sme-matrix.md`'s "Contract Request and Consultation Tool" catch-all list
   (indemnification structure, liability cap, choice of law, termination-for-convenience,
   force majeure, IP ownership, and any provision the 19+ named SMEs don't cover) and is
   the one member every real review needs regardless of domain mix.

4. **Spawn one sub-agent per triggered SME (plus the generalist), in parallel** — via
   the Agent tool, run concurrently, not sequentially; wall-clock should track the
   slowest single member, not their sum, since each is independent given its own scoped
   input. Each member's prompt must include, and ONLY reason from:
   - That SME's own `sme-matrix.md` entry in full (triggers, scope, common issues,
     escalation threshold) — this is what makes it a specific lens, not a generic "look
     for legal issues" pass.
   - The specific `playbook.md` section(s) that SME's entry names (e.g. the Tax member
     gets §8 and HS-3 — read `playbook.md` yourself to find the right section for a
     given SME if the mapping isn't already explicit in `sme-matrix.md`).
   - The already-produced `PASS_1_STRUCTURE` and `PASS_2_COVERAGE` (read-only — a
     member must never flag something Pass 2 already resolved).
   - The document text: the matching clause(s) plus a paragraph or two of surrounding
     context — not the whole document, UNLESS the SME's own scope is inherently
     document-wide (Trade Sanctions and Anti-Bribery, per the design doc, since those
     provisions can appear anywhere and aren't confined to one clause).

   **Explicitly instruct each member**: "A trigger keyword appearing in your list does
   not mean this is automatically your domain — check the actual context against your
   own Scope text (e.g. 'audit' appearing in a definitions section, not an actual
   audit-rights clause, is NOT a real hit). If nothing in your domain is genuinely
   present, return an empty findings list — never a fabricated 'no issues found, all
   clear' narrative. Silence is the correct, honest output when this document has
   nothing for your domain."

   Each real finding must be in `PASS_3_ANALYSIS`'s existing shape (severity tier,
   `clause_reference`, playbook/regulatory citation, VERIFIED/ASSUMED flag, impact,
   recommended action) PLUS two fields this design adds, both copied straight from that
   member's own `sme-matrix.md` entry, never re-derived: `owning_sme` (name + email) and
   `escalation_threshold`. If the finding cites one of the six Hard Stops (HS-1 through
   HS-6, per `risk-scoring.md`), it must carry `hard_stop_id` too — this is what step 5's
   reconciliation dedupes on.

5. **Reconcile every member's findings into one list** (pure Python, no LLM call):
   ```
   python -c "
   import json, workgraph_contract_panel as wcp
   findings = json.load(open('<collected findings from all members, one flat list>'))
   reconciled = wcp.reconcile_panel_findings(findings)
   json.dump(reconciled, open('<PASS_3_ANALYSIS output path>', 'w'), indent=2)
   "
   ```
   This mechanically: (a) drops duplicate findings that cite the SAME `hard_stop_id`
   across different members (risk-scoring.md's -15 Hard Stop deduction must never apply
   more than once for the same real Hard Stop), and (b) when two members flag the SAME
   `clause_reference`, keeps BOTH findings and links them via `related_findings` —
   sme-matrix.md's own worked example (an AI data-processing provision triggering both
   AI/Privacy and InfoSec). **This function never arbitrates a real disagreement between
   two SMEs** — that's the existing, human-facing "Multiple SME Escalation Handling"
   protocol (sme-matrix.md's own text): create separate escalation comments, note each
   other's involvement, wait for all responses, escalate conflicting positions to the
   Contract Request and Consultation Tool. Nothing in this panel replaces that; it only
   makes the underlying FINDING that something needs an SME's eyes more reliable.

6. **`PASS_3_ANALYSIS` is now the reconciled list from step 5**, plus the commercial
   analysis, vendor tactics scan, pharma requirements check, and volume-scaled risk
   calculations SKILL.md's Pass 3 already specifies — those are unaffected by
   panelization (they were never per-SME work) and still need to be produced normally.

7. **Continue to Pass 4 exactly as SKILL.md specifies.** The risk-scoring formula,
   position-card generation, and the locked dashboard structure all consume
   `PASS_3_ANALYSIS` in its existing shape — panelizing Pass 3 changes nothing about how
   Pass 4 reads it.

## Cost/latency note

Real cost is proportional to how many domains a document actually touches (the pre-
filter in step 2), not a flat 19x-plus-one multiplier on every review. A document with
almost no SME-relevant content (a simple NDA) should trigger the pre-filter for zero or
one SME and cost about the same as a single unpanelized pass. A document that genuinely
touches many domains (a real MSA renewal) will trigger most members — that's real,
warranted cost, not overhead to optimize away.
