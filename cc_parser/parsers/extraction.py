"""Transaction extraction from raw PDF pages.

Reconstructs visual lines from PDF word coordinates and extracts
structured transaction rows with date, time, narration, amount,
reward points, and credit/debit classification.
"""

import re
from typing import Any, NamedTuple

from cc_parser.parsers.cards import (
    extract_card_from_line,
    looks_like_member_header,
)
from cc_parser.parsers.models import Transaction
from cc_parser.parsers.narration import (
    clean_narration_artifacts,
    collect_row_context_tokens,
    enrich_reference_only_narration,
    extract_continuation_narration,
    needs_context_merge,
)
from cc_parser.parsers.tokens import (
    SEPARATOR_TOKENS,
    clean_space,
    normalize_amount,
    normalize_token,
    parse_amount_token,
    parse_date_token,
    parse_time_token,
)

_SUMMARY_KEYWORDS = {
    "TOTAL",
    "SUBTOTAL",
    "CARD TOTAL",
    "TOTAL DOMESTIC",
    "TOTAL INTERNATIONAL",
    "STATEMENT DATE",
    "OPENING BALANCE",
    "CLOSING BALANCE",
    "TOTAL SPENDS",
    "AMOUNT DUE",
    "MINIMUM DUE",
}

# Horizontal slack (in points) for deciding that a word box starts inside a
# column rather than left of it. Column left edges are stable within a
# template; this only absorbs sub-point rendering jitter.
_COLUMN_X_TOLERANCE = 2.0

# The transaction table's column header, as separate word boxes. The cell that
# opens the description column is "TRANSACTION DESCRIPTION".
_DESCRIPTION_HEADER_PAIR = ("TRANSACTION", "DESCRIPTION")
_DATE_HEADER_LABEL = "DATE"
_AMOUNT_HEADER_LABEL = "AMOUNT"

# A header is a row of column labels; prose that happens to name the columns is
# not one, and mistaking a sentence for the header would put the column
# anywhere and delete whatever fell left of it. The difference is geometric, so
# the test is geometric: a header opens with its date label sitting on the same
# column as the dates of the rows beneath it, and its cells are separated by
# column-sized gaps. Measured across the layouts, the gap before the
# description cell is at least 42.7pt, where the space between two words of one
# label is ~1.6pt; the threshold sits an order of magnitude clear of prose and
# well under the narrowest real gap.
_HEADER_COLUMN_GAP = 12.0

# What separates an action control from the description it precedes: the
# control occupies a lane of its own, and the space behind it is wider than any
# a description leaves between two of its own words.
#
# Calibrated on real statements, not chosen: across them, every row that begins
# left of the description column begins with the instalment control, and the
# gap from the control's right edge to the description's left edge is 7.1, 8.2
# or 10.5pt. Words within a description sit 1.6 to 3.2pt apart. The threshold
# lies in that window — above ordinary word spacing, below the narrowest
# control gap observed.
_ACTION_LANE_GAP = 6.0


def _is_summary_row(narration: str) -> bool:
    """Return True when *narration* looks like a summary/footer row.

    Rejects rows where the narration text (between date and amount):
    - contains NO alphabetic characters (purely numeric like ``"9"``),
    - matches common statement summary keywords, or
    - is a single small integer (the date + count + amount pattern).
    """
    stripped = narration.strip()
    if not stripped:
        return False

    # Reject narrations with no alphabetic characters at all
    if not any(ch.isalpha() for ch in stripped):
        return True

    upper = stripped.upper()

    # Reject known summary keyword patterns
    for keyword in _SUMMARY_KEYWORDS:
        if keyword in upper:
            return True

    # Reject "single small integer" narrations (e.g. "9", "12")
    # These appear when a page/count number sits between date and amount.
    return bool(re.fullmatch(r"\d{1,4}", stripped))


def _narration_rejection(narration: str) -> str | None:
    """Return the reason a narration disqualifies its row, or ``None``.

    Single gate for every narration-based rejection. Furniture stripping must
    never change whether a row is emitted, only what its narration says, and
    that can only be enforced if all such rejections are asked in one place:
    any new narration-based rejection belongs here, not inline at the call
    site, or the stripped/unstripped comparison would silently miss it.

    Returns:
        ``"empty_narration"``, ``"summary_row"``, or ``None`` when acceptable.
    """
    if not narration:
        return "empty_narration"
    if _is_summary_row(narration):
        return "summary_row"
    return None


