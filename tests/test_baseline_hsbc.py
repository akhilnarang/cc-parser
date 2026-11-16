"""Offline baseline tests for the HSBC parser.

Both fixtures are fully synthetic raw extractions (``extract_raw_pdf`` shape)
that pin current ``HsbcParser`` behaviour ahead of moving it onto
``GenericParser``.

``statement`` (two pages, labelled summary, primary + add-on card):

- ``HSBC`` page-1 header branding for detection;
- statement period spanning two years, so ``DDMMM`` dates in DEC resolve to
  the start year and JAN dates to the end year;
- labelled PAYMENT SUMMARY (total due on the line after the label, due date
  ``DD MMM YYYY`` on the line after ``Payment due date``);
- columnar ACCOUNT SUMMARY and REWARD POINTS SUMMARY (earned + closing);
- a payment under ``OPENING BALANCE`` before any card header (falls back to
  the statement name/card), then card + name member headers;
- address-block name (``MR TEST MIDDLE CARDHOLDER``) fuller than the member
  header name (``MR TEST CARDHOLDER``) on the same mask: canonicalized;
- ``CR`` credits as separate and merged (``1,000.00CR``) tokens;
- EMI principal transfer credit + matching debit with an installment detail
  line (excluded from smart reconciliation credits);
- split date tokens (``13 JAN``) and a date alone on its line whose
  narration/amount sit on the next visual line;
- rows after ``IMPORTANT INFORMATION`` (stop header) are ignored.

``unlabeled_summary`` (one page, single card): payment-summary labels are
missing, so total due comes from the statement-period row and the due date
from the first non-period ``DD MMM YYYY`` in the header area; reward points
come from the last of two unlabeled 4-amount rows; the name comes from the
card + name member header (no honorific); single-card reward rollups apply.
"""

from cc_parser.parsers.factory import detect_bank
from tests.fixture_support import assert_matches_golden, load_raw, parse_fixture

BANK = "hsbc"


def test_hsbc_statement_detected_from_header() -> None:
    """Detection keys off page-1 branding for both fixtures."""
    assert detect_bank(load_raw(BANK, "statement")) == "hsbc"
    assert detect_bank(load_raw(BANK, "unlabeled_summary")) == "hsbc"


