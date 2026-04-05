"""BOB (Bank of Baroda / BOBCARD) credit card statement parser.

BOB statements differ from other banks in several ways:
- Transaction table packs ALL rows into a single table row with
  newline-separated values per cell (7 columns)
- Amount column has explicit "CR" / "DR" suffix per entry
- Member header appears as the first line in the particulars column,
  matching the pattern: ``NAME (PRIMARY|ADDON CARD - XXXX)``
- Due date is in DD/MM/YYYY format near "Payment Due Date"
- Card number format: ``NNNNNNXXXXXXNNNN`` (6 known + 6 masked + 4 known)
- Reward points balance from Reward Points Summary "Closing Balance"
- finance_charges is not separately broken out (None)
- Detected via "BOBCARD" in statement text

The parsing strategy:
1. Use pdfplumber table data (column 6, amount+CR/DR) as the transaction anchor
2. Align dates (column 0) 1:1 with amounts
3. Strip the member-header first line from particulars (column 2)
4. Align reward points from table column 3 with DR transactions
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
from cc_parser.parsers.reconciliation import (
    build_card_summaries,
    build_reconciliation,
    compute_adjustment_totals,
    extract_due_date_from_pages,
    extract_name,
    group_transactions_by_person,
    split_paired_adjustments,
)
from cc_parser.parsers.tokens import (
    clean_space,
    format_amount,
    normalize_amount,
    normalize_token,
    parse_amount_token,
    parse_date_token,
    sum_amounts,
    sum_points,
)

# Regex for the BOB member header line inside the particulars column.
# Matches: "FIRSTNAME LASTNAME (PRIMARY CARD - 1234)" or "(ADDON CARD - 5678)"
_MEMBER_HEADER_RE = re.compile(
    r"^(.+?)\s*\(\s*(PRIMARY|ADDON)\s+CARD\s*[-–]\s*(\d{4})\s*\)$",
    re.IGNORECASE,
)

# BOB amount-with-suffix regex: "50,000.00 CR", "2,018.00DR", "₹ 1,000.00 DR"
_AMOUNT_CRDR_RE = re.compile(
    r"^[₹`]?\s*([\d,]+\.\d{2})\s*(CR|DR)\s*$",
    re.IGNORECASE,
)


def _extract_bob_name(full_text: str) -> str | None:
    """Extract cardholder name from BOB statement.

    BOB prints the name with an honorific followed by a period
    (e.g. ``MR. FIRSTNAME LASTNAME``).  The shared ``extract_name`` function
    handles bare honorifics; this function adds BOB-specific handling for
    the dotted form ``MR.``/``MRS.``/``MS.``/``DR.``.
    """
    # Try shared extractor first (handles bare "MR FIRSTNAME LASTNAME")
    result = extract_name(full_text)
    if result:
        return result

    # BOB-specific: honorific with trailing period, e.g. "MR. FIRSTNAME LASTNAME"
    honorifics_dotted = {"MR.", "MRS.", "MS.", "MISS.", "DR."}
    for raw_line in full_text.splitlines():
        line = clean_space(raw_line)
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3 or len(parts) > 6:
            continue
        if parts[0].upper() not in honorifics_dotted:
            continue
        tail = parts[1:]
        if all(re.fullmatch(r"[A-Za-z][A-Za-z.'-]*", token) for token in tail):
            return " ".join(tail).upper()

    return None


def _extract_bob_card_number(full_text: str) -> str | None:
    """Extract masked card number from BOB statement.

    BOB format: ``NNNNNNXXXXXXNNNN`` (6 known + 6 masked + 4 known).
    """
    # Look for the BOB-specific inline pattern near "Card No"
    match = re.search(
        r"Card\s+No[:\s]+([0-9X]{6,7}X{4,8}[0-9]{4})",
        full_text,
        flags=re.IGNORECASE,
    )
    if match:
        raw = match.group(1).upper()
        candidates = find_card_candidates(raw)
        if candidates:
            return candidates[0]
        # Build a masked version directly if find_card_candidates doesn't catch it
        return raw

    # Fall back to generic card detection
    candidates = find_card_candidates(full_text)
    return candidates[0] if candidates else None


def _extract_bob_due_date(full_text: str, pages: list[dict[str, Any]]) -> str | None:
    """Extract due date from BOB statement.

    BOB puts the due date in DD/MM/YYYY format near "Payment Due Date".
    First tries inline regex on full text, then uses page-level word search.
    """
    # Inline match on full text — BOB uses DD/MM/YYYY format
    match = re.search(
        r"Payment\s+Due\s+Date\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",
        full_text,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1)

    # Page-level word search (handles next-line layout)
    result = extract_due_date_from_pages(pages)
    if result:
        return result

    # Broad search: find "Payment Due Date" then next DD/MM/YYYY within 200 chars
    upper = full_text.upper()
    start = upper.find("PAYMENT DUE DATE")
    if start != -1:
        segment = full_text[start : start + 200]
        date_match = re.search(r"\d{2}/\d{2}/\d{4}", segment)
        if date_match:
            return date_match.group(0)

    return None


def _extract_bob_total_amount_due(full_text: str) -> str | None:
    """Extract total amount due from BOB statement.

    BOB format: ``₹ 13,797.01DR`` — the rupee symbol may be present and
    "DR" (or "CR") may be appended directly to the amount without a space.

    We prefer the amount with an explicit DR/CR suffix (the actual total)
    over a bare amount (which is usually the minimum amount due).
    """
    upper = full_text.upper()
    start = upper.find("TOTAL AMOUNT DUE")
    if start == -1:
        return None

    segment = full_text[start : start + 400]

    # Prefer amount with explicit DR/CR suffix — that's the actual total
    amount_match = re.search(
        r"[₹`]?\s*([\d,]+\.\d{2})\s*(DR|CR)",
        segment,
        flags=re.IGNORECASE,
    )
    if amount_match:
        amt = normalize_amount(amount_match.group(1))
        # CR suffix means credit balance (user is owed money) — negate
        if amount_match.group(2).upper() == "CR":
            return f"-{amt}"
        return amt

    # Fallback: bare amount (no DR/CR suffix)
    amount_match = re.search(
        r"[₹`]?\s*([\d,]+\.\d{2})",
        segment,
    )
    if amount_match:
        return normalize_amount(amount_match.group(1))

    return None


def _parse_amount_crdr(cell: str) -> tuple[str | None, str | None]:
    """Parse a BOB amount cell like '50,000.00 CR' into (amount, 'CR'|'DR').

    Returns (None, None) on failure.
    """
    cell = clean_space(cell)
    m = _AMOUNT_CRDR_RE.match(cell)
    if not m:
        return None, None
    amount_raw = m.group(1)
    suffix = m.group(2).upper()
    return normalize_amount(amount_raw), suffix


def _is_bob_transaction_table(table: list[list[Any]]) -> bool:
    """Check whether a pdfplumber table looks like a BOB transaction table."""
    if not table or len(table) < 2:
        return False
    row0 = table[0]
    if row0 and isinstance(row0[0], str) and "TRANSACTION" in row0[0].upper():
        return True
    for row in table[1:]:
        if len(row) >= 7 and isinstance(row[0], str):
            if "\n" in row[0] and re.search(r"\d{2}/\d{2}/\d{4}", row[0]):
                return True
    return False


def _find_all_bob_transaction_tables(
    pages: list[dict[str, Any]],
) -> list[tuple[list[list[Any]], int]]:
    """Find all transaction tables across all pages.

    BOB statements can split transaction tables across multiple pages.
    Returns list of (table_rows, page_index) tuples.
    """
    results: list[tuple[list[list[Any]], int]] = []
    for page_idx, page in enumerate(pages):
        tables = page.get("tables") or []
        for table in tables:
            if _is_bob_transaction_table(table):
                results.append((table, page_idx))
    return results


def _split_nonempty(cell: str | None) -> list[str]:
    """Split a newline-packed table cell into non-empty stripped lines."""
    if not cell:
        return []
    return [line.strip() for line in cell.split("\n") if line.strip()]


def _extract_bob_transactions(
    pages: list[dict[str, Any]],
) -> tuple[list[Transaction], dict[str, Any]]:
    """Parse transactions from BOB statement pages.

    Uses the pdfplumber table data as primary source:
    - Column 0: newline-packed dates (DD/MM/YYYY)
    - Column 2: newline-packed particulars (first line may be member header)
    - Column 6: newline-packed amounts with CR/DR suffix (authoritative count)

    Word-level extraction is used for reward points alignment.
    """
    debug: dict[str, Any] = {
        "table_found": False,
        "transaction_count": 0,
        "detected_members": [],
        "rejected_rows": [],
        "table_page_indices": [],
    }

    all_tables = _find_all_bob_transaction_tables(pages)
    if not all_tables:
        return [], debug

    debug["table_found"] = True
    debug["table_page_indices"] = [idx for _, idx in all_tables]

    all_transactions: list[Transaction] = []

    for table, page_idx in all_tables:
        # Find data rows: rows with newline-packed amounts in the last column.
        for row in table[1:]:  # skip row 0 (header)
            if not row or len(row) < 7:
                continue
            last_cell = row[6] if row[6] else ""
            if not (
                isinstance(last_cell, str)
                and re.search(
                    r"\d{1,3}(?:,\d{3})*\.\d{2}\s*(?:CR|DR)",
                    last_cell,
                    re.IGNORECASE,
                )
            ):
                continue

            # Parse amounts from this data row
            amounts_raw = _split_nonempty(row[6] if len(row) > 6 else None)
            parsed_amounts: list[tuple[str, str]] = []
            for raw in amounts_raw:
                amt, suffix = _parse_amount_crdr(raw)
                if amt and suffix:
                    parsed_amounts.append((amt, suffix))
                else:
                    debug["rejected_rows"].append(
                        {"reason": "bad_amount_cell", "raw": raw}
                    )

            txn_count = len(parsed_amounts)
            if txn_count == 0:
                continue

            # Parse dates
            dates_raw = _split_nonempty(row[0] if len(row) > 0 else None)
            dates: list[str] = []
            for d in dates_raw:
                parsed = parse_date_token(d)
                dates.append(parsed if parsed else d)
            while len(dates) < txn_count:
                dates.append("")
            dates = dates[:txn_count]

            # Parse particulars — scan ALL lines for member headers, not just
            # the first. This handles add-on card sections mid-table.
            particulars_raw = _split_nonempty(
                row[2] if len(row) > 2 else None
            )
            current_member: str | None = None
            current_card_suffix: str | None = None
            narrations: list[str] = []

            for line in particulars_raw:
                hdr_match = _MEMBER_HEADER_RE.match(line)
                if hdr_match:
                    current_member = clean_space(hdr_match.group(1)).upper()
                    current_card_suffix = hdr_match.group(3)
                    if current_member not in debug["detected_members"]:
                        debug["detected_members"].append(current_member)
                else:
                    narrations.append(line)

            while len(narrations) < txn_count:
                narrations.append("")
            narrations = narrations[:txn_count]

            # Build reward points map
            rp_raw = _split_nonempty(row[3] if len(row) > 3 else None)
            dr_count = sum(1 for _, s in parsed_amounts if s == "DR")
            reward_map: dict[int, str] = {}
            if rp_raw and len(rp_raw) == dr_count:
                rp_cursor = 0
                for i, (_, suffix) in enumerate(parsed_amounts):
                    if suffix == "DR" and rp_cursor < len(rp_raw):
                        reward_map[i] = rp_raw[rp_cursor]
                        rp_cursor += 1
            elif rp_raw:
                debug["rejected_rows"].append(
                    {
                        "reason": "reward_points_count_mismatch",
                        "rp_count": len(rp_raw),
                        "dr_count": dr_count,
                    }
                )

            # Build transaction objects
            for i, (amt, suffix) in enumerate(parsed_amounts):
                date_val = dates[i] if i < len(dates) else ""
                narration = narrations[i] if i < len(narrations) else ""
                narration = clean_space(narration) if narration else ""
                is_credit = suffix == "CR"
                reward_points = reward_map.get(i)

                if not narration:
                    debug["rejected_rows"].append(
                        {
                            "reason": "empty_narration",
                            "index": i,
                            "date": date_val,
                            "amount": amt,
                        }
                    )
                    narration = "UNKNOWN"

                all_transactions.append(
                    Transaction(
                        date=date_val,
                        narration=narration,
                        amount=amt,
                        reward_points=reward_points,
                        card_number=None,
                        person=current_member,
                        transaction_type="credit" if is_credit else "debit",
                        credit_reasons="bob_cr_suffix" if is_credit else None,
                    )
                )

    debug["transaction_count"] = len(all_transactions)
    return all_transactions, debug


def _extract_bob_account_summary(
    pages: list[dict[str, Any]],
) -> StatementSummary:
    """Extract Account Summary values from BOB statement.

    BOB Account Summary table layout (4 columns):
      Opening Balance | Payment/Credits | New Purchases/Debits | Closing Balance

    Amounts appear as "₹ 54,003.36" with optional rupee prefix.
    """
    for page in pages[:3]:
        tables = page.get("tables") or []
        for table in tables:
            if not table:
                continue
            # Look for a table that has "Opening Balance" or "Payment" in headers
            # or has a row with 4 amount-like values
            for row_idx, row in enumerate(table):
                if not row:
                    continue
                # Check if this looks like the account summary header row
                joined = " ".join(
                    str(cell).upper() for cell in row if cell
                )
                if (
                    "OPENING" in joined and "BALANCE" in joined
                ) or (
                    "PAYMENT" in joined and "CREDIT" in joined and "PURCHASE" in joined
                ):
                    # The data row is the next row
                    if row_idx + 1 < len(table):
                        data_row = table[row_idx + 1]
                        amounts = []
                        for cell in data_row:
                            if not cell:
                                continue
                            cell_str = str(cell)
                            # Strip rupee symbol and extract amount
                            amt_match = re.search(r"([\d,]+\.\d{2})", cell_str)
                            if amt_match:
                                amounts.append(normalize_amount(amt_match.group(1)))
                        if len(amounts) >= 4:
                            return StatementSummary(
                                summary_amount_candidates=amounts,
                                previous_statement_dues=amounts[0],
                                payments_credits_received=amounts[1],
                                purchases_debit=amounts[2],
                                finance_charges=None,  # BOB doesn't break this out
                                equation_tail=amounts[3],
                            )

    # Fallback: scan page text for the summary amounts
    for page in pages[:3]:
        words = page.get("words") or []
        if not words:
            continue
        page_text = " ".join(str(w.get("text", "")) for w in words).upper()
        if "OPENING BALANCE" not in page_text and "ACCOUNT SUMMARY" not in page_text:
            continue

        lines = group_words_into_lines(words)
        for i, line_words in enumerate(lines):
            tokens = [normalize_token(str(w.get("text", ""))) for w in line_words]
            joined = clean_space(" ".join(tokens)).upper()
            if "OPENING" in joined and "BALANCE" in joined:
                # Collect amounts from this line and nearby lines
                candidate_amounts: list[str] = []
                for offset in range(0, 5):
                    scan_idx = i + offset
                    if scan_idx >= len(lines):
                        break
                    for w in lines[scan_idx]:
                        t = normalize_token(str(w.get("text", "")))
                        amt = parse_amount_token(t)
                        if amt:
                            candidate_amounts.append(normalize_amount(amt))
                if len(candidate_amounts) >= 4:
                    return StatementSummary(
                        summary_amount_candidates=candidate_amounts[:4],
                        previous_statement_dues=candidate_amounts[0],
                        payments_credits_received=candidate_amounts[1],
                        purchases_debit=candidate_amounts[2],
                        finance_charges=None,
                        equation_tail=candidate_amounts[3],
                    )

    return StatementSummary()


def _extract_bob_reward_points_balance(full_text: str) -> str | None:
    """Extract reward points closing balance from BOB statement.

    BOB Reward Points Summary section has a line like:
    ``5097 2035 0 7132`` where the last number is the closing balance.
    The section is labelled "Reward Points Summary" and has sub-labels
    "Opening", "Earned", "Redeemed", "Closing Balance".

    The layout puts "Closing Balance" on one line and the actual values
    (``5097 2035 0 7132``) on the next line. We find the 4-integer line
    near "Reward Points" and take the last value.
    """
    rp_start = full_text.upper().find("REWARD POINTS")
    if rp_start != -1:
        segment = full_text[rp_start : rp_start + 500]
        # Find a line with exactly 4 space-separated integers:
        # Opening Earned Redeemed Closing — take the last one.
        # Skip lines with "(cid:" which contain CID codes from Hindi text.
        for line in segment.splitlines():
            if "(cid:" in line.lower():
                continue
            nums = re.findall(r"\b\d+\b", line)
            if len(nums) == 4:
                return nums[3]

    return None


class BobParser(StatementParser):
    """Parser entrypoint for Bank of Baroda (BOBCARD) statements."""

    bank = "bob"

    def __init__(self) -> None:
        self._last_txn_debug: dict[str, Any] | None = None
        self._last_transactions: list[Transaction] | None = None

    def parse(self, raw_data: dict[str, Any]) -> ParsedStatement:
        pages = raw_data.get("pages", [])
        full_text = "\n".join(str(page.get("text", "")) for page in pages)

        name = _extract_bob_name(full_text)
        card_number = _extract_bob_card_number(full_text) or extract_card_from_filename(
            str(raw_data.get("file", ""))
        )

        transactions, txn_debug = _extract_bob_transactions(pages)
        self._last_txn_debug = txn_debug
        self._last_transactions = transactions

        # Fill in card number for transactions missing it
        if card_number:
            for txn in transactions:
                if not txn.card_number:
                    txn.card_number = card_number

        # Fix invalid person labels
        normalize_transaction_persons(transactions, name)

        debit_transactions, credit_transactions = split_by_transaction_type(transactions)

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

        due_date = _extract_bob_due_date(full_text, pages)
        statement_total_amount_due = _extract_bob_total_amount_due(full_text)
        summary_fields = _extract_bob_account_summary(pages)
        reconciliation = build_reconciliation(
            statement_total_amount_due,
            debit_transactions,
            credit_transactions,
            summary_fields,
        )

        reward_points_balance = _extract_bob_reward_points_balance(full_text)

        return ParsedStatement(
            file=str(raw_data.get("file", "")),
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
            transactions, txn_debug = _extract_bob_transactions(pages)

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
                "table_found": txn_debug.get("table_found", False),
                "detected_members": txn_debug.get("detected_members", []),
                "rejected_rows": len(txn_debug.get("rejected_rows", [])),
            },
            "card_from_filename": extract_card_from_filename(
                str(raw_data.get("file", ""))
            ),
            "txn_debug": txn_debug,
        }
