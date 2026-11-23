"""Print stock names grouped by momentum status, strongest status first."""

import argparse
from pathlib import Path

import pandas as pd


DEFAULT_CSV = "NIFTY100_BSE500_Momentum.csv"


def main():
    parser = argparse.ArgumentParser(
        description="Print stock names ordered by Momentum_Status."
    )
    parser.add_argument(
        "csv_file",
        nargs="?",
        default=DEFAULT_CSV,
        help=f"CSV file to read (default: {DEFAULT_CSV})",
    )
    args = parser.parse_args()

    csv_path = Path(args.csv_file)
    if not csv_path.is_file():
        raise SystemExit(f"CSV file not found: {csv_path}")

    results = pd.read_csv(csv_path)
    required_columns = {"Stock", "Momentum_Status"}
    if not required_columns.issubset(results.columns):
        raise SystemExit(
            f"{csv_path} must contain 'Stock' and 'Momentum_Status' columns."
        )

    status_order = {
        "Strong Bullish": 0,
        "Moderate Bullish": 1,
        "Weak / Mixed": 2,
    }
    results["_status_order"] = results["Momentum_Status"].map(status_order).fillna(3)

    # Keep each status group in the scanner's original rank order. Use score
    # as the fallback tie-breaker if the CSV has no Rank column.
    if "Rank" in results.columns:
        results["_within_status_order"] = pd.to_numeric(
            results["Rank"], errors="coerce"
        )
    elif "Momentum_Score" in results.columns:
        results["_within_status_order"] = -pd.to_numeric(
            results["Momentum_Score"], errors="coerce"
        )
    else:
        results["_within_status_order"] = range(len(results))

    results = results.sort_values(
        ["_status_order", "_within_status_order"],
        na_position="last",
        kind="stable",
    )

    for rank, stock in enumerate(results["Stock"], start=1):
        print(f"{rank:>3}. {stock}")


if __name__ == "__main__":
    main()
