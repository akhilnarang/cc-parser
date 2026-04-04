"""IDFC FIRST Bank parser profile.

IDFC credit card statements differ from other banks in several ways:
- Dates are ``DD Mon YY`` as three tokens (e.g. ``22 Oct 25``)
- Credit/debit markers are ``CR`` / ``DR`` as the last token
- Amounts may have an ``r`` prefix (e.g. ``r310.80``)
- Card numbers use ``XXXX DDDD`` (last 4 digits) or ``XX2060`` style
- Name appears without honorific prefix, near "Statement Summary"
- Due date format is ``DD/Mon/YYYY`` (e.g. ``08/Nov/2025``)
- Statement summary uses ``Opening Balance``, ``Purchases``,
  ``Payments & Refunds`` with ``r``-prefixed amounts
- Section headers: "Purchases, EMIs & Other Debits" / "Payments & Other Credits"
"""

import re
from typing import Any

from cc_parser.parsers.base import StatementParser
from cc_parser.parsers.cards import (
    extract_card_from_filename,
    find_card_candidates,
    is_invalid_person_label,
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
    compute_adjustment_totals,
    group_transactions_by_person,
    split_paired_adjustments,
)
from cc_parser.parsers.tokens import (
    MONTH_ABBREVS,
    SEPARATOR_TOKENS,
    clean_space,
    format_amount,
    normalize_amount,
    normalize_token,
    parse_amount_token,
    parse_date_token,
    parse_multi_token_date,
    sum_amounts,
    sum_points,
)

# Section headers and noise lines that should not be treated as member names
IDFC_NON_MEMBER_HEADERS = {
    "STATEMENT SUMMARY",
    "REWARDS SUMMARY",
    "PAYMENT MODES",
    "IMPORTANT INFORMATION",
    "YOUR CARD INFORMATION",
    "YOUR TRANSACTIONS",
    "SPECIAL BENEFITS ON YOUR CARD",
    "CREDIT CARD STATEMENT",
}


def _strip_rupee_prefix(token: str) -> str:
    """Strip IDFC's ``r`` currency prefix from a token."""
    if token.startswith("r") and len(token) > 1 and token[1:2].isdigit():
        return token[1:]
    return token


def _extract_idfc_name(full_text: str, pages: list[dict[str, Any]]) -> str | None:
    """Extract cardholder name from IDFC statement.

    IDFC prints the bare name (no honorific) on a line between the
    statement period and "Statement Summary".
    """
    # Try word-level extraction from first page
    if pages:
        lines = group_words_into_lines(pages[0].get("words") or [])
        for i, line_words in enumerate(lines):
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens)).upper()
            if joined == "STATEMENT SUMMARY" and i >= 2:
                # Name is typically the line before "Statement Summary"
                prev_tokens = [
                    normalize_token(str(w.get("text", "")))
                    for w in lines[i - 1]
                ]
                candidate = clean_space(" ".join(prev_tokens)).upper()
                parts = candidate.split()
                if 2 <= len(parts) <= 5 and all(
                    re.fullmatch(r"[A-Z][A-Z.'-]*", p) for p in parts
                ):
                    return candidate

    # Fallback: look for name near "Credit Card Statement" in text
    match = re.search(
        r"Credit\s+Card\s+Statement\s*\n"
        r"[^\n]*\n"
        r"\s*([A-Z][A-Z ]{3,})\s*\n",
        full_text,
    )
    if match:
        candidate = clean_space(match.group(1)).upper()
        parts = candidate.split()
        if 2 <= len(parts) <= 5:
            return candidate

    return None


