"""Similarity metrics and scoring helpers for adjustment pairing."""

from datetime import date
from decimal import Decimal
import re
from typing import Literal

from cc_parser.parsers.adjustment_pairing.candidates import has_refund_keyword
from cc_parser.parsers.adjustment_pairing.constants import (
    CONFIDENCE_HIGH_THRESHOLD,
    CONFIDENCE_MEDIUM_THRESHOLD,
    DATE_GAP_MEDIUM,
    DATE_GAP_SMALL,
    MERCHANT_SIMILARITY_HIGH,
    MERCHANT_SIMILARITY_MEDIUM,
    PARTIAL_REFUND_MIN_MERCHANT_SIMILARITY,
    PARTIAL_REFUND_PERCENT_THRESHOLD,
    PENALTY_LARGE_AMOUNT_DELTA,
    PENALTY_MERCHANT_MISMATCH,
    PENALTY_PERSON_CONFLICT,
    SCORE_EXACT_AMOUNT,
    SCORE_HIGH_MERCHANT_SIMILARITY,
    SCORE_MEDIUM_DATE_GAP,
    SCORE_MEDIUM_MERCHANT_SIMILARITY,
    SCORE_REFUND_KEYWORD,
    SCORE_SAME_CARD,
    SCORE_SAME_PERSON,
    SCORE_SMALL_DATE_GAP,
)
from cc_parser.parsers.models import Transaction
from cc_parser.parsers.narration import normalize_merchant_name
from cc_parser.parsers.tokens import format_amount, parse_amount, parse_date_value


