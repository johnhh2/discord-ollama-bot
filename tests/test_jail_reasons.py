from src.jail_reasons import (
    format_steal_reason,
    format_mug_reason,
    format_bankheist_reason,
)


def test_steal_reason_uses_victim_display_name():
    assert format_steal_reason("Alice") == "Tried to steal from Alice"


def test_mug_reason_includes_amount_with_thousands_sep():
    assert format_mug_reason("Bob", 1500) == "Mugged Bob for 1,500 coins"


def test_bankheist_reason_stub():
    assert (
        format_bankheist_reason("First National")
        == "Participated in a bankheist targeting First National"
    )


def test_reasons_fit_in_varchar_255():
    # Discord display names are capped at 32 chars. Use a worst-case-ish 64
    # to leave headroom for any future relaxation, plus a 12-digit amount.
    long_name = "x" * 64
    assert len(format_steal_reason(long_name)) < 255
    assert len(format_mug_reason(long_name, 999_999_999_999)) < 255
    assert len(format_bankheist_reason(long_name)) < 255
