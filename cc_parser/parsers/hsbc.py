"""HSBC Bank parser profile.

HSBC credit card statements differ from other banks in several ways:
- Dates are ``DDMMM`` format without separator (e.g. ``27FEB``, ``15MAR``)
- Credit marker is ``CR`` as the last token; debits have no suffix
- Card numbers use ``NNxx xxxx xxxx NNNN`` format (lowercase ``xx``);
  normalize to uppercase ``X`` via existing helpers
- Member headers: card number followed by name on one line
- Due date format is ``DD MMM YYYY`` (e.g. ``02 APR 2026``)
- Account summary is a columnar layout under ``ACCOUNT SUMMARY``
- Section headers: ``OPENING BALANCE``, ``PURCHASES & INSTALLMENTS``,
  ``TOTAL PURCHASE OUTSTANDING``, ``NET OUTSTANDING BALANCE``
- Statement period: ``DD MMM YYYY To DD MMM YYYY``
"""

import re
from datetime import datetime
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
from cc_parser.parsers.models import (
    CardSummary,
    ParsedStatement,
    PersonGroup,
    StatementSummary,
    Transaction,
)
from cc_parser.parsers.narration import (
    clean_narration_artifacts,
)
from cc_parser.parsers.adjustment_pairing import detect_adjustment_pairs
from cc_parser.parsers.summary.grouping import (
    build_card_summaries,
    group_transactions_by_person,
)
from cc_parser.parsers.summary.reconciliation import build_reconciliation
from cc_parser.parsers.transaction_id_generator import assign_transaction_ids
from cc_parser.parsers.tokens import (
    MONTH_ABBREVS,
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

# Section headers and noise lines that should not be treated as transactions
HSBC_STOP_HEADERS = {
    "TOTAL PURCHASE OUTSTANDING",
    "NET OUTSTANDING BALANCE",
    "REWARD POINTS SUMMARY",
    "IMPORTANT INFORMATION",
    "PAYMENT SUMMARY",
}

# Regex for HSBC DDMMM date format (e.g. "27FEB", "15MAR", "01JAN")
_DDMMM_RE = re.compile(r"^(\d{2})([A-Z]{3})$", re.IGNORECASE)

# Detail printed directly below an EMI principal/interest posting. These lines
# identify internal loan-to-statement transfer credits, not payments/refunds.
_HSBC_INSTALLMENT_DETAIL_RE = re.compile(
    r"^\d+(?:ST|ND|RD|TH)\s+OF\s+\d+\s+INSTALLMENTS?\s+"
    r"(?:PRINCIPAL|INTEREST)$",
    re.IGNORECASE,
)

# Regex for HSBC card number pattern: ``NNxx xxxx xxxx NNNN``
# Lowercase xx's with spaces between groups
_HSBC_CARD_RE = re.compile(r"\d{2}[xX]{2}\s+[xX]{4}\s+[xX]{4}\s+\d{4}")


def _parse_hsbc_date(token: str, year: str) -> str | None:
    """Parse HSBC ``DDMMM`` date token into ``DD/MM/YYYY``.

    Args:
        token: Raw token like ``27FEB`` or ``15MAR``.
        year: Four-digit year from statement period context.

    Returns:
        Date string in ``DD/MM/YYYY`` format, or None if not a valid date.
    """
    match = _DDMMM_RE.fullmatch(token.strip())
    if not match:
        return None
    day = match.group(1)
    month_abbr = match.group(2).upper()
    month = MONTH_ABBREVS.get(month_abbr)
    if month is None:
        return None
    return f"{day}/{month}/{year}"


def _normalize_hsbc_card(raw: str) -> str:
    """Normalize HSBC card token to standard ``XXXX XXXX XXXX NNNN`` format.

    Converts lowercase ``xx`` to uppercase ``X`` and formats in groups of 4.
    """
    # Remove spaces, uppercase everything
    digits = raw.replace(" ", "").upper()
    if len(digits) >= 16:
        return f"{digits[:4]} {digits[4:8]} {digits[8:12]} {digits[12:16]}"
    return digits


def _extract_statement_year(full_text: str) -> str:
    """Extract statement year from the statement period line.

    Looks for ``DD MMM YYYY To DD MMM YYYY`` pattern and returns the
    end-date year as the primary year context for transaction dates.
    """
    match = re.search(
        r"(\d{1,2})\s+([A-Z]{3})\s+(\d{4})\s+To\s+(\d{1,2})\s+([A-Z]{3})\s+(\d{4})",
        full_text,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(6)

    # Fallback: look for any 4-digit year near "Statement"
    year_match = re.search(r"20\d{2}", full_text)
    if year_match:
        return year_match.group(0)

    return str(datetime.now().year)


def _extract_statement_period_months(
    full_text: str,
) -> tuple[str, str, str, str] | None:
    """Extract start and end month/year from statement period.

    Returns (start_month, start_year, end_month, end_year) or None.
    """
    match = re.search(
        r"(\d{1,2})\s+([A-Z]{3})\s+(\d{4})\s+To\s+(\d{1,2})\s+([A-Z]{3})\s+(\d{4})",
        full_text,
        flags=re.IGNORECASE,
    )
    if match:
        return (
            match.group(2).upper(),
            match.group(3),
            match.group(5).upper(),
            match.group(6),
        )
    return None


def _resolve_year_for_date(
    month_abbr: str, period_info: tuple[str, str, str, str] | None, default_year: str
) -> str:
    """Resolve the correct year for a transaction date.

    When the statement period spans two years (e.g. DEC 2025 to JAN 2026),
    dates in the start month should use the start year.
    """
    if period_info is None:
        return default_year

    start_month, start_year, _end_month, end_year = period_info

    # If start and end year differ, assign based on month
    if start_year != end_year:
        start_mm = MONTH_ABBREVS.get(start_month, "00")
        txn_mm = MONTH_ABBREVS.get(month_abbr, "00")

        if txn_mm >= start_mm:
            return start_year
        return end_year

    return end_year


def _extract_hsbc_name(full_text: str, pages: list[dict[str, Any]]) -> str | None:
    """Extract cardholder name from HSBC statement.

    Strips the honorific prefix before returning.
    """
    # Look for honorific + name pattern in text
    name_match = re.search(
        r"(MR|MRS|MS|MISS|DR)\.?\s+([A-Z][A-Z ]{3,}?)\s*\n",
        full_text,
        flags=re.IGNORECASE,
    )
    if name_match:
        candidate = clean_space(name_match.group(2)).upper()
        parts = candidate.split()
        if 2 <= len(parts) <= 5:
            return candidate

    # Fallback: search word-level on first page for card number + name pattern
    if pages:
        lines = group_words_into_lines(pages[0].get("words") or [])
        for line_words in lines:
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens))
            # Look for card pattern followed by a name
            card_match = _HSBC_CARD_RE.search(joined)
            if card_match:
                after_card = joined[card_match.end() :].strip()
                if after_card:
                    name_parts = after_card.upper().split()
                    # Strip honorific if present
                    if name_parts and name_parts[0] in {
                        "MR",
                        "MRS",
                        "MS",
                        "MISS",
                        "DR",
                    }:
                        name_parts = name_parts[1:]
                    if 2 <= len(name_parts) <= 5 and all(
                        re.fullmatch(r"[A-Z][A-Z.'-]*", p) for p in name_parts
                    ):
                        return " ".join(name_parts)

    return None