def tokenize(text: str) -> set[str]:
    """Convert text to lowercase tokens, removing punctuation and numbers."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    tokens: set[str] = set()
    for token in text.split():
        if len(token) >= 2 and not token.isdigit():
            tokens.add(token)
    return tokens


def jaccard_similarity(set1: set[str], set2: set[str]) -> float:
    """Calculate Jaccard similarity between two token sets."""
    if not set1 or not set2:
        return 0.0
    intersection = len(set1 & set2)
    union = len(set1 | set2)
    if union == 0:
        return 0.0
    return intersection / union


def normalized_equals(text1: str, text2: str) -> bool:
    """Check if two strings are equal after normalization."""
    norm1 = re.sub(r"\s+", " ", text1.lower().strip())
    norm2 = re.sub(r"\s+", " ", text2.lower().strip())
    return norm1 == norm2


def normalized_contains(text1: str, text2: str) -> bool:
    """Check if text1 contains text2 after normalization."""
    norm1 = re.sub(r"\s+", " ", text1.lower().strip())
    norm2 = re.sub(r"\s+", " ", text2.lower().strip())
    return norm2 in norm1 or norm1 in norm2


def merchant_similarity(narration1: str, narration2: str) -> float:
    """Calculate merchant similarity score between two narrations."""
    tokens1 = tokenize(narration1)
    tokens2 = tokenize(narration2)

    if not tokens1 or not tokens2:
        return 0.3

    base_score = jaccard_similarity(tokens1, tokens2)
    if normalized_contains(narration1, narration2):
        base_score = min(1.0, base_score * 1.3)

    return base_score


def _coerce_pairing_date(date_str: str | None) -> date | None:
    """Parse transaction dates using the shared statement date helper."""
    return parse_date_value(date_str)


def calculate_date_gap(debit: Transaction, credit: Transaction) -> int | None:
    """Calculate gap in days between two transactions."""
    debit_date = _coerce_pairing_date(debit.date)
    credit_date = _coerce_pairing_date(credit.date)
    if not debit_date or not credit_date:
        return None
    return abs((debit_date - credit_date).days)


def calculate_amount_delta(
    debit: Transaction, credit: Transaction
) -> tuple[Decimal, str, str | None]:
    """Calculate signed amount delta (debit - credit)."""
    try:
        debit_amt = parse_amount(debit.amount or "0")
        credit_amt = parse_amount(credit.amount or "0")
        delta = debit_amt - credit_amt
        if debit_amt != 0:
            delta_pct = (abs(delta) / debit_amt) * 100
            delta_pct_str = f"{delta_pct:.2f}%"
        else:
            delta_pct_str = None
        return delta, format_amount(delta), delta_pct_str
    # TODO(pep758): drop the parentheses once Pyodide ships Python 3.14.
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
    """Determine the kind of adjustment pair based on signals."""
    if delta_decimal == 0:
        narration = (credit.narration or "").upper()
        if "REVERSAL" in narration or "REVERSED" in narration:
            return "reversal"
        return "exact_refund"

    if delta_percent_str:
        try:
            delta_pct = float(delta_percent_str.rstrip("%"))
            if (
                delta_pct <= PARTIAL_REFUND_PERCENT_THRESHOLD
                and merchant_sim >= PARTIAL_REFUND_MIN_MERCHANT_SIMILARITY
            ):
                return "partial_refund"
        # TODO(pep758): drop the parentheses once Pyodide ships Python 3.14.
        except (ValueError, Exception):
            pass

    return "possible_refund"


def score_candidate_pair(
    debit: Transaction,
    credit: Transaction,
    bank: str | None = None,
) -> tuple[int, list[str], float, float]:
    """Score a single candidate pair and generate reasons."""
    score = 0
    reasons: list[str] = []

    delta_decimal, _delta_str, delta_pct_str = calculate_amount_delta(debit, credit)

    if delta_decimal == 0:
        score += SCORE_EXACT_AMOUNT
        reasons.append("exact_amount_match")

    if (
        debit.card_number
        and credit.card_number
        and debit.card_number == credit.card_number
    ):
        score += SCORE_SAME_CARD
        reasons.append(f"same_card_{debit.card_number}")

    if debit.person and credit.person and debit.person == credit.person:
        score += SCORE_SAME_PERSON
        reasons.append(f"same_person_{debit.person}")

    if debit.person and credit.person and debit.person != credit.person:
        score += PENALTY_PERSON_CONFLICT
        reasons.append(f"person_conflict_{debit.person}_vs_{credit.person}")

    if has_refund_keyword(credit):
        score += SCORE_REFUND_KEYWORD
        reasons.append("refund_keyword_present")

    debit_merchant = normalize_merchant_name(debit.narration or "", bank)
    credit_merchant = normalize_merchant_name(credit.narration or "", bank)
    merchant_sim = merchant_similarity(debit_merchant, credit_merchant)

    if merchant_sim >= MERCHANT_SIMILARITY_HIGH:
        score += SCORE_HIGH_MERCHANT_SIMILARITY
        reasons.append(f"high_merchant_similarity_{merchant_sim:.2f}")
    elif merchant_sim >= MERCHANT_SIMILARITY_MEDIUM:
        score += SCORE_MEDIUM_MERCHANT_SIMILARITY
        reasons.append(f"medium_merchant_similarity_{merchant_sim:.2f}")
    elif merchant_sim < 0.2:
        score += PENALTY_MERCHANT_MISMATCH
        reasons.append(f"merchant_mismatch_{merchant_sim:.2f}")

    date_gap = calculate_date_gap(debit, credit)
    if date_gap is not None:
        if date_gap <= DATE_GAP_SMALL:
            score += SCORE_SMALL_DATE_GAP
            reasons.append(f"small_date_gap_{date_gap}d")
        elif date_gap <= DATE_GAP_MEDIUM:
            score += SCORE_MEDIUM_DATE_GAP
            reasons.append(f"medium_date_gap_{date_gap}d")

    if delta_pct_str:
        try:
            delta_pct = float(delta_pct_str.rstrip("%"))
            if delta_pct > 50:
                score += PENALTY_LARGE_AMOUNT_DELTA
                reasons.append(f"large_amount_delta_{delta_pct:.1f}%")
        # TODO(pep758): drop the parentheses once Pyodide ships Python 3.14.
        except (ValueError, Exception):
            pass

    debit_tokens = tokenize(debit.narration or "")
    credit_tokens = tokenize(credit.narration or "")
    narration_sim = jaccard_similarity(debit_tokens, credit_tokens)

    return score, reasons, merchant_sim, narration_sim


def determine_confidence(score: int) -> Literal["high", "medium", "low"]:
    """Determine confidence level based on score."""
    if score >= CONFIDENCE_HIGH_THRESHOLD:
        return "high"
    if score >= CONFIDENCE_MEDIUM_THRESHOLD:
        return "medium"
    return "low"


__all__ = [
    "calculate_amount_delta",
    "calculate_date_gap",
    "determine_confidence",
    "determine_kind",
    "jaccard_similarity",
    "merchant_similarity",
    "normalized_contains",
    "normalized_equals",
    "score_candidate_pair",
    "tokenize",
]
