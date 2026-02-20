"""Generic statement parser.

This module contains shared parsing logic used across bank profiles:
- line reconstruction from word coordinates,
- transaction extraction,
- debit/credit classification,
- totals and reconciliation helpers.

Bank-specific modules may reuse and override this behavior.
"""

import re
from decimal import Decimal, InvalidOperation
from datetime import datetime
from pathlib import Path
from typing import Any

from cc_parser.parsers.base import StatementParser

DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
TIME_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
AMOUNT_RE = re.compile(r"[-+]?\d[\d,]*\.\d{2}")
CARD_TOKEN_RE = re.compile(r"(?<![0-9A-Za-z])[0-9Xx*]{10,20}(?![0-9A-Za-z])")
CARD_TOKEN_WITH_SEP_RE = re.compile(
    r"(?<![0-9A-Za-z])[0-9Xx*][0-9Xx*\s-]{8,30}[0-9Xx*](?![0-9A-Za-z])"
)
HONORIFIC_RE = re.compile(r"^(MR|MRS|MS|MISS|DR)\.?$", re.IGNORECASE)
SEPARATOR_TOKENS = {"|", "||", ":", "-", "--"}
CARD_LABEL_WORDS = {
    "CREDIT",
    "CARD",
    "NO",
    "NUMBER",
    "STATEMENT",
    "DATE",
    "PRIMARY",
    "ADDON",
    "ADDONCARD",
    "SUPPLEMENTARY",
}
NON_MEMBER_HEADERS = {
    "DOMESTIC TRANSACTIONS",
    "INTERNATIONAL TRANSACTIONS",
    "DATE TIME TRANSACTION DESCRIPTION REWARDS AMOUNT PI",
    "OFFERS ON YOUR CARD",
    "BENEFITS ON YOUR CARD",
    "CREDIT CARD STATEMENT",
    "MILLENNIA CREDIT CARD STATEMENT",
    "DINERS BLACK CREDIT CARD STATEMENT",
}


def clean_space(value: str) -> str:
    """Collapse repeated whitespace in a string.

    Args:
        value: Input text.

    Returns:
        Text with internal whitespace normalized to single spaces.
    """
    return " ".join(value.split())


def normalize_token(token: str) -> str:
    """Normalize a token extracted from PDF words.

    Args:
        token: Raw token text.

    Returns:
        Trimmed token without leading/trailing pipe separators.
    """
    value = token.strip()
    value = value.strip("|")
    return value


def parse_date_token(token: str) -> str | None:
    """Parse a statement date token.

    Args:
        token: Candidate token.

    Returns:
        Normalized date (`DD/MM/YYYY`) when valid; otherwise None.
    """
    value = normalize_token(token)
    return value if DATE_RE.fullmatch(value) else None


def parse_time_token(token: str) -> str | None:
    """Parse a statement time token.

    Args:
        token: Candidate token.

    Returns:
        Time string (`HH:MM`/`HH:MM:SS`) when valid; otherwise None.
    """
    value = normalize_token(token)
    return value if TIME_RE.fullmatch(value) else None


def parse_amount_token(token: str) -> str | None:
    """Extract a decimal amount from a token.

    Args:
        token: Candidate token.

    Returns:
        Amount string when present; otherwise None.
    """
    value = normalize_token(token).replace("`", "")
    match = AMOUNT_RE.search(value)
    return match.group(0) if match else None


def normalize_card_token(token: str) -> str:
    """Keep only card-mask characters and normalize mask symbol to `X`.

    Args:
        token: Raw token that may contain card digits/mask chars.

    Returns:
        Canonicalized token containing digits and `X`.
    """
    return re.sub(r"[^0-9Xx*]", "", token).upper().replace("*", "X")


def looks_like_card_token(token: str) -> bool:
    """Heuristically decide whether token resembles a masked card number.

    Args:
        token: Candidate card token.

    Returns:
        True when token matches expected masked-card shape.
    """
    normalized = normalize_card_token(token)
    if not (10 <= len(normalized) <= 20):
        return False
    digit_count = sum(ch.isdigit() for ch in normalized)
    x_count = normalized.count("X")
    return digit_count >= 6 and x_count >= 2


def mask_card_token(token: str) -> str:
    """Mask middle digits of a card-like token.

    Args:
        token: Card token with digits and mask characters.

    Returns:
        `first4 + masked middle + last4` representation.
    """
    normalized = normalize_card_token(token)
    if len(normalized) < 8:
        return normalized
    return f"{normalized[:4]}{'X' * (len(normalized) - 8)}{normalized[-4:]}"


def find_card_candidates(text: str) -> list[str]:
    """Find masked card-like values in arbitrary text.

    Args:
        text: Input text block.

    Returns:
        List of masked card values discovered in order.
    """
    found: list[str] = []
    seen: set[str] = set()

    for match in CARD_TOKEN_RE.finditer(text):
        token = normalize_card_token(match.group(0))
        if looks_like_card_token(token):
            masked = mask_card_token(token)
            if masked not in seen:
                found.append(masked)
                seen.add(masked)

    for match in CARD_TOKEN_WITH_SEP_RE.finditer(text):
        token = normalize_card_token(match.group(0))
        if looks_like_card_token(token):
            masked = mask_card_token(token)
            if masked not in seen:
                found.append(masked)
                seen.add(masked)

    return found


def parse_amount(amount: str) -> Decimal:
    """Convert an amount string into Decimal.

    Args:
        amount: Amount with optional separators/symbols.

    Returns:
        Decimal amount, or Decimal(0) if parsing fails.
    """
    normalized = amount.replace("`", "").replace(",", "").strip()
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return Decimal("0")


