"""CLI entrypoint for statement parsing.

Workflow:
1) extract raw PDF structure,
2) select parser profile,
3) produce compact/raw/very-raw output,
4) print rich tables for quick inspection.
"""

import getpass
import csv
import json
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table
import typer

from cc_parser.extractor import extract_raw_pdf, is_pdf_encrypted
from cc_parser.parsers.factory import detect_bank, get_parser
from cc_parser.parsers.models import ParsedStatement, Transaction
from cc_parser.parsers.tokens import parse_amount


class BankOption(StrEnum):
    auto = "auto"
    icici = "icici"
    hdfc = "hdfc"
    sbi = "sbi"
    idfc = "idfc"
    indusind = "indusind"
    hsbc = "hsbc"
    axis = "axis"
    jupiter = "jupiter"
    slice = "slice"
    bob = "bob"
    generic = "generic"


def _has_visible_rewards(rows: list[Transaction]) -> bool:
    """Return True if at least one row has non-zero/non-empty reward points.

    Args:
        rows: Transaction rows.

    Returns:
        True when reward column should be shown.
    """
    for row in rows:
        value = str(row.reward_points or "").strip()
        if not value:
            continue
        if value in {"0", "0.0", "0.00"}:
            continue
        return True
    return False


def write_transactions_csv(parsed: ParsedStatement, output_path: Path) -> None:
    """Write flattened transaction rows for spreadsheet analysis.

    Args:
        parsed: Parsed statement model.
        output_path: Destination CSV path.

    Returns:
        None.
    """
    fieldnames = [
        "bank",
        "file",
        "source",
        "transaction_type",
        "adjustment_side",
        "person",
        "card_number",
        "date",
        "time",
        "narration",
        "reward_points",
        "amount",
        "amount_numeric",
        "signed_amount",
        "spend_amount",
        "credit_amount",
    ]

    rows: list[dict[str, str]] = []
    bank = parsed.bank or ""
    file_name = parsed.file or ""

    def add_row(source: str, txn: Transaction) -> None:
        amount_text = str(txn.amount or "0")
        amount_decimal = parse_amount(amount_text)
        txn_type = str(txn.transaction_type or "")
        adj_side = str(txn.adjustment_side or "")

        is_credit = (
            source == "payments_refunds" or adj_side == "credit" or txn_type == "credit"
        )
        signed = -amount_decimal if is_credit else amount_decimal
        spend_amount = amount_decimal if source == "transactions" else parse_amount("0")
        credit_amount = amount_decimal if is_credit else parse_amount("0")

        rows.append(
            {
                "bank": bank,
                "file": file_name,
                "source": source,
                "transaction_type": txn_type,
                "adjustment_side": adj_side,
                "person": str(txn.person or ""),
                "card_number": str(txn.card_number or ""),
                "date": str(txn.date or ""),
                "time": str(txn.time or ""),
                "narration": str(txn.narration or ""),
                "reward_points": str(txn.reward_points or ""),
                "amount": amount_text,
                "amount_numeric": f"{amount_decimal:.2f}",
                "signed_amount": f"{signed:.2f}",
                "spend_amount": f"{spend_amount:.2f}",
                "credit_amount": f"{credit_amount:.2f}",
            }
        )

    for txn in parsed.transactions:
        add_row("transactions", txn)
    for txn in parsed.payments_refunds:
        add_row("payments_refunds", txn)
    for txn in parsed.adjustments:
        add_row("adjustments", txn)

    with output_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def print_compact_table(output_data: ParsedStatement) -> None:
    """Render parsed output as Rich tables.

    Args:
        output_data: Parsed statement model.

    Returns:
        None. Writes tables to stdout.
    """
    console = Console()
    name = output_data.name or "-"
    card_number = output_data.card_number or "-"
    due_date = output_data.due_date or "-"
    bank = output_data.bank or "-"
    transactions = output_data.transactions
    person_groups = output_data.person_groups
    payments_refunds = output_data.payments_refunds
    payments_refunds_total = output_data.payments_refunds_total or "0.00"
    adjustments = output_data.adjustments
    adjustments_debit_total = output_data.adjustments_debit_total or "0.00"
    adjustments_credit_total = output_data.adjustments_credit_total or "0.00"
    card_summaries = output_data.card_summaries
    overall_total = output_data.overall_total or "0.00"
    overall_reward_points = output_data.overall_reward_points or "0"
    statement_total_amount_due = output_data.statement_total_amount_due or "-"
    reconciliation = output_data.reconciliation

    console.print(f"Bank: {bank}")
    console.print(f"Name: {name}")
    console.print(f"Card: {card_number}")
    console.print(f"Due Date: {due_date}")
    console.print(f"Statement Total Amount Due: {statement_total_amount_due}")

    if payments_refunds:
        credit_table = Table(title="Payments / Refunds (Credit Transactions)")
        credit_table.add_column("Date", style="cyan", no_wrap=True)
        credit_table.add_column("Time", style="cyan", no_wrap=True)
        credit_table.add_column("Person", style="white", no_wrap=True)
        credit_table.add_column("Narration", style="white")
        credit_table.add_column("Amount", justify="right", style="magenta")

        for txn in payments_refunds:
            credit_table.add_row(
                str(txn.date or ""),
                str(txn.time or ""),
                str(txn.person or ""),
                str(txn.narration or ""),
                str(txn.amount or ""),
            )

        console.print(credit_table)
        console.print(f"Payments/Refunds Total: {payments_refunds_total}")

    if adjustments:
        adj_table = Table(title="Adjustments (Offsetting Debit/Credit Pairs)")
        adj_table.add_column("Date", style="cyan", no_wrap=True)
        adj_table.add_column("Side", style="yellow", no_wrap=True)
        adj_table.add_column("Person", style="white", no_wrap=True)
        adj_table.add_column("Narration", style="white")
        adj_table.add_column("Amount", justify="right", style="magenta")
        for txn in adjustments:
            adj_table.add_row(
                str(txn.date or ""),
                str(txn.adjustment_side or ""),
                str(txn.person or ""),
                str(txn.narration or ""),
                str(txn.amount or ""),
            )
        console.print(adj_table)
        console.print(
            f"Adjustments totals -> Debits: {adjustments_debit_total} | Credits: {adjustments_credit_total}"
        )

    if person_groups:
        for group in person_groups:
            person = group.person or "UNKNOWN"
            group_rows = group.transactions
            show_reward_col = _has_visible_rewards(group_rows)
            table = Table(title=f"Transactions - {person}")
            table.add_column("Date", style="cyan", no_wrap=True)
            table.add_column("Time", style="cyan", no_wrap=True)
            table.add_column("Narration", style="white")
            if show_reward_col:
                table.add_column("Reward", justify="right", style="green")
            table.add_column("Amount", justify="right", style="magenta")

            for txn in group_rows:
                cells = [
                    str(txn.date or ""),
                    str(txn.time or ""),
                    str(txn.narration or ""),
                ]
                if show_reward_col:
                    cells.append(str(txn.reward_points or ""))
                cells.append(str(txn.amount or ""))
                table.add_row(*cells)

            console.print(table)
            console.print(
                f"{person} totals -> Amount: {group.total_amount or '0.00'} | "
                f"Points: {group.reward_points_total or '0'}"
            )
    else:
        show_reward_col = _has_visible_rewards(transactions)
        table = Table(title="Transactions")
        table.add_column("Date", style="cyan", no_wrap=True)
        table.add_column("Time", style="cyan", no_wrap=True)
        table.add_column("Person", style="white", no_wrap=True)
        table.add_column("Narration", style="white")
        if show_reward_col:
            table.add_column("Reward", justify="right", style="green")
        table.add_column("Amount", justify="right", style="magenta")

        for txn in transactions:
            cells = [
                str(txn.date or ""),
                str(txn.time or ""),
                str(txn.person or ""),
                str(txn.narration or ""),
            ]
            if show_reward_col:
                cells.append(str(txn.reward_points or ""))
            cells.append(str(txn.amount or ""))
            table.add_row(*cells)

        console.print(table)

    summary_table = Table(title="Totals by Person/Card")
    summary_table.add_column("Person", style="white")
    summary_table.add_column("Card", style="white")
    summary_table.add_column("Txn Count", justify="right", style="cyan")
    summary_table.add_column("Points", justify="right", style="green")
    summary_table.add_column("Total", justify="right", style="magenta")

    for row in card_summaries:
        summary_table.add_row(
            str(row.person or ""),
            str(row.card_number or ""),
            str(row.transaction_count),
            str(row.reward_points_total or "0"),
            str(row.total_amount or "0.00"),
        )

    console.print(summary_table)
    console.print(f"Spend Total (debits only): {overall_total}")
    console.print(f"Reward Points (debits only): {overall_reward_points}")
    if output_data.reward_points_balance:
        console.print(f"Reward Points Balance: {output_data.reward_points_balance}")

    recon_table = Table(title="Reconciliation")
    recon_table.add_column("Metric", style="white")
    recon_table.add_column("Value", style="magenta")
    recon_table.add_row(
        "Statement Total Amount Due",
        str(reconciliation.statement_total_amount_due or ""),
    )
    recon_table.add_row(
        "Previous Balance",
        str(reconciliation.header_previous_balance or ""),
    )
    recon_table.add_row(
        "Parsed Debit Total",
        str(reconciliation.parsed_debit_total or ""),
    )
    recon_table.add_row(
        "Parsed Credit Total",
        str(reconciliation.parsed_credit_total or ""),
    )
    recon_table.add_row(
        "Smart Expected Total (prev + debits + fees - credits)",
        str(reconciliation.smart_expected_total or ""),
    )
    recon_table.add_row(
        "Smart Delta (statement - expected)",
        str(reconciliation.smart_delta or ""),
    )
    if reconciliation.prev_balance_cleared_date:
        recon_table.add_row(
            "Previous Balance Cleared On",
            str(reconciliation.prev_balance_cleared_date),
        )
        recon_table.add_row(
            "Excess Paid After Clearing",
            str(reconciliation.excess_paid_after_clearing or "0.00"),
        )
    recon_table.add_row(
        "Delta (Statement - Net)",
        str(reconciliation.delta_statement_vs_parsed_net or ""),
    )
    console.print(recon_table)


