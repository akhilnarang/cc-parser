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

- `cc_parser/parsers/registry.py`
  - ordered parser registry used by CLI/browser surfaces and factory instantiation.

- `cc_parser/parsers/factory.py`
  - bank detection heuristics and dispatch,
  - detection order comments are part of the compatibility contract,
  - auto-detection should prefer first-page header branding plus filename over full statement body text.

- `cc_parser/parsers/registry.py`
  - canonical parser registry used by the CLI/browser/factory.

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

- `cc_parser/parsers/slice.py`
  - Slice profile wrapper (multi-token dates, bare C/D markers).

- `cc_parser/parsers/kotak.py`
  - Kotak Mahindra Bank extraction (DD-Mon-YYYY dates, TAD/MAD summary, transaction parsing).

- `cc_parser/parsers/ssfb.py`
  - Suryoday SFB extraction (page-1 bounded summary parsing, DD-Mon-YYYY normalization, MITC/example-page isolation).

- `cc_parser/parsers/yesbank.py`
  - YES BANK extraction (statement-details bounded transaction parsing, Dr/Cr markers, summary-row rejection).

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

1. Default to extending `GenericParser`; override only the stages that differ (`_extract_transactions_with_debug`, `_extract_summary`, `_extract_due_date`, `_extract_total_amount_due`, `_extract_card_number`, `_extract_name`, or the lower-level parsing hooks).
2. Register parser classes in `cc_parser/parsers/registry.py` and keep `factory.py` detection order comments accurate.
   Detection should key off page-1 header/branding before broader transaction text.
3. Preserve output schema compatibility.
4. Date outputs must stay `DD/MM/YYYY`; use `cc_parser/parsers/tokens.py` dateutil-backed helpers instead of ad hoc `strptime` logic.
5. Validate with `uv run cc-parser ... -vvv` and inspect `debug` deltas.
6. Update docs (`README.md`, `docs/parsing-notes.md`, `.agents/skills/add-bank-parser/SKILL.md`) when behavior changes.
7. Ensure code compiles (`uv run python -m py_compile ...`).

## Coding Conventions

- This repo targets Python 3.14; PEP 758 parenthesis-free `except E1, E2:` syntax is allowed here — do not rewrite it just for style.
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
- **Detection scope matters too**: Auto-detection should prefer first-page header/branding and filename. Whole-statement text can contain merchant rows mentioning other banks.
- **Registry order matters too**: CLI/browser bank lists come from `parsers/registry.py`; keep that order stable unless you intentionally change user-facing surfaces and tests.
- **Pyodide package changes are coupled**: if parser imports need new pure-Python deps, update both `pyproject.toml` and `web/worker.js`.

## Known limitations

- **No OCR**: Only PDFs with a text layer. Scanned image-only PDFs produce empty/garbled output.
- **Add-on card naming is heuristic**: Falls back to `ADDON <last 4 digits>` when names can't be extracted.
- **Summary row rejection**: Recently added filter may undercount in edge cases where a real transaction resembles a summary line.
- **Auto-detection can still misclassify**: Header text and filename reduce false positives, but unusual templates or missing branding can still route to the wrong parser.
