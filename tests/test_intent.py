"""Regression tests for intent.py:
- re.escape() on roster short-codes (task #27)
- dynamic HANDOFF_PATTERNS built from the live roster (task #30 enhancement)
"""
import re

import intent


class _FakeMembers:
    def __init__(self, mapping):
        self._mapping = mapping

    def short_to_slug(self):
        return self._mapping


def test_regex_metachar_in_short_code_does_not_crash(monkeypatch):
    """Before the fix: an unescaped short code containing a regex metachar
    (e.g. a malformed roster entry) would throw re.error - '(' starts an
    unterminated group. Must not raise, and the exact literal text should
    still resolve correctly (escaping doesn't break real matches, only
    metacharacter misinterpretation)."""
    monkeypatch.setattr(intent, "members_mod", _FakeMembers({"a.b(": "weird_worker"}))
    result = intent._find_target_member("hey a.b( can you take this")  # must not raise
    assert result == "weird_worker"  # the literal short code is genuinely present, so it should match


def test_regex_metachar_does_not_match_unrelated_text(monkeypatch):
    """The actual bug: unescaped, '.' in 'a.b(' would match ANY character, so
    "aXb(" (X = any char) would wrongly match too. With re.escape, only the
    exact literal "a.b(" matches."""
    monkeypatch.setattr(intent, "members_mod", _FakeMembers({"a.b(": "weird_worker"}))
    result = intent._find_target_member("hey aXb( can you take this")
    assert result is None


def test_normal_short_code_still_matches(monkeypatch):
    monkeypatch.setattr(intent, "members_mod", _FakeMembers({"ab": "aria_builder"}))
    assert intent._find_target_member("hey @ab can you take this") == "aria_builder"
    assert intent._find_target_member("ab: please review") == "aria_builder"


def test_handoff_patterns_use_live_roster_not_hardcoded_codes(monkeypatch):
    """Fixed 2026-07-29: HANDOFF_PATTERNS used to hardcode (ab|cb|cs|tb|oc)
    from an older roster naming scheme - the CURRENT real roster
    (tia/relay/curator/bridge) has no matching short code, so handoff
    detection was silently dead against today's actual cohort."""
    monkeypatch.setattr(intent, "members_mod", _FakeMembers({"ab": "aria_builder"}))
    patterns = intent._handoff_patterns()
    assert len(patterns) == 2
    assert patterns[0].search("ab: please take this")


def test_handoff_patterns_empty_roster_is_safe(monkeypatch):
    monkeypatch.setattr(intent, "members_mod", _FakeMembers({}))
    assert intent._handoff_patterns() == []
    # must not raise even with no patterns to check
    assert intent.classify_message("ab: please take this", actor="someone") == []
