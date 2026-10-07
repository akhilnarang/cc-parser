"""Member/cardholder section-header detection.

HDFC prints an add-on holder's name as a section header above that holder's
transactions. A recently added holder also carries a CKYC id annotation on the
same line. The annotation must not defeat name detection, or the holder's
transactions fold into the previous holder.
"""

from cc_parser.parsers.cards import (
    extract_card_from_line,
    find_card_candidates,
    looks_like_member_header,
)


def test_clean_member_header_is_detected():
    assert looks_like_member_header(["RAVI", "KUMAR"]) == "RAVI KUMAR"


def test_bracketed_ckyc_id_annotation_is_ignored():
    tokens = ["PRIYA", "SHARMA", "[CKYC", "ID", ":", "12345678901234", "]"]

    assert looks_like_member_header(tokens) == "PRIYA SHARMA"


def test_unbracketed_ckyc_annotation_is_ignored():
    tokens = ["PRIYA", "SHARMA", "CKYC", "ID", "12345678901234"]

    assert looks_like_member_header(tokens) == "PRIYA SHARMA"


def test_amount_or_date_line_is_not_a_member_header():
    assert looks_like_member_header(["01/07/2026", "500.00"]) is None


def test_card_mask_keeps_its_trailing_digits():
    # An older HDFC layout splits the mask into groups on one line, and puts
    # a lone digit on the next line.
    assert find_card_candidates("Card No: 1234 56XX XXXX 7890\n0 Address") == [
        "1234XXXXXXXX7890"
    ]
    assert (
        extract_card_from_line(["Card", "No:", "1234", "56XX", "XXXX", "7890"])[0]
        == "1234XXXXXXXX7890"
    )
    assert extract_card_from_line(["1234", "56XXXX", "7890"])[0] == "1234XXXXXX7890"


def test_pseudo_card_number_is_not_a_card():
    # ICICI prints a 0000-prefixed number above a reward-credit section.
    text = "0000XXXXXXXX1111\nMilestone Offer\n1234XXXXXXXX5678"
    assert find_card_candidates(text) == ["1234XXXXXXXX5678"]
    assert extract_card_from_line(["0000XXXXXXXX1111"]) == (None, None)
