"""Regression tests for workgraph_sequences.py (task #322) - recurring
multi-step signal_type sequences mined across closed/completed projects of
the same category, surfaced as read-only predictive context (never a
gate/rule - that's workgraph_aristotle.py's job, exercised separately in
test_workgraph_aristotle.py)."""
import time

import workgraph_sequences as seq


def _issue(ws_db, title="Issue", project_id=None, category="procurement"):
    issue_id = ws_db.create_issue_with_new_id(title=title, state="active", category=category)
    if project_id:
        ws_db.update_issue(issue_id, project_id=project_id)
    return issue_id


def _raw_item_at(ws_db, issue_id, signal_type, key, ts):
    row_id = ws_db.insert_raw_item(
        source="outlook_mail", stable_key=key, thread_key=key, dedupe_key=key,
        occurred_ts=ts, subject="s", from_actor="a@example.com",
        participants_json="[]", body_preview="b",
    )
    ws_db.link_raw_item_to_issue(row_id, issue_id)
    conn = ws_db._connect()
    conn.execute("UPDATE raw_items SET signal_type = ? WHERE id = ?", (signal_type, row_id))
    return row_id


def _closed_project_with_stages(ws_db, name, category, stages, start_ts):
    """Creates a 'done' project in `category` whose one issue carries one
    raw_item per (signal_type, offset) in `stages` - a list of (signal_type,
    ts_offset) pairs, applied on top of start_ts."""
    proj = ws_db.create_project_with_new_id(name=name, category=category, status="done")
    issue_id = _issue(ws_db, name, project_id=proj, category=category)
    for i, signal_type in enumerate(stages):
        _raw_item_at(ws_db, issue_id, signal_type, f"{name}-{i}", start_ts + i)
    return proj


# --- stage_sequence_for_project ---------------------------------------------

def test_stage_sequence_orders_by_first_occurrence_and_dedupes(ws_db):
    proj = ws_db.create_project_with_new_id(name="P", category="procurement", status="active")
    issue_id = _issue(ws_db, "I", project_id=proj)
    _raw_item_at(ws_db, issue_id, "intake_new_project_assigned", "k1", 100.0)
    _raw_item_at(ws_db, issue_id, "contractpodai_review_requested", "k2", 200.0)
    # a second, later occurrence of the SAME signal_type (e.g. a second
    # approver) - must not appear twice or move the stage's position.
    _raw_item_at(ws_db, issue_id, "intake_new_project_assigned", "k3", 300.0)
    _raw_item_at(ws_db, issue_id, "signature_requested_docusign", "k4", 400.0)

    stages = seq.stage_sequence_for_project(proj)

    assert stages == ["intake_new_project_assigned", "contractpodai_review_requested",
                       "signature_requested_docusign"]


def test_stage_sequence_empty_for_project_with_no_signal_types(ws_db):
    proj = ws_db.create_project_with_new_id(name="P", category="procurement", status="active")
    issue_id = _issue(ws_db, "I", project_id=proj)
    row_id = ws_db.insert_raw_item(
        source="outlook_mail", stable_key="k1", thread_key="k1", dedupe_key="k1",
        occurred_ts=100.0, subject="s", from_actor="a@example.com",
        participants_json="[]", body_preview="b",
    )
    ws_db.link_raw_item_to_issue(row_id, issue_id)  # never classified - signal_type stays NULL

    assert seq.stage_sequence_for_project(proj) == []


# --- detect_sequence_patterns_for_category ----------------------------------

_FULL_CHAIN = ["intake_new_project_assigned", "contractpodai_review_requested",
               "signature_requested_docusign", "signature_completed_docusign"]


