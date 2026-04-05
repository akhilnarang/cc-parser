"""Narration cleaning, merging, and enrichment utilities."""

import re
from typing import Any

from cc_parser.parsers.tokens import (
    SEPARATOR_TOKENS,
    clean_space,
    normalize_token,
    parse_amount_token,
    parse_date_token,
)
from cc_parser.parsers.cards import looks_like_member_header


def _is_noise_context_line(tokens: list[str]) -> bool:
    """Return True for obvious non-transaction helper/footer lines."""
    joined_upper = clean_space(" ".join(tokens)).upper()
    if re.search(r"\bPAGE\s+\d+\s+OF\s+\d+\b", joined_upper):
        return True
    if any(
        phrase in joined_upper
        for phrase in {
            "DATE & TIME TRANSACTION DESCRIPTION",
            "TRANSACTIONS TOTAL AMOUNT",
            "REWARDS PROGRAM POINTS SUMMARY",
            "DOMESTIC TRANSACTIONS",
            "INTERNATIONAL TRANSACTIONS",
            "NOTE:",
            "BONUS NEUCOINS SUMMARY",
            "TERMS AND CONDITIONS APPLY",
            "SR NO.",
        }
    ):
        return True
    return False


def collect_row_context_tokens(
    lines: list[list[dict[str, Any]]], line_index: int
) -> tuple[list[str], list[str]]:
    """Collect nearby non-date lines belonging to same transaction row."""
    prev_tokens: list[str] = []
    next_tokens: list[str] = []

    for back in range(1, 4):
        idx = line_index - back
        if idx < 0:
            break
        tokens = [normalize_token(str(item.get("text", ""))) for item in lines[idx]]
        tokens = [token for token in tokens if token]
        if not tokens:
            continue
        if any(parse_date_token(token) for token in tokens):
            break
        if looks_like_member_header(tokens):
            break
        if _is_noise_context_line(tokens):
            break
        prev_tokens = tokens + prev_tokens

    for fwd in range(1, 4):
        idx = line_index + fwd
        if idx >= len(lines):
            break
        tokens = [normalize_token(str(item.get("text", ""))) for item in lines[idx]]
        tokens = [token for token in tokens if token]
        if not tokens:
            continue
        if any(parse_date_token(token) for token in tokens):
            break
        if looks_like_member_header(tokens):
            break
        if _is_noise_context_line(tokens):
            break
        next_tokens.extend(tokens)

    return prev_tokens, next_tokens


def clean_narration_artifacts(narration: str) -> str:
    """Remove obvious non-transaction artifacts from narration text."""
    cleaned = re.sub(r"\bPage\s+\d+\s+of\s+\d+\b", "", narration, flags=re.IGNORECASE)

    ref_hits = [m.start() for m in re.finditer(r"\(Ref#", cleaned, flags=re.IGNORECASE)]
    if len(ref_hits) >= 2:
        cleaned = cleaned[: ref_hits[1]].rstrip()

    ref_match = re.search(r"\(Ref#", cleaned, flags=re.IGNORECASE)
    if ref_match:
        close_idx = cleaned.find(")", ref_match.start())
        if close_idx != -1:
            cleaned = cleaned[: close_idx + 1]

    cleaned = clean_space(cleaned)
    cleaned = re.sub(r"\(Ref#\s*$", "(Ref#", cleaned)
    return cleaned


def needs_context_merge(narration: str) -> bool:
    """Decide whether wrapped context lines should be merged into narration."""
    if not narration:
        return True
    if re.match(r"^ST\d{10,}\)$", narration):
        return True
    if re.search(r"\(Ref#\s*$", narration, flags=re.IGNORECASE):
        return True
    if "(Ref#" in narration and ")" not in narration:
        return True
    return False


def enrich_reference_only_narration(
    lines: list[list[dict[str, Any]]], start_index: int, narration: str
) -> str:
    """Enrich narration when current line only contains reference fragment."""
    if not re.match(r"^ST\d{10,}\)", narration):
        return narration

    context_candidates: list[str] = []
    for offset in (1, 2):
        idx = start_index - offset
        if idx < 0:
            continue
        tokens = [normalize_token(str(item.get("text", ""))) for item in lines[idx]]
        tokens = [token for token in tokens if token]
        if not tokens:
            continue
        if any(parse_date_token(token) for token in tokens):
            continue
        joined = clean_space(" ".join(tokens))
        joined_upper = joined.upper()
        if "PAGE " in joined_upper and " OF " in joined_upper:
            continue
        if "PAYMENT" in joined_upper or "REF#" in joined_upper:
            context_candidates.append(joined)

    if not context_candidates:
        return narration

    context = context_candidates[0]
    if narration in context:
        return clean_narration_artifacts(context)

    if context.rstrip().endswith("(Ref#"):
        return clean_narration_artifacts(f"{context} {narration}")

    return clean_narration_artifacts(f"{context} {narration}")


def extract_continuation_narration(
    lines: list[list[dict[str, Any]]], start_index: int
) -> str | None:
    """Recover narration from nearby wrapped lines."""
    collected: list[str] = []
    max_lookahead = 3
    skip_phrases = {
        "TRANSACTION TIME CAPTURED",
        "TRANSACTIONS TOTAL AMOUNT",
        "REWARDS PROGRAM POINTS SUMMARY",
    }

    def cleaned_line_tokens(idx: int) -> list[str]:
        line_tokens = [
            normalize_token(str(item.get("text", ""))) for item in lines[idx]
        ]
        line_tokens = [token for token in line_tokens if token]
        if not line_tokens:
            return []
        if any(parse_date_token(token) for token in line_tokens):
            return []
        if looks_like_member_header(line_tokens):
            return []

        joined_upper = clean_space(" ".join(line_tokens)).upper()
        if any(phrase in joined_upper for phrase in skip_phrases):
            return []
        if re.search(r"\bPAGE\s+\d+\s+OF\s+\d+\b", joined_upper):
            return []

        cleaned = []
        for token in line_tokens:
            if token in SEPARATOR_TOKENS or token in {"l", "I", "C", "Cr", "CR"}:
                continue
            if parse_amount_token(token) is not None:
                continue
            cleaned.append(token)
        return cleaned

    for offset in range(2, 0, -1):
        idx = start_index - offset
        if idx < 0:
            continue
        candidate = cleaned_line_tokens(idx)
        if not candidate:
            continue
        joined = clean_space(" ".join(candidate)).upper()
        if any(
            keyword in joined for keyword in {"CONSOLIDATED", "FCY", "MARKUP", "FEE"}
        ):
            collected.extend(candidate)

    for offset in range(1, max_lookahead + 1):
        idx = start_index + offset
        if idx >= len(lines):
            break

        cleaned = cleaned_line_tokens(idx)
        if not cleaned:
            continue
        collected.extend(cleaned)

    if not collected:
        return None
    return clean_space(" ".join(collected))


__all__ = [
    "collect_row_context_tokens",
    "clean_narration_artifacts",
    "needs_context_merge",
    "enrich_reference_only_narration",
    "extract_continuation_narration",
]
