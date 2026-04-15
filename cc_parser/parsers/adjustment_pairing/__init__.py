"""Adjustment pairing subsystem for refunds, reversals, and credits."""

from cc_parser.parsers.adjustment_pairing.candidates import (
    generate_candidate_pairs,
    has_refund_keyword,
    is_malformed,
    is_normal_payment_credit,
    should_early_prune,
    should_hard_reject,
)
from cc_parser.parsers.adjustment_pairing.constants import MERCHANT_SIMILARITY_MEDIUM
from cc_parser.parsers.adjustment_pairing.scoring import (
    calculate_amount_delta,
    calculate_date_gap,
    determine_confidence,
    determine_kind,
    jaccard_similarity,
    merchant_similarity,
    normalized_contains,
    normalized_equals,
    score_candidate_pair,
    tokenize,
)
from cc_parser.parsers.match_selection import select_best_non_overlapping_pairs
from cc_parser.parsers.models import AdjustmentPair, Transaction
from cc_parser.parsers.tokens import format_amount, parse_amount


def detect_adjustment_pairs(
    debit_transactions: list[Transaction],
    credit_transactions: list[Transaction],
    bank: str | None = None,
) -> list[AdjustmentPair]:
    """Detect possible adjustment pairs (refunds/reversals) between debits and credits."""
    contextual_pairs: list[AdjustmentPair] = []
    onesided_debit_ids: set[str] = set()
    for debit in debit_transactions:
        narration = (debit.narration or "").upper()
        reward = (debit.reward_points or "").strip()
        if "CREDIT BALANCE REFUND" in narration and parse_amount(reward) == 0:
            onesided_debit_ids.add(debit.transaction_id)
            contextual_pairs.append(
                AdjustmentPair(
                    pair_id=f"pair_onesided_{debit.transaction_id}",
                    debit_transaction_id=debit.transaction_id,
                    credit_transaction_id=None,
                    debit=debit,
                    credit=None,
                    score=75,
                    confidence="high",
                    kind="credit_balance_refund",
                    amount_delta=format_amount(parse_amount(debit.amount or "0")),
                    amount_delta_percent=None,
                    date_gap_days=None,
                    merchant_similarity=None,
                    narration_similarity=None,
                    reasons=["contextual_credit_balance_refund_debit"],
                )
            )

    regular_debits = [
        debit
        for debit in debit_transactions
        if debit.transaction_id not in onesided_debit_ids
    ]
    candidates = generate_candidate_pairs(regular_debits, credit_transactions)

    all_pairs: list[AdjustmentPair] = []
    for pair_counter, (debit, credit) in enumerate(candidates):
        score, reasons, merchant_sim, narration_sim = score_candidate_pair(
            debit, credit, bank
        )
        date_gap = calculate_date_gap(debit, credit)
        delta_decimal, delta_str, delta_pct_str = calculate_amount_delta(debit, credit)
        confidence = determine_confidence(score)
        kind = determine_kind(
            debit, credit, delta_decimal, delta_pct_str, merchant_sim, score
        )
        all_pairs.append(
            AdjustmentPair(
                pair_id=f"pair_{pair_counter:04d}",
                debit_transaction_id=debit.transaction_id,
                credit_transaction_id=credit.transaction_id,
                debit=debit,
                credit=credit,
                score=score,
                confidence=confidence,
                kind=kind,
                amount_delta=delta_str,
                amount_delta_percent=delta_pct_str,
                date_gap_days=date_gap,
                merchant_similarity=merchant_sim,
                narration_similarity=narration_sim,
                reasons=reasons,
            )
        )

    evidenced = []
    for pair in all_pairs:
        has_keyword = any("refund_keyword" in reason for reason in pair.reasons)
        has_merchant = (
            pair.merchant_similarity is not None
            and pair.merchant_similarity >= MERCHANT_SIMILARITY_MEDIUM
        )
        if has_keyword or has_merchant:
            evidenced.append(pair)

    selected = select_best_non_overlapping_pairs(evidenced)
    return contextual_pairs + selected


def split_paired_adjustments(
    debit_transactions: list[Transaction],
    credit_transactions: list[Transaction],
) -> tuple[list[Transaction], list[Transaction], list[Transaction]]:
    """Backward-compatible no-op adjustment splitter."""
    return debit_transactions, credit_transactions, []


def compute_adjustment_totals(adjustments: list[Transaction]) -> tuple[str, str]:
    """Backward-compatible no-op totals helper."""
    return "0.00", "0.00"


__all__ = [
    "calculate_amount_delta",
    "calculate_date_gap",
    "compute_adjustment_totals",
    "detect_adjustment_pairs",
    "determine_confidence",
    "determine_kind",
    "generate_candidate_pairs",
    "has_refund_keyword",
    "is_malformed",
    "is_normal_payment_credit",
    "jaccard_similarity",
    "merchant_similarity",
    "normalized_contains",
    "normalized_equals",
    "score_candidate_pair",
    "select_best_non_overlapping_pairs",
    "should_early_prune",
    "should_hard_reject",
    "split_paired_adjustments",
    "tokenize",
]
