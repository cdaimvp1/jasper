"""workgraph_recommend.py — the deterministic per-evidence recommendation
generator, including its 2026-07-31 skills_registry.py hook: an email with
an attachment should name a real registered skill when one exists, and fall
back to today's generic wording when nothing is registered (the honest
default for most action kinds on most installs)."""
import json

import skills_registry
import workgraph_recommend as wr


def test_email_with_attachment_generic_fallback_when_no_skill_registered(tmp_path, monkeypatch):
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", tmp_path / "no_such_registry.json")
    ev = {"type": "email", "summary": "please review this work order"}
    rec = wr.recommend_for_evidence(ev, has_attachment=True, now=0)
    assert rec["kind"] == "contract_review"
    assert rec["label"] == "Review the attached document"
    assert "MSA and standard positions" in rec["rationale"]


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
    rec = wr.recommend_for_evidence(ev, has_attachment=True, now=0)
    assert rec["kind"] == "contract_review"
    assert rec["label"] == "Run Fake Skill"
    assert "Fake Skill" in rec["rationale"]
    assert "a fake redlined output" in rec["rationale"]


def test_no_attachment_no_recommendation():
    ev = {"type": "email", "summary": "just a normal note"}
    assert wr.recommend_for_evidence(ev, has_attachment=False, now=0) is None


def test_registered_skill_for_a_different_action_kind_does_not_leak_in(tmp_path, monkeypatch):
    registry_path = tmp_path / "skills_registry.json"
    registry_path.write_text(json.dumps({"some_other_kind": {"label": "irrelevant"}}), encoding="utf-8")
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", registry_path)
    ev = {"type": "email", "summary": "please review this work order"}
    rec = wr.recommend_for_evidence(ev, has_attachment=True, now=0)
    assert rec["label"] == "Review the attached document"


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
    rec = wr.recommend_for_evidence(ev, has_attachment=True, now=0)
    assert rec["kind"] == "audit_invoice"
    assert rec["label"] == "Run Invoice Audit"


def test_invoice_audit_falls_through_to_generic_contract_review_when_unregistered(tmp_path, monkeypatch):
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", tmp_path / "no_such_registry.json")
    ev = {"type": "email", "summary": "Please audit this invoice, we think there's overbilling"}
    rec = wr.recommend_for_evidence(ev, has_attachment=True, now=0)
    assert rec["kind"] == "contract_review"
    assert rec["label"] == "Review the attached document"


def test_plain_attachment_without_invoice_language_still_gets_generic_contract_review(tmp_path, monkeypatch):
    _fake_registry(tmp_path, monkeypatch, "audit_invoice", label="Run Invoice Audit")
    ev = {"type": "email", "summary": "please sign the attached amendment"}
    rec = wr.recommend_for_evidence(ev, has_attachment=True, now=0)
    assert rec["kind"] == "contract_review"


def test_sow_language_names_the_registered_scope_review_skill(tmp_path, monkeypatch):
    _fake_registry(tmp_path, monkeypatch, "scope_review", label="Run Scope Review")
    ev = {"type": "email", "summary": "can you review this SOW before we send it back"}
    rec = wr.recommend_for_evidence(ev, has_attachment=True, now=0)
    assert rec["kind"] == "scope_review"
    assert rec["label"] == "Run Scope Review"


def test_meeting_prep_names_the_registered_skill_within_lookahead_window(tmp_path, monkeypatch):
    _fake_registry(tmp_path, monkeypatch, "meeting_prep", label="Run Meeting Prep")
    ev = {"type": "calendar", "ts": 5 * wr.DAY}
    rec = wr.recommend_for_evidence(ev, has_attachment=False, now=0)
    assert rec["kind"] == "meeting_prep"
    assert rec["label"] == "Run Meeting Prep"


def test_meeting_prep_falls_through_to_generic_pre_read_when_unregistered(tmp_path, monkeypatch):
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", tmp_path / "no_such_registry.json")
    ev = {"type": "calendar", "ts": 5 * wr.DAY}
    rec = wr.recommend_for_evidence(ev, has_attachment=False, now=0)
    assert rec["kind"] == "prep"
    assert rec["label"] == "Draft a pre-read"


def test_market_rate_benchmark_names_the_registered_skill(tmp_path, monkeypatch):
    _fake_registry(tmp_path, monkeypatch, "market_rate_benchmark", label="Run Rate Benchmark")
    ev = {"type": "email", "summary": "what's the market rate for this category"}
    rec = wr.recommend_for_evidence(ev, has_attachment=False, now=0)
    assert rec["kind"] == "market_rate_benchmark"
    assert rec["label"] == "Run Rate Benchmark"


def test_market_rate_benchmark_falls_through_to_generic_summarize_when_unregistered(tmp_path, monkeypatch):
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", tmp_path / "no_such_registry.json")
    # matches BOTH the new specific regex ("benchmark rates?") and the old
    # generic _APPROVAL_RE ("benchmark\w*") - a true fall-through case.
    ev = {"type": "email", "summary": "can you benchmark rates for this category"}
    rec = wr.recommend_for_evidence(ev, has_attachment=False, now=0)
    assert rec["kind"] == "summarize"
