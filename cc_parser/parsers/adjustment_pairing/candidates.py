"""Candidate generation and filtering for adjustment pair detection."""

import re

from cc_parser.parsers.adjustment_pairing.constants import (
    PAYMENT_KEYWORDS,
    REFUND_KEYWORDS,
)
from cc_parser.parsers.models import Transaction
from cc_parser.parsers.tokens import parse_amount

_PAYMENT_PATTERNS = [
    re.compile(r"\b" + re.escape(kw) + r"\b") for kw in PAYMENT_KEYWORDS
]
_REFUND_PATTERNS = [re.compile(r"\b" + re.escape(kw) + r"\b") for kw in REFUND_KEYWORDS]


def is_normal_payment_credit(transaction: Transaction) -> bool:
    """Check if a credit transaction is a normal payment (should be excluded)."""
    narration = (transaction.narration or "").upper()
    return any(pattern.search(narration) for pattern in _PAYMENT_PATTERNS)


def has_refund_keyword(transaction: Transaction) -> bool:
    """Check if transaction narration contains refund/reversal keywords."""
    narration = (transaction.narration or "").upper()
    return any(pattern.search(narration) for pattern in _REFUND_PATTERNS)


def is_malformed(transaction: Transaction) -> bool:
    """Check if transaction lacks required basic fields."""
    if not transaction.amount:
        return True
    if parse_amount(transaction.amount) == 0:
        return True
    if not transaction.date:
        return True
    return False


def should_hard_reject(
    debit: Transaction, credit: Transaction
) -> tuple[bool, str | None]:
    """Check if a candidate pair should be hard-rejected."""
    if debit.transaction_type == credit.transaction_type:
        return True, "same_transaction_type"

    if (
        debit.card_number
        and credit.card_number
        and debit.card_number != credit.card_number
    ):
        return True, "card_conflict"

    if is_normal_payment_credit(credit):
        return True, "normal_payment_credit"

    if is_malformed(debit) or is_malformed(credit):
        return True, "malformed_transaction"

    return False, None


def should_early_prune(debit: Transaction, credit: Transaction) -> bool:
    """Check if candidate should be pruned early to reduce scoring overhead."""
    try:
        debit_amt = parse_amount(debit.amount or "0")
        credit_amt = parse_amount(credit.amount or "0")

        if debit_amt == 0 or credit_amt == 0:
            return True

        delta = abs(debit_amt - credit_amt)
        delta_pct = (delta / debit_amt) * 100

        if delta_pct > 50:
            has_card_match = (
                debit.card_number
                and credit.card_number
                and debit.card_number == credit.card_number
            )
            has_keyword = has_refund_keyword(credit)

            if not has_card_match and not has_keyword:
                return True

        return False

    except ValueError, TypeError, ArithmeticError:
        return False


def generate_candidate_pairs(
    debit_transactions: list[Transaction],
    credit_transactions: list[Transaction],
) -> list[tuple[Transaction, Transaction]]:
    """Generate all candidate debit × credit pairs with filtering."""
    candidates: list[tuple[Transaction, Transaction]] = []

    for debit in debit_transactions:
        for credit in credit_transactions:
            should_reject, _reason = should_hard_reject(debit, credit)
            if should_reject:
                continue
            if should_early_prune(debit, credit):
                continue
            candidates.append((debit, credit))

    return candidates


__all__ = [
    "generate_candidate_pairs",
    "has_refund_keyword",
    "is_malformed",
    "is_normal_payment_credit",
    "should_early_prune",
    "should_hard_reject",
]
