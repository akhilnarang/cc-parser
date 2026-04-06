"""Axis Bank parser profile.

Axis credit card statements differ from other banks in several ways:
- Dates are ``DD/MM/YYYY`` (single token)
- Credit/debit markers are ``Cr`` / ``Dr`` (case varies) as the last token
- Card numbers use ``*`` masking (e.g. ``NNNNNN******NNNN``)
- Member header: ``Card No: NNNNNN******NNNN Name FULL NAME``
- Flipkart variant has a CASHBACK EARNED column (``0.00 Dr``)
- Account summary equation:
  ``Previous Balance - Payments - Credits + Purchase + Cash Advance
  + Other Debit&Charges = Total Payment Due``
  with values on the next line
- End marker: ``**** End of Statement ****``
- Due date in PAYMENT SUMMARY header area (``DD/MM/YYYY``)
- Total Payment Due in PAYMENT SUMMARY: ``2,501.00 Dr`` or ``7.00 Cr``
"""

import re
from decimal import Decimal
from typing import Any

from cc_parser.parsers.base import StatementParser
from cc_parser.parsers.cards import (
    extract_card_from_filename,
    find_card_candidates,
    normalize_card_token,
    normalize_transaction_persons,
    split_by_transaction_type,
)
from cc_parser.parsers.extraction import group_words_into_lines
from cc_parser.parsers.models import ParsedStatement, StatementSummary, Transaction
from cc_parser.parsers.narration import (
    clean_narration_artifacts,
    collect_row_context_tokens,
    extract_continuation_narration,
    needs_context_merge,
)
from cc_parser.parsers.reconciliation import (
    build_card_summaries,
    build_reconciliation,
    detect_adjustment_pairs,
    group_transactions_by_person,
)
from cc_parser.parsers.transaction_id_generator import assign_transaction_ids
from cc_parser.parsers.tokens import (
    SEPARATOR_TOKENS,
    clean_space,
    format_amount,
    normalize_amount,
    normalize_token,
    parse_amount,
    parse_amount_token,
    parse_date_token,
    sum_amounts,
    sum_points,
)

# Merchant category tokens that appear between narration and amount.
# These are stripped from narration to keep it clean.
_MERCHANT_CATEGORIES = {
    "DEPT",
    "STORES",
    "ENTERTAINMENT",
    "GROCERY",
    "TRAVEL",
    "FUEL",
    "UTILITY",
    "DINING",
    "INSURANCE",
    "EDUCATION",
    "HEALTH",
    "TELECOM",
    "GOVERNMENT",
    "AIRLINES",
    "HOTELS",
    "SHOPPING",
    "JEWELLERY",
    "ELECTRONICS",
    "UTILITIES",
    "MEDICAL",
    "FOOD",
    "RESTAURANT",
    "SERVICES",
    "OTHERS",
}


def _has_cr_dr_marker(token: str) -> str | None:
    """Return ``CR`` or ``DR`` if token is a credit/debit marker (case-insensitive)."""
    upper = token.strip().upper()
    if upper in {"CR", "C"}:
        return "CR"
    if upper in {"DR", "D"}:
        return "DR"
    return None


def _format_card(raw: str) -> str:
    """Format a normalized card token into spaced groups of 4."""
    digits = raw.replace(" ", "")
    if len(digits) < 16:
        digits = "X" * (16 - len(digits)) + digits
    return f"{digits[:4]} {digits[4:8]} {digits[8:12]} {digits[12:16]}"