def _extract_idfc_card_number(
    full_text: str, pages: list[dict[str, Any]]
) -> str | None:
    """Extract IDFC card number.

    IDFC uses formats like:
    - ``Card Number: XXXX 2060``
    - ``(FIRST Wealth XX2060)``
    """
    # Look for "Card Number:" line in word tokens
    for page in pages[:3]:
        lines = group_words_into_lines(page.get("words") or [])
        for line_words in lines:
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens)).upper()
            if "CARD NUMBER:" in joined or "CARD NUMBER :" in joined:
                # Extract the card digits after "Card Number:"
                card_match = re.search(
                    r"CARD\s+NUMBER\s*:\s*(X+\s*\d+)", joined
                )
                if card_match:
                    raw = card_match.group(1).replace(" ", "").upper()
                    # Pad to 16 chars with X prefix
                    if len(raw) < 16:
                        raw = "X" * (16 - len(raw)) + raw
                    return f"{raw[:4]} {raw[4:8]} {raw[8:12]} {raw[12:]}"

    # Look for (FIRST Wealth XX2060) pattern
    match = re.search(r"\(FIRST\s+\w+\s+XX(\d+)\)", full_text, flags=re.IGNORECASE)
    if match:
        digits = match.group(1)
        raw = "X" * (16 - len(digits)) + digits
        return f"{raw[:4]} {raw[4:8]} {raw[8:12]} {raw[12:]}"

    # Fall back to generic card detection
    candidates = find_card_candidates(full_text)
    return candidates[0] if candidates else None


def _extract_idfc_due_date(
    full_text: str, pages: list[dict[str, Any]]
) -> str | None:
    """Extract due date from IDFC statement.

    IDFC uses ``DD/Mon/YYYY`` format (e.g. ``08/Nov/2025``) in the
    summary row on page 1.
    """
    # Line-level search: find "Payment Due Date" header, then look at
    # the same or next line for the DD/Mon/YYYY value.
    for page in pages[:2]:
        lines = group_words_into_lines(page.get("words") or [])
        for i, line_words in enumerate(lines):
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens)).upper()
            if "PAYMENT DUE DATE" not in joined:
                continue

            # The header row has column labels; values are on the next line.
            # Find the x-position of "Payment" in the header to locate the
            # due-date value in the values row by x proximity.
            due_x: float | None = None
            for w in line_words:
                if normalize_token(str(w.get("text", ""))).upper() == "PAYMENT":
                    due_x = float(w.get("x0", 0))
                    break

            # Scan the next line for DD/Mon/YYYY tokens
            lines_to_check = [line_words]
            if i + 1 < len(lines):
                lines_to_check.append(lines[i + 1])

            for check_words in lines_to_check:
                for w in check_words:
                    t = normalize_token(str(w.get("text", "")))
                    date_match = re.fullmatch(
                        r"(\d{2})/([A-Za-z]{3})/(\d{4})", t
                    )
                    if date_match:
                        month = MONTH_ABBREVS.get(
                            date_match.group(2).upper()[:3]
                        )
                        if month:
                            return f"{date_match.group(1)}/{month}/{date_match.group(3)}"
                    date_match2 = re.fullmatch(r"\d{2}/\d{2}/\d{4}", t)
                    if date_match2:
                        return t

    # Fallback: text-level search near "Payment Due Date"
    match = re.search(
        r"Payment\s+Due\s+Date.*?(\d{2}/[A-Za-z]{3}/\d{4})",
        full_text,
        flags=re.IGNORECASE,
    )
    if match:
        raw = match.group(1)
        parts = raw.split("/")
        if len(parts) == 3:
            month = MONTH_ABBREVS.get(parts[1].upper()[:3])
            if month:
                return f"{parts[0]}/{month}/{parts[2]}"

    return None


