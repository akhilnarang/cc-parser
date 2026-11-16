"""Offline baseline for the Suryoday SFB (SSFB) parser.

The SSFB parser reads name, due date, total due and the account summary
from page 1 text only, searches every page for a card mask, and emits no
transactions (they are hardcoded to an empty list today).

Synthetic fixtures under ``tests/fixtures/ssfb/``:

- ``statement``: page 1 with branding, a name line followed by
  ``Email :``, a spaced card mask, the three-column total-due row and the
  two-row account summary; page 2 an empty transaction page; page 3 an
  MITC/illustrative-example page whose name, due date, total due and
  summary values must not leak into the output.
- ``fallbacks``: no name, no in-text card mask (filename fallback), the
  total due outside the three-column header (fallback regex), a negative
  credit balance and no summary blocks. The filename has no bank token, so
  detection relies on page-1 branding.
- ``mitc_card_leak``: like ``fallbacks`` plus an MITC page carrying an
  example card mask.
"""

from __future__ import annotations

from cc_parser.parsers.factory import detect_bank
from tests.fixture_support import assert_matches_golden, load_raw, parse_fixture

BANK = "ssfb"
CASES = ("statement", "fallbacks", "mitc_card_leak")


def test_detects_ssfb_from_header() -> None:
    """Every fixture routes to SSFB, even without a bank token in the name."""
    for case in CASES:
        assert detect_bank(load_raw(BANK, case)) == BANK


def test_statement_key_fields() -> None:
    """Page-1 fields win over the MITC example page."""
    parsed = parse_fixture(BANK, "statement")

    assert parsed.bank == BANK
    assert parsed.name == "TEST CARDHOLDER"
    # Unlike most parsers, SSFB returns the mask without group spacing.
    assert parsed.card_number == "6000XXXXXXXX0001"
    assert parsed.due_date == "30/01/2099"
    assert parsed.statement_total_amount_due == "2,618.00"
    assert parsed.transactions == []
    assert parsed.payments_refunds == []
    assert parsed.payments_refunds_total == "0.00"
    assert parsed.overall_total == "0.00"
    assert parsed.overall_reward_points == "0"
    assert parsed.card_summaries == []
    assert parsed.person_groups == []


def test_statement_reconciliation() -> None:
    """Summary blocks feed reconciliation; finance = fees + cash + GST."""
    rec = parse_fixture(BANK, "statement").reconciliation

    assert rec.header_previous_balance == "1,000.00"
    assert rec.header_purchases_debit == "2,000.00"
    assert rec.header_finance_charges == "118.00"
    assert rec.header_payments_credits_received == "500.00"
    assert rec.header_computed_due_estimate == "2,618.00"
    assert rec.delta_statement_vs_header_estimate == "0.00"
    # No transactions are parsed, so the smart estimate misses purchases.
    assert rec.parsed_debit_total == "0.00"
    assert rec.smart_delta == "1,500.00"
    assert rec.summary_amount_candidates == [
        "1,000.00",
        "2,000.00",
        "100.00",
        "0.00",
        "18.00",
        "500.00",
    ]


def test_fallbacks_key_fields() -> None:
    """Filename card fallback, fallback total-due regex, default summary."""
    parsed = parse_fixture(BANK, "fallbacks")

    assert parsed.name is None
    assert parsed.card_number == "6000XXXXXXXX0002"
    assert parsed.due_date == "30/01/2099"
    assert parsed.statement_total_amount_due == "-100.00"

    rec = parsed.reconciliation
    assert rec.header_previous_balance == "0.00"
    assert rec.header_finance_charges == "0.00"
    assert rec.header_computed_due_estimate == "0.00"
    assert rec.delta_statement_vs_header_estimate == "-100.00"


def test_mitc_card_leak() -> None:
    """Card search spans all pages, so an MITC example mask beats the filename.

    Current behaviour, likely a bug: page isolation covers name, due date,
    total due and summary, but not the card number.
    """
    parsed = parse_fixture(BANK, "mitc_card_leak")

    assert parsed.card_number == "6000XXXXXXXX1111"
    assert parsed.due_date == "30/01/2099"
    assert parsed.statement_total_amount_due == "-100.00"


def test_matches_golden() -> None:
    """Full dumps of every SSFB fixture are unchanged."""
    for case in CASES:
        assert_matches_golden(BANK, case, parse_fixture(BANK, case))
