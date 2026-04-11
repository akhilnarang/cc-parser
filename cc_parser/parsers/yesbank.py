"""YES BANK credit card statement parser.

YES BANK statements have these distinctive features:

- ``Dr``/``Cr`` markers at the end of each transaction line
- ``Rs.`` prefix on amounts in the header/summary section
- Indian number formatting (e.g. ``3,45,000.00``)
- ``Statement Details`` section header before transactions
- ``End of the Statement`` footer marker
- Card number in ``Statement for YES BANK Card Number XXXX`` format
- Name interleaved with address digits (security feature)
- Merchant Category column between narration and amount
- ``Payment Due Date: DD/MM/YYYY``
- ``Previous Balance : Rs. XX,XXX.XX Dr``
- ``Total Amount Due: Rs. XX,XXX.XX``
- ``Payment & Credits Received : Rs. XX,XXX.XX Cr``
"""

import re
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

# Known YES BANK merchant category names that appear between narration and amount.
# These are included in the narration for completeness.
_MERCHANT_CATEGORIES = {
    "RETAIL OUTLET SERVICES",
    "CLOTHING STORES",
    "MISCELLANEOUS STORES",
    "UTILITY SERVICES",
    "GROCERY STORES",
    "RESTAURANTS",
    "FUEL",
    "TRAVEL",
    "INSURANCE",
    "FINANCIAL INSTITUTIONS",
    "TELECOMMUNICATION",
    "HEALTH CARE",
    "EDUCATION",
    "ENTERTAINMENT",
    "PROFESSIONAL SERVICES",
}

# Lines that should not be treated as transaction rows
_NON_TRANSACTION_HEADERS = {
    "STATEMENT DETAILS",
    "DATE TRANSACTION DETAILS MERCHANT CATEGORY AMOUNT",
    "DATE TRANSACTION DETAILS",
    "END OF THE STATEMENT",
    "CREDIT CARD STATEMENT",
    "IMPORTANT INFORMATION",
    "IMPORTANT SAFETY INSTRUCTIONS",
    "YOUR REWARD POINTS SUMMARY",
    "REWARD POINTS SUMMARY",
}

# Keywords that indicate a line is a summary/header row, not a transaction.
# These appear in the statement header area and should not be parsed as transactions.
_SUMMARY_LINE_KEYWORDS = {
    "PREVIOUS BALANCE",
    "CREDIT LIMIT",
    "AVAILABLE CREDIT LIMIT",
    "CASH LIMIT",
    "AVAILABLE CASH LIMIT",
    "TOTAL AMOUNT DUE",
    "MINIMUM AMOUNT DUE",
    "PAYMENT DUE DATE",
    "STATEMENT PERIOD",
    "STATEMENT DATE",
    "POINTS EARNED",
    "CURRENT PURCHASES",
    "CASH ADVANCE",
    "OTHER CHARGES",
    "PAYMENT & CREDITS",
    "PAYMENTS & CREDITS",
    "YOUR OUTSTANDING BALANCE",
    "YOUR REWARD",
    "OPENING REWARD",
    "CLOSING REWARD",
    "BONUS POINTS",
    "POINTS REDEEMED",
    "REGISTERED MOBILE",
    "REGISTERED EMAIL",
    "YES ONLINE",
    "OTHER MODE",
}