def _extract_idfc_total_amount_due(
    full_text: str, pages: list[dict[str, Any]]
) -> str | None:
    """Extract total amount due from IDFC statement.

    IDFC has two common layouts:
    - Wealth: ``Total Amount Due = r310.80 DR`` (inline equation)
    - Mayura: Header row ``Minimum Amount Due | Total Amount Due | Payment Due Date``
              with values on the next line, matched by x-position.
    """
    # Line-level: find the summary header row with both "Minimum" and "Total"
    # and use x-position of "Total" column to pick the right value.
    for page in pages[:2]:
        lines = group_words_into_lines(page.get("words") or [])
        for i, line_words in enumerate(lines):
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens)).upper()

            # Header row that has both Minimum and Total Amount Due
            if "TOTAL AMOUNT DUE" in joined and "MINIMUM AMOUNT DUE" in joined:
                # Find x-position of the "Total" word in "Total Amount Due"
                # (not the "Minimum" one). Scan for the second "Total" or the
                # one after "Minimum Amount Due".
                total_x: float | None = None
                seen_minimum = False
                for w in line_words:
                    t = normalize_token(str(w.get("text", ""))).upper()
                    if t == "MINIMUM":
                        seen_minimum = True
                    elif t == "TOTAL" and seen_minimum:
                        total_x = float(w.get("x0", 0))
                        break

                if total_x is not None and i + 1 < len(lines):
                    # Find the amount token on the next line closest to total_x
                    best_amt: str | None = None
                    best_dist = float("inf")
                    for w in lines[i + 1]:
                        t = normalize_token(str(w.get("text", "")))
                        stripped = _strip_rupee_prefix(t)
                        amt = parse_amount_token(stripped)
                        if amt:
                            dist = abs(float(w.get("x0", 0)) - total_x)
                            if dist < best_dist:
                                best_dist = dist
                                best_amt = amt
                    if best_amt:
                        return normalize_amount(best_amt)

            # Header row with "Total Amount Due" but not "Minimum" (e.g. Wealth)
            if "TOTAL AMOUNT DUE" in joined and "MINIMUM AMOUNT DUE" not in joined:
                if i + 1 < len(lines):
                    next_tokens = [
                        normalize_token(str(w.get("text", "")))
                        for w in lines[i + 1]
                    ]
                    for t in next_tokens:
                        stripped = _strip_rupee_prefix(t)
                        amt = parse_amount_token(stripped)
                        if amt:
                            return normalize_amount(amt)

    # Fallback: "Total Amount Due = rXXX" inline equation
    match = re.search(
        r"Total\s+Amount\s+Due\s*=\s*r?([\d,]+\.\d{2})",
        full_text,
        flags=re.IGNORECASE,
    )
    if match:
        return normalize_amount(match.group(1))

    return None


def _extract_idfc_transactions(
    pages: list[dict[str, Any]],
) -> tuple[list[Transaction], dict[str, Any]]:
    """Parse transactions from IDFC statement pages.

    IDFC transaction lines follow the pattern:
    ``DD Mon YY  NARRATION  AMOUNT  DR/CR``
    """
    transactions: list[Transaction] = []
    current_card: str | None = None
    current_member: str | None = None
    in_credits_section = False
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

            # Detect card number lines: "Card Number: XXXX 2060"
            if "CARD NUMBER:" in joined_upper or "CARD NUMBER :" in joined_upper:
                card_match = re.search(r"CARD\s+NUMBER\s*:\s*(X+\s*\d+)", joined_upper)
                if card_match:
                    raw = card_match.group(1).replace(" ", "").upper()
                    if len(raw) < 16:
                        raw = "X" * (16 - len(raw)) + raw
                    current_card = f"{raw[:4]} {raw[4:8]} {raw[8:12]} {raw[12:]}"
                continue

            # Detect section headers for credit/debit context
            if "PAYMENTS" in joined_upper and "CREDITS" in joined_upper:
                in_credits_section = True
                continue
            if "PURCHASES" in joined_upper and "DEBITS" in joined_upper:
                in_credits_section = False
                continue

            # Try to parse date at the start of the line.
            # IDFC Wealth uses DD Mon YY (3 tokens); Mayura uses DD/MM/YYYY (1 token).
            date_value, date_tokens_consumed = parse_multi_token_date(tokens, 0)
            if date_value is None:
                single = parse_date_token(tokens[0])
                if single:
                    date_value = single
                    date_tokens_consumed = 1
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

            # Find the amount (rightmost amount token before DR/CR marker)
            amount_idx = -1
            for i in range(len(tokens) - 1, date_tokens_consumed - 1, -1):
                stripped = _strip_rupee_prefix(tokens[i])
                if parse_amount_token(stripped) is not None:
                    amount_idx = i
                    break

            if amount_idx == -1 or amount_idx <= date_tokens_consumed:
                rejected_date_lines.append(
                    {
                        "page": page_number,
                        "line_index": line_index,
                        "reason": "amount_not_found",
                        "tokens": tokens,
                    }
                )
                continue

            # Determine credit/debit from the last token (DR/CR) or section context
            last_token = tokens[-1].upper() if tokens else ""
            if last_token in {"CR", "C"}:
                is_credit = True
            elif last_token in {"DR", "D"}:
                is_credit = False
            else:
                is_credit = in_credits_section

            # Narration is everything between date tokens and amount
            cursor = date_tokens_consumed
            # Skip separator tokens after date
            while cursor < len(tokens) and tokens[cursor] in SEPARATOR_TOKENS:
                cursor += 1

            # "Convert" marks the start of the EMI Eligibility / Forex
            # columns — everything from it onward is not narration.
            narration_end = amount_idx
            for ni in range(cursor, amount_idx):
                if tokens[ni].upper() == "CONVERT":
                    narration_end = ni
                    break

            narration_tokens = [
                t
                for t in tokens[cursor:narration_end]
                if t
                and t not in SEPARATOR_TOKENS
                and t not in {"+", "l", "I"}
                and t.upper() not in {"DR", "CR"}
            ]

            narration = clean_space(" ".join(narration_tokens))

            # Merge context from wrapped lines if narration is incomplete
            if needs_context_merge(narration, narration_tokens):
                prev_ctx, next_ctx = collect_row_context_tokens(lines, line_index)
                ctx_tokens = [
                    t
                    for t in [*prev_ctx, *next_ctx]
                    if t
                    and t not in SEPARATOR_TOKENS
                    and t not in {"+", "l", "I", "CR", "DR"}
                    and parse_amount_token(_strip_rupee_prefix(t)) is None
                    and parse_multi_token_date([t, "", ""], 0)[0] is None
                ]
                narration = clean_space(" ".join([*narration_tokens, *ctx_tokens]))

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

            amount_raw = _strip_rupee_prefix(tokens[amount_idx])
            amount_value = parse_amount_token(amount_raw)
            if not amount_value:
                rejected_date_lines.append(
                    {
                        "page": page_number,
                        "line_index": line_index,
                        "reason": "amount_parse_failed",
                        "tokens": tokens,
                    }
                )
                continue

            credit_reason = None
            if is_credit:
                if last_token in {"CR", "C"}:
                    credit_reason = "cr_marker"
                else:
                    credit_reason = "credits_section"

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


