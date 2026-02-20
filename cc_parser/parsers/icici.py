"""ICICI parser profile.

Builds on generic parsing and applies ICICI-specific normalization,
especially for add-on card grouping and person labeling.
"""

from decimal import Decimal
import re
from typing import Any

from cc_parser.parsers.generic import (
    GenericParser,
    build_card_summaries,
    format_amount,
    group_transactions_by_person,
    parse_amount,
)


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
    return all(re.fullmatch(r"[A-Z][A-Z.'-]*", part) for part in parts)


class IciciParser(GenericParser):
    """Parser entrypoint for ICICI statements."""

    bank = "icici"

    def parse(self, raw_data: dict[str, Any]) -> dict[str, Any]:
        """Parse ICICI statements with add-on specific normalization.

        Args:
            raw_data: Raw extraction payload from extractor.

        Returns:
            Normalized statement output with ICICI-specific person grouping.
        """
        parsed = super().parse(raw_data)
        parsed["bank"] = self.bank

        debit_transactions = parsed.get("transactions", [])
        credit_transactions = parsed.get("payments_refunds", [])
        all_rows = [*debit_transactions, *credit_transactions]

        primary_name = str(parsed.get("name") or "").upper() or None
        primary_card = parsed.get("card_number")

        card_order: list[str] = []
        for txn in all_rows:
            card = str(txn.get("card_number") or "UNKNOWN")
            if card not in card_order:
                card_order.append(card)

        card_person_map: dict[str, str] = {}
        for card in card_order:
            if primary_card and card == primary_card and primary_name:
                card_person_map[card] = primary_name
                continue

            candidate_names = [
                str(txn.get("person") or "").upper()
                for txn in all_rows
                if str(txn.get("card_number") or "UNKNOWN") == card
                and _looks_like_real_name(str(txn.get("person") or ""))
            ]
            if candidate_names:
                card_person_map[card] = candidate_names[0]
            else:
                card_person_map[card] = (
                    f"ADDON {card[-4:]}" if len(card) >= 4 else "ADDON"
                )

        for txn in all_rows:
            card = str(txn.get("card_number") or "UNKNOWN")
            txn["person"] = card_person_map.get(
                card, str(txn.get("person") or "UNKNOWN")
            )

        card_summaries, overall_total = build_card_summaries(
            debit_transactions, primary_name
        )
        person_groups = group_transactions_by_person(debit_transactions, primary_name)

        credit_total = Decimal("0")
        for txn in credit_transactions:
            credit_total += parse_amount(str(txn.get("amount") or "0"))

        parsed["card_summaries"] = card_summaries
        parsed["overall_total"] = overall_total
        parsed["person_groups"] = person_groups
        parsed["payments_refunds_total"] = format_amount(credit_total)

        return parsed
