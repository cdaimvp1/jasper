"""Regression tests for rule_extraction.py (task #53) - no live Ollama
involved (its model registry is unreachable from this network, a corporate
TLS-inspection issue confirmed this session - not fixable here), so
urllib.request.urlopen is monkeypatched throughout. Every test here mocks
the HTTP layer directly rather than the module's own _call_ollama helper, so
these tests also exercise the real request-building/response-parsing code,
not just extract_rule_candidate's validation logic."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

import rule_extraction as re_


class _FakeResponse:
    def __init__(self, status=200, body=b""):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _ollama_json(**kwargs):
    return json.dumps({"response": json.dumps(kwargs)}).encode("utf-8")


def test_extract_rule_candidate_empty_explanation_returns_none():
    assert re_.extract_rule_candidate("") is None
    assert re_.extract_rule_candidate("   ") is None
    assert re_.extract_rule_candidate(None) is None


def test_extract_rule_candidate_connection_refused_returns_none(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert re_.extract_rule_candidate("a needs b first") is None


def test_extract_rule_candidate_timeout_returns_none(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert re_.extract_rule_candidate("a needs b first") is None


def test_extract_rule_candidate_non_200_returns_none(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(status=500))
    assert re_.extract_rule_candidate("a needs b first") is None


def test_extract_rule_candidate_malformed_outer_json_returns_none(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda req, timeout=None: _FakeResponse(body=b"not json"))
    assert re_.extract_rule_candidate("a needs b first") is None


def test_extract_rule_candidate_malformed_inner_json_returns_none(monkeypatch):
    body = json.dumps({"response": "not valid json for the inner payload"}).encode("utf-8")
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(body=body))
    assert re_.extract_rule_candidate("a needs b first") is None


def test_extract_rule_candidate_valid_high_confidence(monkeypatch):
    body = _ollama_json(
        trigger_signal_type="signature_requested_docusign",
        requires_signal_type="ariba_pr_fully_approved",
        match_on="project", confidence="high",
        reflection="A DocuSign request needs an approved Ariba PO first, matched by project.",
    )
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(body=body))

    result = re_.extract_rule_candidate("a signature request needs an approved Ariba PO first")

    assert result == {
        "trigger_signal_type": "signature_requested_docusign",
        "requires_signal_type": "ariba_pr_fully_approved",
        "match_on": "project",
        "reflection": "A DocuSign request needs an approved Ariba PO first, matched by project.",
    }


def test_extract_rule_candidate_low_confidence_returns_none(monkeypatch):
    body = _ollama_json(
        trigger_signal_type="signature_requested_docusign",
        requires_signal_type="ariba_pr_fully_approved",
        match_on="project", confidence="low", reflection="not sure",
    )
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(body=body))
    assert re_.extract_rule_candidate("something vague") is None


def test_extract_rule_candidate_unknown_signal_type_returns_none(monkeypatch):
    body = _ollama_json(
        trigger_signal_type="made_up_signal_type",
        requires_signal_type="ariba_pr_fully_approved",
        match_on="project", confidence="high", reflection="x",
    )
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(body=body))
    assert re_.extract_rule_candidate("something") is None


def test_extract_rule_candidate_trigger_equals_requires_returns_none(monkeypatch):
    body = _ollama_json(
        trigger_signal_type="signature_requested_docusign",
        requires_signal_type="signature_requested_docusign",
        match_on="project", confidence="high", reflection="x",
    )
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(body=body))
    assert re_.extract_rule_candidate("something") is None


def test_extract_rule_candidate_invalid_match_on_returns_none(monkeypatch):
    body = _ollama_json(
        trigger_signal_type="signature_requested_docusign",
        requires_signal_type="ariba_pr_fully_approved",
        match_on="department", confidence="high", reflection="x",
    )
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(body=body))
    assert re_.extract_rule_candidate("something") is None


def test_extract_rule_candidate_falls_back_to_generated_reflection(monkeypatch):
    body = _ollama_json(
        trigger_signal_type="signature_requested_docusign",
        requires_signal_type="ariba_pr_fully_approved",
        match_on="project", confidence="high", reflection="",
    )
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(body=body))

    result = re_.extract_rule_candidate("something")
    assert "signature_requested_docusign" in result["reflection"]
    assert "ariba_pr_fully_approved" in result["reflection"]


def test_is_ollama_reachable_true(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=None: _FakeResponse(status=200))
    assert re_.is_ollama_reachable() is True


def test_is_ollama_reachable_false_on_connection_error(monkeypatch):
    def fake_urlopen(url, timeout=None):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert re_.is_ollama_reachable() is False
