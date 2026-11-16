"""Offline baseline tests for the Axis Bank parser.

Both fixtures are fully synthetic raw extractions (``extract_raw_pdf`` shape)
that pin current ``AxisParser`` behaviour ahead of moving it onto
``GenericParser``.

``statement`` (two pages, Flipkart-style CASHBACK EARNED column):

- ``AXIS BANK`` page-1 header branding for detection;
- PAYMENT SUMMARY header/value rows: total due picked from the first
  amount + ``Dr`` pair, due date chosen by x-position under
  ``Payment Due Date``;
- account-summary equation row with separate ``Dr`` markers;
- ``Card No: ... Name ...`` member headers for a primary and an add-on card
  (card mask with ``*`` masking, name from that header);
- cashback column: the first amount/marker pair wins over the cashback pair;
- merchant-category stripping (``GROCERY``, ``FUEL``, ``DEPT STORES``...);
- ``Cr`` credits, both as a separate token and merged (``1,000.00Cr``);
- a date line without an amount (rejected) and a post
  ``**** End of Statement ****`` row that must be ignored.

``rewards_credit_balance`` (one page, no cashback column):

- card number from the ``Credit Card Number`` block (no ``Card No:`` line),
  name from the line after ``CREDIT CARD STATEMENT``;
- total payment due with ``Cr`` (credit balance, stored negative);
- ``eDGE REWARD POINTS`` balance; last amount/marker pair per row.
"""

from cc_parser.parsers.factory import detect_bank
from tests.fixture_support import assert_matches_golden, load_raw, parse_fixture

BANK = "axis"


def test_axis_statement_detected_from_header() -> None:
    """Detection keys off page-1 branding for both fixtures."""
    assert detect_bank(load_raw(BANK, "statement")) == "axis"
    assert detect_bank(load_raw(BANK, "rewards_credit_balance")) == "axis"


def test_axis_statement_key_fields() -> None:
    """Header fields, rows, and rollups of the cashback statement."""
    parsed = parse_fixture(BANK, "statement")

    assert parsed.bank == "axis"
    assert parsed.name == "TEST CARDHOLDER"
    assert parsed.card_number == "1111 11XX XXXX 0001"
    assert parsed.due_date == "01/03/2099"
    assert parsed.statement_total_amount_due == "11,111.00"

    assert len(parsed.transactions) == 5
    assert len(parsed.payments_refunds) == 2

    grocery = parsed.transactions[0]
    assert (grocery.date, grocery.narration, grocery.amount) == (
        "05/01/2099",
        "SYNTHETIC GROCERY STORE",
        "1,000.00",
    )
    assert grocery.transaction_type == "debit"
    assert grocery.person == "TEST CARDHOLDER"

    # Two trailing category tokens ("DEPT STORES") are stripped.
    shop = parsed.transactions[2]
    assert (shop.date, shop.narration, shop.amount) == (
        "12/01/2099",
        "DEMO ONLINE SHOP",
        "3,333.00",
    )

    # Current behaviour (likely a bug): "RESTAURANT" is in the merchant
    # category set, so trailing-category stripping also eats it from the
    # merchant name "SAMPLE RESTAURANT" when followed by "DINING".
    restaurant = parsed.transactions[3]
    assert (restaurant.date, restaurant.narration, restaurant.amount) == (
        "18/01/2099",
        "SAMPLE",
        "1,111.00",
    )
    assert restaurant.person == "SAMPLE ADDON"
    assert restaurant.card_number == "2222 22XX XXXX 0002"

    payment, refund = parsed.payments_refunds
    assert (payment.date, payment.narration, payment.amount) == (
        "10/01/2099",
        "BBPS PAYMENT RECEIVED TESTREF0000000001",
        "5,000.00",
    )
    assert payment.transaction_type == "credit"
    assert payment.credit_reasons == "cr_marker"
    # Merged "1,000.00Cr" token.
    assert (refund.date, refund.narration, refund.amount) == (
        "15/01/2099",
        "DEMO ONLINE SHOP REFUND",
        "1,000.00",
    )
    assert refund.transaction_type == "credit"

    # Neither the amount-less date line nor the post-end-of-statement row leaks.
    all_dates = {t.date for t in [*parsed.transactions, *parsed.payments_refunds]}
    assert "16/01/2099" not in all_dates
    assert "15/02/2099" not in all_dates

    assert parsed.payments_refunds_total == "6,000.00"
    assert parsed.overall_total == "12,111.00"
    assert parsed.overall_reward_points == "0"
    assert parsed.reward_points_balance is None

    cards = {c.card_number: (c.person, c.total_amount) for c in parsed.card_summaries}
    assert cards == {
        "1111 11XX XXXX 0001": ("TEST CARDHOLDER", "6,833.00"),
        "2222 22XX XXXX 0002": ("SAMPLE ADDON", "5,278.00"),
    }
    persons = {g.person: g.transaction_count for g in parsed.person_groups}
    assert persons == {"TEST CARDHOLDER": 3, "SAMPLE ADDON": 2}

    recon = parsed.reconciliation
    assert recon.header_previous_balance == "5,000.00"
    assert recon.header_payments_credits_received == "6,000.00"
    assert recon.header_purchases_debit == "12,111.00"
    assert recon.parsed_debit_total == "12,111.00"
    assert recon.parsed_credit_total == "6,000.00"
    assert recon.smart_delta == "0.00"
    assert recon.delta_statement_vs_header_estimate == "0.00"
    assert recon.prev_balance_cleared_date == "10/01/2099"


def test_axis_rewards_credit_balance_key_fields() -> None:
    """Credit-balance total, card-number block, and reward points balance."""
    parsed = parse_fixture(BANK, "rewards_credit_balance")

    assert parsed.name == "TEST CARDHOLDER"
    assert parsed.card_number == "3333 33XX XXXX 0003"
    assert parsed.due_date == "01/03/2099"
    assert parsed.statement_total_amount_due == "-500.00"
    assert parsed.reward_points_balance == "1250"
    assert parsed.overall_reward_points == "0"

    assert [(t.date, t.narration, t.amount) for t in parsed.transactions] == [
        ("03/01/2099", "EXAMPLE PHARMACY", "500.00"),
        ("06/01/2099", "SYNTHETIC ELECTRICITY BOARD", "1,000.00"),
    ]
    assert all(t.person == "TEST CARDHOLDER" for t in parsed.transactions)
    assert all(t.card_number == "3333 33XX XXXX 0003" for t in parsed.transactions)

    (payment,) = parsed.payments_refunds
    assert (payment.date, payment.narration, payment.amount) == (
        "09/01/2099",
        "UPI PAYMENT RECEIVED TESTREF0000000002",
        "4,000.00",
    )
    assert payment.transaction_type == "credit"

    assert parsed.payments_refunds_total == "4,000.00"
    assert parsed.overall_total == "1,500.00"
    assert len(parsed.card_summaries) == 1
    assert parsed.reconciliation.smart_expected_total == "-500.00"
    assert parsed.reconciliation.smart_delta == "0.00"
    assert parsed.reconciliation.excess_paid_after_clearing == "2,000.00"


def test_axis_statement_matches_golden() -> None:
    """Full dump of the cashback statement is unchanged."""
    assert_matches_golden(BANK, "statement", parse_fixture(BANK, "statement"))


def test_axis_rewards_credit_balance_matches_golden() -> None:
    """Full dump of the credit-balance statement is unchanged."""
    assert_matches_golden(
        BANK,
        "rewards_credit_balance",
        parse_fixture(BANK, "rewards_credit_balance"),
    )
