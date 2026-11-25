#!/usr/bin/env python3
"""Serve the latest saved V2 momentum ranking from the SQLite database."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "nse_stock_prices.sqlite3"
HTML = ROOT / "momentum_dashboard.html"


def load_rows(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(
            f"Database not found: {path}. Run new_code_transcipt_load_only.py first."
        )
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        try:
            latest = connection.execute(
                "SELECT MAX(snapshot_date) FROM momentum_rank_history WHERE model='V2'"
            ).fetchone()[0]
        except sqlite3.OperationalError as error:
            raise ValueError(
                "Rank history is not in the database yet. Run the latest "
                "new_code_transcipt_load_only.py first."
            ) from error
        if not latest:
            raise ValueError(
                "No V2 rankings are saved in the database yet. Run "
                "new_code_transcipt_load_only.py to create the first snapshot."
            )

        snapshots = connection.execute(
            """
            SELECT symbol, rank, score, momentum_status, close, metrics_json
            FROM momentum_rank_history
            WHERE model='V2' AND snapshot_date=?
            ORDER BY rank, symbol
            """,
            (latest,),
        ).fetchall()
        categories = {
            row["symbol"]: row["category"]
            for row in connection.execute(
                "SELECT symbol, category FROM market_cap_categories"
            )
        } if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_cap_categories'"
        ).fetchone() else {}
        sectors = {
            row["symbol"]: (row["sector"], row["sector_index"])
            for row in connection.execute(
                "SELECT symbol,sector,sector_index FROM stock_classifications"
            )
        } if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_classifications'"
        ).fetchone() else {}

    rows = []
    for record in snapshots:
        metrics = json.loads(record["metrics_json"]) if record["metrics_json"] else {
            "Stock": record["symbol"],
            "Momentum_V2_Rank": str(record["rank"]),
            "Momentum_V2_Score_%": str(record["score"]),
            "Momentum_Status": record["momentum_status"] or "",
            "Close": str(record["close"]),
        }
        metrics["Market_Cap"] = categories.get(record["symbol"], "Unclassified")
        sector, sector_index = sectors.get(record["symbol"], (None, None))
        metrics.setdefault("Sector", sector or "Unclassified")
        metrics.setdefault("Sector_Index", sector_index or "")
        rows.append(metrics)

    columns = list(rows[0].keys()) if rows else []
    if "Market_Cap" not in columns:
        columns.append("Market_Cap")
    cap_as_of = None
    if path.is_file():
        with sqlite3.connect(path) as connection:
            has_caps = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_cap_categories'"
            ).fetchone()
            if has_caps:
                cap_as_of = connection.execute(
                    "SELECT MAX(as_of) FROM market_cap_categories"
                ).fetchone()[0]
    return {
        "columns": columns,
        "rows": rows,
        "database": path.name,
        "snapshot_date": latest,
        "cap_as_of": cap_as_of or "unknown",
    }


def load_accuracy(
    path: Path, horizon: int, date_from: str | None = None, date_to: str | None = None
) -> dict:
    if horizon not in (1, 5, 20, 60):
        raise ValueError("Horizon must be 1, 5, 20, or 60 trading sessions.")
    if not path.is_file():
        raise ValueError(f"Database not found: {path}")
    for label, value in (("from", date_from), ("to", date_to)):
        if value:
            try:
                date.fromisoformat(value)
            except ValueError as error:
                raise ValueError(f"Invalid {label} date; use YYYY-MM-DD.") from error
    if date_from and date_to and date_from > date_to:
        raise ValueError("Start date must be on or before end date.")
    with sqlite3.connect(path) as connection:
        try:
            summaries = connection.execute(
                """
                SELECT r.snapshot_date, r.ranked_count, r.evaluated_count,
                       SUM(CASE WHEN o.correct=1 THEN 1 ELSE 0 END) AS correct_count,
                       SUM(CASE WHEN o.evaluation_result='incorrect' THEN 1 ELSE 0 END) AS incorrect_count,
                       SUM(CASE WHEN o.neutral=1 THEN 1 ELSE 0 END) AS neutral_count,
                       SUM(CASE WHEN o.evaluation_result='unavailable' THEN 1 ELSE 0 END) AS unavailable_count,
                       AVG(o.return_pct) AS avg_return
                FROM rank_evaluation_runs r
                LEFT JOIN rank_forward_outcomes o
                  ON o.snapshot_date=r.snapshot_date AND o.model=r.model AND o.horizon=r.horizon
                WHERE r.model='V2' AND r.horizon=?
                GROUP BY r.snapshot_date, r.ranked_count, r.evaluated_count,
                         r.universe_median_return
                ORDER BY r.snapshot_date DESC
                """,
                (horizon,),
            ).fetchall()
            pending = connection.execute(
                """
                SELECT COUNT(DISTINCT h.snapshot_date)
                FROM momentum_rank_history h
                WHERE h.model='V2' AND NOT EXISTS (
                    SELECT 1 FROM rank_evaluation_runs r
                    WHERE r.snapshot_date=h.snapshot_date AND r.model=h.model AND r.horizon=?
                )
                """,
                (horizon,),
            ).fetchone()[0]
            latest_pending = connection.execute(
                """
                SELECT MAX(h.snapshot_date)
                FROM momentum_rank_history h
                WHERE h.model='V2' AND NOT EXISTS (
                    SELECT 1 FROM rank_evaluation_runs r
                    WHERE r.snapshot_date=h.snapshot_date AND r.model=h.model AND r.horizon=?
                )
                """,
                (horizon,),
            ).fetchone()[0]
        except sqlite3.OperationalError:
            summaries, pending, latest_pending = [], 0, None

    rows = []
    for snapshot_date, ranked, evaluated, correct, incorrect, neutral, unavailable, avg_return in summaries:
        if date_from and snapshot_date < date_from:
            continue
        if date_to and snapshot_date > date_to:
            continue
        correct = correct or 0
        incorrect = incorrect or 0
        neutral = neutral or 0
        rows.append({
            "Snapshot_Date": snapshot_date,
            "Ranked_Stocks": ranked,
            "Evaluated_Stocks": evaluated,
            "Correct": correct,
            "Incorrect": incorrect,
            "Neutral": neutral,
            "Benchmarks_Unavailable": unavailable or 0,
            "Correct_%": round(correct / (correct + incorrect) * 100, 2)
            if correct + incorrect else "",
            "Average_Return_%": round(avg_return, 2) if avg_return is not None else "",
        })
    correct_total = sum(row["Correct"] for row in rows)
    incorrect_total = sum(row["Incorrect"] for row in rows)
    neutral_total = sum(row["Neutral"] for row in rows)
    directional_total = correct_total + incorrect_total
    return {
        "horizon": horizon,
        "rows": rows,
        "evaluated_snapshots": len(rows),
        "pending_snapshots": pending,
        "latest_pending_snapshot": latest_pending,
        "correct": correct_total,
        "incorrect": incorrect_total,
        "neutral": neutral_total,
        "benchmarks_unavailable": sum(row["Benchmarks_Unavailable"] for row in rows),
        "correct_pct": round(correct_total / directional_total * 100, 2)
        if directional_total else None,
        "latest_completed_snapshot": rows[0]["Snapshot_Date"] if rows else None,
        "earliest_completed_snapshot": rows[-1]["Snapshot_Date"] if rows else None,
        "date_from": date_from,
        "date_to": date_to,
    }


def load_accuracy_stocks(
    path: Path, horizon: int, snapshot_date: str | None = None,
    date_from: str | None = None, date_to: str | None = None,
) -> dict:
    if horizon not in (1, 5, 20, 60):
        raise ValueError("Horizon must be 1, 5, 20, or 60 trading sessions.")
    if not path.is_file():
        raise ValueError(f"Database not found: {path}")
    with sqlite3.connect(path) as connection:
        if snapshot_date:
            date_from = snapshot_date
            date_to = snapshot_date
        if not date_from and not date_to:
            found = connection.execute(
                "SELECT MAX(snapshot_date) FROM rank_evaluation_runs WHERE model='V2' AND horizon=?",
                (horizon,),
            ).fetchone()
            date_from = date_to = found[0] if found else None
        if not date_from and not date_to:
            return {"snapshot_date": None, "horizon": horizon, "rows": []}
        clauses = ["o.model='V2'", "o.horizon=?"]
        params: list = [horizon]
        if date_from:
            clauses.append("o.snapshot_date>=?")
            params.append(date_from)
        if date_to:
            clauses.append("o.snapshot_date<=?")
            params.append(date_to)
        records = connection.execute(
            f"""
            SELECT o.rank,o.symbol,o.snapshot_date,h.data_date,o.exit_date,
                   o.entry_close,o.exit_close,o.return_pct,o.correct,o.neutral,
                   o.evaluation_result,o.sector_index,o.sector_return_pct,
                   o.market_cap_index,o.market_cap_return_pct,
                   h.metrics_json,c.category,sc.sector
            FROM rank_forward_outcomes o
            LEFT JOIN momentum_rank_history h
              ON h.snapshot_date=o.snapshot_date AND h.model=o.model AND h.symbol=o.symbol
            LEFT JOIN market_cap_categories c ON c.symbol=o.symbol
            LEFT JOIN stock_classifications sc ON sc.symbol=o.symbol
            WHERE {' AND '.join(clauses)}
            ORDER BY o.symbol,o.snapshot_date,o.rank
            """,
            params,
        ).fetchall()

    aggregate: dict[str, dict] = {}
    for rank, symbol, rank_date, signal_price_date, check_date, signal_close, check_close, change, correct, neutral, result, sector_index, sector_return, cap_index, cap_return, metrics_json, category, stored_sector in records:
        metrics = json.loads(metrics_json) if metrics_json else {}
        row = aggregate.setdefault(symbol, {
            "Stock": symbol,
            "Sector": stored_sector or metrics.get("Sector", "Unclassified"),
            "Market_Cap": category or "Unclassified",
            "Evaluations": 0,
            "Correct": 0,
            "Incorrect": 0,
            "Neutral": 0,
            "Unavailable": 0,
            "_returns": [],
        })
        row["Evaluations"] += 1
        key = {"correct": "Correct", "incorrect": "Incorrect", "neutral": "Neutral", "unavailable": "Unavailable"}.get(result, "Unavailable")
        row[key] += 1
        row["_returns"].append(change)
    rows = []
    for row in aggregate.values():
        directional = row["Correct"] + row["Incorrect"]
        returns = row.pop("_returns")
        row["Correct_%"] = round(row["Correct"] / directional * 100, 2) if directional else ""
        row["Average_Return_%"] = round(sum(returns) / len(returns), 2) if returns else ""
        rows.append(row)
    rows.sort(key=lambda row: (-row["Correct"], row["Stock"]))
    return {
        "snapshot_date": snapshot_date,
        "date_from": date_from,
        "date_to": date_to,
        "horizon": horizon,
        "rows": rows,
    }


class DashboardHandler(BaseHTTPRequestHandler):
    database_path: Path

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route == "/api/accuracy":
            try:
                query = parse_qs(urlparse(self.path).query)
                horizon = int(query.get("horizon", ["1"])[0])
                date_from = query.get("from", [None])[0]
                date_to = query.get("to", [None])[0]
                body = json.dumps(
                    load_accuracy(self.database_path, horizon, date_from, date_to),
                    allow_nan=False,
                ).encode()
                status = 200
            except (OSError, ValueError, sqlite3.Error) as error:
                body = json.dumps({"error": str(error)}).encode()
                status = 400
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if route == "/api/accuracy/stocks":
            try:
                query = parse_qs(urlparse(self.path).query)
                horizon = int(query.get("horizon", ["1"])[0])
                snapshot_date = query.get("snapshot", [None])[0]
                date_from = query.get("from", [None])[0]
                date_to = query.get("to", [None])[0]
                body = json.dumps(
                    load_accuracy_stocks(
                        self.database_path, horizon, snapshot_date, date_from, date_to
                    ),
                    allow_nan=False,
                ).encode()
                status = 200
            except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError) as error:
                body = json.dumps({"error": str(error)}).encode()
                status = 400
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if route == "/api/data":
            try:
                body = json.dumps(load_rows(self.database_path), allow_nan=False).encode()
                status = 200
            except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError) as error:
                body = json.dumps({"error": str(error)}).encode()
                status = 500
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if route in ("/", "/index.html"):
            try:
                body = HTML.read_bytes()
            except OSError as error:
                self.send_error(500, str(error))
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404, "Not found")

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    DashboardHandler.database_path = DATABASE
    server = ThreadingHTTPServer(("127.0.0.1", args.port), DashboardHandler)
    print(f"Reading latest V2 rankings from {DATABASE}\nOpen http://127.0.0.1:{args.port} (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
