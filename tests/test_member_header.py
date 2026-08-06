"""Member/cardholder section-header detection.

HDFC prints an add-on holder's name as a section header above that holder's
transactions. A recently added holder also carries a CKYC id annotation on the
same line. The annotation must not defeat name detection, or the holder's
transactions fold into the previous holder.
"""

from cc_parser.parsers.cards import looks_like_member_header


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
