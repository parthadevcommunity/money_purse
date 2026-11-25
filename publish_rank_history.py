#!/usr/bin/env python3
"""Export saved V2 rank snapshots as static JSON for GitHub Pages."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "nse_stock_prices.sqlite3"
PUBLIC_DIR = ROOT / "docs"
RANKS_DIR = PUBLIC_DIR / "ranks"


def export_rank_history() -> int:
    if not DATABASE.is_file():
        raise SystemExit(f"Database not found: {DATABASE}. Run the scanner first.")

    PUBLIC_DIR.mkdir(exist_ok=True)
    RANKS_DIR.mkdir(exist_ok=True)
    with sqlite3.connect(DATABASE) as connection:
        snapshots = connection.execute(
            "SELECT DISTINCT snapshot_date FROM momentum_rank_history "
            "WHERE model='V2' ORDER BY snapshot_date DESC"
        ).fetchall()
        if not snapshots:
            raise SystemExit("No saved V2 ranks found. Run the scanner first.")
        caps = dict(connection.execute("SELECT symbol,category FROM market_cap_categories"))
        sectors = {
            symbol: (sector, sector_index)
            for symbol, sector, sector_index in connection.execute(
                "SELECT symbol,sector,sector_index FROM stock_classifications"
            )
        } if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_classifications'"
        ).fetchone() else {}

        for (snapshot_date,) in snapshots:
            records = connection.execute(
                "SELECT symbol,rank,score,momentum_status,close,metrics_json "
                "FROM momentum_rank_history WHERE model='V2' AND snapshot_date=? "
                "ORDER BY rank,symbol",
                (snapshot_date,),
            ).fetchall()
            rows = []
            for symbol, rank, score, status, close, metrics_json in records:
                metrics = json.loads(metrics_json) if metrics_json else {}
                sector, sector_index = sectors.get(symbol, (None, None))
                rows.append({
                    "Rank": rank,
                    "Stock": symbol,
                    "Sector": sector or metrics.get("Sector") or "Unclassified",
                    "Market_Cap": caps.get(symbol, "Unclassified"),
                    "Momentum_V2_Score_%": score,
                    "Momentum_Status": status or metrics.get("Momentum_Status", ""),
                    "Close": close,
                    "Return_1M_%": metrics.get("Return_1M_%", ""),
                    "Return_3M_%": metrics.get("Return_3M_%", ""),
                    "Return_6M_%": metrics.get("Return_6M_%", ""),
                    "Sector_Index": sector_index or metrics.get("Sector_Index", ""),
                })
            payload = {"snapshot_date": snapshot_date, "rows": rows}
            (RANKS_DIR / f"{snapshot_date}.json").write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )

    index = {"snapshot_dates": [row[0] for row in snapshots]}
    (PUBLIC_DIR / "ranks-index.json").write_text(
        json.dumps(index, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    print(f"Exported {len(snapshots)} V2 rank snapshots to {RANKS_DIR}")
    return len(snapshots)


if __name__ == "__main__":
    export_rank_history()
