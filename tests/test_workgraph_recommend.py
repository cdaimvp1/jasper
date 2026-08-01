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
