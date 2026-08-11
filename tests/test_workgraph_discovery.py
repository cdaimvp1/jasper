"""Tests for workgraph_discovery.py (personalized data-point discovery,
docs/design/PERSONALIZED_DATA_POINT_DISCOVERY.md, tasks #212-217).

Deliberately does NOT exercise propose_from_observation/llm_backfill_
missing_values against a real `claude -p` subprocess here - those were
verified manually against the live DB (see session notes), and a real
LLM call is too slow/costly/flaky for the automated suite. Everything
here is the deterministic half: signature derivation, the significance
bar, the fast-track seed, and the grouping retrofit's matching logic."""
import time

import workgraph_discovery as wd


def _item(id=1, from_actor="jane@vendor.com", thread_key="t1", subject="Subject",
          participants_json="[]", occurred_ts=None):
    return {
        "id": id, "from_actor": from_actor, "thread_key": thread_key, "subject": subject,
        "participants": participants_json, "body_preview": "", "raw_ref": None,
        "occurred_ts": occurred_ts if occurred_ts is not None else time.time(),
    }


# --- derive_pattern_signatures ----------------------------------------------

def test_sender_domain_signature_extracted():
    sigs = wd.derive_pattern_signatures(_item(from_actor="jane@newvendor.com"))
    assert "sender_domain:newvendor.com" in sigs


def test_internal_root_domain_excluded_but_subdomain_kept():
    sigs = wd.derive_pattern_signatures(_item(from_actor="colleague@lilly.com"))
    assert not any(s.startswith("sender_domain:") for s in sigs)
    sigs2 = wd.derive_pattern_signatures(_item(from_actor="it@network.lilly.com"))
    assert "sender_domain:network.lilly.com" in sigs2


def test_labeled_field_signature_extracted(monkeypatch):
    monkeypatch.setattr(wd.text_extract, "resolve_item_text", lambda item: "Batch Number: BN12345\nOther text.")
    sigs = wd.derive_pattern_signatures(_item())
    assert "labeled_field:batch number" in sigs


def test_boilerplate_labels_filtered_out(monkeypatch):
    monkeypatch.setattr(
        wd.text_extract, "resolve_item_text",
        lambda item: "Confidentiality Notice: this email...\nPhone: 555-1234\nSubject: hi",
    )
    sigs = wd.derive_pattern_signatures(_item())
    assert not any(s.startswith("labeled_field:") for s in sigs)


# --- significance bar --------------------------------------------------------

def test_crosses_significance_bar_requires_all_three_conditions():
    now = time.time()
    base = {"pattern_signature": "sender_domain:x.com", "first_seen_ts": now, "last_seen_ts": now,
            "promoted_to_definition_id": None}

    too_few_occurrences = {**base, "occurrence_count": 4, "distinct_thread_count": 2}
    assert wd.crosses_significance_bar(too_few_occurrences) is False

    too_few_threads = {**base, "occurrence_count": 10, "distinct_thread_count": 1}
    assert wd.crosses_significance_bar(too_few_threads) is False

    too_wide_a_window = {**base, "occurrence_count": 5, "distinct_thread_count": 2,
                          "last_seen_ts": now, "first_seen_ts": now - 61 * 86400}
    assert wd.crosses_significance_bar(too_wide_a_window) is False

    already_promoted = {**base, "occurrence_count": 10, "distinct_thread_count": 5,
                         "promoted_to_definition_id": "dp-already"}
    assert wd.crosses_significance_bar(already_promoted) is False

    real_crossing = {**base, "occurrence_count": 5, "distinct_thread_count": 2}
    assert wd.crosses_significance_bar(real_crossing) is True


def test_observation_thread_tracking_only_counts_genuinely_distinct_threads(ws_db):
    # Same thread repeated - occurrence_count grows, distinct_thread_count does not.
    ws_db.observe_candidate_pattern("sender_domain:x.com", is_new_thread=ws_db.record_pattern_observation_thread(
        "sender_domain:x.com", "thread-1"))
    for _ in range(4):
        is_new = ws_db.record_pattern_observation_thread("sender_domain:x.com", "thread-1")
        ws_db.observe_candidate_pattern("sender_domain:x.com", is_new_thread=is_new)
    row = ws_db.get_candidate_pattern_observation("sender_domain:x.com")
    assert row["occurrence_count"] == 5
    assert row["distinct_thread_count"] == 1
    assert wd.crosses_significance_bar(row) is False  # only 1 distinct thread

    # A genuinely new thread pushes it over the 2-thread bar.
    is_new = ws_db.record_pattern_observation_thread("sender_domain:x.com", "thread-2")
    ws_db.observe_candidate_pattern("sender_domain:x.com", is_new_thread=is_new)
    row2 = ws_db.get_candidate_pattern_observation("sender_domain:x.com")
    assert row2["distinct_thread_count"] == 2
    assert wd.crosses_significance_bar(row2) is True


