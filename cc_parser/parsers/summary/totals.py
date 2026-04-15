"""Statement-header and summary-field extraction helpers."""

import re
from typing import Any

from cc_parser.parsers.extraction import group_words_into_lines
from cc_parser.parsers.models import StatementSummary
from cc_parser.parsers.tokens import (
    clean_space,
    normalize_amount,
    normalize_token,
    parse_date,
)


def _normalize_due_date_value(value: str) -> str | None:
    """Normalize supported due-date formats to ``DD/MM/YYYY``."""
    return parse_date(value)


def extract_name(full_text: str) -> str | None:
    """Extract cardholder name from statement text."""
    honorifics = {"MR", "MRS", "MS", "MISS", "DR"}
    for raw_line in full_text.splitlines():
        line = clean_space(raw_line)
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3 or len(parts) > 6:
            continue
        if parts[0].upper() not in honorifics:
            continue
        tail = parts[1:]
        if all(re.fullmatch(r"[A-Za-z][A-Za-z.'-]*", token) for token in tail):
            return " ".join(tail)

    match = re.search(
        r"\n\s*([A-Z][A-Z ]{4,})\s+Credit\s+Card\s+No\.",
        full_text,
        flags=re.IGNORECASE,
    )
    if match:
        candidate = clean_space(match.group(1)).upper()
        if 2 <= len(candidate.split()) <= 6:
            return candidate

    return None


def extract_due_date(full_text: str) -> str | None:
    """Extract due date from statement body text."""
    compact_text = clean_space(full_text)
    patterns = [
        r"PAYMENT\s+DUE\s+DATE\s*[:\-]?\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
        r"PAYMENT\s+DUE\s+DATE\s*[:\-]?\s*(\d{2}[/-]\d{2}[/-]\d{4})",
        r"DUE\s+DATE\s*[:\-]?\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
        r"DUE\s+DATE\s*[:\-]?\s*(\d{2}[/-]\d{2}[/-]\d{4})",
        r"DUE\s+DATE.{0,100}?(\d{1,2}\s+[A-Za-z]{3,9},?\s+\d{4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, compact_text, flags=re.IGNORECASE)
        if match:
            return _normalize_due_date_value(match.group(1))
    return None


def extract_due_date_from_pages(pages: list[dict[str, Any]]) -> str | None:
    """Extract due date using line-level page tokens."""
    for page in pages:
        lines = group_words_into_lines(page.get("words") or [])
        for line_index, line_words in enumerate(lines):
            tokens = [normalize_token(str(item.get("text", ""))) for item in line_words]
            upper = [token.upper() for token in tokens]
            joined = clean_space(" ".join(tokens))

            if "DUE" in upper and "DATE" in upper:
                inline = re.search(r"\d{2}[/-]\d{2}[/-]\d{4}", joined)
                if inline:
                    return _normalize_due_date_value(inline.group(0))
                month_fmt = re.search(r"\d{1,2}\s+[A-Za-z]{3,9},?\s+\d{4}", joined)
                if month_fmt:
                    return _normalize_due_date_value(month_fmt.group(0))

                if line_index + 1 < len(lines):
                    next_tokens = [
                        normalize_token(str(item.get("text", "")))
                        for item in lines[line_index + 1]
                    ]
                    next_joined = clean_space(" ".join(next_tokens))
                    next_inline = re.search(r"\d{2}[/-]\d{2}[/-]\d{4}", next_joined)
                    if next_inline:
                        return _normalize_due_date_value(next_inline.group(0))
                    next_month_fmt = re.search(
                        r"\d{1,2}\s+[A-Za-z]{3,9},?\s+\d{4}",
                        next_joined,
                    )
                    if next_month_fmt:
                        return _normalize_due_date_value(next_month_fmt.group(0))
    return None


def extract_total_amount_due(full_text: str) -> str | None:
    """Extract statement-level total amount due from summary area."""
    upper_text = full_text.upper()
    start = upper_text.find("TOTAL AMOUNT DUE")
    if start == -1:
        return None

    end = upper_text.find("TOTAL CREDIT LIMIT", start)
    segment = full_text[start:end] if end != -1 else full_text[start : start + 1200]

    for pattern in [r"C\s*\d[\d,]*\.\d{2}", r"`\s*\d[\d,]*\.\d{2}", r"\d[\d,]*\.\d{2}"]:
        match = re.search(pattern, segment)
        if match:
            return normalize_amount(match.group(0).replace("C", "").strip())

    return None


def extract_statement_summary(full_text: str) -> StatementSummary:
    """Extract summary block amount candidates and heuristic field mapping."""
    upper_text = full_text.upper()
    start = upper_text.find("PAYMENTS/CREDITS")
    end = upper_text.find("TOTAL CREDIT LIMIT", start if start != -1 else 0)

    if start == -1:
        segment = full_text[:2000]
    else:
        segment = full_text[start : end if end != -1 else start + 2000]

    raw_amounts = [
        normalize_amount(match.group(0).replace("C", "").replace("`", "").strip())
        for match in re.finditer(r"[C`]?\s*\d[\d,]*\.\d{2}", segment)
    ]

    unique_amounts: list[str] = []
    for value in raw_amounts:
        if value not in unique_amounts:
            unique_amounts.append(value)

    summary = StatementSummary(summary_amount_candidates=unique_amounts)

    if len(unique_amounts) >= 5:
        summary.payments_credits_received = unique_amounts[0]
        summary.previous_statement_dues = unique_amounts[1]
        summary.purchases_debit = unique_amounts[2]
        summary.finance_charges = unique_amounts[3]
        summary.equation_tail = unique_amounts[4]

    return summary


__all__ = [
    "extract_due_date",
    "extract_due_date_from_pages",
    "extract_name",
    "extract_statement_summary",
    "extract_total_amount_due",
]
