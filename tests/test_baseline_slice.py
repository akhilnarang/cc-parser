"""Offline baseline for the Slice parser.

Two synthetic fixtures under ``tests/fixtures/slice/`` pin the behaviour of
the standalone ``SliceParser`` before it is moved onto ``GenericParser``.

``statement``:
    Older template with a glued ``₹AMOUNT`` token. It has "slice" branding
    on the page-1 header and a single-token ``NAME's`` greeting, so the name
    comes from the line-level path. It covers the period header
    (``21 MAR - 20 APR``) and a ``Due on 5 May`` with no year. The page-1
    ``Statement summary`` rows are not section headers, so they do not
    start transaction parsing. Rows use the three-line layout (merchant +
    amount, avatar initial, ``DD Mon 'YY`` date with an optional mode).
    The Spends section runs from page 2 onto page 3 without a new header,
    and one amount has no decimals (``₹199``). An EMI row is restamped onto
    the period end. The Refunds & repayments credits include an exact
    refund of a debit, which forms an adjustment pair. A Cashback credit is
    kept out of pairing. A ``Glossary`` marker stops scanning, even though
    the prose after it has a ``Spends`` header and a dated ``₹`` row.

``title_only_orphan_rupee``:
    Newer template where ``₹`` renders a few units below the amount as its
    own visual line. Plain orphans are merged, and so is a ``₹ .50`` orphan
    that carries the paise. The page text has no bank branding and the
    filename has no bank token, so detection relies on the PDF ``/Title``.
    The name is two words, so it comes from the text-level fallback. The
    period runs 21 NOV - 20 DEC and the due date is ``Due on 5 Jan``, so
    the due year rolls over. The summary has no Surcharge row, and a
    ``GST details`` stop marker follows the glossary.
"""

from cc_parser.parsers.factory import detect_bank
from tests.fixture_support import assert_matches_golden, load_raw, parse_fixture

BANK = "slice"


def test_statement_detects_slice_from_header() -> None:
    """The branded fixture routes to the Slice parser."""
    assert detect_bank(load_raw(BANK, "statement")) == BANK


def test_statement_header_fields() -> None:
    """Name, card mask, due date and total due come from page 1."""
    parsed = parse_fixture(BANK, "statement")
    assert parsed.bank == BANK
    assert parsed.name == "TESTCARDHOLDER"
    assert parsed.card_number == "XXXX XXXX XXXX 0001"
    assert parsed.due_date == "05/05/2099"
    assert parsed.statement_total_amount_due == "2,999.00"


def test_statement_debits() -> None:
    """Spends across a page break plus a restamped EMI row are debits."""
    parsed = parse_fixture(BANK, "statement")
    debits = parsed.transactions
    assert len(debits) == 6
    assert all(t.transaction_type == "debit" for t in debits)
    assert [t.narration for t in debits] == [
        "SYNTHETIC GROCERY STORE",
        "EXAMPLE FUEL STATION",
        "DEMO ONLINE SHOP",
        "SAMPLE CAFE",
        "SAMPLE STREAMING SERVICE",
        "SAMPLE ELECTRONICS EMI 1 of 6",
    ]
    first = debits[0]
    assert (first.date, first.amount) == ("12/03/2099", "1,250.00")
    assert first.card_number == "XXXX XXXX XXXX 0001"
    assert first.person == "TESTCARDHOLDER"
    # Page-3 continuation row without decimals gets ".00" appended.
    streaming = debits[4]
    assert (streaming.date, streaming.amount) == ("10/04/2099", "199.00")
    # EMI row is billed on the period end; printed purchase date is kept.
    emi = debits[5]
    assert emi.date == "20/04/2099"
    assert emi.amount == "1,000.00"
    assert emi.credit_reasons == "emi_purchase_date:15/01/2099"
    # Glossary prose after the stop marker is never parsed.
    assert all("GLOSSARY" not in (t.narration or "") for t in debits)


def test_statement_credits_and_pairing() -> None:
    """Cashback and refunds are credits; only refunds are paired."""
    parsed = parse_fixture(BANK, "statement")
    credits = parsed.payments_refunds
    assert len(credits) == 3
    assert all(t.transaction_type == "credit" for t in credits)
    # Cashback credits are listed before refund credits.
    cashback, refund, repayment = credits
    assert (cashback.date, cashback.narration, cashback.amount) == (
        "20/04/2099",
        "CASHBACK ON SPENDS",
        "50.00",
    )
    assert cashback.credit_reasons == "section:cashback"
    assert (refund.date, refund.narration, refund.amount) == (
        "28/03/2099",
        "DEMO ONLINE SHOP REFUND",
        "1,500.00",
    )
    assert refund.credit_reasons == "section:refunds & repayments"
    assert (repayment.date, repayment.amount) == ("05/04/2099", "2,000.00")
    assert parsed.payments_refunds_total == "3,550.00"

    # Cashback and refund ID counters both restart at 0000; the hash
    # suffix keeps them distinct. Recorded as current behaviour.
    assert cashback.transaction_id.startswith("slice_txn_0000_")
    assert refund.transaction_id.startswith("slice_txn_0000_")
    assert cashback.transaction_id != refund.transaction_id

    pairs = parsed.possible_adjustment_pairs
    assert len(pairs) == 1
    assert pairs[0].kind == "exact_refund"
    assert pairs[0].debit_transaction_id == parsed.transactions[2].transaction_id
    assert pairs[0].credit_transaction_id == refund.transaction_id