def _extract_hsbc_card_number(
    full_text: str, pages: list[dict[str, Any]]
) -> str | None:
    """Extract card number from HSBC statement."""
    # Search in full text for the HSBC card pattern
    match = _HSBC_CARD_RE.search(full_text)
    if match:
        return _normalize_hsbc_card(match.group(0))

    # Word-level search
    for page in pages[:3]:
        lines = group_words_into_lines(page.get("words") or [])
        for line_words in lines:
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens))
            card_match = _HSBC_CARD_RE.search(joined)
            if card_match:
                return _normalize_hsbc_card(card_match.group(0))

    # Fall back to generic card detection
    candidates = find_card_candidates(full_text)
    return candidates[0] if candidates else None


def _extract_hsbc_due_date(full_text: str, pages: list[dict[str, Any]]) -> str | None:
    """Extract due date from HSBC statement (``DD MMM YYYY`` format)."""
    # Line-level search
    for page in pages[:2]:
        lines = group_words_into_lines(page.get("words") or [])
        for i, line_words in enumerate(lines):
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens)).upper()
            if "PAYMENT DUE DATE" not in joined and "DUE DATE" not in joined:
                continue

            # Look for DD MMM YYYY on same line
            date_match = re.search(r"(\d{1,2})\s+([A-Z]{3})\s+(\d{4})", joined)
            if date_match:
                month = MONTH_ABBREVS.get(date_match.group(2))
                if month:
                    return (
                        f"{date_match.group(1).zfill(2)}/{month}/{date_match.group(3)}"
                    )

            # Check next line
            if i + 1 < len(lines):
                next_tokens = [
                    normalize_token(str(w.get("text", ""))) for w in lines[i + 1]
                ]
                next_joined = clean_space(" ".join(next_tokens)).upper()
                date_match = re.search(r"(\d{1,2})\s+([A-Z]{3})\s+(\d{4})", next_joined)
                if date_match:
                    month = MONTH_ABBREVS.get(date_match.group(2))
                    if month:
                        return f"{date_match.group(1).zfill(2)}/{month}/{date_match.group(3)}"

    # Text-level fallback: look for explicit "Due Date" label
    match = re.search(
        r"(?:Payment\s+Due\s+Date|Due\s+Date)\s*[:\-]?\s*(\d{1,2})\s+([A-Z]{3})\s+(\d{4})",
        full_text,
        flags=re.IGNORECASE,
    )
    if match:
        month = MONTH_ABBREVS.get(match.group(2).upper())
        if month:
            return f"{match.group(1).zfill(2)}/{month}/{match.group(3)}"

    # HSBC-specific fallback: the due date appears as a DD MMM YYYY in the
    # top header area of page 1, without any explicit label.  It is the
    # first DD MMM YYYY on the page that is NOT part of the statement period.
    if pages:
        # Extract statement period dates to exclude them
        period_dates: set[str] = set()
        period_match = re.search(
            r"(\d{1,2}\s+[A-Z]{3}\s+\d{4})\s+To\s+(\d{1,2}\s+[A-Z]{3}\s+\d{4})",
            full_text,
            flags=re.IGNORECASE,
        )
        if period_match:
            period_dates.add(clean_space(period_match.group(1)).upper())
            period_dates.add(clean_space(period_match.group(2)).upper())

        lines = group_words_into_lines(pages[0].get("words") or [])
        for line_words in lines[:15]:  # only scan top of page
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens)).upper()
            date_match = re.search(r"(\d{1,2})\s+([A-Z]{3})\s+(\d{4})", joined)
            if date_match:
                candidate = clean_space(date_match.group(0)).upper()
                if candidate not in period_dates:
                    month = MONTH_ABBREVS.get(date_match.group(2))
                    if month:
                        return f"{date_match.group(1).zfill(2)}/{month}/{date_match.group(3)}"

    return None