def _extract_yesbank_name(full_text: str, pages: list[dict[str, Any]]) -> str | None:
    """Extract cardholder name from YES BANK statement.

    YES BANK interleaves the name with address digits as a security
    feature.  We try multiple approaches:

    1. Word-level: filter words in the address block by font height,
       then reconstruct the name from multi-letter words and single
       letters, deduplicating when a surname appears both as individual
       letters and as a complete word on the next line.
    2. Email-based: derive name from the registered email address.
    """
    # Words that are address parts, not name parts.
    _EXCLUDED_WORDS = {
        "CLICK",
        "HERE",
        "TO",
        "UPDATE",
        "YOUR",
        "NAME",
        "ADDRESS",
        "AND",
        "OR",
        "MOBILE",
        "EMAIL",
        "THE",
        "FILLED",
        "FORM",
        "DOCUMENT",
        "DOWNLOAD",
        "KLICK",
        "BANK",
        "YES",
        "REGISTERED",
        "NUMBER",
        "ID",
        "LANE",
        "ROAD",
        "STREET",
        "COLONY",
        "NAGAR",
        "PUR",
        "PURAM",
        "VIHAR",
        "SECTOR",
        "BLOCK",
        "HOUSE",
        "FLAT",
        "APARTMENT",
        "PLOT",
        "KYC",
        "SAMBALPUR",
        "MUMBAI",
        "DELHI",
        "BENGALURU",
        "CHENNAI",
        "KOLKATA",
        "HYDERABAD",
        "PUNE",
    }

    # Approach 1: word-level extraction from first page.
    # YES BANK can print the name as individual letters interleaved with
    # address digits on one line, and also repeat parts of the name as a
    # complete word on a nearby line. We reconstruct by grouping single
    # letters and deduplicating repeated suffix words.
    if pages:
        words = pages[0].get("words") or []
        if words:
            lines = group_words_into_lines(words)
            for i, line_words in enumerate(lines):
                tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
                joined_upper = clean_space(" ".join(tokens)).upper()
                if "REGISTERED" in joined_upper and "MOBILE" in joined_upper:
                    # Collect name tokens from nearby lines (address block area).
                    # Multi-letter words and single letters with height >= 7.5
                    # are name parts; smaller-height tokens are address digits.
                    name_tokens: list[str] = []
                    multi_letter_words: list[str] = []
                    for scan_idx in range(max(0, i - 4), min(len(lines), i + 3)):
                        scan_line = lines[scan_idx]
                        for w in scan_line:
                            text = normalize_token(str(w.get("text", "")))
                            height = float(w.get("height", 0))
                            if (
                                height >= 7.5
                                and text
                                and re.fullmatch(r"[A-Za-z]+", text)
                                and text.upper() not in _EXCLUDED_WORDS
                            ):
                                upper = text.upper()
                                name_tokens.append(upper)
                                if len(upper) > 1:
                                    multi_letter_words.append(upper)

                    if len(name_tokens) >= 2:
                        # Reconstruct words from mixed tokens.
                        # Rules:
                        # - Multi-letter tokens are base words.
                        # - Runs of single letters are appended to the previous
                        #   base word, unless they duplicate the next base word.
                        # - Repeated consecutive words are deduplicated.
                        words: list[str] = []
                        single_run = ""

                        for idx, tok in enumerate(name_tokens):
                            if len(tok) == 1:
                                single_run += tok
                                continue

                            if single_run:
                                if words:
                                    if single_run == tok:
                                        # Same word already arriving as full token.
                                        pass
                                    elif single_run.endswith(tok):
                                        # Keep only prefix letters with previous word;
                                        # suffix duplicates current token.
                                        prefix = single_run[: -len(tok)]
                                        if prefix:
                                            words[-1] = words[-1] + prefix
                                    else:
                                        words[-1] = words[-1] + single_run
                                else:
                                    words.append(single_run)
                                single_run = ""

                            words.append(tok)

                        if single_run:
                            if words:
                                words[-1] = words[-1] + single_run
                            else:
                                words.append(single_run)

                        # Deduplicate repeated consecutive parts.
                        deduped: list[str] = []
                        for part in words:
                            if not deduped or deduped[-1] != part:
                                deduped.append(part)

                        # Keep only plausible name chunks.
                        deduped = [p for p in deduped if len(p) >= 2]
                        if len(deduped) >= 2:
                            return " ".join(deduped[:3])
                        if deduped:
                            return deduped[0]
                    break

    # Approach 2: derive name from email address.
    # YES BANK shows "Registered Email Id" followed by an email.
    # The name part often contains the cardholder name.
    email_match = re.search(
        r"Registered\s+Email\s+Id\s*\n?\s*([A-Z][A-Z0-9._]+)@",
        full_text,
        flags=re.IGNORECASE,
    )
    if email_match:
        email_local = email_match.group(1).upper()
        name_part = re.sub(r"XXX+$", "", email_local)
        name_part = re.sub(r"\d+$", "", name_part)
        name_part = re.sub(r"[._]", " ", name_part)
        name_part = re.sub(r"([a-z])([A-Z])", r"\1 \2", name_part)
        parts = name_part.strip().split()
        if 2 <= len(parts) <= 5:
            return name_part.strip().upper()

    return None


