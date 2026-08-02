# Design: per-checklist-item Aristotle gating

**Status:** design only (task #34). No live code changed by this doc. The
build is tracked separately as task #49, gated behind Marc reviewing this.

## The real gap, grounded in the current code

`workgraph_aristotle.check_prerequisites(issue_id, raw_items)` (aristotle.py:209)
loops over an issue's `raw_items`, and for the first one whose `signal_type`
matches an active rule that isn't yet satisfied, returns a single
`{"warning": ..., "rule_id": ...}` and stops:

```python
for item in raw_items:
    signal_type = item.get("signal_type")
    if not signal_type or signal_type in checked_signal_types:
        continue
    checked_signal_types.add(signal_type)
    rules = ws.get_active_prerequisite_rules_for_trigger(signal_type)
    for rule in rules:
        if not _prerequisite_satisfied(issue_id, rule):
            return {"warning": _build_warning(rule), "rule_id": rule["id"]}
return None
```

Two real consequences of this shape:

1. **First-match-wins.** If an issue has two raw_items that each trigger a
   different unsatisfied rule, only the first one found is ever reported.
   The second is silently dropped - not wrong, exactly, since the issue
   really is gated either way, but incomplete.
2. **No raw_item_id on the result.** The function knows which `item` in
   the loop had the triggering `signal_type` at the moment it returns, but
   throws that away - the caller only ever sees the rule's warning text and
   `rule_id`, never which specific raw_item carried the trigger.

Right now this doesn't matter much, because the only consumer is
`workgraph_nba.score_issue()`, which folds the warning into the issue's
single `nba_reason` string and a single `has_unmet_prerequisite` boolean
(nba.py:316-358) - both issue-level, one warning per issue, full stop.

## Why it matters now

This session's Detail Panel port unified the Checklist zone so every ask,
decision, commitment, and repeat signal is individually tied to the real
`raw_item_id` that produced it (`checklistItems` in `pccRenderDetail`,
cockpit.html) - and `candidate_actions()` already does the same
raw_item_id-based matching to scope a suggested action to one specific
checklist row instead of a generic issue-level bucket (task #36's earlier
groundwork this session).

Aristotle gating is still stuck at the old, coarser grain: a single
"Cleared to act" / "Gated" banner for the whole issue, with no way to tell
Marc *which* ask or commitment is the one actually blocked. On an issue
with, say, three asks and one of them is a signature request gated behind
an unseen PO approval, today's UI can only say "this issue is gated" -
not "this specific signature-request row is gated; the other two asks are
fine." That's a real loss of precision the rest of the port already has
everywhere else.

## Proposed design

### 1. New function, not a changed one

Add `check_prerequisites_all(issue_id, raw_items) -> list[dict]` that does
the same rule lookups but does **not** stop at the first match - it
collects every `(raw_item_id, rule)` pair where the rule is unsatisfied:

```python
def check_prerequisites_all(issue_id: str, raw_items: list[dict]) -> list[dict]:
    """Like check_prerequisites, but returns every unsatisfied match
    across all raw_items, each tagged with the raw_item_id that carried
    the triggering signal - not just the first one found."""
    results = []
    checked_signal_types: set[str] = set()
    for item in raw_items:
        signal_type = item.get("signal_type")
        if not signal_type or signal_type in checked_signal_types:
            continue
        checked_signal_types.add(signal_type)
        rules = ws.get_active_prerequisite_rules_for_trigger(signal_type)
        for rule in rules:
            if not _prerequisite_satisfied(issue_id, rule):
                results.append({
                    "warning": _build_warning(rule), "rule_id": rule["id"],
                    "raw_item_id": item.get("id"),
                })
    return results
```

Then make `check_prerequisites()` a thin wrapper over it, so the two can
never drift out of sync and there's exactly one place the actual
satisfaction logic lives:

```python
def check_prerequisites(issue_id, raw_items):
    all_checks = check_prerequisites_all(issue_id, raw_items)
    return all_checks[0] if all_checks else None
```

This is zero-risk for every existing caller (`score_issue`, `gate_board`,
all of `test_workgraph_aristotle.py`) - same signature, same return shape,
same first-match value, since a plain loop-with-early-return and
"collect everything then take index 0" produce an identical first result
for the same input order.

**Important framing for whoever builds this:** the `raw_item_id` attached
to each result is the raw_item that carries the *triggering* signal (the
signature request, say) - not a raw_item that's somehow "missing" the
required signal. There's no item to point to for an absence; that's the
whole nature of the check (see aristotle.py's own "no confirmation seen
yet, never a claim it didn't happen" framing). A future UI must not imply
otherwise - the badge belongs on the row whose evidence *raised* the gate,
not on some hypothetical row for the still-unseen approval.

### 2. Wiring into the issue-detail endpoint

`server_lean.py`'s issue-detail route already fetches `raw_items` for
value extraction and calls `workgraph_aristotle.check_prerequisites`
indirectly via `issue["has_unmet_prerequisite"]` (set earlier by
`recompute_all`, not recomputed live here). Add one call:

```python
per_row_gates = workgraph_aristotle.check_prerequisites_all(issue_id, raw_items)
gated_by_raw_item = {g["raw_item_id"]: g for g in per_row_gates if g["raw_item_id"] is not None}
```

then attach it the same way `occurred_ts`/`raw_item_id` already got
piggybacked onto evidence this session - either:

- **(a)** as a new top-level `d["gated_raw_items"]` map the frontend
  matches against checklist rows by `rawItemId` (mirrors how
  `checklistByRawItem` already matches `candidate_actions` today), or
- **(b)** attach a `gated` field directly onto each ask/decision/
  commitment/repeat-signal dict server-side, next to their existing
  `raw_item_id`/`occurred_ts`/`deep_links` fields.

Recommend **(a)** - it's one extra lookup dict instead of touching four
different data-fetch call sites (`_texts_for_issue` for asks/decisions,
commitments' own fetch, repeat_signals' own fetch), and it matches the
existing `checklistByRawItem` pattern already proven this session.

### 3. Frontend consumption

In `pccRenderDetail`'s checklist construction (where `checklistItems` is
built), look up `d.gated_raw_items[it.rawItemId]` per row. When present,
render a small gated marker on that specific row - reusing the existing
`gatedBadgeHtml`/"Gated" `.pcc-deadline-badge` styling already used in the
header, just scoped to the one row, plus the warning text available on
click/expand (the row already has a collapsible body via `pccXTog`).

The issue-level "Cleared to act" zone (added this session) stays exactly
as-is - it's the correct *aggregate* view ("is anything on this issue
gated at all"). The per-row badge is the added *specific* view ("which
thing"). Both are useful; neither replaces the other.

## Edge cases to design around when building this

- **Same rule, multiple raw_items.** Two separate DocuSign requests on one
  issue, same unsatisfied rule, two different raw_items → both should get
  their own row-level badge. Each row is tied to its own raw_item and
  checked independently; this is correct, not something to dedupe away.
- **A checklist row with no matching raw_item_id** (a "General" row from
  an unscoped candidate action) never gets a gate badge - there's nothing
  to key the lookup on. That's fine; it just means it stays covered only
  by the issue-level zone, same as today.
- **Cost.** `check_prerequisites_all` does the same rule lookups
  `check_prerequisites` already does, just without the early return - for
  the realistic number of distinct signal_types on one issue (small,
  already bounded by the existing `checked_signal_types` set), this is not
  a new performance concern. It should only ever be called per-issue-
  detail-view (when Marc actually opens an issue), never inside
  `recompute_all`'s bulk per-issue loop or `gate_board`'s portfolio scan -
  those two callers only need the single aggregate reason and should keep
  calling the thin `check_prerequisites()` wrapper.

## Test plan for the build (task #49)

New tests in `test_workgraph_aristotle.py`:
- `check_prerequisites_all` returns one entry per genuinely unsatisfied
  raw_item/rule pair, each carrying the correct `raw_item_id`.
- Two raw_items triggering the *same* rule both appear as separate entries
  (not deduped to one).
- `check_prerequisites()` still returns exactly what it did before this
  change, for every existing test case in this file (regression, not a
  behavior change for existing callers).
- Empty/no-trigger cases return `[]` / `None` respectively, unchanged.

New tests in `test_server_lean.py` (or wherever the issue-detail endpoint
is already covered): `d["gated_raw_items"]` present and correctly keyed
when an issue has a per-row-attributable gate; absent/empty when it
doesn't.
