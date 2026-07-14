"""Generic statement parser.

This module provides the ``GenericParser`` implementation used as the
default (and base class for bank-specific profiles). The heavy-lifting
helpers now live in dedicated sibling modules:

- ``tokens``             – regex constants, token parsing, amount/point helpers
- ``cards``              – card number detection, masking, member-header logic
- ``narration``          – narration cleaning, merging, enrichment
- ``extraction``         – line reconstruction, transaction extraction
- ``summary``            – due date / totals / grouping / reconciliation helpers
- ``adjustment_pairing`` – refund and reversal detection helpers
"""

from typing import Any

from cc_parser.parsers.adjustment_pairing import detect_adjustment_pairs
from cc_parser.parsers.base import StatementParser
from cc_parser.parsers.cards import (
    extract_card_from_filename,
    extract_card_number,
    find_card_candidates,
    normalize_transaction_persons,
    split_by_transaction_type,
)
from cc_parser.parsers.extraction import (
    _extract_transactions_with_debug as _extract_transactions_with_debug_impl,
)
from cc_parser.parsers.extraction import (
    classify_credit_transaction,
    group_words_into_lines,
)
from cc_parser.parsers.models import ParsedStatement, StatementSummary, Transaction
from cc_parser.parsers.summary import (
    _DUE_DATE_PAGE_LIMIT,
    build_card_summaries,
    build_reconciliation,
    extract_due_date,
    extract_due_date_from_pages,
    extract_name,
    extract_statement_summary,
    extract_total_amount_due,
    group_transactions_by_person,
)
from cc_parser.parsers.tokens import (
    clean_space,
    format_amount,
    normalize_amount,
    normalize_token,
    parse_amount_token,
    parse_date,
    sum_amounts,
    sum_points,
)
from cc_parser.parsers.transaction_id_generator import assign_transaction_ids


class GenericParser(StatementParser):
    """Default parser implementation shared by bank profiles."""

    bank = "generic"

    # Whether to drop words rendered left of the description column (inline
    # action controls sharing a transaction's baseline). The column is read off
    # the page's own table header, so profiles opt in once that header and the
    # lanes around it are known; leaving it off preserves prior behavior.
    strip_action_lane = False

    def __init__(self) -> None:
        self._last_txn_debug: dict[str, Any] | None = None
        self._last_transactions: list[Transaction] | None = None

    def _extract_name(self, full_text: str, pages: list[dict[str, Any]]) -> str | None:
        return extract_name(full_text)

    def _extract_card_number(
        self, full_text: str, pages: list[dict[str, Any]], file_name: str
    ) -> str | None:
        return extract_card_number(full_text) or extract_card_from_filename(file_name)

    def _extract_transactions_with_debug(
        self, pages: list[dict[str, Any]]
    ) -> tuple[list[Transaction], dict[str, Any]]:
        return _extract_transactions_with_debug_impl(pages, self.strip_action_lane)

    def _normalize_transactions(
        self,
        transactions: list[Transaction],
        name: str | None,
        card_number: str | None,
    ) -> None:
        if card_number:
            for txn in transactions:
                if not txn.card_number:
                    txn.card_number = card_number
        normalize_transaction_persons(transactions, name)

    def _extract_due_date(
        self, full_text: str, pages: list[dict[str, Any]]
    ) -> str | None:
        # Why: page-layout extraction is anchored to the actual header position,
        # so it cannot drift onto illustrative dates in the back of the PDF
        # (e.g., ICICI's "Payment due date - Oct 26, 2023" interest example).
        # The regex fallback is also bounded to early pages so it cannot match
        # those same examples when the page-aware path returns nothing.
        if found := extract_due_date_from_pages(pages):
            return found
        early_text = "\n".join(
            str(page.get("text", "")) for page in pages[:_DUE_DATE_PAGE_LIMIT]
        )
        return extract_due_date(early_text or full_text)

    def _extract_total_amount_due(
        self, full_text: str, pages: list[dict[str, Any]]
    ) -> str | None:
        return extract_total_amount_due(full_text)

    def _extract_summary(
        self, full_text: str, pages: list[dict[str, Any]]
    ) -> StatementSummary:
        return extract_statement_summary(full_text)

    def _extract_transaction_lines(
        self, words: list[dict[str, Any]]
    ) -> list[list[dict[str, Any]]]:
        return group_words_into_lines(words)

    def _parse_date(
        self, token: str, context: dict[str, Any] | None = None
    ) -> str | None:
        return parse_date(token)

    def _parse_amount(
        self, token: str, context: dict[str, Any] | None = None
    ) -> str | None:
        parsed = parse_amount_token(token)
        return normalize_amount(parsed) if parsed else None

    def _classify_credit_debit(
        self, tokens: list[str], context: dict[str, Any] | None = None
    ) -> tuple[bool, list[str]]:
        return classify_credit_transaction(tokens)

    def parse(self, raw_data: dict[str, Any]) -> ParsedStatement:
        """Normalize raw extractor payload into compact statement output."""
        pages = raw_data.get("pages", [])
        full_text = "\n".join(str(page.get("text", "")) for page in pages)
        name = self._extract_name(full_text, pages)
        transactions, txn_debug = self._extract_transactions_with_debug(pages)
        self._last_txn_debug = txn_debug
        self._last_transactions = transactions

        detected_card = self._extract_card_number(
            full_text, pages, str(raw_data["file"])
        )
        self._normalize_transactions(transactions, name, detected_card)

        debit_transactions, credit_transactions = split_by_transaction_type(
            transactions
        )
        debit_transactions = assign_transaction_ids(debit_transactions, self.bank)
        credit_transactions = assign_transaction_ids(credit_transactions, self.bank)

        adjustment_pairs = detect_adjustment_pairs(
            debit_transactions, credit_transactions, self.bank
        )

        card_summaries, overall_total = build_card_summaries(debit_transactions, name)
        person_groups = group_transactions_by_person(debit_transactions, name)

        credit_total = sum_amounts(credit_transactions)
        overall_reward_points = sum_points(debit_transactions)

        due_date = self._extract_due_date(full_text, pages)
        statement_total_amount_due = self._extract_total_amount_due(full_text, pages)
        summary_fields = self._extract_summary(full_text, pages)
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
            possible_adjustment_pairs=adjustment_pairs,
            overall_reward_points=str(int(overall_reward_points)),
            transactions=debit_transactions,
            reconciliation=reconciliation,
        )

    def build_debug(self, raw_data: dict[str, Any]) -> dict[str, Any]:
        """Build detailed parser diagnostics for troubleshooting mode."""
        pages = raw_data.get("pages", [])

        if self._last_txn_debug is not None and self._last_transactions is not None:
            txn_debug = self._last_txn_debug
            transactions = self._last_transactions
        else:
            transactions, txn_debug = self._extract_transactions_with_debug(pages)

        interesting_lines: list[dict[str, Any]] = []
        section_markers: list[dict[str, Any]] = []
        card_candidates: list[dict[str, Any]] = []

        for page in pages:
            page_number = int(page.get("page_number", 0) or 0)
            text = str(page.get("text", ""))
            for card in find_card_candidates(text):
                card_candidates.append({"page": page_number, "card": card})

            lines = self._extract_transaction_lines(page.get("words") or [])
            for idx, line_words in enumerate(lines[:800]):
                tokens = [
                    normalize_token(str(item.get("text", ""))) for item in line_words
                ]
                joined = clean_space(" ".join(tokens))
                if not joined:
                    continue

                has_date = any(self._parse_date(token) for token in tokens)
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
