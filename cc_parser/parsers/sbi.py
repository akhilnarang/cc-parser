"""SBI parser profile.

SBI credit card statements differ from HDFC/ICICI in several ways:
- Dates are ``DD Mon YY`` as three tokens (e.g. ``04 Feb 26``)
- Credit/debit markers are bare ``C`` / ``D`` as the last token
- Card numbers show only last 2 digits (``XXXX XXXX XXXX XX60``)
- Member headers use ``TRANSACTIONS FOR <NAME>``
- No honorific prefix on cardholder name
- Due date format is ``DD Mon YYYY`` (no comma)
- Account summary layout differs from HDFC/ICICI

This module overrides the generic extraction pipeline with SBI-specific
date detection, credit classification, and metadata extraction.
"""

import re
from decimal import Decimal
from typing import Any

from cc_parser.parsers.base import StatementParser
from cc_parser.parsers.cards import (
    extract_card_from_filename,
    find_card_candidates,
    is_invalid_person_label,
)
from cc_parser.parsers.extraction import group_words_into_lines
from cc_parser.parsers.narration import (
    clean_narration_artifacts,
    collect_row_context_tokens,
    extract_continuation_narration,
    needs_context_merge,
)
from cc_parser.parsers.reconciliation import (
    build_card_summaries,
    build_reconciliation,
    group_transactions_by_person,
    split_paired_adjustments,
)
from cc_parser.parsers.tokens import (
    SEPARATOR_TOKENS,
    clean_space,
    format_amount,
    normalize_amount,
    normalize_token,
    parse_amount,
    parse_amount_token,
    sum_amounts,
    sum_points,
)

MONTH_ABBREVS = {
    "JAN": "01",
    "FEB": "02",
    "MAR": "03",
    "APR": "04",
    "MAY": "05",
    "JUN": "06",
    "JUL": "07",
    "AUG": "08",
    "SEP": "09",
    "OCT": "10",
    "NOV": "11",
    "DEC": "12",
}

# SBI-specific noise headers that should not be treated as member names
SBI_NON_MEMBER_HEADERS = {
    "SAVINGS AND BENEFITS SECTION",
    "SCHEDULE OF CHARGES",
    "IMPORTANT MESSAGES",
    "IMPORTANT NOTES",
    "IMPORTANT INFORMATION",
    "SAFETY FIRST",
    "EXTENDED CREDIT",
    "MINIMUM FINANCE CHARGES",
    "CASH ADVANCE",
    "LETS CONNECT LETS SIMPLIFY",
    "PAY VIA UPI",
    "AUTO DEBIT",
    "BY PHONE",
    "PAY NOW",
    "PREVIOUS BALANCE TOTAL OUTSTANDING",
    "TRANSACTION DETAILS",
    "ACCOUNT SUMMARY",
    "IN THE PRECEDING YEAR",
}


def _parse_sbi_date(tokens: list[str], start: int) -> tuple[str | None, int]:
    """Try to parse an SBI-style ``DD Mon YY`` date starting at *start*.

    Returns ``(date_str, tokens_consumed)`` where *date_str* is in
    ``DD/MM/YYYY`` format, or ``(None, 0)`` on failure.
    """
    if start + 2 >= len(tokens):
        return None, 0
    day = normalize_token(tokens[start])
    month_tok = normalize_token(tokens[start + 1])
    year_tok = normalize_token(tokens[start + 2])

    if not re.fullmatch(r"\d{1,2}", day):
        return None, 0
    month = MONTH_ABBREVS.get(month_tok.upper())
    if month is None:
        return None, 0
    if not re.fullmatch(r"\d{2,4}", year_tok):
        return None, 0

    day_padded = day.zfill(2)
    year = year_tok if len(year_tok) == 4 else f"20{year_tok}"
    return f"{day_padded}/{month}/{year}", 3


def _extract_sbi_name(full_text: str) -> str | None:
    """Extract cardholder name from SBI statement.

    SBI prints the bare name (no honorific) on a line near
    ``Credit Card Number``.
    """
    # Try: line immediately before "Credit Card Number"
    for pattern in [
        r"([A-Z][A-Z ]{3,})\s*\n\s*Credit\s+Card\s+Number",
        r"\n\s*([A-Z][A-Z ]{3,})\s+Credit\s+Card\s+Number",
    ]:
        match = re.search(pattern, full_text, flags=re.IGNORECASE)
        if match:
            candidate = clean_space(match.group(1)).upper()
            parts = candidate.split()
            if 2 <= len(parts) <= 5:
                return candidate

    # Fallback: first "TRANSACTIONS FOR <NAME>" header
    match = re.search(r"TRANSACTIONS\s+FOR\s+([A-Z][A-Z ]+)", full_text)
    if match:
        candidate = clean_space(match.group(1)).upper()
        parts = candidate.split()
        if 2 <= len(parts) <= 5:
            return candidate

    return None