def _extract_yesbank_card_number(
    full_text: str, pages: list[dict[str, Any]]
) -> str | None:
    """Extract YES BANK card number.

    YES BANK uses the format ``Statement for YES BANK Card Number 3561XXXXXXXX4365``.
    """
    # Text-level: look for "Card Number" followed by masked number
    match = re.search(
        r"Card\s+Number\s+([\dXx]{4,}[\sXx]*[\dXx]{4,})",
        full_text,
        flags=re.IGNORECASE,
    )
    if match:
        raw = match.group(1).replace(" ", "").upper()
        if len(raw) >= 12:
            # Pad to 16 chars
            if len(raw) < 16:
                raw = "X" * (16 - len(raw)) + raw
            return f"{raw[:4]} {raw[4:8]} {raw[8:12]} {raw[12:]}"

    # Word-level: look for "Card Number" then find the number token
    for page in pages[:2]:
        lines = group_words_into_lines(page.get("words") or [])
        for line_words in lines:
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined_upper = clean_space(" ".join(tokens)).upper()
            if "CARD NUMBER" in joined_upper:
                # Find the card-like token after "NUMBER"
                for token in tokens:
                    normalized = token.replace(" ", "").upper()
                    if len(normalized) >= 12 and (
                        "X" in normalized or "*" in normalized
                    ):
                        raw = normalized.replace("*", "X")
                        if len(raw) < 16:
                            raw = "X" * (16 - len(raw)) + raw
                        return f"{raw[:4]} {raw[4:8]} {raw[8:12]} {raw[12:]}"

    # Fallback: generic card detection
    candidates = find_card_candidates(full_text)
    return candidates[0] if candidates else None


def _extract_yesbank_due_date(
    full_text: str, pages: list[dict[str, Any]]
) -> str | None:
    """Extract payment due date from YES BANK statement.

    YES BANK uses ``DD/MM/YYYY`` format after ``Payment Due Date:``.
    """
    # Text-level search
    match = re.search(
        r"Payment\s+Due\s+Date\s*:\s*(\d{2}/\d{2}/\d{4})",
        full_text,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1)

    # Word-level search
    for page in pages[:2]:
        lines = group_words_into_lines(page.get("words") or [])
        for i, line_words in enumerate(lines):
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined_upper = clean_space(" ".join(tokens)).upper()
            if (
                "PAYMENT" in joined_upper
                and "DUE" in joined_upper
                and "DATE" in joined_upper
            ):
                # Check same line for date
                for token in tokens:
                    date_val = parse_date_token(token)
                    if date_val:
                        return date_val
                # Check next line
                if i + 1 < len(lines):
                    next_tokens = [
                        normalize_token(str(w.get("text", ""))) for w in lines[i + 1]
                    ]
                    for token in next_tokens:
                        date_val = parse_date_token(token)
                        if date_val:
                            return date_val

    return None


