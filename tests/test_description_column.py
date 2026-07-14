"""Description-column furniture removal.

Some templates render an inline action control (a "convert to instalment"
button, a promo tag) as a text run on the transaction's own baseline, in a
lane between the time column and the description column. Line grouping is
column-blind, so the control's word lands at the head of the narration.

The column is read off the page's own table header, not guessed from the rows:
a page can be sparse enough that the control's lane holds more rows than the
description's, and a description can legitimately open with a short word that
row statistics would then mistake for a control.

These fixtures are fabricated word boxes: coordinates, merchants and amounts
are invented, only the column *shape* is reproduced.
"""

from typing import Any

from cc_parser.parsers.extraction import (
    derive_description_column_x0,
    extract_transactions,
    group_words_into_lines,
)
from cc_parser.parsers.generic import GenericParser
from cc_parser.parsers.hdfc import HdfcParser
from cc_parser.parsers.icici import IciciParser


def _row(doctop: float, cells: list[tuple[float, str]]) -> list[dict[str, Any]]:
    """Build word boxes for one visual line from ``(x0, text)`` cells."""
    return [
        {"text": text, "x0": x0, "x1": x0 + 6.0 * len(text), "doctop": doctop}
        for x0, text in cells
    ]


def _page(rows: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = [word for row in rows for word in row]
    return [{"page_number": 1, "words": words, "text": ""}]


# --- Template shape A: date | time | action lane | description ---------------
# Columns: date 100.0, time 140.0, action lane 170.0, description 200.0,
# amount 400.0.
A_DATE_X = 100.0
A_TIME_X = 140.0
A_ACTION_X = 170.0
A_DESC_X = 200.0
A_AMOUNT_X = 400.0


def _header(
    doctop: float,
    date_x: float,
    desc_x: float,
    amount_x: float,
    with_time: bool = True,
) -> list[dict[str, Any]]:
    """The transaction table's column header, as the template prints it.

    Labels sit in the columns they name, separated by column-sized gaps.
    """
    cells = [(date_x, "DATE")]
    if with_time:
        cells.append((date_x + 30.0, "TIME"))
    cells += [
        (desc_x, "TRANSACTION"),
        (desc_x + 70.0, "DESCRIPTION"),
        (amount_x, "AMOUNT"),
    ]
    return _row(doctop, cells)


def _template_a_header(doctop: float = 60.0) -> list[dict[str, Any]]:
    return _header(doctop, A_DATE_X, A_DESC_X, A_AMOUNT_X)


def _template_a_plain(
    doctop: float, merchant: str, amount: str
) -> list[dict[str, Any]]:
    cells = [(A_DATE_X, "05/02/2026"), (A_TIME_X, "10:15")]
    x = A_DESC_X
    for word in merchant.split():
        cells.append((x, word))
        x += 6.0 * len(word) + 4.0
    cells.append((A_AMOUNT_X, amount))
    return _row(doctop, cells)


def _template_a_with_button(
    doctop: float,
    button: str,
    merchant: str,
    amount: str,
    x0: float = A_ACTION_X,
) -> list[dict[str, Any]]:
    row = _template_a_plain(doctop, merchant, amount)
    button_box = {
        "text": button,
        "x0": x0,
        "x1": x0 + 6.0 * len(button),
        "doctop": doctop,
    }
    return [row[0], row[1], button_box, *row[2:]]


def _template_a_page(extra_rows: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Page with a column header, three plain rows, plus extras."""
    rows = [
        _template_a_header(),
        _template_a_plain(100.0, "ZEPHYR BAKEHOUSE", "1,200.00"),
        _template_a_plain(120.0, "ORBIT GROCERS", "845.50"),
        _template_a_plain(140.0, "TIDEWATER BOOKS", "310.00"),
        *extra_rows,
    ]
    return _page(rows)


# --- Template shape B: date | description (far left) -------------------------
# A different layout of the same issuer: no action lane, and the description
# column begins much further left, at 60.0 — left of Template A's action lane.
B_DATE_X = 20.0
B_DESC_X = 60.0
B_AMOUNT_X = 400.0


def _template_b_row(
    doctop: float, words: list[str], amount: str
) -> list[dict[str, Any]]:
    cells = [(B_DATE_X, "05/02/2026")]
    x = B_DESC_X
    for word in words:
        cells.append((x, word))
        x += 6.0 * len(word) + 4.0
    cells.append((B_AMOUNT_X, amount))
    return _row(doctop, cells)


def _template_b_header(doctop: float = 40.0) -> list[dict[str, Any]]:
    return _header(doctop, B_DATE_X, B_DESC_X, B_AMOUNT_X, with_time=False)


def _template_b_page(rows: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return _page([_template_b_header(), *rows])


def _narrations(pages: list[dict[str, Any]]) -> list[str]:
    """Narrations as a profile that opts into action-lane removal sees them."""
    return [
        txn.narration for txn in extract_transactions(pages, strip_action_lane=True)
    ]


def test_column_read_from_the_page_header_not_hardcoded() -> None:
    """The column left edge comes from the page's own header row."""
    lines_a = group_words_into_lines(_template_a_page([])[0]["words"])
    assert derive_description_column_x0(lines_a) == A_DESC_X

    page_b = _template_b_page(
        [
            _template_b_row(100.0, ["FINANCE", "charge"], "220.00"),
            _template_b_row(120.0, ["MEMBERSHIP", "renewal"], "999.00"),
        ]
    )
    lines_b = group_words_into_lines(page_b[0]["words"])
    assert derive_description_column_x0(lines_b) == B_DESC_X


def test_action_button_left_of_description_column_is_dropped() -> None:
    """Artifact row: the button token sits in its own lane, not in narration."""
    pages = _template_a_page(
        [_template_a_with_button(160.0, "EMI", "QUILL COFFEE HOUSE", "455.00")]
    )
    assert _narrations(pages)[-1] == "QUILL COFFEE HOUSE"


def test_button_rows_outnumbering_plain_rows_are_still_stripped() -> None:
    """A sparse page whose action lane holds more rows than the description.

    Continuation pages can carry only a handful of rows, and most of them can
    bear the action control. The control's lane is then the *busiest* lane on
    the page, so a column derived from row frequency lands on the control and
    stripping quietly does nothing. The header says which column is which
    however few rows sit under it.
    """
    pages = _page(
        [
            _template_a_header(),
            _template_a_with_button(100.0, "EMI", "CINDER TEAHOUSE", "410.00"),
            _template_a_with_button(120.0, "EMI", "MARLOWE OPTICIANS", "2,250.00"),
            _template_a_plain(140.0, "PENNANT HARDWARE", "780.00"),
        ]
    )
    assert _narrations(pages) == [
        "CINDER TEAHOUSE",
        "MARLOWE OPTICIANS",
        "PENNANT HARDWARE",
    ]


def test_lone_button_row_on_a_page_is_stripped() -> None:
    """One row on the page, and it carries the control: nothing to compare to.

    With no plain row anywhere on the page, the description column has no rows
    of its own to be derived from. The header still has it.
    """
    pages = _page(
        [
            _template_a_header(),
            _template_a_with_button(100.0, "EMI", "SALTFLOWER BAKERY", "615.00"),
        ]
    )
    assert _narrations(pages) == ["SALTFLOWER BAKERY"]


def test_description_opening_with_a_short_word_survives() -> None:
    """False-positive guard: a description whose own first word is short.

    Rows whose narration opens with a token carrying no letters — a rebate's
    rate, say — hide that token from any rule that looks for the first *word*
    of a row, which then reports the second one. Where such rows are the
    majority, the busiest left edge on the page is a lane one word right of the
    description column, and a rule that trusts it treats every row's real first
    word as an action control and eats it: the rebate loses its rate, and the
    merchant beside it loses the prefix its name begins with.

    Every row here starts at the description column. Position against the
    header says so; row frequency says otherwise.
    """
    pages = _page(
        [
            _template_a_header(),
            _template_a_plain(100.0, "WWW BRAMBLE MARKET", "1,150.00"),
            _template_a_plain(120.0, "4% CINDER REBATE", "460.00"),
            _template_a_plain(140.0, "4% CINDER REBATE", "205.00"),
            _template_a_plain(160.0, "4% CINDER REBATE", "330.00"),
        ]
    )
    assert _narrations(pages) == [
        "WWW BRAMBLE MARKET",
        "4% CINDER REBATE",
        "4% CINDER REBATE",
        "4% CINDER REBATE",
    ]


def _assert_not_a_header(cells: list[tuple[float, str]], doctop: float = 30.0) -> None:
    """Assert a line shaped like *cells* cannot anchor the description column.

    The page carries no real header, so if the line were accepted it would be
    the only anchor and the column would land where it says — deleting the
    action-lane word of the row below, and with it the first word of any row
    whose description starts there. Nothing may be stripped.

    Args:
        cells: Word boxes of the header-like line.
        doctop: Where to put it. Above the rows unless a case needs otherwise.
    """
    pages = _page(
        [
            _row(doctop, cells),
            _template_a_plain(100.0, "ZEPHYR BAKEHOUSE", "1,200.00"),
            _template_a_with_button(120.0, "FOREIGN", "CEDAR MARKET", "845.50"),
        ]
    )
    lines = group_words_into_lines(pages[0]["words"])
    assert derive_description_column_x0(lines) is None
    assert _narrations(pages) == ["ZEPHYR BAKEHOUSE", "FOREIGN CEDAR MARKET"]


def test_prose_naming_the_columns_is_not_a_header() -> None:
    """A sentence that names the columns is not a table header.

    The words a header contains, a sentence about the statement can contain
    too. Accepting one anchors the description column on whatever coordinate
    the sentence happens to start a word at, and every row word left of that is
    then deleted — the failure mode is silent destruction of real narration.
    So the test for a header is its geometry, and the guards below take that
    sentence apart one property at a time.
    """
    _assert_not_a_header(
        [
            (20.0, "Please"),
            (55.0, "verify"),
            (90.0, "DATE"),
            (120.0, "TRANSACTION"),
            (190.0, "DESCRIPTION"),
            (260.0, "and"),
            (280.0, "AMOUNT"),
            (330.0, "fields"),
        ]
    )


def test_a_header_opens_with_its_date_label() -> None:
    """Words in front of the date label mean the line is not the header row.

    Every other property here is that of a header — the date label stands on
    the date column, the cells are a column apart — but a table's first column
    is where its header begins.
    """
    _assert_not_a_header(
        [
            (40.0, "NOTE"),
            (A_DATE_X, "DATE"),
            (A_DESC_X, "TRANSACTION"),
            (A_DESC_X + 70.0, "DESCRIPTION"),
            (A_AMOUNT_X, "AMOUNT"),
        ]
    )


def test_a_headers_date_label_stands_over_the_date_column() -> None:
    """A header names the columns of the rows beneath it, or it names nothing.

    This line opens with its date label and spaces its cells like a table, but
    the label floats free of the column the rows put their dates in — so it is
    not the header of *this* table, and its idea of where the description
    begins is not evidence about these rows.
    """
    _assert_not_a_header(
        [
            (A_DATE_X - 60.0, "DATE"),
            (A_DESC_X, "TRANSACTION"),
            (A_DESC_X + 70.0, "DESCRIPTION"),
            (A_AMOUNT_X, "AMOUNT"),
        ]
    )


def test_a_headers_cells_are_a_column_apart_not_a_space_apart() -> None:
    """Cells of a table are a column apart; words of a sentence are not.

    The date label opens the line and stands on the date column, but the
    description cell follows its neighbour by a word space. That is a phrase,
    not a row of cells.
    """
    _assert_not_a_header(
        [
            (A_DATE_X, "DATE"),
            (170.0, "TIME"),
            (A_DESC_X, "TRANSACTION"),
            (A_DESC_X + 70.0, "DESCRIPTION"),
            (A_AMOUNT_X, "AMOUNT"),
        ]
    )


def test_a_header_must_agree_with_a_row_it_governs() -> None:
    """A header is only a header of the rows that begin where it says they do.

    This line is shaped like a header in every respect — it opens with its date
    label, that label stands on the date column, its cells are a column apart,
    and the amount column closes it on the right — and it still names a
    description column ten points left of where these rows actually start
    describing. Nothing about its own shape can catch that; only the rows can.
    Trusting it would delete the first word of every row on the page.
    """
    _assert_not_a_header(
        [
            (A_DATE_X, "DATE"),
            (A_TIME_X, "TIME"),
            (190.0, "TRANSACTION"),
            (260.0, "DESCRIPTION"),
            (A_AMOUNT_X, "AMOUNT"),
        ]
    )


def test_a_narrow_action_lane_is_still_a_lane() -> None:
    """The lane gap must not be set so wide that real controls fall through it.

    A control need not sit far from the description to be a control; it need
    only sit apart from it. This one clears the column by a hair — narrower
    than the merchant rows' own action lane, and far narrower than the gap a
    generous threshold would demand — and the row it precedes is the only row
    on the page, so if the gap is not recognised the page has no anchor at all
    and the control is never stripped.
    """
    pages = _page(
        [
            _template_a_header(),
            _template_a_with_button(
                100.0, "EMI", "SALTFLOWER BAKERY", "615.00", x0=A_DESC_X - 7.0 - 18.0
            ),
        ]
    )
    assert _narrations(pages) == ["SALTFLOWER BAKERY"]


def test_a_header_agreeing_only_with_a_narrations_second_word_is_not_one() -> None:
    """Agreement with a row's second word only counts if the first is a control.

    "ZEPHYR BAKEHOUSE" is an ordinary description: BAKEHOUSE follows ZEPHYR by a
    word space. This line names BAKEHOUSE's left edge as the description column
    — and every other test of a header passes, because the line is shaped like
    one. If a row were allowed to agree with a header on its second word for no
    better reason than that it has a second word, this would anchor there and
    ZEPHYR would be stripped off the front of its own narration.

    A row may begin describing at its second word, but only where the first
    stands apart in a lane of its own: clear of the column, with a gap behind
    it wider than a description leaves between its words. ZEPHYR does not; it
    is simply the word before BAKEHOUSE.
    """
    _assert_not_a_header(
        [
            (A_DATE_X, "DATE"),
            (A_TIME_X, "TIME"),
            (240.0, "TRANSACTION"),
            (310.0, "DESCRIPTION"),
            (A_AMOUNT_X, "AMOUNT"),
        ]
    )


def test_a_header_below_the_rows_does_not_govern_them() -> None:
    """A header stands over its table, not under it.

    This line is a well-formed header of these very rows — it agrees with the
    column they describe in — but it is printed beneath them. A table's header
    introduces the rows that follow it; a line that trails them describes
    nothing, and the rows above it are not its to interpret.
    """
    _assert_not_a_header(
        [
            (A_DATE_X, "DATE"),
            (A_TIME_X, "TIME"),
            (A_DESC_X, "TRANSACTION"),
            (A_DESC_X + 70.0, "DESCRIPTION"),
            (A_AMOUNT_X, "AMOUNT"),
        ],
        doctop=200.0,
    )


def test_a_headers_amount_column_closes_it_on_the_right() -> None:
    """The description column is not the last one: the amount column follows.

    A line that names a date and a description but never an amount is not the
    header of a transaction table.
    """
    _assert_not_a_header(
        [
            (A_DATE_X, "DATE"),
            (A_TIME_X, "TIME"),
            (A_DESC_X, "TRANSACTION"),
            (A_DESC_X + 70.0, "DESCRIPTION"),
        ]
    )


def test_genuine_leading_token_inside_description_column_survives() -> None:
    """False-positive guard: a real EMI row whose description starts with EMI.

    Template B's description column begins far to the left of Template A's
    action lane. Any fixed threshold would delete this row's whole narration.
    """
    pages = _template_b_page(
        [
            _template_b_row(100.0, ["EMI", "processing", "fee"], "590.00"),
            _template_b_row(120.0, ["EMI", "conversion", "adjustment"], "150.00"),
            _template_b_row(140.0, ["ANNUAL", "membership", "charge"], "999.00"),
        ]
    )
    assert _narrations(pages) == [
        "EMI processing fee",
        "EMI conversion adjustment",
        "ANNUAL membership charge",
    ]


def test_non_emi_action_label_is_also_dropped() -> None:
    """The discriminator is position, not the word: any label in the lane goes."""
    pages = _template_a_page(
        [_template_a_with_button(160.0, "OFFER", "LANTERN ELECTRONICS", "7,499.00")]
    )
    assert _narrations(pages)[-1] == "LANTERN ELECTRONICS"


def test_plain_rows_are_unchanged() -> None:
    """Rows without an action control keep their narration verbatim."""
    pages = _template_a_page([])
    assert _narrations(pages) == [
        "ZEPHYR BAKEHOUSE",
        "ORBIT GROCERS",
        "TIDEWATER BOOKS",
    ]


def test_page_without_a_header_is_left_alone() -> None:
    """No header, no column, no stripping: the page is not ours to interpret."""
    pages = _page(
        [
            _template_a_plain(100.0, "ZEPHYR BAKEHOUSE", "1,200.00"),
            _template_a_with_button(120.0, "EMI", "QUILL COFFEE HOUSE", "455.00"),
        ]
    )
    lines = group_words_into_lines(pages[0]["words"])
    assert derive_description_column_x0(lines) is None
    assert _narrations(pages)[-1] == "EMI QUILL COFFEE HOUSE"


def test_page_with_two_disagreeing_headers_is_left_alone() -> None:
    """Two tables on one page: no single column governs it, so strip nothing."""
    pages = _page(
        [
            _template_a_header(),
            _template_a_with_button(100.0, "EMI", "QUILL COFFEE HOUSE", "455.00"),
            _template_b_header(140.0),
            _template_b_row(160.0, ["ORBIT", "GROCERS"], "845.50"),
        ]
    )
    lines = group_words_into_lines(pages[0]["words"])
    assert derive_description_column_x0(lines) is None
    assert _narrations(pages)[0] == "EMI QUILL COFFEE HOUSE"


def test_prose_alongside_a_real_header_does_not_veto_it() -> None:
    """Prose must not be read as a second, disagreeing header either.

    A page carrying both a footnote about the columns and the table that has
    them would otherwise be declared ambiguous, and stripping would silently
    stop. The sentence is not a header in either direction.
    """
    prose = _row(30.0, [(24.0, "TRANSACTION"), (90.0, "DESCRIPTION")])
    pages = _page(
        [
            prose,
            _template_a_header(),
            _template_a_with_button(100.0, "EMI", "QUILL COFFEE HOUSE", "455.00"),
        ]
    )
    lines = group_words_into_lines(pages[0]["words"])
    assert derive_description_column_x0(lines) == A_DESC_X
    assert _narrations(pages) == ["QUILL COFFEE HOUSE"]


def test_row_is_never_lost_when_only_discardable_words_remain() -> None:
    """A row whose in-column words are all discarded by cleanup must survive.

    ``_build_narration`` drops stray glyphs such as ``I``. If furniture removal
    strips the row's only real word on the strength of such a glyph, the row
    would be rejected as ``empty_narration`` and vanish from the ledger.
    """
    row = _row(
        160.0,
        [
            (A_DATE_X, "05/02/2026"),
            (A_TIME_X, "10:15"),
            (A_ACTION_X, "GENUINE"),
            (A_DESC_X, "I"),
            (A_AMOUNT_X, "275.00"),
        ],
    )
    pages = _template_a_page([row])
    txns = extract_transactions(pages, strip_action_lane=True)

    assert len(txns) == 4
    assert txns[-1].narration == "GENUINE"
    assert txns[-1].amount == "275.00"


def test_row_is_never_lost_when_stripping_leaves_a_rejected_narration() -> None:
    """Stripping must not turn an accepted narration into a rejected one.

    A narration with no letters is rejected as a summary row. If the row's only
    in-column word is non-alphabetic, dropping the word left of the column
    would leave a narration that the summary-row filter discards, and the
    transaction would vanish — a different rejection path than an empty
    narration, but the same loss. The unstripped narration is kept instead.
    """
    row = _row(
        160.0,
        [
            (A_DATE_X, "05/02/2026"),
            (A_TIME_X, "10:15"),
            (A_ACTION_X, "FOREIGN"),
            (A_DESC_X, "3.5%"),
            (A_AMOUNT_X, "612.00"),
        ],
    )
    pages = _template_a_page([row])
    txns = extract_transactions(pages, strip_action_lane=True)

    assert len(txns) == 4
    assert txns[-1].narration == "FOREIGN 3.5%"
    assert txns[-1].amount == "612.00"


def test_stripping_never_conjures_a_transaction_from_a_summary_row() -> None:
    """Stripping must not turn a rejected line into an accepted one either.

    A summary line whose keyword happens to sit left of the description column
    would, if the keyword were stripped as furniture, read as an ordinary
    narration and be emitted — a transaction that never existed. The row as
    printed decides whether it is a transaction at all.
    """
    row = _row(
        160.0,
        [
            (A_DATE_X, "05/02/2026"),
            (A_TIME_X, "10:15"),
            (A_ACTION_X, "TOTAL"),
            (A_DESC_X, "QUARTZ"),
            (A_AMOUNT_X, "9,410.00"),
        ],
    )
    pages = _template_a_page([row])

    stripped = extract_transactions(pages, strip_action_lane=True)
    unstripped = extract_transactions(pages)

    assert len(unstripped) == 3
    assert len(stripped) == 3
    assert all(txn.amount != "9,410.00" for txn in stripped)


def test_generic_profile_does_not_strip_left_lane_words() -> None:
    """Removal is not applied to layouts we have not measured.

    A column header is evidence only where the lane between it and the time
    column is known to hold furniture. Profiles that have not opted in — the
    generic fallback and ICICI, which share this extractor — see no change.
    """
    row = _row(
        160.0,
        [
            (A_DATE_X, "05/02/2026"),
            (A_TIME_X, "10:15"),
            (A_ACTION_X, "FOREIGN"),
            (A_DESC_X, "HARBOUR"),
            (A_DESC_X + 60.0, "SUPPLIES"),
            (A_AMOUNT_X, "3,100.00"),
        ],
    )
    pages = _template_a_page([row])

    default_narrations = [txn.narration for txn in extract_transactions(pages)]
    assert default_narrations[-1] == "FOREIGN HARBOUR SUPPLIES"

    generic = GenericParser()
    icici = IciciParser()
    assert generic.strip_action_lane is False
    assert icici.strip_action_lane is False

    generic_txns, _debug = generic._extract_transactions_with_debug(pages)
    icici_txns, _icici_debug = icici._extract_transactions_with_debug(pages)
    assert generic_txns[-1].narration == "FOREIGN HARBOUR SUPPLIES"
    assert icici_txns[-1].narration == "FOREIGN HARBOUR SUPPLIES"


def test_hdfc_profile_opts_into_action_lane_removal() -> None:
    """The HDFC profile is the one that enables removal."""
    hdfc = HdfcParser()
    assert hdfc.strip_action_lane is True

    pages = _template_a_page(
        [_template_a_with_button(160.0, "EMI", "QUILL COFFEE HOUSE", "455.00")]
    )
    txns, _debug = hdfc._extract_transactions_with_debug(pages)
    assert txns[-1].narration == "QUILL COFFEE HOUSE"