# --- fast-track seed (#217) -------------------------------------------------

def test_seed_fasttrack_vocabulary_creates_seven_confirmed_points(ws_db):
    created = wd.seed_fasttrack_vocabulary(confirmed_by="marc")
    assert len(created) == 7
    confirmed = ws_db.list_data_point_definitions(status="confirmed")
    assert len(confirmed) == 7
    assert {d["point_type"] for d in confirmed} == {"entity", "person", "reference", "amount", "freetext", "date"}


def test_seed_fasttrack_vocabulary_is_idempotent(ws_db):
    wd.seed_fasttrack_vocabulary(confirmed_by="marc")
    second_call = wd.seed_fasttrack_vocabulary(confirmed_by="marc")
    assert second_call == []  # nothing new to create the second time
    assert len(ws_db.list_data_point_definitions(status="confirmed")) == 7


# --- matched_discovered_points retrofit (#215/#216) -------------------------

def test_matched_discovered_points_is_a_noop_with_only_fasttrack_definitions(ws_db):
    wd.seed_fasttrack_vocabulary(confirmed_by="marc")
    assert wd.matched_discovered_points("issue-a", "issue-b") == []


def test_matched_discovered_points_detects_a_real_shared_sender_domain(ws_db):
    ws_db.insert_raw_item(source="outlook_mail", stable_key="s1", thread_key="t1", dedupe_key="d1",
                           occurred_ts=time.time(), from_actor="rep@newvendor.com", subject="Hello")
    ws_db.insert_raw_item(source="outlook_mail", stable_key="s2", thread_key="t2", dedupe_key="d2",
                           occurred_ts=time.time(), from_actor="rep2@newvendor.com", subject="Follow-up")
    ws_db.link_raw_item_to_issue(1, "issue-a")
    ws_db.link_raw_item_to_issue(2, "issue-b")

    ws_db.observe_candidate_pattern("sender_domain:newvendor.com", is_new_thread=True)
    ws_db.create_data_point_definition(
        id="dp-newvendor", name="NewVendor", description="test", point_type="entity",
        deterministic_rule=None, discovered_from="test", status="confirmed",
    )
    ws_db.confirm_data_point_definition("dp-newvendor", confirmed_by="test")
    ws_db.mark_candidate_pattern_promoted("sender_domain:newvendor.com", "dp-newvendor")

    points = wd.matched_discovered_points("issue-a", "issue-b")
    assert points == ["discovered:dp-newvendor"]

    values = ws_db.list_data_point_values_for_work_object("issue-a")
    assert any(v["definition_id"] == "dp-newvendor" and v["value"] == "newvendor.com" for v in values)


def test_matched_discovered_points_no_match_when_domains_differ(ws_db):
    ws_db.insert_raw_item(source="outlook_mail", stable_key="s1", thread_key="t1", dedupe_key="d1",
                           occurred_ts=time.time(), from_actor="rep@vendor-a.com", subject="Hello")
    ws_db.insert_raw_item(source="outlook_mail", stable_key="s2", thread_key="t2", dedupe_key="d2",
                           occurred_ts=time.time(), from_actor="rep@vendor-b.com", subject="Follow-up")
    ws_db.link_raw_item_to_issue(1, "issue-a")
    ws_db.link_raw_item_to_issue(2, "issue-b")

    ws_db.observe_candidate_pattern("sender_domain:vendor-a.com", is_new_thread=True)
    ws_db.create_data_point_definition(
        id="dp-vendor-a", name="Vendor A", description="test", point_type="entity",
        deterministic_rule=None, discovered_from="test", status="confirmed",
    )
    ws_db.confirm_data_point_definition("dp-vendor-a", confirmed_by="test")
    ws_db.mark_candidate_pattern_promoted("sender_domain:vendor-a.com", "dp-vendor-a")

    assert wd.matched_discovered_points("issue-a", "issue-b") == []


# --- value_for_signature -----------------------------------------------------

def test_value_for_signature_sender_domain():
    item = _item(from_actor="rep@acme.com")
    assert wd.value_for_signature("sender_domain:acme.com", item) == "acme.com"
    assert wd.value_for_signature("sender_domain:other.com", item) is None


def test_value_for_signature_labeled_field(monkeypatch):
    monkeypatch.setattr(wd.text_extract, "resolve_item_text", lambda item: "Batch Number: BN99887\n")
    item = _item()
    assert wd.value_for_signature("labeled_field:batch number", item) == "BN99887"
    assert wd.value_for_signature("labeled_field:cost center", item) is None