def _extract_idfc_account_summary(
    pages: list[dict[str, Any]],
) -> StatementSummary:
    """Extract account summary values from IDFC statement.

    IDFC has two summary layouts:

    **Wealth** (line-by-line):
    - Opening Balance rXXX
    - Purchases + rXXX
    - Payments & Refunds - rXXX

    **Mayura** (columnar):
    Header: ``Opening + Purchases + Other Debits - Payments - Other = Total``
    Values: ``r1,29,435.97 r1,14,700.11 r700.92 r1,30,633.05 r0.00 r1,14,203.95 DR``
    Columns: [opening_bal, purchases, other_debits, payments, other_credits, total_due]
    """
    for page in pages[:2]:
        words = page.get("words") or []
        if not words:
            continue

        lines = group_words_into_lines(words)

        # --- Try Mayura columnar layout first ---
        for i, line_words in enumerate(lines):
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined_upper = clean_space(" ".join(tokens)).upper()

            # Detect "Opening + Purchases + Other Debits - Payments ..."
            if "OPENING" in joined_upper and "PURCHASES" in joined_upper and "PAYMENTS" in joined_upper:
                # Scan ahead for the values line (has 4+ r-prefixed amounts)
                for offset in range(1, 5):
                    idx = i + offset
                    if idx >= len(lines):
                        break
                    val_words = sorted(lines[idx], key=lambda w: w.get("x0", 0))
                    amounts: list[str] = []
                    for w in val_words:
                        t = normalize_token(str(w.get("text", "")))
                        stripped = _strip_rupee_prefix(t)
                        amt = parse_amount_token(stripped)
                        if amt:
                            amounts.append(normalize_amount(amt))

                    if len(amounts) >= 4:
                        # [opening_bal, purchases, other_debits, payments, other_credits, total_due]
                        # Note: "Other Debits" (index 2) are already included as
                        # parsed transactions, so we do NOT map them to
                        # finance_charges to avoid double-counting in reconciliation.
                        result = StatementSummary(
                            summary_amount_candidates=amounts,
                            previous_statement_dues=amounts[0],
                            purchases_debit=amounts[1],
                        )
                        if len(amounts) >= 4:
                            result.payments_credits_received = amounts[3]
                        return result

        # --- Fallback: Wealth line-by-line layout ---
        opening_balance: str | None = None
        purchases: str | None = None
        payments_refunds: str | None = None

        for line_words in lines:
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens))
            joined_upper = joined.upper()

            if "OPENING BALANCE" in joined_upper and opening_balance is None:
                for t in tokens:
                    stripped = _strip_rupee_prefix(t)
                    amt = parse_amount_token(stripped)
                    if amt:
                        opening_balance = normalize_amount(amt)
                        break

            if joined_upper.startswith("PURCHASES") and purchases is None:
                for t in tokens:
                    stripped = _strip_rupee_prefix(t)
                    amt = parse_amount_token(stripped)
                    if amt:
                        purchases = normalize_amount(amt)
                        break

            if "PAYMENTS" in joined_upper and "REFUNDS" in joined_upper:
                for t in tokens:
                    stripped = _strip_rupee_prefix(t)
                    amt = parse_amount_token(stripped)
                    if amt:
                        payments_refunds = normalize_amount(amt)
                        break

        if opening_balance or purchases or payments_refunds:
            candidates = [
                a for a in [opening_balance, purchases, payments_refunds] if a
            ]
            return StatementSummary(
                summary_amount_candidates=candidates,
                previous_statement_dues=opening_balance,
                purchases_debit=purchases,
                payments_credits_received=payments_refunds,
            )

    return StatementSummary()


