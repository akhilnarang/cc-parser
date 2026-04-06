"""Transaction ID generation for stable statement-local identifiers."""

import hashlib
from cc_parser.parsers.models import Transaction


def generate_transaction_id(transaction: Transaction, index: int, bank: str) -> str:
    """Generate a deterministic transaction ID based on transaction data.

    Uses a hash of key transaction fields to ensure stability across re-parses
    while remaining statement-local and deterministic.

    Args:
        transaction: The transaction to generate an ID for
        index: Zero-based index of transaction in the statement
        bank: Bank identifier (e.g., "axis", "sbi")

    Returns:
        A stable transaction ID like "axis_txn_0001_a1b2c3"
    """
    # Create a stable hash from transaction fields
    hash_input = "|".join(
        [
            transaction.date or "",
            transaction.time or "",
            transaction.narration or "",
            transaction.amount or "",
            transaction.card_number or "",
            transaction.person or "",
            transaction.transaction_type,
        ]
    )

    # Generate a short hash (first 6 chars of md5)
    hash_value = hashlib.md5(hash_input.encode()).hexdigest()[:6]

    # Format: bank_txn_INDEX_HASH
    return f"{bank}_txn_{index:04d}_{hash_value}"


def assign_transaction_ids(
    transactions: list[Transaction], bank: str
) -> list[Transaction]:
    """Assign transaction IDs to all transactions in a statement.

    Args:
        transactions: List of transactions to assign IDs to
        bank: Bank identifier

    Returns:
        List of transactions with transaction_id field populated
    """
    result = []
    for idx, txn in enumerate(transactions):
        txn_id = generate_transaction_id(txn, idx, bank)
        result.append(txn.model_copy(update={"transaction_id": txn_id}))
    return result


__all__ = [
    "generate_transaction_id",
    "assign_transaction_ids",
]