def group_words_into_lines(
    words: list[dict[str, Any]], y_tolerance: float = 1.8
) -> list[list[dict[str, Any]]]:
    """Group extracted PDF words into visual lines.

    Args:
        words: Word dictionaries from extractor (must include x0/doctop).
        y_tolerance: Vertical tolerance to merge words into one line.
    """
    sorted_words = sorted(
        words, key=lambda item: (float(item["doctop"]), float(item["x0"]))
    )
    lines: list[list[dict[str, Any]]] = []
    current_line: list[dict[str, Any]] = []
    current_y: float | None = None

    for word in sorted_words:
        y_value = float(word["doctop"])
        if current_y is None or abs(y_value - current_y) <= y_tolerance:
            current_line.append(word)
            current_y = y_value if current_y is None else (current_y + y_value) / 2
        else:
            lines.append(sorted(current_line, key=lambda item: float(item["x0"])))
            current_line = [word]
            current_y = y_value

    if current_line:
        lines.append(sorted(current_line, key=lambda item: float(item["x0"])))

    return lines


def _find_date_index(tokens: list[str]) -> int:
    """Return index of the first date-like token, or ``-1``."""
    return next(
        (i for i, token in enumerate(tokens) if parse_date_token(token) is not None),
        -1,
    )


def _find_amount_index(tokens: list[str]) -> int:
    """Return index of the rightmost amount-like token, or ``-1``."""
    return next(
        (
            i
            for i in range(len(tokens) - 1, -1, -1)
            if parse_amount_token(tokens[i]) is not None
        ),
        -1,
    )


def _advance_to_narration(
    tokens: list[str], date_idx: int, amount_idx: int
) -> tuple[int, str | None]:
    """Advance past the date, optional time, and reference-number tokens.

    Args:
        tokens: Normalized tokens of a single visual line.
        date_idx: Index of the date token.
        amount_idx: Index of the amount token (narration upper bound).

    Returns:
        Tuple ``(cursor, time_value)`` where cursor is the first index that
        may belong to narration and time_value is the parsed time, if any.
    """
    cursor = date_idx + 1
    while cursor < len(tokens) and tokens[cursor] in SEPARATOR_TOKENS:
        cursor += 1

    time_value: str | None = None
    time_idx = next(
        (
            i
            for i in range(cursor, min(amount_idx, cursor + 6))
            if parse_time_token(tokens[i]) is not None
        ),
        -1,
    )
    if time_idx != -1:
        time_value = parse_time_token(tokens[time_idx])
        cursor = time_idx + 1
        while cursor < len(tokens) and tokens[cursor] in SEPARATOR_TOKENS:
            cursor += 1

    if cursor < len(tokens) and re.fullmatch(r"\d{8,}", tokens[cursor]):
        cursor += 1
        while cursor < len(tokens) and tokens[cursor] in SEPARATOR_TOKENS:
            cursor += 1

    return cursor, time_value


def _word_x0(word: dict[str, Any]) -> float:
    """Return the left edge of a word box, defaulting to ``0.0``."""
    try:
        return float(word.get("x0", 0) or 0)
    except TypeError, ValueError:
        return 0.0


def _word_x1(word: dict[str, Any]) -> float:
    """Return the right edge of a word box, defaulting to ``0.0``."""
    try:
        return float(word.get("x1", 0) or 0)
    except TypeError, ValueError:
        return 0.0


class _RowGeometry(NamedTuple):
    """Where one transaction row puts its date and the head of its narration."""

    line_index: int
    date_x0: float
    first_x0: float
    first_x1: float
    second_x0: float | None


