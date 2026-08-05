"""Regression tests for workgraph_signals.py domain_matches() (task #23) -
real domain-boundary matching, not substring containment (spoofing risk)."""
import workgraph_signals as sig


def test_exact_domain_matches():
    assert sig.domain_matches("noreply@ariba.com", "ariba.com") is True


def test_subdomain_matches():
    assert sig.domain_matches("noreply@ansmtp.ariba.com", "ariba.com") is True


def test_lookalike_domain_does_not_match():
    """The exact spoofing risk this fix closed: a substring-containment check
    would let "ariba.com.evil-phisher.net" match "ariba.com" since the string
    "ariba.com" IS a substring of it."""
    assert sig.domain_matches("noreply@ariba.com.evil-phisher.net", "ariba.com") is False


def test_request_to_closure_signal_entries_are_real_known_signal_types():
    """Every key and value must be a real signal_type this module can
    actually produce (typo-proofing a dict that's easy to get subtly wrong -
    a misspelled closure signal_type would silently never match anything)."""
    known = set(sig.known_signal_types())
    for request_type, closure_type in sig.REQUEST_TO_CLOSURE_SIGNAL.items():
        assert request_type in known, f"{request_type!r} is not a real signal_type"
        assert closure_type in known, f"{closure_type!r} is not a real signal_type"


def test_concur_has_no_request_to_closure_entry():
    """Deliberately absent - no matching closure template exists in _RULES
    for it, so requiring one would freeze an issue 'active' forever."""
    assert "concur_expense_reminder" not in sig.REQUEST_TO_CLOSURE_SIGNAL


def test_unrelated_domain_does_not_match():
    assert sig.domain_matches("someone@example.com", "ariba.com") is False


def test_malformed_email_does_not_crash():
    assert sig.domain_matches("not-an-email", "ariba.com") is False


# --- reference_base (2026-07-31, meeting-grouping/related-project pass) --

def test_reference_base_strips_version_suffix():
    assert sig.reference_base("PR416079-V33") == "PR416079"


def test_reference_base_different_versions_reduce_to_same_base():
    """The real production pair this fixes: PR1140347-V2 and PR1140347-V3
    both exist today as two 'unrelated' strings under exact matching."""
    assert sig.reference_base("PR1140347-V2") == sig.reference_base("PR1140347-V3") == "PR1140347"


def test_reference_base_no_version_suffix_is_unchanged():
    assert sig.reference_base("PR1111865") == "PR1111865"


def test_reference_base_uppercases():
    assert sig.reference_base("pr416079-v33") == "PR416079"


def test_reference_base_none_and_empty_pass_through():
    assert sig.reference_base(None) is None
    assert sig.reference_base("") == ""


def test_classify_signal_includes_reference_base():
    result = sig.classify_signal(
        subject="Notification: Requisition has been fully approved (PR416079-V33)",
        from_actor="noreply@ansmtp.ariba.com",
    )
    assert result is not None
    assert result["pr_number"] == "PR416079-V33"
    assert result["pr_number_base"] == "PR416079"


# --- jasper_ref_issue_id (task #36, Jasper's own outbound reference tag) ---

def test_jasper_ref_issue_id_extracts_real_tag():
    assert sig.jasper_ref_issue_id("Thanks, sounds good.\n\nRef: JW-marc-308") == "marc-308"


def test_jasper_ref_issue_id_case_insensitive_prefix():
    assert sig.jasper_ref_issue_id("ref: jw-marc-42") == "marc-42"


def test_jasper_ref_issue_id_none_when_absent():
    assert sig.jasper_ref_issue_id("just a normal reply, no tag here") is None


def test_jasper_ref_issue_id_none_and_empty_pass_through():
    assert sig.jasper_ref_issue_id(None) is None
    assert sig.jasper_ref_issue_id("") is None


def test_jasper_ref_issue_id_finds_it_inside_quoted_reply_body():
    text = (
        "Sure, works for me.\n\n"
        "On Mon, Aug 3, 2026 at 9:00 AM Marc Lane wrote:\n"
        "> Can you confirm the renewal date?\n"
        "> \n"
        "> Ref: JW-marc-308"
    )
    assert sig.jasper_ref_issue_id(text) == "marc-308"