def _extract_yesbank_total_amount_due(
    full_text: str, pages: list[dict[str, Any]]
) -> str | None:
    """Extract total amount due from YES BANK statement.

    YES BANK has ``Total Amount Due:`` followed by ``Rs. XX,XXX.XX``.
    """
    # Text-level search
    match = re.search(
        r"Total\s+Amount\s+Due\s*:\s*Rs\.?\s*([\d,]+\.\d{2})",
        full_text,
        flags=re.IGNORECASE,
    )
    if match:
        return normalize_amount(match.group(1))

    # Word-level search
    for page in pages[:2]:
        lines = group_words_into_lines(page.get("words") or [])
        for i, line_words in enumerate(lines):
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined_upper = clean_space(" ".join(tokens)).upper()
            if (
                "TOTAL" in joined_upper
                and "AMOUNT" in joined_upper
                and "DUE" in joined_upper
            ):
                # Find amount on same or next line
                for token in tokens:
                    amt = parse_amount_token(token)
                    if amt:
                        return normalize_amount(amt)
                if i + 1 < len(lines):
                    next_tokens = [
                        normalize_token(str(w.get("text", ""))) for w in lines[i + 1]
                    ]
                    for token in next_tokens:
                        amt = parse_amount_token(token)
                        if amt:
                            return normalize_amount(amt)

    return None


def _extract_yesbank_summary(
    full_text: str, pages: list[dict[str, Any]]
) -> StatementSummary:
    """Extract account summary values from YES BANK statement.

    YES BANK summary fields:
    - ``Previous Balance : Rs. XX,XXX.XX Dr``
    - ``Current Purchases / Cash Advance & Other Charges : Rs. XX,XXX.XX Dr``
    - ``Payment & Credits Received : Rs. XX,XXX.XX Cr``
    """
    previous_balance: str | None = None
    purchases_debit: str | None = None
    payments_credits_received: str | None = None

    # Text-level extraction for summary fields
    # Previous Balance
    match = re.search(
        r"Previous\s+Balance\s*:\s*Rs\.?\s*([\d,]+\.\d{2})\s*(Dr|Cr)?",
        full_text,
        flags=re.IGNORECASE,
    )
    if match:
        previous_balance = normalize_amount(match.group(1))
        if match.group(2) and match.group(2).upper() == "CR":
            previous_balance = "-" + previous_balance

    # Current Purchases / Cash Advance & Other Charges
    match = re.search(
        r"Current\s+Purchases\s*/\s*Cash\s+Advance\s*&\s*Other\s+Charges\s*:\s*"
        r"Rs\.?\s*([\d,]+\.\d{2})\s*(Dr|Cr)?",
        full_text,
        flags=re.IGNORECASE,
    )
    if match:
        purchases_debit = normalize_amount(match.group(1))

    # Payment & Credits Received
    match = re.search(
        r"Payment\s+&\s+Credits\s+Received\s*:\s*Rs\.?\s*([\d,]+\.\d{2})\s*(Dr|Cr)?",
        full_text,
        flags=re.IGNORECASE,
    )
    if match:
        payments_credits_received = normalize_amount(match.group(1))

    # Word-level fallback for summary fields
    if not previous_balance or not purchases_debit or not payments_credits_received:
        for page in pages[:2]:
            lines = group_words_into_lines(page.get("words") or [])
            for i, line_words in enumerate(lines):
                tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
                joined_upper = clean_space(" ".join(tokens)).upper()

                if "PREVIOUS" in joined_upper and "BALANCE" in joined_upper:
                    # Find amount on same or next line
                    search_tokens = tokens
                    if i + 1 < len(lines):
                        search_tokens = tokens + [
                            normalize_token(str(w.get("text", "")))
                            for w in lines[i + 1]
                        ]
                    is_credit = any(t.upper() in {"CR", "C"} for t in search_tokens)
                    for token in search_tokens:
                        amt = parse_amount_token(token)
                        if amt and not previous_balance:
                            previous_balance = normalize_amount(amt)
                            if is_credit:
                                previous_balance = "-" + previous_balance
                            break

                if "PURCHASES" in joined_upper and "CHARGES" in joined_upper:
                    search_tokens = tokens
                    if i + 1 < len(lines):
                        search_tokens = tokens + [
                            normalize_token(str(w.get("text", "")))
                            for w in lines[i + 1]
                        ]
                    for token in search_tokens:
                        amt = parse_amount_token(token)
                        if amt and not purchases_debit:
                            purchases_debit = normalize_amount(amt)
                            break

                if (
                    "PAYMENT" in joined_upper
                    and "CREDITS" in joined_upper
                    and "RECEIVED" in joined_upper
                ):
                    search_tokens = tokens
                    if i + 1 < len(lines):
                        search_tokens = tokens + [
                            normalize_token(str(w.get("text", "")))
                            for w in lines[i + 1]
                        ]
                    is_credit = any(t.upper() in {"CR", "C"} for t in search_tokens)
                    for token in search_tokens:
                        amt = parse_amount_token(token)
                        if amt and not payments_credits_received:
                            payments_credits_received = normalize_amount(amt)
                            break

    candidates = [
        a for a in [previous_balance, purchases_debit, payments_credits_received] if a
    ]

    return StatementSummary(
        summary_amount_candidates=candidates,
        previous_statement_dues=previous_balance,
        purchases_debit=purchases_debit,
        payments_credits_received=payments_credits_received,
    )