def test_statement_totals_and_reconciliation() -> None:
    """Summary rows and parsed rows reconcile to the total due."""
    parsed = parse_fixture(BANK, "statement")
    assert parsed.overall_total == "6,549.00"
    assert parsed.overall_reward_points == "0"
    assert len(parsed.card_summaries) == 1
    summary = parsed.card_summaries[0]
    assert summary.card_number == "XXXX XXXX XXXX 0001"
    assert summary.person == "TESTCARDHOLDER"
    assert summary.transaction_count == 6
    assert [g.person for g in parsed.person_groups] == ["TESTCARDHOLDER"]

    recon = parsed.reconciliation
    assert recon.parsed_debit_total == "6,549.00"
    assert recon.parsed_credit_total == "3,550.00"
    # Spends + EMIs.
    assert recon.header_purchases_debit == "6,549.00"
    # Refunds & repayments + Cashback.
    assert recon.header_payments_credits_received == "3,550.00"
    assert recon.header_finance_charges == ""
    assert recon.delta_statement_vs_parsed_net == "0.00"
    assert recon.smart_delta == "0.00"


def test_statement_golden() -> None:
    """Full output matches the recorded baseline."""
    assert_matches_golden(BANK, "statement", parse_fixture(BANK, "statement"))


def test_title_only_detects_slice_from_pdf_title() -> None:
    """With no branding in the text or filename, ``/Title`` decides."""
    raw = load_raw(BANK, "title_only_orphan_rupee")
    assert "SLICE" not in raw["pages"][0]["text"].upper()
    assert "SLICE" not in raw["file"].upper()
    assert detect_bank(raw) == BANK


def test_title_only_header_fields() -> None:
    """Text-fallback name, orphan-rupee total and the due-year rollover."""
    parsed = parse_fixture(BANK, "title_only_orphan_rupee")
    assert parsed.name == "TEST CARDHOLDER"
    assert parsed.card_number == "XXXX XXXX XXXX 0002"
    # December period, due in January of the next year.
    assert parsed.due_date == "05/01/2100"
    assert parsed.statement_total_amount_due == "3,656.83"


def test_title_only_orphan_rupee_rows() -> None:
    """Orphan ``₹`` lines merge back onto amounts, keeping paise."""
    parsed = parse_fixture(BANK, "title_only_orphan_rupee")
    debits = parsed.transactions
    assert [(t.date, t.narration, t.amount) for t in debits] == [
        ("25/11/2099", "SYNTHETIC GROCERY STORE", "1,111.00"),
        ("01/12/2099", "EXAMPLE FUEL STATION", "2,222.50"),
        ("05/12/2099", "DEMO ONLINE SHOP", "333.33"),
        ("20/12/2099", "SAMPLE FURNITURE EMI 2 of 3", "1,000.00"),
    ]
    assert debits[3].credit_reasons == "emi_purchase_date:15/10/2099"

    credits = parsed.payments_refunds
    assert [(t.date, t.narration, t.amount, t.transaction_type) for t in credits] == [
        ("10/12/2099", "REPAYMENT TESTREF0000000002", "1,000.00", "credit"),
        ("12/12/2099", "EXAMPLE FUEL SURCHARGE WAIVER", "10.00", "credit"),
    ]
    assert parsed.payments_refunds_total == "1,010.00"
    assert parsed.overall_total == "4,666.83"


def test_title_only_reconciliation() -> None:
    """Records the summary bleed from page-2 rows into finance charges."""
    parsed = parse_fixture(BANK, "title_only_orphan_rupee")
    recon = parsed.reconciliation
    assert recon.parsed_debit_total == "4,666.83"
    assert recon.parsed_credit_total == "1,010.00"
    assert recon.header_purchases_debit == "4,666.83"
    assert recon.header_payments_credits_received == "1,010.00"
    assert recon.delta_statement_vs_parsed_net == "0.00"
    # Likely parser bug, kept as the baseline: the summary scan stops at
    # "Min amount due" only on page 1 and keeps scanning page 2. The
    # page-2 credit row "EXAMPLE FUEL SURCHARGE WAIVER ₹10" then fills the
    # missing Surcharge field. That makes finance charges 10.00 (should be
    # empty) and smart_delta -10.00 (should be 0.00).
    assert recon.header_finance_charges == "10.00"
    assert recon.smart_delta == "-10.00"
    assert recon.summary_amount_candidates == [
        "3,666.83",
        "1,010.00",
        "0.00",
        "10.00",
        "1,000.00",
    ]


def test_title_only_golden() -> None:
    """Full output matches the recorded baseline."""
    assert_matches_golden(
        BANK,
        "title_only_orphan_rupee",
        parse_fixture(BANK, "title_only_orphan_rupee"),
    )
