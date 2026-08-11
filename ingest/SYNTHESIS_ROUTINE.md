# Synthesis routine — curator's (Colleen's) wake checklist

**Hybrid routing (task #247, 2026-08-07):** you are only woken at all when at least one stale entity
carries genuinely new evidence AT OR ABOVE `workgraph_synthesis_light.LIGHT_PATH_MAX_BYTES` (100KB).
Anything under that gets handled inline by `ingest/scheduled_refresh.py` itself before you're ever
spawned — one non-agentic LLM call, no subprocess, deliberately narrower than this routine (no
`repeat_signals`/`resolution_signals`/duration estimates — see `workgraph_synthesis_light.py`'s
module docstring for why those are safely omittable there). By the time you run your own
`--list-stale`, anything the light path already handled is simply no longer stale — you never need
to know it happened, and there is nothing to exclude or skip on your end.

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
   revision marker (`"rev:N"`) against its stored `synthesized_from_marker`. Each item gives you
   `entity_type`, `entity_id`, `name`, `current_marker`, `previous_marker`, and
   `previous_summary` (null if this entity has never been synthesized before). **Treat
   `current_marker`/`previous_marker` as opaque — never parse them.** (2026-08-03, Section 9.5 of
   the design doc: the old `"count:N|max_ts:T"` marker WAS parseable, and a prior version of this
   routine parsed its `max_ts` to decide which evidence rows were "new" — that's the same class of
   bug that shipped as D9/D10 in the code: a late-arriving, old-timestamped item is invisible to a
   timestamp-based delta. The real, correct novelty signal is the per-raw_item extraction-existence
   check in step 2 below, which is what decides what's new, never the marker.)

2. **For each stale entity, gather context — the DELTA, not the whole history:**
   - The prior synthesis (`previous_summary` above, plus fetch the full row via
     `GET /api/workgraph/{entity_type}/{entity_id}/synthesis` for `next_steps`/`suggested_actions`
     if you need them) — this is your "here's what I said before" anchor.
   - For a project: every issue in it (`GET /api/workgraph/projects/{project_id}` gives you the
     issue list). For a standalone issue: just that one issue
     (`GET /api/workgraph/issues/{issue_id}`).
   - **A project's response also carries `clusters` (added 2026-08-05, corrected-ordering
     pipeline Phase D) — raw, not-yet-promoted-into-a-real-issue members. A freshly-confirmed
     project is routinely made up ENTIRELY of clusters at first (nothing under `issues` yet at
     all) — never read `clusters` as empty just because `issues` looks thin; check both. Each
     cluster entry carries its own `evidence`/`raw_item_ids` the same shape `issues` entries do,
     so steps 2-3 below (read `full_text`, extract via `POST /api/workgraph/raw_items/{id}/
     extraction`) work identically whether the citing work object is a cluster or a real issue —
     extraction is keyed on the raw_item, not on which kind of thing currently owns it.**
   - The evidence rows for those issue(s), from that same response. The real "is this new"
     signal is per-raw_item, not the entity-level marker: for each evidence row's `raw_item_id`,
     call `GET /api/workgraph/raw_items/{raw_item_id}` — if its `extraction` field is already
     populated, this raw_item was already processed on a prior wake (whatever its own
     `occurred_ts` happens to be, including an old one) and you can skip straight to step 4 for it;
     only a raw_item with no `extraction` yet is genuinely new work for you here.
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
                        "dates_mentioned": [{"text": "...", "kind": "hard"|"soft",
                                             "whose": "marc"|"counterparty"|"shared"|"unclear",
                                             "deadline_type": "renewal_notice"|"contract_expiration"|
                                                              "signature_deadline"|"sla_cutoff"|"other",
                                             "resolved_date": "YYYY-MM-DD"}, ...],
                        "commitments": [...], "key_facts": [...],
                        "repeat_signals": [{"ask_text": "...", "days_since_first_ask": 6,
                                             "escalated": true,
                                             "escalation_note": "2nd follow-up, now from the
                                             requester's manager rather than the requester"}, ...],
                        "resolution_signals": [{"claim_type": "ask"|"decision"|"commitment",
                                             "claim_text": "...", "resolution_note": "..."}, ...],
                        "dependency_signals": [{"relationship": "depends_on"|"blocks"|"enables",
                                             "target_project_id": "proj-042", "reason": "..."}, ...]}}
   ```
   Computed ONCE per raw_item, permanently — never re-extract an item that already has a row here
   (check first; the routes list above tell you which raw_items already have one). Writing this
   also materializes it into the `claims` ledger automatically (Phase 3, design doc Section 9) —
   nothing further to do here for that; author/owner attribution is deterministic, computed from
   this raw_item's `direction`, never something you need to judge or add to this payload.

   **`repeat_signals` (added 2026-07-30, Marc's direct request; widened 2026-08-03, Section 9.3, to
   commitments and decisions too, not just asks) — only populate this when a NEW ask, commitment,
   or decision on this raw_item is genuinely restating one already made earlier on the SAME issue,
   never a guess:**
   - Before writing `asks`/`commitments`/`decisions` for this raw_item, check this issue's prior
     ones: `GET /api/workgraph/issues/{issue_id}` returns `asks`/`commitments`/`decisions` lists
     (already scoped to this one issue) — read them first, same "gather the delta, not a guess"
     discipline as everywhere else in this routine.
   - If (and only if) a new one is clearly the same ask/commitment/decision restated — a reminder,
     a follow-up, "as mentioned before," the same specific thing being said again — add one
     `repeat_signals` entry: `ask_text` (the new raw_item's own restatement, verbatim — same field
     name regardless of whether the underlying claim is an ask, commitment, or decision, so the
     claims ledger's dedup logic has one field to match against), `days_since_first_ask` (real
     arithmetic from this raw_item's `occurred_ts` minus the first occurrence's `occurred_ts` —
     never estimate this, it's computable), `escalated` (true only if this occurrence came from a
     DIFFERENT, more senior, or otherwise new sender than the original — not true just because time
     has passed), and `escalation_note` only when `escalated` is true (say who/what changed, don't
     repeat the text here).
   - If a new one is NOT a clear repeat (genuinely new, distinct, even on a related topic), do not
     force a `repeat_signals` entry — omitting it entirely is the normal, correct outcome most of
     the time, same as `estimated_completion` being genuinely absent in step 5 below. This field
     exists to capture a real, judged repeat — never to flag every ask/commitment/decision as
     "maybe related."

   **`resolution_signals` (added 2026-08-04, task #155, claim-resolution suggestions) — only
   populate this when THIS raw_item's own content directly and unambiguously states that a
   SPECIFIC earlier open ask/decision/commitment on the SAME issue was fulfilled, never a guess —
   same discipline as `repeat_signals`, one level further (a completion, not just a repeat):**
   - Same prerequisite as `repeat_signals`: read this issue's prior `asks`/`decisions`/
     `commitments` first (`GET /api/workgraph/issues/{issue_id}`) before judging whether anything
     in THIS raw_item resolves one of them.
   - Add one entry only when the match is explicit and specific — "the signed SOW you asked for
     is attached," a direct "Approved." reply naming what was approved, a clear "done, sent
     Friday" tied to one named ask. `claim_type` and `claim_text` must reproduce that EARLIER
     claim's own text verbatim (not this raw_item's restatement of it — this is the mirror image
     of `repeat_signals`, which records the NEW text; this records which OLD claim just got
     closed), so the suggestion this becomes points at the exact open claim it's resolving.
     `resolution_note` is a short, specific reason (what/where the confirmation is), not a
     restatement of the claim text.
   - (task #319) When `claim_text` matches an existing open claim exactly, or this raw_item carries
     a real structured reference (e.g. a PR number) that ties it to that claim's own reference,
     writing this entry reaches a WIDER match than the byte-exact-only path this signal already fed
     (workgraph_claims._resolve_explicit_completions, alongside workgraph_reconcile.py's own
     byte-exact matcher) — but either path still only ever CREATES A SUGGESTION for a human to
     confirm, same as every other claim-closing mechanism in this codebase; nothing here auto-closes
     a claim. If you're not confident a specific earlier claim was actually fulfilled by THIS
     raw_item, omit the entry entirely; a missed resolution just means one more item stays open a
     little longer, which costs nothing — a wrongly-suggested one costs Marc's trust in the ledger.

   **`dependency_signals` (added 2026-08-11, task #319) — only populate this when THIS raw_item's
   content explicitly and specifically states that this issue's own project depends on, is blocked
   by, or enables another SPECIFIC, real project you have already confirmed exists — never a
   topical-similarity guess, never a project you're merely inferring might exist:**
   - Before adding an entry, confirm the OTHER project is real: you must already have its exact
     `project_id` from your own normal reads this wake (`GET /api/workgraph/projects` or a project
     detail page you've already opened) — never invent or guess an id. If you can't name the real
     other project's id, omit the entry entirely; a missed dependency costs nothing, a wrong one
     writes a real row into `project_links` that a human then has to notice and undo.
   - `relationship` is from THIS project's own point of view: `depends_on` (this project cannot
     finish until the named one does), `blocks` (this project is what's holding the named one up),
     `enables` (this project makes the named one possible, without strictly gating it). `reason` is
     a short, specific quote or paraphrase of what in this raw_item actually said so.
   - This is project-level, not issue-level (same reasoning as `project_links`' own schema
     comment) — only meaningful once this issue is already grouped into a real project; if it
     isn't yet, the signal is silently dropped rather than guessed at, so there's no harm in still
     writing the entry if you're unsure whether grouping has happened yet.

   **`dates_mentioned` entries need real judgment on `kind` and `whose` (added 2026-07-30 for
   `kind`, Marc's direct request; `whose` added 2026-08-03, task #57/design doc Section 9.7) — this
   is exactly the kind of call deterministic code can't make, which is why it lives here and not in
   a keyword filter:**
   - `"hard"` — a real, binding date with an actual consequence for missing it: a contract's
     must-sign-by date, a notice-of-non-renewal or termination deadline, an SLA cutoff, a filing
     deadline. If missing it has a real, nameable consequence, it's hard.
   - `"soft"` — an aspirational or target date with no binding consequence: "shooting to have
     this done by next week," "hoping to close this out by end of month," a rough estimate.
   - When you genuinely can't tell from the text, still pick the closer of the two rather than
     omitting `kind` — Jasper treats a missing/malformed `kind` as "unclassified" and shows it
     more cautiously than either, so a real guess is more useful than silence.
   - **`whose`** — who the date actually binds, judged from what the sentence says, never from who
     sent the message (the sender and the date's owner are frequently different people: a
     counterparty routinely writes about MARC's deadline, and vice versa). `"marc"` if Marc is the
     one who must act by this date; `"counterparty"` if the other side is; `"shared"` for a mutual
     date (e.g. a signing ceremony both sides must hit); `"unclear"` when the text genuinely doesn't
     say. Marc's explicit standing decision (task #57): a counterparty's own deadline that still has
     real consequences for Marc must never be read as "not mine, so lower priority" — `whose` just
     records who the date belongs to; it is not a priority signal by itself, and downstream
     surfacing (NBA) must not downgrade a `"counterparty"` date just because it isn't `"marc"`.
   - Plain strings (the old shape, from before this date) still work and just show as
     unclassified — don't go back and re-extract old raw_items to backfill `kind` (violates the
     "computed once, permanently" rule above); this only applies going forward.

   **`deadline_type` and `resolved_date` (added 2026-08-04, task #141, renewal-window early-
   outreach draft) — ONLY for `kind: "hard"` entries, both optional, both omitted rather than
   guessed when you're not confident:**
   - `deadline_type`: what KIND of hard deadline this is — `"renewal_notice"` (a notice-of-non-
     renewal or auto-renewal cutoff — miss it and the contract silently renews or silently
     lapses), `"contract_expiration"` (the contract/agreement's own end date, distinct from a
     notice deadline), `"signature_deadline"` (a must-sign-by date), `"sla_cutoff"` (a service-
     level deadline), or `"other"` for any other real hard deadline that doesn't fit the first
     four. This drives which hard deadlines Jasper treats as renewal-relevant (only `renewal_
     notice`/`contract_expiration` feed the early-outreach draft) — never guess this from a
     keyword downstream; it only exists because you're reading the actual email.
   - `resolved_date`: the actual calendar date this deadline falls on, as `"YYYY-MM-DD"` —
     ONLY when you can resolve it with real confidence from what's in front of you (an explicit
     date already in the text, or unambiguous arithmetic from THIS message's own date, e.g. "60
     days from today" where you know today's date). If the text is vague ("sometime next
     quarter," "before it auto-renews" with no actual date visible anywhere), omit `resolved_
     date` entirely rather than guess — a downstream date-guess from an already-lossy summary is
     exactly the failure mode that made the old Ariba expiration-date signal wrong ~98% of the
     time (task #61/#72); the same discipline applies here, one level earlier, where you still
     have the real email in front of you and don't need to guess at all if you're confident.
   - Example: `{"text": "Notice of non-renewal must be sent 90 days before the anniversary date
     (2026-11-01)", "kind": "hard", "deadline_type": "renewal_notice", "resolved_date":
     "2026-11-01"}`.

4. **Write the updated synthesis** — a 2-4 sentence narrative (who asked what, what's happened,
   where it stands now, informed by the prior synthesis plus what's new), `next_steps` grounded in
   the sourcing-process phase model where applicable, and `suggested_actions` tied to SPECIFIC
   tasks with a one-line rationale each (never a bare label with no "why" — Marc's explicit
   "in-depth, not oversimplified" requirement).

   **`derived_title` (added 2026-08-04, task #52) — ALWAYS include one, every synthesis write, no
   exceptions.** The list/inbox views already prefer this over the issue's own mechanical `title`
   (which is just the raw email subject line, verbatim) everywhere they render a row - this field
   has been fully wired into the UI the whole time; it was simply never asked for here, which is
   the entire reason roughly half of all real open issues still show a raw subject line like
   `"***Please Read: Action Required**** FW:Lilly Cyber SAE-17010 Review Outcome"` or `"Action
   required: Approve the Requisition that JESSICA GOLINO submitted - PR815290-V2..."` instead of
   what the issue is actually about.
   - A short (aim for 5-10 words), real, specific title naming what this issue actually IS - the
     deal, the ask, the decision, the document - never a restatement of the raw subject line's own
     boilerplate ("Action required," "FW:," "***Please Read***," a system's own notification
     framing). Good: `"UneeQ pricing negotiation"`, `"PR815290-V2 approval - Data Spine Phase 2"`,
     `"Veeva Link SOW - extra T&Cs flagged"`. Bad: reusing the subject line, or anything that needs
     the raw subject to make sense.
   - Update it every time you write a new synthesis if what the issue is about has genuinely
     changed (a decision landed, the ask shifted) - don't just carry the old one forward reflexively
     - but don't rewrite it cosmetically on every pass either if the real subject-matter hasn't moved.
   - This is real judgment, same as the summary itself - never a mechanical truncation of the
     subject line or the summary's own first sentence (that would just relocate the same noise
     problem, not solve it).

   **`label` is rendered verbatim as a button's visible text in the real UI — keep it a short,
   imperative call-to-action (2-5 words: "Go to Ariba", "Confirm with Legal", "Approve the SOW"),
   never a full descriptive sentence.** Put the specific, "in-depth" detail (which PR/PO number,
   which supplier, the dollar amount) in `rationale` instead, where there's room for it and it
   won't get visually truncated. Real bug this fixes (2026-08-02, Marc's live screenshot):
   a label like "Approve or reject PR1149359 in Ariba" rendered as a button and got cut off
   mid-PR-number ("Approve or reject PR114935…") - the label was doing the rationale's job.
   `rationale` still needs the "why," same requirement as before; only `label` itself gets shorter.

   Each `next_steps` item MAY also carry a duration
   estimate (see step 5) — omit `estimate_*` fields entirely on a step rather than guessing one:
   ```
   POST /api/workgraph/{entity_type}/{entity_id}/synthesis
   {"summary": "...",
    "derived_title": "...",
    "next_steps": [{"step": "...", "current": true,
                     "estimate_days_low": 3, "estimate_days_high": 5,
                     "estimate_confidence": "documented", "estimate_note": "per the SOW review SLA"}, ...],
    "suggested_actions": [{"task_id": "...", "label": "...", "rationale": "..."}, ...],
    "estimated_completion": {"note": "...", "confidence": "documented"}}
   ```
   The server computes and stores `synthesized_from_marker` itself at write time (from the current
   evidence marker) — you never send one.

