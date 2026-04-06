"""Pydantic models for parser input/output types."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


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
    transaction_id: str = ""


class AdjustmentPair(BaseModel):
    """A detected debit/credit pair representing a refund or reversal."""

    pair_id: str
    debit_transaction_id: str | None
    credit_transaction_id: str | None
    debit: Transaction | None
    credit: Transaction | None
    score: int
    confidence: Literal["high", "medium", "low"]
    kind: Literal[
        "exact_refund",
        "partial_refund",
        "possible_refund",
        "reversal",
        "credit_balance_refund",
    ]
    amount_delta: str
    amount_delta_percent: str | None = None
    date_gap_days: int | None = None
    merchant_similarity: float | None = None
    narration_similarity: float | None = None
    reasons: list[str] = Field(default_factory=list)


class StatementSummary(BaseModel):
    """Account summary fields extracted from the statement header area."""

    summary_amount_candidates: list[str] = Field(default_factory=list)
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
    summary_amount_candidates: list[str] = Field(default_factory=list)


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
    overall_reward_points: str
    reward_points_balance: str | None = None
    transactions: list[Transaction]
    reconciliation: Reconciliation
    possible_adjustment_pairs: list[AdjustmentPair] = Field(default_factory=list)


__all__ = [
    "AdjustmentPair",
    "CardSummary",
    "ParsedStatement",
    "PersonGroup",
    "Reconciliation",
    "StatementSummary",
    "Transaction",
]
