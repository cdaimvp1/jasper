"""workgraph_recommend.py — the deterministic per-evidence recommendation
generator, including its 2026-07-31 skills_registry.py hook: an email with
an attachment should name a real registered skill when one exists, and fall
back to today's generic wording when nothing is registered (the honest
default for most action kinds on most installs).

2026-08-01 (task #15): recommend_for_evidence now returns a LIST (possibly
more than one item, possibly empty) instead of a single dict-or-None, so a
row that matches more than one specific skill surfaces all of them instead
of only the first. Existing single-recommendation cases below now assert
against recs[0] with len(recs) == 1; the "no recommendation" case now
asserts an empty list."""
import json

import skills_registry
import workgraph_recommend as wr


def test_email_with_attachment_generic_fallback_when_no_skill_registered(tmp_path, monkeypatch):
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", tmp_path / "no_such_registry.json")
    ev = {"type": "email", "summary": "please review this work order"}
    recs = wr.recommend_for_evidence(ev, has_attachment=True, now=0)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["kind"] == "contract_review"
    assert rec["label"] == "Review the attached document"
    assert "Lilly's playbook" in rec["rationale"]


def test_email_with_attachment_names_registered_skill(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    skill_dir = data_dir / "documents" / "reference" / "skills" / "fake-skill"
    skill_dir.mkdir(parents=True)
    registry_path = tmp_path / "skills_registry.json"
    registry_path.write_text(json.dumps({
        "contract_review": {
            "skill_name": "fake-skill",
            "skill_dir": "documents/reference/skills/fake-skill",
            "display_name": "Fake Skill",
            "label": "Run Fake Skill",
            "produces": "a fake redlined output",
            "output_kind": "output",
        },
    }), encoding="utf-8")
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(skills_registry.paths, "DATA_DIR", data_dir)

    ev = {"type": "email", "summary": "please review this work order"}
    recs = wr.recommend_for_evidence(ev, has_attachment=True, now=0)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["kind"] == "contract_review"
    assert rec["label"] == "Run Fake Skill"
    assert "Fake Skill" in rec["rationale"]
    assert "a fake redlined output" in rec["rationale"]


def test_no_attachment_no_recommendation():
    ev = {"type": "email", "summary": "just a normal note"}
    assert wr.recommend_for_evidence(ev, has_attachment=False, now=0) == []


def test_registered_skill_for_a_different_action_kind_does_not_leak_in(tmp_path, monkeypatch):
    registry_path = tmp_path / "skills_registry.json"
    registry_path.write_text(json.dumps({"some_other_kind": {"label": "irrelevant"}}), encoding="utf-8")
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", registry_path)
    ev = {"type": "email", "summary": "please review this work order"}
    recs = wr.recommend_for_evidence(ev, has_attachment=True, now=0)
    assert len(recs) == 1
    assert recs[0]["label"] == "Review the attached document"


# --- task #14: audit_invoice / scope_review / meeting_prep / market_rate_benchmark ---

def _fake_registry(tmp_path, monkeypatch, action_kind, label="Run Fake Skill", display_name="Fake Skill",
                    produces="a fake output"):
    data_dir = tmp_path / "data"
    skill_dir = data_dir / "documents" / "reference" / "skills" / "fake-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    registry_path = tmp_path / "skills_registry.json"
    registry_path.write_text(json.dumps({
        action_kind: {
            "skill_name": "fake-skill", "skill_dir": "documents/reference/skills/fake-skill",
            "display_name": display_name, "label": label, "produces": produces, "output_kind": "output",
        },
    }), encoding="utf-8")
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(skills_registry.paths, "DATA_DIR", data_dir)


def test_invoice_audit_language_names_the_registered_skill(tmp_path, monkeypatch):
    _fake_registry(tmp_path, monkeypatch, "audit_invoice", label="Run Invoice Audit")
    ev = {"type": "email", "summary": "Please audit this invoice, we think there's overbilling"}
    recs = wr.recommend_for_evidence(ev, has_attachment=True, now=0)
    assert len(recs) == 1
    assert recs[0]["kind"] == "audit_invoice"
    assert recs[0]["label"] == "Run Invoice Audit"


def test_invoice_audit_falls_through_to_generic_contract_review_when_unregistered(tmp_path, monkeypatch):
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", tmp_path / "no_such_registry.json")
    ev = {"type": "email", "summary": "Please audit this invoice, we think there's overbilling"}
    recs = wr.recommend_for_evidence(ev, has_attachment=True, now=0)
    assert len(recs) == 1
    assert recs[0]["kind"] == "contract_review"
    assert recs[0]["label"] == "Review the attached document"


def test_plain_attachment_without_invoice_language_still_gets_generic_contract_review(tmp_path, monkeypatch):
    _fake_registry(tmp_path, monkeypatch, "audit_invoice", label="Run Invoice Audit")
    ev = {"type": "email", "summary": "please sign the attached amendment"}
    recs = wr.recommend_for_evidence(ev, has_attachment=True, now=0)
    assert len(recs) == 1
    assert recs[0]["kind"] == "contract_review"


def test_invoice_audit_and_sow_language_both_surface_when_a_row_matches_both(tmp_path, monkeypatch):
    # task #15: an attachment can genuinely have both invoice-audit AND
    # SOW/scope language - both real skills should surface, not just one.
    data_dir = tmp_path / "data"
    for name in ("fake-invoice-skill", "fake-scope-skill"):
        (data_dir / "documents" / "reference" / "skills" / name).mkdir(parents=True, exist_ok=True)
    registry_path = tmp_path / "skills_registry.json"
    registry_path.write_text(json.dumps({
        "audit_invoice": {
            "skill_name": "fake-invoice-skill", "skill_dir": "documents/reference/skills/fake-invoice-skill",
            "display_name": "Invoice Audit", "label": "Run Invoice Audit", "produces": "an audit",
            "output_kind": "output",
        },
        "scope_review": {
            "skill_name": "fake-scope-skill", "skill_dir": "documents/reference/skills/fake-scope-skill",
            "display_name": "Scope Review", "label": "Run Scope Review", "produces": "a diagnostic",
            "output_kind": "output",
        },
    }), encoding="utf-8")
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(skills_registry.paths, "DATA_DIR", data_dir)

    ev = {"type": "email", "summary": "Please audit this invoice against the statement of work — we think there's overbilling"}
    recs = wr.recommend_for_evidence(ev, has_attachment=True, now=0)
    kinds = {r["kind"] for r in recs}
    assert kinds == {"audit_invoice", "scope_review"}


def test_sow_language_names_the_registered_scope_review_skill(tmp_path, monkeypatch):
    _fake_registry(tmp_path, monkeypatch, "scope_review", label="Run Scope Review")
    ev = {"type": "email", "summary": "can you review this SOW before we send it back"}
    recs = wr.recommend_for_evidence(ev, has_attachment=True, now=0)
    assert len(recs) == 1
    assert recs[0]["kind"] == "scope_review"
    assert recs[0]["label"] == "Run Scope Review"


def test_meeting_prep_names_the_registered_skill_within_lookahead_window(tmp_path, monkeypatch):
    _fake_registry(tmp_path, monkeypatch, "meeting_prep", label="Run Meeting Prep")
    ev = {"type": "calendar", "ts": 5 * wr.DAY}
    recs = wr.recommend_for_evidence(ev, has_attachment=False, now=0)
    assert len(recs) == 1
    assert recs[0]["kind"] == "meeting_prep"
    assert recs[0]["label"] == "Run Meeting Prep"


def test_meeting_prep_falls_through_to_generic_pre_read_when_unregistered(tmp_path, monkeypatch):
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", tmp_path / "no_such_registry.json")
    ev = {"type": "calendar", "ts": 5 * wr.DAY}
    recs = wr.recommend_for_evidence(ev, has_attachment=False, now=0)
    assert len(recs) == 1
    assert recs[0]["kind"] == "prep"
    assert recs[0]["label"] == "Draft a pre-read"


def test_market_rate_benchmark_names_the_registered_skill(tmp_path, monkeypatch):
    _fake_registry(tmp_path, monkeypatch, "market_rate_benchmark", label="Run Rate Benchmark")
    ev = {"type": "email", "summary": "what's the market rate for this category"}
    recs = wr.recommend_for_evidence(ev, has_attachment=False, now=0)
    assert len(recs) == 1
    assert recs[0]["kind"] == "market_rate_benchmark"
    assert recs[0]["label"] == "Run Rate Benchmark"


def test_market_rate_benchmark_falls_through_to_generic_summarize_when_unregistered(tmp_path, monkeypatch):
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", tmp_path / "no_such_registry.json")
    # matches BOTH the new specific regex ("benchmark rates?") and the old
    # generic _APPROVAL_RE ("benchmark\w*") - a true fall-through case.
    ev = {"type": "email", "summary": "can you benchmark rates for this category"}
    recs = wr.recommend_for_evidence(ev, has_attachment=False, now=0)
    assert len(recs) == 1
    assert recs[0]["kind"] == "summarize"


def test_market_rate_benchmark_registered_suppresses_generic_summarize(tmp_path, monkeypatch):
    # task #15: once the specific market_rate_benchmark skill is registered
    # and matches, the generic "summarize" cue for the same approval-language
    # match must NOT also appear — it would be pure noise duplicating the
    # specific recommendation.
    _fake_registry(tmp_path, monkeypatch, "market_rate_benchmark", label="Run Rate Benchmark")
    ev = {"type": "email", "summary": "can you benchmark rates for this category"}
    recs = wr.recommend_for_evidence(ev, has_attachment=False, now=0)
    assert len(recs) == 1
    assert recs[0]["kind"] == "market_rate_benchmark"


def test_approval_language_independent_of_benchmark_still_adds_summarize(tmp_path, monkeypatch):
    # An approval/sign-off match unrelated to rate-benchmarking is its own,
    # independent recommendation and must still surface.
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", tmp_path / "no_such_registry.json")
    ev = {"type": "email", "summary": "please sign off on the attached amendment"}
    recs = wr.recommend_for_evidence(ev, has_attachment=False, now=0)
    assert len(recs) == 1
    assert recs[0]["kind"] == "summarize"


def test_approval_language_suppressed_when_raw_item_has_known_signal_type(tmp_path, monkeypatch):
    # Real bug (2026-08-02, Marc's live screenshot): an SAP Ariba
    # requisition-approval auto-notification matched "approv\w*" and got
    # "Summarize the thread" as its suggested action - nonsensical for a
    # single-message automated notification Jasper already knows exactly
    # what to call by its real signal_type, not a free-text thread.
    #
    # Task #233 superseded the "no recommendation at all" half of this: a
    # recognized ACTIONABLE signal_type now gets its own real, type-specific
    # recommendation instead of silence - "Summarize the thread" is still
    # correctly suppressed (that's the part this test still guards), but the
    # replacement is a real action, not an empty list.
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", tmp_path / "no_such_registry.json")
    ev = {"type": "email", "summary": "Action required: Approve the Requisition",
          "signal_type": "ariba_pr_approval_needed"}
    recs = wr.recommend_for_evidence(ev, has_attachment=False, now=0)
    assert len(recs) == 1
    assert recs[0]["kind"] == "approve_requisition"
    assert "summarize" not in recs[0]["kind"]


def test_ariba_approval_signal_quotes_real_requisition_fields():
    ev = {"type": "email",
          "summary": ("Action required: Approve the Requisition that THOMAS TURNER submitted  - "
                       "PR1193376 - Workday HCM SaaS ($53,702,143.00 USD)"),
          "signal_type": "ariba_pr_approval_needed"}
    recs = wr.recommend_for_evidence(ev, has_attachment=False, now=0)
    assert len(recs) == 1
    rec = recs[0]
    assert rec["kind"] == "approve_requisition"
    assert rec["label"] == "Approve or reject in Ariba"
    assert "THOMAS TURNER" in rec["rationale"]
    assert "Workday HCM SaaS" in rec["rationale"]
    assert "53,702,143.00" in rec["rationale"]


def test_ariba_approval_signal_falls_back_to_generic_rationale_without_parseable_fields():
    ev = {"type": "email", "summary": "Action required: Approve the Requisition",
          "signal_type": "ariba_pr_approval_needed"}
    recs = wr.recommend_for_evidence(ev, has_attachment=False, now=0)
    assert len(recs) == 1
    assert recs[0]["rationale"] == "This requisition is waiting on your approval in Ariba."


def test_signature_requested_signal_gets_review_and_sign_action():
    for signal_type in ("signature_requested", "signature_requested_docusign"):
        ev = {"type": "email", "summary": "Signature requested on Amendment.pdf", "signal_type": signal_type}
        recs = wr.recommend_for_evidence(ev, has_attachment=False, now=0)
        assert len(recs) == 1
        assert recs[0]["kind"] == "review_signature"
        assert recs[0]["label"] == "Review and sign"


def test_concur_expense_reminder_signal_gets_apply_expense_action():
    ev = {"type": "email", "summary": "Action Required: Unapplied credit card transactions",
          "signal_type": "concur_expense_reminder"}
    recs = wr.recommend_for_evidence(ev, has_attachment=False, now=0)
    assert len(recs) == 1
    assert recs[0]["kind"] == "apply_expense"


def test_unmapped_signal_type_still_correctly_gets_no_action():
    # A recognized-but-non-actionable signal_type (e.g. a closure
    # notification - "the requisition has been fully approved") isn't in
    # SIGNAL_ACTION_BUILDERS at all (only genuinely ACTIONABLE signal types
    # are), and pre-existing behavior already correctly suppresses the
    # generic approval-language "summarize" guess for ANY known signal_type
    # (not just the mapped ones) - a closure/fyi automated notification
    # genuinely needs no action from Marc, so an empty list here is correct,
    # not a gap this task needed to fill.
    ev = {"type": "email", "summary": "please sign off on the attached amendment",
          "signal_type": "ariba_pr_fully_approved"}
    recs = wr.recommend_for_evidence(ev, has_attachment=False, now=0)
    assert recs == []


def test_approval_language_still_fires_without_signal_type(tmp_path, monkeypatch):
    # A real human-written email (no recognized automated signal_type) with
    # approval language must still get "summarize" - only known automated
    # notifications are suppressed, not every approval-adjacent message.
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", tmp_path / "no_such_registry.json")
    ev = {"type": "email", "summary": "please approve the attached amendment", "signal_type": None}
    recs = wr.recommend_for_evidence(ev, has_attachment=False, now=0)
    assert len(recs) == 1
    assert recs[0]["kind"] == "summarize"
