#!/usr/bin/env python3
"""Export saved V2 rank snapshots as static JSON for GitHub Pages."""

from __future__ import annotations

import json
import sqlite3
from statistics import mean, median
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "nse_stock_prices.sqlite3"
PUBLIC_DIR = ROOT / "docs"
RANKS_DIR = PUBLIC_DIR / "ranks"
SECTOR_OVERRIDES = {
    "ARTEMISMED": "Healthcare",
    "GUJRAFFIA": "Basic Materials",
    "OLIL": "Industrials",
    "SPECIALITY": "Consumer Cyclical",
    "WILLAMAGOR": "Financial Services",
    "ZAGGLE": "Technology",
    "ZODIAC": "Utilities",
}
SECTOR_INDEXES = {
    "Technology": "^CNXIT", "Financial Services": "^CNXFINANCE",
    "Healthcare": "^CNXPHARMA", "Consumer Defensive": "^CNXFMCG",
    "Consumer Cyclical": "^CNXAUTO", "Industrials": "^CNXCG",
    "Basic Materials": "^CNXMETAL", "Energy": "^CNXENERGY",
    "Utilities": "^CNXENERGY", "Communication Services": "^CNXMEDIA",
    "Real Estate": "^CNXREALTY",
}


def number(value) -> float | None:
    try:
        parsed = float(str(value).replace("%", "").replace(",", "").strip())
        return parsed if parsed == parsed else None
    except (TypeError, ValueError):
        return None


def boolean(value) -> bool | None:
    if value in (True, 1, "True", "true", "1"):
        return True
    if value in (False, 0, "False", "false", "0"):
        return False
    return None


def split_groups(groups: dict) -> dict:
    sectors, by_cap = {}, {}
    for (sector, cap), value in groups.items():
        if cap is None:
            sectors[sector] = value
        else:
            by_cap.setdefault(sector, {})[cap] = value
    return {"sectors": sectors, "sector_market_caps": by_cap}


def summarize_snapshot(rows: list[dict]) -> dict:
    score_groups: dict[tuple[str, str | None], list[int]] = {}
    trend_groups: dict[tuple[str, str | None], list[int]] = {}
    returns: dict[str, dict[tuple[str, str | None], list[float]]] = {
        horizon: {} for horizon in ("1M", "3M", "6M")
    }
    universe_returns: dict[str, list[float]] = {key: [] for key in returns}
    cap_universe_returns: dict[str, dict[str, list[float]]] = {
        key: {} for key in returns
    }
    missing_sector = missing_market_cap = 0
    valid_caps = {"Large Cap", "Mid Cap", "Small Cap"}

    def add_group(groups, key, values, value):
        counts = groups.setdefault(key, [0] * len(values))
        counts[value] += 1

    for row in rows:
        sector, cap = row["Sector"], row["Market_Cap"]
        if not sector or sector == "Unclassified":
            missing_sector += 1
            continue
        cap = cap if cap in valid_caps else None
        if cap is None:
            missing_market_cap += 1
        keys = [(sector, None)] + ([(sector, cap)] if cap else [])

        score = number(row.get("Momentum_V2_Score_%"))
        if score is not None:
            for key in keys:
                counts = score_groups.setdefault(key, [0, 0, 0])
                counts[0 if score >= 75 else 1] += 1
                if number(row.get("V2_52W_High")) is None:
                    counts[2] += 1

        above_ema = boolean(row.get("V2_Close_Above_EMA50"))
        ema_bull = boolean(row.get("V2_EMA20_Above_EMA50"))
        if above_ema is not None and ema_bull is not None:
            state = 0 if above_ema and ema_bull else 1 if not above_ema and not ema_bull else 2
            for key in keys:
                add_group(trend_groups, key, (0, 1, 2), state)

        for horizon in returns:
            value = number(row.get(f"Return_{horizon}_%"))
            if value is None:
                continue
            universe_returns[horizon].append(value)
            if cap:
                cap_universe_returns[horizon].setdefault(cap, []).append(value)
            for key in keys:
                returns[horizon].setdefault(key, []).append(value)

    score_summary = {}
    for key, (positive, below, missing_52w) in score_groups.items():
        total = positive + below
        score_summary[key] = {
            "strong_count": positive,
            "below_threshold_count": below,
            "total_count": total,
            "scoreable_52w_count": total - missing_52w,
            "missing_52w_count": missing_52w,
            "strong_pct": round(positive / total * 100, 2) if total else None,
        }

    trend_summary = {}
    for key, (bullish, bearish, neutral) in trend_groups.items():
        total = bullish + bearish + neutral
        trend_summary[key] = {
            "bullish_count": bullish,
            "bearish_count": bearish,
            "neutral_count": neutral,
            "total_count": total,
            "bullish_pct": round(bullish / total * 100, 2) if total else None,
            "bearish_pct": round(bearish / total * 100, 2) if total else None,
            "neutral_pct": round(neutral / total * 100, 2) if total else None,
            "net_breadth": round((bullish - bearish) / total * 100, 2) if total else None,
        }

    relative = {}
    for horizon, groups in returns.items():
        universe_median = median(universe_returns[horizon]) if universe_returns[horizon] else None
        cap_medians = {
            cap: median(values)
            for cap, values in cap_universe_returns[horizon].items() if values
        }
        values = {}
        for key, samples in groups.items():
            sector_median = median(samples)
            benchmark = universe_median if key[1] is None else cap_medians.get(key[1])
            values[key] = {
                "sector_median_pct": round(sector_median, 2),
                "universe_median_pct": round(benchmark, 2) if benchmark is not None else None,
                "relative_strength_pct": round(sector_median - benchmark, 2) if benchmark is not None else None,
                "stock_count": len(samples),
            }
        relative[horizon] = split_groups(values)

    return {
        "trend": {**split_groups(trend_summary), "excluded_missing_sector": missing_sector},
        "strong_score": {**split_groups(score_summary), "excluded_missing_sector": missing_sector},
        "relative_strength": relative,
        "excluded_missing_sector": missing_sector,
        "excluded_missing_market_cap": missing_market_cap,
    }