# --- _parse_proposal adversarial cases (found live 2026-08-06) -------------

def test_parse_proposal_survives_a_literal_pipe_inside_the_description():
    """A naive split("|") on the whole line breaks the moment the model's
    own example text contains a pipe (e.g. quoting a value like
    "BN-1234 | Lot 9") - real output seen live. maxsplit=3 fixes this by
    only treating the first 3 pipes as field separators."""
    stdout = ('PROPOSAL: Batch ID | entity | A batch value, e.g. "BN-1234 | Lot 9" | '
              'Extract after Batch ID:')
    parsed = wd._parse_proposal(stdout)
    assert parsed is not None
    assert parsed["name"] == "Batch ID"
    assert parsed["point_type"] == "entity"


def test_parse_proposal_skips_a_leading_none_line_instead_of_giving_up():
    """A stray 'PROPOSAL: NONE' line before a real one used to make this
    return None immediately (the for-loop returned on the FIRST PROPOSAL
    line seen) - fixed to keep scanning past a NONE line."""
    stdout = "PROPOSAL: NONE\nPROPOSAL: Second | amount | x | NONE"
    parsed = wd._parse_proposal(stdout)
    assert parsed is not None
    assert parsed["name"] == "Second"


def test_parse_proposal_returns_none_for_genuinely_all_none_output():
    assert wd._parse_proposal("PROPOSAL: NONE") is None
    assert wd._parse_proposal("") is None
    assert wd._parse_proposal("some preamble with no PROPOSAL line at all") is None


# --- run_monthly_sweep_if_due (task #249) -----------------------------------

def test_run_monthly_sweep_if_due_runs_once_then_gates_within_the_same_month(ws_db):
    now = time.time()
    r1 = wd.run_monthly_sweep_if_due(now=now)
    assert r1 is not None
    assert "proposals_drafted" in r1

    r2 = wd.run_monthly_sweep_if_due(now=now + 86400)  # next day, same month - gated
    assert r2 is None


def test_run_monthly_sweep_if_due_runs_again_the_following_month(ws_db):
    now = time.time()
    wd.run_monthly_sweep_if_due(now=now)
    r2 = wd.run_monthly_sweep_if_due(now=now + 32 * 86400)  # a month later
    assert r2 is not None


# --- generalized system-table detection (task #266) -------------------------

def _make_domain_significant(ws_db, domain, thread_count=2, occurrences=6):
    """Same real record_pattern_observation_thread + observe_candidate_
    pattern sequence test_observation_thread_tracking_... above already
    uses to cross the bar - reused here as a helper since several tests
    below need a pre-crossed sender_domain observation."""
    signature = f"sender_domain:{domain}"
    for i in range(occurrences):
        thread_key = f"thread-{i % thread_count}"
        is_new = ws_db.record_pattern_observation_thread(signature, thread_key)
        ws_db.observe_candidate_pattern(signature, is_new_thread=is_new)
    return ws_db.get_candidate_pattern_observation(signature)


def _system_item(id, domain, body, thread_key=None):
    return {
        "id": id, "from_actor": f"notify@{domain}", "thread_key": thread_key or f"t{id}",
        "subject": "Notification", "participants": "[]", "body_preview": body,
        "occurred_ts": time.time(),
    }


_THREE_FIELD_BODY = "Request ID: REQ-{n}\nSourcing Lead: Jane Doe\nFunctional Area: Procurement\n"


def test_labels_cooccurring_with_domain_collects_distinct_labels_and_samples():
    pool = [_system_item(i, "cpai.example.com", _THREE_FIELD_BODY.format(n=i)) for i in range(3)]
    labels = wd._labels_cooccurring_with_domain("cpai.example.com", raw_items_pool=pool)
    assert set(labels.keys()) == {"request id", "sourcing lead", "functional area"}
    assert "REQ-0" in labels["request id"]


def test_check_and_propose_system_table_none_when_bar_not_crossed(ws_db):
    pool = [_system_item(0, "cpai.example.com", _THREE_FIELD_BODY.format(n=0))]
    assert wd.check_and_propose_system_table("sender_domain:cpai.example.com", raw_items_pool=pool) is None


def test_check_and_propose_system_table_none_with_too_few_cooccurring_labels(ws_db):
    _make_domain_significant(ws_db, "cpai.example.com")
    pool = [_system_item(i, "cpai.example.com", "Sourcing Lead: Jane Doe\n") for i in range(6)]
    assert wd.check_and_propose_system_table("sender_domain:cpai.example.com", raw_items_pool=pool) is None


