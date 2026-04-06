"""IndusInd Bank parser profile.

IndusInd credit card statements differ from other banks in several ways:
- Dates are ``DD/MM/YYYY`` (single token)
- Credit/debit markers are ``CR`` / ``DR`` as the last token
- Reward points appear as an integer token before the amount
- Merchant category may appear between narration and reward points
- Card numbers use ``NNNNXXXXXXXXNNNN`` (first 4 and last 4 digits visible)
- Member headers: ``Purchases & Cash Transactions for NAME (Credit Card No. CARD)``
  or ``Payment Details for NAME (Credit Card No. CARD)``
- Due date format is ``DD/MM/YYYY``
- Account summary uses Previous Balance, Purchases & Other Charges,
  Cash Advance, Payment & Other Credits in a right sidebar layout
"""

import re
from typing import Any

from cc_parser.parsers.base import StatementParser
from cc_parser.parsers.cards import (
    extract_card_from_filename,
    find_card_candidates,
    looks_like_card_token,
    mask_card_token,
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
    parse_amount_token,
    parse_date_token,
    sum_amounts,
    sum_points,
)

# Regex for member header lines:
# "Payment Details for MR NAME (Credit Card No. XXXXXXXXXXXX1234)"
# "Purchases & Cash Transactions for MR NAME (Credit Card No. XXXX1234)"
_MEMBER_HEADER_RE = re.compile(
    r"(?:PAYMENT\s+DETAILS|PURCHASES\s+(?:&|AND)\s+CASH\s+TRANSACTIONS)"
    r"\s+FOR\s+(.+?)\s*\(CREDIT\s+CARD\s+NO\.\s*([0-9A-ZX][0-9A-ZX \-]*[0-9A-ZX])\)",
    re.IGNORECASE,
)


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


def _format_card(raw: str) -> str:
    """Format a normalized card token into spaced groups of 4."""
    digits = raw.replace(" ", "")
    if len(digits) >= 16:
        return f"{digits[:4]} {digits[4:8]} {digits[8:12]} {digits[12:16]}"
    return digits


def _extract_indusind_name(full_text: str, pages: list[dict[str, Any]]) -> str | None:
    """Extract cardholder name from IndusInd statement.

    IndusInd prints the name with honorific in the member header:
    ``Purchases & Cash Transactions for MR CARDHOLDER NAME (Credit Card No. ...)``
    """
    match = _MEMBER_HEADER_RE.search(full_text)
    if match:
        name = clean_space(match.group(1)).upper()
        name = re.sub(r"^(MR|MRS|MS|MISS|DR)\.?\s+", "", name)
        parts = name.split()
        if 2 <= len(parts) <= 5:
            return name

    # Fallback: "MR/MRS NAME" near the address block
    name_match = re.search(
        r"(MR|MRS|MS|MISS|DR)\.?\s+([A-Z][A-Z ]{3,})\s*\n",
        full_text,
    )
    if name_match:
        candidate = clean_space(name_match.group(2)).upper()
        parts = candidate.split()
        if 2 <= len(parts) <= 5:
            return candidate

    return None


def _extract_indusind_card_number(
    full_text: str, pages: list[dict[str, Any]]
) -> str | None:
    """Extract card number from IndusInd statement.

    IndusInd uses formats like ``1234XXXXXXXX5678`` in member headers.
    """
    match = _MEMBER_HEADER_RE.search(full_text)
    if match:
        raw = normalize_card_token(match.group(2))
        if looks_like_card_token(raw):
            return _format_card(mask_card_token(raw))

    # Try "Credit Card No." pattern elsewhere
    card_match = re.search(
        r"Credit\s+Card\s+No\.\s*([0-9A-Za-zX]+)",
        full_text,
        flags=re.IGNORECASE,
    )
    if card_match:
        raw = normalize_card_token(card_match.group(1))
        if looks_like_card_token(raw):
            return _format_card(mask_card_token(raw))

    candidates = find_card_candidates(full_text)
    return candidates[0] if candidates else None


