# Project-grouping routine — curator's (Colleen's) wake checklist

**What this is for:** the deterministic auto-grouper (`workgraph_projects.py`) already merges issues
on a STRONG signal (shared external party, shared external company, or a matching normalized
subject/topic core) with no confirmation needed. What's left over — same category, opened within
the proximity window, but no shared external contact and no subject-core match — is genuinely
ambiguous: it needs a real read of both issues' content to judge, which is your job here, not
mechanical code's. This is real judgment work, same spirit as synthesis (see SYNTHESIS_ROUTINE.md)
— never a mechanical rubber-stamp of every pending suggestion.

**Three possible verdicts per pair, not two.** Confident same underlying deal → confirm (this now
actually merges the two issues into one project, not just marks the suggestion reviewed). Confident
genuinely unrelated → reject (dismisses it, so it stops sitting in Marc's queue for no reason).
Genuinely unsure → do nothing and leave it pending. Abstaining is a real, correct outcome here, not
a failure — Marc would rather see a smaller number of suggestions he actually needs to look at than
have every ambiguous pair force-resolved one way or the other.

## Steps, in order

1. **Get the work list.**
   ```
   GET /api/workgraph/project-suggestions
   ```
   Each item gives you `id`, `issue_id_a`, `issue_id_b`, and `reason` (why the deterministic pass
   flagged this pair — always the weak-signal reason: same category, proximity window, no shared
   external contact/subject match).

2. **For each pending suggestion, read both issues' real content** — not just their titles:
   ```
   GET /api/workgraph/issues/{issue_id_a}
   GET /api/workgraph/issues/{issue_id_b}
   ```
   Each response includes `evidence` (the actual message/meeting summaries), `synthesis` (if either
   has already been synthesized — a fast way to see "what this issue is actually about" without
   re-reading every raw item), `parties`, and `tasks`. Judge from this whether the two are plausibly
   the same underlying deal/negotiation/request, or just coincidentally similar (same category,
   similar timing, nothing more).

3. **Write your verdict:**
   - Confident same deal:
     ```
     POST /api/workgraph/project-suggestions/{id}/resolve
     {"status": "confirmed"}
     ```
     This actually merges `issue_id_a` and `issue_id_b` into a shared project now (joining
     whichever already has one, or creating a new one) — it is a real action, not a formality.
   - Confident unrelated:
     ```
     POST /api/workgraph/project-suggestions/{id}/resolve
     {"status": "rejected"}
     ```
   - Genuinely unsure: make no call at all for this suggestion. It stays pending exactly as it is
     today, for Marc (or a future pass with more evidence) to judge instead.

4. **Report your status:**
   ```python
   import workgraph_store as ws
   ws.set_worker_status("curator", state="idle", current_task=None, detail="project-grouping wake complete")
   ```

5. **Stop.** No ingestion, no synthesis, no re-classification, no team_room posts beyond what this
   routine itself calls for — this wake is scoped to project-suggestion judgment only.

## Safety net

Every suggestion is judged independently — there's no partial-write hazard if this wake dies
partway through. Whatever you haven't resolved yet simply stays pending for the next pass. Merging
is idempotent from the caller's side (confirming a suggestion whose issues are already in the same
project is a safe no-op), but you should still only call resolve ONCE per suggestion per your own
judgment, not re-confirm the same pair repeatedly.

## Bridge candidates (task #169/#170/#174, 2026-08-04, Marc's direct design ask)

A `reason` starting with `"bridge candidate - connects to project ..."` means the deterministic
scorer (`workgraph_projects.scored_grouping_decision`) found this issue (`issue_id_a`) scoring at
or above the auto-merge threshold against a member of an EXISTING, already-established project —
and it found this for TWO OR MORE distinct projects at once, not just one. That's a real, common
shape: one email/PR/notification can genuinely bridge two things that were previously tracked as
separate — the exact case Marc described (a PR + a contract + other correspondence with the same
supplier, filed as separate requisitions, that should live under one project). Each bridged project
gets its OWN pending suggestion (same `issue_id_a`, different `issue_id_b`/project), so you'll see
2+ suggestions sharing the same `issue_id_a` when this happens.

**Before resolving any bridge-candidate suggestion, find its siblings first:**
```
GET /api/workgraph/project-suggestions
```
Group the results by `issue_id_a` yourself — any other pending suggestion with the SAME
`issue_id_a` and a `reason` also starting with `"bridge candidate"` is part of the same bridge
decision, not a separate, unrelated judgment call. Read all of them, plus the real content of
`issue_id_a` and every bridged project's member issue, before deciding — never resolve one half of
a bridge without having looked at the other.

**Four real outcomes, not two:**
- **Only one project is the real fit** → confirm that one suggestion, reject the other(s). This is
  the common case: the bridging item genuinely belongs with one supplier engagement, and the other
  match was coincidental (same descriptor text, different real deal).
- **Both projects are genuinely the same underlying supplier relationship, just split apart
  earlier** → confirm ALL of them. Confirming the second one after the first already succeeded will
  naturally collide two now-non-trivial established projects — `resolve` already handles that
  safely (it comes back as a new `merge_projects`-kind suggestion for you to confirm right there in
  the same response, not a silent failure or a skipped step). Confirm that too if the read supports
  it. The end state is intentional: one item pulling two previously-separate projects back into one,
  which is the whole point of bridge detection existing at all.
- **Neither project is a real fit** (the shared signal was coincidental — e.g. two different
  reps' different requisitions at the same supplier that just happen to share a descriptor) →
  reject all of them.
- **Genuinely unsure** → same as any other suggestion: leave it pending rather than forcing a call.

Never confirm a bridge suggestion on the strength of its `reason` string alone — the deterministic
score got it into your queue, but whether it's actually the same real-world relationship is exactly
the judgment this routine exists for.