4a. **For a project, extract real issues from confirmed cluster content** (added 2026-08-05,
    corrected-ordering pipeline Phase D) — only when the project's `has_confirmed_grouping` field
    (from the same `GET /api/workgraph/projects/{project_id}` response) is `true`. A still-
    `false` project's grouping hasn't cleared a real confidence bar yet (no exact shared
    reference, no confirmed review) — extracting permanent real issues from a match that might
    still get split back apart would produce issues that are wrong too, so leave it at the
    synthesis narrative alone (steps 1-4 above) until a later wake finds it confirmed.

    This is the step Marc described directly: *"raw communications get extracted for data
    points → matched/clustered into a validated group → that validated group becomes the
    project → only THEN does the LLM read the project's real content and extract the actual
    issues/asks/deliverables from inside it."* Once you've extracted (step 3) every new raw_item
    across the project's `clusters`, you have real, already-materialized `ask`/`decision`/
    `commitment` claims sitting on those clusters — this step is where you decide which of them
    genuinely belong together as one separately-trackable real issue, and make that real:
    ```
    POST /api/workgraph/projects/{project_id}/issues
    {"title": "...", "category": "...", "claim_ids": [123, 124, 129]}
    ```
    - `title` — a short, specific, real title for what this ONE issue actually is (same bar as
      `derived_title` above — never a raw subject line, never a restatement of the project's own
      name).
    - `claim_ids` — the specific claim ids (visible on each cluster's evidence, or read back via
      `GET /api/workgraph/issues/{cluster_or_issue_id}` the same way you'd read any issue's
      asks/decisions) that together make up this one real issue's content. Cite ONLY what
      genuinely belongs to this one issue — a single cluster (e.g. a recurring meeting series)
      can carry the material for more than one real issue at once (Marc's own Authenticz
      example: a pricing-negotiation ask and a separate onboarding-scope ask living on the same
      meeting-series cluster's claims) — don't lump them into one issue just because they share a
      source.
    - The route validates every cited claim actually belongs to one of this project's current
      members (a cluster or an already-real issue) and rejects the call otherwise — never retry
      by inventing a claim id; re-read the project's evidence and cite what's actually there.
    - Safe to call more than once per project as more clusters accumulate real content across
      later wakes — each call only ever creates ONE new issue from the claims you cite that wake;
      it never touches issues you already extracted earlier.
    - This does NOT replace step 4's synthesis write for the project itself — write both: the
      project's own narrative (who's involved, where the whole negotiation stands) AND the real
      issue(s) extracted from inside it (the specific trackable asks/deliverables Marc's
      checklist/NBA actually act on).

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