def _extract_indusind_due_date(
    full_text: str, pages: list[dict[str, Any]]
) -> str | None:
    """Extract due date from IndusInd statement (``DD/MM/YYYY``)."""
    for page in pages[:2]:
        lines = group_words_into_lines(page.get("words") or [])
        for i, line_words in enumerate(lines):
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens)).upper()
            if "PAYMENT DUE DATE" not in joined:
                continue

            # Check same line
            for t in tokens:
                if parse_date_token(t):
                    return t

            # Check next line
            if i + 1 < len(lines):
                for w in lines[i + 1]:
                    t = normalize_token(str(w.get("text", "")))
                    if parse_date_token(t):
                        return t

    # Text-level fallback
    match = re.search(
        r"Payment\s+Due\s+Date.*?(\d{2}/\d{2}/\d{4})",
        full_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        return match.group(1)

    return None


def _extract_indusind_total_amount_due(
    full_text: str, pages: list[dict[str, Any]]
) -> str | None:
    """Extract total amount due from IndusInd statement.

    Format: ``Total Amount Due`` followed by amount (e.g. ``1,920.00 DR``).
    A ``CR`` suffix means a credit balance (overpayment) and is stored as
    a negative value so reconciliation arithmetic stays correct.

    Uses x-position of the ``Total`` label to pick the correct amount
    when multiple summary fields share a visual line.
    """
    for page in pages[:2]:
        lines = group_words_into_lines(page.get("words") or [])
        for i, line_words in enumerate(lines):
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens)).upper()
            if "TOTAL AMOUNT DUE" not in joined:
                continue

            # Find x-position of the "Total" label word
            label_x: float | None = None
            for w in line_words:
                if normalize_token(str(w.get("text", ""))).upper() == "TOTAL":
                    label_x = float(w.get("x0", 0))
                    break

            # Same line — pick amount closest to label x-position
            best_amt: str | None = None
            best_dist = float("inf")
            for w in line_words:
                t = normalize_token(str(w.get("text", "")))
                if t.upper().endswith(("CR", "DR")) and len(t) > 2:
                    t = t[:-2]
                amt = parse_amount_token(t)
                if amt:
                    dist = abs(float(w.get("x0", 0)) - (label_x or 0))
                    if dist < best_dist:
                        best_dist = dist
                        best_amt = amt
            if best_amt:
                result = normalize_amount(best_amt)
                if _has_cr_marker([str(w.get("text", "")) for w in line_words]):
                    result = f"-{result}"
                return result

            # Next line
            if i + 1 < len(lines):
                next_words = lines[i + 1]
                next_raw = [str(w.get("text", "")) for w in next_words]
                best_amt = None
                best_dist = float("inf")
                for w in next_words:
                    t = normalize_token(str(w.get("text", "")))
                    if t.upper().endswith(("CR", "DR")) and len(t) > 2:
                        t = t[:-2]
                    amt = parse_amount_token(t)
                    if amt:
                        dist = abs(float(w.get("x0", 0)) - (label_x or 0))
                        if dist < best_dist:
                            best_dist = dist
                            best_amt = amt
                if best_amt:
                    result = normalize_amount(best_amt)
                    if _has_cr_marker(next_raw):
                        result = f"-{result}"
                    return result

    # Text-level fallback
    match = re.search(
        r"Total\s+Amount\s+Due.*?([\d,]+\.\d{2})\s*(CR)?",
        full_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        result = normalize_amount(match.group(1))
        if match.group(2):
            result = f"-{result}"
        return result

    return None


def _extract_indusind_transactions(
    pages: list[dict[str, Any]],
) -> tuple[list[Transaction], dict[str, Any]]:
    """Parse transactions from IndusInd statement pages.

    IndusInd transaction lines follow the pattern:
    ``DD/MM/YYYY  NARRATION  [MERCHANT_CATEGORY]  REWARD_POINTS  AMOUNT  DR/CR``
    """
    transactions: list[Transaction] = []
    current_card: str | None = None
    current_member: str | None = None
    in_transaction_section = False
    is_credit_section = False
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

            # Detect member header lines
            header_match = _MEMBER_HEADER_RE.search(joined_upper)
            if header_match:
                name = clean_space(header_match.group(1)).upper()
                name = re.sub(r"^(MR|MRS|MS|MISS|DR)\.?\s+", "", name)
                card_raw = normalize_card_token(header_match.group(2))
                if looks_like_card_token(card_raw):
                    current_card = _format_card(mask_card_token(card_raw))

                # "Payment Details" sections contain credits;
                # "Purchases & Cash Transactions" sections contain debits.
                is_credit_section = (
                    "PAYMENT" in joined_upper and "DETAILS" in joined_upper
                )

                parts = name.split()
                if 2 <= len(parts) <= 5:
                    current_member = name
                    in_transaction_section = True
                    detected_members.append(
                        {
                            "page": page_number,
                            "line_index": line_index,
                            "member": name,
                            "card": current_card,
                        }
                    )
                continue

            # Skip summary "Total" lines and end the transaction section.
            # Match "Total" followed by an amount or end-of-line to avoid
            # false positives on merchants like "Total Energies".
            if joined_upper == "TOTAL" or re.match(r"TOTAL\s+\d", joined_upper):
                in_transaction_section = False
                continue

            # Only parse transactions between a member header and a Total line
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

            # Determine credit/debit from last token, falling back to
            # the section type inferred from the member header.
            # Handles both separate markers ("CR") and merged ("1920.00CR").
            last_token = tokens[-1].upper() if tokens else ""
            if last_token in {"CR", "C"}:
                is_credit = True
            elif last_token in {"DR", "D"}:
                is_credit = False
            elif last_token.endswith("CR"):
                is_credit = True
            elif last_token.endswith("DR"):
                is_credit = False
            else:
                is_credit = is_credit_section

            # Find the amount: rightmost amount-matching token before DR/CR
            search_end = len(tokens)
            if last_token in {"CR", "DR", "C", "D"}:
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

            if amount_idx == -1 or amount_idx <= 1:
                rejected_date_lines.append(
                    {
                        "page": page_number,
                        "line_index": line_index,
                        "reason": "amount_not_found",
                        "tokens": tokens,
                    }
                )
                continue

            # Check for reward points: short integer token (≤5 digits)
            # immediately before amount. Longer numerics are likely
            # reference/terminal/MCC codes, not reward points.
            # Guard: the candidate must sit in the rightward portion of
            # the line (past the midpoint) to avoid stealing narration
            # tokens that happen to be purely numeric.
            reward_points: str | None = None
            reward_idx = amount_idx - 1
            amt_x = (
                float(line_words[amount_idx].get("x0", 0))
                if amount_idx < len(line_words)
                else 0
            )
            date_x = float(line_words[0].get("x0", 0)) if line_words else 0
            midpoint_x = (date_x + amt_x) / 2
            reward_candidate_x = (
                float(line_words[reward_idx].get("x0", 0))
                if reward_idx < len(line_words)
                else 0
            )
            if (
                reward_idx > 0
                and re.fullmatch(r"\d{1,5}", tokens[reward_idx])
                and reward_candidate_x > midpoint_x
            ):
                reward_points = tokens[reward_idx]
                narration_end = reward_idx
            else:
                narration_end = amount_idx

            # Narration: everything between date token and narration_end
            cursor = 1  # skip date token
            while cursor < len(tokens) and tokens[cursor] in SEPARATOR_TOKENS:
                cursor += 1

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
            if needs_context_merge(narration):
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
                if last_token in {"CR", "C"} or last_token.endswith("CR"):
                    credit_reason = "cr_marker"
                else:
                    credit_reason = "payment_details_section"

            transactions.append(
                Transaction(
                    date=date_value,
                    narration=narration,
                    amount=normalize_amount(amount_value),
                    reward_points=reward_points,
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


def _find_amount_near_label(
    label_x: float | None,
    line_words: list[dict[str, Any]],
) -> tuple[str | None, bool]:
    """Find the amount token on a line closest to ``label_x``.

    Returns ``(amount_str, has_cr)`` where *has_cr* is True when a ``CR``
    marker appears on the same line (separate or merged into the amount).
    """
    best_amt: str | None = None
    best_dist = float("inf")
    for w in line_words:
        t = normalize_token(str(w.get("text", "")))
        if t.upper().endswith(("CR", "DR")) and len(t) > 2:
            t = t[:-2]
        amt = parse_amount_token(t)
        if amt:
            dist = abs(float(w.get("x0", 0)) - (label_x or 0))
            if dist < best_dist:
                best_dist = dist
                best_amt = normalize_amount(amt)

    has_cr = _has_cr_marker([str(w.get("text", "")) for w in line_words])
    return best_amt, has_cr


def _extract_indusind_account_summary(
    pages: list[dict[str, Any]],
) -> StatementSummary:
    """Extract account summary values from IndusInd statement.

    IndusInd has a right-sidebar layout with:
    - Previous Balance: X.XX DR/CR
    - Purchases & Other Charges: X.XX
    - Cash Advance: X.XX  (intentionally excluded — cash advances are
      already captured as parsed debit transactions, so mapping them here
      would double-count in reconciliation)
    - Payment & Other Credits: X.XX

    A ``CR`` marker on Previous Balance means a credit balance (the bank
    owed the cardholder) and is stored as a negative value.

    Uses x-position of the label keyword to pick the correct amount
    when multiple sidebar fields land on the same visual line.
    """
    previous_balance: str | None = None
    purchases: str | None = None
    payments_credits: str | None = None

    for page in pages[:2]:
        words = page.get("words") or []
        if not words:
            continue

        lines = group_words_into_lines(words)

        for i, line_words in enumerate(lines):
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens)).upper()

            if "PREVIOUS BALANCE" in joined and previous_balance is None:
                # Find label x-position
                label_x: float | None = None
                for w in line_words:
                    if normalize_token(str(w.get("text", ""))).upper() == "PREVIOUS":
                        label_x = float(w.get("x0", 0))
                        break

                amt, has_cr = _find_amount_near_label(label_x, line_words)
                if amt is None and i + 1 < len(lines):
                    amt, has_cr = _find_amount_near_label(label_x, lines[i + 1])
                if amt:
                    previous_balance = f"-{amt}" if has_cr else amt

            if (
                "PURCHASES" in joined
                and "OTHER CHARGES" in joined
                and purchases is None
            ):
                label_x = None
                for w in line_words:
                    if normalize_token(str(w.get("text", ""))).upper() == "PURCHASES":
                        label_x = float(w.get("x0", 0))
                        break

                amt, _ = _find_amount_near_label(label_x, line_words)
                if amt is None and i + 1 < len(lines):
                    amt, _ = _find_amount_near_label(label_x, lines[i + 1])
                if amt:
                    purchases = amt

            if (
                "PAYMENT" in joined
                and "OTHER CREDITS" in joined
                and payments_credits is None
            ):
                label_x = None
                for w in line_words:
                    if normalize_token(str(w.get("text", ""))).upper() == "PAYMENT":
                        label_x = float(w.get("x0", 0))
                        break

                amt, _ = _find_amount_near_label(label_x, line_words)
                if amt is None and i + 1 < len(lines):
                    amt, _ = _find_amount_near_label(label_x, lines[i + 1])
                if amt:
                    payments_credits = amt

    if previous_balance or purchases or payments_credits:
        candidates = [a for a in [previous_balance, purchases, payments_credits] if a]
        return StatementSummary(
            summary_amount_candidates=candidates,
            previous_statement_dues=previous_balance,
            purchases_debit=purchases,
            payments_credits_received=payments_credits,
        )

    return StatementSummary()


class IndusindParser(StatementParser):
    """Parser entrypoint for IndusInd Bank statements."""

    bank = "indusind"

    def __init__(self) -> None:
        self._last_txn_debug: dict[str, Any] | None = None
        self._last_transactions: list[Transaction] | None = None

    def parse(self, raw_data: dict[str, Any]) -> ParsedStatement:
        full_text = "\n".join(
            str(page.get("text", "")) for page in raw_data.get("pages", [])
        )
        pages = raw_data.get("pages", [])

        name = _extract_indusind_name(full_text, pages)
        card_number = _extract_indusind_card_number(
            full_text, pages
        ) or extract_card_from_filename(str(raw_data["file"]))

        transactions, txn_debug = _extract_indusind_transactions(pages)
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

        due_date = _extract_indusind_due_date(full_text, pages)
        statement_total_amount_due = _extract_indusind_total_amount_due(
            full_text, pages
        )
        summary_fields = _extract_indusind_account_summary(pages)
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
            transactions, txn_debug = _extract_indusind_transactions(pages)

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
