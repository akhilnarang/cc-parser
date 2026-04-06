"""Jupiter (Edge CSB Bank) parser profile.

Jupiter credit card statements differ from other banks in several ways:
- Dates are ``DD Mon YYYY`` as three tokens (e.g. ``17 Feb 2026``)
- Time appears on a separate line as ``HH:MM AM/PM``
- Amounts are prefixed with ``Rs.`` as a separate token, e.g. ``Rs.`` ``4,500``
- Amounts may or may not have decimal places (``4,500`` vs ``500.00``)
- NO explicit Cr/Dr markers on transactions -- credits are identified
  by narration keywords (Repayment, Jewels converted, Refund)
- Card numbers use ``XXXX XXXX XXXX DDDD`` format (uppercase X with spaces)
- Name appears without honorific prefix
- Due date format is ``DD Mon YYYY`` (e.g. ``01 Apr 2026``)
- Statement summary uses labeled rows: Previous balance, Spends,
  Interest charges, Fees, Repayments, Refunds, Waivers, Total amount due
- ``End of Transactions`` marks the end of the transaction section
"""

import re
from decimal import Decimal
from typing import Any

from cc_parser.parsers.base import StatementParser
from cc_parser.parsers.cards import (
    extract_card_from_filename,
    find_card_candidates,
    normalize_transaction_persons,
    split_by_transaction_type,
)
from cc_parser.parsers.extraction import group_words_into_lines
from cc_parser.parsers.models import ParsedStatement, StatementSummary, Transaction
from cc_parser.parsers.narration import clean_narration_artifacts
from cc_parser.parsers.reconciliation import (
    build_card_summaries,
    build_reconciliation,
    detect_adjustment_pairs,
    group_transactions_by_person,
)
from cc_parser.parsers.transaction_id_generator import assign_transaction_ids
from cc_parser.parsers.tokens import (
    MONTH_ABBREVS,
    clean_space,
    format_amount,
    normalize_amount,
    normalize_token,
    parse_amount,
    parse_amount_token,
    parse_multi_token_date,
    sum_amounts,
    sum_points,
)

# Amount regex that also handles whole-number amounts without decimals
# e.g. "4,500", "25,907", "20,000" as well as "500.00", "734.00"
_JUPITER_AMOUNT_RE = re.compile(r"^\d[\d,]*(?:\.\d{2})?$")

# Narration keywords that indicate a credit transaction
_CREDIT_KEYWORDS = {
    "REPAYMENT",
    "JEWELS CONVERTED",
    "REFUND",
    "REVERSAL",
    "CASHBACK",
    "WAIVER",
    "WAIVERS",
}


def _parse_jupiter_amount(token: str) -> str | None:
    """Parse a Jupiter amount token (with or without decimals).

    Jupiter amounts may appear as ``4,500`` (no decimals) or ``500.00``
    (with decimals). The standard ``parse_amount_token`` only handles
    the decimal form, so we extend it here.
    """
    # Try the standard parser first
    standard = parse_amount_token(token)
    if standard:
        return standard
    # Try whole-number amounts
    cleaned = normalize_token(token).replace("`", "")
    if _JUPITER_AMOUNT_RE.fullmatch(cleaned):
        return cleaned
    return None


def _normalize_jupiter_amount(raw: str) -> str:
    """Normalize a Jupiter amount string, appending .00 if no decimals."""
    value = normalize_amount(raw)
    if "." not in value:
        value = value + ".00"
    return value


def _is_credit_narration(narration: str) -> tuple[bool, str | None]:
    """Determine if a transaction is a credit based on narration keywords.

    Returns:
        Tuple ``(is_credit, reason)`` where reason describes the match.
    """
    upper = narration.upper()
    for keyword in _CREDIT_KEYWORDS:
        if keyword in upper:
            return True, f"narration_keyword:{keyword.lower()}"
    return False, None