def _has_cr_marker(tokens: list[str]) -> bool:
    """Check if any token is or ends with a ``CR`` credit marker.

    Handles both separate markers (``["250.00", "CR"]``) and merged
    tokens (``["250.00CR"]``) that some PDF extractors produce.
    """
    for t in tokens:
        upper = normalize_token(t).upper()
        if upper == "CR":
            return True
        if upper.endswith("CR") and len(upper) > 2:
            return True
    return False


def _extract_hsbc_amount_from_tokens(tokens: list[str]) -> str | None:
    """Return the rightmost HSBC amount in a visual line.

    Args:
        tokens: Normalized words from one visual line.

    Returns:
        A normalized amount, negated when the line carries a ``CR`` marker.
    """
    for raw_token in reversed(tokens):
        token = raw_token
        if token.upper().endswith(("CR", "DR")) and len(token) > 2:
            token = token[:-2]
        if amount := parse_amount_token(token):
            result = normalize_amount(amount)
            return f"-{result}" if _has_cr_marker(tokens) else result
    return None


def _extract_hsbc_total_amount_due(
    _full_text: str, pages: list[dict[str, Any]]
) -> str | None:
    """Extract total payment due from HSBC's page-1 payment summary.

    ``Net outstanding balance`` includes principal from future loan instalments
    and is not the amount payable for the current statement. Some HSBC PDFs
    render payment-summary labels as non-extractable text, leaving the total due
    on the same visual line as the statement period; that positional structure
    is therefore the primary unlabeled fallback.
    """
    first_page = pages[0] if pages else {}
    lines = group_words_into_lines(first_page.get("words") or [])

    # Prefer the explicit payment-summary label when its font is extractable.
    for i, line_words in enumerate(lines):
        tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
        joined = clean_space(" ".join(tokens)).upper()
        if "TOTAL PAYMENT DUE" not in joined:
            continue
        if amount := _extract_hsbc_amount_from_tokens(tokens):
            return amount
        if i + 1 < len(lines):
            next_tokens = [
                normalize_token(str(w.get("text", ""))) for w in lines[i + 1]
            ]
            if amount := _extract_hsbc_amount_from_tokens(next_tokens):
                return amount

    # In the standard two-column PAYMENT SUMMARY, the statement period and
    # total payment due occupy the same visual row. The dark cell labels may be
    # absent from extracted text even though both values remain available.
    period_re = re.compile(
        r"\b\d{1,2}\s+[A-Z]{3}\s+\d{4}\s+TO\s+"
        r"\d{1,2}\s+[A-Z]{3}\s+\d{4}\b",
        flags=re.IGNORECASE,
    )
    for line_words in lines:
        tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
        joined = clean_space(" ".join(tokens))
        if period_re.search(joined):
            if amount := _extract_hsbc_amount_from_tokens(tokens):
                return amount

    # Keep text fallback bounded to page 1 so tariff examples cannot leak in.
    first_page_text = str(first_page.get("text", ""))
    match = re.search(
        r"Total\s+Payment\s+Due\s*[:\-]?\s*([\d,]+\.\d{2})\s*(CR)?",
        first_page_text,
        flags=re.IGNORECASE,
    )
    if match:
        result = normalize_amount(match.group(1))
        return f"-{result}" if match.group(2) else result

    # Do not guess from purchase, loan, or net-outstanding totals. Those fields
    # describe different scopes and can include future principal; missing the
    # payment-summary evidence is safer than returning a plausible wrong due.
    return None


