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

The scanner saves a `Sector` column for each stock, based on Yahoo Finance company classification, and caches sector classifications in `stock_classifications`. It also saves daily benchmark closes in `index_daily_prices` for comparison with each stock. Large-cap stocks use Nifty 100, mid-cap stocks use Nifty Midcap 150, and small-cap stocks use an ETF tracking Nifty Smallcap 250 (the direct index history is not consistently available from Yahoo Finance). Sector classifications are mapped to corresponding Yahoo Finance NSE sector-index tickers; this is a broad mapping and can be missing for some stocks.

The scanner evaluates saved rankings after 1, 5, 20, and 60 trading sessions and stores results in `rank_forward_outcomes` and `rank_evaluation_runs`. For V2 scores of 75 or higher, a rise is correct, a fall is incorrect, and a flat return is neutral. Below 75, a stock is correct only if its return is lower than both its mapped sector index and market-cap index; a flat stock return is neutral. If required benchmark prices or mappings are unavailable, the result is marked unavailable and excluded from the hit rate. The dashboard's correctness view can filter rank dates by a start/end range and filter or search results by sector; it summarizes correct, incorrect, neutral, and unavailable outcomes for each stock over that range. A horizon remains pending until enough future prices are available.

Sector and benchmark data refresh when the scanner runs. Yahoo Finance ticker coverage can be incomplete. Sector membership and AMFI market-cap classification are not archived by effective date, so historical evaluations apply the currently stored classification to old snapshots. Existing V2 evaluation rows are rebuilt once after this rule update.

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

## Publish rank snapshots with GitHub Pages

The static site is in [`docs/`](docs/). After each scanner run, the saved V2 rank snapshots are exported as JSON files under `docs/ranks/`; the page lets visitors select a date and search, filter, and sort ranks by sector and market cap. The SQLite database and price history are not published.

To publish or update the page, run the scanner and push the changed `docs/` files to the `main` branch. GitHub Actions deploys the site. In the repository, set **Settings → Pages → Build and deployment → Source** to **GitHub Actions** if it is not already selected. The page URL is `https://parthadevcommunity.github.io/money_purse/`.

If you only need to refresh the static rank files from the existing database without scanning prices again, run:

```bash
python3 publish_rank_history.py
```

Commit and push the resulting `docs/` changes for them to appear on the public site.

## Other files

- `print_sorted_momentum.py` — prints momentum results in ranked order.
- `new_code_transcipt_load_only.py` — downloads/caches NSE stock prices and generates momentum reports.
- `momentum_dashboard.py` and `momentum_dashboard.html` — local SQLite-backed web dashboard with ranking correctness results.

Generated price databases and momentum CSV/XLSX outputs are local data files and are excluded from Git.
