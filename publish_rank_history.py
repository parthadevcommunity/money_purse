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


def summarize_breadth(rows: list[dict]) -> dict:
    groups: dict[tuple[str, str | None], list[int]] = {}
    for row in rows:
        try:
            score = float(row["Momentum_V2_Score_%"])
        except (TypeError, ValueError):
            continue
        sector = row["Sector"] or "Unclassified"
        cap = row["Market_Cap"] or "Unclassified"
        for key in ((sector, None), (sector, cap)):
            counts = groups.setdefault(key, [0, 0])
            counts[0 if score >= 75 else 1] += 1

    def summary(counts: list[int]) -> dict:
        positive, negative = counts
        total = positive + negative
        positive_pct = positive / total * 100 if total else 0
        negative_pct = negative / total * 100 if total else 0
        return {
            "positive_count": positive,
            "negative_count": negative,
            "total_count": total,
            "positive_pct": round(positive_pct, 2),
            "negative_pct": round(negative_pct, 2),
            "net_breadth": round(positive_pct - negative_pct, 2),
        }

    sectors: dict[str, dict] = {}
    sector_market_caps: dict[str, dict] = {}
    for (sector, cap), counts in groups.items():
        if cap is None:
            sectors[sector] = summary(counts)
        else:
            sector_market_caps.setdefault(sector, {})[cap] = summary(counts)
    return {"sectors": sectors, "sector_market_caps": sector_market_caps}


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
        index_prices = {}
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='index_daily_prices'"
        ).fetchone():
            for ticker, trade_date, close in connection.execute(
                "SELECT index_ticker,trade_date,close FROM index_daily_prices "
                "ORDER BY index_ticker,trade_date"
            ):
                index_prices.setdefault(ticker, []).append((trade_date, close))

        breadth_history = []
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
            sector_momentum = {}
            sector_tickers = {}
            for symbol, (sector, ticker) in sectors.items():
                if sector and ticker:
                    sector_tickers.setdefault(sector, ticker)
            for sector, ticker in sorted(sector_tickers.items()):
                available = [item for item in index_prices.get(ticker, []) if item[0] <= snapshot_date]
                if len(available) >= 2:
                    prior_date, prior_close = available[-2]
                    current_date, current_close = available[-1]
                    if prior_close:
                        sector_momentum[sector] = {
                            "index": ticker,
                            "change_pct": round((current_close / prior_close - 1) * 100, 2),
                            "from_date": prior_date,
                            "to_date": current_date,
                        }
            breadth = summarize_breadth(rows)
            breadth_history.append({"snapshot_date": snapshot_date, **breadth})
            payload = {
                "snapshot_date": snapshot_date,
                "rows": rows,
                "sector_momentum": sector_momentum,
                "sector_breadth": breadth,
            }
            (RANKS_DIR / f"{snapshot_date}.json").write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )

    index = {"snapshot_dates": [row[0] for row in snapshots]}
    (PUBLIC_DIR / "ranks-index.json").write_text(
        json.dumps(index, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    (PUBLIC_DIR / "sector-breadth.json").write_text(
        json.dumps({"snapshots": breadth_history}, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"Exported {len(snapshots)} V2 rank snapshots to {RANKS_DIR}")
    return len(snapshots)


if __name__ == "__main__":
    export_rank_history()