def _extract_sbi_card_number(full_text: str) -> str | None:
    """Extract SBI card number which shows only last 2 digits.

    SBI format: ``XXXX XXXX XXXX XX60``
    """
    # Look for the SBI-specific pattern with mostly X's
    match = re.search(
        r"(X{4}\s+X{4}\s+X{4}\s+X{2}\d{2})", full_text, flags=re.IGNORECASE
    )
    if match:
        raw = match.group(1).replace(" ", "").upper()
        return f"{raw[:4]} {raw[4:8]} {raw[8:12]} {raw[12:]}"

    # Fall back to generic card detection
    candidates = find_card_candidates(full_text)
    return candidates[0] if candidates else None


def _extract_sbi_due_date(full_text: str, pages: list[dict[str, Any]]) -> str | None:
    """Extract due date, handling SBI's ``DD Mon YYYY`` (no comma) and
    the ``NO PAYMENT REQUIRED`` special case.

    SBI puts "Payment Due Date" on one line and the value on the next,
    so we search both full text and page-level word lines.
    """
    # Check for no-payment-required (may be on same or next line)
    if re.search(
        r"Payment\s+Due\s+Date\s*[:\-]?\s*NO\s+PAYMENT\s+REQUIRED",
        full_text,
        flags=re.IGNORECASE,
    ):
        return "NO PAYMENT REQUIRED"

    # Inline match: DD Mon YYYY without comma
    match = re.search(
        r"Payment\s+Due\s+Date\s*[:\-]?\s*(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})",
        full_text,
        flags=re.IGNORECASE,
    )
    if match:
        return _normalize_sbi_date_long(match.group(1))

    # Line-level search: value may be on the next visual line
    for page in pages[:2]:
        lines = group_words_into_lines(page.get("words") or [])
        for i, line_words in enumerate(lines):
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens)).upper()
            if "PAYMENT DUE DATE" not in joined:
                continue

            # Check same line for a date or NO PAYMENT REQUIRED
            if "NO PAYMENT REQUIRED" in joined:
                return "NO PAYMENT REQUIRED"
            date_match = re.search(
                r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})", " ".join(tokens)
            )
            if date_match:
                return _normalize_sbi_date_long(date_match.group(1))

            # Check the next line
            if i + 1 < len(lines):
                next_tokens = [
                    normalize_token(str(w.get("text", ""))) for w in lines[i + 1]
                ]
                next_joined = clean_space(" ".join(next_tokens))
                if "NO PAYMENT REQUIRED" in next_joined.upper():
                    return "NO PAYMENT REQUIRED"
                date_match = re.search(
                    r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})", next_joined
                )
                if date_match:
                    return _normalize_sbi_date_long(date_match.group(1))

    return None


def _normalize_sbi_date_long(raw: str) -> str:
    """Convert ``DD Mon YYYY`` to ``DD/MM/YYYY``."""
    parts = clean_space(raw).split()
    if len(parts) == 3:
        month = MONTH_ABBREVS.get(parts[1].upper()[:3])
        if month:
            return f"{parts[0].zfill(2)}/{month}/{parts[2]}"
    return raw


def _extract_sbi_total_amount_due(full_text: str) -> str | None:
    """Extract total amount due from SBI statement.

    SBI format: ``*Total Amount Due ( ` ) 72,202.00``
    """
    match = re.search(
        r"Total\s+Amount\s+Due\s*\([^)]*\)\s*([\d,]+\.\d{2})",
        full_text,
        flags=re.IGNORECASE,
    )
    if match:
        return normalize_amount(match.group(1))

    # Fallback to generic
    upper = full_text.upper()
    start = upper.find("TOTAL AMOUNT DUE")
    if start == -1:
        return None
    segment = full_text[start : start + 500]
    amount_match = re.search(r"\d[\d,]*\.\d{2}", segment)
    if amount_match:
        return normalize_amount(amount_match.group(0))
    return None


