from smart_dilution_extractor.models import DilutionEvent, OfferingType
from smart_dilution_extractor.validators import ground_shares_in_quote, validate_event


def _ev(shares, quote):
    return DilutionEvent(offering_type=OfferingType.warrant_exercise, shares=shares, source_quote=quote)


def test_misread_digit_is_corrected_to_quote():
    ev = _ev(2_339_675, "2,349,675 shares issuable upon exercise of warrants at $2.36 per share")
    assert ground_shares_in_quote(ev) == "shares_corrected_from_quote"
    assert ev.shares == 2_349_675


def test_grounded_value_untouched():
    ev = _ev(2_349_675, "2,349,675 shares issuable upon exercise of warrants")
    assert ground_shares_in_quote(ev) == "shares_grounded"
    assert ev.shares == 2_349_675


def test_unrelated_number_flagged_not_changed():
    ev = _ev(7_143_000, "On July 24, 2025, the closing price was $2.04 per share; 100,000,000 authorized")
    assert ground_shares_in_quote(ev) == "shares_not_in_quote"
    assert ev.shares == 7_143_000


def test_quote_without_full_numerals_is_ignored():
    assert ground_shares_in_quote(_ev(2_200_000, "approximately 2.2 million shares")) is None


def test_validate_event_records_flag_and_penalty():
    ok = validate_event(_ev(1_000_000, "1,000,000 shares issuable"))
    bad = validate_event(_ev(5_000_000, "1,000,000 shares issuable"))
    assert "shares_grounded" in ok.validation_flags
    assert "shares_not_in_quote" in bad.validation_flags
    assert bad.confidence < ok.confidence
