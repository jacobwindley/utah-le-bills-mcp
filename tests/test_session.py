import pytest

from le_bills_mcp.session import InvalidInput, normalize_bill, normalize_prefix, normalize_session


@pytest.mark.parametrize(
    "raw,expected",
    [("2026", "2026GS"), ("2026gs", "2026GS"), ("2025s1", "2025S1"), ("2025 S2", "2025S2"), (2024, "2024GS"), (None, None), ("", None)],
)
def test_normalize_session(raw, expected):
    assert normalize_session(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("hb 1", "HB0001"),
        ("H.B. 1", "HB0001"),
        ("HB1", "HB0001"),
        ("sb0042", "SB0042"),
        ("HB0005S01", "HB0005"),
        ("hcr 3", "HCR003"),
        ("SJR12", "SJR012"),
        ("HR 2", "HR0002"),
    ],
)
def test_normalize_bill(raw, expected):
    assert normalize_bill(raw) == expected


@pytest.mark.parametrize("raw", ["XB1", "HB", "2026", "HB 12345", "drop table"])
def test_normalize_bill_rejects(raw):
    with pytest.raises(InvalidInput):
        normalize_bill(raw)


def test_bad_session_and_prefix():
    with pytest.raises(InvalidInput):
        normalize_session("last year")
    with pytest.raises(InvalidInput):
        normalize_prefix("XX")
    assert normalize_prefix("s.b.") == "SB"