def extract_with_password_prompt(
    pdf_path: Path, include_blocks: bool
) -> dict[str, Any]:
    """Extract raw PDF data, prompting for password when required.

    Args:
        pdf_path: Path to input PDF.
        include_blocks: Whether to include PyMuPDF text blocks.

    Returns:
        Raw extraction payload.

    Raises:
        ValueError: If decryption fails after retry attempts.
    """
    if not is_pdf_encrypted(pdf_path):
        return extract_raw_pdf(pdf_path, include_blocks=include_blocks, password=None)

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        password = getpass.getpass("PDF is encrypted. Enter password: ")
        try:
            return extract_raw_pdf(
                pdf_path,
                include_blocks=include_blocks,
                password=password,
            )
        except ValueError as error:
            if "Failed to decrypt PDF" in str(error) and attempt < max_attempts:
                print(f"Incorrect password ({attempt}/{max_attempts}). Try again.")
                continue
            raise

    raise ValueError("Failed to decrypt PDF.")


def parse_statement(
    pdf: Path = typer.Argument(..., help="Path to the PDF file"),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Output JSON path when using -v/-vv/-vvv",
    ),
    export_csv: Path | None = typer.Option(
        None,
        "--export-csv",
        help="Write flattened transaction CSV for spreadsheet analysis",
    ),
    export_json: Path | None = typer.Option(
        None,
        "--export-json",
        help="Write parsed JSON (same shape as -v)",
    ),
    export_raw_json: Path | None = typer.Option(
        None,
        "--export-raw-json",
        help="Write raw extractor JSON (pages/words/tables/metadata)",
    ),
    skip_blocks: bool = typer.Option(
        False,
        "--skip-blocks",
        help="Skip PyMuPDF block extraction to keep output smaller",
    ),
    verbose: int = typer.Option(
        0,
        "-v",
        count=True,
        help="Write JSON output (-v parsed, -vv parsed+debug, -vvv parsed+debug+raw)",
    ),
    bank: BankOption = typer.Option(
        BankOption.auto,
        "--bank",
        help="Force parser selection (default: auto)",
    ),
) -> None:
    """Parse a statement PDF and print normalized tables.

    Args:
        pdf: Path to input PDF file.
        output: Optional output path for JSON when verbosity is enabled.
        export_csv: Optional CSV export path for flattened transaction rows.
        export_json: Optional parsed JSON export path.
        export_raw_json: Optional raw extractor JSON export path.
        skip_blocks: Skip PyMuPDF block extraction to reduce payload size.
        verbose: Verbosity count (`-v`, `-vv`, `-vvv`) controlling JSON output.
        bank: Parser profile (`auto`, `icici`, `hdfc`, `sbi`, `idfc`, `indusind`, `hsbc`, `axis`, `jupiter`, `slice`, `bob`, `generic`).

    Returns:
        None. Prints summary tables and optionally writes JSON.
    """
    if not pdf.exists():
        raise typer.BadParameter(f"File not found: {pdf}")
    if pdf.suffix.lower() != ".pdf":
        raise typer.BadParameter("Input must be a .pdf file")

    try:
        raw_data = extract_with_password_prompt(
            pdf,
            include_blocks=not skip_blocks,
        )
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error

    parser_impl = get_parser(bank.value, raw_data)
    parsed = parser_impl.parse(raw_data)

    print_compact_table(parsed)
    typer.echo(f"Transactions: {len(parsed.transactions)}")

    if verbose > 0:
        output_path: Path = output or (Path.cwd() / f"run_{uuid.uuid7().hex}.json")
        parsed_dict = parsed.model_dump()
        parsed_dict["bank_detected"] = detect_bank(raw_data)
        parsed_dict["bank_parser"] = parser_impl.bank

        if verbose >= 3:
            output_obj: Any = {
                "parsed": parsed_dict,
                "debug": parser_impl.build_debug(raw_data),
                "raw": raw_data,
            }
        elif verbose == 2:
            output_obj = {
                "parsed": parsed_dict,
                "debug": parser_impl.build_debug(raw_data),
            }
        else:
            output_obj = parsed_dict

        output_path.write_text(
            json.dumps(output_obj, indent=2, ensure_ascii=True), encoding="utf-8"
        )
        typer.echo(f"Wrote extraction to {output_path}")

    if export_json is not None:
        parsed_dict = parsed.model_dump()
        parsed_dict["bank_detected"] = detect_bank(raw_data)
        parsed_dict["bank_parser"] = parser_impl.bank
        export_json.write_text(
            json.dumps(parsed_dict, indent=2, ensure_ascii=True), encoding="utf-8"
        )
        typer.echo(f"Wrote parsed JSON to {export_json}")

    if export_raw_json is not None:
        export_raw_json.write_text(
            json.dumps(raw_data, indent=2, ensure_ascii=True), encoding="utf-8"
        )
        typer.echo(f"Wrote raw JSON to {export_raw_json}")

    if export_csv is not None:
        write_transactions_csv(parsed, export_csv)
        typer.echo(f"Wrote CSV to {export_csv}")


def main() -> None:
    """Program entrypoint for console script execution.

    Returns:
        None.
    """
    typer.run(parse_statement)


if __name__ == "__main__":
    main()
