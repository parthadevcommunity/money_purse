#!/usr/bin/env python3
"""Download older Yahoo Finance prices for symbols in saved V2 snapshots."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import date
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "nse_stock_prices.sqlite3"
START_DATE = "2024-12-01"
END_DATE = "2025-10-20"  # Yahoo's end date is exclusive.
BATCH_SIZE = 20


def ticker_for(symbol: str, metrics: dict) -> str:
    ticker = str(metrics.get("Yahoo_Ticker") or "").strip()
    if ticker:
        return ticker
    aliases = {"M&M": "M&M.NS", "BAJAJ-AUTO": "BAJAJ-AUTO.NS"}
    return aliases.get(symbol, f"{symbol}.NS")


def extract_ticker_frame(data: pd.DataFrame, ticker: str, requested: list[str]) -> pd.DataFrame:
    if not isinstance(data.columns, pd.MultiIndex):
        return data if len(requested) == 1 else pd.DataFrame()
    for level in range(data.columns.nlevels):
        names = set(map(str, data.columns.get_level_values(level).unique()))
        if ticker in names:
            try:
                return data.xs(ticker, axis=1, level=level, drop_level=True)
            except KeyError:
                pass
    return pd.DataFrame()


def price_rows(symbol: str, frame: pd.DataFrame) -> list[tuple]:
    if frame.empty:
        return []
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(-1)
    if "Close" not in frame.columns:
        return []
    rows = []
    for timestamp, row in frame.iterrows():
        close = row.get("Close")
        if pd.isna(close):
            continue
        values = [row.get(name) for name in ("Open", "High", "Low", "Close", "Volume")]
        rows.append((
            symbol,
            pd.Timestamp(timestamp).date().isoformat(),
            *(None if pd.isna(value) else float(value) for value in values[:4]),
            None if pd.isna(values[4]) else int(values[4]),
        ))
    return rows


def main() -> None:
    with sqlite3.connect(DATABASE, timeout=60) as connection:
        records = connection.execute(
            "SELECT h.symbol,h.metrics_json FROM momentum_rank_history h "
            "JOIN (SELECT symbol,MAX(snapshot_date) AS latest FROM momentum_rank_history "
            "WHERE model='V2' GROUP BY symbol) latest "
            "ON h.symbol=latest.symbol AND h.snapshot_date=latest.latest "
            "WHERE h.model='V2' ORDER BY h.symbol"
        ).fetchall()
        symbols = []
        for symbol, metrics_json in records:
            metrics = json.loads(metrics_json or "{}")
            ticker = ticker_for(symbol, metrics)
            earliest = connection.execute(
                "SELECT MIN(trade_date) FROM daily_prices WHERE symbol=?", (symbol,)
            ).fetchone()[0]
            if earliest and earliest < END_DATE:
                continue
            symbols.append((symbol, ticker))

        print(f"Downloading pre-{END_DATE} prices for {len(symbols)} symbols from {START_DATE}.", flush=True)
        saved = 0
        failed: list[tuple[str, str]] = []
        for batch_start in range(0, len(symbols), BATCH_SIZE):
            batch = symbols[batch_start:batch_start + BATCH_SIZE]
            tickers = [ticker for _, ticker in batch]
            try:
                downloaded = yf.download(
                    tickers=tickers,
                    start=START_DATE,
                    end=END_DATE,
                    interval="1d",
                    auto_adjust=True,
                    progress=False,
                    threads=True,
                    group_by="ticker",
                )
            except Exception as error:
                downloaded = pd.DataFrame()
                failed.extend((symbol, f"batch download failed: {error}") for symbol, _ in batch)

            for symbol, ticker in batch:
                frame = extract_ticker_frame(downloaded, ticker, tickers) if not downloaded.empty else pd.DataFrame()
                rows = price_rows(symbol, frame)
                if not rows:
                    try:
                        single = yf.download(
                            ticker,
                            start=START_DATE,
                            end=END_DATE,
                            interval="1d",
                            auto_adjust=True,
                            progress=False,
                            threads=False,
                        )
                        if isinstance(single.columns, pd.MultiIndex):
                            single.columns = single.columns.get_level_values(0)
                        rows = price_rows(symbol, single)
                    except Exception as error:
                        failed.append((symbol, str(error)))
                if rows:
                    with connection:
                        connection.executemany(
                            "INSERT INTO daily_prices(symbol,trade_date,open,high,low,close,volume) "
                            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(symbol,trade_date) DO UPDATE SET "
                            "open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume",
                            rows,
                        )
                    saved += len(rows)
            completed = min(batch_start + len(batch), len(symbols))
            print(f"Processed {completed}/{len(symbols)} symbols; saved {saved} older price rows.", flush=True)
            time.sleep(0.4)

        print(f"Finished: {saved} older price rows saved; {len(failed)} symbol download failures.", flush=True)
        if failed:
            issue_file = ROOT / "older_price_download_failures.csv"
            pd.DataFrame(failed, columns=["symbol", "error"]).to_csv(issue_file, index=False)
            print(f"Failures written to {issue_file}", flush=True)


if __name__ == "__main__":
    main()
