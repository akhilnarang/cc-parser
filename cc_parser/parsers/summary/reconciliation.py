"""Delta math for reconciling parsed transactions with statement totals."""

from decimal import Decimal

from cc_parser.parsers.models import Reconciliation, StatementSummary, Transaction
from cc_parser.parsers.tokens import (
    format_amount,
    parse_amount,
    parse_date_value,
    sum_amounts,
)


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
    """Build reconciliation metrics across statement/header/parsed totals."""
    debit_total = sum_amounts(debit_transactions)
    credit_total = sum_amounts(credit_transactions)

    statement_due = _to_decimal(statement_total_amount_due)
    parsed_net_due = debit_total - credit_total

    prev_dues = _to_decimal(summary_fields.previous_statement_dues)
    purchases = _to_decimal(summary_fields.purchases_debit)
    finance = _to_decimal(summary_fields.finance_charges)
    received = _to_decimal(summary_fields.payments_credits_received)
    header_computed_due = prev_dues + purchases + finance - received

    smart_expected = prev_dues + debit_total + finance - credit_total
    smart_delta = statement_due - smart_expected

    prev_balance_cleared_date: str | None = None
    excess_after_clearing: str | None = None
    if prev_dues > 0 and credit_transactions:
        dated_credits = []
        for txn in credit_transactions:
            dt = parse_date_value(txn.date)
            amount = parse_amount(str(txn.amount or "0"))
            if dt and amount > 0:
                dated_credits.append((dt, amount))
        dated_credits.sort(key=lambda item: item[0])

        running = Decimal("0")
        for dt, amount in dated_credits:
            running += amount
            if running >= prev_dues:
                prev_balance_cleared_date = dt.strftime("%d/%m/%Y")
                break

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


__all__ = ["build_reconciliation"]
