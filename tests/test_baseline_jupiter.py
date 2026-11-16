"""Offline baseline for the Jupiter (Edge CSB Bank) parser.

Two synthetic fixtures pin current ``JupiterParser`` behaviour ahead of the
planned move onto ``GenericParser`` (see TODO-parser-cleanup.md):

``statement`` (labeled layout)
    Page-1 ``Name / Card number`` header row, ``XXXX XXXX XXXX DDDD`` mask,
    ``BILL SUMMARY`` header row with total/minimum/due-date values on one
    line, a labeled ``STATEMENT SUMMARY`` (amount on the same line and on the
    following line). Page 2 has ``DD Mon YYYY`` three-token dates, separate
    ``HH:MM AM/PM`` time lines (plus a 24h time without AM/PM and a row with
    no time line), ``Rs.``/``Rs`` prefixed amounts with and without decimals,
    narration-keyword credits (Repayment, Refund, Jewels converted), an exact
    refund adjustment pair, a statement-period date line that must be
    rejected, and a row after ``End of Transactions`` that must be ignored.

``labelless`` (2026+ vector-label layout)
    No labels on page 1: name from the first ALL-CAPS line, total/due date,
    and summary values read positionally around the ``Pay your bill now``
    anchor; card suffix from the ``Rupay Transactions - DDDD`` header.
"""

from cc_parser.parsers.factory import detect_bank
from tests.fixture_support import assert_matches_golden, load_raw, parse_fixture

BANK = "jupiter"


def test_detects_jupiter_from_header() -> None:
    """Both layouts route to the Jupiter parser."""
    for case in ("statement", "labelless"):
        assert detect_bank(load_raw(BANK, case)) == BANK


def test_labeled_statement_fields() -> None:
    """Labeled layout: header fields, transactions, and reconciliation."""
    parsed = parse_fixture(BANK, "statement")

    assert parsed.bank == BANK
    assert parsed.name == "TEST CARDHOLDER"
    assert parsed.card_number == "XXXX XXXX XXXX 0001"
    # Current behaviour (likely bug): the due-date scan in
    # ``_extract_jupiter_due_date`` slides a 3-token window over the value
    # row "Rs. 3,268.50 Rs. 200 20 Feb 2099" and dateutil's lenient parse
    # accepts "200 20 Feb" first, yielding year 0200 instead of 20/02/2099.
    assert parsed.due_date == "20/02/0200"
    assert parsed.statement_total_amount_due == "3,268.50"

    debits = parsed.transactions
    credits = parsed.payments_refunds
    assert len(debits) == 6
    assert len(credits) == 3

    grocery = debits[0]
    assert grocery.date == "03/01/2099"
    assert grocery.time == "10:15 AM"
    assert grocery.narration == "SYNTHETIC GROCERY STORE"
    assert grocery.amount == "1,200.00"
    assert grocery.transaction_type == "debit"

    # ``Rs`` without a dot, whole-number amount, 24h time without AM/PM.
    online = next(t for t in debits if t.narration == "DEMO ONLINE SHOP")
    assert online.amount == "1,500.00"
    assert online.time == "21:05"
    # No time line follows this row.
    cafe = next(t for t in debits if t.narration == "SAMPLE CAFE")
    assert cafe.time is None
    assert cafe.amount == "250.00"
    # Nothing after ``End of Transactions`` is parsed.
    assert all("AFTER END MARKER" not in t.narration for t in debits + credits)

    repayment = credits[0]
    assert repayment.date == "08/01/2099"
    assert repayment.narration == "Repayment received TESTREF0000000001"
    assert repayment.amount == "1,500.00"
    assert repayment.transaction_type == "credit"
    assert repayment.credit_reasons == "narration_keyword:repayment"
    assert [t.credit_reasons for t in credits[1:]] == [
        "narration_keyword:refund",
        "narration_keyword:jewels converted",
    ]
    assert all(t.card_number == "XXXX XXXX XXXX 0001" for t in debits + credits)
    assert all(t.person == "TEST CARDHOLDER" for t in debits + credits)

    assert parsed.payments_refunds_total == "1,800.00"
    assert parsed.overall_total == "3,568.50"
    assert parsed.overall_reward_points == "0"
    assert len(parsed.card_summaries) == 1
    assert len(parsed.person_groups) == 1

    pairs = parsed.possible_adjustment_pairs
    assert len(pairs) == 1
    assert pairs[0].kind == "exact_refund"

    recon = parsed.reconciliation
    assert recon.header_previous_balance == "1,500.00"
    assert recon.header_purchases_debit == "3,450.50"
    assert recon.header_finance_charges == "118.00"
    assert recon.header_payments_credits_received == "1,800.00"
    assert recon.header_computed_due_estimate == "3,268.50"
    assert recon.delta_statement_vs_header_estimate == "0.00"

    assert_matches_golden(BANK, "statement", parsed)


def test_labelless_statement_fields() -> None:
    """Label-less layout: positional page-1 values and Rupay card suffix."""
    parsed = parse_fixture(BANK, "labelless")

    assert parsed.bank == BANK
    assert parsed.name == "SAMPLE MEMBER"
    assert parsed.card_number == "XXXX XXXX XXXX 0002"
    assert parsed.due_date == "10/04/2099"
    assert parsed.statement_total_amount_due == "3,000.00"

    debits = parsed.transactions
    credits = parsed.payments_refunds
    assert len(debits) == 3
    assert len(credits) == 2

    shop = debits[0]
    assert shop.date == "05/03/2099"
    assert shop.time == "08:00 PM"
    assert shop.narration == "DEMO ONLINE SHOP"
    assert shop.amount == "2,000.00"
    assert shop.transaction_type == "debit"

    reversal = credits[1]
    assert reversal.date == "20/03/2099"
    assert reversal.narration == "EXAMPLE FUEL STATION reversal"
    assert reversal.amount == "500.00"
    assert reversal.transaction_type == "credit"
    assert reversal.credit_reasons == "narration_keyword:reversal"

    assert parsed.payments_refunds_total == "1,500.00"
    assert parsed.overall_total == "3,500.00"

    recon = parsed.reconciliation
    assert recon.header_previous_balance == "1,000.00"
    assert recon.header_purchases_debit == "3,500.00"
    assert recon.header_payments_credits_received == "1,500.00"
    assert recon.header_computed_due_estimate == "3,000.00"
    assert recon.smart_delta == "0.00"

    assert_matches_golden(BANK, "labelless", parsed)