def test_detects_recurring_multi_step_sequence_across_two_projects(ws_db):
    proj_a = _closed_project_with_stages(ws_db, "A", "procurement", _FULL_CHAIN, 100.0)
    # same order, but with an unrelated stage interleaved - order-preserving
    # (not contiguous) matching must still count this project.
    proj_b = _closed_project_with_stages(
        ws_db, "B", "procurement",
        ["intake_new_project_assigned", "ariba_pr_partial_approval", "contractpodai_review_requested",
         "signature_requested_docusign", "signature_completed_docusign"],
        1000.0,
    )
    # a third closed project that only shares a PARTIAL sub-chain (no legal
    # review at all) - must not count toward the full 4-step pattern.
    _closed_project_with_stages(ws_db, "C", "procurement",
                                 ["intake_new_project_assigned", "signature_requested_docusign"], 2000.0)

    patterns = seq.detect_sequence_patterns_for_category("procurement")

    full_chain_matches = [p for p in patterns if p["step_sequence"] == _FULL_CHAIN]
    assert len(full_chain_matches) == 1
    top = full_chain_matches[0]
    assert top["total_projects_in_category"] == 3
    assert top["matching_project_count"] == 2
    assert set(top["matching_project_ids"]) == {proj_a, proj_b}


def test_subsumption_drops_shorter_pattern_with_identical_evidence(ws_db):
    _closed_project_with_stages(ws_db, "A", "procurement", _FULL_CHAIN, 100.0)
    _closed_project_with_stages(ws_db, "B", "procurement", _FULL_CHAIN, 1000.0)

    patterns = seq.detect_sequence_patterns_for_category("procurement")

    sequences = [tuple(p["step_sequence"]) for p in patterns]
    # The full 4-step chain is kept...
    assert tuple(_FULL_CHAIN) in sequences
    # ...but every length-3 contiguous sub-window of it, which has the exact
    # same 2-project evidence set, is subsumed rather than listed separately.
    assert tuple(_FULL_CHAIN[0:3]) not in sequences
    assert tuple(_FULL_CHAIN[1:4]) not in sequences


def test_requires_minimum_two_matching_projects(ws_db):
    _closed_project_with_stages(ws_db, "A", "procurement", _FULL_CHAIN, 100.0)
    _closed_project_with_stages(ws_db, "B", "procurement",
                                 ["intake_new_project_assigned", "ariba_pr_fully_approved", "concur_expense_reminder"],
                                 1000.0)

    patterns = seq.detect_sequence_patterns_for_category("procurement")

    assert patterns == []  # only ONE project shows the 4-step chain - not "recurring"


def test_requires_minimum_projects_in_category(ws_db):
    _closed_project_with_stages(ws_db, "A", "procurement", _FULL_CHAIN, 100.0)
    # only one closed project total in this category - can't support an
    # honest "M of N" statement yet, regardless of how repetitive its own
    # stage list is.
    assert seq.detect_sequence_patterns_for_category("procurement") == []


def test_open_projects_are_not_counted(ws_db):
    _closed_project_with_stages(ws_db, "A", "procurement", _FULL_CHAIN, 100.0)
    proj_b = ws_db.create_project_with_new_id(name="B", category="procurement", status="active")
    issue_b = _issue(ws_db, "B", project_id=proj_b)
    for i, st in enumerate(_FULL_CHAIN):
        _raw_item_at(ws_db, issue_b, st, f"b-{i}", 1000.0 + i)

    # Still below MIN_PROJECTS_FOR_CATEGORY (only 1 CLOSED project) even
    # though a second, open project exists with the identical chain.
    assert seq.detect_sequence_patterns_for_category("procurement") == []


def test_dismissed_projects_are_excluded_from_completed_history(ws_db):
    _closed_project_with_stages(ws_db, "A", "procurement", _FULL_CHAIN, 100.0)
    proj_b = ws_db.create_project_with_new_id(name="B", category="procurement", status="dismissed")
    issue_b = _issue(ws_db, "B", project_id=proj_b)
    for i, st in enumerate(_FULL_CHAIN):
        _raw_item_at(ws_db, issue_b, st, f"b-{i}", 1000.0 + i)

    assert seq.detect_sequence_patterns_for_category("procurement") == []


def test_different_category_never_counted_together(ws_db):
    _closed_project_with_stages(ws_db, "A", "procurement", _FULL_CHAIN, 100.0)
    _closed_project_with_stages(ws_db, "B", "legal_only_category", _FULL_CHAIN, 1000.0)

    assert seq.detect_sequence_patterns_for_category("procurement") == []


# --- recompute_and_store / list_sequence_patterns (workgraph_store) --------

