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

The scanner evaluates saved rankings after 1, 5, 20, and 60 trading sessions and stores results in `rank_forward_outcomes` and `rank_evaluation_runs`. A stock is correct when its adjusted close after the selected horizon is higher than the close used to calculate its signal; lower is incorrect and unchanged is neutral. Hit rate excludes neutral outcomes. The dashboard reports counts and provides a searchable row for every stock in each completed V2 snapshot, including rank, signal close, later close, price change, and result. A horizon remains pending until enough future prices are available.

To reconstruct up to one year of historical rankings from cached daily prices and backtest them, run:

```bash
python3 backtest_momentum_history.py --days 365
```

This command uses the scanner's V2 scoring rules and does not download prices. Before writing, it compares reconstructed current V2 scores with the saved current snapshot and stops if they differ. Historical ranks can only be created for dates with at least 200 prior traded sessions; exact past NSE membership was not archived. Increase `--days` to use a longer calendar lookback, but that cannot create ranks for dates lacking enough cached history.

With the current cache (prices from October 20, 2025 through November 23, 2026), the backfill creates 70 trading-day rank snapshots from August 12 through November 23, 2026. Earlier dates are skipped because the cache does not yet contain 200 prior trading sessions for eligible stocks. Rebuilding a full year of ranks requires older daily price history before the target year, plus historical NSE listing data for the exact universe.

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
