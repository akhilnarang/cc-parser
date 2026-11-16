"""Offline baseline for the YES BANK parser.

Purely synthetic fixtures under ``tests/fixtures/yesbank/`` pin the current
``YesbankParser`` output so the planned move onto ``GenericParser`` (see
``TODO-parser-cleanup.md``) can be checked for behaviour drift.

``statement`` (two pages) covers:

- detection from the page-1 ``YES BANK`` header,
- the text-level ``Label: value`` paths for due date, total amount due and
  the account summary (previous balance / purchases / payments & credits),
- card number from ``Statement for YES BANK Card Number ...``,
- name reconstruction from single letters interleaved with small-font
  digits in the address block, deduplicated against the full-word repeat,
- ``Statement Details`` .. ``End of the Statement`` section bounds, with a
  dated line after the footer that must not become a transaction,
- merchant-category column stripping (``GROCERY STORES``, ``FUEL``, ...),
- ``Dr``/``Cr`` classification, a same-amount reversal adjustment pair,
- summary-row rejection of repeated page-2 header lines inside the section,
- an in-section ``Card Number`` line switching later rows to an add-on card,
- a row whose narration sits on the line above (empty-narration merge).

``word_level_fallbacks`` covers labels printed without colons, so the
word-level fallbacks run for card number, due date, total amount due and
summary fields; the name comes from the registered e-mail fallback; and a
payment row whose ``(Ref#`` wraps onto the next line.
"""

from cc_parser.parsers.factory import detect_bank
from cc_parser.parsers.models import Transaction
from tests.fixture_support import assert_matches_golden, load_raw, parse_fixture

BANK = "yesbank"


def _row(rows: list[Transaction], date: str) -> Transaction:
    """Return the single row with the given date.

    Args:
        rows: Parsed transactions.
        date: Date in ``DD/MM/YYYY``.

    Returns:
        The matching transaction.
    """
    matches = [row for row in rows if row.date == date]
    assert len(matches) == 1, matches
    return matches[0]


def test_detects_yesbank_from_header() -> None:
    """Both fixtures route to the YES BANK parser."""
    for case in ("statement", "word_level_fallbacks"):
        assert detect_bank(load_raw(BANK, case)) == BANK


def test_statement_header_fields() -> None:
    """Text-level header and summary extraction."""
    parsed = parse_fixture(BANK, "statement")
    assert parsed.bank == BANK
    assert parsed.name == "TEST CARDHOLDER"
    assert parsed.card_number == "0000 XXXX XXXX 0001"
    assert parsed.due_date == "20/02/2099"
    assert parsed.statement_total_amount_due == "5,461.11"
    assert parsed.overall_reward_points == "0"


def test_statement_transactions() -> None:
    """Debit/credit split, category stripping and section bounds."""
    parsed = parse_fixture(BANK, "statement")
    debits = parsed.transactions
    credits = parsed.payments_refunds
    assert len(debits) == 7
    assert len(credits) == 2

    grocery = _row(debits, "02/01/2099")
    assert grocery.narration == "SYNTHETIC GROCERY STORE SAMPLE CITY"
    assert grocery.amount == "1,111.11"
    assert grocery.transaction_type == "debit"
    assert grocery.card_number == "0000 XXXX XXXX 0001"

    shop = _row(debits, "10/01/2099")
    assert shop.narration == "DEMO ONLINE SHOP"
    assert shop.amount == "2,500.00"

    payment = _row(credits, "08/01/2099")
    assert payment.narration == "PAYMENT RECEIVED NEFT TESTREF0000000001"
    assert payment.amount == "2,000.00"
    assert payment.transaction_type == "credit"
    assert payment.credit_reasons == "cr_marker"

    reversal = _row(credits, "12/01/2099")
    assert reversal.narration == "SAMPLE RESTAURANT REVERSAL"
    assert reversal.amount == "300.00"

    # Rows after an in-section "Card Number" line belong to the add-on card.
    pharmacy = _row(debits, "18/01/2099")
    assert pharmacy.card_number == "0000 XXXX XXXX 0002"
    assert pharmacy.narration == "SAMPLE PHARMACY"
    # Current behaviour: add-on rows inherit the primary name rather than an
    # "ADDON <last4>" label.
    assert pharmacy.person == "TEST CARDHOLDER"

    # Current behaviour (likely bug): the merchant printed on the line above
    # is dropped (it looks like a member header), the category is kept, and
    # the following add-on "Card Number" header is absorbed into narration.
    wrapped = _row(debits, "15/01/2099")
    assert (
        wrapped.narration == "GROCERY STORES SAMPLE ADDON Card Number 0000XXXXXXXX0002"
    )
    assert wrapped.amount == "400.00"

    # Current behaviour (likely bug): a real row containing "CASH ADVANCE"
    # is skipped by the summary-keyword filter and is not even recorded in
    # rejected_date_lines.
    assert all(row.date != "20/01/2099" for row in debits)
    # The dated line after "End of the Statement" stays out.
    assert all(row.date != "01/03/2099" for row in [*debits, *credits])

    assert len(parsed.possible_adjustment_pairs) == 1
    assert parsed.possible_adjustment_pairs[0].kind == "reversal"


