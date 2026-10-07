"""Slice EMI instalment dating.

A Slice ``EMIs`` row prints the ORIGINAL purchase date, not the date of
the instalment being billed. That date repeats on every instalment, so
two instalments of one EMI carry the same date and, when the amounts also
match, become indistinguishable rows in the wrong month. The parser must
restamp an EMI row onto the statement period end and keep the printed
purchase date as provenance.
"""

from cc_parser.parsers.slice import (
    _extract_slice_due_date,
    _extract_slice_period_end,
    _extract_slice_transactions,
)


def _word(text: str, x0: float, top: float) -> dict:
    """Build a minimal pdfplumber-style word dict."""
    return {
        "text": text,
        "x0": x0,
        "x1": x0 + 30,
        "top": top,
        "doctop": top,
        "bottom": top + 9,
    }


def _period_words(start_day: str, start_mon: str, end_day: str, end_mon: str) -> list:
    """Page-1 statement period header, e.g. ``21 MAR - 20 APR``."""
    return [
        _word(start_day, 18.0, 124.5),
        _word(start_mon, 30.0, 124.5),
        _word("-", 50.0, 124.5),
        _word(end_day, 60.0, 124.5),
        _word(end_mon, 75.0, 124.5),
    ]


def _emi_section_words(top: float, amount: str, purchase_date: tuple) -> list:
    """An ``EMIs`` header plus one instalment row with its printed date."""
    day, mon, year = purchase_date
    return [
        _word("EMIs", 18.0, top),
        _word("Merchant", 57.0, top + 35),
        _word(amount, 205.0, top + 35),
        _word(day, 57.0, top + 54),
        _word(mon, 69.0, top + 54),
        _word(year, 88.0, top + 54),
    ]


def test_emi_row_is_dated_on_the_cycle_close_not_the_purchase():
    """Two instalments of one EMI print the same purchase date. Each must
    be restamped onto its own statement period end so they stop colliding,
    and the printed purchase date must survive as provenance."""
    # Two statements, consecutive cycles, same EMI, same printed date.
    april = [
        {
            "page_number": 1,
            "words": _period_words("21", "MAR", "20", "APR"),
        },
        {
            "page_number": 2,
            "words": _emi_section_words(100.0, "₹1,000.00", ("17", "Mar", "'26")),
        },
    ]
    may = [
        {
            "page_number": 1,
            "words": _period_words("21", "APR", "20", "MAY"),
        },
        {
            "page_number": 2,
            "words": _emi_section_words(100.0, "₹1,000.00", ("17", "Mar", "'26")),
        },
    ]

    april_txns, _ = _extract_slice_transactions(
        april, _extract_slice_period_end(april, "2026")
    )
    may_txns, _ = _extract_slice_transactions(
        may, _extract_slice_period_end(may, "2026")
    )

    assert len(april_txns) == 1
    assert len(may_txns) == 1

    # Each instalment is billed on its own cycle close.
    assert april_txns[0].date == "20/04/2026"
    assert may_txns[0].date == "20/05/2026"

    # The two instalments no longer collide on date, despite equal amounts.
    assert april_txns[0].amount == may_txns[0].amount
    assert april_txns[0].date != may_txns[0].date

    # The printed purchase date is kept, not discarded.
    assert april_txns[0].credit_reasons == "emi_purchase_date:17/03/2026"
    assert may_txns[0].credit_reasons == "emi_purchase_date:17/03/2026"


def test_spends_row_keeps_its_printed_date():
    """Only EMI rows are restamped. An ordinary Spends row prints the real
    transaction date and must pass through untouched."""
    pages = [
        {
            "page_number": 1,
            "words": _period_words("21", "MAR", "20", "APR"),
        },
        {
            "page_number": 2,
            "words": [
                _word("Spends", 18.0, 100.0),
                _word("Merchant", 57.0, 135.0),
                _word("₹350.00", 205.0, 135.0),
                _word("10", 57.0, 154.0),
                _word("Apr", 69.0, 154.0),
                _word("'26", 88.0, 154.0),
            ],
        },
    ]

    txns, _ = _extract_slice_transactions(
        pages, _extract_slice_period_end(pages, "2026")
    )

    assert len(txns) == 1
    assert txns[0].date == "10/04/2026"
    assert txns[0].credit_reasons is None


def test_due_date_after_a_december_period_is_in_the_next_year():
    """Slice prints the due date without a year. A December period is due
    in January, so the year must roll over."""
    text = "Due on 5 Jan\n15 Dec '25"
    assert _extract_slice_due_date(text, [], "20/12/2025") == "05/01/2026"
    text = "Due on 5 Dec\n15 Nov '25"
    assert _extract_slice_due_date(text, [], "20/11/2025") == "05/12/2025"
