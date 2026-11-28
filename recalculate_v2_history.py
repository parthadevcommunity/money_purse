#!/usr/bin/env python3
"""Recalculate saved V2 scores from daily_prices using the current scoring rules."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "nse_stock_prices.sqlite3"
WEIGHTS = {
    "V2_Close_Above_EMA50": 10,
    "V2_Close_Above_EMA20": 10,
    "V2_EMA20_Above_EMA50": 20,
    "V2_Within_20Pct_Of_52W_High": 30,
    "V2_ATR14_Below_3Pct": 10,
    "V2_CCI20_Rising": 10,
    "V2_Volume_Above_20D_Avg": 5,
    "V2_Avg_Volume_2D_Above_5D": 5,
}


def recalculate() -> tuple[int, int]:
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    targets: dict[str, list[sqlite3.Row]] = {}
    for row in connection.execute(
        "SELECT snapshot_date,data_date,symbol,metrics_json FROM momentum_rank_history "
        "WHERE model='V2' ORDER BY symbol,snapshot_date"
    ):
        targets.setdefault(row["symbol"], []).append(row)

    updates = []
    unavailable_52w = 0
    calculated = 0
    for symbol, snapshots in targets.items():
        prices = pd.read_sql_query(
            "SELECT trade_date,high,low,close,volume FROM daily_prices "
            "WHERE symbol=? ORDER BY trade_date",
            connection,
            params=(symbol,),
        )
        if prices.empty:
            continue
        dates = prices["trade_date"].tolist()
        date_index = {day: index for index, day in enumerate(dates)}
        close = pd.to_numeric(prices["close"], errors="coerce")
        high = pd.to_numeric(prices["high"], errors="coerce")
        low = pd.to_numeric(prices["low"], errors="coerce")
        volume = pd.to_numeric(prices["volume"], errors="coerce")
        ema20 = close.ewm(span=20, adjust=False).mean()
        ema50 = close.ewm(span=50, adjust=False).mean()
        previous_close = close.shift(1)
        true_range = pd.concat(
            [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
            axis=1,
        ).max(axis=1)
        atr14 = true_range.rolling(14).mean()
        atr14_pct = atr14 / close * 100
        typical = (high + low + close) / 3
        typical_sma20 = typical.rolling(20).mean()
        mean_deviation = typical.rolling(20).apply(
            lambda values: np.mean(np.abs(values - np.mean(values))), raw=True
        )
        cci = (typical - typical_sma20) / (0.015 * mean_deviation)
        valid_cci = cci.dropna()
        cci_slope = valid_cci.rolling(20, min_periods=2).apply(
            lambda values: np.polyfit(np.arange(len(values)), values, 1)[0], raw=True
        )
        avg_volume20 = volume.rolling(20, min_periods=1).mean()
        avg_volume2 = volume.rolling(2, min_periods=1).mean()
        avg_volume5 = volume.rolling(5, min_periods=1).mean()

        for snapshot in snapshots:
            position = date_index.get(snapshot["data_date"])
            if position is None:
                eligible = [i for i, day in enumerate(dates) if day <= snapshot["data_date"]]
                if not eligible:
                    continue
                position = eligible[-1]
            last_close = close.iloc[position]
            last_ema20 = ema20.iloc[position]
            last_ema50 = ema50.iloc[position]
            last_atr_pct = atr14_pct.iloc[position]
            cci_pos = valid_cci.index.searchsorted(position, side="right") - 1
            slope_value = cci_slope.iloc[cci_pos] if cci_pos >= 0 else np.nan
            high52 = np.nan
            if position + 1 >= 252:
                high52 = high.iloc[position - 251:position + 1].max()
            pct_below_high = (
                (high52 - last_close) / high52 * 100
                if pd.notna(high52) and high52 > 0 and pd.notna(last_close)
                else np.nan
            )
            latest_volume = volume.iloc[position]
            trailing_returns = {}
            for label, lookback in (("1M", 21), ("3M", 63), ("6M", 126)):
                trailing = np.nan
                if position >= lookback and pd.notna(close.iloc[position - lookback]) and close.iloc[position - lookback] != 0:
                    trailing = (last_close / close.iloc[position - lookback] - 1) * 100
                trailing_returns[label] = trailing
            checks = {
                "V2_Close_Above_EMA50": bool(pd.notna(last_close) and pd.notna(last_ema50) and last_close > last_ema50),
                "V2_Close_Above_EMA20": bool(pd.notna(last_close) and pd.notna(last_ema20) and last_close > last_ema20),
                "V2_EMA20_Above_EMA50": bool(pd.notna(last_ema20) and pd.notna(last_ema50) and last_ema20 > last_ema50),
                "V2_Within_20Pct_Of_52W_High": bool(
                    pd.notna(pct_below_high)
                    and (last_close >= high52 or 0 <= pct_below_high <= 20)
                ),
                "V2_ATR14_Below_3Pct": bool(pd.notna(last_atr_pct) and last_atr_pct < 3),
                "V2_CCI20_Rising": bool(pd.notna(slope_value) and slope_value > 0),
                "V2_Volume_Above_20D_Avg": bool(
                    pd.notna(latest_volume) and latest_volume > avg_volume20.iloc[position]
                ),
                "V2_Avg_Volume_2D_Above_5D": bool(
                    avg_volume2.iloc[position] > avg_volume5.iloc[position]
                ),
            }
            score = sum(WEIGHTS[name] for name, passed in checks.items() if passed)
            metrics = json.loads(snapshot["metrics_json"] or "{}")
            metrics["Momentum_V2_Score_%"] = str(score)
            metrics["V2_EMA20"] = round(float(last_ema20), 2) if pd.notna(last_ema20) else None
            metrics["V2_EMA50"] = round(float(last_ema50), 2) if pd.notna(last_ema50) else None
            metrics["V2_52W_High"] = round(float(high52), 2) if pd.notna(high52) else None
            metrics["V2_Pct_Below_52W_High"] = round(float(pct_below_high), 2) if pd.notna(pct_below_high) else None
            metrics["V2_ATR14_%"] = round(float(last_atr_pct), 2) if pd.notna(last_atr_pct) else None
            metrics["V2_CCI20_Slope"] = round(float(slope_value), 2) if pd.notna(slope_value) else None
            metrics["Return_1M_%"] = round(float(trailing_returns["1M"]), 2) if pd.notna(trailing_returns["1M"]) else ""
            metrics["Return_3M_%"] = round(float(trailing_returns["3M"]), 2) if pd.notna(trailing_returns["3M"]) else ""
            metrics["Return_6M_%"] = round(float(trailing_returns["6M"]), 2) if pd.notna(trailing_returns["6M"]) else ""
            metrics.update({name: str(passed) for name, passed in checks.items()})
            updates.append((float(score), json.dumps(metrics, allow_nan=False), snapshot["snapshot_date"], symbol))
            calculated += 1
            if pd.isna(high52):
                unavailable_52w += 1

    with connection:
        connection.executemany(
            "UPDATE momentum_rank_history SET score=?,metrics_json=? "
            "WHERE model='V2' AND snapshot_date=? AND symbol=?",
            updates,
        )
    connection.close()
    return calculated, unavailable_52w


if __name__ == "__main__":
    count, missing_high = recalculate()
    print(f"Recalculated {count} saved V2 rows; 52-week high unavailable for {missing_high} rows.")
