"""Pydantic models for parser input/output types."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class Transaction(BaseModel):
    """A single credit card transaction (debit or credit side)."""

    date: str
    time: str | None = None
    narration: str
    reward_points: str | None = None
    amount: str
    card_number: str | None = None
    person: str | None = None
    transaction_type: Literal["debit", "credit"] = "debit"
    credit_reasons: str | None = None
    adjustment_side: str | None = None
    adjustment_reason: str | None = None


class StatementSummary(BaseModel):
    """Account summary fields extracted from the statement header area."""

    summary_amount_candidates: list[str] = []
    payments_credits_received: str | None = None
    previous_statement_dues: str | None = None
    purchases_debit: str | None = None
    finance_charges: str | None = None
    equation_tail: str | None = None


class Reconciliation(BaseModel):
    """Reconciliation metrics across statement/header/parsed totals."""

    statement_total_amount_due: str | None = None
    parsed_debit_total: str
    parsed_credit_total: str
    parsed_net_due_estimate: str
    header_previous_balance: str
    header_purchases_debit: str
    header_finance_charges: str
    header_payments_credits_received: str
    header_computed_due_estimate: str
    smart_expected_total: str
    smart_delta: str
    prev_balance_cleared_date: str | None = None
    excess_paid_after_clearing: str | None = None
    delta_statement_vs_parsed_debit: str
    delta_statement_vs_parsed_net: str
    delta_statement_vs_header_estimate: str
    summary_amount_candidates: list[str] = []


class PersonGroup(BaseModel):
    """Transactions grouped by person with computed totals."""

    person: str
    transaction_count: int
    total_amount: str
    reward_points_total: str
    transactions: list[Transaction]


class CardSummary(BaseModel):
    """Aggregated totals for a person/card combination."""

    card_number: str
    person: str
    transaction_count: int
    total_amount: str
    reward_points_total: str


class ParsedStatement(BaseModel):
    """Root output of all statement parsers."""

    file: str
    bank: str
    name: str | None = None
    card_number: str | None = None
    due_date: str | None = None
    statement_total_amount_due: str | None = None
    card_summaries: list[CardSummary]
    overall_total: str
    person_groups: list[PersonGroup]
    payments_refunds: list[Transaction]
    payments_refunds_total: str
    adjustments: list[Transaction]
    adjustments_debit_total: str
    adjustments_credit_total: str
    overall_reward_points: str
    reward_points_balance: str | None = None
    transactions: list[Transaction]
    reconciliation: Reconciliation


__all__ = [
    "CardSummary",
    "ParsedStatement",
    "PersonGroup",
    "Reconciliation",
    "StatementSummary",
    "Transaction",
]
