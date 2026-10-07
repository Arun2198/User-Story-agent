import string

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from story_agent.guardrails.redaction import (
    Redactor,
    Span,
    find_spans,
    iban_ok,
    luhn_ok,
)


def test_each_entity_type() -> None:
    text = (
        "Mail a.b@example.com, phone +91 98765 43210, card 4111 1111 1111 1111, "
        "PAN ABCDE1234F, Aadhaar 2345 6789 0123, IFSC HDFC0001234, account number 123456789012, "
        "password: hunter2xyz, IBAN GB82 WEST 1234 5698 7654 32, SSN 123-45-6789"
    )
    out = Redactor().redact(text)
    for label in (
        "EMAIL",
        "PHONE",
        "CARD",
        "PAN",
        "AADHAAR",
        "IFSC",
        "ACCOUNT",
        "PASSWORD",
        "IBAN",
        "SSN",
    ):
        assert f"<{label}_1>" in out
    assert "hunter2xyz" not in out
    assert out.count("<") == 10


def test_stable_placeholders_for_repeated_values() -> None:
    out = Redactor().redact("x@y.com wrote to z@y.com and x@y.com again")
    assert out == "<EMAIL_1> wrote to <EMAIL_2> and <EMAIL_1> again"


def test_numbering_shared_across_texts() -> None:
    r = Redactor()
    assert r.redact("a@b.co") == "<EMAIL_1>"
    assert r.redact("c@d.co and a@b.co") == "<EMAIL_2> and <EMAIL_1>"


def test_existing_placeholders_are_kept_and_numbering_continues() -> None:
    r = Redactor()
    assert r.redact("<EMAIL_3> and new@x.io") == "<EMAIL_3> and <EMAIL_4>"


def test_restore_is_explicit() -> None:
    r = Redactor()
    out = r.redact("mail a@b.co now")
    assert "a@b.co" not in out
    assert r.restore(out) == "mail a@b.co now"
    assert r.restore("<EMAIL_9>") == "<EMAIL_9>"


def test_resume_from_saved_mapping() -> None:
    first = Redactor()
    first.redact("a@b.co")
    second = Redactor(first.mapping)
    assert second.redact("a@b.co c@d.co") == "<EMAIL_1> <EMAIL_2>"


def test_secret_value_keeps_trailing_punctuation() -> None:
    assert Redactor().redact("password: abc123xyz, next") == "password: <PASSWORD_1>, next"


def test_mistyped_numbers_do_not_leak() -> None:
    assert Redactor().redact("number 4111 1111 1111 1112") == "number <CARD_1>"
    assert Redactor().redact("number 4111111111111112") == "number <ACCOUNT_1>"


def test_invalid_iban_is_not_redacted() -> None:
    text = "code GB00 WEST 1234 5698 7654 32"
    assert Redactor().redact(text) == text


def test_checksum_helpers() -> None:
    assert luhn_ok("4111111111111111")
    assert not luhn_ok("4111111111111112")
    assert iban_ok("GB82 WEST 1234 5698 7654 32")
    assert not iban_ok("GB82")


def test_plain_numbers_and_words_untouched() -> None:
    text = "limit 100000, 5,000,000 paise, 12 months, REF9876543210, 2026-10-07"
    assert Redactor().redact(text) == text


def test_custom_detector_is_used() -> None:
    class Names:
        def detect(self, text: str) -> list[Span]:
            i = text.find("Alice")
            return [] if i < 0 else [Span(i, i + 5, "NAME", 8)]

    assert Redactor(detectors=[Names()]).redact("Alice paid") == "<NAME_1> paid"


def test_count() -> None:
    r = Redactor()
    assert r.count("a@b.co and c@d.co") == 2
    assert r.count("<EMAIL_1>") == 0


def test_find_spans_prefers_higher_priority_on_overlap() -> None:
    spans = find_spans("account number 4111111111111111")
    assert [s.label for s in spans] == ["ACCOUNT"]


_ALPHA = st.text(alphabet=string.ascii_lowercase + " ,.", min_size=0, max_size=20)


def _luhn_card(prefix: str) -> str:
    base = prefix.ljust(15, "0")[:15]
    for check in "0123456789":
        if luhn_ok(base + check):
            return base + check
    raise AssertionError("unreachable")  # pragma: no cover


_SENSITIVE = st.one_of(
    st.builds(
        lambda a, b: f"{a}@{b}.com",
        st.text(string.ascii_lowercase, min_size=1, max_size=8),
        st.text(string.ascii_lowercase, min_size=1, max_size=8),
    ),
    st.builds(
        _luhn_card, st.text(string.digits, min_size=6, max_size=6).map(lambda s: "4" + s[1:])
    ),
    st.builds(lambda n: f"ABCDE{n}F", st.text(string.digits, min_size=4, max_size=4)),
    st.builds(
        lambda n: f"HDFC0{n}",
        st.text(string.digits + string.ascii_uppercase, min_size=6, max_size=6),
    ),
)


@given(st.lists(st.tuples(_ALPHA, _SENSITIVE), min_size=1, max_size=5), _ALPHA)
def test_property_no_sensitive_value_survives(parts: list[tuple[str, str]], tail: str) -> None:
    text = " ".join(f"{filler} {value}" for filler, value in parts) + " " + tail
    redactor = Redactor()
    out = redactor.redact(text)
    for _, value in parts:
        assert value not in out
    assert redactor.count(out) == 0


@given(st.lists(st.tuples(_ALPHA, _SENSITIVE), min_size=1, max_size=5))
def test_property_same_value_same_placeholder(parts: list[tuple[str, str]]) -> None:
    value = parts[0][1]
    out = Redactor().redact(f"{value} then {value}")
    placeholders = [w for w in out.split() if w.startswith("<")]
    assert len(placeholders) == 2
    assert placeholders[0] == placeholders[1]


@settings(max_examples=200)
@given(st.text(max_size=200))
def test_property_roundtrip_and_idempotence(text: str) -> None:
    redactor = Redactor()
    once = redactor.redact(text)
    assert redactor.restore(once) == text
    assert Redactor(redactor.mapping).redact(once) == once


@pytest.mark.parametrize("empty", ["", "   ", "\n"])
def test_empty_text(empty: str) -> None:
    assert Redactor().redact(empty) == empty
