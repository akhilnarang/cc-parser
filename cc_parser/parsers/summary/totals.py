"""Statement-header and summary-field extraction helpers."""

import re
from typing import Any

from cc_parser.parsers.extraction import group_words_into_lines
from cc_parser.parsers.models import StatementSummary
from cc_parser.parsers.tokens import (
    clean_space,
    normalize_amount,
    normalize_token,
    parse_date,
)


def _normalize_due_date_value(value: str) -> str | None:
    """Normalize supported due-date formats to ``DD/MM/YYYY``."""
    return parse_date(value)


def _collapse_doubled_token(token: str) -> str:
    """Collapse fully doubled-letter tokens (``DDUUEE`` -> ``DUE``).

    Why: ICICI Amazon Pay statements render top-of-page section headers in a
    stylized font where every glyph is duplicated. pdfplumber emits those
    tokens verbatim, so equality checks against ``"DUE"`` / ``"DATE"`` miss
    the header. Only collapses when *every* consecutive pair is identical, so
    ordinary words like ``BOOK`` or ``MM`` are unaffected.
    """
    if len(token) < 4 or len(token) % 2:
        return token
    if all(token[i] == token[i + 1] for i in range(0, len(token), 2)):
        return token[::2]
    return token


def extract_name(full_text: str) -> str | None:
    """Extract cardholder name from statement text."""
    honorifics = {"MR", "MRS", "MS", "MISS", "DR"}
    for raw_line in full_text.splitlines():
        line = clean_space(raw_line)
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3 or len(parts) > 6:
            continue
        if parts[0].upper() not in honorifics:
            continue
        tail = parts[1:]
        if all(re.fullmatch(r"[A-Za-z][A-Za-z.'-]*", token) for token in tail):
            return " ".join(tail)

    match = re.search(
        r"\n\s*([A-Z][A-Z ]{4,})\s+Credit\s+Card\s+No\.",
        full_text,
        flags=re.IGNORECASE,
    )
    if match:
        candidate = clean_space(match.group(1)).upper()
        if 2 <= len(candidate.split()) <= 6:
            return candidate

    return None


