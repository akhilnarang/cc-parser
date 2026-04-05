"""Transaction extraction from raw PDF pages.

Reconstructs visual lines from PDF word coordinates and extracts
structured transaction rows with date, time, narration, amount,
reward points, and credit/debit classification.
"""

import re
from typing import Any

from cc_parser.parsers.models import Transaction
from cc_parser.parsers.tokens import (
    SEPARATOR_TOKENS,
    clean_space,
    normalize_token,
    normalize_amount,
    parse_amount_token,
    parse_date_token,
    parse_time_token,
)
from cc_parser.parsers.cards import (
    extract_card_from_line,
    looks_like_member_header,
)
from cc_parser.parsers.narration import (
    clean_narration_artifacts,
    collect_row_context_tokens,
    enrich_reference_only_narration,
    extract_continuation_narration,
    needs_context_merge,
)

_SUMMARY_KEYWORDS = {
    "TOTAL",
    "SUBTOTAL",
    "CARD TOTAL",
    "TOTAL DOMESTIC",
    "TOTAL INTERNATIONAL",
    "STATEMENT DATE",
    "OPENING BALANCE",
    "CLOSING BALANCE",
    "TOTAL SPENDS",
    "AMOUNT DUE",
    "MINIMUM DUE",
}


def _is_summary_row(narration: str) -> bool:
    """Return True when *narration* looks like a summary/footer row.

    Rejects rows where the narration text (between date and amount):
    - contains NO alphabetic characters (purely numeric like ``"9"``),
    - matches common statement summary keywords, or
    - is a single small integer (the date + count + amount pattern).
    """
    stripped = narration.strip()
    if not stripped:
        return False

    # Reject narrations with no alphabetic characters at all
    if not any(ch.isalpha() for ch in stripped):
        return True

    upper = stripped.upper()

    # Reject known summary keyword patterns
    for keyword in _SUMMARY_KEYWORDS:
        if keyword in upper:
            return True

    # Reject "single small integer" narrations (e.g. "9", "12")
    # These appear when a page/count number sits between date and amount.
    if re.fullmatch(r"\d{1,4}", stripped):
        return True

    return False


def group_words_into_lines(
    words: list[dict[str, Any]], y_tolerance: float = 1.8
) -> list[list[dict[str, Any]]]:
    """Group extracted PDF words into visual lines.

    Args:
        words: Word dictionaries from extractor (must include x0/doctop).
        y_tolerance: Vertical tolerance to merge words into one line.
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


def classify_credit_transaction(tokens: list[str]) -> tuple[bool, list[str]]:
    """Classify a parsed row as credit using structural markers.

    Returns:
        Tuple ``(is_credit, reasons)`` where reasons contains matched markers.
    """
    reasons: list[str] = []

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

        if next_upper in {"C", "CR"}:
            reasons.append("plus_amount_marker")
            break
        if parse_amount_token(next_token) is not None:
            reasons.append("plus_amount_marker")
            break

        if re.fullmatch(r"\d{1,6}", next_token):
            continue

    return (len(reasons) > 0), reasons


def _extract_reward_points(
    tokens: list[str], cursor: int, amount_idx: int
) -> tuple[str | None, int | None, int]:
    """Extract reward points value and determine narration end boundary.

    Returns:
        Tuple ``(reward_value, reward_idx, narration_end)``.
    """
    plus_idx = next((i for i in range(cursor, amount_idx) if tokens[i] == "+"), -1)

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

    return reward_value, reward_idx, narration_end


def _build_narration(
    tokens: list[str],
    cursor: int,
    narration_end: int,
    lines: list[list[dict[str, Any]]],
    line_index: int,
) -> str:
    """Build narration text from tokens, merging wrapped context as needed."""
    narration_tokens = [
        token
        for token in tokens[cursor:narration_end]
        if token and token not in SEPARATOR_TOKENS and token not in {"+", "l", "I"}
    ]

    while narration_tokens and narration_tokens[-1] in {"C", "c"}:
        narration_tokens.pop()

    narration = clean_space(" ".join(narration_tokens))

    if needs_context_merge(narration):
        prev_ctx_tokens, next_ctx_tokens = collect_row_context_tokens(lines, line_index)
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

    return narration


def _extract_transactions_with_debug(
    pages: list[dict[str, Any]],
) -> tuple[list[Transaction], dict[str, Any]]:
    """Parse transactions and capture parser diagnostics."""
    transactions: list[Transaction] = []
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

            # Extract time token
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

            # Skip long numeric reference tokens
            if cursor < len(tokens) and re.fullmatch(r"\d{8,}", tokens[cursor]):
                cursor += 1
                while cursor < len(tokens) and tokens[cursor] in SEPARATOR_TOKENS:
                    cursor += 1

            # Extract reward points and narration boundary
            reward_value, _reward_idx, narration_end = _extract_reward_points(
                tokens, cursor, amount_idx
            )

            # Build narration
            narration = _build_narration(
                tokens, cursor, narration_end, lines, line_index
            )

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

            if _is_summary_row(narration):
                rejected_date_lines.append(
                    {
                        "page": page_number,
                        "line_index": line_index,
                        "reason": "summary_row",
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

            is_credit, reasons = classify_credit_transaction(tokens)
            transactions.append(
                Transaction(
                    date=date_value,
                    time=time_value,
                    narration=narration,
                    reward_points=reward_value,
                    amount=normalize_amount(amount_value),
                    card_number=current_card,
                    person=current_member,
                    transaction_type="credit" if is_credit else "debit",
                    credit_reasons=",".join(reasons) if is_credit else None,
                )
            )

    debug = {
        "date_lines": date_lines,
        "rejected_date_lines": rejected_date_lines,
        "detected_members": detected_members,
    }
    return transactions, debug


def extract_transactions(pages: list[dict[str, Any]]) -> list[Transaction]:
    """Parse transactions from raw pages."""
    transactions, _ = _extract_transactions_with_debug(pages)
    return transactions


__all__ = [
    "group_words_into_lines",
    "classify_credit_transaction",
    "extract_transactions",
]
