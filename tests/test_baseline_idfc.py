"""Offline baseline tests for the IDFC FIRST Bank parser.

Every fixture under ``tests/fixtures/idfc/`` is synthetic. The three cases
pin the current standalone ``IdfcParser`` pipeline ahead of its planned move
onto ``GenericParser`` (see ``TODO-parser-cleanup.md``):

- ``statement``: "Wealth"-style layout. Three-token ``DD Mon YY`` dates,
  ``r``-prefixed amounts, trailing ``DR``/``CR`` markers, line-by-line summary
  (Opening Balance / Purchases / Payments & Refunds), ``Total Amount Due``
  header with the value plus a ``DD/Mon/YYYY`` due date on the next line,
  ``Card Number: XXXX 0001`` masks with a second card block mid-section, an
  EMI ``Convert`` column cut from the narration, a wrapped ``(Ref#`` row merged
  from the next line, a stray ``+`` token, a ``CR`` refund inside the debit
  section, an unmarked row in the credits section, an exact refund pair, and
  a page-1 statement-period line that is seen as a date line but rejected.
- ``mayura``: columnar layout. Single-token ``DD/MM/YYYY`` dates, six-column
  ``Opening + Purchases + Other Debits - Payments - Other Credits = Total``
  summary, ``Minimum Amount Due | Total Amount Due | Payment Due Date`` header
  resolved by x-position, numeric due date, and the name text fallback (the
  line before ``Statement Summary`` is not the name).
- ``credit_balance``: credit-balance statement. ``(FIRST Wealth XX0002)`` card
  fallback, ``CR(cid:9)`` artifact on the opening balance, inline
  ``Total Amount Due = r300.00 CR`` regex fallback giving a negative total,
  and a partial refund pair.
"""

from cc_parser.parsers.factory import detect_bank, get_parser
from cc_parser.parsers.models import ParsedStatement, Transaction
from tests.fixture_support import assert_matches_golden, load_raw, parse_fixture

BANK = "idfc"


def _row(rows: list[Transaction], narration: str) -> Transaction:
    """Return the single row whose narration matches exactly.

    Args:
        rows: Transactions to search.
        narration: Exact narration text.

    Returns:
        The matching transaction.
    """
    matches = [row for row in rows if row.narration == narration]
    assert len(matches) == 1, f"expected one row {narration!r}, got {len(matches)}"
    return matches[0]


def _parse(case: str) -> ParsedStatement:
    """Parse an IDFC fixture with the explicit IDFC parser.

    Args:
        case: Fixture case name.

    Returns:
        Parsed statement model.
    """
    return parse_fixture(BANK, case)


def test_detects_idfc_from_header() -> None:
    for case in ("statement", "mayura", "credit_balance"):
        raw = load_raw(BANK, case)
        assert detect_bank(raw) == BANK
        # Header branding alone is enough: the filename carries no bank token.
        raw["file"] = "attachment.pdf"
        raw["metadata"] = {}
        assert detect_bank(raw) == BANK


def test_statement_header_fields() -> None:
    parsed = _parse("statement")
    assert parsed.bank == BANK
    assert parsed.file == "synthetic_idfc_statement.pdf"
    assert parsed.name == "TEST CARDHOLDER"
    assert parsed.card_number == "XXXX XXXX XXXX 0001"
    assert parsed.due_date == "05/02/2099"
    assert parsed.statement_total_amount_due == "1,311.11"
    assert parsed.overall_reward_points == "0"


def test_statement_transactions() -> None:
    parsed = _parse("statement")
    assert len(parsed.transactions) == 5
    assert len(parsed.payments_refunds) == 3

    grocery = _row(parsed.transactions, "SYNTHETIC GROCERY STORE SAMPLE CITY")
    assert (grocery.date, grocery.amount, grocery.transaction_type) == (
        "05/01/2099",
        "1,111.11",
        "debit",
    )
    assert grocery.card_number == "XXXX XXXX XXXX 0001"
    assert grocery.person == "TEST CARDHOLDER"

    # "Convert to EMI" column is cut from the narration.
    emi = _row(parsed.transactions, "DEMO ONLINE SHOP")
    assert (emi.date, emi.amount) == ("08/01/2099", "2,000.00")

    # Wrapped "(Ref#" narration is completed from the following line.
    fuel = _row(parsed.transactions, "EXAMPLE FUEL STATION (Ref# TESTREF0000000002)")
    assert fuel.amount == "500.00"

    # No DR marker and a leading "+" token: debit by section context.
    restaurant = _row(parsed.transactions, "SAMPLE RESTAURANT")
    assert (restaurant.amount, restaurant.transaction_type) == ("150.00", "debit")

    second_card = _row(parsed.transactions, "SAMPLE BOOK STORE")
    assert second_card.card_number == "XXXX XXXX XXXX 0002"
    assert second_card.person == "TEST CARDHOLDER"

    # CR marker wins even inside the debits section.
    refund = _row(parsed.payments_refunds, "DEMO ONLINE SHOP REFUND")
    assert (refund.date, refund.amount, refund.transaction_type) == (
        "18/01/2099",
        "2,000.00",
        "credit",
    )
    assert refund.credit_reasons == "cr_marker"

    # Current behaviour: card context carries over section headers, so the
    # payment rows listed after the second card block are tagged with card
    # 0002 rather than the primary card.
    payment = _row(parsed.payments_refunds, "PAYMENT RECEIVED TESTREF0000000001")
    assert (payment.date, payment.amount) == ("15/01/2099", "1,500.00")
    assert payment.card_number == "XXXX XXXX XXXX 0002"

    cashback = _row(parsed.payments_refunds, "CASHBACK CREDIT")
    assert (cashback.amount, cashback.transaction_type) == ("200.00", "credit")
    assert cashback.credit_reasons == "credits_section"

    assert parsed.payments_refunds_total == "3,700.00"
    assert parsed.overall_total == "4,011.11"


