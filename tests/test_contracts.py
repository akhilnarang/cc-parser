"""Regression tests for public parser contracts and privacy behavior."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from cc_parser.cli import BankOption
from cc_parser.extractor import extract_raw_pdf
from cc_parser.parsers.equitas import _extract_total_due_and_date
from cc_parser.parsers.factory import detect_bank, get_parser, list_bank_choices
from cc_parser.parsers.models import ParsedStatement
from cc_parser.parsers.registry import PARSER_REGISTRY
from cc_parser.parsers.summary.totals import (
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

    def test_extract_due_date_from_pages_handles_doubled_letter_header(self) -> None:
        # Why: ICICI Amazon Pay renders the page-1 header with a stylized
        # font that pdfplumber extracts as DDUUEE / DDAATTEE / PPAAYYMMEENNTT,
        # and the right column carries informational notes at the same y.
        pages = [
            {
                "words": [
                    {"text": "PPAAYYMMEENNTT", "doctop": 10, "x0": 10, "x1": 75},
                    {"text": "DDUUEE", "doctop": 10, "x0": 78, "x1": 105},
                    {"text": "DDAATTEE", "doctop": 10, "x0": 108, "x1": 142},
                    # Right-column note merged onto the same logical line.
                    {"text": "To", "doctop": 10, "x0": 220, "x1": 232},
                    {"text": "update", "doctop": 10, "x0": 235, "x1": 270},
                    {"text": "May", "doctop": 30, "x0": 12, "x1": 36},
                    {"text": "23,", "doctop": 30, "x0": 39, "x1": 56},
                    {"text": "2026", "doctop": 30, "x0": 59, "x1": 82},
                ]
            }
        ]

        self.assertEqual(extract_due_date_from_pages(pages), "23/05/2026")

    def test_extract_due_date_from_pages_skips_statement_date_header(self) -> None:
        # Why: "STATEMENT DATE" tokens contain "DATE" but not "DUE";
        # must not be mistaken for the due-date header even when doubled.
        pages = [
            {
                "words": [
                    {"text": "SSTTAATTEEMMEENNTT", "doctop": 10, "x0": 10, "x1": 90},
                    {"text": "DDAATTEE", "doctop": 10, "x0": 95, "x1": 130},
                    {"text": "May", "doctop": 30, "x0": 12, "x1": 35},
                    {"text": "5,", "doctop": 30, "x0": 38, "x1": 50},
                    {"text": "2026", "doctop": 30, "x0": 53, "x1": 78},
                ]
            }
        ]

        self.assertIsNone(extract_due_date_from_pages(pages))

    def test_extract_due_date_from_pages_ignores_back_page_examples(self) -> None:
        # Why: ICICI statements include illustrative interest-calculation samples
        # (e.g., "Payment due date - Oct 26, 2023") on later pages. The page-aware
        # extractor must bound its scan so a back-page example cannot hijack it.
        pages = [
            {"words": []},  # page 1: no header (e.g., scanned/empty page)
            {"words": []},  # page 2: still nothing
            {
                "words": [
                    {"text": "Payment", "doctop": 10, "x0": 10, "x1": 60},
                    {"text": "due", "doctop": 10, "x0": 63, "x1": 85},
                    {"text": "date", "doctop": 10, "x0": 88, "x1": 115},
                    {"text": "-", "doctop": 10, "x0": 118, "x1": 125},
                    {"text": "Oct", "doctop": 10, "x0": 128, "x1": 150},
                    {"text": "26,", "doctop": 10, "x0": 153, "x1": 172},
                    {"text": "2023", "doctop": 10, "x0": 175, "x1": 200},
                ]
            },
        ]

        self.assertIsNone(extract_due_date_from_pages(pages))

    def test_generic_extract_due_date_regex_fallback_is_page_bounded(self) -> None:
        # Why: even when the page-layout extractor finds nothing, the regex
        # fallback must not drift to back-page educational examples.
        from cc_parser.parsers.generic import GenericParser

        parser = GenericParser()
        # Pages 1-2 have no due-date header at all; page 3 carries the example.
        pages = [
            {"words": [], "text": "(blank summary)"},
            {"words": [], "text": "(more spend lines)"},
            {
                "words": [],
                "text": "4 Payment due date - Oct 26, 2023 (illustrative example)",
            },
        ]
        full_text = "\n".join(str(p["text"]) for p in pages)

        self.assertIsNone(parser._extract_due_date(full_text, pages))

    def test_generic_extract_due_date_prefers_page_layout_over_full_text(self) -> None:
        # Why: ICICI statements include illustrative interest-calculation examples
        # in the back ("Payment due date - Oct 26, 2023"). The full-text regex
        # would match those samples, so page-layout extraction must take priority
        # so the actual page-1 header wins.
        from cc_parser.parsers.generic import GenericParser

        parser = GenericParser()
        full_text = (
            "PPAAYYMMEENNTT DDUUEE DDAATTEE\nMay 23, 2026\n"
            "...later in the statement...\n"
            "4 Payment due date - Oct 26, 2023\n"
        )
        pages = [
            {
                "words": [
                    {"text": "PPAAYYMMEENNTT", "doctop": 10, "x0": 10, "x1": 75},
                    {"text": "DDUUEE", "doctop": 10, "x0": 78, "x1": 105},
                    {"text": "DDAATTEE", "doctop": 10, "x0": 108, "x1": 142},
                    {"text": "May", "doctop": 30, "x0": 12, "x1": 36},
                    {"text": "23,", "doctop": 30, "x0": 39, "x1": 56},
                    {"text": "2026", "doctop": 30, "x0": 59, "x1": 82},
                ]
            }
        ]

        self.assertEqual(parser._extract_due_date(full_text, pages), "23/05/2026")


class SurfaceAreaTests(unittest.TestCase):
    """Verify CLI/parser exposure stays aligned."""

    def test_bank_surface_stays_in_sync(self) -> None:
        """BankOption, list_bank_choices(), and PARSER_REGISTRY must agree.

        Adding a bank means editing three places (registry, CLI enum, and
        anywhere a Literal lists slugs). This test catches drift immediately.
        """
        registry_slugs = list(PARSER_REGISTRY.keys())
        enum_slugs = [member.value for member in BankOption]
        choice_slugs = list_bank_choices()

        self.assertEqual(enum_slugs, ["auto", *registry_slugs])
        self.assertEqual(choice_slugs, ["auto", *registry_slugs])

    def test_bank_option_exposes_slice(self) -> None:
        self.assertEqual(BankOption.slice.value, "slice")

    def test_bank_option_exposes_ssfb(self) -> None:
        self.assertEqual(BankOption.ssfb.value, "ssfb")

    def test_bank_option_exposes_yesbank(self) -> None:
        self.assertEqual(BankOption.yesbank.value, "yesbank")

    def test_bank_option_exposes_equitas(self) -> None:
        self.assertEqual(BankOption.equitas.value, "equitas")

    def test_bank_choice_includes_ssfb(self) -> None:
        """Verify ssfb is a valid BankChoice value via get_parser."""
        raw_data = {"file": "test.pdf", "pages": []}
        parser = get_parser("ssfb", raw_data)
        self.assertEqual(parser.bank, "ssfb")

    def test_bank_choice_includes_yesbank(self) -> None:
        """Verify yesbank is a valid BankChoice value via get_parser."""
        raw_data = {"file": "test.pdf", "pages": []}
        parser = get_parser("yesbank", raw_data)
        self.assertEqual(parser.bank, "yesbank")

    def test_bank_choice_includes_equitas(self) -> None:
        """Verify equitas is a valid BankChoice value via get_parser."""
        raw_data = {"file": "test.pdf", "pages": []}
        parser = get_parser("equitas", raw_data)
        self.assertEqual(parser.bank, "equitas")

    def test_factory_detects_and_returns_slice_parser(self) -> None:
        raw_data = {"file": "statement.pdf", "pages": [{"text": "SLICE statement"}]}

        self.assertEqual(detect_bank(raw_data), "slice")
        self.assertEqual(get_parser("slice", raw_data).bank, "slice")

    def test_factory_detects_and_returns_ssfb_parser(self) -> None:
        raw_data = {
            "file": "statement.pdf",
            "pages": [{"text": "SURYODAY SMALL FINANCE BANK"}],
        }

        self.assertEqual(detect_bank(raw_data), "ssfb")
        self.assertEqual(get_parser("ssfb", raw_data).bank, "ssfb")

    def test_factory_detects_and_returns_yesbank_parser(self) -> None:
        raw_data = {
            "file": "statement.pdf",
            "pages": [{"text": "YES BANK Credit Card Statement"}],
        }

        self.assertEqual(detect_bank(raw_data), "yesbank")
        self.assertEqual(get_parser("yesbank", raw_data).bank, "yesbank")

    def test_factory_detects_and_returns_equitas_parser(self) -> None:
        raw_data = {
            "file": "statement.pdf",
            "pages": [
                {
                    "text": "EQUITAS SMALL FINANCE BANK\n"
                    "16/04/2026 HDFC FASTAG RECHARGE IND RT123 +2 E ₹152.65 Dr."
                }
            ],
        }

        self.assertEqual(detect_bank(raw_data), "equitas")
        self.assertEqual(get_parser("equitas", raw_data).bank, "equitas")


class ParserContractSmokeTests(unittest.TestCase):
    """Smoke-test that each parser returns a valid ParsedStatement with minimal input."""

    def _make_minimal_raw_data(self, text: str) -> dict:
        return {
            "file": "test.pdf",
            "pages": [{"text": text, "words": [], "page_number": 1}],
        }

    def test_ssfb_parser_returns_parsed_statement(self) -> None:
        raw_data = self._make_minimal_raw_data(
            "SURYODAY SMALL FINANCE BANK\nStatement\n"
        )
        parser = get_parser("ssfb", raw_data)
        result = parser.parse(raw_data)
        self.assertIsInstance(result, ParsedStatement)
        self.assertEqual(result.bank, "ssfb")
        self.assertEqual(result.file, "test.pdf")

    def test_yesbank_parser_returns_parsed_statement(self) -> None:
        raw_data = self._make_minimal_raw_data(
            "YES BANK Credit Card Statement\nStatement Details\nEnd of the Statement\n"
        )
        parser = get_parser("yesbank", raw_data)
        result = parser.parse(raw_data)
        self.assertIsInstance(result, ParsedStatement)
        self.assertEqual(result.bank, "yesbank")
        self.assertEqual(result.file, "test.pdf")

    def test_equitas_parser_returns_parsed_statement(self) -> None:
        raw_data = self._make_minimal_raw_data(
            "EQUITAS SMALL FINANCE BANK\nYour Credit Card Statement\n"
        )
        parser = get_parser("equitas", raw_data)
        result = parser.parse(raw_data)
        self.assertIsInstance(result, ParsedStatement)
        self.assertEqual(result.bank, "equitas")
        self.assertEqual(result.file, "test.pdf")

    def test_equitas_parser_extracts_summary_and_transactions(self) -> None:
        raw_data = {
            "file": "equitas.pdf",
            "pages": [
                {
                    "page_number": 1,
                    "words": [],
                    "text": (
                        "Your Credit Card Statement - Apr 26\n"
                        "Jane Example RUPAY SELFE CARD\n"
                        "Card No: 1234********5678\n"
                        "Statement Summary Total Due: Due Date:\n"
                        "₹12,345.67 10 May 2026\n"
                        "Opening Balance Payments/Credits Spends/Charges\n"
                        "Minimum Due: ₹ 617.28\n"
                        "₹1,000.00 ₹500.00 ₹11,845.67\n"
                        "Reward Points Summary\n"
                        "Opening Balance Reward Points Earned_E Bonus Points Earned_B Redeemed Adjusted Lapsed Closing Balance\n"
                        "10 20 30 0 0 0 60\n"
                        "Transaction History\n"
                        "Date Transaction Details Reference Number Rewards Earned Amount\n"
                        "JANE EXAMPLE : (123456XXXXXX5678)\n"
                        "01/04/2026 123456789012 TEST MERCHANT 12345678901234567890 -10 B ₹100.00 Cr.\n"
                        "02/04/2026 TEST STORE RT12345678901234567890 +25 B ₹500.00 Dr.\n"
                        "Page : 1 of 2\n"
                    ),
                },
                {
                    "page_number": 2,
                    "words": [],
                    "text": (
                        "Date Transaction Details Reference Number Rewards Earned Amount\n"
                        "03/04/2026 CASHBACK PROMO 12345678901234567890 0 ₹50.00 Cr.\n"
                        "04/04/2026 BILLER NAME 99 RT09876543210987654321 +35 E ₹100.00 Dr.\n"
                        "***End of Statement***\n"
                        "Page : 2 of 2\n"
                    ),
                },
            ],
        }

        parser = get_parser("equitas", raw_data)
        result = parser.parse(raw_data)

        self.assertEqual(result.name, "JANE EXAMPLE")
        self.assertEqual(result.card_number, "1234XXXXXXXX5678")
        self.assertEqual(result.due_date, "10/05/2026")
        self.assertEqual(result.statement_total_amount_due, "12,345.67")
        self.assertEqual(result.reconciliation.header_previous_balance, "1,000.00")
        self.assertEqual(
            result.reconciliation.header_payments_credits_received, "500.00"
        )
        self.assertEqual(result.reconciliation.header_purchases_debit, "11,845.67")
        self.assertEqual(len(result.transactions), 2)
        self.assertEqual(len(result.payments_refunds), 2)
        self.assertEqual(result.transactions[0].reward_points, "25")
        self.assertEqual(result.transactions[1].reward_points, "35")
        self.assertEqual(result.payments_refunds[0].reward_points, "-10")
        self.assertEqual(result.payments_refunds[1].reward_points, "0")
        self.assertEqual(result.overall_reward_points, "50")
        self.assertEqual(result.reward_points_bonus, "30")
        self.assertEqual(result.reward_points_balance, "60")
        self.assertEqual(result.reward_points_line_total, "50")

    def test_equitas_parser_handles_scrambled_summary_layout(self) -> None:
        """Some Equitas statements emit the Total Due / Due Date block with the
        labels reordered and the due date split across lines around the amount.

        Layout observed (all values below are synthetic):
            Due Date:
            Statement Summary Total Due:
            DD Month
            ₹<amount>
            YYYY
        """
        raw_data = {
            "file": "equitas.pdf",
            "pages": [
                {
                    "page_number": 1,
                    "words": [],
                    "text": (
                        "Your Credit Card Statement - Jul 26\n"
                        "Jane Example RUPAY SELFE CARD\n"
                        "Card No: 1234********5678\n"
                        "Total Credit Limit Available Credit Limit Cash Limit "
                        "Available Cash Limit\n"
                        "₹ 300,000.00 ₹ 250,000.00 ₹ 100,000.00 ₹ 100,000.00\n"
                        "Due Date:\n"
                        "Statement Summary Total Due:\n"
                        "15 September\n"
                        "₹12,345.67\n"
                        "2026\n"
                        "Opening Balance Payments/Credits Spends/Charges\n"
                        "Minimum Due: ₹ 617.28\n"
                        "₹1,000.00 ₹500.00 ₹11,845.67\n"
                        "Reward Points Summary\n"
                        "Opening Balance Reward Points Earned_E Bonus Points Earned_B Redeemed Adjusted Lapsed Closing Balance\n"
                        "10 20 30 0 0 0 60\n"
                        "Transaction History\n"
                        "Date Transaction Details Reference Number Rewards Earned Amount\n"
                        "JANE EXAMPLE : (123456XXXXXX5678)\n"
                        "01/07/2026 TEST STORE RT12345678901234567890 +25 B ₹500.00 Dr.\n"
                        "Page : 1 of 1\n"
                    ),
                },
            ],
        }

        parser = get_parser("equitas", raw_data)
        result = parser.parse(raw_data)

        self.assertEqual(result.due_date, "15/09/2026")
        self.assertEqual(result.statement_total_amount_due, "12,345.67")
        self.assertEqual(result.reconciliation.header_previous_balance, "1,000.00")
        self.assertEqual(
            result.reconciliation.header_payments_credits_received, "500.00"
        )
        self.assertEqual(result.reconciliation.header_purchases_debit, "11,845.67")


class EquitasSummaryWindowTests(unittest.TestCase):
    """Guard behavior for the Equitas Total Due / Due Date window extractor."""

    def test_minimum_due_amount_inside_window_is_ignored(self) -> None:
        """A ``Minimum Due:`` line pulled into the window must not be read as
        the total due; its amount is dropped before the single-amount check."""
        text = (
            "Total Due:\n15 September\n₹12,345.67\n2026\n"
            "Minimum Due: ₹ 617.28\nOpening Balance Payments"
        )
        self.assertEqual(_extract_total_due_and_date(text), ("12,345.67", "15/09/2026"))

    def test_ambiguous_multi_amount_window_bails_to_none(self) -> None:
        """More than one amount in the window is an unseen layout: return None
        for the total rather than guessing the first match."""
        text = "Total Due:\n₹100.00 ₹200.00\n10 May 2026\nOpening Balance Payments"
        total_due, due_date = _extract_total_due_and_date(text)
        self.assertIsNone(total_due)
        self.assertEqual(due_date, "10/05/2026")

    def test_ambiguous_multi_date_window_bails_to_none(self) -> None:
        """More than one date in the window returns None for the due date."""
        text = "Total Due:\n₹100.00\n10 May 2026 22 May 2026\nOpening Balance Payments"
        total_due, due_date = _extract_total_due_and_date(text)
        self.assertEqual(total_due, "100.00")
        self.assertIsNone(due_date)

    def test_missing_end_anchor_returns_none(self) -> None:
        """Without the summary end anchor the window is undefined; both fields
        fall back to None so the generic extractor can try."""
        self.assertEqual(
            _extract_total_due_and_date("Total Due:\n₹100.00\n10 May 2026\n"),
            (None, None),
        )


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