def _transaction_row_geometry(
    lines: list[list[dict[str, Any]]],
) -> list[_RowGeometry]:
    """Where each of a page's transaction rows puts its date and description.

    Returns:
        One ``_RowGeometry`` per line that reads as a transaction row.
    """
    geometry: list[_RowGeometry] = []
    for line_index, line_words in enumerate(lines):
        tokens = [
            normalize_token(str(item.get("text", ""))).strip() for item in line_words
        ]
        date_idx = _find_date_index(tokens)
        if date_idx == -1:
            continue
        amount_idx = _find_amount_index(tokens)
        if amount_idx == -1 or amount_idx <= date_idx:
            continue

        cursor, _time_value = _advance_to_narration(tokens, date_idx, amount_idx)
        span = [
            i
            for i in range(cursor, min(amount_idx, len(line_words)))
            if tokens[i] and tokens[i] not in SEPARATOR_TOKENS
        ]
        if not span:
            continue
        geometry.append(
            _RowGeometry(
                line_index=line_index,
                date_x0=_word_x0(line_words[date_idx]),
                first_x0=_word_x0(line_words[span[0]]),
                first_x1=_word_x1(line_words[span[0]]),
                second_x0=(_word_x0(line_words[span[1]]) if len(span) > 1 else None),
            )
        )
    return geometry


def _row_starts_describing_at(
    row: _RowGeometry, column_x0: float, tolerance: float
) -> bool:
    """Whether *row* begins its description at *column_x0*.

    Ordinarily that is simply the row's first narration word. It is the second
    only when the first is an action control — and a control is not merely a
    word that happens to come first: it stands alone in a lane of its own, with
    a gap behind it that no two words of one description would leave. Offering
    the second word unconditionally would let a line claiming a column one word
    into an ordinary narration call itself the header and eat the word it
    skipped.

    The lane gap also places the first word clear of the column: it ends at
    least ``_ACTION_LANE_GAP`` left of a second word that is itself within
    ``tolerance`` of the column, and the gap exceeds twice the tolerance, so no
    separate check for that is needed.
    """
    if abs(column_x0 - row.first_x0) <= tolerance:
        return True
    if row.second_x0 is None:
        return False
    return (
        abs(column_x0 - row.second_x0) <= tolerance
        and row.second_x0 - row.first_x1 >= _ACTION_LANE_GAP
    )


def _governs_rows_below(
    column_x0: float,
    header_index: int,
    geometry: list[_RowGeometry],
    tolerance: float,
) -> bool:
    """Whether *column_x0* is where a row beneath the header starts describing.

    A header describes the rows under it. A line that names a description
    column no row actually begins in is not their header, whatever it looks
    like — and its column, left of where the descriptions really start, would
    eat the first word of every row. One matching row is enough: a page may
    hold a single transaction, and that transaction may carry an action control
    of its own, so a majority is not available to ask for.
    """
    return any(
        row.line_index > header_index
        and _row_starts_describing_at(row, column_x0, tolerance)
        for row in geometry
    )


def _header_description_x0(
    line_words: list[dict[str, Any]],
    date_column_x0s: list[float],
    tolerance: float = _COLUMN_X_TOLERANCE,
) -> float | None:
    """Return the description column's left edge if *line* is a table header.

    A header row is recognised by its geometry, not by containing the words a
    header contains: a sentence naming the columns contains those words too,
    and accepting it would anchor the column on an arbitrary coordinate and
    strip whatever fell left of it. So the line must be laid out as a header:
    it opens with the date label, that label sits on the very column the rows
    below it put their dates in, the description cell follows after a
    column-sized gap rather than a word space, and the amount label closes the
    line to the right of it.

    Args:
        line_words: Word boxes of one visual line, ordered left to right.
        date_column_x0s: Left edges of the date token on the page's rows.
        tolerance: Horizontal slack (pt) for two edges being the same column.

    Returns:
        Left edge of the ``TRANSACTION DESCRIPTION`` header cell, or ``None``
        when the line is not the transaction table's header row.
    """
    labels = [
        re.sub(r"[^A-Z]", "", str(item.get("text", "")).upper()) for item in line_words
    ]
    if _DATE_HEADER_LABEL not in labels:
        return None

    # A header opens with the label of its first column; a sentence puts words
    # in front of it.
    date_idx = labels.index(_DATE_HEADER_LABEL)
    if date_idx != 0:
        return None

    # And that label stands over the column it names: the rows below put their
    # dates where the header says the dates go. Prose lands wherever it lands.
    date_x0 = _word_x0(line_words[date_idx])
    if not any(abs(date_x0 - column) <= tolerance for column in date_column_x0s):
        return None

    first, second = _DESCRIPTION_HEADER_PAIR
    for i in range(1, len(labels) - 1):
        if labels[i] != first or labels[i + 1] != second:
            continue
        # Cells of a table are a column apart. Words of a sentence are a space
        # apart.
        if _word_x0(line_words[i]) - _word_x1(line_words[i - 1]) < _HEADER_COLUMN_GAP:
            return None
        # And the amount column closes the table to the right of the
        # description.
        if _AMOUNT_HEADER_LABEL not in labels[i + 2 :]:
            return None
        return _word_x0(line_words[i])
    return None


