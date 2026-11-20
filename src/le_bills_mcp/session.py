"""Normalisation helpers for session ids and bill numbers."""

from __future__ import annotations

import re
from datetime import date

BILL_PREFIXES = ("HJR", "SJR", "HCR", "SCR", "HB", "SB", "HR", "SR")
_SESSION_RE = re.compile(r"^(\d{4})\s*(GS|S\d+|GENERAL|SPECIAL\s*\d+)?$")
_BILL_RE = re.compile(r"^(HJR|SJR|HCR|SCR|HB|SB|HR|SR)0*(\d{1,4})(S\d+)?$")


class InvalidInput(ValueError):
    pass


def normalize_session(session: str | int | None) -> str | None:
    """'2026' -> '2026GS', '2025s1' -> '2025S1'. None stays None (caller picks default)."""
    if session is None or str(session).strip() == "":
        return None
    s = str(session).strip().upper().replace("-", "").replace("_", "")
    m = _SESSION_RE.match(s)
    if not m:
        raise InvalidInput(
            f"Unrecognised session {session!r}. Use a year ('2026'), '2026GS' for a "
            "general session, or '2025S1' for a special session."
        )
    year, kind = m.groups()
    if kind is None or kind in ("GS", "GENERAL"):
        return f"{year}GS"
    if kind.startswith("SPECIAL"):
        number = re.sub(r"\D", "", kind)
        return f"{year}S{number}"
    return f"{year}{kind}"


def normalize_bill(bill: str) -> str:
    """'hb 1', 'H.B. 1', 'HB1', 'HB0001S01' -> 'HB0001'; 'hcr 3' -> 'HCR003'."""
    s = re.sub(r"[\s.]", "", str(bill).upper())
    m = _BILL_RE.match(s)
    if not m:
        raise InvalidInput(
            f"Unrecognised bill number {bill!r}. Expected e.g. 'HB 1', 'S.B. 42', 'HJR3'."
        )
    prefix, num, _sub = m.groups()
    return prefix + num.zfill(6 - len(prefix))


def normalize_prefix(prefix: str) -> str:
    p = re.sub(r"[\s.]", "", prefix.upper())
    if p not in BILL_PREFIXES:
        raise InvalidInput(f"Unknown bill prefix {prefix!r}. Use one of {', '.join(BILL_PREFIXES)}.")
    return p


def candidate_default_sessions(today: date | None = None) -> list[str]:
    """Current year's general session, then last year's."""
    year = (today or date.today()).year
    return [f"{year}GS", f"{year - 1}GS"]
