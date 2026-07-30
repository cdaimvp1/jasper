"""Regression tests for link_extraction.py (task #48) - vendor action-link
extraction from a raw_item's staged HTML body, gated to known LIVE signal
types only (workgraph_signals.py). No real vendor HTML sample was available
this session (no ingestion has run since task #43 added HTML staging), so
these use realistic, hand-built approximations of each vendor's transactional
template shape - see the module's own "HONEST CAVEAT" docstring."""
import json

import link_extraction as le


ADOBE_SIGN_HTML = """
<html><body>
<p>Please review and sign this document.</p>
<a href="https://na1.adobesign.com/public/esignWidget?wid=abc123">REVIEW AND SIGN</a>
<hr>
<a href="https://na1.adobesign.com/public/help">Help</a>
</body></html>
"""

DOCUSIGN_HTML = """
<html><body>
<a href="https://demo.docusign.net/Signing/StartInSession.aspx?envelopeId=xyz">Review Document</a>
<a href="https://www.docusign.com/privacy">Privacy Policy</a>
</body></html>
"""

ARIBA_HTML = """
<html><body>
<p>A requisition needs your approval.</p>
<a href="https://s1.ariba.com/Sourcing/Main/aw?awh=r&awssk=abcd">Click here to approve</a>
</body></html>
"""

NO_VENDOR_LINK_HTML = "<html><body><p>Just a plain message, no links at all.</p></body></html>"


def test_best_link_prefers_action_text_over_footer_link():
    url = le.best_link(ADOBE_SIGN_HTML, ("adobesign.com",))
    assert url == "https://na1.adobesign.com/public/esignWidget?wid=abc123"


def test_best_link_docusign_shape():
    url = le.best_link(DOCUSIGN_HTML, ("docusign.net",))
    assert url == "https://demo.docusign.net/Signing/StartInSession.aspx?envelopeId=xyz"


def test_best_link_ariba_shape():
    url = le.best_link(ARIBA_HTML, ("ariba.com",))
    assert url == "https://s1.ariba.com/Sourcing/Main/aw?awh=r&awssk=abcd"


def test_best_link_falls_back_to_first_vendor_link_without_action_text():
    html = '<a href="https://adobesign.com/foo">Some Link</a><a href="https://adobesign.com/bar">Another</a>'
    url = le.best_link(html, ("adobesign.com",))
    assert url == "https://adobesign.com/foo"


def test_best_link_none_when_no_vendor_domain_present():
    assert le.best_link(NO_VENDOR_LINK_HTML, ("adobesign.com",)) is None


def test_best_link_malformed_html_does_not_crash():
    le.best_link("<a href='unterminated", ("adobesign.com",))  # must not raise


def test_extract_link_for_raw_item_none_for_non_live_signal_type():
    raw_item = {"signal_type": "signature_signed_by_me", "raw_ref": json.dumps({"body_html": "x/body.html"})}
    assert le.extract_link_for_raw_item(raw_item) is None


def test_extract_link_for_raw_item_none_when_no_signal_type():
    raw_item = {"signal_type": None, "raw_ref": json.dumps({"body_html": "x/body.html"})}
    assert le.extract_link_for_raw_item(raw_item) is None


def test_extract_link_for_raw_item_none_when_raw_ref_missing():
    raw_item = {"signal_type": "signature_requested", "raw_ref": None}
    assert le.extract_link_for_raw_item(raw_item) is None


def test_extract_link_for_raw_item_none_when_raw_ref_malformed_json():
    raw_item = {"signal_type": "signature_requested", "raw_ref": "not json"}
    assert le.extract_link_for_raw_item(raw_item) is None


def test_extract_link_for_raw_item_none_when_html_file_missing(isolated_paths):
    raw_item = {"signal_type": "signature_requested",
                "raw_ref": json.dumps({"body_html": "999/body.html"})}
    assert le.extract_link_for_raw_item(raw_item) is None


def test_extract_link_for_raw_item_full_path(isolated_paths):
    dest_dir = isolated_paths.DOCUMENTS_DIR / "raw_items" / "42"
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "body.html").write_text(DOCUSIGN_HTML, encoding="utf-8")
    raw_item = {
        "signal_type": "signature_requested_docusign",
        "raw_ref": json.dumps({"body_text": "raw_items/42/body.txt", "body_html": "raw_items/42/body.html"}),
    }

    result = le.extract_link_for_raw_item(raw_item)

    assert result == {
        "link_type": "docusign",
        "url": "https://demo.docusign.net/Signing/StartInSession.aspx?envelopeId=xyz",
        "label": "Open in DocuSign",
    }
