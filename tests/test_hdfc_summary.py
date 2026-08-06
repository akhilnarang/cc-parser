"""HDFC summary-box (previous dues / payments / purchases / finance) mapping.

HDFC prints the summary as an equation row:

    C<prev> C<payments> + C<purchases> + C<finance> =

with the total amount due on the line above. The shared positional heuristic
mis-maps this layout, so HDFC parses the equation row directly.
"""

from cc_parser.parsers.hdfc import HdfcParser


def _hdfc_summary_text(prev, payments, purchases, finance, total_due):
    """Build a page-1 HDFC summary block with the given values."""
    return (
        "PAYMENTS/CREDITS PURCHASES/DEBIT\n"
        "PREVIOUS STATEMENT DUES FINANCE CHARGES TOTAL AMOUNT DUE\n"
        "RECEIVED (Current Billing Cycle)\n"
        f"_ C{total_due}\n"
        f"C{prev} C{payments} + C{purchases} + C{finance} =\n"
        "TOTAL CREDIT LIMIT\n"
        "C5,00,000\n"
    )


def test_equation_row_maps_prev_payments_purchases_finance_in_order():
    """Each field must come from its equation position, not amount order."""
    full_text = _hdfc_summary_text(
        prev="1,000.00",
        payments="1,200.00",
        purchases="300.00",
        finance="0.00",
        total_due="100.00",
    )

    summary = HdfcParser()._extract_summary(full_text, [])

    assert summary.previous_statement_dues == "1,000.00"
    assert summary.payments_credits_received == "1,200.00"
    assert summary.purchases_debit == "300.00"
    assert summary.finance_charges == "0.00"


def test_equation_row_survives_duplicate_zero_values():
    """A quiet month has purchases and finance both zero.

    The shared heuristic drops below its distinct-amount threshold when zeros
    repeat. The equation parse must still map every field.
    """
    full_text = _hdfc_summary_text(
        prev="500.00",
        payments="500.00",
        purchases="0.00",
        finance="0.00",
        total_due="0.00",
    )

    summary = HdfcParser()._extract_summary(full_text, [])

    assert summary.previous_statement_dues == "500.00"
    assert summary.payments_credits_received == "500.00"
    assert summary.purchases_debit == "0.00"
    assert summary.finance_charges == "0.00"


def test_negative_previous_dues_keeps_its_sign():
    """A prior credit balance carries a negative previous-dues value."""
    full_text = _hdfc_summary_text(
        prev="-0.55",
        payments="100.00",
        purchases="150.00",
        finance="0.00",
        total_due="49.45",
    )

    summary = HdfcParser()._extract_summary(full_text, [])

    assert summary.previous_statement_dues == "-0.55"
    assert summary.payments_credits_received == "100.00"
    assert summary.purchases_debit == "150.00"


def test_equation_search_ignores_other_page_illustration():
    """A later-page illustration must not overwrite the page-1 summary.

    Some layouts print the labels on page 1 without the equation row, while a
    tariff illustration later repeats the equation shape. That illustration
    must never become the summary.
    """
    page_one = (
        "PAYMENTS/CREDITS PURCHASES/DEBIT\n"
        "PREVIOUS STATEMENT DUES FINANCE CHARGES TOTAL AMOUNT DUE\n"
        "TOTAL CREDIT LIMIT\n"
        "C5,00,000\n"
    )
    decoy_page = "Illustration\nC900.00 C800.00 + C70.00 + C30.00 =\n"
    pages = [{"text": page_one}, {"text": decoy_page}]
    full_text = page_one + "\n" + decoy_page

    summary = HdfcParser()._extract_summary(full_text, pages)

    assert summary.previous_statement_dues != "900.00"


def test_missing_equation_row_falls_back_without_error():
    """A statement without the equation row must not crash the override."""
    summary = HdfcParser()._extract_summary("no summary block here\n", [])

    assert summary.previous_statement_dues is None
