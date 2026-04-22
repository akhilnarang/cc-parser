---
name: add-bank-parser
description: Add a new bank-specific credit card statement PDF parser, or improve an existing one. Use when adding CC statement support for a new bank or fixing parsing issues with an existing bank.
---

# Add or Update a CC Statement Parser

**This skill is interactive.** It requires running Python/Bash to extract PDF data, iterating on the parser, and testing. Do not run this in the background. If you need tool permissions, ask for them.

Arguments: `$ARGUMENTS` — bank slug and path to sample PDF.

## Step 1: Study the codebase

Read these files:
- `AGENTS.md` — architecture, parser contract, consumer contracts, detection order, privacy rules
- `cc_parser/parsers/models.py` — output schema (see Output Schema section below)
- `cc_parser/parsers/generic.py` — `GenericParser` base class. Start here and override only the stages that differ.
- `cc_parser/parsers/registry.py` — add the parser class in stable user-facing order.
- At least one existing parser in `cc_parser/parsers/` — read a simple one (e.g., `hdfc.py`, ~28 lines) and a complex one
- `cc_parser/parsers/factory.py` — detection heuristics and registration

## Step 2: Extract raw PDF data

**MANDATORY. Do not write any parser code before completing this step.**

Run `extract_raw_pdf()` from `cc_parser/extractor.py` via `uv run python -c "..."`. Print all tables on all pages (every row). Print page text for metadata. If encrypted, ask the user for the password.

From the extraction, determine:
- Column layout and count
- Date format (DD/MM/YYYY, DD Mon YY, DDMMM, etc.)
- Credit/debit markers (Cr/Dr per-transaction, section-based "Payments & Refunds", or bare C/D)
- Card number format and member/person headers (for multi-card statements)
- Amount format (any prefixes like IDFC's "r", commas)
- Where summary data lives (previous balance, purchases, payments, due date, total amount due)

## Step 3: Try the generic parser first

Before writing any code, test if `GenericParser` already handles the format:

```bash
uv run cc-parser <pdf> --bank generic
```

If it produces correct transactions with good debit/credit classification, your parser can be a thin wrapper (see `hdfc.py`). Only write custom extraction if generic fails.

## Step 4: Write or update the parser

**New bank:** Create `cc_parser/parsers/{bank}.py`. Extend `GenericParser`. Override only what's different — typically:
- `_extract_transactions_with_debug()` — if the statement needs a custom transaction pipeline
- `_extract_due_date()`, `_extract_total_amount_due()`, `_extract_summary()` — if summary/header layout differs
- `_extract_name()` / `_extract_card_number()` — if metadata format is non-standard
- `_parse_date()`, `_parse_amount()`, `_classify_credit_debit()`, `_extract_transaction_lines()` — if shared token hooks are enough

**Existing bank fix:** Read the existing parser, fix the issue, test with `-vvv` for debug output.

## Step 5: Register (new bank only)

- `parsers/registry.py`: add the parser class in stable ordering
- `parsers/factory.py`: add/update heuristic in `detect_bank()` (order matters — see gotchas)
- `cli.py`: add to `BankOption` enum

## Step 6: Test and iterate

Run: `uv run cc-parser <pdf> --bank {bank}`

Check:
- [ ] Transaction count matches the PDF
- [ ] Debits in `transactions`, credits in `payments_refunds`
- [ ] Person groups correct (for multi-card statements)
- [ ] Due date and total amount due extracted
- [ ] `reconciliation.smart_delta` is small (ideally 0)
- [ ] Adjustment pairs detected (refunds/reversals)

Use `-vvv` for full debug output if something is wrong. This is iterative — fix and re-test.

## Output Schema

Parsers return `ParsedStatement` (from `models.py`):

```
ParsedStatement:
  file: str
  bank: str
  name: str | None                          # cardholder name
  card_number: str | None                   # masked card number
  due_date: str | None                      # DD/MM/YYYY
  statement_total_amount_due: str | None
  card_summaries: list[CardSummary]         # per-card totals
  overall_total: str                        # sum of debit transactions
  person_groups: list[PersonGroup]          # transactions grouped by cardholder
  payments_refunds: list[Transaction]       # credit transactions
  payments_refunds_total: str
  overall_reward_points: str
  reward_points_balance: str | None
  transactions: list[Transaction]           # debit transactions
  reconciliation: Reconciliation
  possible_adjustment_pairs: list[AdjustmentPair]
```

Each `Transaction`:
```
Transaction:
  date: str                    # DD/MM/YYYY (REQUIRED — consumer parses with strptime)
  time: str | None
  narration: str
  reward_points: str | None
  amount: str                  # comma-separated "25,000.00" (REQUIRED — consumer strips commas)
  card_number: str | None
  person: str | None
  transaction_type: "debit" | "credit"
  credit_reasons: str | None
  transaction_id: str
```

**Consumer contract (from AGENTS.md — breaking if changed):**
- Dates MUST be DD/MM/YYYY
- Amounts MUST be comma-separated strings
- Detection order in `factory.py` matters

## Gotchas

- **Try generic first.** `GenericParser` already handles standard DD/MM/YYYY layouts and now exposes shared date/amount/credit hooks before you reach for a full custom parser.
- **Prefer header-region detection.** Auto-detection should key off first-page branding/header content and filename before considering broader body text. Merchant rows can mention other banks and cause false positives.
- **Use shared date helpers.** Keep output as `DD/MM/YYYY` via `cc_parser/parsers/tokens.py` (`parse_date`, `parse_multi_token_date`, `normalize_date_long`). If browser/Pyodide imports this code, remember `python-dateutil` must stay available there too.
- **Detection order matters.** In `detect_bank()`: INDUSIND before ICICI (fine print mentions "ICICI Lombard"), "AXIS BANK" not just "AXIS" (avoids "TAXATION"), HSBC/Jupiter before SBI. Read existing `detect_bank()` comments.
- **Watch for example/MITC pages.** Some issuers, including Equitas, include later illustration pages with fake totals and dates. Total due and due date should come from the page-1 summary, not generic whole-document regex matches.
- **Credit classification is the hardest part.** Per-transaction Cr/Dr markers, section headers ("Payments & Refunds"), bare C/D (SBI), no marker on debits with CR only on credits (HSBC) — each bank is different. Study how existing parsers handle this.
- **Multi-card statements.** Primary + add-on cards appear as sections with member headers. Parser must detect headers, group transactions by person, build `card_summaries` and `person_groups`. Use "ADDON <last 4 digits>" if names can't be extracted.
- **Reconciliation is observability.** Don't coerce totals. Report the delta honestly. Non-zero delta means the parser missed something.
- **Privacy.** Never commit real PDFs. Never hardcode customer names, card numbers, or amounts.

## Self-improvement

If you discover new patterns, update this skill. Update AGENTS.md if anything affects the consumer contract or detection order.
