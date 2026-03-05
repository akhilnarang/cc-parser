"""Statement summary extraction and reconciliation logic."""

import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from cc_parser.parsers.models import (
    CardSummary,
    PersonGroup,
    Reconciliation,
    StatementSummary,
    Transaction,
)
from cc_parser.parsers.tokens import (
    clean_space,
    format_amount,
    normalize_amount,
    parse_amount,
    parse_points,
    sum_amounts,
    sum_points,
    normalize_token,
)
from cc_parser.parsers.extraction import group_words_into_lines


def extract_name(full_text: str) -> str | None:
    """Extract cardholder name from statement text."""
    honorifics = {"MR", "MRS", "MS", "MISS", "DR"}
    for raw_line in full_text.splitlines():
        line = clean_space(raw_line)
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3 or len(parts) > 6:
            continue
        if parts[0].upper() not in honorifics:
            continue
        tail = parts[1:]
        if all(re.fullmatch(r"[A-Za-z][A-Za-z.'-]*", token) for token in tail):
            return " ".join(tail)

    match = re.search(
        r"\n\s*([A-Z][A-Z ]{4,})\s+Credit\s+Card\s+No\.",
        full_text,
        flags=re.IGNORECASE,
    )
    if match:
        candidate = clean_space(match.group(1)).upper()
        if 2 <= len(candidate.split()) <= 6:
            return candidate

    return None