def _extract_jupiter_name(full_text: str, pages: list[dict[str, Any]]) -> str | None:
    """Extract cardholder name from Jupiter statement.

    Jupiter prints the bare name (no honorific) on the line after the
    ``Name`` / ``Card number`` header row.
    """
    for page in pages[:2]:
        lines = group_words_into_lines(page.get("words") or [])
        for i, line_words in enumerate(lines):
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens)).upper()
            if (
                joined == "NAME CARD NUMBER"
                or "NAME" in joined
                and "CARD NUMBER" in joined
            ):
                # Name is on the next line
                if i + 1 < len(lines):
                    next_tokens = [
                        normalize_token(str(w.get("text", ""))) for w in lines[i + 1]
                    ]
                    # Filter out card number tokens (XXXX and digits)
                    name_parts = []
                    for t in next_tokens:
                        # Stop collecting name parts when we hit card-like tokens
                        if re.fullmatch(r"[Xx]{4}", t) or re.fullmatch(r"\d{4}", t):
                            break
                        if re.fullmatch(r"[A-Za-z][A-Za-z.'-]*", t):
                            name_parts.append(t.upper())
                    if 2 <= len(name_parts) <= 5:
                        return " ".join(name_parts)

    # Fallback: look for name in text
    match = re.search(
        r"Name\s+Card\s+number\s*\n\s*([A-Z][A-Z ]+?)(?:\s+X{4}|\s*\n)",
        full_text,
        flags=re.IGNORECASE,
    )
    if match:
        candidate = clean_space(match.group(1)).upper()
        parts = candidate.split()
        if 2 <= len(parts) <= 5:
            return candidate

    return None


def _extract_jupiter_card_number(
    full_text: str, pages: list[dict[str, Any]]
) -> str | None:
    """Extract card number from Jupiter statement.

    Jupiter uses ``XXXX XXXX XXXX DDDD`` format spread across 4 tokens.
    """
    for page in pages[:2]:
        lines = group_words_into_lines(page.get("words") or [])
        for line_words in lines:
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            # Look for XXXX XXXX XXXX DDDD pattern
            for j in range(len(tokens) - 3):
                if (
                    re.fullmatch(r"[Xx]{4}", tokens[j])
                    and re.fullmatch(r"[Xx]{4}", tokens[j + 1])
                    and re.fullmatch(r"[Xx]{4}", tokens[j + 2])
                    and re.fullmatch(r"\d{4}", tokens[j + 3])
                ):
                    return f"XXXX XXXX XXXX {tokens[j + 3]}"

    # Text-level fallback
    match = re.search(r"XXXX\s+XXXX\s+XXXX\s+(\d{4})", full_text)
    if match:
        return f"XXXX XXXX XXXX {match.group(1)}"

    # Generic fallback
    candidates = find_card_candidates(full_text)
    return candidates[0] if candidates else None


