"""Slice credit card parser profile.

Slice credit card statements differ from other banks in several ways:
- Transactions are laid out in 3 visual lines:
  Line 1: ``MERCHANT_NAME  ₹AMOUNT``
  Line 2: Single character (avatar initial — skipped)
  Line 3: ``DD Mon 'YY  •  UPI`` (date and optional payment mode)
- Section headers (``Spends``, ``Cashback``, ``EMIs``) determine debit/credit
- Amounts are prefixed with ``₹`` (e.g. ``₹459.17``, ``₹25,000``)
- Amounts may or may not have decimal places
- Dates use ``DD Mon 'YY`` format (e.g. ``16 Mar '26``) with apostrophe year
- Card number is ``XXXX XXXX XXXX DDDD``
- Name appears as ``NAME's`` on the first line
- Due date appears as ``Due on DD Mon`` (year inferred from transactions)
- Statement summary uses labeled rows: Spends, Refunds & repayments,
  Cashback, Interest, Surcharge, EMIs, Total amount due, Min amount due
- Cashback is a distinct section and should NOT be paired as adjustments
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
from cc_parser.parsers.models import ParsedStatement, StatementSummary, Transaction
from cc_parser.parsers.narration import clean_narration_artifacts
from cc_parser.parsers.reconciliation import (
    build_card_summaries,
    build_reconciliation,
    compute_adjustment_totals,
    group_transactions_by_person,
    split_paired_adjustments,
)
from cc_parser.parsers.tokens import (
    MONTH_ABBREVS,
    clean_space,
    format_amount,
    normalize_token,
    parse_amount,
    sum_amounts,
    sum_points,
)

# Matches amounts with or without decimals: "459.17", "25,000", "1,425.50"
_SLICE_AMOUNT_RE = re.compile(r"^\d[\d,]*(?:\.\d{2})?$")

# Section name constants
_SECTION_SPENDS = "SPENDS"
_SECTION_CASHBACK = "CASHBACK"
_SECTION_EMIS = "EMIS"
_SECTION_REFUNDS = "REFUNDS & REPAYMENTS"

# All recognized section headers (without amounts — standalone headers only)
_SECTION_HEADERS = {_SECTION_SPENDS, _SECTION_CASHBACK, _SECTION_EMIS,
                    _SECTION_REFUNDS, "REFUNDS"}

_DEBIT_SECTIONS = {_SECTION_SPENDS, _SECTION_EMIS}
_CREDIT_SECTIONS = {_SECTION_CASHBACK, _SECTION_REFUNDS, "REFUNDS"}


def _parse_slice_amount(token: str) -> str | None:
    """Parse a Slice amount token, stripping the ₹ prefix.

    Handles both ``₹459.17`` and ``₹25,000`` forms. Also handles
    bare amounts without the rupee prefix.
    """
    if not (cleaned := normalize_token(token).lstrip("₹").strip()):
        return None
    return cleaned if _SLICE_AMOUNT_RE.fullmatch(cleaned) else None


def _normalize_slice_amount(raw: str) -> str:
    """Normalize a Slice amount string, appending .00 if no decimals."""
    value = raw.lstrip("₹").replace("`", "").strip()
    if "." not in value:
        value = value + ".00"
    return value


def _parse_slice_date(tokens: list[str], start: int) -> tuple[str | None, int]:
    """Parse a ``DD Mon 'YY`` date spread across three tokens.

    The year token has an apostrophe prefix (e.g. ``'26`` → ``2026``).

    Returns:
        ``(date_str, tokens_consumed)`` where *date_str* is ``DD/MM/YYYY``,
        or ``(None, 0)`` on failure.
    """
    if start + 2 >= len(tokens):
        return None, 0
    day = normalize_token(tokens[start])
    month_tok = normalize_token(tokens[start + 1])
    year_tok = normalize_token(tokens[start + 2])

    if not re.fullmatch(r"\d{1,2}", day):
        return None, 0
    if (month := MONTH_ABBREVS.get(month_tok.upper()[:3])) is None:
        return None, 0

    # Handle 'YY format (e.g. "'26" → "2026")
    if not re.fullmatch(r"\d{2,4}", year_cleaned := year_tok.lstrip("'").lstrip("\u2018").lstrip("\u2019")):
        return None, 0

    day_padded = day.zfill(2)
    year = year_cleaned if len(year_cleaned) == 4 else f"20{year_cleaned}"
    return f"{day_padded}/{month}/{year}", 3


def _infer_year_from_text(full_text: str) -> str | None:
    """Infer statement year from transaction dates in the text.

    Looks for ``'YY`` patterns in the text to determine the year.
    """
    if matches := re.findall(r"[''\u2019](\d{2})\b", full_text):
        return f"20{matches[0]}"
    return None


def _extract_slice_name(
    full_text: str, pages: list[dict[str, Any]]
) -> str | None:
    """Extract cardholder name from Slice statement.

    Slice prints the name as ``NAME's`` on the first line (for example,
    ``CARDHOLDER's``).
    """
    for page in pages[:1]:
        lines = group_words_into_lines(page.get("words") or [])
        for line_words in lines[:3]:
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            if len(tokens) == 1:
                # Match "NAME's" pattern
                if match := re.fullmatch(r"([A-Za-z][A-Za-z ]+?)[''\u2019]s", tokens[0]):
                    return match.group(1).upper()

    # Text-level fallback: look for "NAME's" pattern
    if match := re.search(r"^([A-Za-z][A-Za-z ]+?)[''\u2019]s\b", full_text, re.MULTILINE):
        return match.group(1).upper()

    return None


def _extract_slice_card_number(
    full_text: str, pages: list[dict[str, Any]]
) -> str | None:
    """Extract card number from Slice statement.

    Slice uses ``XXXX XXXX XXXX DDDD`` format spread across 4 tokens.
    """
    for page in pages[:1]:
        lines = group_words_into_lines(page.get("words") or [])
        for line_words in lines:
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            for j in range(len(tokens) - 3):
                if (
                    re.fullmatch(r"[Xx]{4}", tokens[j])
                    and re.fullmatch(r"[Xx]{4}", tokens[j + 1])
                    and re.fullmatch(r"[Xx]{4}", tokens[j + 2])
                    and re.fullmatch(r"\d{4}", tokens[j + 3])
                ):
                    return f"XXXX XXXX XXXX {tokens[j + 3]}"

    # Text-level fallback
    if match := re.search(r"XXXX\s+XXXX\s+XXXX\s+(\d{4})", full_text):
        return f"XXXX XXXX XXXX {match.group(1)}"

    candidates = find_card_candidates(full_text)
    return candidates[0] if candidates else None


def _extract_slice_due_date(
    full_text: str, pages: list[dict[str, Any]]
) -> str | None:
    """Extract due date from Slice statement.

    Slice uses ``Due on DD Mon`` format (e.g. ``Due on 5 Apr``) without
    an explicit year. The year is inferred from transaction dates in the text.
    """
    year = _infer_year_from_text(full_text)

    for page in pages[:1]:
        lines = group_words_into_lines(page.get("words") or [])
        for line_words in lines:
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens)).upper()
            if "DUE" not in joined or "ON" not in joined:
                continue

            if due_match := re.search(
                r"DUE\s+ON\s+(\d{1,2})\s+([A-Za-z]{3})",
                joined,
                re.IGNORECASE,
            ):
                day = due_match.group(1).zfill(2)
                if (month := MONTH_ABBREVS.get(due_match.group(2).upper()[:3])) and year:
                    return f"{day}/{month}/{year}"

    # Text fallback
    if match := re.search(
        r"Due\s+on\s+(\d{1,2})\s+([A-Za-z]{3})",
        full_text,
        re.IGNORECASE,
    ):
        day = match.group(1).zfill(2)
        if (month := MONTH_ABBREVS.get(match.group(2).upper()[:3])) and year:
            return f"{day}/{month}/{year}"

    return None


def _extract_slice_total_amount_due(
    full_text: str, pages: list[dict[str, Any]]
) -> str | None:
    """Extract total amount due from Slice statement summary.

    Slice has ``Total amount due ₹AMOUNT`` in the Statement summary section.
    """
    for page in pages[:1]:
        lines = group_words_into_lines(page.get("words") or [])
        for line_words in lines:
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens)).upper()
            if "TOTAL AMOUNT DUE" not in joined:
                continue

            for t in tokens:
                if t.startswith("₹") and (amt := _parse_slice_amount(t)):
                    return _normalize_slice_amount(amt)

    # Text fallback
    if match := re.search(
        r"Total\s+amount\s+due\s+₹([\d,]+(?:\.\d{2})?)",
        full_text,
        re.IGNORECASE,
    ):
        return _normalize_slice_amount(match.group(1))

    return None


def _extract_slice_transactions(
    pages: list[dict[str, Any]],
) -> tuple[list[Transaction], dict[str, Any]]:
    """Parse transactions from Slice statement pages.

    Slice transaction layout (per transaction):
      Line A: ``MERCHANT_NAME  ₹AMOUNT``  (narration + amount)
      Line B: Single character              (avatar initial — skip)
      Line C: ``DD Mon 'YY  •  UPI``       (date + optional payment mode)

    Section headers (``Spends``, ``Cashback``, ``EMIs``) determine
    whether transactions are debits or credits.

    Only standalone section headers (without ₹ amounts) activate transaction
    parsing. Summary rows like ``Spends ₹35,898.67`` on page 1 are skipped.
    """
    transactions: list[Transaction] = []
    date_lines: list[dict[str, Any]] = []
    rejected_date_lines: list[dict[str, Any]] = []
    current_section: str | None = None  # No active section until we see a header
    scanning_done = False  # Once we hit glossary/GST, stop permanently

    # Collect all visual lines across pages with their page info
    all_lines: list[tuple[int, list[dict[str, Any]]]] = []
    for page in pages:
        page_number = int(page.get("page_number", 0) or 0)
        words = page.get("words") or []
        lines = group_words_into_lines(words)
        for line_words in lines:
            all_lines.append((page_number, line_words))

    line_idx = 0
    while line_idx < len(all_lines):
        page_number, line_words = all_lines[line_idx]
        raw_tokens = [str(item.get("text", "")).strip() for item in line_words]
        if not raw_tokens:
            line_idx += 1
            continue

        tokens = [normalize_token(t) for t in raw_tokens]
        joined_upper = clean_space(" ".join(tokens)).upper()

        # Once we've hit glossary/GST, stop scanning permanently —
        # glossary pages reuse section names like "Spends" in prose.
        if scanning_done:
            line_idx += 1
            continue

        # Check if this is a standalone section header (no ₹ amount)
        has_rupee = any(t.startswith("₹") for t in tokens)
        if joined_upper in _SECTION_HEADERS and not has_rupee:
            current_section = joined_upper
            line_idx += 1
            continue

        # Skip everything until we've seen a real section header
        if current_section is None:
            line_idx += 1
            continue

        # Non-transaction sections end scanning permanently
        if any(skip in joined_upper for skip in (
            "GST DETAILS", "GLOSSARY", "MONIES",
        )):
            scanning_done = True
            line_idx += 1
            continue

        # Try to detect a transaction line: NARRATION ₹AMOUNT
        amount_raw: str | None = None
        narration_end = len(tokens)

        # Look for ₹AMOUNT token (usually last token on the line)
        for k in range(len(tokens) - 1, -1, -1):
            if tokens[k].startswith("₹") and (amt := _parse_slice_amount(tokens[k])):
                amount_raw = amt
                narration_end = k
                break

        if amount_raw is None:
            line_idx += 1
            continue

        # Build narration from tokens before the amount
        narration_tokens = [
            t for t in tokens[:narration_end]
            if t and t not in {"|", "||", ":", "--"}
        ]
        narration = clean_space(" ".join(narration_tokens))
        narration = clean_narration_artifacts(narration)

        if not narration:
            line_idx += 1
            continue

        # Now look ahead for the date line.
        # Pattern: skip avatar initial line (single char), then find date.
        date_value: str | None = None
        lookahead = 1
        max_lookahead = 4

        while lookahead <= max_lookahead and (line_idx + lookahead) < len(all_lines):
            _, next_line_words = all_lines[line_idx + lookahead]
            next_tokens = [
                normalize_token(str(w.get("text", "")))
                for w in next_line_words
            ]

            if not next_tokens:
                lookahead += 1
                continue

            # Skip single-character avatar initials
            if len(next_tokens) == 1 and len(next_tokens[0]) == 1:
                lookahead += 1
                continue

            # Try to parse date from this line (scan all positions
            # in case an extra token precedes the date)
            for start in range(len(next_tokens)):
                date_val, consumed = _parse_slice_date(next_tokens, start)
                if date_val:
                    date_value = date_val
                    break
            if date_value:
                break

            # Not a date and not a single char — stop looking
            break

        if date_value is None:
            rejected_date_lines.append({
                "page": page_number,
                "line_index": line_idx,
                "reason": "date_not_found_in_lookahead",
                "tokens": tokens,
            })
            line_idx += 1
            continue

        date_lines.append({
            "page": page_number,
            "line_index": line_idx,
            "tokens": tokens,
        })

        # Determine credit/debit from section header
        is_credit = current_section in _CREDIT_SECTIONS
        credit_reason: str | None = None
        if is_credit:
            credit_reason = f"section:{current_section.lower()}"

        amount_normalized = _normalize_slice_amount(amount_raw)

        transactions.append(
            Transaction(
                date=date_value,
                narration=narration,
                amount=amount_normalized,
                transaction_type="credit" if is_credit else "debit",
                credit_reasons=credit_reason,
            )
        )

        # Advance past the date line we consumed
        line_idx += lookahead + 1
        continue

    debug = {
        "date_lines": date_lines,
        "rejected_date_lines": rejected_date_lines,
        "detected_members": [],
    }
    return transactions, debug


def _extract_slice_account_summary(
    pages: list[dict[str, Any]],
) -> StatementSummary:
    """Extract account summary values from Slice statement.

    Slice has a ``Statement summary`` section with labeled rows:
    - Spends ₹X,XXX.XX
    - Refunds & repayments ₹X.XX
    - Cashback ₹X.XX
    - Interest ₹X.XX
    - Surcharge ₹X.XX
    - EMIs ₹X,XXX.XX
    - Total amount due ₹X,XXX.XX
    - Min amount due ₹X,XXX.XX
    """
    spends: str | None = None
    refunds_repayments: str | None = None
    cashback: str | None = None
    interest: str | None = None
    surcharge: str | None = None
    emis: str | None = None
    in_summary = False

    for page in pages[:2]:
        words = page.get("words") or []
        if not words:
            continue

        lines = group_words_into_lines(words)

        for line_words in lines:
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined_upper = clean_space(" ".join(tokens)).upper()

            if "STATEMENT SUMMARY" in joined_upper:
                in_summary = True
                continue

            if not in_summary:
                continue

            # Stop at non-summary content
            if "MIN AMOUNT DUE" in joined_upper:
                break

            # Find ₹AMOUNT on this line
            line_amount: str | None = None
            for t in tokens:
                if t.startswith("₹") and (amt := _parse_slice_amount(t)):
                    line_amount = _normalize_slice_amount(amt)
                    break

            if line_amount is None:
                continue

            # Identify which field
            if joined_upper.startswith("SPENDS") and spends is None:
                spends = line_amount
            elif "REFUND" in joined_upper and refunds_repayments is None:
                refunds_repayments = line_amount
            elif "CASHBACK" in joined_upper and cashback is None:
                cashback = line_amount
            elif "INTEREST" in joined_upper and interest is None:
                interest = line_amount
            elif "SURCHARGE" in joined_upper and surcharge is None:
                surcharge = line_amount
            elif joined_upper.startswith("EMIS") and emis is None:
                emis = line_amount

    candidates = [
        a for a in [spends, refunds_repayments, cashback, interest, surcharge, emis]
        if a
    ]

    if spends or emis:
        # Finance charges = interest + surcharge
        finance = Decimal("0")
        if interest:
            finance += parse_amount(interest)
        if surcharge:
            finance += parse_amount(surcharge)
        finance_str = format_amount(finance) if finance > 0 else None

        # Total credits = refunds + cashback
        total_credits = Decimal("0")
        if refunds_repayments:
            total_credits += parse_amount(refunds_repayments)
        if cashback:
            total_credits += parse_amount(cashback)
        combined_credits = (
            format_amount(total_credits) if total_credits > 0 else refunds_repayments
        )

        # Total purchases = spends + emis
        total_purchases = Decimal("0")
        if spends:
            total_purchases += parse_amount(spends)
        if emis:
            total_purchases += parse_amount(emis)

        return StatementSummary(
            summary_amount_candidates=candidates,
            purchases_debit=format_amount(total_purchases),
            payments_credits_received=combined_credits,
            finance_charges=finance_str,
        )

    return StatementSummary()


class SliceParser(StatementParser):
    """Parser entrypoint for Slice credit card statements."""

    bank = "slice"

    def __init__(self) -> None:
        self._last_txn_debug: dict[str, Any] | None = None
        self._last_transactions: list[Transaction] | None = None

    def parse(self, raw_data: dict[str, Any]) -> ParsedStatement:
        full_text = "\n".join(
            str(page.get("text", "")) for page in raw_data.get("pages", [])
        )
        pages = raw_data.get("pages", [])

        name = _extract_slice_name(full_text, pages)
        card_number = _extract_slice_card_number(
            full_text, pages
        ) or extract_card_from_filename(str(raw_data["file"]))

        transactions, txn_debug = _extract_slice_transactions(pages)
        self._last_txn_debug = txn_debug
        self._last_transactions = transactions

        # Fill in card number and person for all transactions
        if card_number:
            for txn in transactions:
                if not txn.card_number:
                    txn.card_number = card_number
        if name:
            for txn in transactions:
                if not txn.person:
                    txn.person = name

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

        # Cashback is categorically different from refunds/reversals —
        # do NOT run adjustment pairing on cashback credits.
        cashback_credits = [
            txn for txn in credit_transactions
            if (txn.credit_reasons or "").startswith("section:cashback")
        ]
        refund_credits = [
            txn for txn in credit_transactions
            if not (txn.credit_reasons or "").startswith("section:cashback")
        ]

        # Only pair refunds (not cashback) with debits for adjustments
        debit_transactions, refund_credits, adjustments = split_paired_adjustments(
            debit_transactions, refund_credits
        )

        # Recombine: cashback + remaining refunds = all credits
        credit_transactions = cashback_credits + refund_credits

        card_summaries, overall_total = build_card_summaries(debit_transactions, name)
        person_groups = group_transactions_by_person(debit_transactions, name)

        credit_total = sum_amounts(credit_transactions)
        overall_reward_points = sum_points(debit_transactions)

        adjustments_debit_total, adjustments_credit_total = compute_adjustment_totals(
            adjustments
        )

        due_date = _extract_slice_due_date(full_text, pages)
        statement_total_amount_due = _extract_slice_total_amount_due(full_text, pages)
        summary_fields = _extract_slice_account_summary(pages)
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
            transactions, txn_debug = _extract_slice_transactions(pages)

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
