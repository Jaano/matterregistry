"""Unit tests for resolve_lang's Accept-Language handling."""

from app.i18n import resolve_lang


def test_cookie_wins_over_header():
    assert resolve_lang("de", "en") == "de"


def test_invalid_cookie_falls_back_to_header():
    assert resolve_lang("xx", "de") == "de"


def test_short_code_matches_regional_tag():
    assert resolve_lang("", "de-DE") == "de"


def test_no_match_falls_back_to_english():
    assert resolve_lang("", "ko-KR") == "en"


def test_q_weight_picks_higher_priority_language():
    assert resolve_lang("", "en;q=0.5, de;q=0.9") == "de"


def test_document_order_wins_on_equal_weight():
    assert resolve_lang("", "fr;q=0.8, de;q=0.8") == "fr"


def test_missing_q_defaults_to_1():
    assert resolve_lang("", "de;q=0.5, fr") == "fr"


def test_unparseable_q_is_deprioritized():
    assert resolve_lang("", "de;q=bogus, fr;q=0.1") == "fr"
