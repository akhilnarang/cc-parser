# AGENTS

Guidance for contributors and coding agents working on this repository.

## Project Purpose

`cc-parser` parses credit card statement PDFs into normalized, comparable output.

Primary goals:

- robust extraction from noisy PDFs,
- stable schema across bank templates,
- clear reconciliation signals,
- privacy-safe development (no account-specific hardcoding).

## High-Level Architecture

- `cc_parser/cli.py`
  - command entrypoint,
  - password prompt flow,
  - parser selection,
  - Rich table presentation,
  - optional JSON export via `-v/-vv/-vvv`.

- `cc_parser/extractor.py`
  - bank-agnostic raw extraction,
  - encryption detection/decryption,
  - page text/words/tables/blocks and metadata.

- `cc_parser/parsers/base.py`
  - `StatementParser` interface.

- `cc_parser/parsers/factory.py`
  - bank detection and parser dispatch.

- `cc_parser/parsers/generic.py`
  - shared parsing pipeline,
  - transaction extraction,
  - credit/debit split,
  - points/totals/reconciliation helpers.

- `cc_parser/parsers/hdfc.py`
  - HDFC profile wrapper (extend here for HDFC-specific behavior).

- `cc_parser/parsers/icici.py`
  - ICICI-specific normalization (especially add-on grouping labels).

- `cc_parser/parsers/sbi.py`
  - SBI-specific extraction (multi-token dates, bare C/D markers, account summary).

- `cc_parser/parsers/idfc.py`
  - IDFC FIRST Bank extraction (DD Mon YY dates, DR/CR markers, r-prefixed amounts, statement summary).

## Parser Contract

All parsers must implement `StatementParser.parse(raw_data)` and return the same compact shape.

Required top-level fields:

- `file`
- `bank`
- `name`
- `card_number`
- `due_date`
- `statement_total_amount_due`
- `transactions` (debits)
- `payments_refunds` (credits)
- `card_summaries`
- `person_groups`
- `overall_total`
- `overall_reward_points`
- `reconciliation`

## Output Modes

- default run: prints tables only, no JSON file.
- `-v`: writes parsed compact JSON.
- `-vv`: writes `{ parsed, debug }`.
- `-vvv`: writes `{ parsed, debug, raw }`.

## Add-on Labeling Rule

If add-on holder names are not confidently extractable, use:

- `ADDON <last 4 digits of addon>`

Do not use static examples or account-specific labels.

## Classification and Reconciliation Principles

- Use structural evidence first (columns, markers like `CR`, section layout).
- Avoid brittle account-specific heuristics.
- Keep debits and credits separate in output.
- Treat reconciliation as observability; do not silently coerce totals.

## Privacy and Safety Rules

- Never commit real statement PDFs or raw personal data.
- Never add sample values copied from real statements.
- Keep logs and docs generic and template-focused.
- Do not hardcode customer-specific names, card numbers, addresses, or amounts.

## Change Workflow

When modifying parser logic:

1. Keep bank-specific behavior in bank parser modules.
2. Preserve output schema compatibility.
3. Validate with `-vvv` output and inspect `debug` deltas.
4. Update docs (`README.md`, `docs/parsing-notes.md`) when behavior changes.
5. Ensure code compiles (`uv run python -m py_compile ...`).

## Coding Conventions

- Use typed Python signatures.
- Add docstrings with `Args` and `Returns` for non-trivial functions.
- Prefer pure helper functions for parsing steps.
- Keep CLI presentation logic out of parser core logic.

## Non-Goals

- OCR model training,
- guaranteed perfect reconciliation for every issuer template,
- storing statement data in this repository.

## Consumer contract

`bank-email-fetcher` uses this library programmatically. These are downstream-breaking if changed:

- **Date format is DD/MM/YYYY**: `bank-email-fetcher` parses with `strptime(date, "%d/%m/%Y")`.
- **Amount strings are comma-separated**: Expects `"25,000.00"`, strips commas to convert to Decimal.
- **Detection order matters**: In `factory.py`, IndusInd before ICICI, HSBC/Jupiter before SBI. Wrong order causes misclassification.

## Known limitations

- **No OCR**: Only PDFs with a text layer. Scanned image-only PDFs produce empty/garbled output.
- **Add-on card naming is heuristic**: Falls back to `ADDON <last 4 digits>` when names can't be extracted.
- **Summary row rejection**: Recently added filter may undercount in edge cases where a real transaction resembles a summary line.
- **Auto-detection can misclassify**: If statement text mentions another bank (e.g., payment to HDFC in an ICICI statement), the auto-detector may pick the wrong parser.
