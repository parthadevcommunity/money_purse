#!/usr/bin/env python3
"""Retry price downloads for scanner issues and report unresolved symbols."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from new_code_transcipt_load_only import open_price_database


INDIA_TZ = ZoneInfo("Asia/Kolkata")
RETRY_HISTORY_DAYS = 800
REQUIRED_TRADED_SESSIONS = 200


def download_history(ticker: str, start: date, end: date) -> pd.DataFrame:
    prices = yf.download(
        ticker,
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=False,
    )
    if prices.empty:
        return pd.DataFrame()
    if isinstance(prices.columns, pd.MultiIndex):
        prices.columns = prices.columns.get_level_values(0)
    return prices


def main() -> None:
    connection = open_price_database()
    today = datetime.now(INDIA_TZ).date()
    now = datetime.now(INDIA_TZ).isoformat(timespec="seconds")
    issues = connection.execute(
        """
        SELECT symbol,issue_type,yahoo_ticker,attempt_count,reason
        FROM stock_data_issues WHERE status='pending'
        ORDER BY last_seen_at,symbol
        """
    ).fetchall()
    if not issues:
        print("No pending stock data issues to retry.")
        connection.close()
        return

    print(f"Retrying {len(issues)} pending symbols using up to {RETRY_HISTORY_DAYS} calendar days of history.")
    manual_review = []
    ready_count = 0
    for position, (symbol, issue_type, ticker, attempts, old_reason) in enumerate(issues, 1):
        download_rows = 0
        download_error = None
        try:
            prices = download_history(
                ticker, today - timedelta(days=RETRY_HISTORY_DAYS), today
            )
            if not prices.empty:
                rows = []
                for timestamp, row in prices.iterrows():
                    values = [row.get(field) for field in ("Open", "High", "Low", "Close", "Volume")]
                    if pd.isna(values[3]):
                        continue
                    rows.append((
                        symbol,
                        pd.Timestamp(timestamp).date().isoformat(),
                        *(None if pd.isna(value) else float(value) for value in values[:4]),
                        None if pd.isna(values[4]) else int(values[4]),
                    ))
                download_rows = len(rows)
                connection.executemany(
                    """
                    INSERT INTO daily_prices(symbol,trade_date,open,high,low,close,volume)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(symbol,trade_date) DO UPDATE SET
                        open=excluded.open,high=excluded.high,low=excluded.low,
                        close=excluded.close,volume=excluded.volume
                    """,
                    rows,
                )
                connection.execute(
                    """
                    INSERT INTO sync_state(symbol,last_checked_date) VALUES(?,?)
                    ON CONFLICT(symbol) DO UPDATE SET last_checked_date=excluded.last_checked_date
                    """,
                    (symbol, today.isoformat()),
                )
                connection.commit()
        except Exception as error:
            download_error = f"{type(error).__name__}: {error}"

        cached_count, first_date, last_date = connection.execute(
            """
            SELECT COUNT(*),MIN(trade_date),MAX(trade_date) FROM daily_prices
            WHERE symbol=? AND close IS NOT NULL AND COALESCE(volume,0)>0
            """,
            (symbol,),
        ).fetchone()
        is_ready = cached_count >= REQUIRED_TRADED_SESSIONS
        status = "ready_for_rescan" if is_ready else "pending"
        reason = (
            f"Retry loaded {download_rows} price rows; {cached_count} traded sessions are now cached"
            if is_ready else
            f"Still insufficient: {cached_count}/{REQUIRED_TRADED_SESSIONS} traded sessions after retry"
        )
        if download_error:
            reason += f"; download failed: {download_error}"

        connection.execute(
            """
            UPDATE stock_data_issues SET
                last_seen_at=?,attempt_count=attempt_count+1,traded_rows=?,
                required_rows=?,first_price_date=?,last_price_date=?,reason=?,
                price_update_error=?,status=?,resolved_at=NULL
            WHERE symbol=? AND issue_type=?
            """,
            (now, cached_count, REQUIRED_TRADED_SESSIONS, first_date, last_date,
             reason, download_error, status, symbol, issue_type),
        )
        connection.commit()
        if is_ready:
            ready_count += 1
            print(f"[{position}/{len(issues)}] {symbol}: ready for scanner rescan ({cached_count} sessions)")
        else:
            manual_review.append({
                "Symbol": symbol,
                "Yahoo_Ticker": ticker,
                "Issue": issue_type,
                "Previous_Reason": old_reason,
                "Retry_Error": download_error or "No usable additional history returned",
                "Cached_Traded_Sessions": cached_count,
                "Required_Sessions": REQUIRED_TRADED_SESSIONS,
                "First_Price_Date": first_date or "",
                "Last_Price_Date": last_date or "",
                "Rows_Downloaded_This_Retry": download_rows,
            })
            print(
                f"[{position}/{len(issues)}] MANUAL REVIEW {symbol} ({ticker}): "
                f"{cached_count}/{REQUIRED_TRADED_SESSIONS} sessions; "
                f"{download_error or 'no usable additional history returned'}; "
                f"cached dates {first_date or 'none'} to {last_date or 'none'}"
            )
        time.sleep(0.4)

    connection.close()
    print(f"Retry complete: {ready_count} ready for scanner rescan, {len(manual_review)} need manual review.")
    if manual_review:
        print("Manual-review details are recorded in stock_data_issues (status='pending').")


if __name__ == "__main__":
    main()