def test_recompute_and_store_persists_and_is_idempotent(ws_db):
    _closed_project_with_stages(ws_db, "A", "procurement", _FULL_CHAIN, 100.0)
    _closed_project_with_stages(ws_db, "B", "procurement", _FULL_CHAIN, 1000.0)

    result = seq.recompute_and_store()
    assert result["categories_scanned"] >= 1
    assert result["patterns_stored"] >= 1

    stored = ws_db.list_sequence_patterns(category="procurement")
    assert any(p["step_sequence"] == _FULL_CHAIN for p in stored)

    # Recomputing again with no data change must not duplicate the row -
    # replace_sequence_patterns deletes the category's old rows before
    # reinserting, so a re-run is idempotent, not additive.
    seq.recompute_and_store()
    stored_again = ws_db.list_sequence_patterns(category="procurement")
    assert len(stored_again) == len(stored)


def test_replace_sequence_patterns_wholesale_replaces_stale_rows(ws_db):
    """Store-level test, independent of the detection algorithm: a category
    recomputed with a NEW pattern set must lose its OLD rows entirely, not
    accumulate them - the real fix a naive upsert-only writer would miss
    (see replace_sequence_patterns' own docstring: a pattern's support can
    shrink as well as grow between recomputes)."""
    ws_db.replace_sequence_patterns("procurement", [{
        "step_sequence": ["a", "b", "c"], "total_projects_in_category": 3,
        "matching_project_count": 2, "matching_project_ids": ["p1", "p2"],
    }])
    assert len(ws_db.list_sequence_patterns(category="procurement")) == 1

    ws_db.replace_sequence_patterns("procurement", [{
        "step_sequence": ["x", "y", "z"], "total_projects_in_category": 4,
        "matching_project_count": 3, "matching_project_ids": ["p1", "p2", "p3"],
    }])
    rows = ws_db.list_sequence_patterns(category="procurement")
    assert len(rows) == 1
    assert rows[0]["step_sequence"] == ["x", "y", "z"]
    assert rows[0]["matching_project_ids"] == ["p1", "p2", "p3"]


def test_recompute_daily_if_due_gates_once_per_day(ws_db):
    _closed_project_with_stages(ws_db, "A", "procurement", _FULL_CHAIN, 100.0)
    _closed_project_with_stages(ws_db, "B", "procurement", _FULL_CHAIN, 1000.0)

    now = time.time()
    first = seq.recompute_daily_if_due(now=now)
    assert first is not None
    assert first["patterns_stored"] >= 1

    second = seq.recompute_daily_if_due(now=now)
    assert second is None  # same day - gated


# --- top_pattern_for_category (the one consumer) ----------------------------

def test_top_pattern_for_category_returns_informational_note(ws_db):
    proj_a = _closed_project_with_stages(ws_db, "A", "procurement", _FULL_CHAIN, 100.0)
    proj_b = _closed_project_with_stages(ws_db, "B", "procurement", _FULL_CHAIN, 1000.0)
    seq.recompute_and_store()

    result = seq.top_pattern_for_category("procurement")

    assert result is not None
    assert result["step_sequence"] == _FULL_CHAIN
    assert result["matching_project_count"] == 2
    assert result["total_projects_in_category"] == 2
    assert set(result["matching_project_ids"]) == {proj_a, proj_b}
    assert "have historically also involved" in result["note"]
    assert "2 of 2" in result["note"]
    assert "procurement" in result["note"]


def test_top_pattern_for_category_none_when_no_category(ws_db):
    assert seq.top_pattern_for_category(None) is None
    assert seq.top_pattern_for_category("") is None


def test_top_pattern_for_category_none_when_nothing_stored_yet(ws_db):
    assert seq.top_pattern_for_category("procurement") is None


# --- deviation_note_for_project / deviation_notes_for_projects (task #374) -
# Informational-only expected-next-step deviation notes: does an OPEN
# project's own observed stage history still contain the category's
# strongest mined chain, in order? Never a gate - see workgraph_sequences'
# own module-level docstring addendum for the design reasoning.

