#!/usr/bin/env python3
"""Run the scanner and publish ranks at the scheduled time on NSE trading days."""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


ROOT = Path(__file__).resolve().parent
INDIA_TZ = ZoneInfo("Asia/Kolkata")
NSE_HOLIDAY_URL = "https://www.nseindia.in/api/holiday-master?type=trading"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.in/resources/exchange-communication-holidays",
}


def parse_holiday_date(value: str) -> date:
    value = value.strip()
    for pattern in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            pass
    raise ValueError(f"Unrecognized date in NSE holiday response: {value!r}")


def get_nse_cash_market_holidays() -> dict[date, str]:
    """Fetch the official NSE trading calendar and return Cash Market holidays."""
    session = requests.Session()
    session.headers.update(HEADERS)
    response = session.get(NSE_HOLIDAY_URL, timeout=20)
    response.raise_for_status()
    payload = response.json()
    cash_holidays = payload.get("CM") or payload.get("cm")
    if not isinstance(cash_holidays, list):
        raise ValueError("NSE holiday response did not contain the Cash Market (CM) calendar")

    holidays: dict[date, str] = {}
    for item in cash_holidays:
        if not isinstance(item, dict):
            continue
        normalized = {str(key).lower().replace("_", ""): value for key, value in item.items()}
        raw_date = next(
            (normalized[key] for key in ("tradingdate", "date", "holidaydate") if normalized.get(key)),
            None,
        )
        if not raw_date:
            continue
        holiday_date = parse_holiday_date(str(raw_date))
        description = str(
            normalized.get("description") or normalized.get("holidaydescription") or "NSE trading holiday"
        )
        holidays[holiday_date] = description
    if not holidays:
        raise ValueError("NSE returned an empty Cash Market holiday calendar")
    return holidays


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-only", action="store_true",
        help="Check whether today is an NSE trading day without running the scanner.",
    )
    args = parser.parse_args()

    today = datetime.now(INDIA_TZ).date()
    if today.weekday() >= 5:
        print(f"Skipping {today}: weekend.", flush=True)
        return 0

    try:
        holidays = get_nse_cash_market_holidays()
    except Exception as error:
        # Fail closed so the job never publishes a same-day snapshot on an
        # unverified exchange holiday.
        print(
            f"Skipping {today}: could not verify the NSE holiday calendar; "
            f"scanner and publisher were not run. {type(error).__name__}: {error}",
            flush=True,
        )
        return 2

    if not any(day.year == today.year for day in holidays):
        print(
            f"Skipping {today}: NSE has not published a Cash Market holiday list for {today.year}; "
            "update the holiday source before relying on the scheduled run.",
            flush=True,
        )
        return 2

    if today in holidays:
        print(f"Skipping {today}: NSE market holiday ({holidays[today]}).", flush=True)
        return 0
    print(f"{today} is an NSE Cash Market trading day.", flush=True)
    if args.check_only:
        print("Check only: scanner and publisher were not run.", flush=True)
        return 0

    subprocess.run(
        [sys.executable, str(ROOT / "new_code_transcipt_load_only.py")],
        cwd=ROOT,
        check=True,
    )
    with sqlite3.connect(ROOT / "nse_stock_prices.sqlite3") as connection:
        snapshot = connection.execute(
            "SELECT MAX(snapshot_date) FROM momentum_rank_history WHERE model='V2'"
        ).fetchone()[0]
        latest_price_date = connection.execute(
            "SELECT data_date FROM momentum_rank_history "
            "WHERE model='V2' AND snapshot_date=? "
            "GROUP BY data_date ORDER BY COUNT(*) DESC, data_date DESC LIMIT 1",
            (snapshot,),
        ).fetchone()[0] if snapshot else None
    if snapshot != today.isoformat() or latest_price_date != today.isoformat():
        print(
            f"Not publishing: latest rank snapshot is {snapshot or 'missing'} and "
            f"latest price date in it is {latest_price_date or 'missing'}; expected {today}.",
            flush=True,
        )
        return 3
    subprocess.run([str(ROOT / "publish_ranks.sh")], cwd=ROOT, check=True)
    print("Daily refresh and GitHub Pages publish completed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
