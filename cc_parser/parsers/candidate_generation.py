"""Candidate generation and filtering for adjustment pair detection."""

import re
from typing import Tuple

from cc_parser.parsers.models import Transaction
from cc_parser.parsers.tokens import parse_amount
from cc_parser.parsers.scoring_constants import (
    REFUND_KEYWORDS,
    PAYMENT_KEYWORDS,
)

# Pre-compile word-boundary patterns for payment keywords
_PAYMENT_PATTERNS = [
    re.compile(r"\b" + re.escape(kw) + r"\b") for kw in PAYMENT_KEYWORDS
]
_REFUND_PATTERNS = [re.compile(r"\b" + re.escape(kw) + r"\b") for kw in REFUND_KEYWORDS]


def is_normal_payment_credit(transaction: Transaction) -> bool:
    """Check if a credit transaction is a normal payment (should be excluded).

    Args:
        transaction: Credit transaction to check

    Returns:
        True if this is a normal payment, False otherwise
    """
    narration = (transaction.narration or "").upper()

    # Check for payment keywords using word-boundary matching
    for pattern in _PAYMENT_PATTERNS:
        if pattern.search(narration):
            return True

    return False


def has_refund_keyword(transaction: Transaction) -> bool:
    """Check if transaction narration contains refund/reversal keywords.

    Args:
        transaction: Transaction to check

    Returns:
        True if refund keyword found, False otherwise
    """
    narration = (transaction.narration or "").upper()
    for pattern in _REFUND_PATTERNS:
        if pattern.search(narration):
            return True
    return False


def is_malformed(transaction: Transaction) -> bool:
    """Check if transaction lacks required basic fields.

    Args:
        transaction: Transaction to check

    Returns:
        True if malformed, False otherwise
    """
    if not transaction.amount:
        return True
    if parse_amount(transaction.amount) == 0:
        return True
    if not transaction.date:
        return True
    return False


def should_hard_reject(
    debit: Transaction, credit: Transaction
) -> Tuple[bool, str | None]:
    """Check if a candidate pair should be hard-rejected.

    Args:
        debit: Debit transaction
        credit: Credit transaction

    Returns:
        (should_reject, reason) tuple
    """
    # Both same side (shouldn't happen in normal flow but check anyway)
    if debit.transaction_type == credit.transaction_type:
        return True, "same_transaction_type"

    # Card conflict
    if (
        debit.card_number
        and credit.card_number
        and debit.card_number != credit.card_number
    ):
        return True, "card_conflict"

    # Normal payment credit
    if is_normal_payment_credit(credit):
        return True, "normal_payment_credit"

    # Malformed transactions
    if is_malformed(debit) or is_malformed(credit):
        return True, "malformed_transaction"

    return False, None


def should_early_prune(debit: Transaction, credit: Transaction) -> bool:
    """Check if candidate should be pruned early to reduce scoring overhead.

    Early pruning criteria:
    - Large amount delta (> 50%) AND
    - No refund keyword AND
    - No card match

    Args:
        debit: Debit transaction
        credit: Credit transaction

    Returns:
        True if should prune, False otherwise
    """
    try:
        debit_amt = parse_amount(debit.amount or "0")
        credit_amt = parse_amount(credit.amount or "0")

        if debit_amt == 0 or credit_amt == 0:
            return True

        # Calculate delta percentage
        delta = abs(debit_amt - credit_amt)
        delta_pct = (delta / debit_amt) * 100

        # If delta > 50% and no strong signals, prune
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
        # On any parsing error, don't prune (let scoring handle it)
        return False


def generate_candidate_pairs(
    debit_transactions: list[Transaction],
    credit_transactions: list[Transaction],
) -> list[Tuple[Transaction, Transaction]]:
    """Generate all candidate debit × credit pairs with filtering.

    Applies hard-reject rules and early pruning to reduce the candidate set.

    Args:
        debit_transactions: List of debit transactions
        credit_transactions: List of credit transactions

    Returns:
        List of (debit, credit) candidate pairs
    """
    candidates = []

    for debit in debit_transactions:
        for credit in credit_transactions:
            # Apply hard reject rules
            should_reject, reason = should_hard_reject(debit, credit)
            if should_reject:
                continue

            # Apply early pruning
            if should_early_prune(debit, credit):
                continue

            # This is a valid candidate
            candidates.append((debit, credit))

    return candidates


__all__ = [
    "is_normal_payment_credit",
    "has_refund_keyword",
    "is_malformed",
    "should_hard_reject",
    "should_early_prune",
    "generate_candidate_pairs",
]