def test_hsbc_statement_key_fields() -> None:
    """Header fields, rows, EMI handling, and rollups of the main statement."""
    parsed = parse_fixture(BANK, "statement")

    assert parsed.bank == "hsbc"
    assert parsed.name == "TEST MIDDLE CARDHOLDER"
    assert parsed.card_number == "11XX XXXX XXXX 0001"
    assert parsed.due_date == "08/02/2099"
    assert parsed.statement_total_amount_due == "11,111.00"

    assert [(t.date, t.narration, t.amount) for t in parsed.transactions] == [
        ("20/12/2098", "SYNTHETIC GROCERY STORE", "1,000.00"),
        ("28/12/2098", "EXAMPLE FUEL STATION", "2,500.00"),
        ("05/01/2099", "DEMO ONLINE SHOP", "3,333.00"),
        (
            "10/01/2099",
            "EMI PRINCIPAL AMOUNT 1ST OF 6 INSTALLMENTS PRINCIPAL",
            "1,000.00",
        ),
        ("12/01/2099", "SAMPLE RESTAURANT", "1,111.00"),
        ("13/01/2099", "EXAMPLE TRAVEL BOOKING", "2,667.00"),
        ("14/01/2099", "EXAMPLE PHARMACY", "500.00"),
    ]
    assert all(t.transaction_type == "debit" for t in parsed.transactions)
    # Member-header short name is canonicalized to the address-block name.
    assert {t.person for t in parsed.transactions[:4]} == {"TEST MIDDLE CARDHOLDER"}
    assert {t.person for t in parsed.transactions[4:]} == {"SAMPLE ADDON"}
    assert {t.card_number for t in parsed.transactions[4:]} == {"22XX XXXX XXXX 0002"}

    payment, refund, emi_transfer = parsed.payments_refunds
    assert (payment.date, payment.narration, payment.amount) == (
        "18/12/2098",
        "PAYMENT RECEIVED TESTREF0000000001",
        "5,000.00",
    )
    assert payment.transaction_type == "credit"
    assert payment.credit_reasons == "cr_marker"
    assert payment.person == "TEST MIDDLE CARDHOLDER"
    assert payment.card_number == "11XX XXXX XXXX 0001"
    assert (refund.date, refund.narration, refund.amount) == (
        "08/01/2099",
        "DEMO ONLINE SHOP REFUND",
        "1,000.00",
    )
    assert refund.credit_reasons == "cr_marker"
    assert emi_transfer.date == "10/01/2099"
    assert emi_transfer.amount == "1,000.00"
    assert emi_transfer.credit_reasons == "emi_installment_transfer"

    # Rows after IMPORTANT INFORMATION are not transactions.
    assert all(
        t.narration != "EXAMPLE TARIFF ILLUSTRATION" for t in parsed.transactions
    )

    assert parsed.payments_refunds_total == "7,000.00"
    assert parsed.overall_total == "12,111.00"
    assert parsed.overall_reward_points == "25"
    assert parsed.reward_points_balance == "125"

    cards = {
        c.card_number: (c.person, c.total_amount, c.reward_points_total)
        for c in parsed.card_summaries
    }
    # Multi-card statements do not get the statement-level points allocated.
    assert cards == {
        "11XX XXXX XXXX 0001": ("TEST MIDDLE CARDHOLDER", "7,833.00", "0"),
        "22XX XXXX XXXX 0002": ("SAMPLE ADDON", "4,278.00", "0"),
    }
    persons = {g.person: g.transaction_count for g in parsed.person_groups}
    assert persons == {"TEST MIDDLE CARDHOLDER": 4, "SAMPLE ADDON": 3}

    # Current behaviour (questionable): the 1,000.00 refund is paired with the
    # EMI principal debit rather than a same-merchant purchase, purely on the
    # exact amount and short date gap.
    (pair,) = parsed.possible_adjustment_pairs
    assert pair.debit is not None
    assert pair.credit is not None
    assert pair.debit.narration.startswith("EMI PRINCIPAL AMOUNT")
    assert pair.credit.narration == "DEMO ONLINE SHOP REFUND"

    recon = parsed.reconciliation
    assert recon.header_previous_balance == "5,000.00"
    assert recon.header_purchases_debit == "12,111.00"
    assert recon.header_payments_credits_received == "6,000.00"
    assert recon.parsed_credit_total == "7,000.00"
    assert recon.smart_credit_total == "6,000.00"
    assert recon.smart_excluded_credit_total == "1,000.00"
    assert recon.smart_expected_total == "11,111.00"
    assert recon.smart_delta == "0.00"
    assert recon.prev_balance_cleared_date == "18/12/2098"
    assert recon.summary_amount_candidates == [
        "5,000.00",
        "12,111.00",
        "6,000.00",
        "16,111.00",
    ]


def test_hsbc_unlabeled_summary_key_fields() -> None:
    """Positional fallbacks for total due, due date, name, and points."""
    parsed = parse_fixture(BANK, "unlabeled_summary")

    assert parsed.name == "TEST CARDHOLDER"
    assert parsed.card_number == "33XX XXXX XXXX 0003"
    assert parsed.due_date == "08/03/2099"
    assert parsed.statement_total_amount_due == "2,000.00"

    assert [(t.date, t.narration, t.amount) for t in parsed.transactions] == [
        ("20/01/2099", "SYNTHETIC GROCERY STORE", "1,500.00"),
        ("25/01/2099", "EXAMPLE FUEL STATION", "500.00"),
    ]
    (payment,) = parsed.payments_refunds
    assert (payment.date, payment.narration, payment.amount) == (
        "30/01/2099",
        "PAYMENT RECEIVED TESTREF0000000002",
        "1,000.00",
    )
    assert payment.transaction_type == "credit"

    assert parsed.payments_refunds_total == "1,000.00"
    assert parsed.overall_total == "2,000.00"
    assert parsed.overall_reward_points == "50"
    assert parsed.reward_points_balance == "50"
    # Single card/person: statement-level earned points are rolled up.
    (card,) = parsed.card_summaries
    assert card.reward_points_total == "50"
    (person,) = parsed.person_groups
    assert person.reward_points_total == "50"

    assert parsed.reconciliation.smart_delta == "0.00"
    assert parsed.reconciliation.delta_statement_vs_header_estimate == "0.00"


def test_hsbc_statement_matches_golden() -> None:
    """Full dump of the main statement is unchanged."""
    assert_matches_golden(BANK, "statement", parse_fixture(BANK, "statement"))


def test_hsbc_unlabeled_summary_matches_golden() -> None:
    """Full dump of the unlabeled-summary statement is unchanged."""
    assert_matches_golden(
        BANK, "unlabeled_summary", parse_fixture(BANK, "unlabeled_summary")
    )
