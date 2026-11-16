"""Offline baseline for the IndusInd parser.

Synthetic fixtures under ``tests/fixtures/indusind/``:

- ``statement``: three pages. Page 1 holds the branded header (plus an
  "ICICI Lombard" fine-print line, so detection order matters), the
  right-sidebar summary with labels and values on one line, and a
  ``Payment Details`` section with a ``CR``-marked payment whose
  ``(Ref#`` wraps onto the next line and a marker-less credit row that is
  classified by section. Page 2 holds two ``Purchases & Cash Transactions``
  member sections (primary and add-on) with merchant categories, reward
  points, a merged ``1,111.11DR`` marker, a numeric narration token left
  of the midpoint (must not become points), and a ``CR`` refund inside a
  purchases section. Page 3 has a date-led line outside any section that
  must be ignored.
- ``credit_balance``: one page where summary labels and values sit on
  separate lines (next-line lookups), ``CR`` totals become negative, and
  every amount carries a merged ``DR`` marker.
"""

from __future__ import annotations

from cc_parser.parsers.factory import detect_bank
from tests.fixture_support import assert_matches_golden, load_raw, parse_fixture

BANK = "indusind"
PRIMARY_CARD = "4000 XXXX XXXX 0001"
ADDON_CARD = "4000 XXXX XXXX 0002"


def test_detects_indusind_from_header() -> None:
    """Both fixtures route to IndusInd even with ICICI fine print present."""
    for case in ("statement", "credit_balance"):
        assert detect_bank(load_raw(BANK, case)) == BANK


def test_statement_key_fields() -> None:
    """Header, summary and debit rows of the main statement."""
    parsed = parse_fixture(BANK, "statement")

    assert parsed.bank == BANK
    assert parsed.name == "TEST CARDHOLDER"
    assert parsed.card_number == PRIMARY_CARD
    assert parsed.due_date == "30/01/2099"
    assert parsed.statement_total_amount_due == "5,000.00"
    assert parsed.overall_total == "4,500.00"
    assert parsed.payments_refunds_total == "1,500.00"
    assert parsed.overall_reward_points == "46"

    assert len(parsed.transactions) == 6
    assert len(parsed.payments_refunds) == 3

    debits = parsed.transactions
    # The merchant category column stays in the narration (current behaviour).
    assert debits[0].date == "02/01/2099"
    assert debits[0].narration == "SYNTHETIC GROCERY STORE GROCERY"
    assert debits[0].amount == "1,000.00"
    assert debits[0].reward_points == "10"
    assert debits[0].transaction_type == "debit"
    assert debits[0].person == "TEST CARDHOLDER"
    assert debits[0].card_number == PRIMARY_CARD

    # Merged "1,111.11DR" marker.
    assert debits[2].narration == "DEMO ONLINE SHOP SHOPPING"
    assert debits[2].amount == "1,111.11"
    assert debits[2].reward_points == "11"

    # Numeric token left of the date/amount midpoint is narration, not points.
    assert debits[3].narration == "EXAMPLE STORE 123"
    assert debits[3].amount == "388.89"
    assert debits[3].reward_points is None

    addon = [t for t in debits if t.card_number == ADDON_CARD]
    assert [t.person for t in addon] == ["SAMPLE ADDON", "SAMPLE ADDON"]
    assert [t.amount for t in addon] == ["1,250.00", "250.00"]


def test_statement_credits() -> None:
    """CR-marker, section-derived and in-purchases refund credits."""
    parsed = parse_fixture(BANK, "statement")
    credits = {t.date: t for t in parsed.payments_refunds}

    payment = credits["03/01/2099"]
    assert payment.amount == "1,000.00"
    assert payment.transaction_type == "credit"
    assert payment.credit_reasons == "cr_marker"
    # Current behaviour (likely a bug): the "(Ref#" context merge pulls the
    # preceding summary label and member-header line into the narration, and
    # the header's closing ")" truncates it before the real wrapped reference.
    assert payment.narration == (
        "PAYMENT RECEIVED (Ref# Payment & Other Credits Payment Details for MR "
        "TEST CARDHOLDER (Credit Card No. 4000XXXXXXXX0001)"
    )

    autopay = credits["07/01/2099"]
    assert autopay.narration == "AUTOPAY TESTREF0000000002"
    assert autopay.amount == "300.00"
    assert autopay.credit_reasons == "payment_details_section"

    refund = credits["10/01/2099"]
    assert refund.narration == "DEMO ONLINE SHOP REFUND SHOPPING"
    assert refund.amount == "200.00"
    assert refund.credit_reasons == "cr_marker"

    pairs = parsed.possible_adjustment_pairs
    assert len(pairs) == 1
    assert pairs[0].kind == "possible_refund"


def test_statement_groups_and_reconciliation() -> None:
    """Per-card summaries and header-vs-parsed reconciliation."""
    parsed = parse_fixture(BANK, "statement")

    summaries = {s.card_number: s for s in parsed.card_summaries}
    assert summaries[PRIMARY_CARD].person == "TEST CARDHOLDER"
    assert summaries[PRIMARY_CARD].transaction_count == 4
    assert summaries[PRIMARY_CARD].total_amount == "3,000.00"
    assert summaries[ADDON_CARD].person == "SAMPLE ADDON"
    assert summaries[ADDON_CARD].total_amount == "1,500.00"
    assert summaries[ADDON_CARD].reward_points_total == "20"
    assert {g.person for g in parsed.person_groups} == {
        "TEST CARDHOLDER",
        "SAMPLE ADDON",
    }

    rec = parsed.reconciliation
    assert rec.parsed_debit_total == "4,500.00"
    assert rec.parsed_credit_total == "1,500.00"
    assert rec.header_previous_balance == "2,000.00"
    assert rec.header_purchases_debit == "4,500.00"
    assert rec.header_payments_credits_received == "1,500.00"
    assert rec.header_computed_due_estimate == "5,000.00"
    assert rec.smart_delta == "0.00"
    assert rec.delta_statement_vs_header_estimate == "0.00"


def test_credit_balance_key_fields() -> None:
    """Next-line summary values and CR-signed negative totals."""
    parsed = parse_fixture(BANK, "credit_balance")

    assert parsed.name == "TEST CARDHOLDER"
    assert parsed.card_number == PRIMARY_CARD
    assert parsed.due_date == "30/01/2099"
    assert parsed.statement_total_amount_due == "-300.00"
    assert parsed.overall_total == "700.00"
    assert parsed.payments_refunds == []
    assert parsed.payments_refunds_total == "0.00"
    assert [(t.date, t.amount, t.reward_points) for t in parsed.transactions] == [
        ("05/01/2099", "400.00", "4"),
        ("12/01/2099", "300.00", "3"),
    ]

    rec = parsed.reconciliation
    assert rec.header_previous_balance == "-1,000.00"
    assert rec.header_purchases_debit == "700.00"
    assert rec.header_computed_due_estimate == "-300.00"
    assert rec.smart_delta == "0.00"


def test_statement_matches_golden() -> None:
    """Full dump of the main statement is unchanged."""
    assert_matches_golden(BANK, "statement", parse_fixture(BANK, "statement"))


def test_credit_balance_matches_golden() -> None:
    """Full dump of the credit-balance statement is unchanged."""
    assert_matches_golden(BANK, "credit_balance", parse_fixture(BANK, "credit_balance"))