def test_statement_totals_and_reconciliation() -> None:
    """Totals, per-card summaries and reconciliation numbers."""
    parsed = parse_fixture(BANK, "statement")
    assert parsed.overall_total == "5,761.11"
    assert parsed.payments_refunds_total == "2,300.00"
    assert [
        (s.card_number, s.transaction_count, s.total_amount)
        for s in parsed.card_summaries
    ] == [
        ("0000 XXXX XXXX 0001", 5, "4,811.11"),
        ("0000 XXXX XXXX 0002", 2, "950.00"),
    ]
    assert [g.person for g in parsed.person_groups] == ["TEST CARDHOLDER"]

    recon = parsed.reconciliation
    assert recon.header_previous_balance == "1,000.00"
    assert recon.header_purchases_debit == "6,761.11"
    assert recon.header_payments_credits_received == "2,300.00"
    assert recon.header_computed_due_estimate == "5,461.11"
    assert recon.delta_statement_vs_header_estimate == "0.00"
    # The 1,000.00 gap is the dropped CASH ADVANCE row.
    assert recon.smart_delta == "1,000.00"


def test_statement_golden() -> None:
    """Full-output golden for the main case."""
    assert_matches_golden(BANK, "statement", parse_fixture(BANK, "statement"))


def test_word_level_fallbacks() -> None:
    """Colon-less labels exercise the word-level fallbacks."""
    parsed = parse_fixture(BANK, "word_level_fallbacks")
    assert parsed.name == "SAMPLE CARDHOLDER"
    assert parsed.card_number == "0000 XXXX XXXX 1111"
    assert parsed.due_date == "20/02/2099"
    assert parsed.statement_total_amount_due == "1,000.00"
    assert len(parsed.transactions) == 2
    assert len(parsed.payments_refunds) == 1
    assert parsed.overall_total == "1,500.00"
    assert parsed.payments_refunds_total == "1,000.00"

    payment = _row(parsed.payments_refunds, "06/01/2099")
    assert payment.narration == "PAYMENT RECEIVED UPI (Ref# TESTREF0000000002)"
    assert payment.transaction_type == "credit"

    fuel = _row(parsed.transactions, "09/01/2099")
    assert fuel.narration == "EXAMPLE FUEL STATION"
    assert fuel.amount == "500.00"
    assert fuel.card_number == "0000 XXXX XXXX 1111"

    recon = parsed.reconciliation
    # Current behaviour (likely bug): the word-level previous-balance fallback
    # takes its Dr/Cr sign from the next line too, so a "500.00 Dr" balance
    # followed by the "Payment & Credits Received ... Cr" line becomes negative.
    assert recon.header_previous_balance == "-500.00"
    assert recon.header_purchases_debit == "1,500.00"
    assert recon.header_payments_credits_received == "1,000.00"
    assert recon.header_computed_due_estimate == "0.00"


def test_word_level_fallbacks_golden() -> None:
    """Full-output golden for the word-level fallback case."""
    assert_matches_golden(
        BANK, "word_level_fallbacks", parse_fixture(BANK, "word_level_fallbacks")
    )
