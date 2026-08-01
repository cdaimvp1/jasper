"""skills_registry.py — the swappable action_kind -> installed-skill mapping.
No domain name (e.g. "lilly-contract-review") is ever hardcoded in the
module itself; these tests deliberately use a fake action_kind/skill_dir to
prove the module is genuinely generic, not just happening to work for the
one real entry shipped in config/skills_registry.json."""
import json

import skills_registry


def test_returns_none_when_registry_file_does_not_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", tmp_path / "no_such_file.json")
    assert skills_registry.get_skill_for_action("anything") is None


def test_returns_none_for_unregistered_action_kind(tmp_path, monkeypatch):
    registry_path = tmp_path / "skills_registry.json"
    registry_path.write_text(json.dumps({"some_other_kind": {}}), encoding="utf-8")
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", registry_path)
    assert skills_registry.get_skill_for_action("contract_review") is None


def test_malformed_json_is_an_honest_miss_not_a_crash(tmp_path, monkeypatch):
    registry_path = tmp_path / "skills_registry.json"
    registry_path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", registry_path)
    assert skills_registry.get_skill_for_action("contract_review") is None


def test_registered_but_not_vendored_on_disk_is_none(tmp_path, monkeypatch):
    # Real, load-bearing case: an entry can exist in the JSON (someone wrote
    # a registration) without the skill actually having been vendored onto
    # this install yet - must be an honest miss, never a guess at a path
    # that doesn't exist.
    registry_path = tmp_path / "skills_registry.json"
    registry_path.write_text(json.dumps({
        "contract_review": {
            "skill_name": "fake-skill",
            "skill_dir": "documents/reference/skills/fake-skill",
            "display_name": "Fake Skill",
            "label": "Run Fake Skill",
            "produces": "a fake output",
            "output_kind": "output",
        },
    }), encoding="utf-8")
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(skills_registry.paths, "DATA_DIR", tmp_path / "data_that_has_no_skills_dir")
    assert skills_registry.get_skill_for_action("contract_review") is None


def test_resolves_a_real_registered_and_vendored_skill(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    skill_dir = data_dir / "documents" / "reference" / "skills" / "fake-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Fake Skill\n", encoding="utf-8")

    registry_path = tmp_path / "skills_registry.json"
    registry_path.write_text(json.dumps({
        "contract_review": {
            "skill_name": "fake-skill",
            "skill_dir": "documents/reference/skills/fake-skill",
            "display_name": "Fake Skill",
            "label": "Run Fake Skill",
            "produces": "a fake output",
            "output_kind": "output",
        },
    }), encoding="utf-8")
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(skills_registry.paths, "DATA_DIR", data_dir)

    result = skills_registry.get_skill_for_action("contract_review")
    assert result is not None
    assert result["skill_name"] == "fake-skill"
    assert result["display_name"] == "Fake Skill"
    assert result["skill_dir"] == skill_dir
    assert result["skill_dir"].exists()
