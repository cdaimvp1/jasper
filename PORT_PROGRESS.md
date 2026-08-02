# Detail Panel port + autonomous task queue — recovery log

**If you are a fresh Claude session reading this after a restart/compaction:**
this file plus `git log` on this repo (branch `main`) is the source of truth
for what's actually done. Task list state (TaskList tool) should also still
have current status per task ID referenced below. Read both before assuming
anything is or isn't finished.

**Standing instructions from Marc (2026-08-02), still in force:**
- Continue autonomously through task #47, then #18/#21/#34/#36/#45/#48, in
  that order, without stopping to ask unless genuinely blocked.
- Do not drift or hallucinate - every claim of "done" must be backed by a
  real passing test and/or a real live-server check, not asserted from
  memory.
- Test often. Commit/push often - after each real, coherent piece, not
  batched into one giant commit at the end.
- Keep this file current as you go, so context loss doesn't lose the thread.
- Tasks #35, #37, #44 and everything tagged "[Future phase]" (#38-43) are
  explicitly NOT part of this autonomous run - do not start them without
  Marc asking again.

## Task #47 — port v7 mockup design into real cockpit.html

Real decisions already made this session (don't re-litigate):
- Checklist keeps the real app's existing bundling (asks + decisions +
  commitments + repeat/escalated signals together) - do NOT split decisions/
  commitments out into Progress the way the standalone mockup did. That was
  confirmed explicitly after finding the real "Checklist rework, task #124
  follow-on" was a deliberate earlier design decision.
- `--lk-pri` (shared :root token, 18 usages across Detail Panel + chat
  composer + inbox row indicator) changed from plum to brand red
  `#E1251B`, app-wide, on Marc's direct call - not scoped to just this
  panel. `--lk-danger` untouched (already identical to `--red-d`).
- Dark mode: do NOT add new dark-mode-specific overrides for the tokens
  touched here - `--lk-pri` had no dark override before this change and
  still doesn't; that's intentional (matches "the mockup's light colors,
  not its dark mode").
- Stakeholder compose: port the mockup's plain `mailto:` link mechanism
  (client-only, no backend call) as the INTERIM compose action. Do NOT
  build the real Outlook-COM `compose_new` action here - that's task #35,
  explicitly not part of this autonomous run.
- Per-row done/snooze/dismiss icons: UI/interaction only in this pass. Do
  NOT claim or imply these persist anywhere real - task #44 (a real
  "dismissed" state distinct from "done") is not part of this run either.
  Wire them so they're honest about being non-persistent for now (e.g. a
  visible note), not silently no-op-ing behind a convincing-looking button.

### Pieces, in order

1. [DONE, committed `3b711ea`] Header: reordered (title -> merged status/
   reason pill -> trimmed meta line), `--lk-pri` app-wide color switch,
   icon/logo swap (separate concern, same commit). Verified live against
   the real running server (200s, correct classes present in served HTML).
2. [DONE, committed `3b711ea`] Backend groundwork for checklist dates:
   `deep_links.attach_deep_links()` now also attaches `occurred_ts` per row
   (piggybacks on the lookup it already does - zero extra queries). Real
   tests added (`test_attach_deep_links_attaches_real_occurred_ts`,
   `..._occurred_ts_none_when_raw_item_missing`). Full suite green before
   commit.
3. [DONE, committed `3b711ea`] Backend groundwork for scoped checklist
   actions: `workgraph_nba.candidate_actions()` now attaches
   `raw_item_id` to `evidence_row`-surfaced candidates (the only surface
   with a real single raw_item to point to). Real tests added
   (`test_candidate_actions_evidence_row_candidate_carries_raw_item_id`,
   `..._nba_surface_has_no_raw_item_id`). Full suite green before commit.
