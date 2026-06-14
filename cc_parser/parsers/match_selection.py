"""Match selection algorithm for choosing best non-overlapping pairs."""

from cc_parser.parsers.models import AdjustmentPair


def select_best_non_overlapping_pairs(
    all_pairs: list[AdjustmentPair],
) -> list[AdjustmentPair]:
    """Select best non-overlapping pairs using greedy algorithm.

    Constraints:
    - One debit can belong to at most one selected pair
    - One credit can belong to at most one selected pair

    Selection order:
    1. Sort by score (descending)
    2. For ties, prefer smaller amount_delta
    3. For ties, prefer smaller date_gap
    4. For ties, use input order

    Args:
        all_pairs: List of all scored candidate pairs

    Returns:
        List of selected non-overlapping pairs
    """
    if not all_pairs:
        return []

    # Filter out non-positive scores — no useful signal
    all_pairs = [p for p in all_pairs if p.score > 0]
    if not all_pairs:
        return []

    # Sort pairs by priority
    def sort_key(pair: AdjustmentPair) -> tuple:
        # Parse amount_delta for comparison
        try:
            delta = abs(float(pair.amount_delta.replace(",", "")))
        except ValueError, AttributeError:
            delta = 999999.0

        # Parse date_gap
        date_gap = pair.date_gap_days if pair.date_gap_days is not None else 999999

        # Return tuple for sorting (negated score for descending)
        return (-pair.score, delta, date_gap)

    sorted_pairs = sorted(all_pairs, key=sort_key)

    # Track used transaction IDs
    used_debits = set()
    used_credits = set()

    selected = []

    for pair in sorted_pairs:
        # Check if either transaction is already used
        debit_used = (
            pair.debit_transaction_id and pair.debit_transaction_id in used_debits
        )
        credit_used = (
            pair.credit_transaction_id and pair.credit_transaction_id in used_credits
        )

        # Skip if overlapping
        if debit_used or credit_used:
            continue

        # Select this pair
        selected.append(pair)

        # Mark transactions as used
        if pair.debit_transaction_id:
            used_debits.add(pair.debit_transaction_id)
        if pair.credit_transaction_id:
            used_credits.add(pair.credit_transaction_id)

    return selected


__all__ = [
    "select_best_non_overlapping_pairs",
]
