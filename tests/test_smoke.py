"""Smoke test - confirms the fixture skeleton itself works before any real
regression tests get built on top of it."""


def test_ws_db_fixture_gives_isolated_db(ws_db):
    iid = ws_db.create_issue_with_new_id(title="smoke test issue", state="active", category="other")
    assert ws_db.get_issue(iid) is not None


def test_bus_db_fixture_gives_isolated_db(bus_db):
    bus_db.emit_event(source="test", kind="smoke", actor="pytest", target=None, payload={})
    assert bus_db.event_count() == 1


def test_fixtures_are_isolated_from_each_other(ws_db):
    """A DB created in one test must not leak into another."""
    issues = ws_db.list_issues(states=["active"], limit=100)
    assert len(issues) == 0  # the issue from test_ws_db_fixture_gives_isolated_db should NOT be here
