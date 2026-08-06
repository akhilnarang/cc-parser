"""Generic total-amount-due extraction regressions.

Shared by HDFC and ICICI (and Equitas via fallback). The summary box lists
PREVIOUS STATEMENT DUES right after the TOTAL AMOUNT DUE header, so the real
value must win even when it is negative.
"""

from cc_parser.parsers.summary.totals import extract_total_amount_due


def test_negative_total_due_is_not_shadowed_by_previous_dues():
    """A credit balance must win over the positive previous-dues figure.

    A paid-off card shows a small negative TOTAL AMOUNT DUE. It sits right
    before the summary row that starts with PREVIOUS STATEMENT DUES. A
    positive-only match skips the negative value and grabs the previous dues.
    """
    full_text = (
        "TOTAL AMOUNT DUE\n"
        "RECEIVED (Current Billing Cycle)\n"
        "_ C-12.34\n"
        "C9,999.00 C10,011.34 + C0.00 + C0.00 =\n"
        "TOTAL CREDIT LIMIT\n"
        "C5,00,000\n"
    )

    assert extract_total_amount_due(full_text) == "-12.34"


def test_positive_total_due_still_extracted():
    """A normal positive balance is unchanged by sign-aware matching."""
    full_text = (
        "TOTAL AMOUNT DUE\n"
        "RECEIVED (Current Billing Cycle)\n"
        "_ C7,777.00\n"
        "C1,234.00 C1,000.00 + C7,777.00 + C0.00 =\n"
        "TOTAL CREDIT LIMIT\n"
        "C5,00,000\n"
    )

    assert extract_total_amount_due(full_text) == "7,777.00"


def test_negative_total_due_without_currency_prefix():
    """The bare fallback also keeps the sign when no C prefix is present."""
    full_text = "TOTAL AMOUNT DUE\n-0.42\nTOTAL CREDIT LIMIT\n"

    assert extract_total_amount_due(full_text) == "-0.42"


def test_sign_does_not_attach_to_a_preceding_token():
    """A hyphen glued to a prior token must not flip the total-due sign.

    A trailing ``C`` on a token such as ``A/C-1,234.56`` must not read as a
    currency prefix on a negative amount and shadow the real value.
    """
    full_text = (
        "TOTAL AMOUNT DUE\n"
        "PAYMENT VIA A/C-1,234.56\n"
        "_ C-0.42\n"
        "TOTAL CREDIT LIMIT\n"
    )

    assert extract_total_amount_due(full_text) == "-0.42"
