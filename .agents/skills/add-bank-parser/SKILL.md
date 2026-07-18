---
name: add-bank-parser
description: Use when adding support for a new bank's credit card statement PDF, fixing parsing or detection regressions on an existing bank, repairing reconciliation deltas (smart_delta != 0), or adapting to a changed statement layout (new headers, columns, date/amount/credit-marker formats).
---

# Add or Update a CC Statement Parser

**This skill is interactive.** It requires running Python/Bash to extract PDF data, iterating on the parser, and testing. Do not run this in the background. If you need tool permissions, ask for them.

Arguments: `$ARGUMENTS` — bank slug and path to sample PDF.

## Step 1: Read the architectural files

Read in this order. Do **not** read existing parsers yet — wait until raw extraction (Step 2) tells you which existing parser is structurally closest.

- `AGENTS.md` — architecture, parser contract, consumer contracts, detection order, privacy rules, Change Workflow (compile + test + docs updates).
- `cc_parser/parsers/models.py` — Pydantic output schema. **Source of truth.** Do not rely on the abridged section below for completeness.
- `cc_parser/parsers/generic.py` — `GenericParser` pipeline and override hooks (see Step 4).
- `cc_parser/parsers/registry.py` — parser registry. **CLI/browser surface order comes from here**, not factory.py.
- `cc_parser/parsers/factory.py` — bank detection only (`detect_bank`, `_BANK_DETECTION_RULES`). Detection ordering is enforced by the rule list.
- `cc_parser/parsers/tokens.py` — shared date/amount/points helpers. Reuse instead of writing ad hoc parsing.

## Step 2: Extract raw PDF data

**MANDATORY. Do not write any parser code before completing this step.**

Call `extract_raw_pdf(pdf_path: Path, include_blocks: bool, password: str | None)` from `cc_parser/extractor.py` via `uv run python -c "..."`. Print all pages. If encrypted, ask the user for the password.

For each page, inspect:
- `text` — header lines, summary boxes, due-date/total-due fields.
- `words` (with x/y coordinates) — most custom parsers reconstruct rows from these, not from `tables`.
- `tables` — only when `pdfplumber` surfaces a clean cell grid (e.g., BOB).

