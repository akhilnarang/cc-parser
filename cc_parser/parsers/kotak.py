"""Kotak Mahindra Bank credit card parser profile.

This parser focuses on Kotak statement extraction with DD-Mon-YYYY dates,
TAD/MAD summary fields, and transaction parsing between section headers.
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
from cc_parser.parsers.adjustment_pairing import detect_adjustment_pairs
from cc_parser.parsers.summary.grouping import (
    build_card_summaries,
    group_transactions_by_person,
)
from cc_parser.parsers.summary.reconciliation import build_reconciliation
from cc_parser.parsers.tokens import (
    format_amount,
    normalize_amount,
    parse_amount,
    parse_amount_token,
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
    # Format: "Ansuman Mishra Monthly statement for your League Credit Card X3188"
    match = re.search(
        r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+Monthly\s+statement",
        first_page_text,
        re.MULTILINE,
    )
    if not match:
        return None
    candidate = " ".join(match.group(1).split()).strip().upper()
    parts = candidate.split()
    if 2 <= len(parts) <= 6:
        return candidate
    return None


def _extract_card_number(full_text: str) -> str | None:
    """Extract masked card number from text or filename."""
    # Pattern: "Credit Card X3188" or "Primary Card X3188"
    match = re.search(r"(?:Credit\s+Card|Primary\s+Card)\s+(X\d+)", full_text, re.IGNORECASE)
    if match:
        raw = match.group(1).strip().upper()
        # Pad to 16 chars and format: X3188 -> XXXX XXXX XXXX 3188
        if len(raw) < 16:
            raw = "X" * (16 - len(raw)) + raw
        return f"{raw[:4]} {raw[4:8]} {raw[8:12]} {raw[12:]}"

    candidates = find_card_candidates(full_text)
    if candidates:
        return candidates[0]
    return None


def _extract_due_date(first_page_text: str) -> str | None:
    """Extract payment due date from first page."""
    # Format: "Due Date: 08-May-2026"
    match = re.search(
        r"Due\s+Date\s*[:\-]?\s*(\d{1,2}-[A-Za-z]{3}-\d{4})",
        first_page_text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return _normalize_dd_mon_yyyy(match.group(1))


def _extract_total_amount_due(first_page_text: str) -> str | None:
    """Extract statement total amount due (TAD) from first page."""
    # Format: "Total Amount Due (TAD) Rs. 4,186.50"
    match = re.search(
        r"Total\s+Amount\s+Due\s*\(\s*TAD\s*\)\s*Rs\.?\s*([\d,]+\.\d{2})",
        first_page_text,
        flags=re.IGNORECASE,
    )
    if match:
        return normalize_amount(match.group(1))

    # Fallback: find first amount after "Total Amount Due"
    fallback = re.search(
        r"Total\s+Amount\s+Due[^\n]*?([\d,]+\.\d{2})",
        first_page_text,
        flags=re.IGNORECASE,
    )
    if fallback:
        return normalize_amount(fallback.group(1))
    return None


def _extract_summary(first_page_text: str) -> StatementSummary:
    """Extract Kotak account-summary fields used for reconciliation."""
    previous_statement_dues = "0.00"
    purchases_debit = "0.00"
    fees_adjustments = "0.00"
    payments_credits = "0.00"

    # Summary table lines:
    # Previous statement dues 0.00
    # Purchases made in this cycle 4,186.50
    # Other fees & charges 0.00
    # Payments and Other Credits 0.00
    # Total Amount Due 4,186.50

    prev_match = re.search(
        r"Previous\s+statement\s+dues?\s+([\d,]+\.\d{2})",
        first_page_text,
        flags=re.IGNORECASE,
    )
    if prev_match:
        previous_statement_dues = normalize_amount(prev_match.group(1))

    purch_match = re.search(
        r"Purchases\s+made\s+in\s+this\s+cycle\s+([\d,]+\.\d{2})",
        first_page_text,
        flags=re.IGNORECASE,
    )
    if purch_match:
        purchases_debit = normalize_amount(purch_match.group(1))

    fees_match = re.search(
        r"Other\s+fees?\s*&\s*charges?\s+([\d,]+\.\d{2})",
        first_page_text,
        flags=re.IGNORECASE,
    )
    if fees_match:
        fees_adjustments = normalize_amount(fees_match.group(1))

    pay_match = re.search(
        r"Payments\s+and\s+Other\s+Credits?\s+([\d,]+\.\d{2})",
        first_page_text,
        flags=re.IGNORECASE,
    )
    if pay_match:
        payments_credits = normalize_amount(pay_match.group(1))

    candidates = [
        previous_statement_dues,
        purchases_debit,
        fees_adjustments,
        payments_credits,
    ]
    return StatementSummary(
        summary_amount_candidates=[c for c in candidates if c],
        previous_statement_dues=previous_statement_dues,
        purchases_debit=purchases_debit,
        finance_charges=fees_adjustments,
        payments_credits_received=payments_credits,
    )


def _extract_transactions(pages: list[dict[str, Any]]) -> list[Transaction]:
    """Extract transactions from Kotak statement pages.

    Sections:
    - "PURCHASES MADE IN THIS CYCLE" -> debits
    - "PAYMENTS AND OTHER CREDITS" -> credits
    """
    transactions: list[Transaction] = []
    current_section: str | None = None
    current_person: str | None = None

    # Skip headers/footers
    _SKIP_PATTERNS = [
        r"^TRANSACTIONS?\s+DETAILS?\s+FROM",
        r"^DATE\s+DESCRIPTION\s+SPENDS\s+CATEGORY",
        r"^TOTAL\s+PURCHASES",
        r"^TOTAL\s+CREDITS",
        r"^\s*Page\s+\d+\s+of\s+\d+",
        r"^\s*MITC",
        r"^\s*Terms?\s+and\s+Conditions",
        r"^\s*End\s+of\s+Statement",
    ]

    for page in pages:
        page_text = str(page.get("text", ""))
        for line in page_text.splitlines():
            upper = line.upper().strip()

            # Track section boundaries
            if "PURCHASES MADE IN THIS CYCLE" in upper:
                current_section = "debit"
                # Extract person from "Purchases made in this cycle - Primary Card X3188"
                person_match = re.search(
                    r"Primary\s+Card\s+(X\d+)",
                    line,
                    flags=re.IGNORECASE,
                )
                if person_match:
                    current_person = f"PRIMARY {person_match.group(1)}"
                else:
                    current_person = None
                continue

            if "PAYMENTS AND OTHER CREDITS" in upper:
                current_section = "credit"
                current_person = None
                continue

            # Skip non-transaction lines
            if any(re.search(p, upper) for p in _SKIP_PATTERNS):
                continue

            if not current_section:
                continue

            # Parse transaction line: "DD-Mon-YYYY MERCHANT CITY IN CATEGORY AMOUNT"
            # e.g. "26-Mar-2026 BLINKIT GURGAON IN Grocery 912.50"
            tokens = line.strip().split()
            if len(tokens) < 4:
                continue

            # Try to find a date token at start
            date_token = None
            date_idx = None
            for i, tok in enumerate(tokens[:4]):
                normalized = _normalize_dd_mon_yyyy(tok)
                if normalized:
                    date_token = normalized
                    date_idx = i
                    break

            if not date_token:
                continue

            # Find amount at end
            amount_str = None
            for i in range(len(tokens) - 1, date_idx, -1):
                amt = parse_amount_token(tokens[i])
                if amt:
                    amount_str = amt
                    break

            if not amount_str:
                continue

            # Narration = tokens between date and amount
            narration_tokens = tokens[date_idx + 1 : len(tokens) - 1]
            narration = " ".join(narration_tokens)

            # Strip trailing "IN <category>" pattern
            narration = re.sub(r"\s+IN\s+[A-Z]+$", "", narration, flags=re.IGNORECASE)
            narration = narration.strip()

            if not narration:
                continue

            txn_type = "debit" if current_section == "debit" else "credit"

            transactions.append(
                Transaction(
                    date=date_token,
                    time="",
                    person=current_person,
                    narration=narration,
                    amount=normalize_amount(amount_str),
                    transaction_type=txn_type,
                    card_number=None,
                    reward_points=None,
                )
            )

    return transactions


class KotakParser(StatementParser):
    """Parser entrypoint for Kotak Mahindra Bank credit card statements."""

    bank = "kotak"

    def parse(self, raw_data: dict[str, Any]) -> ParsedStatement:
        pages = raw_data.get("pages", [])
        first_page_text = str((pages[0] if pages else {}).get("text", ""))
        full_text = "\n".join(str(page.get("text", "")) for page in pages)

        name = _extract_name(first_page_text)
        card_number = _extract_card_number(full_text) or extract_card_from_filename(str(raw_data.get("file", "")))
        due_date = _extract_due_date(first_page_text)
        statement_total_amount_due = _extract_total_amount_due(first_page_text)
        summary_fields = _extract_summary(first_page_text)

        transactions = _extract_transactions(pages)

        # Fill in card number for transactions missing it
        if card_number:
            for txn in transactions:
                if not txn.card_number:
                    txn.card_number = card_number

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

    def build_debug(self, raw_data: dict[str, Any]) -> dict[str, Any]:
        """Return debug diagnostics for development."""
        pages = raw_data.get("pages", [])
        first_page_text = str((pages[0] if pages else {}).get("text", ""))
        full_text = "\n".join(str(page.get("text", "")) for page in pages)

        return {
            "name": _extract_name(first_page_text),
            "card_number": _extract_card_number(full_text),
            "due_date": _extract_due_date(first_page_text),
            "total_amount_due": _extract_total_amount_due(first_page_text),
            "summary": _extract_summary(first_page_text).__dict__,
            "transaction_count": len(_extract_transactions(pages)),
        }