def _extract_axis_name(full_text: str, pages: list[dict[str, Any]]) -> str | None:
    """Extract cardholder name from Axis statement.

    Axis prints the name after ``Card No: XXXX Name FULL NAME`` in the
    transaction section header, or as a bare name line near the top.
    """
    # Try "Card No: ... Name FULL NAME" pattern from word tokens
    for page in pages[:2]:
        lines = group_words_into_lines(page.get("words") or [])
        for line_words in lines:
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens)).upper()
            if "CARD NO:" not in joined and "CARD NO :" not in joined:
                continue
            # Find the "Name" keyword and take everything after it
            name_match = re.search(
                r"(?:CARD\s+NO\s*:\s*\S+\s+)NAME\s+(.+)",
                joined,
            )
            if name_match:
                candidate = clean_space(name_match.group(1)).upper()
                parts = candidate.split()
                if 2 <= len(parts) <= 5 and all(
                    re.fullmatch(r"[A-Z][A-Z.'-]*", p) for p in parts
                ):
                    return candidate

    # Fallback: bare name on line after statement title
    if pages:
        lines = group_words_into_lines(pages[0].get("words") or [])
        for i, line_words in enumerate(lines):
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens)).upper()
            if "CREDIT CARD STATEMENT" in joined and i + 1 < len(lines):
                next_tokens = [
                    normalize_token(str(w.get("text", ""))) for w in lines[i + 1]
                ]
                candidate = clean_space(" ".join(next_tokens)).upper()
                parts = candidate.split()
                if 2 <= len(parts) <= 5 and all(
                    re.fullmatch(r"[A-Z][A-Z.'-]*", p) for p in parts
                ):
                    return candidate

    return None


def _extract_axis_card_number(
    full_text: str, pages: list[dict[str, Any]]
) -> str | None:
    """Extract card number from Axis statement.

    Axis uses ``Card No: NNNNNN******NNNN`` in the transaction header
    and ``Credit Card Number`` section near the top.
    """
    # Word-level: "Card No:" line
    for page in pages[:2]:
        lines = group_words_into_lines(page.get("words") or [])
        for line_words in lines:
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens)).upper()
            if "CARD NO:" not in joined and "CARD NO :" not in joined:
                continue
            # Find the card token right after "No:"
            for idx, t in enumerate(tokens):
                if t.upper().rstrip(":") == "NO" or t.upper() == "NO:":
                    # Next token should be the card number (or ":" then card)
                    search_start = idx + 1
                    if search_start < len(tokens) and tokens[search_start] == ":":
                        search_start += 1
                    if search_start < len(tokens):
                        raw = normalize_card_token(tokens[search_start])
                        if len(raw) >= 10:
                            return _format_card(raw)

    # "Credit Card Number" section: card token on next line
    for page in pages[:2]:
        lines = group_words_into_lines(page.get("words") or [])
        for i, line_words in enumerate(lines):
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens)).upper()
            if "CREDIT CARD NUMBER" in joined and i + 2 < len(lines):
                # Card number is usually 2 lines below (skipping label row)
                for offset in range(1, 4):
                    if i + offset >= len(lines):
                        break
                    for w in lines[i + offset]:
                        t = normalize_token(str(w.get("text", "")))
                        raw = normalize_card_token(t)
                        if len(raw) >= 10:
                            return _format_card(raw)

    # Fallback: generic card detection
    candidates = find_card_candidates(full_text)
    return candidates[0] if candidates else None