def derive_description_column_x0(
    lines: list[list[dict[str, Any]]], tolerance: float = _COLUMN_X_TOLERANCE
) -> float | None:
    """Derive the left edge of the description column for a page.

    Read off the page's own transaction-table header rather than inferred from
    the rows beneath it: the header cell that opens the description column
    starts exactly where the descriptions start, so a page carrying a single
    row — or only rows that all bear an action control — is measured as
    reliably as a dense one. Row statistics cannot say that, since on a sparse
    page the control's lane can hold more rows than the description's.

    No coordinate is hard-coded: templates of the same issuer disagree on where
    the columns fall, and each page states where its own are.

    The column is taken to be the left edge of the description text as well as
    of its header, so a word starting more than ``tolerance`` left of it is
    furniture. That is an assumption about the layout, not a universal truth —
    it is why removal is opt-in per profile. It is also why a header is not
    trusted on its own word: a line can be shaped exactly like a header and
    still name a column a few points left of where the descriptions really
    begin, and the difference between those two claims is the first word of
    every row on the page. So the header must agree with a row it governs.

    Args:
        lines: Visual lines of one page, each a list of word boxes.
        tolerance: Horizontal slack (pt) for treating two header left edges as
            the same column.

    Returns:
        Left edge of the description column, ``None`` when the page has no
        header row, or ``None`` when it has several that disagree (two tables
        on one page: no single column governs it, so nothing is stripped).
    """
    geometry = _transaction_row_geometry(lines)
    date_columns = [row.date_x0 for row in geometry]

    edges: list[float] = []
    for header_index, line_words in enumerate(lines):
        x0 = _header_description_x0(line_words, date_columns, tolerance)
        if x0 is None:
            continue
        if not _governs_rows_below(x0, header_index, geometry, tolerance):
            continue
        edges.append(x0)

    if not edges:
        return None
    if max(edges) - min(edges) > tolerance:
        return None
    return edges[0]


def _furniture_indices(
    line_words: list[dict[str, Any]],
    cursor: int,
    narration_end: int,
    column_x0: float | None,
    tolerance: float = _COLUMN_X_TOLERANCE,
) -> set[int]:
    """Indices of narration-span words that sit left of the description column.

    Statement templates can render an inline action control (e.g. a
    convert-to-instalment button) as a text run on the transaction's own
    baseline, in a lane between the date/time columns and the description
    column. Line grouping is column-blind, so such a word otherwise lands at
    the head of the narration. Its provenance is positional, not textual:
    anything in the narration span whose left edge precedes the description
    column is page furniture, whatever it says.

    These indices are a proposal, not a verdict: the caller keeps them only if
    the narration built without them still stands on its own. A row left with
    nothing to say is therefore not this function's problem to avoid.

    Returns:
        Indices to omit from narration; empty when no column is known or when
        nothing in the span precedes it.
    """
    if column_x0 is None:
        return set()

    threshold = column_x0 - tolerance
    span = range(cursor, min(narration_end, len(line_words)))
    return {i for i in span if _word_x0(line_words[i]) < threshold}


def classify_credit_transaction(tokens: list[str]) -> tuple[bool, list[str]]:
    """Classify a parsed row as credit using structural markers.

    Returns:
        Tuple ``(is_credit, reasons)`` where reasons contains matched markers.
    """
    reasons: list[str] = []

    for token in tokens:
        normalized = re.sub(r"[^A-Z]", "", token.upper())
        if normalized == "CR":
            reasons.append("cr_marker")
            break

    for token in tokens:
        token_upper = token.upper()
        if token_upper.endswith("CR") and any(ch.isdigit() for ch in token_upper):
            if "cr_marker" not in reasons:
                reasons.append("cr_marker")
            break

    plus_positions = [i for i, token in enumerate(tokens) if token == "+"]
    for idx in plus_positions:
        j = idx + 1
        while j < len(tokens) and tokens[j] in SEPARATOR_TOKENS:
            j += 1
        if j >= len(tokens):
            continue

        next_token = tokens[j]
        next_upper = re.sub(r"[^A-Z]", "", next_token.upper())

        if next_upper in {"C", "CR"}:
            reasons.append("plus_amount_marker")
            break
        if parse_amount_token(next_token) is not None:
            reasons.append("plus_amount_marker")
            break

        if re.fullmatch(r"\d{1,6}", next_token):
            continue

    return (len(reasons) > 0), reasons


