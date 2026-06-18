"""IndusInd amount selection when a total column bleeds onto a row.

When a running/closing total lands within the line y-tolerance of the
transaction amount, the row holds two ``AMOUNT DR`` pairs. The charge is the
first (leftmost); the rightward one is the total and must not be picked.
"""

from cc_parser.parsers.indusind import _extract_indusind_transactions


def _word(text: str, x0: float, top: float = 480.0) -> dict:
    """Build a minimal pdfplumber-style word dict at a fixed baseline."""
    return {
        "text": text,
        "x0": x0,
        "x1": x0 + 30,
        "top": top,
        "doctop": top,
        "bottom": top + 8,
    }


def _member_header_words(top: float) -> list[dict]:
    """Member header that opens a purchases (debit) transaction section."""
    return [
        _word("Purchases", 22.5, top),
        _word("&", 70.0, top),
        _word("Cash", 80.0, top),
        _word("Transactions", 110.0, top),
        _word("for", 160.0, top),
        _word("MR", 175.0, top),
        _word("TEST", 190.0, top),
        _word("CARDHOLDER", 220.0, top),
        _word("(Credit", 260.0, top),
        _word("Card", 300.0, top),
        _word("No.", 330.0, top),
        _word("4000XXXXXXXX0002)", 360.0, top),
    ]


def test_total_column_bleed_does_not_steal_amount():
    """A closing-total ``AMOUNT DR`` bleeding rightward onto a transaction
    row must not be picked as the charge; the real charge sits in the
    consistent amount column to its left."""
    words = [
        *_member_header_words(top=400.0),
        # Transaction row with a closing total bleeding in to the right of
        # the real charge, both with DR markers.
        _word("06/06/2026", 22.5, 480.0),
        _word("RAZ*SWIGGY", 63.0, 480.0),
        _word("BANGALORE", 105.0, 480.0),
        _word("IND", 147.0, 480.0),
        _word("GROCERY", 252.0, 480.0),
        _word("&", 285.0, 480.0),
        _word("4", 335.0, 480.0),  # reward points
        _word("161.00", 405.0, 480.0),  # real charge amount
        _word("DR", 426.0, 480.0),
        _word("8,343.28", 487.0, 480.0),  # bled-in total — must be ignored
        _word("DR", 520.0, 480.0),
    ]
    pages = [{"page_number": 1, "words": words}]

    transactions, _ = _extract_indusind_transactions(pages)

    assert len(transactions) == 1
    txn = transactions[0]
    assert txn.amount == "161.00"
    assert txn.reward_points == "4"
    # Merchant category ("GROCERY &") stays in the narration, as on sibling
    # rows; what matters is the bled-in total no longer leaks into it.
    assert txn.narration == "RAZ*SWIGGY BANGALORE IND GROCERY &"
    assert "8,343.28" not in txn.narration
    assert "161.00" not in txn.narration
    assert txn.transaction_type == "debit"


def test_single_amount_row_unaffected():
    """A normal row with one amount column parses exactly as before."""
    words = [
        *_member_header_words(top=400.0),
        _word("23/05/2026", 22.5, 480.0),
        _word("ZEPTO", 63.0, 480.0),
        _word("MARKETPLACE", 105.0, 480.0),
        _word("PRI", 160.0, 480.0),
        _word("BANGALORE", 200.0, 480.0),
        _word("KAR", 240.0, 480.0),
        _word("GROCERY", 252.0, 480.0),
        _word("&", 300.0, 480.0),
        _word("58", 360.0, 480.0),  # reward points
        _word("2,313.00", 400.0, 480.0),  # only amount
        _word("DR", 440.0, 480.0),
    ]
    pages = [{"page_number": 1, "words": words}]

    transactions, _ = _extract_indusind_transactions(pages)

    assert len(transactions) == 1
    txn = transactions[0]
    assert txn.amount == "2,313.00"
    assert txn.reward_points == "58"
    assert txn.transaction_type == "debit"


def test_merged_marker_amount_with_total_bleed():
    """When the marker is merged into the amount ("161.00DR"), the first such
    token is the charge and a bled total ("8,343.28DR") to its right is ignored."""
    words = [
        *_member_header_words(top=400.0),
        _word("06/06/2026", 22.5, 480.0),
        _word("RAZ*SWIGGY", 63.0, 480.0),
        _word("BANGALORE", 105.0, 480.0),
        _word("IND", 147.0, 480.0),
        _word("4", 335.0, 480.0),  # reward points
        _word("161.00DR", 405.0, 480.0),  # real charge, marker merged in
        _word("8,343.28DR", 487.0, 480.0),  # bled total — must be ignored
    ]
    pages = [{"page_number": 1, "words": words}]

    transactions, _ = _extract_indusind_transactions(pages)

    assert len(transactions) == 1
    txn = transactions[0]
    assert txn.amount == "161.00"
    assert txn.reward_points == "4"
    assert txn.transaction_type == "debit"
