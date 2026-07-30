"""Regression test for projects.py's path traversal fix (task #17) -
project_id used to flow unsanitized into a glob pattern; a crafted id like
"../secret/proj_zzz" matched a file outside PROJ_DIR in a live repro."""
import pytest


@pytest.fixture
def isolated_projects(tmp_path, monkeypatch):
    import projects
    proj_dir = tmp_path / "projects"
    monkeypatch.setattr(projects, "PROJ_DIR", proj_dir)
    return projects


def test_path_traversal_project_id_rejected(isolated_projects):
    assert isolated_projects._project_file("../secret/proj_zzzzzzzzzz") is None


def test_malformed_project_id_rejected(isolated_projects):
    assert isolated_projects._project_file("not_a_real_id") is None
    assert isolated_projects._project_file("") is None
    assert isolated_projects._project_file(None) is None


def test_valid_project_id_still_resolves(isolated_projects):
    isolated_projects.PROJ_DIR.mkdir(parents=True, exist_ok=True)
    real_id = "proj_0123456789"
    (isolated_projects.PROJ_DIR / f"{real_id}_test_project.md").write_text("# Test", encoding="utf-8")
    resolved = isolated_projects._project_file(real_id)
    assert resolved is not None
    assert resolved.name == f"{real_id}_test_project.md"
