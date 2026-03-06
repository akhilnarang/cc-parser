"""Token-level parsing utilities.

Regex constants and helpers for parsing dates, times, amounts, and
other atomic tokens extracted from PDF word lists.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cc_parser.parsers.models import Transaction

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


def clean_space(value: str) -> str:
    """Collapse repeated whitespace in a string."""
    return " ".join(value.split())


def normalize_token(token: str) -> str:
    """Normalize a token extracted from PDF words."""
    value = token.strip()
    value = value.strip("|")
    return value


def parse_date_token(token: str) -> str | None:
    """Parse a statement date token, returning ``DD/MM/YYYY`` or None."""
    value = normalize_token(token)
    return value if DATE_RE.fullmatch(value) else None


def parse_multi_token_date(tokens: list[str], start: int) -> tuple[str | None, int]:
    """Parse a ``DD Mon YY`` date spread across three tokens.

    Args:
        tokens: Token list from a visual line.
        start: Index to start parsing from.

    Returns:
        ``(date_str, tokens_consumed)`` where *date_str* is ``DD/MM/YYYY``,
        or ``(None, 0)`` on failure.
    """
    if start + 2 >= len(tokens):
        return None, 0
    day = normalize_token(tokens[start])
    month_tok = normalize_token(tokens[start + 1])
    year_tok = normalize_token(tokens[start + 2])

    if not re.fullmatch(r"\d{1,2}", day):
        return None, 0
    month = MONTH_ABBREVS.get(month_tok.upper())
    if month is None:
        return None, 0
    if not re.fullmatch(r"\d{2,4}", year_tok):
        return None, 0

    day_padded = day.zfill(2)
    year = year_tok if len(year_tok) == 4 else f"20{year_tok}"
    return f"{day_padded}/{month}/{year}", 3


def normalize_date_long(raw: str) -> str:
    """Convert ``DD Mon YYYY`` (e.g. ``08 Nov 2025``) to ``DD/MM/YYYY``."""
    parts = clean_space(raw).split()
    if len(parts) == 3:
        month = MONTH_ABBREVS.get(parts[1].upper()[:3])
        if month:
            return f"{parts[0].zfill(2)}/{month}/{parts[2]}"
    return raw


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
        return Decimal("0")


def parse_points(points: str | None) -> Decimal:
    """Extract numeric reward points from a token."""
    if not points:
        return Decimal("0")
    value = str(points).strip()
    match = re.search(r"\d+", value)
    if not match:
        return Decimal("0")
    return Decimal(match.group(0))


def format_amount(value: Decimal) -> str:
    """Format Decimal as comma-separated 2-decimal amount string."""
    return f"{value:,.2f}"


def sum_amounts(transactions: list[Transaction]) -> Decimal:
    """Sum the amount field across transaction rows."""
    total = Decimal("0")
    for txn in transactions:
        total += parse_amount(str(txn.amount or "0"))
    return total


def sum_points(transactions: list[Transaction]) -> Decimal:
    """Sum reward points across transaction rows."""
    total = Decimal("0")
    for txn in transactions:
        total += parse_points(txn.reward_points)
    return total


__all__ = [
    "DATE_RE",
    "TIME_RE",
    "AMOUNT_RE",
    "HONORIFIC_RE",
    "MONTH_ABBREVS",
    "SEPARATOR_TOKENS",
    "clean_space",
    "normalize_token",
    "parse_date_token",
    "parse_multi_token_date",
    "normalize_date_long",
    "parse_time_token",
    "parse_amount_token",
    "normalize_amount",
    "parse_amount",
    "parse_points",
    "format_amount",
    "sum_amounts",
    "sum_points",
]
