"""Offline baseline tests for the BOB (BOBCARD) parser.

The fixtures under ``tests/fixtures/bob/`` are fully synthetic raw
extractions that mimic the BOBCARD layout:

* ``statement``: one page. ``BOBCARD`` branding in the page-1 header, a
  dotted honorific name (``MR.``), a ``Card No:`` mask, an inline
  ``Payment Due Date``, a ``Total Amount Due`` with a glued ``DR`` suffix,
  a four-column Account Summary table, a Reward Points Summary, and one
  transaction table whose data row packs every entry into newline-separated
  cells. The particulars cell holds a PRIMARY member header and, mid-cell,
  an ADDON member header. The amount column mixes ``CR``/``DR`` suffixes,
  with and without a space. The reward-points cell aligns with DR rows only.
* ``multipage_credit_balance``: two pages. The due date sits below its
  header in a column layout, so the page-word fallback finds it. The total
  due has a ``CR`` suffix, so it is negated. There is no Account Summary
  table, so the summary comes from the word-line fallback. The transaction
  table continues on page 2 without a ``TRANSACTION`` header cell or member
  header. That continuation has a reward-points count mismatch, which drops
  the points, and one entry has no narration, which becomes ``UNKNOWN``.

The goldens record current behaviour, quirks included, so the planned
move onto a table-oriented GenericParser hook (TODO-parser-cleanup.md)
can be checked for drift.
"""

from cc_parser.parsers.factory import detect_bank
from tests.fixture_support import assert_matches_golden, load_raw, parse_fixture

BANK = "bob"


def test_statement_detects_bob_from_header() -> None:
    """Detection keys off the page-1 BOBCARD header, not the filename."""
    raw = load_raw(BANK, "statement")
    assert "BOBCARD" not in raw["file"].upper()
    assert detect_bank(raw) == BANK


def test_statement_key_fields() -> None:
    """Header fields, debit/credit split and reconciliation of the main case."""
    parsed = parse_fixture(BANK, "statement")

    assert parsed.bank == BANK
    assert parsed.name == "TEST CARDHOLDER"
    assert parsed.card_number == "1111XXXXXXXX0001"
    assert parsed.due_date == "25/01/2100"
    assert parsed.statement_total_amount_due == "4,000.00"
    assert parsed.reward_points_balance == "147"
    assert parsed.overall_reward_points == "47"

    assert len(parsed.transactions) == 5
    assert len(parsed.payments_refunds) == 2

    first_debit = parsed.transactions[0]
    assert first_debit.date == "13/12/2099"
    assert first_debit.narration == "SYNTHETIC GROCERY STORE SAMPLE CITY"
    assert first_debit.amount == "1,000.00"
    assert first_debit.reward_points == "10"
    assert first_debit.transaction_type == "debit"

    # "2,000.00DR" has no space before the suffix.
    glued = parsed.transactions[2]
    assert glued.narration == "DEMO ONLINE SHOP"
    assert glued.amount == "2,000.00"
    assert glued.reward_points == "20"

    payment = parsed.payments_refunds[0]
    assert payment.date == "12/12/2099"
    assert payment.narration == "PAYMENT RECEIVED TESTREF0000000001"
    assert payment.amount == "2,500.00"
    assert payment.transaction_type == "credit"
    assert payment.credit_reasons == "bob_cr_suffix"
    assert payment.reward_points is None

    refund = parsed.payments_refunds[1]
    assert refund.narration == "REFUND DEMO ONLINE SHOP"
    assert refund.amount == "200.00"

    assert parsed.payments_refunds_total == "2,700.00"
    assert parsed.overall_total == "4,700.00"

    rec = parsed.reconciliation
    assert rec.parsed_debit_total == "4,700.00"
    assert rec.parsed_credit_total == "2,700.00"
    assert rec.header_previous_balance == "2,000.00"
    assert rec.header_payments_credits_received == "2,700.00"
    assert rec.header_purchases_debit == "4,700.00"
    assert rec.header_computed_due_estimate == "4,000.00"
    assert rec.smart_delta == "0.00"
    assert rec.delta_statement_vs_header_estimate == "0.00"

    assert len(parsed.possible_adjustment_pairs) == 1