4. [DONE, not yet committed as of this writing - committing alongside #5]
   Checklist row rewrite (CSS+JS in cockpit.html): date-left column (using
   occurred_ts), 3-icon toolbar (mark done/snooze/dismiss - visual only,
   see caveat above), scoped action slot matching candidate_actions to the
   specific ask/decision/commitment/repeat row sharing a raw_item_id
   (unmatched candidates become "General" rows in the same list, no
   separate zone), collapsed-by-default rows. Also found and removed two
   more real live duplications while doing this: (a) a standalone
   nextActionsZoneHtml zone that duplicated the same candidate_actions now
   folded into the checklist - removed, along with the now-dead
   primaryCandidate/primaryLabel/primaryKind/primaryInstructions variables;
   (b) a standalone suggestZoneHtml zone rendering
   activeSynth.suggested_actions, which the server already merges into
   candidate_actions via the same synthesis/project_synthesis fallback -
   removed. Also deduped the header's actions row against the sidebar's
   sideActionsHtml (they rendered the exact same primary/mark-done/snooze
   buttons twice) - header now only keeps its kebab menu.
5. [DONE, not yet committed as of this writing - committing alongside #4]
   Progress timeline: stripped `pccTlRowHtml`'s inline
   `item.recommendations` rendering (`.pcc-tl-rec-list`/`.pcc-tl-rec`/"Run
   this" - all removed) - this was the SAME duplication problem found in
   the standalone mockup work, an evidence_row recommendation showing once
   via candidate_actions/checklist AND again via the Progress timeline row.
   `.pcc-tl-row`/`.pcc-tl-thread-hd` CSS changed from a 2-column grid
   (content + 210px action column) to full-width/flex since there's no
   action column left; the thread-header's empty rec-spacer div removed
   too. Progress is now history-only, full width, date-right via
   `.pcc-tl-age`'s existing `margin-left:auto` (matches mockup's "history
   only" rule for real this time).
   Verified: grepped the whole file for `pcc-tl-rec`, `primaryCandidate`,
   `nextActionsZoneHtml`, `suggestZoneHtml`, `otherCandidates`,
   `altActionsHtml` - zero live references left, only one explanatory
   comment. Full test suite green (`pytest -q`, all passing, no failures)
   before this commit.
6. [DONE, committing now] Stakeholder redesign: real parties grouped by
   company (external, header "{company} (External)", falls back to plain
   "External" when company is unknown) and "Lilly" (internal) - dot-toggle
   multi-select (`pccStakeToggle`) replacing the old flat chip-pill list,
   envelope button (`pccComposeSelected`) opens a client-only `mailto:`
   draft to just the selected people, subject carries a "Ref: JW-<id>" tag
   (task #36's own design, applied inline to this one interim action per
   the mockup's own legend - task #36's real build is the separate,
   generic inbound-matching mechanism). "confirmed" badge shown when a
   party's affiliation_source is manual_correction (real signal - the
   /api/workgraph/parties/{id}/correct endpoint sets this). No star/
   primary marker - per-issue parties have no is_primary field (that's
   project-scope only); this matches what was already true before the
   port, not a regression. Added missing `.pcc-src-tag`/`.pcc-src-real`
   CSS (used by the mockup but never defined in the real file) using the
   file's existing --blue-t/--blue-d tokens, consistent with how
   pcc-procbox-gate already mixes old-system blue into lk-scoped markup.
   Verified live: full pytest suite green, server restarted, curl against
   /cockpit (not /) shows pcc-stake-row/pccStakeToggle/pccComposeSelected/
   pcc-envelope-btn/pcc-src-real present, zero live references to any
   piece-4/5-removed variable (one comment-only match, expected).
7. [VERIFIED ALREADY SATISFIED - no code change needed] Internal vertical
   scrollbar on the panel body. Traced the full height chain by reading
   the actual CSS (not assumed): `.app{height:calc(100vh - 56px);
   overflow:hidden}` (line ~2938) -> `.main{height:100%;overflow:hidden;
   display:flex;flex-direction:column}` (line ~2944) -> `#pccBoardWrap{
   flex:1;min-height:0;display:flex;flex-direction:column}` (line ~2665)
   -> `.pcc-board{flex:1;min-height:0}` (line ~2662) -> `.card.pcc-detail-
   card{overflow-y:auto;min-height:0}` (line ~2668, reinforced at ~3093).
   This is a fully bounded flex-height chain with `overflow-y:auto` on the
   detail card specifically - it already has its own independent
   scrollbar, distinct from the page (page-level scroll is impossible,
   `.app`/`.main` are both `overflow:hidden`) and distinct from the issue-
   list pane's own separate scroll (`.pcc-list{overflow-y:auto}`, line
   ~2753). This predates the mockup port (task #124 era) and none of this
   session's edits (checklist rewrite, Progress strip, stakeholder
   redesign) touched the ancestor chain or introduced a competing fixed
   height/overflow rule - checked by reading every `.pcc-detail-grid`/
   `.pcc-detail-main`/`.pcc-detail-side` rule, no conflicts found. No
   headless-browser tool is available in this environment to screenshot-
   verify the rendered scrollbar directly, so this is a source-level
   verification (deterministic for a bounded flex chain, not something
   that needs a pixel check to confirm) rather than a visual one - flagged
   explicitly rather than silently treated as equal to a live check.
8. [DONE, verified live] Final full-suite test run + live server
   verification of the whole panel. `pytest -q` green (all files, no -k
   filter) after every piece above. Server restarted after each piece;
   curl against `/cockpit` (the real route - `/` is a different landing
   page) confirmed 200 + presence of new markup/classes + zero live
   references to anything removed. #47 is complete - committing/pushing
   final state now.

### Post-completion correction (2026-08-02): visual-fidelity audit

Marc flagged, after #47 was marked done, that the port did not actually
look like the mockup he'd iterated on - a real, valid complaint. Instead
of trusting my own summary of the work, ran a mechanical, line-by-line
diff (Explore agent) of the mockup file against the real CSS/JS, with
instructions to report concrete discrepancies only, not opinions. Findings
and fixes, ranked by what the audit found highest-impact:

1. Only `--lk-pri`/`--lk-danger`-adjacent tokens had moved to the mockup's
   palette - `--lk-panel`/`--lk-nested`/`--lk-line`/`--lk-line2`/`--lk-ink`/
   `--lk-ink2`/`--lk-mut`/`--lk-mut2`/`--lk-elev` were still the old
   neutral olive/beige theme. Ported the mockup's full :root block
   verbatim. Also caught and fixed: `--lk-danger` had been left at
   `--red-d` (byte-identical to the primary), recreating exactly the
   "blocked reads as just another button" problem the mockup's palette
   was designed to avoid - now the mockup's actual `#521207` deep maroon.
2. The entire `.pcc-xrow` checklist-row CSS family was missing - the JS
   (`pccCheckRowHtml`) already emitted `.pcc-xrow`/`.pcc-xrow-date`/
   `.pcc-micro-btn`/`.chev`/etc, but NOTHING styled them, so every
   checklist row rendered as unstyled stacked divs. This was probably the
   single biggest visible gap. Ported the full rule set from the mockup.
3. Sidebar Actions was missing 4 of 6 mockup buttons (Dismiss, Open email,
   Draft reply, Draft forward) and used the wrong button component
   (`.mqb.ghost` instead of `.pcc-actbtn`, which didn't exist in CSS
   either). Restored all 6 using real data: Dismiss is visual-only (same
   honesty pattern as checklist icons, new `pccSideDismissNote`), the mail
   actions pull from the first evidence item that actually has deep links
   (never fabricated, simply absent when no evidence has any yet).
4. "Cleared to act" - an always-visible Aristotle gate zone in the mockup
   - only existed conditionally inside "Procurement detail" (gated behind
   having a reference id or dollar value). Added as its own always-shown
   zone; left the conditional Procurement-detail box in place rather than
   removing it (that's real informational content, not just styling -
   changing what data shows, not just how, felt like the wrong call to
   make unilaterally after already being told once this session not to
   change more than asked).
5. Zone headers (`.pcc-zone-hd`) were still the old bold/banded/bordered
   "HOME-SURFACE" style, not the mockup's plain small-caps-mono label.
   Fixed via a `.pcc-detail-main .pcc-zone-hd` override (scoped so it
   doesn't also reskin the Project tab's zone headers, which share the
   bare class and weren't part of what Marc asked to match here).
6. Sidebar block order was Stakeholders/At a glance/Actions; mockup is
   Actions/Stakeholders/At a glance. Reordered.
7. `.pcc-src-real` and the gate-status/precedent colors used `--blue-d`
   instead of `--bblue` (mockup's actual "cleared" blue - already existed
   in the real file at the correct value, just unused by these rules).
   Fixed. Precedent stat now highlights blue when `days_to_close <= 1`
   (mockup's "fast precedent" emphasis), shows the real day count.
8. Per-row attachment card (two-column, name/date/type left + description
   right) was entirely gone from checklist rows - attachments had been
   generalized into a separate "Related documents" zone instead. Restored
   the per-row card via a real join (a "reference"-kind attachment's
   entity_id IS its raw_item_id, same relationship
   list_attachments_for_issue already uses server-side) - and filtered
   Related documents to exclude whatever's now shown per-row, so the same
   attachment doesn't render twice.
9. Minor spacing/font-size drift fixed: detail title 17px, meta-line 12px,
   headline-pill 12.5px, side column 280px, side-block margin 22px,
   side-sub margin 10px/4px, envelope button now right-aligned via
   `.pcc-side-hd{display:flex;justify-content:space-between}`, added
   missing `.pcc-not-real-note` styling.

Deliberately NOT changed, and why:
- Amber/teal token values (`--amber-t`/`--amber-d`/`--teal-t`/`--teal-d`)
  drift slightly from the mockup's exact PMS values, but these are old,
  app-wide-shared tokens used in badges/tags far outside the Detail Panel
  - remapping them globally for a Detail-Panel-only hover-color match was
  a bigger blast radius than the ask warranted. Low visual impact anyway
  (amber is a near-miss; teal isn't even used by anything the real
  Detail Panel currently renders).
- The mockup's "Run Contract Review" completed/re-run-warning flow
  (View Redlined DOCX link, re-run confirmation) is a real feature gap,
  not ported - it needs a real "already ran" signal per checklist row
  that doesn't exist in the data model yet. Flagging rather than faking
  it with a fake timer, consistent with this app's own honesty rule.
- Progress's channel-tag taxonomy (`.pcc-chan-chip`) vs. the mockup's
  kind-tag taxonomy (`.pcc-kind-decision`/etc), and the missing
  stakeholder "primary contact" star, are BOTH already-made, correct
  decisions from earlier this session (Checklist absorbed decision/
  escalation semantics per Marc's own confirmed choice; per-issue parties
  genuinely have no is_primary field) - not misses, listed here so a
  future session doesn't "fix" them back into duplicating something.

Full pytest suite green; all 17 real inline `<script>` blocks pass
`node --check` (only the expected Jinja `{{ }}` tag flags); server
restarted and curl-verified against `/cockpit` for every new class/value
and zero dangling references to anything removed.

### Task-list additions since this file was first written (2026-08-02)

Marc asked for a commercial-value gut-check and a full task-list audit
mid-run. Three new build-follow-on tasks were added to the tracker (not
part of this autonomous run - each is gated behind its design task landing
and Marc reviewing it):
- #49 - build Aristotle gating, gated behind #34.
- #50 - build the SME panel, gated behind #45.
- #51 - build the telemetry page, gated behind #48.
No change to the approved autonomous sequence (#47 -> #18 -> #21 -> #34 ->
#36 -> #45 -> #48) - these three are follow-on, not inserted into it.

## Then, in order, once #47 is fully done/stable/pushed:

- #18 — remove duplicate "Back Office" label (2 known lines:
  cockpit.html:3504 and :5423 per the original task note - RE-VERIFY these
  line numbers live before editing, this file has moved a lot during the
  port; do not trust the cached line numbers blindly).
- #21 — fix `candidate_actions()`'s project-synthesis fallback
  truthy-empty-dict bug. Read the real code first to confirm the bug is
  exactly what the task title implies before fixing it.
- #34 — design/scope per-checklist-item Aristotle gating. Design-only
  document, no live code change. (Real finding already in the task
  description: `check_prerequisites()` loops per-raw_item internally but
  stops at the first match and doesn't expose which raw_item triggered it.)
- #36 — build the email reference-tag mechanism. Design already written
  in the task description (short quiet text token in the signature/
  subject, e.g. "Ref: JW-<issue-id>", NOT a fully-invisible header/comment
  approach). Build: append the tag when drafting/composing via
  outlook_actions.py; have the ingest/classify path check for the pattern
  as a fallback matching signal.
- #45 — write the AI-native SME panel + synthesis design doc for
  contract_review. Design-only, no build. Real grounding already gathered
  this session: `references/sme-matrix.md` (18-person real SME matrix),
  `references/playbook.md` (Position/Hard-Stop/Acceptable-fallback ladder),
  `references/risk-scoring.md` (existing multi-pass architecture) inside
  `documents/reference/skills/lilly-contract-review-1c344a/`.
- #48 — research/design the leadership telemetry + time-saved page.
  Design-only. Audit `nba_choice_log`, `issue_state_history` (actor
  column), and bus.db for what's already logged before assuming new
  instrumentation is needed - see the task's own description for the full
  brief.

## Discipline reminders (from this session, don't relearn the hard way)

- Two Python interpreters exist: `../venv/Scripts/python.exe` (runs the
  real app) and the WindowsApps python.exe (has pytest) - use the
  WindowsApps one for all test runs, matching every prior test invocation
  this session.
- `TEAM_DATA_DIR` and `TEAM_HOME` env vars are required for every Python
  invocation, always.
- Server restart pattern after any live-code change: find the PID on port
  8700 (may be orphaned, not tracked by Task Scheduler - check both),
  Stop-Process, then Start-ScheduledTask "SymphonyCockpitServer".
- Run the FULL test suite (no -k filter) before every commit, not just the
  file(s) touched.
- Never amend a previous commit - always a new one.
