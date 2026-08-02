# Synthesis routine — curator's (Colleen's) wake checklist

**What this is for:** curator's synthesis job is real judgment — reading communications, extracting
facts, and writing a narrative — never mechanical (that's `relay`'s job, see
`GRAPH_INGEST_ROUTINE.md`), and never a wholesale re-read of an entity's whole history on every
wake (Marc's explicit requirement). The deterministic staleness check
(`workgraph_synthesis.list_stale_entities()`) has already decided WHAT needs your attention before
you ever wake for this — your job here is only to do the actual synthesis work for the entities it
names, incrementally, using what's genuinely new since last time.

**Two entity types, one mechanism.** A Project aggregates ALL of its constituent issues' evidence
into one synthesis (a Project can span multiple email threads/Teams chats that are really the same
underlying negotiation — Marc wants ONE synthesis reflecting the whole thing). A standalone Issue
not yet grouped into a Project is synthesized the same way, on its own. Don't treat these as two
different jobs.

## Steps, in order

1. **Get the work list.**
   ```
   python workgraph_synthesis.py --list-stale
   ```
   This is pure/deterministic (no LLM call inside it) — it just diffs each entity's current
   evidence marker against its stored `synthesized_from_marker`. Each item gives you
   `entity_type`, `entity_id`, `name`, `current_marker`, `previous_marker`, and
   `previous_summary` (null if this entity has never been synthesized before).

2. **For each stale entity, gather context — the DELTA, not the whole history:**
   - The prior synthesis (`previous_summary` above, plus fetch the full row via
     `GET /api/workgraph/{entity_type}/{entity_id}/synthesis` for `next_steps`/`suggested_actions`
     if you need them) — this is your "here's what I said before" anchor.
   - For a project: every issue in it (`GET /api/workgraph/projects/{project_id}` gives you the
     issue list). For a standalone issue: just that one issue
     (`GET /api/workgraph/issues/{issue_id}`).
   - The evidence rows for those issue(s), from that same response — but only what's NEW since
     `previous_marker` (parse the `count:`/`max_ts:` out of both markers; anything with
     `ts`/`occurred_ts` newer than the previous `max_ts` is new). If `previous_marker` is null,
     this entity has never been synthesized — treat everything as new, but this only happens once
     per entity.
   - **`evidence[].summary` is subject-line-only by construction (`classify_item`'s own summary
     field falls back to body only when there's no subject at all, which is nearly never) — it is
     NOT the communication's content, just a label for the Progress-timeline list. Never write an
     extraction from `summary` alone.** For each new evidence row's `raw_item_id`, call
     `GET /api/workgraph/raw_items/{raw_item_id}` (added 2026-08-01, task #33 — this route didn't
     exist before, so there was previously no way for this routine to read a communication's real
     content at all) to get `full_text` (the real body, quoted-reply stripped, task #29's full-text
     pipeline) and `attachments[].extracted_text` (PDF/XLSX content, also task #29). That response
     also returns `extraction` — the existing `raw_item_extractions` row for that raw_item, if one
     was already written; skip straight to step 4 for any raw_item that already has one rather than
     re-reading its `full_text` for nothing.

3. **Extract any newly-seen raw_item that doesn't have an extraction yet.** This is real LLM
   judgment on the `full_text`/`attachments` you just read — pulling out `asks`, `decisions`,
   `dates_mentioned`, `commitments`, `key_facts` — deterministic code cannot do this part, that's
   why it's yours. Write each one via:
   ```
   POST /api/workgraph/raw_items/{raw_item_id}/extraction
   {"extracted_json": {"asks": [...], "decisions": [...],
                        "dates_mentioned": [{"text": "...", "kind": "hard"|"soft"}, ...],
                        "commitments": [...], "key_facts": [...],
                        "repeat_signals": [{"ask_text": "...", "days_since_first_ask": 6,
                                             "escalated": true,
                                             "escalation_note": "2nd follow-up, now from the
                                             requester's manager rather than the requester"}, ...]}}
   ```
   Computed ONCE per raw_item, permanently — never re-extract an item that already has a row here
   (check first; the routes list above tell you which raw_items already have one).

   **`repeat_signals` (added 2026-07-30, Marc's direct request) — only populate this when a NEW ask
   on this raw_item is genuinely restating one already asked earlier on the SAME issue, never a
   guess:**
   - Before writing `asks` for this raw_item, check this issue's prior asks:
     `GET /api/workgraph/issues/{issue_id}` returns an `asks` list (already scoped to this one
     issue) — read it first, same "gather the delta, not a guess" discipline as everywhere else in
     this routine.
   - If (and only if) a new ask is clearly the same request restated — a reminder, a follow-up, "as
     mentioned before," the same specific thing being asked again — add one `repeat_signals` entry:
     `ask_text` (the new raw_item's own restatement, verbatim), `days_since_first_ask` (real
     arithmetic from this raw_item's `occurred_ts` minus the first ask's `occurred_ts` — never
     estimate this, it's computable), `escalated` (true only if this occurrence came from a
     DIFFERENT, more senior, or otherwise new sender than the original ask — not true just because
     time has passed), and `escalation_note` only when `escalated` is true (say who/what changed,
     don't repeat the ask text here).
   - If a new ask is NOT a clear repeat (a genuinely new, distinct ask, even on a related topic),
     do not force a `repeat_signals` entry — omitting it entirely is the normal, correct outcome for
     most asks, same as `estimated_completion` being genuinely absent in step 5 below. This field
     exists to capture a real, judged repeat — never to flag every ask as "maybe related."

   **`dates_mentioned` entries need real judgment on `kind` (added 2026-07-30, Marc's direct
   request) — this is exactly the kind of call deterministic code can't make, which is why it
   lives here and not in a keyword filter:**
   - `"hard"` — a real, binding date with an actual consequence for missing it: a contract's
     must-sign-by date, a notice-of-non-renewal or termination deadline, an SLA cutoff, a filing
     deadline. If missing it has a real, nameable consequence, it's hard.
   - `"soft"` — an aspirational or target date with no binding consequence: "shooting to have
     this done by next week," "hoping to close this out by end of month," a rough estimate.
   - When you genuinely can't tell from the text, still pick the closer of the two rather than
     omitting `kind` — Jasper treats a missing/malformed `kind` as "unclassified" and shows it
     more cautiously than either, so a real guess is more useful than silence.
   - Plain strings (the old shape, from before this date) still work and just show as
     unclassified — don't go back and re-extract old raw_items to backfill `kind` (violates the
     "computed once, permanently" rule above); this only applies going forward.

4. **Write the updated synthesis** — a 2-4 sentence narrative (who asked what, what's happened,
   where it stands now, informed by the prior synthesis plus what's new), `next_steps` grounded in
   the sourcing-process phase model where applicable, and `suggested_actions` tied to SPECIFIC
   tasks with a one-line rationale each (never a bare label with no "why" — Marc's explicit
   "in-depth, not oversimplified" requirement). Each `next_steps` item MAY also carry a duration
   estimate (see step 5) — omit `estimate_*` fields entirely on a step rather than guessing one:
   ```
   POST /api/workgraph/{entity_type}/{entity_id}/synthesis
   {"summary": "...",
    "next_steps": [{"step": "...", "current": true,
                     "estimate_days_low": 3, "estimate_days_high": 5,
                     "estimate_confidence": "documented", "estimate_note": "per the SOW review SLA"}, ...],
    "suggested_actions": [{"task_id": "...", "label": "...", "rationale": "..."}, ...],
    "estimated_completion": {"note": "...", "confidence": "documented"}}
   ```
   The server computes and stores `synthesized_from_marker` itself at write time (from the current
   evidence marker) — you never send one.

5. **Ground timeline/duration/next-step language in
   `$TEAM_DATA_DIR/documents/reference/sourcing_process_knowledge_base.md`** (the shared document
   library, not a worker-private memory file — any worker can read it) — the negotiation ->
   ATC/ATS approval -> signature phase model, TPRM/review branches, etc. Its own confidence labels
   map directly onto `estimate_confidence`/the top-level `estimated_completion.confidence`:
   - `[DOCUMENTED]` (a real Lilly SharePoint figure) -> `"documented"`.
   - `[MARC'S MODEL]` (his own skill-suite's considered estimate, not an official SLA) -> `"model"`.
   - `[UNKNOWN / GAP]` (not in either source) -> `"unknown"`, and say so in the note plainly
     ("not documented — routing to TPRM directly would confirm") rather than inventing a number.
   Never state a `"model"` or `"unknown"` figure as though it were `"documented"` — that mislabeling
   is the one thing this whole feature exists to prevent. `estimated_completion` is a roof-level
   read across all remaining `next_steps` (sum the ranges, or say plainly why you can't - e.g. an
   open-ended external dependency with no committed date). Leaving `estimated_completion` out
   entirely (or a step's `estimate_*` fields out) is a normal, correct outcome when nothing in the
   knowledge base supports even a `"model"`-tier guess - do not fill it with an `"unknown"` entry
   just to have something there.

6. **Report your status** (per the cockpit's worker_status mechanism):
   ```python
   import workgraph_store as ws
   ws.set_worker_status("curator", state="idle", current_task=None, detail="synthesis wake complete")
   ```

7. **Stop.** Nothing else this wake — no ingestion, no re-classification, no team_room posts beyond
   what this routine itself calls for.

## Safety net

`list_stale_entities()` is safe to re-run at any time (it's read-only and deterministic) — if a
synthesis wake dies partway through, the next one will simply see the entities you didn't get to
still listed as stale, and any you already wrote as no longer stale. There is no partial-write
hazard: each entity's extraction/synthesis writes are independent, self-contained REST calls.