def test_deviation_note_for_project_flags_missing_typical_step(ws_db):
    _closed_project_with_stages(ws_db, "A", "procurement", _FULL_CHAIN, 100.0)
    _closed_project_with_stages(ws_db, "B", "procurement", _FULL_CHAIN, 1000.0)
    seq.recompute_and_store()

    # Open project D: everything in _FULL_CHAIN except the legal-review
    # step ("contractpodai_review_requested") - a real, planted gap.
    proj_d = ws_db.create_project_with_new_id(name="D", category="procurement", status="active")
    issue_d = _issue(ws_db, "D", project_id=proj_d)
    _raw_item_at(ws_db, issue_d, "intake_new_project_assigned", "d-0", 2000.0)
    _raw_item_at(ws_db, issue_d, "signature_requested_docusign", "d-1", 2001.0)
    _raw_item_at(ws_db, issue_d, "signature_completed_docusign", "d-2", 2002.0)

    note = seq.deviation_note_for_project(proj_d, "procurement")

    assert note is not None
    assert "contractpodai_review_requested" in note
    assert "before" in note
    assert "signature_requested_docusign" in note
    assert "procurement" in note
    assert "2 of 2" in note
    assert "not a rule" in note  # explicitly non-enforcing wording


def test_deviation_note_for_project_none_when_project_follows_typical_sequence(ws_db):
    _closed_project_with_stages(ws_db, "A", "procurement", _FULL_CHAIN, 100.0)
    _closed_project_with_stages(ws_db, "B", "procurement", _FULL_CHAIN, 1000.0)
    seq.recompute_and_store()

    proj_c = ws_db.create_project_with_new_id(name="C", category="procurement", status="active")
    issue_c = _issue(ws_db, "C", project_id=proj_c)
    for i, st in enumerate(_FULL_CHAIN):
        _raw_item_at(ws_db, issue_c, st, f"c-{i}", 2000.0 + i)

    assert seq.deviation_note_for_project(proj_c, "procurement") is None


def test_deviation_note_for_project_none_without_category_or_pattern(ws_db):
    proj = ws_db.create_project_with_new_id(name="Solo", category="procurement", status="active")
    assert seq.deviation_note_for_project(proj, None) is None
    assert seq.deviation_note_for_project(proj, "procurement") is None  # nothing stored yet


def test_deviation_notes_for_projects_batches_by_distinct_project(ws_db):
    _closed_project_with_stages(ws_db, "A", "procurement", _FULL_CHAIN, 100.0)
    _closed_project_with_stages(ws_db, "B", "procurement", _FULL_CHAIN, 1000.0)
    seq.recompute_and_store()

    proj_d = ws_db.create_project_with_new_id(name="D", category="procurement", status="active")
    issue_d = _issue(ws_db, "D", project_id=proj_d)
    _raw_item_at(ws_db, issue_d, "intake_new_project_assigned", "d-0", 2000.0)
    _raw_item_at(ws_db, issue_d, "signature_requested_docusign", "d-1", 2001.0)
    _raw_item_at(ws_db, issue_d, "signature_completed_docusign", "d-2", 2002.0)

    proj_c = ws_db.create_project_with_new_id(name="C", category="procurement", status="active")
    issue_c = _issue(ws_db, "C", project_id=proj_c)
    for i, st in enumerate(_FULL_CHAIN):
        _raw_item_at(ws_db, issue_c, st, f"c-{i}", 3000.0 + i)

    notes = seq.deviation_notes_for_projects([proj_d, proj_c, proj_d, None, "no-such-project"])

    assert set(notes.keys()) == {proj_d}
    assert notes[proj_d] == seq.deviation_note_for_project(proj_d, "procurement")


def test_top_pattern_for_category_excludes_the_viewed_project_from_its_own_evidence(ws_db):
    proj_a = _closed_project_with_stages(ws_db, "A", "procurement", _FULL_CHAIN, 100.0)
    proj_b = _closed_project_with_stages(ws_db, "B", "procurement", _FULL_CHAIN, 1000.0)
    seq.recompute_and_store()

    # Viewing proj_a itself: excluding it drops support to 1 of 1 real
    # OTHER project - below MIN_MATCHING_PROJECTS, so no pattern is shown
    # rather than a project citing itself as its own precedent.
    result = seq.top_pattern_for_category("procurement", exclude_project_id=proj_a)
    assert result is None

    # A genuinely unrelated project being viewed doesn't lose any evidence.
    result_unaffected = seq.top_pattern_for_category("procurement", exclude_project_id="proj-does-not-exist")
    assert result_unaffected is not None
    assert set(result_unaffected["matching_project_ids"]) == {proj_a, proj_b}
