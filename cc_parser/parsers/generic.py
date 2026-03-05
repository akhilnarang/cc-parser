"""Generic statement parser.

This module provides the ``GenericParser`` implementation used as the
default (and base class for bank-specific profiles).  The heavy-lifting
helpers now live in dedicated sibling modules:

- ``tokens``        – regex constants, token parsing, amount/point helpers
- ``cards``         – card number detection, masking, member-header logic
- ``narration``     – narration cleaning, merging, enrichment
- ``extraction``    – line reconstruction, transaction extraction
- ``reconciliation``– summary extraction, reconciliation, grouping
"""

from decimal import Decimal
from typing import Any

from cc_parser.parsers.base import StatementParser
from cc_parser.parsers.models import ParsedStatement, Transaction

# Re-export public helpers so existing callers (icici.py, cli.py, etc.)
# that import from ``generic`` keep working.
from cc_parser.parsers.tokens import (  # noqa: F401
    clean_space,
    format_amount,
    normalize_amount,
    normalize_token,
    parse_amount,
    parse_amount_token,
    parse_date_token,
    parse_points,
    parse_time_token,
    sum_amounts,
    sum_points,
)
from cc_parser.parsers.cards import (  # noqa: F401
    extract_card_from_filename,
    extract_card_from_line,
    extract_card_number,
    find_card_candidates,
    is_invalid_person_label,
    looks_like_card_token,
    looks_like_member_header,
    mask_card_token,
    normalize_card_token,
)
from cc_parser.parsers.narration import (  # noqa: F401
    clean_narration_artifacts,
    collect_row_context_tokens,
    enrich_reference_only_narration,
    extract_continuation_narration,
    needs_context_merge,
)
from cc_parser.parsers.extraction import (  # noqa: F401
    classify_credit_transaction,
    extract_transactions,
    group_words_into_lines,
    _extract_transactions_with_debug,
)
from cc_parser.parsers.reconciliation import (  # noqa: F401
    build_card_summaries,
    build_reconciliation,
    extract_due_date,
    extract_due_date_from_pages,
    extract_name,
    extract_statement_summary,
    extract_total_amount_due,
    group_transactions_by_person,
    split_paired_adjustments,
)


