#!/usr/bin/env python3
"""Serve the latest saved V2 momentum ranking from the SQLite database."""

from __future__ import annotations

import argparse
import json
import sqlite3
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


def load_accuracy(path: Path, horizon: int) -> dict:
    if horizon not in (1, 5, 20, 60):
        raise ValueError("Horizon must be 1, 5, 20, or 60 trading sessions.")
    if not path.is_file():
        raise ValueError(f"Database not found: {path}")
    with sqlite3.connect(path) as connection:
        try:
            summaries = connection.execute(
                """
                SELECT r.snapshot_date, r.ranked_count, r.evaluated_count,
                       r.universe_median_return,
                       COUNT(CASE WHEN o.top_decile=1 THEN 1 END) AS top_count,
                       SUM(CASE WHEN o.correct=1 THEN 1 ELSE 0 END) AS correct_count,
                       SUM(CASE WHEN o.top_decile=1 AND o.correct=1 THEN 1 ELSE 0 END) AS top_correct_count,
                       AVG(CASE WHEN o.top_decile=1 THEN o.return_pct END) AS top_avg_return,
                       AVG(CASE WHEN o.top_decile=0 THEN o.return_pct END) AS rest_avg_return
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
        except sqlite3.OperationalError:
            summaries, pending = [], 0

    rows = []
    for snapshot_date, ranked, evaluated, median, top_count, correct, top_correct, top_avg, rest_avg in summaries:
        top_count = top_count or 0
        correct = correct or 0
        top_correct = top_correct or 0
        rows.append({
            "Snapshot_Date": snapshot_date,
            "Ranked_Stocks": ranked,
            "Evaluated_Stocks": evaluated,
            "Top_10_Count": top_count,
            "Correct": correct,
            "Incorrect": evaluated - correct,
            "Correct_%": round(correct / evaluated * 100, 2) if evaluated else "",
            "Top_10_Correct": top_correct,
            "Top_10_Incorrect": top_count - top_correct,
            "Top_10_Correct_%": round(top_correct / top_count * 100, 2) if top_count else "",
            "Top_10_Avg_Return_%": round(top_avg, 2) if top_avg is not None else "",
            "Other_Stocks_Avg_Return_%": round(rest_avg, 2) if rest_avg is not None else "",
            "Universe_Median_Return_%": round(median, 2),
        })
    correct_total = sum(row["Correct"] for row in rows)
    incorrect_total = sum(row["Incorrect"] for row in rows)
    all_total = correct_total + incorrect_total
    top10_correct = sum(row["Top_10_Correct"] for row in rows)
    top10_incorrect = sum(row["Top_10_Incorrect"] for row in rows)
    return {
        "horizon": horizon,
        "rows": rows,
        "evaluated_snapshots": len(rows),
        "pending_snapshots": pending,
        "correct": correct_total,
        "incorrect": incorrect_total,
        "correct_pct": round(correct_total / all_total * 100, 2) if all_total else None,
        "top10_correct": top10_correct,
        "top10_incorrect": top10_incorrect,
        "top10_correct_pct": round(top10_correct / (top10_correct + top10_incorrect) * 100, 2)
        if top10_correct + top10_incorrect else None,
    }


class DashboardHandler(BaseHTTPRequestHandler):
    database_path: Path

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route == "/api/accuracy":
            try:
                query = parse_qs(urlparse(self.path).query)
                horizon = int(query.get("horizon", ["1"])[0])
                body = json.dumps(load_accuracy(self.database_path, horizon), allow_nan=False).encode()
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
