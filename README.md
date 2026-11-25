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

Each scanner run also saves its V1 and V2 ranks in the `momentum_rank_history` table in the same database. This preserves daily rank snapshots for later comparison with future prices. A fresh database gets its first snapshot the next time you run the scanner; past rankings cannot be recovered unless you have their CSV files.

The scanner evaluates saved rankings after 1, 5, 20, and 60 trading sessions and stores results in `rank_forward_outcomes` and `rank_evaluation_runs`. A stock is correct when its adjusted close after the selected horizon is higher than its close on the rank date; flat or lower is incorrect. The dashboard reports counts and hit rates across all ranked stocks, with a separate top-10% summary. A horizon remains pending until enough future prices are available.

To reconstruct historical rankings from cached daily prices and backtest them, run:

```bash
python3 backtest_momentum_history.py --days 30
```

This command uses the same V2 scoring function as the scanner and does not download prices. It reconstructs the historical universe from symbols in the database with at least 200 traded sessions; exact past NSE membership was not archived. Increase `--days` to rebuild a longer history; longer forward horizons need older rank dates to have enough later trading sessions.

## Open the local dashboard

Start the dashboard:

```bash
python3 momentum_dashboard.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765) in a browser. The dashboard reads the latest V2 rank snapshot from the `momentum_rank_history` table in `nse_stock_prices.sqlite3`. It does not read momentum CSV files. Run the scanner to save a new snapshot, then refresh the page.

The table supports searching, filtering by momentum status and market-cap category, and sorting by any column. The dashboard always shows the latest V2 snapshot; it has no CSV input option.

The scanner imports market-cap categories into the database from [`amfi_market_cap_2026-06.csv`](amfi_market_cap_2026-06.csv). Categories come from AMFI's six-month average market capitalization list dated June 30, 2026. Symbols without a match are shown as `Unclassified`. See [AMFI's stock categorisation page](https://www.amfiindia.com/otherdata/categorisation-of-stocks) for the source list. The included classification file is a dated snapshot and does not update automatically.

## Other files

- `print_sorted_momentum.py` — prints momentum results in ranked order.
- `new_code_transcipt_load_only.py` — downloads/caches NSE stock prices and generates momentum reports.
- `momentum_dashboard.py` and `momentum_dashboard.html` — local SQLite-backed web dashboard with ranking correctness results.

Generated price databases and momentum CSV/XLSX outputs are local data files and are excluded from Git.