class GenericParser(StatementParser):
    """Default parser implementation shared by bank profiles."""

    bank = "generic"

    def __init__(self) -> None:
        self._last_txn_debug: dict[str, Any] | None = None
        self._last_transactions: list[Transaction] | None = None

    def parse(self, raw_data: dict[str, Any]) -> ParsedStatement:
        """Normalize raw extractor payload into compact statement output."""
        full_text = "\n".join(
            str(page.get("text", "")) for page in raw_data.get("pages", [])
        )
        name = extract_name(full_text)
        transactions, txn_debug = _extract_transactions_with_debug(
            raw_data.get("pages", [])
        )
        self._last_txn_debug = txn_debug
        self._last_transactions = transactions

        detected_card = extract_card_number(full_text) or extract_card_from_filename(
            str(raw_data["file"])
        )
        if detected_card:
            for txn in transactions:
                if not txn.card_number:
                    txn.card_number = detected_card

        for txn in transactions:
            person_value = (txn.person or "").strip()
            if is_invalid_person_label(person_value):
                txn.person = name

        debit_transactions = [
            txn for txn in transactions if txn.transaction_type != "credit"
        ]
        credit_transactions = [
            txn for txn in transactions if txn.transaction_type == "credit"
        ]

        debit_transactions, credit_transactions, adjustments = split_paired_adjustments(
            debit_transactions,
            credit_transactions,
        )

        card_summaries, overall_total = build_card_summaries(debit_transactions, name)
        person_groups = group_transactions_by_person(debit_transactions, name)

        credit_total = sum_amounts(credit_transactions)
        overall_reward_points = sum_points(debit_transactions)

        adjustments_debit_total = Decimal("0")
        adjustments_credit_total = Decimal("0")
        for txn in adjustments:
            amount = parse_amount(str(txn.amount or "0"))
            if txn.adjustment_side == "debit":
                adjustments_debit_total += amount
            elif txn.adjustment_side == "credit":
                adjustments_credit_total += amount

        due_date = extract_due_date(full_text) or extract_due_date_from_pages(
            raw_data.get("pages", [])
        )
        statement_total_amount_due = extract_total_amount_due(full_text)
        summary_fields = extract_statement_summary(full_text)
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
            card_number=detected_card,
            due_date=due_date,
            statement_total_amount_due=statement_total_amount_due,
            card_summaries=card_summaries,
            overall_total=overall_total,
            person_groups=person_groups,
            payments_refunds=credit_transactions,
            payments_refunds_total=format_amount(credit_total),
            adjustments=adjustments,
            adjustments_debit_total=format_amount(adjustments_debit_total),
            adjustments_credit_total=format_amount(adjustments_credit_total),
            overall_reward_points=str(int(overall_reward_points)),
            transactions=debit_transactions,
            reconciliation=reconciliation,
        )

    def build_debug(self, raw_data: dict[str, Any]) -> dict[str, Any]:
        """Build detailed parser diagnostics for troubleshooting mode.

        Reuses cached transaction debug from the last ``parse()`` call
        when available, avoiding a redundant re-parse.
        """
        pages = raw_data.get("pages", [])

        if self._last_txn_debug is not None and self._last_transactions is not None:
            txn_debug = self._last_txn_debug
            transactions = self._last_transactions
        else:
            transactions, txn_debug = _extract_transactions_with_debug(pages)

        interesting_lines: list[dict[str, Any]] = []
        section_markers: list[dict[str, Any]] = []
        card_candidates: list[dict[str, Any]] = []

        for page in pages:
            page_number = int(page.get("page_number", 0) or 0)
            text = str(page.get("text", ""))
            for card in find_card_candidates(text):
                card_candidates.append({"page": page_number, "card": card})

            lines = group_words_into_lines(page.get("words") or [])
            for idx, line_words in enumerate(lines[:800]):
                tokens = [
                    normalize_token(str(item.get("text", ""))) for item in line_words
                ]
                joined = clean_space(" ".join(tokens))
                if not joined:
                    continue

                has_date = any(parse_date_token(token) for token in tokens)
                has_mask = any("X" in token.upper() or "*" in token for token in tokens)
                if has_date or has_mask:
                    interesting_lines.append(
                        {
                            "page": page_number,
                            "line_index": idx,
                            "tokens": tokens,
                            "text": joined,
                        }
                    )

                upper_joined = joined.upper()
                if any(
                    marker in upper_joined
                    for marker in [
                        "DOMESTIC TRANSACTIONS",
                        "INTERNATIONAL TRANSACTIONS",
                        "DUE DATE",
                        "CREDIT CARD NO",
                    ]
                ):
                    section_markers.append(
                        {
                            "page": page_number,
                            "line_index": idx,
                            "text": joined,
                        }
                    )

        return {
            "bank": self.bank,
            "stats": {
                "page_count": len(pages),
                "transactions_parsed": len(transactions),
                "credit_transactions": len(
                    [txn for txn in transactions if txn.transaction_type == "credit"]
                ),
                "date_lines_seen": len(txn_debug["date_lines"]),
                "date_lines_rejected": len(txn_debug["rejected_date_lines"]),
                "member_headers_detected": len(txn_debug["detected_members"]),
            },
            "card_from_filename": extract_card_from_filename(
                str(raw_data.get("file", ""))
            ),
            "card_candidates": card_candidates,
            "section_markers": section_markers,
            "detected_members": txn_debug["detected_members"],
            "date_lines": txn_debug["date_lines"],
            "rejected_date_lines": txn_debug["rejected_date_lines"],
            "interesting_lines": interesting_lines[:1000],
        }
