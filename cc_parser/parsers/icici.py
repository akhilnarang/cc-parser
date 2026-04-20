"""ICICI parser profile.

Builds on generic parsing and applies ICICI-specific normalization,
especially for add-on card grouping and person labeling.
"""

import re
from typing import Any

from cc_parser.parsers.generic import GenericParser
from cc_parser.parsers.models import ParsedStatement, StatementSummary
from cc_parser.parsers.tokens import format_amount, normalize_amount, sum_amounts
from cc_parser.parsers.summary.grouping import (
    build_card_summaries,
    group_transactions_by_person,
)


_BREAKDOWN_HEADER = re.compile(
    r"Previous Balance\s+Purchases\s*/\s*Charges\s+Cash Advances\s+Payments\s*/\s*Credits",
    re.IGNORECASE,
)
_AMOUNT_RE = re.compile(r"`?\s*([\d,]+\.\d{2})")


INVALID_PERSON_KEYWORDS = {
    "PLACE OF SUPPLY",
    "POINTS AMOUNT",
    "TRAVEL APPAREL",
    "FUEL OTHERS",
    "IMPORTANT",
    "OUTSTANDING",
    "INTEREST",
    "MINIMUM AMOUNT",
    "STATEMENT",
    "SUMMARY",
    "MESSAGES",
}

# Common merchant/address suffixes that should not appear in person names
_MERCHANT_SUFFIXES = {"IN", "PVT", "LTD", "INC", "LLC", "CO", "CORP"}


def _looks_like_real_name(value: str | None) -> bool:
    """Validate whether a tokenized label resembles a real person name.

    Args:
        value: Candidate label string.

    Returns:
        True when the value looks like a human name; otherwise False.
    """
    if not value:
        return False
    name = " ".join(value.split()).upper()
    if len(name) < 5:
        return False
    if any(keyword in name for keyword in INVALID_PERSON_KEYWORDS):
        return False
    parts = name.split()
    if not (2 <= len(parts) <= 4):
        return False
    # Reject names containing merchant/address suffixes
    if any(part in _MERCHANT_SUFFIXES for part in parts):
        return False
    # Every word must be at least 3 characters (rejects fragments like "IN")
    if any(len(part) < 3 for part in parts):
        return False
    return all(re.fullmatch(r"[A-Z][A-Z.'-]*", part) for part in parts)


class IciciParser(GenericParser):
    """Parser entrypoint for ICICI statements."""

    bank = "icici"

    def _extract_summary(
        self, full_text: str, pages: list[dict[str, Any]]
    ) -> StatementSummary:
        # ICICI's top "Statement Summary" block lists Total Due, Min Due, and
        # credit-limit figures — the generic heuristic misreads credit-limit
        # amounts as finance charges. The real breakdown lives in a row
        # labeled "Previous Balance | Purchases/Charges | Cash Advances |
        # Payments/Credits" near the bottom of page 1.
        match = _BREAKDOWN_HEADER.search(full_text)
        if not match:
            return StatementSummary()
        tail = full_text[match.end() : match.end() + 400]
        amounts = _AMOUNT_RE.findall(tail)
        if len(amounts) < 4:
            return StatementSummary()
        prev, purchases, _cash, credits = (normalize_amount(a) for a in amounts[:4])
        return StatementSummary(
            summary_amount_candidates=[prev, purchases, amounts[2], credits],
            previous_statement_dues=prev,
            purchases_debit=purchases,
            payments_credits_received=credits,
        )

    def parse(self, raw_data: dict[str, Any]) -> ParsedStatement:
        """Parse ICICI statements with add-on specific normalization.

        Args:
            raw_data: Raw extraction payload from extractor.

        Returns:
            Normalized statement output with ICICI-specific person grouping.
        """
        parsed = super().parse(raw_data)
        parsed.bank = self.bank

        debit_transactions = parsed.transactions
        credit_transactions = parsed.payments_refunds
        all_rows = [*debit_transactions, *credit_transactions]

        primary_name = (parsed.name or "").upper() or None
        primary_card = parsed.card_number

        card_order: list[str] = []
        for txn in all_rows:
            card = txn.card_number or "UNKNOWN"
            if card not in card_order:
                card_order.append(card)

        card_person_map: dict[str, str] = {}
        for card in card_order:
            if primary_card and card == primary_card and primary_name:
                card_person_map[card] = primary_name
                continue

            candidate_names = [
                (txn.person or "").upper()
                for txn in all_rows
                if (txn.card_number or "UNKNOWN") == card
                and _looks_like_real_name(txn.person)
            ]
            if candidate_names:
                card_person_map[card] = candidate_names[0]
            else:
                card_person_map[card] = (
                    f"ADDON {card[-4:]}" if len(card) >= 4 else "ADDON"
                )

        for txn in all_rows:
            card = txn.card_number or "UNKNOWN"
            txn.person = card_person_map.get(card, txn.person or "UNKNOWN")

        card_summaries, overall_total = build_card_summaries(
            debit_transactions, primary_name
        )
        person_groups = group_transactions_by_person(debit_transactions, primary_name)

        credit_total = sum_amounts(credit_transactions)

        parsed.card_summaries = card_summaries
        parsed.overall_total = overall_total
        parsed.person_groups = person_groups
        parsed.payments_refunds_total = format_amount(credit_total)

        return parsed