def parse_points(points: str | None) -> Decimal:
    """Extract numeric reward points from a token.

    Args:
        points: Points token, possibly None or mixed text.

    Returns:
        Decimal points value, defaulting to Decimal(0).
    """
    if not points:
        return Decimal("0")
    value = str(points).strip()
    match = re.search(r"\d+", value)
    if not match:
        return Decimal("0")
    return Decimal(match.group(0))


def format_amount(value: Decimal) -> str:
    """Format Decimal as comma-separated 2-decimal amount string.

    Args:
        value: Decimal amount.

    Returns:
        Human-readable amount string.
    """
    return f"{value:,.2f}"


def group_words_into_lines(
    words: list[dict[str, Any]], y_tolerance: float = 1.8
) -> list[list[dict[str, Any]]]:
    """Group extracted PDF words into visual lines.

    Args:
        words: Word dictionaries from extractor (must include x0/doctop).
        y_tolerance: Vertical tolerance to merge words into one line.

    Returns:
        List of lines; each line is a left-to-right word list.
    """
    sorted_words = sorted(
        words, key=lambda item: (float(item["doctop"]), float(item["x0"]))
    )
    lines: list[list[dict[str, Any]]] = []
    current_line: list[dict[str, Any]] = []
    current_y: float | None = None

    for word in sorted_words:
        y_value = float(word["doctop"])
        if current_y is None or abs(y_value - current_y) <= y_tolerance:
            current_line.append(word)
            current_y = y_value if current_y is None else (current_y + y_value) / 2
        else:
            lines.append(sorted(current_line, key=lambda item: float(item["x0"])))
            current_line = [word]
            current_y = y_value

    if current_line:
        lines.append(sorted(current_line, key=lambda item: float(item["x0"])))

    return lines


def extract_name(full_text: str) -> str | None:
    """Extract cardholder name from statement text.

    Args:
        full_text: Concatenated text across pages.

    Returns:
        Detected name or None when no confident match is found.
    """
    honorifics = {"MR", "MRS", "MS", "MISS", "DR"}
    for raw_line in full_text.splitlines():
        line = clean_space(raw_line)
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3 or len(parts) > 6:
            continue
        if parts[0].upper() not in honorifics:
            continue
        tail = parts[1:]
        if all(re.fullmatch(r"[A-Za-z][A-Za-z.'-]*", token) for token in tail):
            return " ".join(tail)

    match = re.search(
        r"\n\s*([A-Z][A-Z ]{4,})\s+Credit\s+Card\s+No\.",
        full_text,
        flags=re.IGNORECASE,
    )
    if match:
        candidate = clean_space(match.group(1)).upper()
        if 2 <= len(candidate.split()) <= 6:
            return candidate

    return None


def extract_card_number(full_text: str) -> str | None:
    """Extract first masked card number from statement text.

    Args:
        full_text: Concatenated statement text.

    Returns:
        Masked card number if found, else None.
    """
    candidates = find_card_candidates(full_text)
    return candidates[0] if candidates else None


def extract_card_from_filename(file_path: str) -> str | None:
    """Extract masked card number from input file name.

    Args:
        file_path: Source PDF path.

    Returns:
        Masked card number when detectable in file name, else None.
    """
    candidates = find_card_candidates(Path(file_path).name)
    return candidates[0] if candidates else None


