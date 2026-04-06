"""Regression tests for public parser contracts and privacy behavior."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from cc_parser.cli import BankOption
from cc_parser.extractor import extract_raw_pdf
from cc_parser.parsers.factory import detect_bank, get_parser
from cc_parser.parsers.reconciliation import (
    extract_due_date,
    extract_due_date_from_pages,
)


class DueDateContractTests(unittest.TestCase):
    """Verify due dates always use the downstream-required format."""

    def test_extract_due_date_normalizes_supported_formats(self) -> None:
        samples = {
            "PAYMENT DUE DATE April 5, 2026": "05/04/2026",
            "PAYMENT DUE DATE April 5 2026": "05/04/2026",
            "DUE DATE 5 Apr, 2026": "05/04/2026",
            "DUE DATE 5 Apr 2026": "05/04/2026",
            "DUE DATE 05/04/2026": "05/04/2026",
        }

        for text, expected in samples.items():
            with self.subTest(text=text):
                self.assertEqual(extract_due_date(text), expected)

    def test_extract_due_date_from_pages_normalizes_page_tokens(self) -> None:
        pages = [
            {
                "words": [
                    {"text": "DUE", "doctop": 10, "x0": 10},
                    {"text": "DATE", "doctop": 10, "x0": 50},
                    {"text": "5", "doctop": 10, "x0": 100},
                    {"text": "Apr,", "doctop": 10, "x0": 120},
                    {"text": "2026", "doctop": 10, "x0": 160},
                ]
            }
        ]

        self.assertEqual(extract_due_date_from_pages(pages), "05/04/2026")


class SurfaceAreaTests(unittest.TestCase):
    """Verify CLI/parser exposure stays aligned."""

    def test_bank_option_exposes_slice(self) -> None:
        self.assertEqual(BankOption.slice.value, "slice")

    def test_factory_detects_and_returns_slice_parser(self) -> None:
        raw_data = {"file": "statement.pdf", "pages": [{"text": "SLICE statement"}]}

        self.assertEqual(detect_bank(raw_data), "slice")
        self.assertEqual(get_parser("slice", raw_data).bank, "slice")


class PrivacyTests(unittest.TestCase):
    """Verify exported raw payloads do not leak local paths."""

    def test_extract_raw_pdf_uses_input_basename(self) -> None:
        pdf_path = Path("/tmp/private/nested/statement.pdf")

        fake_page = type(
            "FakePage",
            (),
            {
                "width": 100,
                "height": 200,
                "extract_text": lambda self: "page text",
                "extract_words": lambda self: [],
                "extract_tables": lambda self: [],
            },
        )()

        fake_plumber_doc = type(
            "FakePlumberDoc",
            (),
            {
                "pages": [fake_page],
                "__enter__": lambda self: self,
                "__exit__": lambda self, exc_type, exc, tb: None,
            },
        )()

        fake_fitz_doc = type(
            "FakeFitzDoc",
            (),
            {
                "metadata": {},
                "__enter__": lambda self: self,
                "__exit__": lambda self, exc_type, exc, tb: None,
            },
        )()

        with (
            patch(
                "cc_parser.extractor.prepare_pdf_bytes_if_encrypted",
                return_value=(
                    None,
                    {"is_encrypted": False, "was_decrypted": False},
                    {},
                ),
            ),
            patch("cc_parser.extractor.pdfplumber.open", return_value=fake_plumber_doc),
            patch("cc_parser.extractor.fitz.open", return_value=fake_fitz_doc),
        ):
            document = extract_raw_pdf(pdf_path, include_blocks=False, password=None)

        self.assertEqual(document["file"], "statement.pdf")


if __name__ == "__main__":
    unittest.main()
