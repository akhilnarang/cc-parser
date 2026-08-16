"""Token-level parsing utilities.

Regex constants and helpers for parsing dates, times, amounts, and
other atomic tokens extracted from PDF word lists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from dateutil import parser as date_parser
from dateutil.parser import parserinfo

if TYPE_CHECKING:
    from cc_parser.parsers.models import Transaction


class _StatementParserInfo(parserinfo):
    """Dateutil parser info with stable 2000-based two-digit year handling."""

    def convertyear(self, year: int, century_specified: bool = False) -> int:
        if century_specified or year >= 100:
            return year
        return 2000 + year


@dataclass(frozen=True, slots=True)
class DateParseHints:
    """Optional date parsing hints for non-standard statement formats."""

    dayfirst: bool = True
    yearfirst: bool = False
    fuzzy: bool = False
    parser_info: parserinfo | None = None


DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
MONTH_ABBREVS = {
    "JAN": "01",
    "FEB": "02",
    "MAR": "03",
    "APR": "04",
    "MAY": "05",
    "JUN": "06",
    "JUL": "07",
    "AUG": "08",
    "SEP": "09",
    "OCT": "10",
    "NOV": "11",
    "DEC": "12",
}
TIME_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
AMOUNT_RE = re.compile(r"[-+]?\d[\d,]*\.\d{2}")
HONORIFIC_RE = re.compile(r"^(MR|MRS|MS|MISS|DR)\.?$", re.IGNORECASE)
SEPARATOR_TOKENS = {"|", "||", ":", "-", "--"}
_DEFAULT_DATE_HINTS = DateParseHints()
_DEFAULT_PARSER_INFO = _StatementParserInfo(dayfirst=True, yearfirst=False)


def clean_space(value: str) -> str:
    """Collapse repeated whitespace in a string."""
    return " ".join(value.split())


def normalize_token(token: str) -> str:
    """Normalize a token extracted from PDF words."""
    value = token.strip()
    value = value.strip("|")
    return value


def parse_date_value(
    value: str | None, hints: DateParseHints | None = None
) -> date | None:
    """Parse a statement date and return a ``date`` object when successful."""
    if not value:
        return None

    cleaned = clean_space(normalize_token(value))
    if not cleaned:
        return None

    if DATE_RE.fullmatch(cleaned):
        day, month, year = cleaned.split("/")
        try:
            return date(int(year), int(month), int(day))
        except ValueError:
            return None

    active_hints = hints or _DEFAULT_DATE_HINTS
    parser_info = active_hints.parser_info or _DEFAULT_PARSER_INFO
    try:
        parsed = date_parser.parse(
            cleaned,
            dayfirst=active_hints.dayfirst,
            yearfirst=active_hints.yearfirst,
            fuzzy=active_hints.fuzzy,
            parserinfo=parser_info,
        )
    except ValueError, OverflowError, TypeError:
        return None
    return parsed.date()


def parse_date(token: str, hints: DateParseHints | None = None) -> str | None:
    """Parse a statement date, returning ``DD/MM/YYYY`` or None."""
    parsed = parse_date_value(token, hints)
    if parsed is None:
        return None
    return parsed.strftime("%d/%m/%Y")


def parse_date_token(token: str) -> str | None:
    """Parse a strict transaction-date token, returning ``DD/MM/YYYY`` or None."""
    value = normalize_token(token)
    if DATE_RE.fullmatch(value):
        return parse_date(value)
    return None


def parse_multi_token_date(
    tokens: list[str], start: int, hints: DateParseHints | None = None
) -> tuple[str | None, int]:
    """Parse a multi-token date spread across three tokens."""
    if start + 2 >= len(tokens):
        return None, 0
    raw = " ".join(normalize_token(tokens[idx]) for idx in range(start, start + 3))
    parsed = parse_date(raw, hints)
    return (parsed, 3) if parsed else (None, 0)


def normalize_date_long(raw: str) -> str:
    """Convert long-form statement dates to ``DD/MM/YYYY`` when possible."""
    return parse_date(raw) or raw


def parse_time_token(token: str) -> str | None:
    """Parse a statement time token, returning ``HH:MM`` or None."""
    value = normalize_token(token)
    return value if TIME_RE.fullmatch(value) else None


def parse_amount_token(token: str) -> str | None:
    """Extract a decimal amount from a token, or None."""
    value = normalize_token(token).replace("`", "")
    match = AMOUNT_RE.search(value)
    return match.group(0) if match else None


def normalize_amount(value: str) -> str:
    """Remove statement currency markers from an amount string."""
    return value.replace("`", "")


def parse_amount(amount: str) -> Decimal:
    """Convert an amount string into Decimal (0 on failure)."""
    normalized = amount.replace("`", "").replace(",", "").strip()
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return Decimal(0)


def parse_points(points: str | None) -> Decimal:
    """Extract numeric reward points from a token."""
    if not points:
        return Decimal(0)
    value = str(points).strip()
    match = re.search(r"\d+", value)
    if not match:
        return Decimal(0)
    return Decimal(match.group(0))


def format_amount(value: Decimal) -> str:
    """Format Decimal as comma-separated 2-decimal amount string."""
    return f"{value:,.2f}"


def sum_amounts(transactions: list[Transaction]) -> Decimal:
    """Sum the amount field across transaction rows."""
    total = Decimal(0)
    for txn in transactions:
        total += parse_amount(str(txn.amount or "0"))
    return total


def sum_points(transactions: list[Transaction]) -> Decimal:
    """Sum reward points across transaction rows."""
    total = Decimal(0)
    for txn in transactions:
        total += parse_points(txn.reward_points)
    return total


__all__ = [
    "AMOUNT_RE",
    "DATE_RE",
    "HONORIFIC_RE",
    "MONTH_ABBREVS",
    "SEPARATOR_TOKENS",
    "TIME_RE",
    "DateParseHints",
    "clean_space",
    "format_amount",
    "normalize_amount",
    "normalize_date_long",
    "normalize_token",
    "parse_amount",
    "parse_amount_token",
    "parse_date",
    "parse_date_token",
    "parse_date_value",
    "parse_multi_token_date",
    "parse_points",
    "parse_time_token",
    "sum_amounts",
    "sum_points",
]
