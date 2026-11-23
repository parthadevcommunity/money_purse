# Money Purse: NSE Momentum Scanner

This repository contains a momentum scanner for NSE-listed stocks, a local CSV dashboard, and related data utilities.

## Requirements

- Python 3.10 or newer
- The packages listed in [`requirements.txt`](requirements.txt)
- Internet access when running the scanner to retrieve the NSE symbol list and Yahoo Finance price history

Install the dependencies from the project directory:

```bash
python3 -m pip install -r requirements.txt
```

## Refresh the momentum CSV files

Run the scanner:

```bash
python3 new_code_transcipt_load_only.py
```

The scanner reads the NSE equity symbol list, updates the SQLite price cache at `nse_stock_prices.sqlite3`, calculates momentum, and writes its CSV/XLSX output files, including `NSE_All_Stocks_Momentum_V2.csv`.

Price history is cached in the database. A symbol already checked on the current date is skipped. On a later date, the scanner refreshes recent history with a seven-day overlap; a symbol with no cached prices starts with about 400 calendar days of history. This overlap allows recent price corrections to be applied.

## Open the local dashboard

Start the dashboard:

```bash
python3 momentum_dashboard.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765) in a browser. The dashboard reads `NSE_All_Stocks_Momentum_V2.csv` from this directory. It does not download prices or read the SQLite database. Refresh the page after rerunning the scanner.

The table supports searching, filtering by momentum status and market-cap category, and sorting by any column. To display a different CSV:

```bash
python3 momentum_dashboard.py path/to/another.csv
```

The dashboard adds a `Market_Cap` column by matching the CSV's `Stock` symbols to [`amfi_market_cap_2026-06.csv`](amfi_market_cap_2026-06.csv). Categories come from AMFI's six-month average market capitalization list dated June 30, 2026. Symbols without a match are shown as `Unclassified`. See [AMFI's stock categorisation page](https://www.amfiindia.com/otherdata/categorisation-of-stocks) for the source list. The included classification file is a dated snapshot and does not update when the scanner runs.

## Other files

- `print_sorted_momentum.py` — prints momentum results in ranked order.
- `new_code_transcipt_load_only.py` — downloads/caches NSE stock prices and generates momentum reports.
- `momentum_dashboard.py` and `momentum_dashboard.html` — local CSV web dashboard.

Generated price databases and momentum CSV/XLSX outputs are local data files and are excluded from Git.