def _extract_axis_due_date(full_text: str, pages: list[dict[str, Any]]) -> str | None:
    """Extract payment due date from Axis statement.

    Axis has a PAYMENT SUMMARY header row with ``Payment Due Date``
    and the value on the next line in DD/MM/YYYY format.
    """
    for page in pages[:2]:
        lines = group_words_into_lines(page.get("words") or [])
        for i, line_words in enumerate(lines):
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens)).upper()
            if "PAYMENT DUE DATE" not in joined:
                continue

            # Find x-position of "Payment" before "Due Date"
            # to locate the correct date on the values line
            due_x: float | None = None
            for wi, w in enumerate(line_words):
                t = normalize_token(str(w.get("text", ""))).upper()
                if t == "PAYMENT":
                    # Check if followed by "Due" and "Date"
                    remaining = " ".join(
                        normalize_token(str(line_words[j].get("text", ""))).upper()
                        for j in range(wi, min(wi + 3, len(line_words)))
                    )
                    if "PAYMENT DUE DATE" in remaining:
                        due_x = float(w.get("x0", 0))
                        # Don't break — we want the last "Payment Due Date"
                        # occurrence (first one is "Total Payment Due")

            # Check next line for DD/MM/YYYY tokens
            if i + 1 < len(lines):
                next_words = lines[i + 1]
                # Find date token closest to due_x
                best_date: str | None = None
                best_dist = float("inf")
                for w in next_words:
                    t = normalize_token(str(w.get("text", "")))
                    dt = parse_date_token(t)
                    if dt:
                        if due_x is not None:
                            dist = abs(float(w.get("x0", 0)) - due_x)
                            if dist < best_dist:
                                best_dist = dist
                                best_date = dt
                        else:
                            best_date = dt
                if best_date:
                    return best_date

    # Text-level fallback
    match = re.search(
        r"Payment\s+Due\s+Date.*?(\d{2}/\d{2}/\d{4})",
        full_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        return match.group(1)

    return None


def _extract_axis_total_amount_due(
    full_text: str, pages: list[dict[str, Any]]
) -> str | None:
    """Extract total payment due from Axis statement.

    Axis PAYMENT SUMMARY has header row:
      ``Total Payment Due  Minimum Payment Due  Statement Period  Payment Due Date  ...``
    Values row:
      ``2,501.00 Dr  100.00 Dr  12/02/2026 - 10/03/2026  30/03/2026  ...``
    or ``7.00 Cr  0.00 Cr  ...``

    The first amount + Cr/Dr pair on the values line is the total payment due.
    A ``Cr`` suffix means a credit balance (overpayment) — stored as negative.
    """
    for page in pages[:2]:
        lines = group_words_into_lines(page.get("words") or [])
        for i, line_words in enumerate(lines):
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens)).upper()

            # Look for the header row with "Total Payment Due"
            if "TOTAL PAYMENT DUE" not in joined:
                continue
            if "MINIMUM PAYMENT DUE" not in joined:
                continue

            # Values are on the next line
            if i + 1 >= len(lines):
                continue

            next_tokens = [
                normalize_token(str(w.get("text", ""))) for w in lines[i + 1]
            ]

            # First amount token followed by Cr/Dr
            for ti, t in enumerate(next_tokens):
                # Handle merged tokens like "7.00Cr" or "2501.00Dr"
                raw = t
                merged_marker: str | None = None
                if raw.upper().endswith(("CR", "DR")) and len(raw) > 2:
                    merged_marker = raw[-2:].upper()
                    raw = raw[:-2]
                amt = parse_amount_token(raw)
                if amt:
                    is_credit = False
                    if merged_marker:
                        is_credit = merged_marker == "CR"
                    elif ti + 1 < len(next_tokens):
                        marker = _has_cr_dr_marker(next_tokens[ti + 1])
                        if marker == "CR":
                            is_credit = True
                    result = normalize_amount(amt)
                    if is_credit:
                        result = f"-{result}"
                    return result

    # Text-level fallback: "Total Payment Due" near amount
    match = re.search(
        r"Total\s+Payment\s+Due.*?([\d,]+\.\d{2})\s*(Cr|Dr)?",
        full_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        result = normalize_amount(match.group(1))
        if match.group(2) and match.group(2).upper() == "CR":
            result = f"-{result}"
        return result

    return None


def _is_end_of_statement(tokens: list[str]) -> bool:
    """Check if a line is the end-of-statement marker."""
    joined = clean_space(" ".join(tokens)).upper()
    return "END OF STATEMENT" in joined


def _extract_axis_transactions(
    pages: list[dict[str, Any]],
) -> tuple[list[Transaction], dict[str, Any]]:
    """Parse transactions from Axis statement pages.

    Axis transaction lines follow the pattern:
    ``DD/MM/YYYY  NARRATION  [MERCHANT_CATEGORY]  AMOUNT  Cr/Dr  [CASHBACK_AMOUNT  Cr/Dr]``

    Parsing stops at ``**** End of Statement ****``.
    """
    transactions: list[Transaction] = []
    current_card: str | None = None
    current_member: str | None = None
    in_transaction_section = False
    has_cashback_column = False
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

            tokens = [normalize_token(t) for t in raw_tokens]
            joined_upper = clean_space(" ".join(tokens)).upper()

            # Stop at end-of-statement marker
            if _is_end_of_statement(tokens):
                in_transaction_section = False
                continue

            # Detect table header to know if CASHBACK column exists
            if "TRANSACTION DETAILS" in joined_upper and "AMOUNT" in joined_upper:
                in_transaction_section = True
                has_cashback_column = "CASHBACK" in joined_upper
                continue

            # Detect "Card No:" member header
            if "CARD NO:" in joined_upper or "CARD NO :" in joined_upper:
                # Extract card number
                for idx, t in enumerate(tokens):
                    if t.upper().rstrip(":") == "NO" or t.upper() == "NO:":
                        search_start = idx + 1
                        if search_start < len(tokens) and tokens[search_start] == ":":
                            search_start += 1
                        if search_start < len(tokens):
                            raw = normalize_card_token(tokens[search_start])
                            if len(raw) >= 10:
                                current_card = _format_card(raw)
                                break

                # Extract name after "Name" keyword
                name_match = re.search(
                    r"NAME\s+(.+)",
                    joined_upper,
                )
                if name_match:
                    candidate = clean_space(name_match.group(1)).upper()
                    parts = candidate.split()
                    if 2 <= len(parts) <= 5 and all(
                        re.fullmatch(r"[A-Z][A-Z.'-]*", p) for p in parts
                    ):
                        current_member = candidate

                detected_members.append(
                    {
                        "page": page_number,
                        "line_index": line_index,
                        "member": current_member,
                        "card": current_card,
                    }
                )
                in_transaction_section = True
                continue

            # Only parse transactions in the transaction section
            if not in_transaction_section:
                continue

            # Try to parse date at start of line (DD/MM/YYYY)
            date_value = parse_date_token(tokens[0])
            if date_value is None:
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

            # Find the amount and Cr/Dr marker.
            # Axis format: ... AMOUNT Cr/Dr [CASHBACK_AMOUNT Cr/Dr]
            # We need the FIRST amount+marker pair (the transaction amount),
            # not the cashback column.
            #
            # Strategy: scan from position 1 rightward, find amount tokens
            # followed by Cr/Dr markers. The first such pair is the
            # transaction amount. If there's a cashback column, there will
            # be a second pair which we skip.

            amount_value: str | None = None
            is_credit = False
            amount_idx = -1

            # Find all (amount_idx, marker) pairs
            amount_pairs: list[tuple[int, str]] = []
            ti = 1
            while ti < len(tokens):
                raw_tok = tokens[ti]
                # Handle merged tokens like "2501.00Dr" or "7.00Cr"
                merged_marker: str | None = None
                if raw_tok.upper().endswith(("CR", "DR")) and len(raw_tok) > 2:
                    merged_marker = raw_tok[-2:].upper()
                    raw_tok = raw_tok[:-2]
                amt = (
                    parse_amount_token(raw_tok)
                    if merged_marker
                    else parse_amount_token(tokens[ti])
                )
                if amt and merged_marker:
                    amount_pairs.append((ti, merged_marker))
                    ti += 1
                    continue
                if amt and ti + 1 < len(tokens):
                    marker = _has_cr_dr_marker(tokens[ti + 1])
                    if marker:
                        amount_pairs.append((ti, marker))
                        ti += 2  # skip past marker
                        continue
                # Also handle amount as last token without separate marker
                # (shouldn't normally happen for Axis, but handle gracefully)
                if amt and ti == len(tokens) - 1:
                    amount_pairs.append((ti, "DR"))  # default to debit
                ti += 1

            # If we have cashback column, the first pair is the main amount
            # and the second is the cashback. Without cashback, the last
            # pair before any non-amount noise is the main amount.
            if has_cashback_column and len(amount_pairs) >= 1:
                # First pair is the transaction amount
                main_pair = amount_pairs[0]
                amount_idx = main_pair[0]
                is_credit = main_pair[1] == "CR"
                raw_amt_tok = tokens[amount_idx]
                if raw_amt_tok.upper().endswith(("CR", "DR")) and len(raw_amt_tok) > 2:
                    raw_amt_tok = raw_amt_tok[:-2]
                amount_value = parse_amount_token(raw_amt_tok)
            elif amount_pairs:
                # Without cashback: use the last amount+marker pair,
                # as merchant category tokens don't have Cr/Dr after them
                main_pair = amount_pairs[-1]
                amount_idx = main_pair[0]
                is_credit = main_pair[1] == "CR"
                raw_amt_tok = tokens[amount_idx]
                if raw_amt_tok.upper().endswith(("CR", "DR")) and len(raw_amt_tok) > 2:
                    raw_amt_tok = raw_amt_tok[:-2]
                amount_value = parse_amount_token(raw_amt_tok)

            if amount_value is None or amount_idx <= 0:
                # Fallback: find rightmost amount token
                for ri in range(len(tokens) - 1, 0, -1):
                    t = tokens[ri]
                    if t.upper() in {"CR", "DR", "C", "D"}:
                        continue
                    # Handle merged Cr/Dr suffix
                    fallback_marker: str | None = None
                    if t.upper().endswith(("CR", "DR")) and len(t) > 2:
                        fallback_marker = t[-2:].upper()
                        t = t[:-2]
                    amt = parse_amount_token(t)
                    if amt:
                        amount_value = amt
                        amount_idx = ri
                        if fallback_marker:
                            is_credit = fallback_marker == "CR"
                        elif ri + 1 < len(tokens):
                            # Check next token for marker
                            marker = _has_cr_dr_marker(tokens[ri + 1])
                            if marker:
                                is_credit = marker == "CR"
                        break

            if amount_value is None or amount_idx <= 0:
                rejected_date_lines.append(
                    {
                        "page": page_number,
                        "line_index": line_index,
                        "reason": "amount_not_found",
                        "tokens": tokens,
                    }
                )
                continue

            # Narration: everything between date token and amount,
            # excluding merchant category tokens at the end
            narration_end = amount_idx
            narration_tokens_raw = tokens[1:narration_end]

            # Strip trailing merchant category tokens
            while narration_tokens_raw:
                last = narration_tokens_raw[-1].upper()
                if last in _MERCHANT_CATEGORIES:
                    narration_tokens_raw.pop()
                else:
                    break

            narration_tokens_clean = [
                t
                for t in narration_tokens_raw
                if t
                and t not in SEPARATOR_TOKENS
                and t not in {"+", "l", "I"}
                and t.upper() not in {"DR", "CR"}
            ]

            narration = clean_space(" ".join(narration_tokens_clean))

            # Merge context from wrapped lines if narration is incomplete
            if needs_context_merge(narration):
                prev_ctx, next_ctx = collect_row_context_tokens(lines, line_index)
                ctx_tokens = [
                    t
                    for t in [*prev_ctx, *next_ctx]
                    if t
                    and t not in SEPARATOR_TOKENS
                    and t not in {"+", "l", "I", "CR", "DR", "Cr", "Dr"}
                    and parse_amount_token(t) is None
                    and parse_date_token(t) is None
                ]
                narration = clean_space(
                    " ".join([*narration_tokens_clean, *ctx_tokens])
                )

            if not narration:
                continuation = extract_continuation_narration(lines, line_index)
                if continuation:
                    narration = continuation

            narration = clean_narration_artifacts(narration)

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

            credit_reason = None
            if is_credit:
                credit_reason = "cr_marker"

            transactions.append(
                Transaction(
                    date=date_value,
                    narration=narration,
                    amount=normalize_amount(amount_value),
                    card_number=current_card,
                    person=current_member,
                    transaction_type="credit" if is_credit else "debit",
                    credit_reasons=credit_reason,
                )
            )

    debug = {
        "date_lines": date_lines,
        "rejected_date_lines": rejected_date_lines,
        "detected_members": detected_members,
    }
    return transactions, debug


def _extract_axis_account_summary(
    pages: list[dict[str, Any]],
) -> StatementSummary:
    """Extract account summary values from Axis statement.

    Axis has an equation header:
    ``Previous Balance - Payments - Credits + Purchase + Cash Advance
    + Other Debit&Charges = Total Payment Due``

    Values line (next line):
    ``4,230.00 Dr  4,230.00  0.00  2,501.00  0.00  0.00  2,501.00 Dr``

    Columns: [prev_balance, payments, credits, purchases, cash_advance,
              other_charges, total_due]

    The ``Dr`` markers on prev_balance and total_due are separate tokens.
    """
    for page in pages[:2]:
        words = page.get("words") or []
        if not words:
            continue

        lines = group_words_into_lines(words)

        for i, line_words in enumerate(lines):
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined_upper = clean_space(" ".join(tokens)).upper()

            # Detect the equation header
            if (
                "PREVIOUS BALANCE" not in joined_upper
                or "PAYMENTS" not in joined_upper
                or "PURCHASE" not in joined_upper
            ):
                continue

            # Values are on one of the next few lines
            for offset in range(1, 5):
                idx = i + offset
                if idx >= len(lines):
                    break

                val_tokens = [
                    normalize_token(str(w.get("text", ""))) for w in lines[idx]
                ]

                # Collect just the amount tokens (skip Cr/Dr markers
                # and any trailing text like "over years with...")
                amounts: list[str] = []
                for vt in val_tokens:
                    if _has_cr_dr_marker(vt):
                        continue
                    amt = parse_amount_token(vt)
                    if amt:
                        amounts.append(normalize_amount(amt))
                    elif amounts:
                        # Stop collecting once we hit non-amount,
                        # non-marker tokens (trailing text)
                        break

                if len(amounts) >= 4:
                    # [prev_balance, payments, credits, purchases,
                    #  cash_advance, other_charges, total_due]
                    # Sum payments + credits for the received total
                    payments_val = parse_amount(amounts[1])
                    credits_val = (
                        parse_amount(amounts[2]) if len(amounts) > 2 else Decimal("0")
                    )
                    combined_credits = format_amount(payments_val + credits_val)
                    result = StatementSummary(
                        summary_amount_candidates=amounts,
                        previous_statement_dues=amounts[0],
                        payments_credits_received=combined_credits,
                        purchases_debit=amounts[3] if len(amounts) > 3 else None,
                    )
                    return result

    return StatementSummary()


def _extract_axis_reward_points(
    full_text: str, pages: list[dict[str, Any]]
) -> str | None:
    """Extract reward points balance from Axis statement.

    The Amex Privilege variant has an ``eDGE REWARD POINTS`` section
    with the current balance. The Flipkart variant uses cashback instead.
    """
    # Text-level: "eDGE REWARD" header — value on same or nearby line.
    # The "POINTS" word may be on a different line or absent entirely.
    match = re.search(
        r"(?:EDGE|eDGE)\s+REWARD[\s\S]*?(\d[\d,]{2,})",
        full_text,
        flags=re.IGNORECASE,
    )
    if match:
        candidate = match.group(1).replace(",", "")
        # Sanity: reward points balance should be a reasonable integer
        if candidate.isdigit() and int(candidate) < 10_000_000:
            return candidate

    # Word-level: look for "eDGE REWARD POINTS" header with value below
    for page in pages[:2]:
        words = page.get("words") or []
        if not words:
            continue
        lines = group_words_into_lines(words)
        for i, line_words in enumerate(lines):
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined_upper = clean_space(" ".join(tokens)).upper()
            if "EDGE" in joined_upper and "REWARD" in joined_upper:
                # Value may be on same or next line
                for t in tokens:
                    if re.fullmatch(r"\d[\d,]*", t) and len(t.replace(",", "")) >= 2:
                        return t.replace(",", "")
                if i + 1 < len(lines):
                    for w in lines[i + 1]:
                        t = normalize_token(str(w.get("text", "")))
                        if (
                            re.fullmatch(r"\d[\d,]*", t)
                            and len(t.replace(",", "")) >= 2
                        ):
                            return t.replace(",", "")

    return None


class AxisParser(StatementParser):
    """Parser entrypoint for Axis Bank statements."""

    bank = "axis"

    def __init__(self) -> None:
        self._last_txn_debug: dict[str, Any] | None = None
        self._last_transactions: list[Transaction] | None = None

    def parse(self, raw_data: dict[str, Any]) -> ParsedStatement:
        full_text = "\n".join(
            str(page.get("text", "")) for page in raw_data.get("pages", [])
        )
        pages = raw_data.get("pages", [])

        name = _extract_axis_name(full_text, pages)
        card_number = _extract_axis_card_number(
            full_text, pages
        ) or extract_card_from_filename(str(raw_data["file"]))

        transactions, txn_debug = _extract_axis_transactions(pages)
        self._last_txn_debug = txn_debug
        self._last_transactions = transactions

        # Fill in card number for transactions missing it
        if card_number:
            for txn in transactions:
                if not txn.card_number:
                    txn.card_number = card_number

        # Fill in person from name
        if name:
            for txn in transactions:
                if not txn.person:
                    txn.person = name

        # Fix invalid person labels
        normalize_transaction_persons(transactions, name)

        debit_transactions, credit_transactions = split_by_transaction_type(
            transactions
        )

        # Assign transaction IDs
        debit_transactions = assign_transaction_ids(debit_transactions, self.bank)
        credit_transactions = assign_transaction_ids(credit_transactions, self.bank)

        # Detect adjustment pairs
        adjustment_pairs = detect_adjustment_pairs(
            debit_transactions, credit_transactions, self.bank
        )

        card_summaries, overall_total = build_card_summaries(debit_transactions, name)
        person_groups = group_transactions_by_person(debit_transactions, name)

        credit_total = sum_amounts(credit_transactions)
        # Axis doesn't have per-transaction or per-cycle earned points.
        # The "eDGE REWARD POINTS" section shows a cumulative balance,
        # not cycle earnings, so we use it for reward_points_balance only.
        overall_reward_points = sum_points(debit_transactions)
        reward_points_balance = _extract_axis_reward_points(full_text, pages)

        due_date = _extract_axis_due_date(full_text, pages)
        statement_total_amount_due = _extract_axis_total_amount_due(full_text, pages)
        summary_fields = _extract_axis_account_summary(pages)
        reconciliation = build_reconciliation(
            statement_total_amount_due,
            debit_transactions,
            credit_transactions,
            summary_fields,
        )

        return ParsedStatement(
            file=raw_data["file"],
            bank=self.bank,
            name=name,
            card_number=card_number,
            due_date=due_date,
            statement_total_amount_due=statement_total_amount_due,
            card_summaries=card_summaries,
            overall_total=overall_total,
            person_groups=person_groups,
            payments_refunds=credit_transactions,
            payments_refunds_total=format_amount(credit_total),
            possible_adjustment_pairs=adjustment_pairs,
            overall_reward_points=str(int(overall_reward_points)),
            reward_points_balance=reward_points_balance,
            transactions=debit_transactions,
            reconciliation=reconciliation,
        )

    def build_debug(self, raw_data: dict[str, Any]) -> dict[str, Any]:
        pages = raw_data.get("pages", [])

        if self._last_txn_debug is not None and self._last_transactions is not None:
            txn_debug = self._last_txn_debug
            transactions = self._last_transactions
        else:
            transactions, txn_debug = _extract_axis_transactions(pages)

        return {
            "bank": self.bank,
            "stats": {
                "page_count": len(pages),
                "transactions_parsed": len(transactions),
                "credit_transactions": len(
                    [t for t in transactions if t.transaction_type == "credit"]
                ),
                "debit_transactions": len(
                    [t for t in transactions if t.transaction_type != "credit"]
                ),
                "date_lines_seen": len(txn_debug["date_lines"]),
                "date_lines_rejected": len(txn_debug["rejected_date_lines"]),
                "member_headers_detected": len(txn_debug["detected_members"]),
            },
            "card_from_filename": extract_card_from_filename(
                str(raw_data.get("file", ""))
            ),
            "detected_members": txn_debug["detected_members"],
            "date_lines": txn_debug["date_lines"],
            "rejected_date_lines": txn_debug["rejected_date_lines"],
        }