def _extract_jupiter_due_date(
    full_text: str, pages: list[dict[str, Any]]
) -> str | None:
    """Extract due date from Jupiter statement.

    Jupiter uses ``DD Mon YYYY`` format (e.g. ``01 Apr 2026``) on the line
    below the ``Payment due date`` header.
    """
    for page in pages[:2]:
        lines = group_words_into_lines(page.get("words") or [])
        for i, line_words in enumerate(lines):
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens)).upper()
            if "PAYMENT DUE DATE" not in joined:
                continue

            # Check the next line for DD Mon YYYY
            if i + 1 < len(lines):
                next_tokens = [
                    normalize_token(str(w.get("text", ""))) for w in lines[i + 1]
                ]
                # Try to find DD Mon YYYY in next line tokens
                for k in range(len(next_tokens) - 2):
                    date_val, consumed = parse_multi_token_date(next_tokens, k)
                    if date_val and consumed == 3:
                        return date_val

    # Text-level fallback: "Payment due date\nDD Mon YYYY"
    match = re.search(
        r"Payment\s+due\s+date\s*\n.*?(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})",
        full_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        day = match.group(1).zfill(2)
        month = MONTH_ABBREVS.get(match.group(2).upper()[:3])
        year = match.group(3)
        if month:
            return f"{day}/{month}/{year}"

    return None


def _extract_jupiter_total_amount_due(
    full_text: str, pages: list[dict[str, Any]]
) -> str | None:
    """Extract total amount due from Jupiter statement.

    Jupiter has ``Total amount due`` followed by ``Rs. AMOUNT`` on the
    next line, both in the Bill Summary area and the Statement Summary area.
    We prefer the Bill Summary area (first occurrence).
    """
    for page in pages[:2]:
        lines = group_words_into_lines(page.get("words") or [])
        for i, line_words in enumerate(lines):
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens)).upper()
            if "TOTAL AMOUNT DUE" not in joined:
                continue
            # Skip lines that are in the legal text ("Total Amount Due" in quotes)
            if "'TOTAL" in joined or '"TOTAL' in joined:
                continue

            # Check same line for amount after Rs.
            for k, t in enumerate(tokens):
                if t.upper() == "RS." and k + 1 < len(tokens):
                    amt = _parse_jupiter_amount(tokens[k + 1])
                    if amt:
                        return _normalize_jupiter_amount(amt)

            # Check next line
            if i + 1 < len(lines):
                next_tokens = [
                    normalize_token(str(w.get("text", ""))) for w in lines[i + 1]
                ]
                for k, t in enumerate(next_tokens):
                    if t.upper() == "RS." and k + 1 < len(next_tokens):
                        amt = _parse_jupiter_amount(next_tokens[k + 1])
                        if amt:
                            return _normalize_jupiter_amount(amt)
                    # Also try standalone amount
                    amt = _parse_jupiter_amount(t)
                    if amt:
                        return _normalize_jupiter_amount(amt)

    # Text-level fallback
    match = re.search(
        r"Total\s+amount\s+due\s*\n.*?Rs\.?\s*([\d,]+(?:\.\d{2})?)",
        full_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        return _normalize_jupiter_amount(match.group(1))

    return None


def _extract_jupiter_transactions(
    pages: list[dict[str, Any]],
) -> tuple[list[Transaction], dict[str, Any]]:
    """Parse transactions from Jupiter statement pages.

    Jupiter transaction layout:
    Line 1: ``DD Mon YYYY  NARRATION  Rs. AMOUNT``
    Line 2: ``HH:MM AM/PM``

    Transactions end at the ``End of Transactions`` marker.
    """
    transactions: list[Transaction] = []
    date_lines: list[dict[str, Any]] = []
    rejected_date_lines: list[dict[str, Any]] = []
    end_of_transactions = False

    for page in pages:
        if end_of_transactions:
            break

        page_number = int(page.get("page_number", 0) or 0)
        words = page.get("words") or []
        lines = group_words_into_lines(words)

        for line_index, line_words in enumerate(lines):
            if end_of_transactions:
                break

            raw_tokens = [str(item.get("text", "")).strip() for item in line_words]
            if not raw_tokens:
                continue

            tokens = [normalize_token(t) for t in raw_tokens]
            joined_upper = clean_space(" ".join(tokens)).upper()

            # Check for end of transactions marker
            if "END OF TRANSACTIONS" in joined_upper:
                end_of_transactions = True
                break

            # Skip header lines
            if "TRANSACTION DETAILS" in joined_upper and "AMOUNT" in joined_upper:
                continue
            if "EDGE CSB BANK" in joined_upper:
                continue

            # Try to parse date at the start of the line (DD Mon YYYY)
            date_value, date_tokens_consumed = parse_multi_token_date(tokens, 0)
            if date_value is None:
                continue

            date_lines.append(
                {
                    "page": page_number,
                    "line_index": line_index,
                    "tokens": tokens,
                }
            )

            # Find the amount: look for "Rs." token followed by amount
            amount_raw: str | None = None
            amount_idx = -1
            narration_end = len(tokens)

            for k in range(date_tokens_consumed, len(tokens)):
                if tokens[k].upper() in ("RS.", "RS"):
                    if k + 1 < len(tokens):
                        amt = _parse_jupiter_amount(tokens[k + 1])
                        if amt:
                            amount_raw = amt
                            amount_idx = k + 1
                            narration_end = k  # Narration ends before "Rs."
                            break

            # Jupiter always uses "Rs." prefix for amounts; if no "Rs."
            # token was found, this is not a transaction line (e.g. it may
            # be the statement period "17 FEB 2026 - 16 MAR 2026").
            if amount_raw is None or amount_idx <= date_tokens_consumed:
                rejected_date_lines.append(
                    {
                        "page": page_number,
                        "line_index": line_index,
                        "reason": "amount_not_found",
                        "tokens": tokens,
                    }
                )
                continue

            # Extract narration: everything between date tokens and the amount/Rs.
            cursor = date_tokens_consumed
            narration_tokens = [
                t
                for t in tokens[cursor:narration_end]
                if t
                and t not in {"|", "||", ":", "--"}
                and t.upper() not in {"RS.", "RS"}
            ]

            narration = clean_space(" ".join(narration_tokens))
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

            # Extract time from the next line (HH:MM AM/PM pattern)
            time_value: str | None = None
            if line_index + 1 < len(lines):
                next_line_words = lines[line_index + 1]
                next_tokens = [
                    normalize_token(str(w.get("text", ""))) for w in next_line_words
                ]
                if len(next_tokens) >= 1:
                    # Check if first token is a time (HH:MM)
                    time_match = re.fullmatch(r"(\d{1,2}:\d{2})", next_tokens[0])
                    if time_match:
                        time_str = time_match.group(1)
                        # Check for AM/PM
                        if len(next_tokens) >= 2 and next_tokens[1].upper() in (
                            "AM",
                            "PM",
                        ):
                            time_value = f"{time_str} {next_tokens[1].upper()}"
                        else:
                            time_value = time_str

            # Determine credit/debit from narration keywords
            is_credit, credit_reason = _is_credit_narration(narration)

            amount_normalized = _normalize_jupiter_amount(amount_raw)

            transactions.append(
                Transaction(
                    date=date_value,
                    time=time_value,
                    narration=narration,
                    amount=amount_normalized,
                    transaction_type="credit" if is_credit else "debit",
                    credit_reasons=credit_reason,
                )
            )

    debug = {
        "date_lines": date_lines,
        "rejected_date_lines": rejected_date_lines,
        "detected_members": [],
    }
    return transactions, debug


def _extract_jupiter_account_summary(
    pages: list[dict[str, Any]],
) -> StatementSummary:
    """Extract account summary values from Jupiter statement.

    Jupiter has a ``STATEMENT SUMMARY`` section with labeled rows:
    - Previous balance ... Rs. X,XXX
    - Spends ... Rs. X,XXX
    - Interest charges ... Rs. X.XX
    - Fees and other charges ... Rs. X.XX
    - Applicable taxes ... Rs. X.XX
    - Repayments ... Rs. X,XXX
    - Refunds and reversals ... Rs. X.XX
    - Waivers ... Rs. X.XX
    - Total amount due ... Rs. X,XXX
    """
    previous_balance: str | None = None
    spends: str | None = None
    interest_charges: str | None = None
    fees: str | None = None
    applicable_taxes: str | None = None
    repayments: str | None = None
    refunds: str | None = None
    waivers: str | None = None
    in_summary = False

    def _extract_rs_amount(line_tokens: list[str]) -> str | None:
        """Find ``Rs. AMOUNT`` in a token list."""
        for k, t in enumerate(line_tokens):
            if t.upper() in ("RS.", "RS") and k + 1 < len(line_tokens):
                amt = _parse_jupiter_amount(line_tokens[k + 1])
                if amt:
                    return _normalize_jupiter_amount(amt)
        return None

    for page in pages[:2]:
        words = page.get("words") or []
        if not words:
            continue

        lines = group_words_into_lines(words)

        for i, line_words in enumerate(lines):
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined_upper = clean_space(" ".join(tokens)).upper()

            if "STATEMENT SUMMARY" in joined_upper:
                in_summary = True
                continue

            if not in_summary:
                continue

            # Identify which summary field this label line is for
            label: str | None = None
            if "PREVIOUS BALANCE" in joined_upper:
                label = "previous_balance"
            elif joined_upper.startswith("SPENDS"):
                label = "spends"
            elif "INTEREST CHARGES" in joined_upper:
                label = "interest_charges"
            elif "FEES" in joined_upper and "CHARGES" in joined_upper:
                label = "fees"
            elif "APPLICABLE TAXES" in joined_upper:
                label = "applicable_taxes"
            elif "REPAYMENTS" in joined_upper:
                label = "repayments"
            elif "REFUND" in joined_upper and "REVERSAL" in joined_upper:
                label = "refunds"
            elif "WAIVER" in joined_upper:
                label = "waivers"

            if label is None:
                continue

            # Try to find the Rs. amount on this line first
            line_amount = _extract_rs_amount(tokens)

            # If not found on same line, check the next line (Jupiter often
            # puts the label and the Rs. amount on separate visual lines)
            if line_amount is None and i + 1 < len(lines):
                next_tokens = [
                    normalize_token(str(w.get("text", ""))) for w in lines[i + 1]
                ]
                line_amount = _extract_rs_amount(next_tokens)

            if line_amount is None:
                continue

            if label == "previous_balance" and previous_balance is None:
                previous_balance = line_amount
            elif label == "spends" and spends is None:
                spends = line_amount
            elif label == "interest_charges" and interest_charges is None:
                interest_charges = line_amount
            elif label == "fees" and fees is None:
                fees = line_amount
            elif label == "applicable_taxes" and applicable_taxes is None:
                applicable_taxes = line_amount
            elif label == "repayments" and repayments is None:
                repayments = line_amount
            elif label == "refunds" and refunds is None:
                refunds = line_amount
            elif label == "waivers" and waivers is None:
                waivers = line_amount

    # Build summary
    candidates = [
        a
        for a in [
            previous_balance,
            spends,
            interest_charges,
            fees,
            applicable_taxes,
            repayments,
            refunds,
            waivers,
        ]
        if a
    ]

    if previous_balance or spends or repayments:
        # Compute finance charges = interest + fees + taxes (all non-transaction charges)
        finance = Decimal("0")
        if interest_charges:
            finance += parse_amount(interest_charges)
        if fees:
            finance += parse_amount(fees)
        if applicable_taxes:
            finance += parse_amount(applicable_taxes)
        finance_str = format_amount(finance) if finance > 0 else None

        # Sum repayments + refunds + waivers for total credits received
        total_credits = Decimal("0")
        for val in [repayments, refunds, waivers]:
            if val:
                total_credits += parse_amount(val)
        combined_credits = (
            format_amount(total_credits) if total_credits > 0 else repayments
        )

        return StatementSummary(
            summary_amount_candidates=candidates,
            previous_statement_dues=previous_balance,
            purchases_debit=spends,
            payments_credits_received=combined_credits,
            finance_charges=finance_str,
        )

    return StatementSummary()


class JupiterParser(StatementParser):
    """Parser entrypoint for Jupiter (Edge CSB Bank) statements."""

    bank = "jupiter"

    def __init__(self) -> None:
        self._last_txn_debug: dict[str, Any] | None = None
        self._last_transactions: list[Transaction] | None = None

    def parse(self, raw_data: dict[str, Any]) -> ParsedStatement:
        full_text = "\n".join(
            str(page.get("text", "")) for page in raw_data.get("pages", [])
        )
        pages = raw_data.get("pages", [])

        name = _extract_jupiter_name(full_text, pages)
        card_number = _extract_jupiter_card_number(
            full_text, pages
        ) or extract_card_from_filename(str(raw_data["file"]))

        transactions, txn_debug = _extract_jupiter_transactions(pages)
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
        overall_reward_points = sum_points(debit_transactions)

        due_date = _extract_jupiter_due_date(full_text, pages)
        statement_total_amount_due = _extract_jupiter_total_amount_due(full_text, pages)
        summary_fields = _extract_jupiter_account_summary(pages)
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
            transactions=debit_transactions,
            reconciliation=reconciliation,
        )

    def build_debug(self, raw_data: dict[str, Any]) -> dict[str, Any]:
        pages = raw_data.get("pages", [])

        if self._last_txn_debug is not None and self._last_transactions is not None:
            txn_debug = self._last_txn_debug
            transactions = self._last_transactions
        else:
            transactions, txn_debug = _extract_jupiter_transactions(pages)

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