def _extract_reward_points(
    tokens: list[str], cursor: int, amount_idx: int
) -> tuple[str | None, int | None, int]:
    """Extract reward points value and determine narration end boundary.

    Returns:
        Tuple ``(reward_value, reward_idx, narration_end)``.
    """
    plus_idx = next((i for i in range(cursor, amount_idx) if tokens[i] == "+"), -1)

    reward_value: str | None = None
    reward_idx: int | None = None
    if plus_idx != -1 and plus_idx + 1 < amount_idx:
        maybe_reward = tokens[plus_idx + 1]
        if re.fullmatch(r"\d{1,6}", maybe_reward):
            reward_value = maybe_reward
            reward_idx = plus_idx + 1

    if reward_idx is None:
        for i in range(amount_idx - 1, max(cursor - 1, amount_idx - 8), -1):
            candidate = tokens[i]
            if re.fullmatch(r"\d{1,5}", candidate):
                reward_idx = i
                reward_value = candidate
                break

    narration_end = plus_idx if plus_idx != -1 else amount_idx
    if reward_idx is not None:
        narration_end = reward_idx

    return reward_value, reward_idx, narration_end


def _build_narration(
    tokens: list[str],
    cursor: int,
    narration_end: int,
    lines: list[list[dict[str, Any]]],
    line_index: int,
    skip_indices: set[int] | None = None,
) -> str:
    """Build narration text from tokens, merging wrapped context as needed.

    Args:
        tokens: Normalized tokens of the transaction line.
        cursor: First index that may belong to narration.
        narration_end: Exclusive upper bound of the narration span.
        lines: All visual lines of the page (for wrapped-context merging).
        line_index: Index of the transaction line within ``lines``.
        skip_indices: Token indices to omit (words outside the description
            column).
    """
    skipped = skip_indices or set()
    narration_tokens = [
        token
        for index, token in enumerate(tokens[cursor:narration_end], start=cursor)
        if index not in skipped
        and token
        and token not in SEPARATOR_TOKENS
        and token not in {"+", "l", "I"}
    ]

    while narration_tokens and narration_tokens[-1] in {"C", "c"}:
        narration_tokens.pop()

    narration = clean_space(" ".join(narration_tokens))

    if needs_context_merge(narration):
        prev_ctx_tokens, next_ctx_tokens = collect_row_context_tokens(lines, line_index)
        context_narration_tokens = [
            token
            for token in [*prev_ctx_tokens, *next_ctx_tokens]
            if token
            and token not in SEPARATOR_TOKENS
            and token not in {"+", "l", "I", "C", "CR", "Cr"}
            and parse_amount_token(token) is None
            and parse_date_token(token) is None
            and parse_time_token(token) is None
        ]
        merged_narration_tokens = [*narration_tokens, *context_narration_tokens]
        narration = clean_space(" ".join(merged_narration_tokens))

    if not narration:
        continuation = extract_continuation_narration(lines, line_index)
        if continuation:
            narration = continuation

    narration = clean_narration_artifacts(narration)
    narration = enrich_reference_only_narration(lines, line_index, narration)

    return narration