def extract_due_date(full_text: str) -> str | None:
    """Extract due date from statement body text."""
    compact_text = clean_space(full_text)
    patterns = [
        r"PAYMENT\s+DUE\s+DATE\s*[:\-]?\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})",
        r"PAYMENT\s+DUE\s+DATE\s*[:\-]?\s*(\d{2}[/-]\d{2}[/-]\d{4})",
        r"DUE\s+DATE\s*[:\-]?\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})",
        r"DUE\s+DATE\s*[:\-]?\s*(\d{2}[/-]\d{2}[/-]\d{4})",
        r"DUE\s+DATE.{0,100}?(\d{1,2}\s+[A-Za-z]{3,9},\s+\d{4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, compact_text, flags=re.IGNORECASE)
        if match:
            return clean_space(match.group(1))
    return None


def extract_due_date_from_pages(pages: list[dict[str, Any]]) -> str | None:
    """Extract due date using line-level page tokens."""
    for page in pages:
        lines = group_words_into_lines(page.get("words") or [])
        for line_index, line_words in enumerate(lines):
            tokens = [normalize_token(str(item.get("text", ""))) for item in line_words]
            upper = [token.upper() for token in tokens]
            joined = clean_space(" ".join(tokens))

            if "DUE" in upper and "DATE" in upper:
                inline = re.search(r"\d{2}[/-]\d{2}[/-]\d{4}", joined)
                if inline:
                    return inline.group(0)
                month_fmt = re.search(r"\d{1,2}\s+[A-Za-z]{3,9},\s+\d{4}", joined)
                if month_fmt:
                    return month_fmt.group(0)

                if line_index + 1 < len(lines):
                    next_tokens = [
                        normalize_token(str(item.get("text", "")))
                        for item in lines[line_index + 1]
                    ]
                    next_joined = clean_space(" ".join(next_tokens))
                    next_inline = re.search(r"\d{2}[/-]\d{2}[/-]\d{4}", next_joined)
                    if next_inline:
                        return next_inline.group(0)
                    next_month_fmt = re.search(
                        r"\d{1,2}\s+[A-Za-z]{3,9},\s+\d{4}",
                        next_joined,
                    )
                    if next_month_fmt:
                        return next_month_fmt.group(0)
    return None


def extract_total_amount_due(full_text: str) -> str | None:
    """Extract statement-level total amount due from summary area."""
    upper_text = full_text.upper()
    start = upper_text.find("TOTAL AMOUNT DUE")
    if start == -1:
        return None

    end = upper_text.find("TOTAL CREDIT LIMIT", start)
    segment = full_text[start:end] if end != -1 else full_text[start : start + 1200]

    for pattern in [r"C\s*\d[\d,]*\.\d{2}", r"`\s*\d[\d,]*\.\d{2}", r"\d[\d,]*\.\d{2}"]:
        match = re.search(pattern, segment)
        if match:
            return normalize_amount(match.group(0).replace("C", "").strip())

    return None


def extract_statement_summary(full_text: str) -> StatementSummary:
    """Extract summary block amount candidates and heuristic field mapping.

    Note: the positional mapping (indices 0-4) is fragile and depends on
    consistent statement layout.
    """
    upper_text = full_text.upper()
    start = upper_text.find("PAYMENTS/CREDITS")
    end = upper_text.find("TOTAL CREDIT LIMIT", start if start != -1 else 0)

    if start == -1:
        segment = full_text[:2000]
    else:
        segment = full_text[start : end if end != -1 else start + 2000]

    raw_amounts = [
        normalize_amount(match.group(0).replace("C", "").replace("`", "").strip())
        for match in re.finditer(r"[C`]?\s*\d[\d,]*\.\d{2}", segment)
    ]

    unique_amounts: list[str] = []
    for value in raw_amounts:
        if value not in unique_amounts:
            unique_amounts.append(value)

    summary = StatementSummary(summary_amount_candidates=unique_amounts)

    if len(unique_amounts) >= 5:
        summary.payments_credits_received = unique_amounts[0]
        summary.previous_statement_dues = unique_amounts[1]
        summary.purchases_debit = unique_amounts[2]
        summary.finance_charges = unique_amounts[3]
        summary.equation_tail = unique_amounts[4]

    return summary


def _to_decimal(amount: str | None) -> Decimal:
    if not amount:
        return Decimal("0")
    return parse_amount(amount)


def build_reconciliation(
    statement_total_amount_due: str | None,
    debit_transactions: list[Transaction],
    credit_transactions: list[Transaction],
    summary_fields: StatementSummary,
) -> Reconciliation:
    """Build reconciliation metrics across statement/header/parsed totals.

    The "smart" reconciliation computes:
        expected = previous_balance + parsed_debits + fees - parsed_credits

    This accounts for the fact that credit transactions include both
    payments toward previous dues *and* advance payments/refunds for
    current-cycle charges.  When the delta is near zero, all transactions
    are accounted for.
    """
    debit_total = sum_amounts(debit_transactions)
    credit_total = sum_amounts(credit_transactions)

    statement_due = _to_decimal(statement_total_amount_due)
    parsed_net_due = debit_total - credit_total

    prev_dues = _to_decimal(summary_fields.previous_statement_dues)
    purchases = _to_decimal(summary_fields.purchases_debit)
    finance = _to_decimal(summary_fields.finance_charges)
    received = _to_decimal(summary_fields.payments_credits_received)
    header_computed_due = prev_dues + purchases + finance - received

    # Smart reconciliation: expected = prev_balance + parsed_debits + fees - parsed_credits
    # This should equal the statement total when all transactions are captured.
    smart_expected = prev_dues + debit_total + finance - credit_total
    smart_delta = statement_due - smart_expected

    # Determine when previous balance was fully cleared by payments
    prev_balance_cleared_date: str | None = None
    excess_after_clearing: str | None = None
    if prev_dues > 0 and credit_transactions:
        dated_credits = []
        for txn in credit_transactions:
            dt = _parse_txn_date(txn.date)
            amount = parse_amount(str(txn.amount or "0"))
            if dt and amount > 0:
                dated_credits.append((dt, amount))
        dated_credits.sort(key=lambda x: x[0])

        running = Decimal("0")
        for dt, amount in dated_credits:
            running += amount
            if running >= prev_dues:
                prev_balance_cleared_date = dt.strftime("%d/%m/%Y")
                break

        # Excess is total credits minus previous balance — the portion that
        # went toward current-cycle charges rather than clearing old dues.
        excess_after_clearing = format_amount(credit_total - prev_dues)

    return Reconciliation(
        statement_total_amount_due=statement_total_amount_due,
        parsed_debit_total=format_amount(debit_total),
        parsed_credit_total=format_amount(credit_total),
        parsed_net_due_estimate=format_amount(parsed_net_due),
        header_previous_balance=format_amount(prev_dues),
        header_purchases_debit=summary_fields.purchases_debit or "",
        header_finance_charges=summary_fields.finance_charges or "",
        header_payments_credits_received=summary_fields.payments_credits_received or "",
        header_computed_due_estimate=format_amount(header_computed_due),
        smart_expected_total=format_amount(smart_expected),
        smart_delta=format_amount(smart_delta),
        prev_balance_cleared_date=prev_balance_cleared_date,
        excess_paid_after_clearing=excess_after_clearing,
        delta_statement_vs_parsed_debit=format_amount(statement_due - debit_total),
        delta_statement_vs_parsed_net=format_amount(statement_due - parsed_net_due),
        delta_statement_vs_header_estimate=format_amount(
            statement_due - header_computed_due
        ),
        summary_amount_candidates=summary_fields.summary_amount_candidates,
    )


def _parse_txn_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d/%m/%Y")
    except ValueError:
        return None


def split_paired_adjustments(
    debit_transactions: list[Transaction],
    credit_transactions: list[Transaction],
) -> tuple[list[Transaction], list[Transaction], list[Transaction]]:
    """Split offsetting debit/credit pairs into separate adjustments bucket."""

    def is_contextual_adjustment_debit(txn: Transaction) -> bool:
        narration = (txn.narration or "").upper()
        reward = (txn.reward_points or "").strip()
        return "CREDIT BALANCE REFUND" in narration and reward in {"", "0"}

    credit_buckets: dict[tuple[str, str, str], list[int]] = {}
    for idx, txn in enumerate(credit_transactions):
        narration_upper = (txn.narration or "").upper()
        if "PAYMENT" in narration_upper and "RECEIVED" in narration_upper:
            continue
        key = (
            txn.card_number or "UNKNOWN",
            txn.person or "UNKNOWN",
            txn.amount or "0",
        )
        credit_buckets.setdefault(key, []).append(idx)

    used_credit: set[int] = set()
    used_debit: set[int] = set()
    contextual_debit: set[int] = {
        idx
        for idx, txn in enumerate(debit_transactions)
        if is_contextual_adjustment_debit(txn)
    }

    for d_idx, debit in enumerate(debit_transactions):
        reward_token = (debit.reward_points or "0").strip()
        if reward_token not in {"", "0"}:
            continue

        key = (
            debit.card_number or "UNKNOWN",
            debit.person or "UNKNOWN",
            debit.amount or "0",
        )
        candidates = credit_buckets.get(key, [])
        if not candidates:
            continue

        debit_date = _parse_txn_date(debit.date)
        best_idx: int | None = None
        best_distance = 10**9

        for c_idx in candidates:
            if c_idx in used_credit:
                continue
            credit = credit_transactions[c_idx]
            credit_date = _parse_txn_date(credit.date)

            if debit_date is None or credit_date is None:
                distance = 999
            else:
                distance = abs((debit_date - credit_date).days)

            if distance <= 15 and distance < best_distance:
                best_distance = distance
                best_idx = c_idx

        if best_idx is not None:
            used_debit.add(d_idx)
            used_credit.add(best_idx)

    adjustments: list[Transaction] = []
    used_debit = used_debit | contextual_debit

    for idx in sorted(used_debit):
        update: dict[str, str] = {"adjustment_side": "debit"}
        if idx in contextual_debit:
            update["adjustment_reason"] = "credit_balance_refund"
        adjustments.append(debit_transactions[idx].model_copy(update=update))
    for idx in sorted(used_credit):
        adjustments.append(
            credit_transactions[idx].model_copy(update={"adjustment_side": "credit"})
        )

    kept_debits = [
        txn for idx, txn in enumerate(debit_transactions) if idx not in used_debit
    ]
    kept_credits = [
        txn for idx, txn in enumerate(credit_transactions) if idx not in used_credit
    ]
    return kept_debits, kept_credits, adjustments


def group_transactions_by_person(
    transactions: list[Transaction], fallback_name: str | None
) -> list[PersonGroup]:
    """Group transactions by person and compute totals/points."""
    grouped: dict[str, list[Transaction]] = {}
    for txn in transactions:
        person = txn.person or fallback_name or "UNKNOWN"
        grouped.setdefault(person, []).append(txn)

    grouped_rows: list[PersonGroup] = []
    for person, rows in grouped.items():
        total = sum_amounts(rows)
        points_total = sum_points(rows)
        grouped_rows.append(
            PersonGroup(
                person=person,
                transaction_count=len(rows),
                total_amount=format_amount(total),
                reward_points_total=str(int(points_total)),
                transactions=rows,
            )
        )

    grouped_rows.sort(key=lambda item: item.person)
    return grouped_rows


def build_card_summaries(
    transactions: list[Transaction], fallback_name: str | None
) -> tuple[list[CardSummary], str]:
    """Build person/card summary totals for parsed transactions."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}

    for txn in transactions:
        card_number = txn.card_number or "UNKNOWN"
        person = txn.person or fallback_name or "UNKNOWN"
        key = (card_number, person)

        if key not in grouped:
            grouped[key] = {
                "card_number": card_number,
                "person": person,
                "total": Decimal("0"),
                "points_total": Decimal("0"),
                "transaction_count": 0,
            }

        grouped[key]["total"] += parse_amount(str(txn.amount or "0"))
        grouped[key]["points_total"] += parse_points(txn.reward_points)
        grouped[key]["transaction_count"] += 1

    summaries: list[CardSummary] = []
    overall_total = Decimal("0")
    for item in grouped.values():
        total = item["total"]
        overall_total += total
        summaries.append(
            CardSummary(
                card_number=str(item["card_number"]),
                person=str(item["person"]),
                transaction_count=int(item["transaction_count"]),
                total_amount=format_amount(total),
                reward_points_total=str(int(item["points_total"])),
            )
        )

    summaries.sort(key=lambda row: (row.person, row.card_number))
    return summaries, format_amount(overall_total)


__all__ = [
    "extract_name",
    "extract_due_date",
    "extract_due_date_from_pages",
    "extract_total_amount_due",
    "extract_statement_summary",
    "build_reconciliation",
    "split_paired_adjustments",
    "group_transactions_by_person",
    "build_card_summaries",
]