def _extract_yesbank_transactions(
    pages: list[dict[str, Any]],
) -> tuple[list[Transaction], dict[str, Any]]:
    """Parse transactions from YES BANK statement pages.

    YES BANK transaction lines follow the pattern::

        DD/MM/YYYY  NARRATION  MERCHANT_CATEGORY  AMOUNT  Dr|Cr

    The ``Dr``/``Cr`` marker at the end determines debit/credit classification.
    Transactions appear between ``Statement Details`` header and
    ``End of the Statement`` footer.
    """
    transactions: list[Transaction] = []
    current_card: str | None = None
    current_member: str | None = None
    date_lines: list[dict[str, Any]] = []
    rejected_date_lines: list[dict[str, Any]] = []
    detected_members: list[dict[str, Any]] = []

    in_transaction_section = False

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

            # Track transaction section boundaries
            if "STATEMENT DETAILS" in joined_upper:
                in_transaction_section = True
                continue
            if "END OF THE STATEMENT" in joined_upper:
                in_transaction_section = False
                continue

            # Skip known non-transaction headers
            if joined_upper in _NON_TRANSACTION_HEADERS:
                continue
            # Skip header row: "Date Transaction Details Merchant Category Amount (Rs.)"
            if (
                "TRANSACTION DETAILS" in joined_upper
                and "MERCHANT CATEGORY" in joined_upper
            ):
                continue
            # Skip summary/header lines that have dates but aren't transactions
            # (e.g. "Statement Period: 13/02/2026 To 12/03/2026")
            if any(kw in joined_upper for kw in _SUMMARY_LINE_KEYWORDS):
                continue

            # Skip footer/header noise
            if any(
                kw in joined_upper
                for kw in {
                    "SMS",
                    "HELP",
                    "PHONEBANKING",
                    "YESTOUCH",
                    "CIN :",
                    "IMPORTANT",
                    "REWARD POINTS",
                    "SAFETY",
                    "NETBANKING",
                    "PAGE ",
                    "OVERVIEW",
                    "STATEMENT SUMMARY",
                }
            ):
                continue

            # Detect card number lines
            if "CARD NUMBER" in joined_upper:
                card_match = re.search(
                    r"CARD\s+NUMBER\s+([\dXx]{4,}[\sXx]*[\dXx]{4,})",
                    joined_upper,
                )
                if card_match:
                    raw = card_match.group(1).replace(" ", "")
                    if len(raw) < 16:
                        raw = "X" * (16 - len(raw)) + raw
                    current_card = f"{raw[:4]} {raw[4:8]} {raw[8:12]} {raw[12:]}"
                continue

            # Try to parse date at the start of the line
            date_idx = -1
            date_value = None
            for idx, token in enumerate(tokens):
                parsed = parse_date_token(token)
                if parsed:
                    date_idx = idx
                    date_value = parsed
                    break

            if date_idx == -1 or date_value is None:
                continue

            # Only parse transactions when we're inside the transaction section.
            # YES BANK transactions appear between "Statement Details" and
            # "End of the Statement" markers.
            if not in_transaction_section:
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

            # Find the amount (rightmost amount token)
            amount_idx = -1
            for i in range(len(tokens) - 1, date_idx, -1):
                if parse_amount_token(tokens[i]) is not None:
                    amount_idx = i
                    break

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

            # Determine credit/debit from the last token (Dr/Cr)
            last_token = tokens[-1].upper() if tokens else ""
            is_credit = last_token in {"CR", "C"}

            # Also check if the token just before the amount is Dr/Cr
            # (in case amount is not the last token)
            if not is_credit and amount_idx + 1 < len(tokens):
                next_after_amount = tokens[amount_idx + 1].upper()
                if next_after_amount in {"DR", "D", "CR", "C"}:
                    is_credit = next_after_amount in {"CR", "C"}

            # Narration is everything between date and amount
            cursor = date_idx + 1
            # Skip separator tokens after date
            while cursor < len(tokens) and tokens[cursor] in SEPARATOR_TOKENS:
                cursor += 1

            narration_end = amount_idx

            # Remove merchant category from narration if it appears at the end
            # (just before the amount). We check known category names.
            narration_tokens = [
                t
                for t in tokens[cursor:narration_end]
                if t and t not in SEPARATOR_TOKENS
            ]

            # Check if the last few narration tokens form a known merchant category
            narration_text = clean_space(" ".join(narration_tokens))
            for cat in _MERCHANT_CATEGORIES:
                if narration_text.upper().endswith(cat):
                    # Remove the merchant category from narration
                    narration_text = narration_text[: -len(cat)].strip()
                    break

            # Also remove Dr/Cr markers from narration if present
            narration_text = re.sub(
                r"\s+(Dr|Cr|DR|CR)\s*$", "", narration_text, flags=re.IGNORECASE
            ).strip()

            # Merge context from wrapped lines if narration is incomplete
            if needs_context_merge(narration_text):
                prev_ctx, next_ctx = collect_row_context_tokens(lines, line_index)
                ctx_tokens = [
                    t
                    for t in [*prev_ctx, *next_ctx]
                    if t
                    and t not in SEPARATOR_TOKENS
                    and t not in {"+", "l", "I", "CR", "DR"}
                    and parse_amount_token(t) is None
                    and parse_date_token(t) is None
                ]
                narration_text = clean_space(" ".join([*narration_tokens, *ctx_tokens]))

            if not narration_text:
                continuation = extract_continuation_narration(lines, line_index)
                if continuation:
                    narration_text = continuation

            narration_text = clean_narration_artifacts(narration_text)

            if not narration_text:
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
                credit_reason = "cr_marker"

            transactions.append(
                Transaction(
                    date=date_value,
                    narration=narration_text,
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


class YesbankParser(StatementParser):
    """Parser entrypoint for YES BANK credit card statements."""

    bank = "yesbank"

    def __init__(self) -> None:
        self._last_txn_debug: dict[str, Any] | None = None
        self._last_transactions: list[Transaction] | None = None

    def parse(self, raw_data: dict[str, Any]) -> ParsedStatement:
        full_text = "\n".join(
            str(page.get("text", "")) for page in raw_data.get("pages", [])
        )
        pages = raw_data.get("pages", [])

        name = _extract_yesbank_name(full_text, pages)
        card_number = _extract_yesbank_card_number(
            full_text, pages
        ) or extract_card_from_filename(str(raw_data["file"]))

        transactions, txn_debug = _extract_yesbank_transactions(pages)
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

        due_date = _extract_yesbank_due_date(full_text, pages)
        statement_total_amount_due = _extract_yesbank_total_amount_due(full_text, pages)
        summary_fields = _extract_yesbank_summary(full_text, pages)
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
            transactions, txn_debug = _extract_yesbank_transactions(pages)

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
