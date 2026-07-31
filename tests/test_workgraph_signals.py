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
