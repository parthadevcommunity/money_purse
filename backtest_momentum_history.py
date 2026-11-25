#!/usr/bin/env python3
"""Rebuild historical ranks from cached prices and evaluate their outcomes."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, timedelta

import numpy as np
import pandas as pd

from new_code_transcipt_load_only import DATABASE_PATH, update_rank_evaluations


def calculate_v2_scores_by_date(prices: pd.DataFrame) -> np.ndarray:
    """Vectorize the scanner's existing V2 rules over one stock's price series."""
    close = prices["Close"]
    high = prices["High"]
    low = prices["Low"]
    volume = prices["Volume"]

    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr14_pct = true_range.rolling(14).mean() / close * 100

    typical = (high + low + close) / 3
    typical_sma20 = typical.rolling(20).mean()
    mean_deviation20 = typical.rolling(20).apply(
        lambda values: np.mean(np.abs(values - np.mean(values))), raw=True
    )
    cci = (typical - typical_sma20) / (0.015 * mean_deviation20)
    cci_values = cci.to_numpy(dtype=float)
    valid_cci = np.flatnonzero(np.isfinite(cci_values))
    cci_rising = np.zeros(len(prices), dtype=bool)
    for i in range(len(prices)):
        end = np.searchsorted(valid_cci, i, side="right")
        if end > 1:
            recent = cci_values[valid_cci[max(0, end - 20):end]]
            slope = np.polyfit(np.arange(len(recent)), recent, 1)[0]
            cci_rising[i] = slope > 0

    high52 = high.rolling(252).max()
    pct_below_high = (high52 - close) / high52 * 100
    near_high = (close >= high52) | pct_below_high.between(0, 20, inclusive="both")
    volume_above20 = volume > volume.rolling(20).mean()
    volume2_above5 = volume.rolling(2).mean() > volume.rolling(5).mean()

    score = (
        (close > ema50).astype(int) * 10
        + (close > ema20).astype(int) * 10
        + (ema20 > ema50).astype(int) * 20
        + near_high.fillna(False).astype(int) * 30
        + (atr14_pct < 3).fillna(False).astype(int) * 10
        + cci_rising.astype(int) * 10
        + volume_above20.fillna(False).astype(int) * 5
        + volume2_above5.fillna(False).astype(int) * 5
    )
    return score.to_numpy(dtype=int)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days", type=int, default=365,
        help="Calendar-day lookback for rank snapshots (default: 365)",
    )
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be at least 1")

    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    try:
        latest_date = connection.execute(
            "SELECT MAX(trade_date) FROM daily_prices"
        ).fetchone()[0]
        if not latest_date:
            raise SystemExit("No cached daily prices. Run the scanner first.")
        start_date = (date.fromisoformat(latest_date) - timedelta(days=args.days)).isoformat()
        snapshot_dates = [row[0] for row in connection.execute(
            "SELECT DISTINCT trade_date FROM daily_prices WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
            (start_date, latest_date),
        )]
        symbols = [row[0] for row in connection.execute(
            "SELECT DISTINCT symbol FROM daily_prices ORDER BY symbol"
        )]
        existing = {
            (snapshot, model)
            for snapshot, model in connection.execute(
                "SELECT DISTINCT snapshot_date,model FROM momentum_rank_history"
            )
        }
        current_snapshot_scores = {
            symbol: (data_date, int(round(score)))
            for symbol, data_date, score in connection.execute(
                """
                SELECT symbol,data_date,score FROM momentum_rank_history
                WHERE snapshot_date=? AND model='V2'
                """,
                (latest_date,),
            )
        }
        history_start = (date.fromisoformat(start_date) - timedelta(days=400)).isoformat()
        print(
            f"Rebuilding up to {len(snapshot_dates)} trading dates from {start_date} "
            f"through {latest_date} using cached prices."
        )

        date_rows: dict[str, list[dict]] = {snapshot: [] for snapshot in snapshot_dates}
        score_mismatches = []
        score_checks = 0
        for symbol in symbols:
            frame = pd.read_sql_query(
                """
                SELECT trade_date AS Date,open AS Open,high AS High,low AS Low,
                       close AS Close,volume AS Volume
                FROM daily_prices WHERE symbol=? AND trade_date BETWEEN ? AND ?
                ORDER BY trade_date
                """,
                connection,
                params=(symbol, history_start, latest_date),
            )
            if frame.empty:
                continue
            frame["Date"] = pd.to_datetime(frame["Date"])
            frame = frame.set_index("Date")
            frame = frame.loc[
                pd.to_numeric(frame["Volume"], errors="coerce").fillna(0) > 0
            ]
            if len(frame) < 200:
                continue

            close = frame["Close"]
            return1 = ((close / close.shift(20) - 1) * 100).round(2)
            return3 = ((close / close.shift(62) - 1) * 100).round(2)
            return6 = ((close / close.shift(125) - 1) * 100).round(2)
            v1_score = (return1 * 0.20 + return3 * 0.40 + return6 * 0.40).round(2)
            v2_scores = calculate_v2_scores_by_date(frame)
            index_dates = frame.index

            if symbol in current_snapshot_scores:
                signal_date, expected_score = current_snapshot_scores[symbol]
                signal_index = index_dates.searchsorted(pd.Timestamp(signal_date), side="right") - 1
                if signal_index >= 0:
                    score_checks += 1
                    actual_score = int(v2_scores[signal_index])
                    if actual_score != expected_score:
                        score_mismatches.append((symbol, expected_score, actual_score))

            for snapshot in snapshot_dates:
                asof = pd.Timestamp(snapshot)
                index = index_dates.searchsorted(asof, side="right") - 1
                if index < 199 or pd.isna(v1_score.iloc[index]):
                    continue
                date_rows[snapshot].append({
                    "Stock": symbol,
                    "Date": index_dates[index].date().isoformat(),
                    "Close": float(close.iloc[index]),
                    "Momentum_Score": float(v1_score.iloc[index]),
                    "Momentum_V2_Score_%": int(v2_scores[index]),
                })

        if score_mismatches:
            raise SystemExit(
                f"Historical backfill stopped: V2 score check differed for "
                f"{len(score_mismatches)} of {score_checks} current stocks. "
                f"Examples: {score_mismatches[:5]}"
            )
        print(f"V2 score formula check: {score_checks} current stocks matched saved scores.")

        added = 0
        skipped_insufficient_history = 0
        for snapshot in snapshot_dates:
            if (snapshot, "V1") in existing and (snapshot, "V2") in existing:
                continue
            ranked = date_rows[snapshot]
            if not ranked:
                skipped_insufficient_history += 1
                continue
            v1_ranked = sorted(ranked, key=lambda row: (-row["Momentum_Score"], row["Stock"]))
            v1_rank_by_stock = {row["Stock"]: rank for rank, row in enumerate(v1_ranked, 1)}
            v2_ranked = sorted(v1_ranked, key=lambda row: -row["Momentum_V2_Score_%"])
            records = []
            for model, rows in (("V1", v1_ranked), ("V2", v2_ranked)):
                if (snapshot, model) in existing:
                    continue
                for rank, row in enumerate(rows, 1):
                    v1_rank = v1_rank_by_stock[row["Stock"]]
                    v2_rank = rank if model == "V2" else ""
                    metrics = {
                        **row,
                        "Rank": str(v1_rank),
                        "Momentum_V2_Rank": str(v2_rank),
                    }
                    records.append((
                        snapshot,
                        row["Date"],
                        model,
                        row["Stock"],
                        rank if model == "V2" else v1_rank,
                        row["Momentum_V2_Score_%"] if model == "V2" else row["Momentum_Score"],
                        "",
                        row["Close"],
                        json.dumps({key: str(value) for key, value in metrics.items()}),
                    ))
            connection.executemany(
                """
                INSERT INTO momentum_rank_history
                    (snapshot_date,data_date,model,symbol,rank,score,momentum_status,close,metrics_json)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(snapshot_date,model,symbol) DO UPDATE SET
                    data_date=excluded.data_date,rank=excluded.rank,score=excluded.score,
                    momentum_status=excluded.momentum_status,close=excluded.close,
                    metrics_json=excluded.metrics_json
                """,
                records,
            )
            connection.commit()
            added += 1
            print(f"Saved {len(ranked)} stocks for {snapshot}")
        if skipped_insufficient_history:
            print(
                f"Skipped {skipped_insufficient_history} dates: no cached stocks met "
                "the scanner's 200-trading-session minimum."
            )
    finally:
        connection.close()

    update_rank_evaluations()
    print(f"Historical rank backfill complete. Added snapshots for {added} trading dates.")


if __name__ == "__main__":
    main()