class IdfcParser(StatementParser):
    """Parser entrypoint for IDFC FIRST Bank statements."""

    bank = "idfc"

    def __init__(self) -> None:
        self._last_txn_debug: dict[str, Any] | None = None
        self._last_transactions: list[Transaction] | None = None

    def parse(self, raw_data: dict[str, Any]) -> ParsedStatement:
        full_text = "\n".join(
            str(page.get("text", "")) for page in raw_data.get("pages", [])
        )
        pages = raw_data.get("pages", [])

        name = _extract_idfc_name(full_text, pages)
        card_number = _extract_idfc_card_number(
            full_text, pages
        ) or extract_card_from_filename(str(raw_data["file"]))

        transactions, txn_debug = _extract_idfc_transactions(pages)
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
        for txn in transactions:
            person_value = (txn.person or "").strip()
            if is_invalid_person_label(person_value):
                txn.person = name

        debit_transactions = [
            txn for txn in transactions if txn.transaction_type != "credit"
        ]
        credit_transactions = [
            txn for txn in transactions if txn.transaction_type == "credit"
        ]

        debit_transactions, credit_transactions, adjustments = split_paired_adjustments(
            debit_transactions, credit_transactions
        )

        card_summaries, overall_total = build_card_summaries(debit_transactions, name)
        person_groups = group_transactions_by_person(debit_transactions, name)

        credit_total = sum_amounts(credit_transactions)
        overall_reward_points = sum_points(debit_transactions)

        adjustments_debit_total, adjustments_credit_total = compute_adjustment_totals(
            adjustments
        )

        due_date = _extract_idfc_due_date(full_text, pages)
        statement_total_amount_due = _extract_idfc_total_amount_due(full_text, pages)
        summary_fields = _extract_idfc_account_summary(pages)
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
            adjustments=adjustments,
            adjustments_debit_total=adjustments_debit_total,
            adjustments_credit_total=adjustments_credit_total,
            overall_reward_points=str(int(overall_reward_points)),
            transactions=debit_transactions,
            reconciliation=reconciliation,
        )

    def build_debug(self, raw_data: dict[str, Any]) -> dict[str, Any]:
        pages = raw_data.get("pages", [])

        if self._last_txn_debug is not None and self._last_transactions is not None:
            txn_debug = self._last_txn_debug
            transactions = self._last_transactions
        else:
            transactions, txn_debug = _extract_idfc_transactions(pages)

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
