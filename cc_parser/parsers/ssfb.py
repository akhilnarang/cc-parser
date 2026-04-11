"""Suryoday Small Finance Bank (SSFB) credit card parser profile.

This parser focuses on SSFB statement-summary extraction and avoids
pollution from informational MITC/example pages.
"""

from __future__ import annotations

import re
from typing import Any

from cc_parser.parsers.base import StatementParser
from cc_parser.parsers.cards import (
    extract_card_from_filename,
    find_card_candidates,
    normalize_transaction_persons,
    split_by_transaction_type,
)
from cc_parser.parsers.models import ParsedStatement, StatementSummary, Transaction
from cc_parser.parsers.reconciliation import (
    build_card_summaries,
    build_reconciliation,
    detect_adjustment_pairs,
    group_transactions_by_person,
)
from cc_parser.parsers.tokens import (
    format_amount,
    parse_amount,
    sum_amounts,
    sum_points,
)
from cc_parser.parsers.transaction_id_generator import assign_transaction_ids


_MONTHS = {
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


def _normalize_dd_mon_yyyy(value: str) -> str | None:
    """Convert ``DD-Mon-YYYY`` to ``DD/MM/YYYY``."""
    match = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", value.strip())
    if not match:
        return None
    day, mon, year = match.groups()
    month = _MONTHS.get(mon.upper()[:3])
    if not month:
        return None
    return f"{day.zfill(2)}/{month}/{year}"


def _extract_name(first_page_text: str) -> str | None:
    """Extract cardholder name near the top of page 1."""
    match = re.search(r"\n([A-Z][A-Z ]{3,})\nEmail\s*:", first_page_text)
    if not match:
        return None
    candidate = " ".join(match.group(1).split()).strip().upper()
    parts = candidate.split()
    if 2 <= len(parts) <= 6:
        return candidate
    return None


def _extract_due_date(first_page_text: str) -> str | None:
    """Extract payment due date from account summary header."""
    match = re.search(
        r"Payment\s+Due\s+Date\s*:\s*(\d{1,2}-[A-Za-z]{3}-\d{4})",
        first_page_text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return _normalize_dd_mon_yyyy(match.group(1))


def _extract_total_amount_due(first_page_text: str) -> str | None:
    """Extract statement total amount due from the summary row."""
    # Layout:
    # Statement Date Total Amount Due Minimum Amount Due
    # 15-Mar-2026 ₹ -0.01 ₹ 0.00
    block = re.search(
        r"Statement\s+Date\s+Total\s+Amount\s+Due\s+Minimum\s+Amount\s+Due\s*\n([^\n]+)",
        first_page_text,
        flags=re.IGNORECASE,
    )
    if block:
        amounts = re.findall(r"[-+]?\d[\d,]*\.\d{2}", block.group(1))
        if amounts:
            return amounts[0]

    # Fallback: first amount after "Total Amount Due"
    fallback = re.search(
        r"Total\s+Amount\s+Due[^\n]*\n[^\n]*?([-+]?\d[\d,]*\.\d{2})",
        first_page_text,
        flags=re.IGNORECASE,
    )
    return fallback.group(1) if fallback else None


def _extract_summary(first_page_text: str) -> StatementSummary:
    """Extract SSFB account-summary fields used for reconciliation."""
    opening = "0.00"
    purchases = "0.00"
    fees_adjustments = "0.00"
    cash_advance = "0.00"
    gst = "0.00"
    payments_reversals = "0.00"

    block1 = re.search(
        r"Opening\s*\+\s*Purchases\s*\+\s*Fees\s*,Adjustments\s*\+\s*Cash\s+Advance\s*\+\s*"
        r"\nBalance\s*&\s*Other\s+Charges\s*\n([^\n]+)",
        first_page_text,
        flags=re.IGNORECASE,
    )
    if block1:
        amounts = re.findall(r"[-+]?\d[\d,]*\.\d{2}", block1.group(1))
        if len(amounts) >= 4:
            opening, purchases, fees_adjustments, cash_advance = amounts[:4]

    block2 = re.search(
        r"GST\s*-\s*Payments,\s*Reversals\s*&\s*Other\s+credits\s*=\s*Closing\s+Balance\s*\n([^\n]+)",
        first_page_text,
        flags=re.IGNORECASE,
    )
    if block2:
        amounts = re.findall(r"[-+]?\d[\d,]*\.\d{2}", block2.group(1))
        if len(amounts) >= 3:
            gst, payments_reversals, _closing = amounts[:3]

    finance_total = (
        parse_amount(fees_adjustments) + parse_amount(cash_advance) + parse_amount(gst)
    )

    candidates = [
        opening,
        purchases,
        fees_adjustments,
        cash_advance,
        gst,
        payments_reversals,
    ]
    return StatementSummary(
        summary_amount_candidates=[c for c in candidates if c],
        previous_statement_dues=opening,
        purchases_debit=purchases,
        finance_charges=format_amount(finance_total),
        payments_credits_received=payments_reversals,
    )


def _extract_card_number(full_text: str, file_path: str) -> str | None:
    """Extract masked card number if present, otherwise fallback to filename."""
    candidates = find_card_candidates(full_text)
    if candidates:
        return candidates[0]
    return extract_card_from_filename(file_path)


class SsfbParser(StatementParser):
    """Parser entrypoint for SSFB credit card statements."""

    bank = "ssfb"

    def parse(self, raw_data: dict[str, Any]) -> ParsedStatement:
        pages = raw_data.get("pages", [])
        first_page_text = str((pages[0] if pages else {}).get("text", ""))
        full_text = "\n".join(str(page.get("text", "")) for page in pages)

        name = _extract_name(first_page_text)
        card_number = _extract_card_number(full_text, str(raw_data.get("file", "")))
        due_date = _extract_due_date(first_page_text)
        statement_total_amount_due = _extract_total_amount_due(first_page_text)
        summary_fields = _extract_summary(first_page_text)

        # SSFB sample statements may not contain a transaction table in extracted text.
        transactions: list[Transaction] = []

        normalize_transaction_persons(transactions, name)
        debit_transactions, credit_transactions = split_by_transaction_type(
            transactions
        )

        debit_transactions = assign_transaction_ids(debit_transactions, self.bank)
        credit_transactions = assign_transaction_ids(credit_transactions, self.bank)

        adjustment_pairs = detect_adjustment_pairs(
            debit_transactions,
            credit_transactions,
            self.bank,
        )

        card_summaries, overall_total = build_card_summaries(debit_transactions, name)
        person_groups = group_transactions_by_person(debit_transactions, name)
        credit_total = sum_amounts(credit_transactions)
        overall_reward_points = sum_points(debit_transactions)

        reconciliation = build_reconciliation(
            statement_total_amount_due,
            debit_transactions,
            credit_transactions,
            summary_fields,
        )

        return ParsedStatement(
            file=raw_data["file"],
            bank=self.bank,
            name=name,
            card_number=card_number,
            due_date=due_date,
            statement_total_amount_due=statement_total_amount_due,
            card_summaries=card_summaries,
            overall_total=overall_total,
            person_groups=person_groups,
            payments_refunds=credit_transactions,
            payments_refunds_total=format_amount(credit_total),
            possible_adjustment_pairs=adjustment_pairs,
            overall_reward_points=str(int(overall_reward_points)),
            transactions=debit_transactions,
            reconciliation=reconciliation,
        )
