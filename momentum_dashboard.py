#!/usr/bin/env python3
"""Serve the local momentum CSV as a searchable, sortable web dashboard."""

from __future__ import annotations

import argparse
import csv
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
DEFAULT_CSV = ROOT / "NSE_All_Stocks_Momentum_V2.csv"
CAP_CSV = ROOT / "amfi_market_cap_2026-06.csv"
HTML = ROOT / "momentum_dashboard.html"
CAP_AS_OF = "30 June 2026"


def load_cap_categories(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8-sig") as stream:
        return {
            row["NSE Symbol"].strip().upper(): row["Category"].strip()
            for row in csv.DictReader(stream)
            if row.get("NSE Symbol", "").strip() and row.get("Category", "").strip()
        }


def load_rows(path: Path, categories: dict[str, str]) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValueError(f"CSV has no header: {path}")
        rows = []
        for row in reader:
            symbol = (row.get("Stock") or "").strip().upper()
            row["Market_Cap"] = categories.get(symbol, "Unclassified")
            rows.append(row)
        return [*reader.fieldnames, "Market_Cap"], rows


class DashboardHandler(BaseHTTPRequestHandler):
    csv_path: Path

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route == "/api/data":
            try:
                columns, rows = load_rows(self.csv_path, load_cap_categories(CAP_CSV))
                body = json.dumps(
                    {"columns": columns, "rows": rows, "file": self.csv_path.name,
                     "cap_as_of": CAP_AS_OF},
                    allow_nan=False,
                ).encode()
            except (OSError, ValueError, csv.Error) as error:
                body = json.dumps({"error": str(error)}).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
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
    parser.add_argument("csv", nargs="?", type=Path, default=DEFAULT_CSV,
                        help="CSV to display (default: NSE_All_Stocks_Momentum_V2.csv)")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    csv_path = args.csv.expanduser().resolve()
    if not csv_path.is_file():
        parser.error(f"CSV file not found: {csv_path}")
    DashboardHandler.csv_path = csv_path
    server = ThreadingHTTPServer(("127.0.0.1", args.port), DashboardHandler)
    print(f"Showing {csv_path}\nOpen http://127.0.0.1:{args.port} (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
