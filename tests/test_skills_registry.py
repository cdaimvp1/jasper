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


def test_get_skill_for_action_passes_through_arbitrary_extra_fields(tmp_path, monkeypatch):
    """get_skill_for_action's dict(entry) copy must carry any extra field
    an entry has, not just the fixed set this module itself writes - task
    #50's panel_protocol (an optional pass-execution hint) is the real
    case this covers, added to config/skills_registry.json by hand, not
    through install_skill."""
    data_dir = tmp_path / "data"
    skill_dir = data_dir / "documents" / "reference" / "skills" / "fake-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Fake Skill\n", encoding="utf-8")

    registry_path = tmp_path / "skills_registry.json"
    registry_path.write_text(json.dumps({
        "contract_review": {
            "skill_name": "fake-skill", "skill_dir": "documents/reference/skills/fake-skill",
            "display_name": "Fake Skill", "label": "Run Fake Skill",
            "produces": "a fake output", "output_kind": "output",
            "panel_protocol": "ingest/CONTRACT_REVIEW_PANEL_ROUTINE.md",
        },
    }), encoding="utf-8")
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(skills_registry.paths, "DATA_DIR", data_dir)

    result = skills_registry.get_skill_for_action("contract_review")
    assert result["panel_protocol"] == "ingest/CONTRACT_REVIEW_PANEL_ROUTINE.md"


# --- list_all() (task #112, 2026-08-04) -------------------------------------
# Marc's explicit ask: the system should be able to run ANY registered skill
# on request, not just the couple with a dedicated button - the UI's "Run a
# skill" picker (GET /api/skills) is powered by this function.

def test_list_all_returns_empty_dict_when_registry_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", tmp_path / "no_such_file.json")
    assert skills_registry.list_all() == {}


def test_list_all_skips_entries_not_actually_vendored_on_disk(tmp_path, monkeypatch):
    # Same honest-miss rule as get_skill_for_action, applied across the
    # whole registry - a JSON entry with no real files backing it must
    # never be offered to Marc as something runnable.
    registry_path = tmp_path / "skills_registry.json"
    registry_path.write_text(json.dumps({
        "contract_review": {
            "skill_name": "fake-skill", "skill_dir": "documents/reference/skills/fake-skill",
            "display_name": "Fake Skill", "label": "Run Fake Skill",
            "produces": "a fake output", "output_kind": "output",
        },
    }), encoding="utf-8")
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(skills_registry.paths, "DATA_DIR", tmp_path / "data_that_has_no_skills_dir")
    assert skills_registry.list_all() == {}


