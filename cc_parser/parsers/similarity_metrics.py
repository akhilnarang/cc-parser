"""String and merchant similarity metrics for adjustment pairing."""

import re
from typing import Set


def tokenize(text: str) -> Set[str]:
    """Convert text to lowercase tokens, removing punctuation and numbers."""
    # Convert to lowercase
    text = text.lower()
    # Remove common punctuation and special characters
    text = re.sub(r"[^\w\s]", " ", text)
    # Remove numbers and standalone single chars
    tokens = set()
    for token in text.split():
        # Skip very short tokens and pure numbers
        if len(token) >= 2 and not token.isdigit():
            tokens.add(token)
    return tokens


def jaccard_similarity(set1: Set[str], set2: Set[str]) -> float:
    """Calculate Jaccard similarity between two token sets.

    Returns a value between 0.0 (no overlap) and 1.0 (identical sets).
    """
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
    """Calculate merchant similarity score between two narrations.

    Uses token-based Jaccard similarity with normalization.

    Args:
        narration1: First narration string
        narration2: Second narration string

    Returns:
        Similarity score between 0.0 and 1.0
    """
    tokens1 = tokenize(narration1)
    tokens2 = tokenize(narration2)

    # If either narration tokenizes to empty (e.g. stripped processor wrapper),
    # return a neutral score: above the mismatch penalty threshold (0.2) but
    # below the medium similarity threshold (0.4) so it cannot serve as
    # refund evidence on its own.
    if not tokens1 or not tokens2:
        return 0.3

    # Boost score if one is contained in the other
    base_score = jaccard_similarity(tokens1, tokens2)
    if normalized_contains(narration1, narration2):
        base_score = min(1.0, base_score * 1.3)

    return base_score


__all__ = [
    "tokenize",
    "jaccard_similarity",
    "normalized_equals",
    "normalized_contains",
    "merchant_similarity",
]
