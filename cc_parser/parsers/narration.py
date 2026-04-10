"""Narration cleaning, merging, and enrichment utilities."""

import re
from typing import Any

from cc_parser.parsers.tokens import (
    SEPARATOR_TOKENS,
    clean_space,
    normalize_token,
    parse_amount_token,
    parse_date_token,
    parse_multi_token_date,
)
from cc_parser.parsers.cards import looks_like_member_header


def normalize_merchant_name(narration: str, bank: str | None = None) -> str:
    """Normalize merchant name from narration for similarity matching.

    This function strips bank-specific formatting, reference numbers, auth codes,
    terminal IDs, card fragments, location suffixes, and processor wrappers to
    extract the core merchant identity.

    Args:
        narration: Raw transaction narration
        bank: Optional bank identifier for bank-specific rules

    Returns:
        Normalized merchant-ish string suitable for similarity matching
    """
    if not narration:
        return ""

    text = narration.upper().strip()

    # Strip common reference patterns
    text = re.sub(r"\(REF#[^)]*\)", "", text)
    text = re.sub(r"REF\s*#?\s*\d+", "", text)
    text = re.sub(r"AUTH\s*CODE\s*:?\s*\w+", "", text)
    text = re.sub(r"CARD\s*\d{4}", "", text)
    text = re.sub(r"XX+\d{4}", "", text)
    text = re.sub(r"\*+\d{4}", "", text)

    # Strip terminal IDs and transaction IDs
    text = re.sub(r"TERMINAL\s*ID\s*:?\s*\w+", "", text)
    text = re.sub(r"TXN\s*ID\s*:?\s*\w+", "", text)
    text = re.sub(r"RRN\s*:?\s*\w+", "", text)

    # Strip long numeric sequences (likely reference numbers)
    text = re.sub(r"\b\d{8,}\b", "", text)

    # Strip processor wrappers
    processor_patterns = [
        r"REFUND\s+FR(OM|M)\s+",
        r"RAZORPAY\s+PAYMENTS?\s*",
        r"PAYMENT\s+GATEWAY\s*",
        r"\bGATEWAY\b\s*",
        r"\bPG\b\s+",
    ]
    for pattern in processor_patterns:
        text = re.sub(pattern, "", text)

    # Strip cosmetic wording at start/end
    cosmetic_patterns = [
        r"^REFUND\s+",
        r"^REVERSAL\s+",
        r"\s+REFUND$",
        r"\s+REVERSAL$",
        r"\s+REVERSED$",
    ]
    for pattern in cosmetic_patterns:
        text = re.sub(pattern, "", text)

    # Strip location suffixes (city/country codes)
    text = re.sub(r"\s+IN/[A-Z]{2,}$", "", text)  # e.g., "IN/KA", "IN/BANGALORE"
    # Only strip known country codes, not arbitrary 2-char merchant endings
    text = re.sub(r"\s+(?:US|UK|SG|AE|AU|HK|JP|DE|FR|NL|CA)$", "", text)

    # Strip noisy add-ons
    text = re.sub(r"PAY\s+IN\s+EMI'?S?", "", text)

    # Bank-specific normalization
    if bank:
        bank = bank.lower()
        if bank == "axis":
            # Axis-specific patterns
            text = re.sub(r"VISA\s+POS\s+TXN\s+AT\s+", "", text)
            text = re.sub(r"POS\s+TXN\s+AT\s+", "", text)
            # Strip "IN/MERCHANT CITY" patterns (e.g., "IN/ZEPTO BANGALORE" → "ZEPTO")
            text = re.sub(r"^IN/([A-Z]+)\s+[A-Z]+$", r"\1", text)
            text = re.sub(r"^IN/([A-Z]+)$", r"\1", text)
        elif bank == "sbi":
            # SBI-specific patterns
            text = re.sub(r"POS\s+\d+-\d+\s+", "", text)
        elif bank == "hsbc":
            # HSBC-specific patterns
            pass
        elif bank == "icici":
            # ICICI-specific patterns
            pass
        elif bank == "hdfc":
            # HDFC-specific patterns
            pass
        elif bank == "idfc":
            # IDFC-specific patterns
            pass
        elif bank == "indusind":
            # IndusInd-specific patterns
            pass
        elif bank == "slice":
            # Slice-specific patterns
            pass
        elif bank == "jupiter":
            # Jupiter-specific patterns
            pass
        elif bank == "bob":
            # BOB-specific patterns
            pass

    # Final cleanup
    text = clean_space(text)
    return text.strip()


def _is_page_marker(tokens: list[str]) -> bool:
    """Return True for page number markers like ``<2/3>``."""
    joined = clean_space(" ".join(tokens)).strip()
    return bool(re.fullmatch(r"<\d+/\d+>", joined))


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
        if _is_page_marker(tokens):
            continue
        if any(parse_date_token(token) for token in tokens):
            break
        if parse_multi_token_date(tokens, 0)[0] is not None:
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
        if _is_page_marker(tokens):
            continue
        if any(parse_date_token(token) for token in tokens):
            break
        if parse_multi_token_date(tokens, 0)[0] is not None:
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

    # Strip "(Pay in EMIs)" / "(Pay in EMI)" variants
    cleaned = re.sub(r"\s*\(Pay\s+in\s+EMI'?s?\)", "", cleaned, flags=re.IGNORECASE)

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
    "normalize_merchant_name",
    "collect_row_context_tokens",
    "clean_narration_artifacts",
    "needs_context_merge",
    "enrich_reference_only_narration",
    "extract_continuation_narration",
]
