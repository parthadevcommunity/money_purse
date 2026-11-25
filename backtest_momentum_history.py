#!/usr/bin/env python3
"""Rebuild historical momentum ranks from cached daily prices and score outcomes."""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta

import pandas as pd

from new_code_transcipt_load_only import (
    calculate_momentum_v2,
    open_price_database,
    update_rank_evaluations,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days", type=int, default=30,
        help="Calendar-day lookback for rank snapshots (default: 30)",
    )
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be at least 1")

    connection = open_price_database()
    try:
        latest_date = connection.execute(
            "SELECT MAX(trade_date) FROM daily_prices"
        ).fetchone()[0]
        if not latest_date:
            raise SystemExit("No cached daily prices. Run the scanner first.")
        start_date = (date.fromisoformat(latest_date) - timedelta(days=args.days)).isoformat()
        snapshot_dates = [
            row[0] for row in connection.execute(
                """
                SELECT DISTINCT trade_date FROM daily_prices
                WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date
                """,
                (start_date, latest_date),
            )
        ]
        symbols = [row[0] for row in connection.execute(
            "SELECT DISTINCT symbol FROM daily_prices ORDER BY symbol"
        )]
        history_start = (date.fromisoformat(start_date) - timedelta(days=400)).isoformat()
        print(
            f"Rebuilding ranks for {len(snapshot_dates)} trading dates "
            f"from {start_date} through {latest_date} using cached prices."
        )

        added = 0
        for snapshot_date in snapshot_dates:
            present = {
                row[0] for row in connection.execute(
                    "SELECT DISTINCT model FROM momentum_rank_history WHERE snapshot_date=?",
                    (snapshot_date,),
                )
            }
            if {"V1", "V2"}.issubset(present):
                continue

            scores = []
            for symbol in symbols:
                price_rows = connection.execute(
                    """
                    SELECT trade_date, open, high, low, close, volume
                    FROM daily_prices
                    WHERE symbol=? AND trade_date BETWEEN ? AND ?
                    ORDER BY trade_date
                    """,
                    (symbol, history_start, snapshot_date),
                ).fetchall()
                if len(price_rows) < 200:
                    continue
                prices = pd.DataFrame(
                    price_rows,
                    columns=["Date", "Open", "High", "Low", "Close", "Volume"],
                )
                prices["Date"] = pd.to_datetime(prices["Date"])
                prices = prices.set_index("Date")
                prices = prices.loc[
                    pd.to_numeric(prices["Volume"], errors="coerce").fillna(0) > 0
                ]
                if len(prices) < 200:
                    continue

                close = prices["Close"]
                returns = {
                    "Return_1M_%": round((float(close.iloc[-1] / close.iloc[-21]) - 1) * 100, 2),
                    "Return_3M_%": round((float(close.iloc[-1] / close.iloc[-63]) - 1) * 100, 2),
                    "Return_6M_%": round((float(close.iloc[-1] / close.iloc[-126]) - 1) * 100, 2),
                }
                v1_score = round(
                    returns["Return_1M_%"] * 0.20
                    + returns["Return_3M_%"] * 0.40
                    + returns["Return_6M_%"] * 0.40,
                    2,
                )
                v2 = calculate_momentum_v2(prices)
                scores.append({
                    "Stock": symbol,
                    "Date": prices.index[-1].date().isoformat(),
                    "Close": float(close.iloc[-1]),
                    "Momentum_Score": round(v1_score, 2),
                    "Momentum_V2_Score_%": v2["Momentum_V2_Score_%"],
                })

            v1_ranked = sorted(scores, key=lambda row: (-row["Momentum_Score"], row["Stock"]))
            v1_rank_map = {row["Stock"]: rank for rank, row in enumerate(v1_ranked, 1)}
            v2_ranked = sorted(
                v1_ranked,
                key=lambda row: -row["Momentum_V2_Score_%"],
            )
            records = []
            for model, ranked in (("V1", v1_ranked), ("V2", v2_ranked)):
                if model in present:
                    continue
                for rank, row in enumerate(ranked, 1):
                    v1_rank = v1_rank_map[row["Stock"]]
                    metrics = {
                        **row,
                        "Rank": str(v1_rank),
                        "Momentum_V2_Rank": str(rank if model == "V2" else ""),
                    }
                    records.append((
                        snapshot_date,
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
            print(f"Saved {len(scores)} stocks for {snapshot_date}")
    finally:
        connection.close()

    update_rank_evaluations()
    print(f"Historical rank backfill complete. Added snapshots for {added} trading dates.")


if __name__ == "__main__":
    main()