def test_statement_groups_and_reconciliation() -> None:
    parsed = _parse("statement")
    assert [
        (s.card_number, s.person, s.transaction_count, s.total_amount)
        for s in parsed.card_summaries
    ] == [
        ("XXXX XXXX XXXX 0001", "TEST CARDHOLDER", 4, "3,761.11"),
        ("XXXX XXXX XXXX 0002", "TEST CARDHOLDER", 1, "250.00"),
    ]
    assert [(g.person, g.transaction_count) for g in parsed.person_groups] == [
        ("TEST CARDHOLDER", 5)
    ]

    pairs = parsed.possible_adjustment_pairs
    assert [(p.kind, p.confidence) for p in pairs] == [("exact_refund", "high")]

    rec = parsed.reconciliation
    assert rec.header_previous_balance == "1,000.00"
    assert rec.header_purchases_debit == "4,011.11"
    assert rec.header_payments_credits_received == "3,700.00"
    assert rec.header_computed_due_estimate == "1,311.11"
    assert rec.parsed_debit_total == "4,011.11"
    assert rec.parsed_credit_total == "3,700.00"
    assert rec.smart_expected_total == "1,311.11"
    assert rec.smart_delta == "0.00"
    assert rec.summary_amount_candidates == ["1,000.00", "4,011.11", "3,700.00"]


def test_statement_debug_rejects_period_line() -> None:
    raw = load_raw(BANK, "statement")
    parser = get_parser(BANK, raw)
    parser.parse(raw)
    debug = parser.build_debug(raw)
    assert debug["stats"]["transactions_parsed"] == 8
    assert debug["stats"]["date_lines_seen"] == 9
    rejected = debug["rejected_date_lines"]
    assert [(r["page"], r["reason"]) for r in rejected] == [(1, "amount_not_found")]


def test_mayura_columnar_layout() -> None:
    parsed = _parse("mayura")
    assert parsed.name == "DEMO CARDHOLDER"
    assert parsed.card_number == "XXXX XXXX XXXX 1111"
    assert parsed.due_date == "10/02/2099"
    # Picked by x-position under "Total", not the minimum due.
    assert parsed.statement_total_amount_due == "2,700.00"

    assert len(parsed.transactions) == 3
    assert len(parsed.payments_refunds) == 1
    fee = _row(parsed.transactions, "ANNUAL FEE")
    assert (fee.date, fee.amount, fee.transaction_type) == (
        "09/01/2099",
        "100.00",
        "debit",
    )
    payment = _row(parsed.payments_refunds, "PAYMENT RECEIVED TESTREF0000000003")
    assert (payment.date, payment.amount) == ("15/01/2099", "1,000.00")
    assert payment.credit_reasons == "credits_section"
    assert parsed.overall_total == "2,700.00"
    assert parsed.payments_refunds_total == "1,000.00"

    rec = parsed.reconciliation
    assert rec.summary_amount_candidates == [
        "1,000.00",
        "2,600.00",
        "100.00",
        "1,000.00",
        "0.00",
        "2,700.00",
    ]
    assert rec.header_previous_balance == "1,000.00"
    assert rec.header_purchases_debit == "2,600.00"
    assert rec.header_payments_credits_received == "1,000.00"
    # Current behaviour: "Other Debits" is deliberately not mapped to
    # finance charges, so the header estimate misses it by 100.00 while the
    # smart estimate (from parsed rows) still reconciles.
    assert rec.header_finance_charges == ""
    assert rec.header_computed_due_estimate == "2,600.00"
    assert rec.delta_statement_vs_header_estimate == "100.00"
    assert rec.smart_delta == "0.00"


def test_credit_balance_statement() -> None:
    parsed = _parse("credit_balance")
    assert parsed.name == "SAMPLE CARDHOLDER"
    assert parsed.card_number == "XXXX XXXX XXXX 0002"
    assert parsed.due_date == "25/02/2099"
    assert parsed.statement_total_amount_due == "-300.00"

    assert len(parsed.transactions) == 2
    assert all(t.card_number == "XXXX XXXX XXXX 0002" for t in parsed.transactions)
    refund = _row(parsed.payments_refunds, "DEMO TRAVEL DESK REFUND")
    assert (refund.date, refund.amount, refund.credit_reasons) == (
        "12/02/2099",
        "100.00",
        "cr_marker",
    )
    assert parsed.overall_total == "300.00"
    assert parsed.payments_refunds_total == "100.00"
    assert [p.kind for p in parsed.possible_adjustment_pairs] == ["possible_refund"]

    rec = parsed.reconciliation
    assert rec.header_previous_balance == "-500.00"
    assert rec.header_computed_due_estimate == "-300.00"
    assert rec.smart_expected_total == "-300.00"
    assert rec.smart_delta == "0.00"


def test_matches_golden() -> None:
    for case in ("statement", "mayura", "credit_balance"):
        assert_matches_golden(BANK, case, _parse(case))