def test_statement_member_attribution_current_behaviour() -> None:
    """Pin the current member attribution for a packed row with two members.

    Possible parser bug, kept in the baseline: ``_extract_bob_transactions``
    scans every particulars line before it builds the transactions, so each
    entry in the packed row gets the LAST member header seen (the add-on),
    including the primary cardholder's entries. The add-on's last-4 (0002)
    is also dropped, because every row inherits the primary card number.
    """
    parsed = parse_fixture(BANK, "statement")

    assert {t.person for t in parsed.transactions} == {"SAMPLE ADDON"}
    assert {t.person for t in parsed.payments_refunds} == {"SAMPLE ADDON"}
    assert {t.card_number for t in parsed.transactions} == {"1111XXXXXXXX0001"}

    assert len(parsed.card_summaries) == 1
    summary = parsed.card_summaries[0]
    assert summary.person == "SAMPLE ADDON"
    assert summary.transaction_count == 5
    assert summary.total_amount == "4,700.00"
    assert summary.reward_points_total == "47"

    assert [g.person for g in parsed.person_groups] == ["SAMPLE ADDON"]


def test_statement_matches_golden() -> None:
    """Full output of the main case matches its golden dump."""
    assert_matches_golden(BANK, "statement", parse_fixture(BANK, "statement"))


def test_multipage_detects_bob_from_header() -> None:
    """The second fixture is also detected from its header."""
    assert detect_bank(load_raw(BANK, "multipage_credit_balance")) == BANK


def test_multipage_credit_balance_key_fields() -> None:
    """Fallback paths: column due date, CR total, word summary, page split."""
    parsed = parse_fixture(BANK, "multipage_credit_balance")

    assert parsed.name == "SAMPLE HOLDER"
    assert parsed.card_number == "2222XXXXXXXX1111"
    assert parsed.due_date == "05/02/2100"
    assert parsed.statement_total_amount_due == "-150.00"
    assert parsed.reward_points_balance == "62"

    # Page 1: two debits and one credit. Page 2: three debits.
    assert len(parsed.transactions) == 5
    assert len(parsed.payments_refunds) == 1
    assert [t.date for t in parsed.transactions] == [
        "02/01/2100",
        "08/01/2100",
        "12/01/2100",
        "15/01/2100",
        "18/01/2100",
    ]

    payment = parsed.payments_refunds[0]
    assert payment.date == "05/01/2100"
    assert payment.narration == "PAYMENT RECEIVED TESTREF0000000002"
    assert payment.amount == "3,000.00"
    assert payment.transaction_type == "credit"

    # Page-2 continuation: reward points dropped on count mismatch, and the
    # entry with no narration line becomes "UNKNOWN".
    page2 = parsed.transactions[2:]
    assert [t.reward_points for t in page2] == [None, None, None]
    assert page2[2].narration == "UNKNOWN"
    assert page2[2].amount == "150.00"
    assert page2[2].transaction_type == "debit"

    # Without a member header, person falls back to the cardholder name.
    assert {t.person for t in parsed.transactions} == {"SAMPLE HOLDER"}

    assert parsed.payments_refunds_total == "3,000.00"
    assert parsed.overall_total == "1,850.00"
    assert parsed.overall_reward_points == "11"

    rec = parsed.reconciliation
    assert rec.header_previous_balance == "1,000.00"
    assert rec.header_payments_credits_received == "3,000.00"
    assert rec.header_purchases_debit == "1,850.00"
    assert rec.header_computed_due_estimate == "-150.00"
    assert rec.smart_delta == "0.00"
    # Possible quirk, kept: the "150.00CR" closing balance loses its CR
    # sign in the word-line summary fallback.
    assert rec.summary_amount_candidates == [
        "1,000.00",
        "3,000.00",
        "1,850.00",
        "150.00",
    ]


def test_multipage_credit_balance_matches_golden() -> None:
    """Full output of the multi-page case matches its golden dump."""
    assert_matches_golden(
        BANK,
        "multipage_credit_balance",
        parse_fixture(BANK, "multipage_credit_balance"),
    )
