## cc-parser

I wanted to build this for a while because I had been manually copy pasting credit card statements into Google Sheet for some calculations and that was tedious (first had to copy into a text editor, add some separators, then into a sheet, etc). Delayed it for ages because I didn't want to write PDF parsing code, but Codex and Claude handled for me this time.

It can handle most bank's card statements, if anything doesn't seem to work, please raise an issue.

There's also a [web version](https://cc-statement-parser.akhilnarang.dev) that runs entirely in your browser using Pyodide (Python compiled to WebAssembly). No server is involved — your statements never leave your machine. Features:
- Upload PDFs via drag-and-drop or file picker
- Auto-detects bank and handles encrypted PDFs (password cached per session)
- Stores parsed statements in browser storage (IndexedDB) for multi-statement analysis
- Dashboard with spend-by-month charts, spend-by-person/card breakdowns, and reward points tracking
- Export individual or all stored statements as JSON/CSV
- Works on mobile and desktop, with dark/light mode support

Default output extracts:
- name
- input file name
- masked card number
- due date
- statement total amount due
- debit transactions (`date`, `time`, `narration`, `reward_points`, `amount`, `person`, `card_number`)
- payments/refunds (credit transactions) in a separate list
- totals by person/card (`transaction_count`, `total_amount`, `reward_points_total`)
- person groups with totals
- overall debit spend total and overall reward points
- reconciliation metrics (smart delta, previous balance clearing date, excess payments)

Regular run prints tables to terminal and does not write JSON.

Use verbosity flags to write JSON - the use case is basically for adding support for new cards.
- `-v`: parsed compact output
- `-vv`: `{ parsed, debug }`
- `-vvv`: `{ parsed, debug, raw }`

If `-o/--output` is not provided, JSON is written as `$PWD/run_<uuid7hex>.json`.

Privacy note:
- `-v`, `-vv`, `-vvv`, `--export-json`, `--export-raw-json`, and `--export-csv` write statement-derived data to disk.
- `-vvv` and `--export-raw-json` include page text, word coordinates, metadata, and other high-sensitivity content.
- Generated exports, PDFs, and agent scratch files are gitignored in this repo; do not override that by force-adding them.
- Share only redacted outputs outside your local machine.

## Usage

```bash
uv run cc-parser /path/to/statement.pdf
```

Optional flags:

```bash
uv run cc-parser /path/to/statement.pdf --skip-blocks -v -o output.json
```

Spreadsheet-friendly CSV export:

```bash
uv run cc-parser /path/to/statement.pdf --export-csv output.csv
```

For Google Sheets per-addon/person spend totals, filter `source = transactions` and sum `spend_amount` grouped by `person` (or `person + card_number`).

Direct JSON exports (without using `-v`):

```bash
uv run cc-parser /path/to/statement.pdf --export-json parsed.json --export-raw-json raw.json
```

Parser selection (default: `auto`):

```bash
uv run cc-parser /path/to/statement.pdf --bank hdfc
```

Supported banks: `icici`, `hdfc`, `sbi`, `idfc`, `indusind`, `hsbc`, `axis`, `jupiter`, `slice`, `bob`, `generic`.

Extra debug bundle (local troubleshooting only; redact before sharing):

```bash
uv run cc-parser /path/to/statement.pdf -vvv
```

Encrypted PDFs are auto-detected. The CLI prompts for password interactively.

Note: some PDFs may print warnings like `ignore '/Perms' verify failed`; parsing can still succeed.

The CLI prints Rich tables in compact mode:
- Payments / Refunds (if present)
- Transactions grouped by person
- Totals by person/card with points
- Reconciliation summary

Bank-specific parsing logic is under `cc_parser/parsers/` and all parsers implement a shared interface in `cc_parser/parsers/base.py`.

Detailed parsing notes are in `docs/parsing-notes.md`.

Output schema reference is in `docs/output-schema.md`.

Contributor/agent guidance is in `AGENTS.md`.

ICICI add-on handling:
- if explicit add-on holder names are not reliably extractable, output uses stable labels in the format `ADDON <last 4 digits of addon>`.