def extract_due_date(full_text: str) -> str | None:
    """Extract due date from statement body text.

    Args:
        full_text: Concatenated statement text.

    Returns:
        Due date string in detected format, or None.
    """
    compact_text = clean_space(full_text)
    patterns = [
        r"PAYMENT\s+DUE\s+DATE\s*[:\-]?\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})",
        r"PAYMENT\s+DUE\s+DATE\s*[:\-]?\s*(\d{2}[/-]\d{2}[/-]\d{4})",
        r"DUE\s+DATE\s*[:\-]?\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})",
        r"DUE\s+DATE\s*[:\-]?\s*(\d{2}[/-]\d{2}[/-]\d{4})",
        r"DUE\s+DATE.{0,100}?(\d{1,2}\s+[A-Za-z]{3,9},\s+\d{4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, compact_text, flags=re.IGNORECASE)
        if match:
            return clean_space(match.group(1))
    return None


def extract_due_date_from_pages(pages: list[dict[str, Any]]) -> str | None:
    """Extract due date using line-level page tokens.

    Args:
        pages: Raw extractor page payloads.

    Returns:
        Due date string if identified, else None.
    """
    for page in pages:
        lines = group_words_into_lines(page.get("words") or [])
        for line_index, line_words in enumerate(lines):
            tokens = [normalize_token(str(item.get("text", ""))) for item in line_words]
            upper = [token.upper() for token in tokens]
            joined = clean_space(" ".join(tokens))

            if "DUE" in upper and "DATE" in upper:
                inline = re.search(r"\d{2}[/-]\d{2}[/-]\d{4}", joined)
                if inline:
                    return inline.group(0)
                month_fmt = re.search(r"\d{1,2}\s+[A-Za-z]{3,9},\s+\d{4}", joined)
                if month_fmt:
                    return month_fmt.group(0)

                if line_index + 1 < len(lines):
                    next_tokens = [
                        normalize_token(str(item.get("text", "")))
                        for item in lines[line_index + 1]
                    ]
                    next_joined = clean_space(" ".join(next_tokens))
                    next_inline = re.search(r"\d{2}[/-]\d{2}[/-]\d{4}", next_joined)
                    if next_inline:
                        return next_inline.group(0)
                    next_month_fmt = re.search(
                        r"\d{1,2}\s+[A-Za-z]{3,9},\s+\d{4}",
                        next_joined,
                    )
                    if next_month_fmt:
                        return next_month_fmt.group(0)
    return None


def extract_total_amount_due(full_text: str) -> str | None:
    """Extract statement-level total amount due from summary area.

    Args:
        full_text: Concatenated statement text.

    Returns:
        Amount string when found, otherwise None.
    """
    upper_text = full_text.upper()
    start = upper_text.find("TOTAL AMOUNT DUE")
    if start == -1:
        return None

    end = upper_text.find("TOTAL CREDIT LIMIT", start)
    segment = full_text[start:end] if end != -1 else full_text[start : start + 1200]

    for pattern in [r"C\s*\d[\d,]*\.\d{2}", r"`\s*\d[\d,]*\.\d{2}", r"\d[\d,]*\.\d{2}"]:
        match = re.search(pattern, segment)
        if match:
            return normalize_amount(match.group(0).replace("C", "").strip())

    return None


def _to_decimal(amount: str | None) -> Decimal:
    """Convert optional amount string to Decimal.

    Args:
        amount: Amount string or None.

    Returns:
        Decimal value (0 for missing values).
    """
    if not amount:
        return Decimal("0")
    return parse_amount(amount)


def extract_statement_summary(full_text: str) -> dict[str, str | list[str] | None]:
    """Extract summary block amount candidates and heuristic field mapping.

    Args:
        full_text: Concatenated statement text.

    Returns:
        Dictionary containing amount candidates and mapped summary fields.
    """
    upper_text = full_text.upper()
    start = upper_text.find("PAYMENTS/CREDITS")
    end = upper_text.find("TOTAL CREDIT LIMIT", start if start != -1 else 0)

    if start == -1:
        segment = full_text[:2000]
    else:
        segment = full_text[start : end if end != -1 else start + 2000]

    raw_amounts = [
        normalize_amount(match.group(0).replace("C", "").replace("`", "").strip())
        for match in re.finditer(r"[C`]?\s*\d[\d,]*\.\d{2}", segment)
    ]

    unique_amounts: list[str] = []
    for value in raw_amounts:
        if value not in unique_amounts:
            unique_amounts.append(value)

    summary: dict[str, str | list[str] | None] = {
        "summary_amount_candidates": unique_amounts,
        "payments_credits_received": None,
        "previous_statement_dues": None,
        "purchases_debit": None,
        "finance_charges": None,
        "equation_tail": None,
    }

    if len(unique_amounts) >= 5:
        summary["payments_credits_received"] = unique_amounts[0]
        summary["previous_statement_dues"] = unique_amounts[1]
        summary["purchases_debit"] = unique_amounts[2]
        summary["finance_charges"] = unique_amounts[3]
        summary["equation_tail"] = unique_amounts[4]

    return summary


def build_reconciliation(
    statement_total_amount_due: str | None,
    debit_transactions: list[dict[str, str | None]],
    credit_transactions: list[dict[str, str | None]],
    summary_fields: dict[str, str | list[str] | None],
) -> dict[str, str | list[str] | None]:
    """Build reconciliation metrics across statement/header/parsed totals.

    Args:
        statement_total_amount_due: Statement due amount from header.
        debit_transactions: Parsed debit transactions.
        credit_transactions: Parsed credit transactions.
        summary_fields: Parsed header summary fields/candidates.

    Returns:
        Dictionary with computed totals and delta values.
    """

    def _str_field(value: str | list[str] | None) -> str | None:
        return value if isinstance(value, str) else None

    debit_total = Decimal("0")
    for txn in debit_transactions:
        debit_total += parse_amount(str(txn.get("amount") or "0"))

    credit_total = Decimal("0")
    for txn in credit_transactions:
        credit_total += parse_amount(str(txn.get("amount") or "0"))

    statement_due = _to_decimal(statement_total_amount_due)
    parsed_net_due = debit_total - credit_total

    prev_dues = _to_decimal(_str_field(summary_fields.get("previous_statement_dues")))
    purchases = _to_decimal(_str_field(summary_fields.get("purchases_debit")))
    finance = _to_decimal(_str_field(summary_fields.get("finance_charges")))
    received = _to_decimal(_str_field(summary_fields.get("payments_credits_received")))
    header_computed_due = prev_dues + purchases + finance - received

    return {
        "statement_total_amount_due": statement_total_amount_due,
        "parsed_debit_total": format_amount(debit_total),
        "parsed_credit_total": format_amount(credit_total),
        "parsed_net_due_estimate": format_amount(parsed_net_due),
        "delta_statement_vs_parsed_debit": format_amount(statement_due - debit_total),
        "delta_statement_vs_parsed_net": format_amount(statement_due - parsed_net_due),
        "header_previous_statement_dues": str(
            summary_fields.get("previous_statement_dues") or ""
        ),
        "header_purchases_debit": str(summary_fields.get("purchases_debit") or ""),
        "header_finance_charges": str(summary_fields.get("finance_charges") or ""),
        "header_payments_credits_received": str(
            summary_fields.get("payments_credits_received") or ""
        ),
        "header_computed_due_estimate": format_amount(header_computed_due),
        "delta_statement_vs_header_estimate": format_amount(
            statement_due - header_computed_due
        ),
        "summary_amount_candidates": summary_fields.get("summary_amount_candidates")
        or [],
    }


def extract_card_from_line(tokens: list[str]) -> tuple[str | None, str | None]:
    """Extract card and optional member label from a tokenized line.

    Args:
        tokens: Normalized line tokens.

    Returns:
        Tuple of `(masked_card, member_name)`; values may be None.
    """
    card_indices: list[int] = []
    card_value: str | None = None

    for index, token in enumerate(tokens):
        normalized = normalize_card_token(token)
        if looks_like_card_token(normalized):
            card_indices = [index]
            card_value = mask_card_token(normalized)
            break

    if card_value is None:
        for size in (2, 3):
            for start in range(0, len(tokens) - size + 1):
                chunk = "".join(
                    normalize_card_token(tokens[start + offset])
                    for offset in range(size)
                )
                if looks_like_card_token(chunk):
                    card_indices = list(range(start, start + size))
                    card_value = mask_card_token(chunk)
                    break
            if card_value:
                break

    if card_value is None:
        return None, None

    candidate_tokens = [
        token for i, token in enumerate(tokens) if i not in set(card_indices)
    ]
    words_only = []
    for token in candidate_tokens:
        cleaned = re.sub(r"[^A-Za-z.'-]", "", token)
        if not cleaned:
            continue
        key = re.sub(r"[^A-Za-z]", "", cleaned).upper()
        if key in CARD_LABEL_WORDS:
            continue
        if key in {"SPENDS", "OVERVIEW", "TRANSACTION", "DETAILS"}:
            continue
        if len(cleaned) == 1:
            continue
        words_only.append(cleaned)

    if words_only and HONORIFIC_RE.fullmatch(words_only[0]):
        words_only = words_only[1:]

    member = " ".join(words_only).strip() if words_only else None
    if member and member.upper() in {"CREDIT CARD", "CREDIT CARD NO", "NO"}:
        member = None
    return card_value, member


def looks_like_member_header(tokens: list[str]) -> str | None:
    """Detect whether a line looks like a person/member section header.

    Args:
        tokens: Normalized line tokens.

    Returns:
        Uppercased member/header text when valid, else None.
    """
    cleaned_words: list[str] = []
    for token in tokens:
        value = normalize_token(token)
        if (
            not value
            or parse_date_token(value)
            or parse_time_token(value)
            or parse_amount_token(value)
        ):
            return None
        if value in SEPARATOR_TOKENS:
            continue
        letters = re.sub(r"[^A-Za-z ]", "", value).strip()
        if not letters:
            return None
        if len(letters) == 1:
            return None
        cleaned_words.append(letters.upper())

    if not (2 <= len(cleaned_words) <= 4):
        return None

    candidate = " ".join(cleaned_words)
    if candidate in NON_MEMBER_HEADERS:
        return None
    if (
        "CREDIT" in cleaned_words
        and "CARD" in cleaned_words
        and "STATEMENT" in cleaned_words
    ):
        return None
    if any(
        word in {"CARD", "STATEMENT", "DATE", "TIME", "DESCRIPTION"}
        for word in cleaned_words
    ):
        return None
    if any(
        word in {"TRANSACTIONS", "REWARDS", "PROGRAM", "SUMMARY"}
        for word in cleaned_words
    ):
        return None
    return candidate


def is_invalid_person_label(value: str | None) -> bool:
    """Check whether extracted person label is clearly a section/header label.

    Args:
        value: Candidate person label.

    Returns:
        True if value appears to be a non-person heading.
    """
    if not value:
        return True
    words = [w for w in re.sub(r"[^A-Za-z ]", "", value).upper().split() if w]
    if not words:
        return True
    candidate = " ".join(words)
    if candidate in NON_MEMBER_HEADERS:
        return True
    if "CREDIT" in words and "CARD" in words and "STATEMENT" in words:
        return True
    if any(
        word in {"CARD", "STATEMENT", "DATE", "TIME", "DESCRIPTION"} for word in words
    ):
        return True
    return False


def normalize_amount(value: str) -> str:
    """Normalize amount token formatting.

    Args:
        value: Amount text.

    Returns:
        Amount text without statement currency markers.
    """
    return value.replace("`", "")


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
    """Collect nearby non-date lines belonging to same transaction row.

    Args:
        lines: Tokenized page lines.
        line_index: Index of the primary line containing date/time/amount.

    Returns:
        Tuple `(prev_tokens, next_tokens)` from wrapped transaction fragments.
    """
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
    """Remove obvious non-transaction artifacts from narration text.

    Args:
        narration: Parsed narration text.

    Returns:
        Cleaned narration text.
    """
    cleaned = re.sub(r"\bPage\s+\d+\s+of\s+\d+\b", "", narration, flags=re.IGNORECASE)

    # If a wrapped row merges two transaction fragments, the same reference marker
    # often appears twice; keep only the first fragment.
    ref_hits = [m.start() for m in re.finditer(r"\(Ref#", cleaned, flags=re.IGNORECASE)]
    if len(ref_hits) >= 2:
        cleaned = cleaned[: ref_hits[1]].rstrip()

    # If a reference block is complete, keep text through the first closing
    # parenthesis to avoid trailing merged fragments from wrapped rows.
    ref_match = re.search(r"\(Ref#", cleaned, flags=re.IGNORECASE)
    if ref_match:
        close_idx = cleaned.find(")", ref_match.start())
        if close_idx != -1:
            cleaned = cleaned[: close_idx + 1]

    cleaned = clean_space(cleaned)

    # Normalize common split pattern around payment reference.
    cleaned = re.sub(r"\(Ref#\s*$", "(Ref#", cleaned)
    return cleaned


def needs_context_merge(narration: str, narration_tokens: list[str]) -> bool:
    """Decide whether wrapped context lines should be merged into narration.

    Args:
        narration: Base narration extracted from the anchor transaction line.
        narration_tokens: Token list used to build base narration.

    Returns:
        True when base narration looks incomplete and needs context merge.
    """
    if not narration:
        return True
    if re.match(r"^ST\d{10,}\)$", narration):
        return True
    if re.search(r"\(Ref#\s*$", narration, flags=re.IGNORECASE):
        return True
    if "(Ref#" in narration and ")" not in narration:
        return True
    del narration_tokens
    return False


def enrich_reference_only_narration(
    lines: list[list[dict[str, Any]]], start_index: int, narration: str
) -> str:
    """Enrich narration when current line only contains reference fragment.

    Args:
        lines: Tokenized lines for current page.
        start_index: Index of current transaction line.
        narration: Current parsed narration.

    Returns:
        Improved narration when contextual payment line is found, else original.
    """
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

    # Typical broken split: "... (Ref#" on previous line and "ST... )" on current line.
    if context.rstrip().endswith("(Ref#"):
        return clean_narration_artifacts(f"{context} {narration}")

    return clean_narration_artifacts(f"{context} {narration}")


def extract_continuation_narration(
    lines: list[list[dict[str, Any]]], start_index: int
) -> str | None:
    """Recover narration from nearby wrapped lines.

    Args:
        lines: Tokenized lines for a page.
        start_index: Index of the main transaction line.

    Returns:
        Reconstructed narration text when available, else None.
    """
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


def classify_credit_transaction(
    tokens: list[str], narration: str, context_text: str = ""
) -> tuple[bool, list[str]]:
    """Classify a parsed row as credit using structural markers.

    Args:
        tokens: Parsed line tokens.
        narration: Row narration text (unused; kept for compatibility).
        context_text: Nearby context text (unused; kept for compatibility).

    Returns:
        Tuple `(is_credit, reasons)` where reasons contains matched markers.
    """
    reasons: list[str] = []
    del narration
    del context_text

    for token in tokens:
        normalized = re.sub(r"[^A-Z]", "", token.upper())
        if normalized == "CR":
            reasons.append("cr_marker")
            break

    for token in tokens:
        token_upper = token.upper()
        if token_upper.endswith("CR") and any(ch.isdigit() for ch in token_upper):
            if "cr_marker" not in reasons:
                reasons.append("cr_marker")
            break

    plus_positions = [i for i, token in enumerate(tokens) if token == "+"]
    for idx in plus_positions:
        j = idx + 1
        while j < len(tokens) and tokens[j] in SEPARATOR_TOKENS:
            j += 1
        if j >= len(tokens):
            continue

        next_token = tokens[j]
        next_upper = re.sub(r"[^A-Z]", "", next_token.upper())

        # Structural credit marker pattern: "+ C <amount>" (or "+ <amount>")
        if next_upper in {"C", "CR"}:
            reasons.append("plus_amount_marker")
            break
        if parse_amount_token(next_token) is not None:
            reasons.append("plus_amount_marker")
            break

        # If + is followed by reward points number, treat it as debit reward syntax.
        if re.fullmatch(r"\d{1,6}", next_token):
            continue

    return (len(reasons) > 0), reasons


def group_transactions_by_person(
    transactions: list[dict[str, str | None]], fallback_name: str | None
) -> list[dict[str, Any]]:
    """Group transactions by person and compute totals/points.

    Args:
        transactions: Parsed debit transactions.
        fallback_name: Name used when row person is missing.

    Returns:
        Per-person grouped records including totals and row lists.
    """
    grouped: dict[str, list[dict[str, str | None]]] = {}
    for txn in transactions:
        person = str(txn.get("person") or fallback_name or "UNKNOWN")
        grouped.setdefault(person, []).append(txn)

    grouped_rows: list[dict[str, Any]] = []
    for person, rows in grouped.items():
        total = Decimal("0")
        points_total = Decimal("0")
        for row in rows:
            total += parse_amount(str(row.get("amount") or "0"))
            points_total += parse_points(row.get("reward_points"))
        grouped_rows.append(
            {
                "person": person,
                "transaction_count": len(rows),
                "total_amount": format_amount(total),
                "reward_points_total": str(int(points_total)),
                "transactions": rows,
            }
        )

    grouped_rows.sort(key=lambda item: str(item["person"]))
    return grouped_rows


def _parse_txn_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d/%m/%Y")
    except ValueError:
        return None


def split_paired_adjustments(
    debit_transactions: list[dict[str, str | None]],
    credit_transactions: list[dict[str, str | None]],
) -> tuple[
    list[dict[str, str | None]],
    list[dict[str, str | None]],
    list[dict[str, str | None]],
]:
    """Split offsetting debit/credit pairs into separate adjustments bucket.

    Args:
        debit_transactions: Debit-side parsed transactions.
        credit_transactions: Credit-side parsed transactions.

    Returns:
        Tuple of `(debits, credits, adjustments)` where paired adjustment rows are
        removed from debits/credits and returned in `adjustments`.
    """

    def is_contextual_adjustment_debit(txn: dict[str, str | None]) -> bool:
        narration = str(txn.get("narration") or "").upper()
        reward = str(txn.get("reward_points") or "").strip()
        return "CREDIT BALANCE REFUND" in narration and reward in {"", "0"}

    credit_buckets: dict[tuple[str, str, str], list[int]] = {}
    for idx, txn in enumerate(credit_transactions):
        key = (
            str(txn.get("card_number") or "UNKNOWN"),
            str(txn.get("person") or "UNKNOWN"),
            str(txn.get("amount") or "0"),
        )
        credit_buckets.setdefault(key, []).append(idx)

    used_credit: set[int] = set()
    used_debit: set[int] = set()
    contextual_debit: set[int] = {
        idx
        for idx, txn in enumerate(debit_transactions)
        if is_contextual_adjustment_debit(txn)
    }

    for d_idx, debit in enumerate(debit_transactions):
        reward_token = str(debit.get("reward_points") or "0").strip()
        if reward_token not in {"", "0"}:
            continue

        key = (
            str(debit.get("card_number") or "UNKNOWN"),
            str(debit.get("person") or "UNKNOWN"),
            str(debit.get("amount") or "0"),
        )
        candidates = credit_buckets.get(key, [])
        if not candidates:
            continue

        debit_date = _parse_txn_date(str(debit.get("date") or ""))
        best_idx: int | None = None
        best_distance = 10**9

        for c_idx in candidates:
            if c_idx in used_credit:
                continue
            credit = credit_transactions[c_idx]
            credit_date = _parse_txn_date(str(credit.get("date") or ""))

            if debit_date is None or credit_date is None:
                distance = 999
            else:
                distance = abs((debit_date - credit_date).days)

            if distance <= 15 and distance < best_distance:
                best_distance = distance
                best_idx = c_idx

        if best_idx is not None:
            used_debit.add(d_idx)
            used_credit.add(best_idx)

    adjustments: list[dict[str, str | None]] = []
    used_debit = used_debit | contextual_debit

    for idx in sorted(used_debit):
        txn = dict(debit_transactions[idx])
        txn["adjustment_side"] = "debit"
        if idx in contextual_debit:
            txn["adjustment_reason"] = "credit_balance_refund"
        adjustments.append(txn)
    for idx in sorted(used_credit):
        txn = dict(credit_transactions[idx])
        txn["adjustment_side"] = "credit"
        adjustments.append(txn)

    kept_debits = [
        txn for idx, txn in enumerate(debit_transactions) if idx not in used_debit
    ]
    kept_credits = [
        txn for idx, txn in enumerate(credit_transactions) if idx not in used_credit
    ]
    return kept_debits, kept_credits, adjustments


def _extract_transactions_with_debug(
    pages: list[dict[str, Any]],
) -> tuple[list[dict[str, str | None]], dict[str, Any]]:
    """Parse transactions and capture parser diagnostics.

    Args:
        pages: Raw extractor page payloads.

    Returns:
        Tuple `(transactions, debug)` where debug includes line-level traces.
    """
    transactions: list[dict[str, str | None]] = []
    current_card: str | None = None
    current_member: str | None = None
    date_lines: list[dict[str, Any]] = []
    rejected_date_lines: list[dict[str, Any]] = []
    detected_members: list[dict[str, Any]] = []

    for page in pages:
        page_number = int(page.get("page_number", 0) or 0)
        words = page.get("words") or []
        lines = group_words_into_lines(words)

        for line_index, line_words in enumerate(lines):
            raw_tokens = [str(item.get("text", "")).strip() for item in line_words]
            if not raw_tokens:
                continue

            tokens = [normalize_token(token) for token in raw_tokens]

            member_header = looks_like_member_header(tokens)
            if member_header:
                current_member = member_header
                detected_members.append(
                    {
                        "page": page_number,
                        "line_index": line_index,
                        "member": member_header,
                    }
                )

            line_card, line_member = extract_card_from_line(tokens)
            if line_card:
                current_card = line_card
                if line_member:
                    current_member = line_member

            date_idx = next(
                (
                    i
                    for i, token in enumerate(tokens)
                    if parse_date_token(token) is not None
                ),
                -1,
            )
            if date_idx == -1:
                continue

            date_lines.append(
                {
                    "page": page_number,
                    "line_index": line_index,
                    "tokens": tokens,
                    "current_member": current_member,
                    "current_card": current_card,
                }
            )

            amount_idx = next(
                (
                    i
                    for i in range(len(tokens) - 1, -1, -1)
                    if parse_amount_token(tokens[i]) is not None
                ),
                -1,
            )
            if amount_idx == -1 or amount_idx <= date_idx:
                rejected_date_lines.append(
                    {
                        "page": page_number,
                        "line_index": line_index,
                        "reason": "amount_not_found",
                        "tokens": tokens,
                    }
                )
                continue

            time_value: str | None = None
            cursor = date_idx + 1
            while cursor < len(tokens) and tokens[cursor] in SEPARATOR_TOKENS:
                cursor += 1

            time_idx = next(
                (
                    i
                    for i in range(cursor, min(amount_idx, cursor + 6))
                    if parse_time_token(tokens[i]) is not None
                ),
                -1,
            )
            if time_idx != -1:
                time_value = parse_time_token(tokens[time_idx])
                cursor = time_idx + 1
                while cursor < len(tokens) and tokens[cursor] in SEPARATOR_TOKENS:
                    cursor += 1

            if cursor < len(tokens) and re.fullmatch(r"\d{8,}", tokens[cursor]):
                cursor += 1
                while cursor < len(tokens) and tokens[cursor] in SEPARATOR_TOKENS:
                    cursor += 1

            plus_idx = next(
                (i for i in range(cursor, amount_idx) if tokens[i] == "+"), -1
            )

            reward_value: str | None = None
            reward_idx: int | None = None
            if plus_idx != -1 and plus_idx + 1 < amount_idx:
                maybe_reward = tokens[plus_idx + 1]
                if re.fullmatch(r"\d{1,6}", maybe_reward):
                    reward_value = maybe_reward
                    reward_idx = plus_idx + 1

            if reward_idx is None:
                for i in range(amount_idx - 1, max(cursor - 1, amount_idx - 8), -1):
                    candidate = tokens[i]
                    if re.fullmatch(r"\d{1,5}", candidate):
                        reward_idx = i
                        reward_value = candidate
                        break

            narration_end = plus_idx if plus_idx != -1 else amount_idx
            if reward_idx is not None:
                narration_end = reward_idx

            narration_tokens = [
                token
                for token in tokens[cursor:narration_end]
                if token
                and token not in SEPARATOR_TOKENS
                and token not in {"+", "l", "I"}
            ]

            while narration_tokens and narration_tokens[-1] in {"C", "c"}:
                narration_tokens.pop()

            narration = clean_space(" ".join(narration_tokens))

            if needs_context_merge(narration, narration_tokens):
                prev_ctx_tokens, next_ctx_tokens = collect_row_context_tokens(
                    lines, line_index
                )
                context_narration_tokens = [
                    token
                    for token in [*prev_ctx_tokens, *next_ctx_tokens]
                    if token
                    and token not in SEPARATOR_TOKENS
                    and token not in {"+", "l", "I", "C", "CR", "Cr"}
                    and parse_amount_token(token) is None
                    and parse_date_token(token) is None
                    and parse_time_token(token) is None
                ]
                merged_narration_tokens = [*narration_tokens, *context_narration_tokens]
                narration = clean_space(" ".join(merged_narration_tokens))

            if not narration:
                continuation = extract_continuation_narration(lines, line_index)
                if continuation:
                    narration = continuation

            narration = clean_narration_artifacts(narration)
            narration = enrich_reference_only_narration(lines, line_index, narration)

            if not narration:
                rejected_date_lines.append(
                    {
                        "page": page_number,
                        "line_index": line_index,
                        "reason": "empty_narration",
                        "tokens": tokens,
                    }
                )
                continue

            date_value = parse_date_token(tokens[date_idx])
            amount_value = parse_amount_token(tokens[amount_idx])
            if not date_value or not amount_value:
                rejected_date_lines.append(
                    {
                        "page": page_number,
                        "line_index": line_index,
                        "reason": "date_or_amount_parse_failed",
                        "tokens": tokens,
                    }
                )
                continue

            transactions.append(
                {
                    "date": date_value,
                    "time": time_value,
                    "narration": narration,
                    "reward_points": reward_value,
                    "amount": normalize_amount(amount_value),
                    "card_number": current_card,
                    "person": current_member,
                    "transaction_type": "debit",
                }
            )

            prev_context_tokens: list[str] = []
            for back in (2, 1):
                prev_idx = line_index - back
                if prev_idx < 0:
                    continue
                prev_line_tokens = [
                    normalize_token(str(item.get("text", "")))
                    for item in lines[prev_idx]
                ]
                prev_line_tokens = [token for token in prev_line_tokens if token]
                if any(parse_date_token(token) for token in prev_line_tokens):
                    continue
                prev_context_tokens.extend(prev_line_tokens)

            context_text = clean_space(" ".join(prev_context_tokens))
            is_credit, reasons = classify_credit_transaction(
                tokens, narration, context_text
            )
            if is_credit:
                transactions[-1]["transaction_type"] = "credit"
                transactions[-1]["credit_reasons"] = ",".join(reasons)

    debug = {
        "date_lines": date_lines,
        "rejected_date_lines": rejected_date_lines,
        "detected_members": detected_members,
    }
    return transactions, debug


def extract_transactions(pages: list[dict[str, Any]]) -> list[dict[str, str | None]]:
    """Parse transactions from raw pages.

    Args:
        pages: Raw extractor page payloads.

    Returns:
        Parsed transaction rows (debit + credit rows with type labels).
    """
    transactions, _ = _extract_transactions_with_debug(pages)
    return transactions


def build_card_summaries(
    transactions: list[dict[str, str | None]], fallback_name: str | None
) -> tuple[list[dict[str, str | int]], str]:
    """Build person/card summary totals for parsed transactions.

    Args:
        transactions: Transactions to aggregate.
        fallback_name: Name used when row person is missing.

    Returns:
        Tuple of `(summary_rows, overall_total_amount)`.
    """
    grouped: dict[tuple[str, str], dict[str, Any]] = {}

    for txn in transactions:
        card_number = txn.get("card_number") or "UNKNOWN"
        person = txn.get("person") or fallback_name or "UNKNOWN"
        key = (card_number, person)

        if key not in grouped:
            grouped[key] = {
                "card_number": card_number,
                "person": person,
                "total": Decimal("0"),
                "points_total": Decimal("0"),
                "transaction_count": 0,
            }

        grouped[key]["total"] += parse_amount(str(txn.get("amount") or "0"))
        grouped[key]["points_total"] += parse_points(txn.get("reward_points"))
        grouped[key]["transaction_count"] += 1

    summaries: list[dict[str, str | int]] = []
    overall_total = Decimal("0")
    for item in grouped.values():
        total = item["total"]
        overall_total += total
        summaries.append(
            {
                "card_number": str(item["card_number"]),
                "person": str(item["person"]),
                "transaction_count": int(item["transaction_count"]),
                "total_amount": format_amount(total),
                "reward_points_total": str(int(item["points_total"])),
            }
        )

    summaries.sort(key=lambda row: (str(row["person"]), str(row["card_number"])))
    return summaries, format_amount(overall_total)


class GenericParser(StatementParser):
    """Default parser implementation shared by bank profiles."""

    bank = "generic"

    def parse(self, raw_data: dict[str, Any]) -> dict[str, Any]:
        """Normalize raw extractor payload into compact statement output.

        Args:
            raw_data: Output from `extract_raw_pdf`.

        Returns:
            Normalized statement dictionary used by CLI/JSON output.
        """
        full_text = "\n".join(
            str(page.get("text", "")) for page in raw_data.get("pages", [])
        )
        name = extract_name(full_text)
        transactions, _ = _extract_transactions_with_debug(raw_data.get("pages", []))

        detected_card = extract_card_number(full_text) or extract_card_from_filename(
            str(raw_data["file"])
        )
        if detected_card:
            for txn in transactions:
                if not txn.get("card_number"):
                    txn["card_number"] = detected_card

        for txn in transactions:
            person_value = str(txn.get("person") or "").strip()
            if is_invalid_person_label(person_value):
                txn["person"] = name

        debit_transactions = [
            txn
            for txn in transactions
            if str(txn.get("transaction_type") or "debit") != "credit"
        ]
        credit_transactions = [
            txn
            for txn in transactions
            if str(txn.get("transaction_type") or "debit") == "credit"
        ]

        debit_transactions, credit_transactions, adjustments = split_paired_adjustments(
            debit_transactions,
            credit_transactions,
        )

        card_summaries, overall_total = build_card_summaries(debit_transactions, name)
        person_groups = group_transactions_by_person(debit_transactions, name)

        credit_total = Decimal("0")
        for txn in credit_transactions:
            credit_total += parse_amount(str(txn.get("amount") or "0"))

        overall_reward_points = Decimal("0")
        for txn in debit_transactions:
            overall_reward_points += parse_points(txn.get("reward_points"))

        adjustments_debit_total = Decimal("0")
        adjustments_credit_total = Decimal("0")
        for txn in adjustments:
            side = str(txn.get("adjustment_side") or "")
            amount = parse_amount(str(txn.get("amount") or "0"))
            if side == "debit":
                adjustments_debit_total += amount
            elif side == "credit":
                adjustments_credit_total += amount

        due_date = extract_due_date(full_text) or extract_due_date_from_pages(
            raw_data.get("pages", [])
        )
        statement_total_amount_due = extract_total_amount_due(full_text)
        summary_fields = extract_statement_summary(full_text)
        reconciliation = build_reconciliation(
            statement_total_amount_due,
            debit_transactions,
            credit_transactions,
            summary_fields,
        )

        return {
            "file": raw_data["file"],
            "bank": self.bank,
            "name": name,
            "card_number": detected_card,
            "due_date": due_date,
            "statement_total_amount_due": statement_total_amount_due,
            "card_summaries": card_summaries,
            "overall_total": overall_total,
            "person_groups": person_groups,
            "payments_refunds": credit_transactions,
            "payments_refunds_total": format_amount(credit_total),
            "adjustments": adjustments,
            "adjustments_debit_total": format_amount(adjustments_debit_total),
            "adjustments_credit_total": format_amount(adjustments_credit_total),
            "overall_reward_points": str(int(overall_reward_points)),
            "transactions": debit_transactions,
            "reconciliation": reconciliation,
        }

    def build_debug(self, raw_data: dict[str, Any]) -> dict[str, Any]:
        """Build detailed parser diagnostics for troubleshooting mode.

        Args:
            raw_data: Output from `extract_raw_pdf`.

        Returns:
            Debug dictionary with markers, parsed lines, and parser stats.
        """
        pages = raw_data.get("pages", [])
        transactions, txn_debug = _extract_transactions_with_debug(pages)

        interesting_lines: list[dict[str, Any]] = []
        section_markers: list[dict[str, Any]] = []
        card_candidates: list[dict[str, Any]] = []

        for page in pages:
            page_number = int(page.get("page_number", 0) or 0)
            text = str(page.get("text", ""))
            for card in find_card_candidates(text):
                card_candidates.append({"page": page_number, "card": card})

            lines = group_words_into_lines(page.get("words") or [])
            for idx, line_words in enumerate(lines[:800]):
                tokens = [
                    normalize_token(str(item.get("text", ""))) for item in line_words
                ]
                joined = clean_space(" ".join(tokens))
                if not joined:
                    continue

                has_date = any(parse_date_token(token) for token in tokens)
                has_mask = any("X" in token.upper() or "*" in token for token in tokens)
                if has_date or has_mask:
                    interesting_lines.append(
                        {
                            "page": page_number,
                            "line_index": idx,
                            "tokens": tokens,
                            "text": joined,
                        }
                    )

                upper_joined = joined.upper()
                if any(
                    marker in upper_joined
                    for marker in [
                        "DOMESTIC TRANSACTIONS",
                        "INTERNATIONAL TRANSACTIONS",
                        "DUE DATE",
                        "CREDIT CARD NO",
                    ]
                ):
                    section_markers.append(
                        {
                            "page": page_number,
                            "line_index": idx,
                            "text": joined,
                        }
                    )

        return {
            "bank": self.bank,
            "stats": {
                "page_count": len(pages),
                "transactions_parsed": len(transactions),
                "credit_transactions": len(
                    [
                        txn
                        for txn in transactions
                        if str(txn.get("transaction_type") or "debit") == "credit"
                    ]
                ),
                "date_lines_seen": len(txn_debug["date_lines"]),
                "date_lines_rejected": len(txn_debug["rejected_date_lines"]),
                "member_headers_detected": len(txn_debug["detected_members"]),
            },
            "card_from_filename": extract_card_from_filename(
                str(raw_data.get("file", ""))
            ),
            "card_candidates": card_candidates,
            "section_markers": section_markers,
            "detected_members": txn_debug["detected_members"],
            "date_lines": txn_debug["date_lines"],
            "rejected_date_lines": txn_debug["rejected_date_lines"],
            "interesting_lines": interesting_lines[:1000],
        }