def _extract_transactions_with_debug(
    pages: list[dict[str, Any]],
    strip_action_lane: bool = False,
) -> tuple[list[Transaction], dict[str, Any]]:
    """Parse transactions and capture parser diagnostics.

    Args:
        pages: Raw extractor pages.
        strip_action_lane: Opt in to dropping words that sit left of the
            description column (inline action controls rendered on the
            transaction's baseline). Off by default: it presumes the page's
            column header names the description column and that nothing else
            legitimately renders left of it, which holds on templates whose
            word boxes have been examined, so parsers enable it per profile.
    """
    transactions: list[Transaction] = []
    current_card: str | None = None
    current_member: str | None = None
    date_lines: list[dict[str, Any]] = []
    rejected_date_lines: list[dict[str, Any]] = []
    detected_members: list[dict[str, Any]] = []

    for page in pages:
        page_number = int(page.get("page_number", 0) or 0)
        words = page.get("words") or []
        lines = group_words_into_lines(words)
        description_x0 = (
            derive_description_column_x0(lines) if strip_action_lane else None
        )

        for line_index, line_words in enumerate(lines):
            raw_tokens = [str(item.get("text", "")).strip() for item in line_words]
            if not raw_tokens:
                continue

            tokens = [normalize_token(token) for token in raw_tokens]

            member_header = looks_like_member_header(tokens)
            if member_header:
                current_member = member_header
                detected_members.append(
                    {
                        "page": page_number,
                        "line_index": line_index,
                        "member": member_header,
                    }
                )

            line_card, line_member = extract_card_from_line(tokens)
            if line_card:
                current_card = line_card
                if line_member:
                    current_member = line_member

            date_idx = _find_date_index(tokens)
            if date_idx == -1:
                continue

            date_lines.append(
                {
                    "page": page_number,
                    "line_index": line_index,
                    "tokens": tokens,
                    "current_member": current_member,
                    "current_card": current_card,
                }
            )

            amount_idx = _find_amount_index(tokens)
            if amount_idx == -1 or amount_idx <= date_idx:
                rejected_date_lines.append(
                    {
                        "page": page_number,
                        "line_index": line_index,
                        "reason": "amount_not_found",
                        "tokens": tokens,
                    }
                )
                continue

            # Skip the date, optional time, and reference-number columns
            cursor, time_value = _advance_to_narration(tokens, date_idx, amount_idx)

            # Extract reward points and narration boundary
            reward_value, _reward_idx, narration_end = _extract_reward_points(
                tokens, cursor, amount_idx
            )

            # Drop inline action controls rendered left of the description
            # column (they share the row's baseline, so line grouping sweeps
            # them into the narration).
            skip_indices = _furniture_indices(
                line_words, cursor, narration_end, description_x0
            )

            # INVARIANT: furniture stripping may only change what a
            # transaction's narration says, never whether the transaction is
            # emitted. The row as printed is the ground truth for "is this a
            # transaction at all", so the unstripped narration decides
            # emission in BOTH directions: stripping may neither lose a real
            # row (a polluted row beats a lost row) nor conjure one out of a
            # summary line whose only alphabetic word sat left of the column.
            # The stripped narration only supplies the text, and only when it
            # is itself acceptable.
            unstripped = _build_narration(
                tokens, cursor, narration_end, lines, line_index
            )
            rejection = _narration_rejection(unstripped)

            narration = unstripped
            if skip_indices and rejection is None:
                stripped = _build_narration(
                    tokens, cursor, narration_end, lines, line_index, skip_indices
                )
                if _narration_rejection(stripped) is None:
                    narration = stripped

            if rejection is not None:
                rejected_date_lines.append(
                    {
                        "page": page_number,
                        "line_index": line_index,
                        "reason": rejection,
                        "tokens": tokens,
                    }
                )
                continue

            date_value = parse_date_token(tokens[date_idx])
            amount_value = parse_amount_token(tokens[amount_idx])
            if not date_value or not amount_value:
                rejected_date_lines.append(
                    {
                        "page": page_number,
                        "line_index": line_index,
                        "reason": "date_or_amount_parse_failed",
                        "tokens": tokens,
                    }
                )
                continue

            is_credit, reasons = classify_credit_transaction(tokens)
            transactions.append(
                Transaction(
                    date=date_value,
                    time=time_value,
                    narration=narration,
                    reward_points=reward_value,
                    amount=normalize_amount(amount_value),
                    card_number=current_card,
                    person=current_member,
                    transaction_type="credit" if is_credit else "debit",
                    credit_reasons=",".join(reasons) if is_credit else None,
                )
            )

    debug = {
        "date_lines": date_lines,
        "rejected_date_lines": rejected_date_lines,
        "detected_members": detected_members,
    }
    return transactions, debug


def extract_transactions(
    pages: list[dict[str, Any]], strip_action_lane: bool = False
) -> list[Transaction]:
    """Parse transactions from raw pages."""
    transactions, _ = _extract_transactions_with_debug(pages, strip_action_lane)
    return transactions


__all__ = [
    "classify_credit_transaction",
    "derive_description_column_x0",
    "extract_transactions",
    "group_words_into_lines",
]