# --- is_personal_calendar_block / is_ooo_subject (personal/OOO filter) ---

def test_is_personal_calendar_block_true_when_only_participant_is_organizer():
    """Real confirmed shape: HOLD/Focus Time/School Drop off/Pick up all
    have the organizer as the only real participant."""
    assert sig.is_personal_calendar_block(organizer="lane_marc@lilly.com", participants=["lane_marc@lilly.com"]) is True


def test_is_personal_calendar_block_true_when_no_participants_at_all():
    assert sig.is_personal_calendar_block(organizer="lane_marc@lilly.com", participants=[]) is True
    assert sig.is_personal_calendar_block(organizer="lane_marc@lilly.com", participants=None) is True


def test_is_personal_calendar_block_false_with_a_real_other_attendee():
    assert sig.is_personal_calendar_block(
        organizer="lane_marc@lilly.com", participants=["lane_marc@lilly.com", "rep@acme.com"]
    ) is False


def test_is_personal_calendar_block_false_with_no_organizer():
    assert sig.is_personal_calendar_block(organizer=None, participants=[]) is False


def test_is_personal_calendar_block_case_insensitive():
    assert sig.is_personal_calendar_block(organizer="Lane_Marc@Lilly.com", participants=["lane_marc@lilly.com"]) is True


def test_is_ooo_subject_matches_real_confirmed_examples():
    assert sig.is_ooo_subject("Lane - OOO") is True
    assert sig.is_ooo_subject("Dima OOO Paternity Leave") is True


def test_is_ooo_subject_false_for_ordinary_meeting():
    assert sig.is_ooo_subject("C5 Contracts Weekly Touchbase") is False


def test_is_ooo_subject_false_for_none():
    assert sig.is_ooo_subject(None) is False


def test_is_automated_sender_covers_local_part_pattern():
    assert sig.is_automated_sender("no-reply@ansmtp.ariba.com") is True
    assert sig.is_automated_sender("notifications@github.com") is True


def test_is_automated_sender_covers_machine_signal_domains():
    """Task #53: a real live over-merge (proj-012 "Adobesign," 15 unrelated
    issues wrongly combined) traced to workgraph_projects.py's grouping code
    only ever checking _SYSTEM_SENDER, never this domain list - fixed by
    consolidating both into this one combined check."""
    assert sig.is_automated_sender("adobesign@adobesign.com") is True
    assert sig.is_automated_sender("EmailReminderService@concursolutions.com") is True


def test_is_automated_sender_false_for_a_real_person():
    assert sig.is_automated_sender("real.person@acme.com") is False


def test_is_automated_sender_false_for_none_or_empty():
    assert sig.is_automated_sender(None) is False
    assert sig.is_automated_sender("") is False


# --- extract_ariba_requisition_fields (task #169/#170, 2026-08-04) ----------
# Real fields out of an Ariba requisition-approval subject, for the grouping
# model's Ariba-specific matching signal - is_automated_sender already
# excludes the notification address itself from party/company matching, so
# without this, two different Ariba requisitions (or two versions of the
# same one) look structurally identical to the grouping signature.

def test_extract_ariba_requisition_fields_real_subject():
    fields = sig.extract_ariba_requisition_fields(
        "Action required: Approve the Requisition that THOMAS TURNER submitted  - "
        "PR1193376 - Workday HCM SaaS ($53,702,143.00 USD)"
    )
    assert fields == {
        "requester": "THOMAS TURNER",
        "pr_number": "PR1193376",
        "descriptor": "Workday HCM SaaS",
        "amount": 53702143.0,
    }


def test_extract_ariba_requisition_fields_versioned_pr():
    fields = sig.extract_ariba_requisition_fields(
        "Action required: Approve the Requisition that ALICIA MORRIS submitted  - "
        "PR854779-V4 - Conversational AI ($1,938,100.00 USD)"
    )
    assert fields["pr_number"] == "PR854779-V4"
    assert fields["amount"] == 1938100.0


