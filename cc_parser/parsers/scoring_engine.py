"""Scoring engine for adjustment pair candidates."""

from datetime import datetime
from decimal import Decimal
from typing import Literal, Tuple

from cc_parser.parsers.models import Transaction
from cc_parser.parsers.tokens import parse_amount, format_amount
from cc_parser.parsers.narration import normalize_merchant_name
from cc_parser.parsers.similarity_metrics import (
    merchant_similarity,
    jaccard_similarity,
    tokenize,
)
from cc_parser.parsers.candidate_generation import has_refund_keyword
from cc_parser.parsers.scoring_constants import (
    SCORE_EXACT_AMOUNT,
    SCORE_SAME_CARD,
    SCORE_SAME_PERSON,
    SCORE_REFUND_KEYWORD,
    SCORE_HIGH_MERCHANT_SIMILARITY,
    SCORE_MEDIUM_MERCHANT_SIMILARITY,
    SCORE_SMALL_DATE_GAP,
    SCORE_MEDIUM_DATE_GAP,
    PENALTY_PERSON_CONFLICT,
    PENALTY_MERCHANT_MISMATCH,
    PENALTY_LARGE_AMOUNT_DELTA,
    CONFIDENCE_HIGH_THRESHOLD,
    CONFIDENCE_MEDIUM_THRESHOLD,
    PARTIAL_REFUND_PERCENT_THRESHOLD,
    PARTIAL_REFUND_MIN_MERCHANT_SIMILARITY,
    DATE_GAP_SMALL,
    DATE_GAP_MEDIUM,
    MERCHANT_SIMILARITY_HIGH,
    MERCHANT_SIMILARITY_MEDIUM,
)


def _parse_date(date_str: str) -> datetime | None:
    """Parse date string in DD/MM/YYYY format."""
    try:
        return datetime.strptime(date_str, "%d/%m/%Y")
    except (ValueError, Exception):
        return None


def calculate_date_gap(debit: Transaction, credit: Transaction) -> int | None:
    """Calculate gap in days between two transactions."""
    debit_date = _parse_date(debit.date)
    credit_date = _parse_date(credit.date)

    if not debit_date or not credit_date:
        return None

    return abs((debit_date - credit_date).days)


def calculate_amount_delta(
    debit: Transaction, credit: Transaction
) -> Tuple[Decimal, str, str | None]:
    """Calculate signed amount delta (debit - credit).

    Returns:
        (delta_decimal, delta_formatted, delta_percent_formatted)
    """
    try:
        debit_amt = parse_amount(debit.amount or "0")
        credit_amt = parse_amount(credit.amount or "0")

        delta = debit_amt - credit_amt

        # Calculate percentage
        if debit_amt != 0:
            delta_pct = (abs(delta) / debit_amt) * 100
            delta_pct_str = f"{delta_pct:.2f}%"
        else:
            delta_pct_str = None

        return delta, format_amount(delta), delta_pct_str

    except (ValueError, Exception):
        return Decimal("0"), "0.00", None


def determine_kind(
    debit: Transaction,
    credit: Transaction,
    delta_decimal: Decimal,
    delta_percent_str: str | None,
    merchant_sim: float,
    score: int,
) -> Literal[
    "exact_refund",
    "partial_refund",
    "possible_refund",
    "reversal",
    "credit_balance_refund",
]:
    """Determine the kind of adjustment pair based on signals.

    Args:
        debit: Debit transaction
        credit: Credit transaction
        delta_decimal: Amount delta as Decimal
        delta_percent_str: Delta percentage string
        merchant_sim: Merchant similarity score
        score: Total score

    Returns:
        Kind classification
    """
    # Exact refund: zero delta
    if delta_decimal == 0:
        # Check for reversal keywords
        narration = (credit.narration or "").upper()
        if "REVERSAL" in narration or "REVERSED" in narration:
            return "reversal"
        return "exact_refund"

    # Partial refund: small delta with good merchant similarity
    if delta_percent_str:
        try:
            delta_pct = float(delta_percent_str.rstrip("%"))
            if (
                delta_pct <= PARTIAL_REFUND_PERCENT_THRESHOLD
                and merchant_sim >= PARTIAL_REFUND_MIN_MERCHANT_SIMILARITY
            ):
                return "partial_refund"
        except (ValueError, Exception):
            pass

    # Default to possible_refund
    return "possible_refund"


