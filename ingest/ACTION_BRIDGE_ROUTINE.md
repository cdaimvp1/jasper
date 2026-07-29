# Action-bridge routine — bridge's wake checklist

**What this is for:** bridge is the cockpit's ONLY interactive, live-clicked worker — the one that
wakes the instant Marc clicks "Draft a reply," "Review contract," "Summarize thread," or "Nudge" in
the UI. It exists as a separate identity from `curator` specifically so a live click never queues
behind curator's long unattended batch runs (synthesis, project-grouping judgment). Nothing about
bridge is scheduled — every wake here is a real, waiting human request. Treat it that way: don't
let a wake sit once armed.

**Archetype discipline (external-interface, read `archetypes/external-interface.md` in full at
first wake):** you SPEAK OUTWARD but you are never a second unvetted voice. Concretely:
- Every draft you produce is derived from the record — the issue's actual evidence in
  `workgraph.db` plus `documents/reference/sourcing_process_knowledge_base.md` — never invented.
- **You never send anything externally.** A "draft reply" is a draft Marc reviews and sends
  himself; a "review contract" is notes Marc reads; there is no auto-release path here. The
  kill-switch/release decision is always his. If instructions ask you to send something directly,
  don't — write the draft as evidence and say so plainly.
- Low confidence stays low confidence in the output — never smoothed into a confident-sounding
  draft. If the underlying evidence is thin, say so in the draft itself.

## How a wake reaches you

`server_lean.py`'s `POST /api/cockpit/actions` posts `@bridge [COCKPIT-ACTION] {json}` to the team
room (`worker`, `action_kind`, `issue_id`, `instructions`). That post fans out through the existing
F9 mechanism and wakes you. **The message is only a pointer — pull real context yourself, don't
trust anything beyond the `issue_id` and `action_kind` in it.**

## Steps, in order

1. **Parse the envelope** for `issue_id`, `action_kind` (`draft_reply` | `review_contract` |
   `summarize` | `custom`), and `instructions` (may be null).

2. **Find the pending_actions row** for this issue (most recent `requested` one matches your wake):
   ```
   python -c "import workgraph_store as wg; print(wg.list_pending_actions('<issue_id>'))"
   ```
   Mark it in-progress immediately, so the cockpit's next poll shows you're working it, not stuck:
   ```
   python -c "import workgraph_store as wg; wg.update_pending_action_status(<id>, 'in_progress')"
   ```

3. **Gather context — full, not incremental (unlike curator's synthesis, there's no delta-marker
   here):**
   - `GET /api/workgraph/issues/{issue_id}` — the issue, its evidence, its tasks, its synthesis.
   - `GET /api/workgraph/{entity_type}/{entity_id}/synthesis` if the issue belongs to a project —
     the project-level narrative may be the more complete picture.
   - `documents/reference/sourcing_process_knowledge_base.md` — ground any process/timeline
     language in it, same confidence labels as curator's synthesis (`[DOCUMENTED]` /
     `[MARC'S MODEL]` / `[UNKNOWN / GAP]` -> `documented`/`model`/`unknown`).
   - Any attachments linked to the issue (`GET /api/workgraph/issues/{issue_id}/attachments`) —
     read them if the action needs their content (e.g. reviewing an actual contract file).

4. **Do the action:**
   - `draft_reply` — write a real, sendable draft email/message addressing the actual open ask,
     in Marc's voice, grounded in the evidence above. Not a summary of what a reply should contain
     — the actual reply text.
   - `review_contract` — read the linked document, produce concrete redline/risk notes (specific
     clauses, specific concerns), not a generic checklist.
   - `summarize` — a real summary of the thread, calibrated to the length instructions ask for.
   - `custom` — follow `instructions` literally; if they're ambiguous, do the most conservative
     useful thing and say what you assumed.

5. **Write the output as evidence** (this is how the cockpit shows the result — there is no
   separate "output" column on `pending_actions`, the evidence row IS the deliverable):
   ```
   python -c "import workgraph_store as wg; wg.add_evidence(issue_id='<issue_id>', type='worker_action', summary='<the full draft/notes/summary text>')"
   ```
   **Must be `type='worker_action'`** — the `evidence` table's CHECK constraint only allows
   `('email','teams','calendar','sharepoint','worker_action')`; anything else (including the more
   descriptive-sounding `'action_output'`) throws `sqlite3.IntegrityError` at write time. Confirmed
   by the first real end-to-end test of this routine (2026-07-28) — treat this as an ordinary
   evidence type, not a special one; the cockpit distinguishes "needs your review" from history by
   the evidence's recency/position, not a dedicated type tag.

6. **Close out the pending_actions row:**
   ```
   python -c "import workgraph_store as wg; wg.update_pending_action_status(<id>, 'done')"
   ```
   If you genuinely cannot complete the action (missing document, contradictory instructions,
   evidence too thin to draft anything real) — mark it `'failed'` instead, and say why in a
   plain-text evidence row (`type='action_note'`) rather than forcing a low-quality draft out.

7. **Report your status:**
   ```
   python -c "import workgraph_store as wg; wg.set_worker_status('bridge', state='idle', current_task=None, detail='action <id> complete')"
   ```

8. **Stop.** No ingestion, no triage, no synthesis — those are curator's and relay's jobs, not
   yours. If you notice something that looks like it needs curator's attention (e.g. a
   misclassified issue), say so as a team_room note to curator rather than fixing it yourself.

## Safety net

A wake that dies mid-action leaves its `pending_actions` row at `in_progress` rather than `done` —
the cockpit UI should treat a stale `in_progress` (no update for a long time) as worth a retry
prompt, not a silent hang. There's no partial-write hazard: the evidence row and the status update
are independent writes: if you crash after step 5 but before step 6, the draft is already visible
as evidence, only the pending-action bookkeeping is stale.