def _extract_sbi_transactions(
    pages: list[dict[str, Any]],
) -> tuple[list[dict[str, str | None]], dict[str, Any]]:
    """Parse transactions from SBI statement pages.

    SBI transaction lines follow the pattern:
    ``DD Mon YY  NARRATION [REFERENCE]  AMOUNT  C/D``
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

            tokens = [normalize_token(t) for t in raw_tokens]

            # Check for "TRANSACTIONS FOR <NAME>" member header
            joined_upper = clean_space(" ".join(tokens)).upper()
            txn_for_match = re.match(r"TRANSACTIONS\s+FOR\s+(.+)", joined_upper)
            if txn_for_match:
                name = clean_space(txn_for_match.group(1))
                # Validate it looks like a name (2-5 alpha words)
                parts = name.split()
                if 2 <= len(parts) <= 5 and all(
                    re.fullmatch(r"[A-Z][A-Z.'-]*", p) for p in parts
                ):
                    current_member = name
                    detected_members.append(
                        {
                            "page": page_number,
                            "line_index": line_index,
                            "member": name,
                        }
                    )
                continue

            # Try to parse SBI date at the start of the line
            date_value, date_tokens_consumed = _parse_sbi_date(tokens, 0)
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

            # Find the amount (rightmost amount token before C/D marker)
            amount_idx = -1
            for i in range(len(tokens) - 1, date_tokens_consumed - 1, -1):
                if parse_amount_token(tokens[i]) is not None:
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

            # Determine credit/debit from the last token
            last_token = tokens[-1].upper() if tokens else ""
            is_credit = last_token == "C"

            # Narration is everything between date tokens and amount
            cursor = date_tokens_consumed
            # Skip separator tokens after date
            while cursor < len(tokens) and tokens[cursor] in SEPARATOR_TOKENS:
                cursor += 1

            narration_tokens = [
                t
                for t in tokens[cursor:amount_idx]
                if t and t not in SEPARATOR_TOKENS and t not in {"+", "l", "I"}
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
                    and t not in {"+", "l", "I", "C", "D", "CR"}
                    and parse_amount_token(t) is None
                    and _parse_sbi_date([t, "", ""], 0)[0] is None
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

            amount_value = parse_amount_token(tokens[amount_idx])
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

            transactions.append(
                {
                    "date": date_value,
                    "time": None,
                    "narration": narration,
                    "reward_points": None,
                    "amount": normalize_amount(amount_value),
                    "card_number": current_card,
                    "person": current_member,
                    "transaction_type": "credit" if is_credit else "debit",
                }
            )
            if is_credit:
                transactions[-1]["credit_reasons"] = "sbi_c_marker"

    debug = {
        "date_lines": date_lines,
        "rejected_date_lines": rejected_date_lines,
        "detected_members": detected_members,
    }
    return transactions, debug


def _extract_sbi_account_summary(
    pages: list[dict[str, Any]],
) -> dict[str, str | list[str] | None]:
    """Extract Account Summary values from SBI statement.

    SBI lays out the Account Summary as 5 columns:
      Previous Balance | Payments/Credits | Purchases/Debits | Fees | Total Outstanding

    The column header text is spread across multiple lines. The actual values
    appear on a subsequent line, ordered left-to-right by x-position.
    Total Outstanding may appear on a separate line (possibly with ``CR``).
    """
    for page in pages[:2]:
        words = page.get("words") or []
        if not words:
            continue

        # Check if ACCOUNT SUMMARY is on this page
        page_text = " ".join(str(w.get("text", "")) for w in words).upper()
        if "ACCOUNT SUMMARY" not in page_text:
            continue

        lines = group_words_into_lines(words)

        # Find the ACCOUNT SUMMARY header line
        summary_line_idx: int | None = None
        for i, line_words in enumerate(lines):
            joined = clean_space(
                " ".join(normalize_token(str(w.get("text", ""))) for w in line_words)
            ).upper()
            if "ACCOUNT SUMMARY" in joined:
                summary_line_idx = i
                break

        if summary_line_idx is None:
            continue

        # Scan lines after the header for one with 4+ amounts — that's the values line
        amounts_line_idx: int | None = None
        total_outstanding_line_idx: int | None = None
        for offset in range(1, 25):
            idx = summary_line_idx + offset
            if idx >= len(lines):
                break
            line_words = lines[idx]
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            amount_count = sum(1 for t in tokens if re.fullmatch(r"[\d,]+\.\d{2}", t))
            # The values line has 4 amounts (prev_bal, payments, purchases, fees)
            # Total Outstanding may be on this line too (5 amounts) or previous line
            if amount_count >= 4:
                amounts_line_idx = idx
                break
            # A line with 1 amount + optional "CR" could be Total Outstanding
            if amount_count == 1 and amounts_line_idx is None:
                # Only treat as total outstanding if it's after the header area
                if offset > 8:
                    total_outstanding_line_idx = idx

        if amounts_line_idx is None:
            continue

        # Extract amounts from the values line, sorted by x-position
        value_words = sorted(lines[amounts_line_idx], key=lambda w: w.get("x0", 0))
        amounts_with_x: list[tuple[float, str]] = []
        for w in value_words:
            t = normalize_token(str(w.get("text", "")))
            if re.fullmatch(r"[\d,]+\.\d{2}", t):
                amounts_with_x.append((float(w.get("x0", 0)), normalize_amount(t)))

        # If the total outstanding was on a separate earlier line, extract it
        total_outstanding: str | None = None
        if (
            total_outstanding_line_idx is not None
            and total_outstanding_line_idx < amounts_line_idx
        ):
            for w in lines[total_outstanding_line_idx]:
                t = normalize_token(str(w.get("text", "")))
                if re.fullmatch(r"[\d,]+\.\d{2}", t):
                    total_outstanding = normalize_amount(t)
                    break

        # Map amounts by position
        # If 5 amounts on one line: [prev_bal, payments, purchases, fees, total_outstanding]
        # If 4 amounts: [prev_bal, payments, purchases, fees] — total outstanding is separate
        result: dict[str, str | list[str] | None] = {
            "summary_amount_candidates": [a for _, a in amounts_with_x],
            "previous_statement_dues": None,
            "payments_credits_received": None,
            "purchases_debit": None,
            "finance_charges": None,
            "equation_tail": None,
        }

        if len(amounts_with_x) >= 5:
            result["previous_statement_dues"] = amounts_with_x[0][1]
            result["payments_credits_received"] = amounts_with_x[1][1]
            result["purchases_debit"] = amounts_with_x[2][1]
            result["finance_charges"] = amounts_with_x[3][1]
            result["equation_tail"] = amounts_with_x[4][1]
        elif len(amounts_with_x) >= 4:
            result["previous_statement_dues"] = amounts_with_x[0][1]
            result["payments_credits_received"] = amounts_with_x[1][1]
            result["purchases_debit"] = amounts_with_x[2][1]
            result["finance_charges"] = amounts_with_x[3][1]
            if total_outstanding is not None:
                result["equation_tail"] = total_outstanding
                candidates = list(result["summary_amount_candidates"] or [])
                candidates.append(total_outstanding)
                result["summary_amount_candidates"] = candidates

        return result

    # Fallback: return empty summary
    return {
        "summary_amount_candidates": [],
        "previous_statement_dues": None,
        "payments_credits_received": None,
        "purchases_debit": None,
        "finance_charges": None,
        "equation_tail": None,
    }


class SbiParser(StatementParser):
    """Parser entrypoint for SBI statements."""

    bank = "sbi"

    def __init__(self) -> None:
        self._last_txn_debug: dict[str, Any] | None = None
        self._last_transactions: list[dict[str, str | None]] | None = None

    def parse(self, raw_data: dict[str, Any]) -> dict[str, Any]:
        full_text = "\n".join(
            str(page.get("text", "")) for page in raw_data.get("pages", [])
        )

        name = _extract_sbi_name(full_text)
        card_number = _extract_sbi_card_number(full_text) or extract_card_from_filename(
            str(raw_data["file"])
        )

        transactions, txn_debug = _extract_sbi_transactions(raw_data.get("pages", []))
        self._last_txn_debug = txn_debug
        self._last_transactions = transactions

        # Fill in card number for transactions missing it
        if card_number:
            for txn in transactions:
                if not txn.get("card_number"):
                    txn["card_number"] = card_number

        # Fix invalid person labels
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
            debit_transactions, credit_transactions
        )

        card_summaries, overall_total = build_card_summaries(debit_transactions, name)
        person_groups = group_transactions_by_person(debit_transactions, name)

        credit_total = sum_amounts(credit_transactions)
        overall_reward_points = sum_points(debit_transactions)

        adjustments_debit_total = Decimal("0")
        adjustments_credit_total = Decimal("0")
        for txn in adjustments:
            side = str(txn.get("adjustment_side") or "")
            amount = parse_amount(str(txn.get("amount") or "0"))
            if side == "debit":
                adjustments_debit_total += amount
            elif side == "credit":
                adjustments_credit_total += amount

        due_date = _extract_sbi_due_date(full_text, raw_data.get("pages", []))
        statement_total_amount_due = _extract_sbi_total_amount_due(full_text)
        summary_fields = _extract_sbi_account_summary(raw_data.get("pages", []))
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
            "card_number": card_number,
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
        pages = raw_data.get("pages", [])

        if self._last_txn_debug is not None and self._last_transactions is not None:
            txn_debug = self._last_txn_debug
            transactions = self._last_transactions
        else:
            transactions, txn_debug = _extract_sbi_transactions(pages)

        return {
            "bank": self.bank,
            "stats": {
                "page_count": len(pages),
                "transactions_parsed": len(transactions),
                "credit_transactions": len(
                    [
                        t
                        for t in transactions
                        if str(t.get("transaction_type") or "debit") == "credit"
                    ]
                ),
                "debit_transactions": len(
                    [
                        t
                        for t in transactions
                        if str(t.get("transaction_type") or "debit") != "credit"
                    ]
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