def score_candidate_pair(
    debit: Transaction,
    credit: Transaction,
    bank: str | None = None,
) -> Tuple[int, list[str], float, float]:
    """Score a single candidate pair and generate reasons.

    Args:
        debit: Debit transaction
        credit: Credit transaction
        bank: Optional bank identifier for normalization

    Returns:
        (score, reasons, merchant_similarity, narration_similarity)
    """
    score = 0
    reasons = []

    # Calculate amount delta
    delta_decimal, delta_str, delta_pct_str = calculate_amount_delta(debit, credit)

    # Signal: Exact amount match
    if delta_decimal == 0:
        score += SCORE_EXACT_AMOUNT
        reasons.append("exact_amount_match")

    # Signal: Same card
    if (
        debit.card_number
        and credit.card_number
        and debit.card_number == credit.card_number
    ):
        score += SCORE_SAME_CARD
        reasons.append(f"same_card_{debit.card_number}")

    # Signal: Same person
    if debit.person and credit.person and debit.person == credit.person:
        score += SCORE_SAME_PERSON
        reasons.append(f"same_person_{debit.person}")

    # Signal: Person conflict (penalty)
    if debit.person and credit.person and debit.person != credit.person:
        score += PENALTY_PERSON_CONFLICT
        reasons.append(f"person_conflict_{debit.person}_vs_{credit.person}")

    # Signal: Refund keyword
    if has_refund_keyword(credit):
        score += SCORE_REFUND_KEYWORD
        reasons.append("refund_keyword_present")

    # Calculate merchant similarity
    debit_merchant = normalize_merchant_name(debit.narration or "", bank)
    credit_merchant = normalize_merchant_name(credit.narration or "", bank)
    merchant_sim = merchant_similarity(debit_merchant, credit_merchant)

    # Signal: Merchant similarity
    if merchant_sim >= MERCHANT_SIMILARITY_HIGH:
        score += SCORE_HIGH_MERCHANT_SIMILARITY
        reasons.append(f"high_merchant_similarity_{merchant_sim:.2f}")
    elif merchant_sim >= MERCHANT_SIMILARITY_MEDIUM:
        score += SCORE_MEDIUM_MERCHANT_SIMILARITY
        reasons.append(f"medium_merchant_similarity_{merchant_sim:.2f}")
    elif merchant_sim < 0.2:
        score += PENALTY_MERCHANT_MISMATCH
        reasons.append(f"merchant_mismatch_{merchant_sim:.2f}")

    # Signal: Date gap
    date_gap = calculate_date_gap(debit, credit)
    if date_gap is not None:
        if date_gap <= DATE_GAP_SMALL:
            score += SCORE_SMALL_DATE_GAP
            reasons.append(f"small_date_gap_{date_gap}d")
        elif date_gap <= DATE_GAP_MEDIUM:
            score += SCORE_MEDIUM_DATE_GAP
            reasons.append(f"medium_date_gap_{date_gap}d")

    # Signal: Large amount delta (penalty)
    if delta_pct_str:
        try:
            delta_pct = float(delta_pct_str.rstrip("%"))
            if delta_pct > 50:
                score += PENALTY_LARGE_AMOUNT_DELTA
                reasons.append(f"large_amount_delta_{delta_pct:.1f}%")
        except (ValueError, Exception):
            pass

    # Calculate narration similarity (Jaccard on raw narrations)
    debit_tokens = tokenize(debit.narration or "")
    credit_tokens = tokenize(credit.narration or "")
    narration_sim = jaccard_similarity(debit_tokens, credit_tokens)

    return score, reasons, merchant_sim, narration_sim


def determine_confidence(
    score: int,
) -> Literal["high", "medium", "low"]:
    """Determine confidence level based on score.

    Args:
        score: Total score

    Returns:
        Confidence level
    """
    if score >= CONFIDENCE_HIGH_THRESHOLD:
        return "high"
    elif score >= CONFIDENCE_MEDIUM_THRESHOLD:
        return "medium"
    else:
        return "low"


__all__ = [
    "calculate_date_gap",
    "calculate_amount_delta",
    "determine_kind",
    "score_candidate_pair",
    "determine_confidence",
]