def test_check_and_propose_system_table_creates_a_proposal(ws_db, monkeypatch):
    _make_domain_significant(ws_db, "cpai.example.com")
    pool = [_system_item(i, "cpai.example.com", _THREE_FIELD_BODY.format(n=i)) for i in range(6)]
    reply = (
        "SYSTEM: ContractPodAI\n"
        "FIELD: request id | reference | the system's own internal request identifier\n"
        "FIELD: sourcing lead | person | the assigned sourcing lead\n"
        "FIELD: functional area | freetext | which business function this request is for\n"
    )
    monkeypatch.setattr(wd, "_run_headless_claude",
                         lambda prompt, timeout, model=None: type("P", (), {"stdout": reply})())

    proposal = wd.check_and_propose_system_table("sender_domain:cpai.example.com", raw_items_pool=pool)

    assert proposal is not None
    assert proposal["system_name"] == "ContractPodAI"
    assert proposal["sender_domain"] == "cpai.example.com"
    assert proposal["status"] == "proposed"
    labels = {c["label"] for c in proposal["suggested_columns"]}
    assert labels == {"request id", "sourcing lead", "functional area"}


def test_check_and_propose_system_table_none_when_llm_says_not_one_system(ws_db, monkeypatch):
    _make_domain_significant(ws_db, "cpai.example.com")
    pool = [_system_item(i, "cpai.example.com", _THREE_FIELD_BODY.format(n=i)) for i in range(6)]
    monkeypatch.setattr(wd, "_run_headless_claude",
                         lambda prompt, timeout, model=None: type("P", (), {"stdout": "SYSTEM: NONE\n"})())

    assert wd.check_and_propose_system_table("sender_domain:cpai.example.com", raw_items_pool=pool) is None


def test_check_and_propose_system_table_never_proposes_twice_for_the_same_domain(ws_db, monkeypatch):
    _make_domain_significant(ws_db, "cpai.example.com")
    pool = [_system_item(i, "cpai.example.com", _THREE_FIELD_BODY.format(n=i)) for i in range(6)]
    reply = "SYSTEM: ContractPodAI\nFIELD: request id | reference | id\n" \
            "FIELD: sourcing lead | person | lead\nFIELD: functional area | freetext | area\n"
    monkeypatch.setattr(wd, "_run_headless_claude",
                         lambda prompt, timeout, model=None: type("P", (), {"stdout": reply})())

    first = wd.check_and_propose_system_table("sender_domain:cpai.example.com", raw_items_pool=pool)
    second = wd.check_and_propose_system_table("sender_domain:cpai.example.com", raw_items_pool=pool)

    assert first is not None
    assert second is None
    assert len(ws_db.list_system_table_proposals()) == 1


# --- sensitive-content exclusion (task #330) --------------------------------

def test_record_observations_for_item_skips_sensitive_item(ws_db):
    """A raw_item flagged sensitive=1 contributes zero observations - the
    choke point both the live per-item hook (workgraph_classify.py) and
    the setup bulk pass go through."""
    item = _item(id=1, from_actor="jane@newvendor.com")
    item["sensitive"] = 1

    rows = wd.record_observations_for_item(item)

    assert rows == []
    assert ws_db.get_candidate_pattern_observation("sender_domain:newvendor.com") is None


def test_record_observations_for_item_still_records_when_not_sensitive(ws_db):
    """Sanity check the guard is specific to sensitive=1, not a regression
    that broke the ordinary path."""
    item = _item(id=1, from_actor="jane@newvendor.com")
    item["sensitive"] = 0

    rows = wd.record_observations_for_item(item)

    assert len(rows) == 1
    assert ws_db.get_candidate_pattern_observation("sender_domain:newvendor.com") is not None


def test_sample_raw_items_for_signature_excludes_sensitive_items():
    normal = _item(id=1, from_actor="jane@newvendor.com")
    normal["sensitive"] = 0
    sensitive = _item(id=2, from_actor="jane@newvendor.com")
    sensitive["sensitive"] = 1

    samples = wd._sample_raw_items_for_signature("sender_domain:newvendor.com", [normal, sensitive])

    ids = [s["id"] for s in samples]
    assert 1 in ids
    assert 2 not in ids


def test_labels_cooccurring_with_domain_excludes_sensitive_items(monkeypatch):
    monkeypatch.setattr(wd.text_extract, "resolve_item_text", lambda item: "Batch Number: BN-SENSITIVE")
    sensitive = _item(id=1, from_actor="jane@vendor.example.com")
    sensitive["sensitive"] = 1

    labels = wd._labels_cooccurring_with_domain("vendor.example.com", raw_items_pool=[sensitive])

    assert labels == {}