def _extract_hsbc_transactions(
    pages: list[dict[str, Any]],
    statement_year: str,
    period_info: tuple[str, str, str, str] | None,
) -> tuple[list[Transaction], dict[str, Any]]:
    """Parse transactions from HSBC statement pages.

    HSBC transaction lines follow the pattern:
    ``DDMMM  NARRATION  AMOUNT [CR]``

    Transactions appear between ``PURCHASES & INSTALLMENTS`` (or card/member
    header) and ``TOTAL PURCHASE OUTSTANDING`` / ``NET OUTSTANDING BALANCE``.
    """
    transactions: list[Transaction] = []
    current_card: str | None = None
    current_member: str | None = None
    in_transaction_section = False
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

            # Detect card number + member name headers
            card_match = _HSBC_CARD_RE.search(clean_space(" ".join(tokens)))
            if card_match:
                current_card = _normalize_hsbc_card(card_match.group(0))
                after_card = clean_space(" ".join(tokens))[card_match.end() :].strip()
                if after_card:
                    name_parts = after_card.upper().split()
                    # Strip honorific if present
                    if name_parts and name_parts[0] in {
                        "MR",
                        "MRS",
                        "MS",
                        "MISS",
                        "DR",
                    }:
                        name_parts = name_parts[1:]
                    if 2 <= len(name_parts) <= 5 and all(
                        re.fullmatch(r"[A-Z][A-Z.'-]*", p) for p in name_parts
                    ):
                        current_member = " ".join(name_parts)
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

            # Detect section headers
            if "PURCHASES" in joined_upper and "INSTALLMENTS" in joined_upper:
                in_transaction_section = True
                continue

            # Stop parsing at summary/total lines
            if any(header in joined_upper for header in HSBC_STOP_HEADERS):
                in_transaction_section = False
                continue

            # "OPENING BALANCE" marks the start of the payment/balance area;
            # transactions (e.g. payments clearing the opening balance) can
            # appear between OPENING BALANCE and PURCHASES & INSTALLMENTS.
            if "OPENING BALANCE" in joined_upper:
                in_transaction_section = True
                continue
            if "ACCOUNT SUMMARY" in joined_upper:
                continue
            if "HSBC PLATINUM" in joined_upper:
                continue
            if "CREDIT CARD STATEMENT" in joined_upper:
                continue
            # Skip the "Interest Rate applicable" info lines
            if "INTEREST RATE" in joined_upper:
                continue

            if not in_transaction_section:
                continue

            # Try to parse DDMMM date at the start of the line
            if not tokens:
                continue

            date_value = _parse_hsbc_date(tokens[0], statement_year)
            if date_value is None:
                # Sometimes the date might be split; try combining first two tokens
                if len(tokens) >= 2:
                    combined = tokens[0] + tokens[1]
                    date_value = _parse_hsbc_date(combined, statement_year)
                    if date_value is not None:
                        # Consume both tokens as date
                        tokens = [tokens[0] + tokens[1]] + tokens[2:]

            if date_value is None:
                continue

            # When the date token is alone (or has no amount), the
            # narration/amount may have been split to the next visual line
            # due to slight y-offset in the PDF layout.  Merge the next
            # line's tokens into the current one.
            has_amount = any(parse_amount_token(t) is not None for t in tokens[1:])
            detail_line_index = line_index + 1
            if not has_amount and line_index + 1 < len(lines):
                next_line_words = lines[line_index + 1]
                next_tokens = [
                    normalize_token(str(item.get("text", "")).strip())
                    for item in next_line_words
                ]
                # Only merge if the next line does NOT start with a DDMMM date
                if (
                    next_tokens
                    and _parse_hsbc_date(next_tokens[0], statement_year) is None
                ):
                    tokens = tokens + next_tokens
                    detail_line_index += 1

            installment_detail: str | None = None
            if detail_line_index < len(lines):
                detail_tokens = [
                    normalize_token(str(item.get("text", "")).strip())
                    for item in lines[detail_line_index]
                ]
                detail_candidate = clean_space(" ".join(detail_tokens))
                if _HSBC_INSTALLMENT_DETAIL_RE.fullmatch(detail_candidate):
                    installment_detail = detail_candidate.upper()

            # Resolve year based on month in the date token
            ddmmm_match = _DDMMM_RE.fullmatch(tokens[0].strip())
            if ddmmm_match:
                month_abbr = ddmmm_match.group(2).upper()
                resolved_year = _resolve_year_for_date(
                    month_abbr, period_info, statement_year
                )
                date_value = _parse_hsbc_date(tokens[0], resolved_year)

            date_lines.append(
                {
                    "page": page_number,
                    "line_index": line_index,
                    "tokens": tokens,
                    "installment_detail": installment_detail,
                    "current_member": current_member,
                    "current_card": current_card,
                }
            )

            # Determine credit/debit from last token
            # Handles both separate markers ("CR") and merged ("1280.25CR").
            # HSBC uses only "CR" (not bare "C"/"D" like SBI).
            last_token = tokens[-1].upper() if tokens else ""
            if last_token == "CR":
                is_credit = True
            elif last_token == "DR":
                is_credit = False
            elif last_token.endswith("CR"):
                is_credit = True
            elif last_token.endswith("DR"):
                is_credit = False
            else:
                is_credit = False

            # Find the amount: rightmost amount-matching token before DR/CR
            search_end = len(tokens)
            if last_token in {"CR", "DR"}:
                search_end = len(tokens) - 1

            amount_idx = -1
            for i in range(search_end - 1, 0, -1):
                t = tokens[i]
                # Strip merged CR/DR suffix before checking for amount
                if t.upper().endswith(("CR", "DR")) and len(t) > 2:
                    t = t[:-2]
                if parse_amount_token(t) is not None:
                    amount_idx = i
                    break

            if amount_idx == -1 or amount_idx <= 0:
                rejected_date_lines.append(
                    {
                        "page": page_number,
                        "line_index": line_index,
                        "reason": "amount_not_found",
                        "tokens": tokens,
                    }
                )
                continue

            # Narration: everything between date token and amount
            cursor = 1  # skip date token
            while cursor < len(tokens) and tokens[cursor] in SEPARATOR_TOKENS:
                cursor += 1

            narration_end = amount_idx

            narration_tokens = [
                t
                for t in tokens[cursor:narration_end]
                if t
                and t not in SEPARATOR_TOKENS
                and t not in {"+", "l", "I"}
                and t.upper() not in {"DR", "CR"}
            ]

            narration = clean_space(" ".join(narration_tokens))
            narration = clean_narration_artifacts(narration)
            if installment_detail:
                narration = clean_space(f"{narration} {installment_detail}")

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

            amount_raw = tokens[amount_idx]
            if amount_raw.upper().endswith(("CR", "DR")) and len(amount_raw) > 2:
                amount_raw = amount_raw[:-2]
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
                credit_reason = (
                    "emi_installment_transfer" if installment_detail else "cr_marker"
                )

            transactions.append(
                Transaction(
                    date=date_value or "",
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


def _split_hsbc_reconciliation_credits(
    debit_transactions: list[Transaction],
    credit_transactions: list[Transaction],
) -> tuple[list[Transaction], list[Transaction]]:
    """Separate payable credits from paired EMI transfer credits.

    HSBC prints an internal ``CR`` transfer immediately before the matching
    billed EMI debit. Both rows remain in parsed output, but subtracting that
    transfer from current dues without also parsing the hidden loan-ledger debit
    understates the payable amount.

    Args:
        debit_transactions: Parsed HSBC debit rows.
        credit_transactions: Parsed HSBC credit rows.

    Returns:
        ``(payable_credits, excluded_emi_transfers)`` for smart reconciliation.
    """
    debit_keys = {
        (txn.date, txn.amount, txn.narration.upper()) for txn in debit_transactions
    }
    payable_credits: list[Transaction] = []
    emi_transfers: list[Transaction] = []
    for txn in credit_transactions:
        key = (txn.date, txn.amount, txn.narration.upper())
        if txn.credit_reasons == "emi_installment_transfer" and key in debit_keys:
            emi_transfers.append(txn)
        else:
            payable_credits.append(txn)
    return payable_credits, emi_transfers


def _extract_hsbc_account_summary(
    pages: list[dict[str, Any]],
) -> StatementSummary:
    """Extract account summary values from HSBC statement.

    HSBC has a columnar layout under ACCOUNT SUMMARY with headers like
    ``Opening balance | Purchase & other charges | Payment & other credits | Net outstanding balance``
    followed by a values row with amounts.
    """
    for page in pages[:2]:
        words = page.get("words") or []
        if not words:
            continue

        lines = group_words_into_lines(words)

        for i, line_words in enumerate(lines):
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined_upper = clean_space(" ".join(tokens)).upper()

            # Look for the header row with "Opening balance" and purchase/payment
            if "OPENING BALANCE" in joined_upper and (
                "PURCHASE" in joined_upper or "PAYMENT" in joined_upper
            ):
                # This is the header row; values should be on the next line
                for offset in range(1, 5):
                    idx = i + offset
                    if idx >= len(lines):
                        break
                    val_words = sorted(lines[idx], key=lambda w: w.get("x0", 0))
                    amounts: list[str] = []
                    for w in val_words:
                        t = normalize_token(str(w.get("text", "")))
                        amt = parse_amount_token(t)
                        if amt:
                            amounts.append(normalize_amount(amt))

                    if len(amounts) >= 3:
                        # [opening_bal, purchases, payments, net_outstanding]
                        result = StatementSummary(
                            summary_amount_candidates=amounts,
                            previous_statement_dues=amounts[0],
                            purchases_debit=amounts[1],
                        )
                        if len(amounts) >= 3:
                            result.payments_credits_received = amounts[2]
                        return result

    # Fallback: look for individual labeled lines
    opening_balance: str | None = None
    purchases: str | None = None
    payments_credits: str | None = None

    for page in pages[:2]:
        words = page.get("words") or []
        if not words:
            continue

        lines = group_words_into_lines(words)

        for line_words in lines:
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined_upper = clean_space(" ".join(tokens)).upper()

            if "OPENING BALANCE" in joined_upper and opening_balance is None:
                for t in reversed(tokens):
                    amt = parse_amount_token(t)
                    if amt:
                        opening_balance = normalize_amount(amt)
                        break

            if (
                "PURCHASE" in joined_upper
                and "CHARGE" in joined_upper
                and purchases is None
            ):
                for t in reversed(tokens):
                    amt = parse_amount_token(t)
                    if amt:
                        purchases = normalize_amount(amt)
                        break

            if (
                "PAYMENT" in joined_upper
                and "CREDIT" in joined_upper
                and payments_credits is None
            ):
                for t in reversed(tokens):
                    amt = parse_amount_token(t)
                    if amt:
                        payments_credits = normalize_amount(amt)
                        break

    if opening_balance or purchases or payments_credits:
        candidates = [a for a in [opening_balance, purchases, payments_credits] if a]
        return StatementSummary(
            summary_amount_candidates=candidates,
            previous_statement_dues=opening_balance,
            purchases_debit=purchases,
            payments_credits_received=payments_credits,
        )

    return StatementSummary()


def _normalize_hsbc_reward_points(value: str) -> str:
    """Normalize a points value to a non-monetary numeric string.

    Args:
        value: Extracted points value, often rendered with two decimal places.

    Returns:
        Integer-looking points without decimal places, preserving a fractional
        component only when the statement actually contains one.
    """
    points = parse_amount(value)
    if points == points.to_integral_value():
        return str(int(points))
    return format(points.normalize(), "f")


def _apply_hsbc_reward_rollups(
    card_summaries: list[CardSummary],
    person_groups: list[PersonGroup],
    earned_points: str | None,
) -> None:
    """Attach statement-only earnings when they have one unambiguous owner.

    HSBC does not print points per transaction. When exactly one card/person
    group exists, the statement's earned value can still populate those
    rollups. Multi-card statements remain unallocated rather than fabricating a
    split.

    Args:
        card_summaries: Parsed card-level rollups to update in place.
        person_groups: Parsed person-level rollups to update in place.
        earned_points: Statement-declared points earned during this cycle.

    Returns:
        None.
    """
    if earned_points is None or len(card_summaries) != 1 or len(person_groups) != 1:
        return
    card_summaries[0].reward_points_total = earned_points
    person_groups[0].reward_points_total = earned_points


def _extract_hsbc_reward_points(
    pages: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    """Extract reward points from HSBC REWARD POINT SUMMARY.

    Layout: ``Opening Balance | Earned | Redeemed | Closing balance``
    Values: ``0.00           | 17.00  | 0.00     | 17.00``
    Returns ``(earned, closing_balance)``.

    The column headers may be rendered as images, so we also look for
    the last row of exactly 4 amounts on page 1 (after the ACCOUNT
    SUMMARY values row which also has 4 amounts).
    """
    for page in pages[:2]:
        words = page.get("words") or []
        if not words:
            continue

        lines = group_words_into_lines(words)

        # Strategy 1: look for "REWARD" header with values on next line
        for i, line_words in enumerate(lines):
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined_upper = clean_space(" ".join(tokens)).upper()

            if "REWARD" in joined_upper and (
                "EARNED" in joined_upper or "POINT" in joined_upper
            ):
                for offset in range(1, 5):
                    idx = i + offset
                    if idx >= len(lines):
                        break
                    val_words = sorted(lines[idx], key=lambda w: w.get("x0", 0))
                    amounts: list[str] = []
                    for w in val_words:
                        t = normalize_token(str(w.get("text", "")))
                        amt = parse_amount_token(t)
                        if amt:
                            amounts.append(normalize_amount(amt))
                    if len(amounts) >= 4:
                        return (
                            _normalize_hsbc_reward_points(amounts[1]),
                            _normalize_hsbc_reward_points(amounts[3]),
                        )
                    if len(amounts) >= 2:
                        return _normalize_hsbc_reward_points(amounts[1]), None

        # Strategy 2: the reward summary is the last 4-amount row on
        # page 1 (the first such row is typically the account summary).
        # Only use this fallback when we see at least two such rows
        # to avoid mistaking a monetary summary for reward points.
        four_amount_rows: list[list[str]] = []
        for line_words in lines:
            val_words = sorted(line_words, key=lambda w: w.get("x0", 0))
            amounts: list[str] = []
            for w in val_words:
                t = normalize_token(str(w.get("text", "")))
                amt = parse_amount_token(t)
                if amt:
                    amounts.append(normalize_amount(amt))
            if len(amounts) == 4:
                four_amount_rows.append(amounts)

        if len(four_amount_rows) >= 2:
            # Second 4-amount row: [opening, earned, redeemed, closing]
            reward_row = four_amount_rows[-1]
            return (
                _normalize_hsbc_reward_points(reward_row[1]),
                _normalize_hsbc_reward_points(reward_row[3]),
            )

    return None, None


def _person_tokens(label: str | None) -> list[str]:
    """Return the uppercase whitespace tokens of a person label.

    Args:
        label: Raw person label, or None.

    Returns:
        Uppercase tokens. Empty when the label has none.
    """
    return clean_space((label or "")).upper().split()


def _is_subsequence(short: list[str], long: list[str]) -> bool:
    """Return True when ``short`` is an order-preserving subsequence of ``long``.

    Args:
        short: Candidate shorter token list.
        long: Candidate longer token list.

    Returns:
        True when every token of ``short`` appears in ``long`` in order.
    """
    it = iter(long)
    return all(token in it for token in short)


def _canonicalize_hsbc_persons(
    transactions: list[Transaction], statement_name: str | None
) -> None:
    """Collapse short/full spellings of one cardholder on the same card.

    HSBC prints the cardholder name two ways. The address block gives a fuller
    name. The member header next to the card number gives a shorter one. Rows
    outside a member header fall back to the fuller name. The result is two
    person labels on one physical card, which splits card summaries and shows a
    phantom add-on card.

    This step rewrites the shorter label to the fuller one when the shorter is a
    proper order-preserving subsequence of a single fuller label under the same
    card mask. The rewrite is scoped per exact card mask. It requires at least
    two tokens on both sides. It skips a mask when more than one distinct fuller
    label could absorb the shorter one, because that is genuine ambiguity.

    Genuine add-on cards keep distinct masks, so they are never merged.

    Args:
        transactions: Parsed HSBC rows. Modified in place.
        statement_name: Statement-level name. Preferred as the canonical winner
            when it equals the resolved fuller label.

    Returns:
        None.
    """
    labels_by_mask: dict[str, set[str]] = {}
    for txn in transactions:
        mask = (txn.card_number or "").strip()
        person = (txn.person or "").strip()
        if not mask or not person:
            continue
        labels_by_mask.setdefault(mask, set()).add(person)

    # Map each shorter label to the fuller label that must replace it.
    rewrite_by_mask: dict[str, dict[str, str]] = {}
    for mask, labels in labels_by_mask.items():
        token_lists = {label: _person_tokens(label) for label in labels}
        rewrites: dict[str, str] = {}
        for short_label, short_tokens in token_lists.items():
            if len(short_tokens) < 2:
                continue
            supersets = [
                long_label
                for long_label, long_tokens in token_lists.items()
                if long_label != short_label
                and len(long_tokens) > len(short_tokens)
                and len(long_tokens) >= 2
                and _is_subsequence(short_tokens, long_tokens)
            ]
            if len(supersets) != 1:
                continue
            winner = supersets[0]
            if (
                statement_name
                and _person_tokens(statement_name) == token_lists[winner]
            ):
                winner = statement_name
            rewrites[short_label] = winner
        if rewrites:
            rewrite_by_mask[mask] = rewrites

    if not rewrite_by_mask:
        return

    for txn in transactions:
        mask = (txn.card_number or "").strip()
        person = (txn.person or "").strip()
        winner = rewrite_by_mask.get(mask, {}).get(person)
        if winner:
            txn.person = winner


class HsbcParser(StatementParser):
    """Parser entrypoint for HSBC Bank statements."""

    bank = "hsbc"

    def __init__(self) -> None:
        self._last_txn_debug: dict[str, Any] | None = None
        self._last_transactions: list[Transaction] | None = None

    def parse(self, raw_data: dict[str, Any]) -> ParsedStatement:
        full_text = "\n".join(
            str(page.get("text", "")) for page in raw_data.get("pages", [])
        )
        pages = raw_data.get("pages", [])

        statement_year = _extract_statement_year(full_text)
        period_info = _extract_statement_period_months(full_text)

        name = _extract_hsbc_name(full_text, pages)
        card_number = _extract_hsbc_card_number(
            full_text, pages
        ) or extract_card_from_filename(str(raw_data["file"]))

        transactions, txn_debug = _extract_hsbc_transactions(
            pages, statement_year, period_info
        )
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

        # Collapse short/full spellings of one cardholder on the same card.
        _canonicalize_hsbc_persons(transactions, name)

        debit_transactions, credit_transactions = split_by_transaction_type(
            transactions
        )

        # Assign transaction IDs
        debit_transactions = assign_transaction_ids(debit_transactions, self.bank)
        credit_transactions = assign_transaction_ids(credit_transactions, self.bank)

        payable_credits, _emi_transfer_credits = _split_hsbc_reconciliation_credits(
            debit_transactions, credit_transactions
        )

        # Internal EMI transfers are bookkeeping rows, not refunds/reversals.
        adjustment_pairs = detect_adjustment_pairs(
            debit_transactions, payable_credits, self.bank
        )

        card_summaries, overall_total = build_card_summaries(debit_transactions, name)
        person_groups = group_transactions_by_person(debit_transactions, name)

        credit_total = sum_amounts(credit_transactions)
        # HSBC doesn't have per-transaction points; use the statement-level
        # "Earned" and "Closing balance" from the REWARD POINT SUMMARY.
        earned_points, closing_points = _extract_hsbc_reward_points(pages)
        overall_reward_points = (
            Decimal(earned_points.replace(",", ""))
            if earned_points
            else sum_points(debit_transactions)
        )
        reward_points_balance = closing_points
        _apply_hsbc_reward_rollups(card_summaries, person_groups, earned_points)

        due_date = _extract_hsbc_due_date(full_text, pages)
        statement_total_amount_due = _extract_hsbc_total_amount_due(full_text, pages)
        summary_fields = _extract_hsbc_account_summary(pages)
        reconciliation = build_reconciliation(
            statement_total_amount_due,
            debit_transactions,
            credit_transactions,
            summary_fields,
            smart_credit_transactions=payable_credits,
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
            full_text = "\n".join(str(page.get("text", "")) for page in pages)
            statement_year = _extract_statement_year(full_text)
            period_info = _extract_statement_period_months(full_text)
            transactions, txn_debug = _extract_hsbc_transactions(
                pages, statement_year, period_info
            )

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
                "emi_transfer_credits": len(
                    [
                        t
                        for t in transactions
                        if t.credit_reasons == "emi_installment_transfer"
                    ]
                ),
            },
            "card_from_filename": extract_card_from_filename(
                str(raw_data.get("file", ""))
            ),
            "detected_members": txn_debug["detected_members"],
            "date_lines": txn_debug["date_lines"],
            "rejected_date_lines": txn_debug["rejected_date_lines"],
        }