def test_list_all_returns_every_real_vendored_skill(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    for name in ("fake-skill-a", "fake-skill-b"):
        skill_dir = data_dir / "documents" / "reference" / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")

    registry_path = tmp_path / "skills_registry.json"
    registry_path.write_text(json.dumps({
        "should_cost": {
            "skill_name": "fake-skill-a", "skill_dir": "documents/reference/skills/fake-skill-a",
            "display_name": "Fake A", "label": "Run A", "produces": "a", "output_kind": "output",
        },
        "supplier_landscape": {
            "skill_name": "fake-skill-b", "skill_dir": "documents/reference/skills/fake-skill-b",
            "display_name": "Fake B", "label": "Run B", "produces": "b", "output_kind": "output",
        },
    }), encoding="utf-8")
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(skills_registry.paths, "DATA_DIR", data_dir)

    result = skills_registry.list_all()
    assert set(result.keys()) == {"should_cost", "supplier_landscape"}
    assert result["should_cost"]["display_name"] == "Fake A"
    assert result["supplier_landscape"]["skill_dir"] == data_dir / "documents" / "reference" / "skills" / "fake-skill-b"


def _fake_source_skill(tmp_path, name="fake-skill", content="v1"):
    src = tmp_path / "sources" / f"{name}-{content}"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text(f"# {name} ({content})\n", encoding="utf-8")
    return src


def _install_kwargs(**overrides):
    kwargs = dict(skill_name="fake-skill", display_name="Fake Skill", label="Run Fake Skill",
                   produces="a fake output", output_kind="output", version="v1")
    kwargs.update(overrides)
    return kwargs


def test_install_skill_vendors_files_and_registers_action_kind(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    registry_path = tmp_path / "skills_registry.json"
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(skills_registry.paths, "DATA_DIR", data_dir)

    source = _fake_source_skill(tmp_path)
    entry = skills_registry.install_skill("audit_invoice", source, **_install_kwargs())

    assert entry["version"] == "v1"
    assert entry["previous_versions"] == []
    vendored = data_dir / entry["skill_dir"]
    assert vendored.is_dir()
    assert (vendored / "SKILL.md").read_text(encoding="utf-8") == "# fake-skill (v1)\n"

    result = skills_registry.get_skill_for_action("audit_invoice")
    assert result is not None
    assert result["skill_dir"] == vendored


def test_install_skill_new_version_keeps_old_version_on_disk_as_fallback(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    registry_path = tmp_path / "skills_registry.json"
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(skills_registry.paths, "DATA_DIR", data_dir)

    v1_source = _fake_source_skill(tmp_path, content="v1")
    v1 = skills_registry.install_skill("audit_invoice", v1_source, **_install_kwargs(version="v1"))
    v1_dir = data_dir / v1["skill_dir"]

    v2_source = _fake_source_skill(tmp_path, content="v2")
    v2 = skills_registry.install_skill("audit_invoice", v2_source, **_install_kwargs(version="v2"))

    # the OLD version's files are untouched on disk - a real fallback copy,
    # not just a pointer that happens to still resolve.
    assert v1_dir.is_dir()
    assert (v1_dir / "SKILL.md").read_text(encoding="utf-8") == "# fake-skill (v1)\n"
    assert v2["previous_versions"] == [{"version": "v1", "skill_dir": v1["skill_dir"], "installed_at": v1["installed_at"]}]
    assert skills_registry.get_skill_for_action("audit_invoice")["version"] == "v2"


def test_install_skill_same_version_reinstall_does_not_fabricate_a_fallback(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    registry_path = tmp_path / "skills_registry.json"
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(skills_registry.paths, "DATA_DIR", data_dir)

    source = _fake_source_skill(tmp_path)
    skills_registry.install_skill("audit_invoice", source, **_install_kwargs())
    entry = skills_registry.install_skill("audit_invoice", source, **_install_kwargs())

    assert entry["previous_versions"] == []


def test_install_skill_prunes_fallback_files_beyond_the_cap(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    registry_path = tmp_path / "skills_registry.json"
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(skills_registry.paths, "DATA_DIR", data_dir)

    dirs_by_version = {}
    for v in ["v1", "v2", "v3", "v4", "v5"]:
        source = _fake_source_skill(tmp_path, content=v)
        entry = skills_registry.install_skill("audit_invoice", source, **_install_kwargs(version=v))
        dirs_by_version[v] = data_dir / entry["skill_dir"]

    # cap is 3 previous versions - v5 is current, so v2/v3/v4 survive and v1
    # (the oldest, pushed past the cap) gets pruned from disk entirely.
    assert not dirs_by_version["v1"].exists()
    for v in ["v2", "v3", "v4"]:
        assert dirs_by_version[v].exists()
    assert dirs_by_version["v5"].exists()
    versions_kept = [p["version"] for p in skills_registry.get_skill_for_action("audit_invoice")["previous_versions"]]
    assert versions_kept == ["v4", "v3", "v2"]


def test_rollback_skill_restores_the_most_recent_previous_version(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    registry_path = tmp_path / "skills_registry.json"
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(skills_registry.paths, "DATA_DIR", data_dir)

    v1_source = _fake_source_skill(tmp_path, content="v1")
    skills_registry.install_skill("audit_invoice", v1_source, **_install_kwargs(version="v1"))
    v2_source = _fake_source_skill(tmp_path, content="v2")
    skills_registry.install_skill("audit_invoice", v2_source, **_install_kwargs(version="v2"))

    restored = skills_registry.rollback_skill("audit_invoice")
    assert restored["version"] == "v1"
    assert skills_registry.get_skill_for_action("audit_invoice")["version"] == "v1"
    # the rollback itself is reversible - v2 is now the fallback.
    assert restored["previous_versions"][0]["version"] == "v2"


def test_rollback_skill_returns_none_when_there_is_nothing_to_fall_back_to(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    registry_path = tmp_path / "skills_registry.json"
    monkeypatch.setattr(skills_registry, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(skills_registry.paths, "DATA_DIR", data_dir)

    source = _fake_source_skill(tmp_path)
    skills_registry.install_skill("audit_invoice", source, **_install_kwargs())

    assert skills_registry.rollback_skill("audit_invoice") is None
    assert skills_registry.rollback_skill("never_registered") is None
