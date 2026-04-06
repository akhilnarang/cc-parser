"""Card number detection, masking, and person-label utilities."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cc_parser.parsers.models import Transaction

from cc_parser.parsers.tokens import (
    HONORIFIC_RE,
    SEPARATOR_TOKENS,
    normalize_token,
    parse_amount_token,
    parse_date_token,
    parse_time_token,
)

CARD_TOKEN_RE = re.compile(r"(?<![0-9A-Za-z])[0-9Xx*]{10,20}(?![0-9A-Za-z])")
CARD_TOKEN_WITH_SEP_RE = re.compile(
    r"(?<![0-9A-Za-z])[0-9Xx*][0-9Xx*\s-]{8,30}[0-9Xx*](?![0-9A-Za-z])"
)
CARD_LABEL_WORDS = {
    "CREDIT",
    "CARD",
    "NO",
    "NUMBER",
    "STATEMENT",
    "DATE",
    "PRIMARY",
    "ADDON",
    "ADDONCARD",
    "SUPPLEMENTARY",
}
NON_MEMBER_HEADERS = {
    "DOMESTIC TRANSACTIONS",
    "INTERNATIONAL TRANSACTIONS",
    "DATE TIME TRANSACTION DESCRIPTION REWARDS AMOUNT PI",
    "OFFERS ON YOUR CARD",
    "BENEFITS ON YOUR CARD",
    "CREDIT CARD STATEMENT",
    "MILLENNIA CREDIT CARD STATEMENT",
    "DINERS BLACK CREDIT CARD STATEMENT",
}


def normalize_card_token(token: str) -> str:
    """Keep only card-mask characters and normalize mask symbol to ``X``."""
    return re.sub(r"[^0-9Xx*]", "", token).upper().replace("*", "X")


def looks_like_card_token(token: str) -> bool:
    """Heuristically decide whether token resembles a masked card number."""
    normalized = normalize_card_token(token)
    if not (10 <= len(normalized) <= 20):
        return False
    digit_count = sum(ch.isdigit() for ch in normalized)
    x_count = normalized.count("X")
    return digit_count >= 6 and x_count >= 2


def mask_card_token(token: str) -> str:
    """Mask middle digits of a card-like token."""
    normalized = normalize_card_token(token)
    if len(normalized) < 8:
        return normalized
    return f"{normalized[:4]}{'X' * (len(normalized) - 8)}{normalized[-4:]}"


def find_card_candidates(text: str) -> list[str]:
    """Find masked card-like values in arbitrary text."""
    found: list[str] = []
    seen: set[str] = set()

    for match in CARD_TOKEN_RE.finditer(text):
        token = normalize_card_token(match.group(0))
        if looks_like_card_token(token):
            masked = mask_card_token(token)
            if masked not in seen:
                found.append(masked)
                seen.add(masked)

    for match in CARD_TOKEN_WITH_SEP_RE.finditer(text):
        token = normalize_card_token(match.group(0))
        if looks_like_card_token(token):
            masked = mask_card_token(token)
            if masked not in seen:
                found.append(masked)
                seen.add(masked)

    return found


def extract_card_from_line(tokens: list[str]) -> tuple[str | None, str | None]:
    """Extract card and optional member label from a tokenized line."""
    card_indices: list[int] = []
    card_value: str | None = None

    for index, token in enumerate(tokens):
        normalized = normalize_card_token(token)
        if looks_like_card_token(normalized):
            card_indices = [index]
            card_value = mask_card_token(normalized)
            break

    if card_value is None:
        for size in (2, 3):
            for start in range(0, len(tokens) - size + 1):
                chunk = "".join(
                    normalize_card_token(tokens[start + offset])
                    for offset in range(size)
                )
                if looks_like_card_token(chunk):
                    card_indices = list(range(start, start + size))
                    card_value = mask_card_token(chunk)
                    break
            if card_value:
                break

    if card_value is None:
        return None, None

    candidate_tokens = [
        token for i, token in enumerate(tokens) if i not in set(card_indices)
    ]
    words_only = []
    for token in candidate_tokens:
        cleaned = re.sub(r"[^A-Za-z.'-]", "", token)
        if not cleaned:
            continue
        key = re.sub(r"[^A-Za-z]", "", cleaned).upper()
        if key in CARD_LABEL_WORDS:
            continue
        if key in {"SPENDS", "OVERVIEW", "TRANSACTION", "DETAILS"}:
            continue
        if len(cleaned) == 1:
            continue
        words_only.append(cleaned)

    if words_only and HONORIFIC_RE.fullmatch(words_only[0]):
        words_only = words_only[1:]

    member = " ".join(words_only).strip() if words_only else None
    if member and member.upper() in {"CREDIT CARD", "CREDIT CARD NO", "NO"}:
        member = None

    # Only accept member names that look like real person names:
    # at least 2 words, each word at least 3 characters.
    if member:
        parts = member.split()
        if len(parts) < 2 or not all(len(p) >= 3 for p in parts):
            member = None

    return card_value, member


def looks_like_member_header(tokens: list[str]) -> str | None:
    """Detect whether a line looks like a person/member section header."""
    cleaned_words: list[str] = []
    for token in tokens:
        value = normalize_token(token)
        if (
            not value
            or parse_date_token(value)
            or parse_time_token(value)
            or parse_amount_token(value)
        ):
            return None
        if value in SEPARATOR_TOKENS:
            continue
        letters = re.sub(r"[^A-Za-z ]", "", value).strip()
        if not letters:
            return None
        if len(letters) == 1:
            return None
        cleaned_words.append(letters.upper())

    if not (2 <= len(cleaned_words) <= 4):
        return None

    candidate = " ".join(cleaned_words)
    if candidate in NON_MEMBER_HEADERS:
        return None
    if (
        "CREDIT" in cleaned_words
        and "CARD" in cleaned_words
        and "STATEMENT" in cleaned_words
    ):
        return None
    if any(
        word in {"CARD", "STATEMENT", "DATE", "TIME", "DESCRIPTION"}
        for word in cleaned_words
    ):
        return None
    if any(
        word in {"TRANSACTIONS", "REWARDS", "PROGRAM", "SUMMARY"}
        for word in cleaned_words
    ):
        return None
    return candidate


def is_invalid_person_label(value: str | None) -> bool:
    """Check whether extracted person label is clearly a section/header label."""
    if not value:
        return True
    words = [w for w in re.sub(r"[^A-Za-z ]", "", value).upper().split() if w]
    if not words:
        return True
    candidate = " ".join(words)
    if candidate in NON_MEMBER_HEADERS:
        return True
    if "CREDIT" in words and "CARD" in words and "STATEMENT" in words:
        return True
    if any(
        word in {"CARD", "STATEMENT", "DATE", "TIME", "DESCRIPTION"} for word in words
    ):
        return True
    return False


def normalize_transaction_persons(
    transactions: list[Transaction], fallback_name: str | None
) -> None:
    """Replace invalid person labels on transactions with a fallback name.

    Args:
        transactions: List of transactions to normalize in-place.
        fallback_name: Name to use when person label is invalid.
    """
    for txn in transactions:
        person_value = (txn.person or "").strip()
        if is_invalid_person_label(person_value):
            txn.person = fallback_name


def split_by_transaction_type(
    transactions: list[Transaction],
) -> tuple[list[Transaction], list[Transaction]]:
    """Split transactions into debits and credits.

    Args:
        transactions: List of all transactions.

    Returns:
        Tuple of (debit_transactions, credit_transactions).
    """
    debits = [t for t in transactions if t.transaction_type != "credit"]
    credits = [t for t in transactions if t.transaction_type == "credit"]
    return debits, credits


def extract_card_number(full_text: str) -> str | None:
    """Extract first masked card number from statement text."""
    candidates = find_card_candidates(full_text)
    return candidates[0] if candidates else None


def extract_card_from_filename(file_path: str) -> str | None:
    """Extract masked card number from input file name."""
    from pathlib import Path

    candidates = find_card_candidates(Path(file_path).name)
    return candidates[0] if candidates else None


__all__ = [
    "CARD_TOKEN_RE",
    "CARD_TOKEN_WITH_SEP_RE",
    "CARD_LABEL_WORDS",
    "NON_MEMBER_HEADERS",
    "normalize_card_token",
    "looks_like_card_token",
    "mask_card_token",
    "find_card_candidates",
    "extract_card_from_line",
    "looks_like_member_header",
    "is_invalid_person_label",
    "normalize_transaction_persons",
    "split_by_transaction_type",
    "extract_card_number",
    "extract_card_from_filename",
]