def summarize_forward_returns(outcomes: list[tuple], sector_map: dict, caps: dict) -> dict:
    grouped: dict[int, dict[tuple[str, str | None], list[float]]] = {}
    universe: dict[int, list[float]] = {}
    cap_universe: dict[int, dict[str, list[float]]] = {}
    for horizon, symbol, return_pct in outcomes:
        if return_pct is None:
            continue
        sector = SECTOR_OVERRIDES.get(symbol, sector_map.get(symbol, (None, None))[0])
        if not sector or sector == "Unclassified":
            continue
        cap = caps.get(symbol)
        cap = cap if cap in ("Large Cap", "Mid Cap", "Small Cap") else None
        value = float(return_pct)
        grouped.setdefault(horizon, {}).setdefault((sector, None), []).append(value)
        universe.setdefault(horizon, []).append(value)
        if cap:
            grouped[horizon].setdefault((sector, cap), []).append(value)
            cap_universe.setdefault(horizon, {}).setdefault(cap, []).append(value)

    result = {}
    for horizon, groups in grouped.items():
        values = {}
        for key, samples in groups.items():
            benchmark_samples = universe[horizon] if key[1] is None else cap_universe.get(horizon, {}).get(key[1], [])
            benchmark = mean(benchmark_samples) if benchmark_samples else None
            sector_mean = mean(samples)
            values[key] = {
                "mean_return_pct": round(sector_mean, 2),
                "median_return_pct": round(median(samples), 2),
                "universe_mean_pct": round(benchmark, 2) if benchmark is not None else None,
                "excess_mean_pct": round(sector_mean - benchmark, 2) if benchmark is not None else None,
                "stock_count": len(samples),
            }
        result[str(horizon)] = split_groups(values)
    return result


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
                sector = SECTOR_OVERRIDES.get(symbol, sector)
                if sector and not sector_index:
                    sector_index = SECTOR_INDEXES.get(sector)
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
                    "V2_Close_Above_EMA50": metrics.get("V2_Close_Above_EMA50"),
                    "V2_EMA20_Above_EMA50": metrics.get("V2_EMA20_Above_EMA50"),
                    "V2_52W_High": metrics.get("V2_52W_High"),
                })
            sector_momentum = {}
            sector_tickers = {}
            for symbol, (sector, ticker) in sectors.items():
                sector = SECTOR_OVERRIDES.get(symbol, sector)
                ticker = ticker or SECTOR_INDEXES.get(sector)
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
            breadth = summarize_snapshot(rows)
            forward_rows = connection.execute(
                "SELECT horizon,symbol,return_pct FROM rank_forward_outcomes "
                "WHERE model='V2' AND snapshot_date=? AND horizon IN (5,20,60)",
                (snapshot_date,),
            ).fetchall() if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rank_forward_outcomes'"
            ).fetchone() else []
            breadth["forward_returns"] = summarize_forward_returns(
                forward_rows,
                sectors,
                caps,
            )
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
