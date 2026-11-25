#!/usr/bin/env python3
"""Serve the latest saved V2 momentum ranking from the SQLite database."""

from __future__ import annotations

import argparse
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


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


class DashboardHandler(BaseHTTPRequestHandler):
    database_path: Path

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
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