def extract_due_date(full_text: str) -> str | None:
    """Extract due date from statement body text."""
    compact_text = clean_space(full_text)
    patterns = [
        r"PAYMENT\s+DUE\s+DATE\s*[:\-]?\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
        r"PAYMENT\s+DUE\s+DATE\s*[:\-]?\s*(\d{2}[/-]\d{2}[/-]\d{4})",
        r"DUE\s+DATE\s*[:\-]?\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
        r"DUE\s+DATE\s*[:\-]?\s*(\d{2}[/-]\d{2}[/-]\d{4})",
        r"DUE\s+DATE.{0,100}?(\d{1,2}\s+[A-Za-z]{3,9},?\s+\d{4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, compact_text, flags=re.IGNORECASE)
        if match:
            return _normalize_due_date_value(match.group(1))
    return None


_DUE_DATE_PATTERNS = (
    r"\d{2}[/-]\d{2}[/-]\d{4}",
    r"\d{1,2}\s+[A-Za-z]{3,9},?\s+\d{4}",
    r"[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}",
)
# Why: ICICI page 1 is a 2-column layout — the left "PAYMENT DUE DATE" header sits at
# the same y as right-column informational notes ("To update mobile number, visit..."),
# so naive line grouping merges them. Splitting on a wide x-gap isolates the column
# that owns the header before searching for the date below it. The threshold is the
# whitespace between adjacent words measured as ``next.x0 - prev.x1``.
_COLUMN_GAP_THRESHOLD = 50.0
# Why: the actual due-date value is on the next visual line below the header, but
# pdfplumber-extracted lines may include intervening single-glyph CID artifacts.
# Sweep a small vertical band below the header instead of only "next line".
_DUE_DATE_LOOKAHEAD = 30.0
# Why: due-date headers always live in the first page or two of statement summaries
# in the banks that use the generic chain. Bounding the scan keeps a stray
# illustrative "Payment due date - Oct 26, 2023" in a back-page footnote from
# beating the real header's location on a long statement.
_DUE_DATE_PAGE_LIMIT = 2


def _search_due_date_in_line(joined: str) -> str | None:
    for pattern in _DUE_DATE_PATTERNS:
        if match := re.search(pattern, joined):
            return _normalize_due_date_value(match.group(0))
    return None


def _word_right(word: dict[str, Any]) -> float:
    """Return the right edge of a word, falling back to its left edge."""
    x1 = word.get("x1")
    if x1 is None:
        return float(word.get("x0", 0))
    return float(x1)


def _column_chunks(
    line_words: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Split a logical line into x-contiguous chunks (one per visual column)."""
    if not line_words:
        return []
    sorted_words = sorted(line_words, key=lambda item: float(item.get("x0", 0)))
    chunks: list[list[dict[str, Any]]] = [[sorted_words[0]]]
    for word in sorted_words[1:]:
        if (
            float(word.get("x0", 0)) - _word_right(chunks[-1][-1])
            > _COLUMN_GAP_THRESHOLD
        ):
            chunks.append([word])
        else:
            chunks[-1].append(word)
    return chunks


def _chunk_x_bounds(chunk: list[dict[str, Any]]) -> tuple[float, float]:
    xs = [float(w.get("x0", 0)) for w in chunk]
    return min(xs), max(_word_right(w) for w in chunk)


def _due_date_below_header(
    words: list[dict[str, Any]],
    header_y: float,
    x_min: float,
    x_max: float,
) -> str | None:
    below = [
        w for w in words
        if header_y < float(w.get("doctop", 0)) <= header_y + _DUE_DATE_LOOKAHEAD
        and x_min - 10 <= float(w.get("x0", 0)) <= x_max + 30
    ]
    if not below:
        return None
    below.sort(key=lambda w: (float(w.get("doctop", 0)), float(w.get("x0", 0))))
    joined = clean_space(
        " ".join(normalize_token(str(w.get("text", ""))) for w in below)
    )
    return _search_due_date_in_line(joined)


def extract_due_date_from_pages(pages: list[dict[str, Any]]) -> str | None:
    """Extract due date using line-level page tokens."""
    for page in pages[:_DUE_DATE_PAGE_LIMIT]:
        words = page.get("words") or []
        lines = group_words_into_lines(words)
        for line_words in lines:
            for chunk in _column_chunks(line_words):
                tokens = [normalize_token(str(w.get("text", ""))) for w in chunk]
                upper = {_collapse_doubled_token(token).upper() for token in tokens}
                if "DUE" not in upper or "DATE" not in upper:
                    continue

                if same_line := _search_due_date_in_line(
                    clean_space(" ".join(tokens))
                ):
                    return same_line

                x_min, x_max = _chunk_x_bounds(chunk)
                header_y = max(float(w.get("doctop", 0)) for w in chunk)
                if found := _due_date_below_header(words, header_y, x_min, x_max):
                    return found
    return None


def extract_total_amount_due(full_text: str) -> str | None:
    """Extract statement-level total amount due from summary area."""
    upper_text = full_text.upper()
    start = upper_text.find("TOTAL AMOUNT DUE")
    if start == -1:
        return None

    end = upper_text.find("TOTAL CREDIT LIMIT", start)
    segment = full_text[start:end] if end != -1 else full_text[start : start + 1200]

    # A paid-off card shows a negative total due (overpayment, refund, or
    # reversal). The value sits before the PREVIOUS STATEMENT DUES figure in
    # the summary row. The sign must be kept, or a positive-only match skips
    # the real value and returns the previous dues instead. The lookbehind
    # stops the sign from attaching to a preceding token such as "A/C-1,234.56".
    for pattern in [
        r"(?<![\w/])C\s*-?\d[\d,]*\.\d{2}",
        r"`\s*-?\d[\d,]*\.\d{2}",
        r"(?<![\w./-])-?\d[\d,]*\.\d{2}",
    ]:
        match = re.search(pattern, segment)
        if match:
            cleaned = match.group(0).replace("C", "").replace("`", "").strip()
            return normalize_amount(cleaned)

    return None


def extract_statement_summary(full_text: str) -> StatementSummary:
    """Extract summary block amount candidates and heuristic field mapping."""
    upper_text = full_text.upper()
    start = upper_text.find("PAYMENTS/CREDITS")
    end = upper_text.find("TOTAL CREDIT LIMIT", start if start != -1 else 0)

    if start == -1:
        segment = full_text[:2000]
    else:
        segment = full_text[start : end if end != -1 else start + 2000]

    raw_amounts = [
        normalize_amount(match.group(0).replace("C", "").replace("`", "").strip())
        for match in re.finditer(r"[C`]?\s*\d[\d,]*\.\d{2}", segment)
    ]

    unique_amounts: list[str] = []
    for value in raw_amounts:
        if value not in unique_amounts:
            unique_amounts.append(value)

    summary = StatementSummary(summary_amount_candidates=unique_amounts)

    if len(unique_amounts) >= 5:
        summary.payments_credits_received = unique_amounts[0]
        summary.previous_statement_dues = unique_amounts[1]
        summary.purchases_debit = unique_amounts[2]
        summary.finance_charges = unique_amounts[3]
        summary.equation_tail = unique_amounts[4]

    return summary


__all__ = [
    "extract_due_date",
    "extract_due_date_from_pages",
    "extract_name",
    "extract_statement_summary",
    "extract_total_amount_due",
]