def test_extract_ariba_requisition_fields_none_for_unrelated_subject():
    assert sig.extract_ariba_requisition_fields("Some unrelated subject with no requisition info") is None


def test_extract_ariba_requisition_fields_none_for_empty():
    assert sig.extract_ariba_requisition_fields("") is None
    assert sig.extract_ariba_requisition_fields(None) is None


# --- extract_ariba_supplier_field / normalize_company_name (2026-08-05, ---
# Marc's direct design ask, live on the Authenticx case): the requisition's
# real vendor lives in the line-item table's own "Supplier" field in the
# BODY, never the subject - extract_ariba_requisition_fields above can't
# see it at all. Ariba/SAP must never surface as "the supplier" - only the
# real vendor named in that field counts.

def test_extract_ariba_supplier_field_from_real_html_shape():
    """Real captured shape (PR854779-V4, Conversational AI) run through
    text_extract._html_to_text exactly the way resolve_item_text would -
    every tag collapses to one space, so the real cell sequence
    '...>Supplier </td>...<td>AUTHENTICX INC </td>...<td>...>Qty</td>...'
    comes out flat: 'Supplier AUTHENTICX INC Qty 1.00 Unit Power Unit...'."""
    import text_extract as te
    html = (
        '<td style="padding-bottom:2px">Description</td></tr><tr>'
        '<td style="padding-bottom:15px">Authenticx will enable Eli Lilly to '
        'analyze (TLAC) and (LAC) call recordings.</td></tr></tbody></table>'
        '</td><td style="padding-right:15px"><table><tbody><tr>'
        '<td style="padding-bottom:2px">Supplier </td></tr><tr>'
        '<td style="padding-bottom:15px">AUTHENTICX INC </td></tr></tbody>'
        '</table></td><td style="padding-right:15px"><table><tbody><tr>'
        '<td style="padding-bottom:2px">Qty</td></tr><tr>'
        '<td style="padding-bottom:15px">1.00</td></tr></tbody></table></td>'
    )
    text = te._html_to_text(html)
    assert sig.extract_ariba_supplier_field(text) == "AUTHENTICX INC"


def test_extract_ariba_supplier_field_plain_text_with_newline():
    text = "Description: Widgets\nSupplier: Acme Vendor Co\nQty: 1.00\n"
    assert sig.extract_ariba_supplier_field(text) == "Acme Vendor Co"


def test_extract_ariba_supplier_field_none_when_absent():
    assert sig.extract_ariba_supplier_field("no supplier field in this text at all") is None
    assert sig.extract_ariba_supplier_field("") is None
    assert sig.extract_ariba_supplier_field(None) is None


def test_extract_ariba_supplier_field_never_returns_the_transport_system():
    """Marc's own words: 'the supplier needs to be identified as authenticx
    and not ariba/sap' - a defensive floor in case a malformed/atypical
    body ever put the transport system's own name in this field."""
    assert sig.extract_ariba_supplier_field("Supplier Ariba Qty 1.00") is None
    assert sig.extract_ariba_supplier_field("Supplier SAP Qty 1.00") is None


def test_normalize_company_name_strips_corporate_suffix_and_case():
    assert sig.normalize_company_name("AUTHENTICX INC") == "authenticx"
    assert sig.normalize_company_name("Authenticx") == "authenticx"
    assert sig.normalize_company_name("Acme Vendor Co") == "acme vendor"
    assert sig.normalize_company_name("") == ""
    assert sig.normalize_company_name(None) == ""


def test_is_automated_sender_covers_sap_alert_domain():
    """Task #169/#170 (2026-08-04, Marc's direct report): SAP's bulk alert
    feed uses alerts.ondemand.com, a different domain than plain sap.com -
    a real @sap.com person address must stay a genuine party signal."""
    assert sig.is_automated_sender("sapcloudsupport@alerts.ondemand.com") is True
    assert sig.is_automated_sender("real.rep@sap.com") is False
