"""Token-level parsing utilities.

Regex constants and helpers for parsing dates, times, amounts, and
other atomic tokens extracted from PDF word lists.
"""

import re
from decimal import Decimal, InvalidOperation

DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
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


def sum_amounts(transactions: list[dict], key: str = "amount") -> Decimal:
    """Sum a decimal field across transaction rows."""
    total = Decimal("0")
    for txn in transactions:
        total += parse_amount(str(txn.get(key) or "0"))
    return total


def sum_points(transactions: list[dict]) -> Decimal:
    """Sum reward points across transaction rows."""
    total = Decimal("0")
    for txn in transactions:
        total += parse_points(txn.get("reward_points"))
    return total


__all__ = [
    "DATE_RE",
    "TIME_RE",
    "AMOUNT_RE",
    "HONORIFIC_RE",
    "SEPARATOR_TOKENS",
    "clean_space",
    "normalize_token",
    "parse_date_token",
    "parse_time_token",
    "parse_amount_token",
    "normalize_amount",
    "parse_amount",
    "parse_points",
    "format_amount",
    "sum_amounts",
    "sum_points",
]
