"""Helpers for synthetic raw-extraction fixtures and golden baselines.

Fixtures live under ``tests/fixtures/<bank>/<case>.json`` and follow the
``extract_raw_pdf`` schema: ``{file, metadata, pages: [{page_number, text,
words, tables}]}``. Every value in them is synthetic.

Each fixture may have a golden ``<case>.expected.json`` holding the full
``ParsedStatement`` dump. Baseline tests compare the current parser output
against it so a refactor that changes behaviour fails loudly. After an
*intentional* behaviour change, regenerate the goldens with::

    CC_PARSER_UPDATE_BASELINES=1 uv run --frozen pytest tests/test_baseline_*.py

and review the resulting diff before committing it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from cc_parser.parsers.factory import get_parser
from cc_parser.parsers.models import ParsedStatement

FIXTURES_DIR = Path(__file__).parent / "fixtures"
UPDATE_ENV = "CC_PARSER_UPDATE_BASELINES"


def fixture_path(bank: str, case: str) -> Path:
    """Return the path of a raw-data fixture.

    Args:
        bank: Parser key, e.g. ``"bob"``.
        case: Fixture case name without extension.

    Returns:
        Path to ``tests/fixtures/<bank>/<case>.json``.
    """
    return FIXTURES_DIR / bank / f"{case}.json"


def load_raw(bank: str, case: str) -> dict[str, Any]:
    """Load a synthetic raw-extraction fixture.

    Args:
        bank: Parser key, e.g. ``"bob"``.
        case: Fixture case name without extension.

    Returns:
        Raw extraction payload as produced by ``extract_raw_pdf``.
    """
    return json.loads(fixture_path(bank, case).read_text(encoding="utf-8"))


def parse_fixture(bank: str, case: str) -> ParsedStatement:
    """Parse a fixture with the named parser.

    Args:
        bank: Parser key, e.g. ``"bob"``.
        case: Fixture case name without extension.

    Returns:
        Parsed statement model.
    """
    raw = load_raw(bank, case)
    return get_parser(bank, raw).parse(raw)


def assert_matches_golden(bank: str, case: str, parsed: ParsedStatement) -> None:
    """Compare a parsed statement with its golden dump.

    When ``CC_PARSER_UPDATE_BASELINES=1`` is set, the golden file is
    (re)written instead of compared.

    Args:
        bank: Parser key, e.g. ``"bob"``.
        case: Fixture case name without extension.
        parsed: Current parser output.

    Raises:
        AssertionError: If the output differs from the golden file, or the
            golden file is missing and updates are not enabled.
    """
    golden = FIXTURES_DIR / bank / f"{case}.expected.json"
    actual = parsed.model_dump(mode="json")
    if os.environ.get(UPDATE_ENV) == "1":
        golden.write_text(
            json.dumps(actual, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return
    assert golden.exists(), (
        f"missing golden {golden.name}; run with {UPDATE_ENV}=1 to create it"
    )
    expected = json.loads(golden.read_text(encoding="utf-8"))
    assert actual == expected, (
        f"{bank}/{case} output drifted from {golden.name}; if intentional, "
        f"regenerate with {UPDATE_ENV}=1 and review the diff"
    )