From the extraction, determine:
- Column layout and count.
- Date format (`DD/MM/YYYY`, `DD Mon YY`, `DDMMM`, `DD-Mon-YYYY`, multi-token).
- Credit/debit markers: per-row `Cr`/`Dr`, section headers ("Payments & Refunds"), bare `C`/`D` (SBI), CR-only on credits with no marker on debits (HSBC), `CR`/`DR` glued onto an amount token (some Axis/BOB rows).
- Card-number format and member/person headers (multi-card statements).
- Amount format (currency prefixes like IDFC's `` ` ``/`r`, commas, glued markers).
- Where summary fields live (previous balance, purchases, payments, due date, total amount due) — and whether later pages contain MITC/illustration content with fake totals/dates (Equitas, sometimes others).

## Step 3: Try the generic parser first

Before writing any code:

```bash
uv run cc-parser <pdf> --bank generic
```

If transactions, debit/credit split, due date, total, and `reconciliation.smart_delta` look correct, your parser can be a thin wrapper. Only write a custom pipeline if generic provably fails.

**Now read existing parsers**, picking the closest match:
- Thin `GenericParser` extension: `hdfc.py`, `icici.py`, `equitas.py`, `sbi.py`.
- Table-driven via direct `StatementParser`: `bob.py`.
- Full custom via direct `StatementParser`: `axis.py`, `hsbc.py`, `jupiter.py`, `idfc.py`, `indusind.py`, `slice.py`, `ssfb.py`, `yesbank.py`.

## Step 4: Write or update the parser

**Default:** subclass `GenericParser` and override only what differs. Available hooks:
- `_extract_transactions_with_debug()` — custom transaction extraction pipeline.
- `_extract_summary()`, `_extract_due_date()`, `_extract_total_amount_due()` — header/summary differs.
- `_extract_name()`, `_extract_card_number()` — non-standard metadata.
- `_normalize_transactions(transactions, name, card_number)` — post-extraction normalization (e.g., Equitas card-mask repair).
- `_parse_date()`, `_parse_amount()`, `_classify_credit_debit()`, `_extract_transaction_lines()` — token-level hooks.

**Direct `StatementParser` subclass** only when the format is fundamentally different from the generic pipeline — typically table-driven extraction or coordinate-based row reconstruction. Most existing parsers actually take this path; AGENTS.md still says default to `GenericParser`, so prefer it when feasible.

**Existing-bank fix:** read the existing parser, fix the issue in place. Use `-vvv` for full debug output.

## Step 5: Register (new bank only)

- `cc_parser/parsers/registry.py` — add the parser class to `PARSER_REGISTRY` in stable, user-facing order.
- `cc_parser/cli.py` — add the slug to `BankOption`. **Order must mirror `PARSER_REGISTRY` order** — `tests/test_contracts.py` asserts exact sync between `BankOption`, `PARSER_REGISTRY`, and `list_bank_choices()`.
- `cc_parser/parsers/factory.py` — add a `DetectionRule` to `_BANK_DETECTION_RULES`. Order matters (see Gotchas).

## Step 6: Test, validate, and update docs

```bash
uv run cc-parser <pdf> --bank <bank> -vvv      # full debug output
uv run python -m py_compile cc_parser/parsers/<bank>.py
uv run pytest                                  # contract tests must pass
```

Acceptance checks:
- [ ] Transaction count matches the PDF.
- [ ] Debits in `transactions`, credits in `payments_refunds`.
- [ ] Person groups correct (multi-card statements).
- [ ] Due date and total amount due come from the page-1 summary, not later illustration pages.
- [ ] `reconciliation.smart_delta` is small (ideally 0). Don't coerce — investigate.
- [ ] Refund/reversal adjustment pairs are plausible when present; no false positives on unrelated credits.

When behavior changes, follow the AGENTS.md Change Workflow:
- Update `README.md`, `docs/parsing-notes.md`, and this skill file when relevant.
- If the parser imports a new pure-Python dependency, update **both** `pyproject.toml` and `web/worker.js` (Pyodide surface) — they're coupled.

## Output schema

Parsers return `ParsedStatement` from `cc_parser/parsers/models.py`. **`models.py` is the source of truth** — verify field names there before depending on them. Top-level fields cover identity (`file`, `bank`, `name`, `card_number`), summary (`due_date`, `statement_total_amount_due`), aggregates (`card_summaries`, `person_groups`, `overall_total`), split transactions (`transactions` for debits, `payments_refunds` for credits, `payments_refunds_total`), rewards (`overall_reward_points`, `reward_points_balance`, `reward_points_line_total`, `reward_points_bonus`, `reward_points_bonus_breakdown`), `reconciliation`, and `possible_adjustment_pairs`.

`Transaction` (from `models.py`):

```
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
- Dates MUST be `DD/MM/YYYY`.
- Amounts MUST be comma-separated strings.
- Detection rule order in `factory.py`'s `_BANK_DETECTION_RULES` matters.
- `BankOption`, `PARSER_REGISTRY`, and `list_bank_choices()` must stay in sync (asserted by `tests/test_contracts.py`).

## Gotchas

- **Try generic first.** `GenericParser` already handles standard `DD/MM/YYYY` layouts and exposes shared date/amount/credit hooks. Only write a full custom parser after generic provably fails.
- **Header-region detection.** `detect_bank()` keys off the first-page header lines plus the filename basename, not full body text. Merchant rows often mention other banks. Match the uppercased header content with `header_tokens`; supply a separate `file_tokens` tuple if the filename uses a different abbreviation (e.g., `AXIS BANK` header vs `AXIS` filename).
- **Detection order matters.** Current ordering in `_BANK_DETECTION_RULES`: indusind → axis → equitas → icici → hdfc → hsbc → jupiter → sbi → idfc → slice → ssfb → bob → yesbank. Reasons (do not reorder without reading factory.py comments): IndusInd before ICICI ("ICICI Lombard" appears in IndusInd fine print); `AXIS BANK` (header) vs bare `AXIS` (filename) avoids "TAXATION"; HSBC / Jupiter before SBI (substring collisions); YES BANK ahead of generic.
- **Date helpers.** Use `tokens.py`: `parse_date`, `parse_date_value`, `parse_date_token`, `parse_multi_token_date`, `normalize_date_long`, with `DateParseHints` for non-standard formats. Output stays `DD/MM/YYYY`. Anything new must keep working under Pyodide (`python-dateutil` is available there — keep it that way).
- **Amount helpers.** Use `parse_amount_token` / `normalize_amount` / `parse_amount` / `format_amount` so currency markers (`` ` ``) and commas are handled consistently.
- **Bound summary parsing to early pages.** Several issuers (Equitas, sometimes others) include MITC/illustration pages with fake totals and dates. Restrict due-date and total-due extraction to page 1 (or to the explicit summary region) rather than whole-document regex.
- **Do not conflate current due with total outstanding.** Loan/EMI statements can show future principal in `Net outstanding balance`. For HSBC, `statement_total_amount_due` is the page-1 `Total payment due`; if dark-cell labels are not extractable, use the amount sharing the statement-period visual row, never purchase/net/loan outstanding as a guess.
- **Do not fabricate reward allocation.** Some HSBC layouts declare only aggregate opening/earned/redeemed/closing points. Normalize earned/closing as point counts, keep transaction points empty, and copy earned points into card/person rollups only when exactly one card/person group makes ownership unambiguous.
- **Preserve EMI continuation semantics.** HSBC may print `NTH OF N INSTALLMENTS PRINCIPAL|INTEREST` below both an internal `CR` transfer and its matching billed debit. Merge that subline into narration. Keep the credit in parsed output, but when an exact debit counterpart confirms it is an internal transfer, exclude it transparently from smart payable math and refund pairing rather than dropping or reclassifying the row.
- **Credit classification is the hardest part.** Per-row Cr/Dr markers, section headers, bare C/D (SBI), CR-only on credits (HSBC), `CR`/`DR` glued onto the amount token (Axis/BOB) — every bank is different. Study how the closest existing parser handles this.
- **Multi-card statements.** Primary + add-on cards appear as sections with member headers. Group transactions by person, build `card_summaries` and `person_groups`. Use `ADDON <last 4 digits>` if names can't be extracted (per AGENTS.md).
- **Reconciliation is observability.** Don't coerce totals. Non-zero `smart_delta` means the parser missed something — investigate before merging.
- **Privacy.** Never commit real PDFs. Never hardcode customer names, card numbers, or amounts.

## Self-improvement

If you discover new patterns or formats, update this skill **and**:
- `AGENTS.md` if the consumer contract, detection order, or change workflow shifts.
- `README.md` and `docs/parsing-notes.md` per AGENTS Change Workflow.
- `pyproject.toml` and `web/worker.js` together when adding a new pure-Python dependency.
