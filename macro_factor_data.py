#!/usr/bin/env python3
"""Refresh macro observations in the existing SQLite database and export Pages JSON.

GDP uses MoSPI's public e-Sankhyiki API, household inflation uses an RBI IESH
release, and 10-year yields use FBIL's public daily workbook endpoint. PMI is
read only from a user-supplied authorized CSV: S&P Global's release terms
prohibit unauthorized copying or redistribution of its PMI data.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "nse_stock_prices.sqlite3"
PUBLIC_JSON = ROOT / "docs" / "macro-momentum.json"
PMI_CSV = ROOT / "data" / "macro" / "pmi.csv"
INDIA_TZ = ZoneInfo("Asia/Kolkata")
HEADERS = {"User-Agent": "MoneyPurseMacroDashboard/1.0 (public economic data refresh)"}

MOSPI_GDP_API = "https://api.mospi.gov.in/api/nas/getNASData"
RBI_IESH_URL = os.getenv(
    "RBI_IESH_URL", "https://www.rbi.org.in/Scripts/PublicationsView.aspx?id=24089"
)
FBIL_ARCHIVE_API = "https://www.fbil.org.in/wasdm/gsec/fetch"
FBIL_WORKBOOK_API = "https://www.fbil.org.in/wasdm/gsec/downloadPublished"

# MoSPI returns reference periods but not the original publication date.
# Verify and add new dates from the MoSPI release note/calendar; never use the
# time this script downloaded the data as a substitute.
GDP_RELEASE_DATES = {("2026-27", "Q1"): "2026-08-31"}
MAX_AGE_DAYS = {"gdp": 150, "pmi": 60, "inflation": 150, "bond_yield": 10}


def get(url: str, **kwargs) -> requests.Response:
    response = requests.get(url, headers=HEADERS, timeout=35, **kwargs)
    response.raise_for_status()
    return response


def fetch_gdp() -> dict:
    payload = get(MOSPI_GDP_API, params={
        "base_year": "2022-23", "series": "Current", "frequency_code": "Quarterly",
        "indicator_code": "22", "Format": "JSON", "limit": 200, "page": 1,
    }).json()
    rows = payload.get("data")
    records = [row for row in (rows or []) if row.get("indicator") == "GDP Growth Rate"
               and row.get("frequency") == "Quarterly" and row.get("unit") == "%"
               and row.get("constant_price") not in (None, "")]
    if not records:
        raise ValueError("MoSPI returned no quarterly real GDP growth rate records.")
    row = max(records, key=lambda item: (item["year"], item["quarter"]))
    publication_date = os.getenv("GDP_PUBLICATION_DATE") or GDP_RELEASE_DATES.get((row["year"], row["quarter"]))
    if not publication_date:
        raise ValueError(f"GDP {row['year']} {row['quarter']} has no verified publication-date mapping.")
    return {
        "indicator": "gdp", "reference_period": f"{row['year']} {row['quarter']}",
        "publication_date": publication_date, "value": float(row["constant_price"]),
        "unit": "% real GDP growth, year over year",
        "source_name": "MoSPI e-Sankhyiki, quarterly GDP growth",
        "source_url": "https://pib.gov.in/PressReleasePage.aspx?PRID=2304949&lang=1&reg=3",
    }


def fetch_inflation() -> dict:
    response = get(RBI_IESH_URL)
    soup = BeautifulSoup(response.content, "html.parser")
    text = soup.get_text(" ", strip=True)
    if "Households’ Inflation Expectations Survey" not in text and "Households' Inflation Expectations Survey" not in text:
        raise ValueError("Configured RBI page is not an IESH release.")
    release = re.search(r"Date\s*:\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})", text)
    if not release:
        raise ValueError("Could not read the RBI release date.")
    publication_date = datetime.strptime(release.group(1), "%b %d, %Y").date().isoformat()
    table = next((table for table in soup.find_all("table")
                  if "Table 3:" in table.get_text(" ", strip=True)
                  and "Household Inflation Expectations" in table.get_text(" ", strip=True)
                  and "One Year Ahead" in table.get_text(" ", strip=True)), None)
    if table is None:
        raise ValueError("Expected RBI Table 12 was not found.")
    observations = []
    for row in table.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
        if len(cells) < 13 or not re.fullmatch(r"[A-Za-z]{3}-\d{2}", cells[0]):
            continue
        period = datetime.strptime(cells[0], "%b-%y").strftime("%Y-%m")
        # Table 3 groups each horizon as mean estimate/SE then median
        # estimate/SE. The second-from-last entry is the one-year median.
        observations.append((period, float(cells[-2])))
    if not observations:
        raise ValueError("Could not parse RBI median one-year-ahead expectation from Table 3.")
    period, value = max(observations)
    return {
        "indicator": "inflation", "reference_period": period, "publication_date": publication_date,
        "value": value, "unit": "% median household inflation expectation, one year ahead",
        "source_name": "RBI Inflation Expectations Survey of Households (IESH)", "source_url": RBI_IESH_URL,
    }


def fetch_fbil_yields() -> list[dict]:
    archive = get(FBIL_ARCHIVE_API, params={"authenticated": "false"}).json()
    dates = sorted({str(row["processRunDate"]) for row in archive if row.get("processRunDate")}, reverse=True)[:2]
    if len(dates) < 2:
        raise ValueError("FBIL has fewer than two available daily workbooks.")
    observations = []
    for observed in dates:
        content = get(FBIL_WORKBOOK_API, params={"date": observed}).content
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        if "Par Yield" not in workbook.sheetnames:
            raise ValueError("FBIL workbook is missing its Par Yield sheet.")
        value = None
        for row in workbook["Par Yield"].iter_rows(min_row=6, values_only=True):
            try:
                if abs(float(row[0]) - 10.0) < 1e-9:
                    value = float(row[1])  # YTM % p.a. on the semi-annual basis
                    break
            except (TypeError, ValueError, IndexError):
                continue
        if value is None:
            raise ValueError(f"FBIL 10-year par-yield row missing for {observed}.")
        observations.append({
            "indicator": "bond_yield", "reference_period": observed, "publication_date": observed,
            "value": value, "unit": "% FBIL 10-year G-Sec par yield, semi-annual basis",
            "source_name": "FBIL GOI Prices including Par Yield", "source_url": "https://www.fbil.org.in/",
        })
    return observations


def fetch_authorized_pmi_csv() -> dict | None:
    """Read PMI only from a user-supplied licensed file; never scrape S&P data."""
    path = Path(os.getenv("PMI_CSV_PATH", str(PMI_CSV)))
    if not path.exists():
        return None
    with path.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        return None
    if not {str(key).lower().strip() for key in rows[0]}.issuperset({"date", "value"}):
        raise ValueError("PMI CSV must contain date,value columns.")
    valid = []
    for row in rows:
        normalized = {str(key).lower().strip(): value for key, value in row.items()}
        try:
            published = date.fromisoformat(normalized["date"].strip())
            value = float(normalized["value"])
        except (ValueError, TypeError, KeyError):
            continue
        valid.append((published, value))
    if not valid:
        return None
    published, value = max(valid)
    return {
        "indicator": "pmi", "reference_period": published.strftime("%Y-%m"),
        "publication_date": published.isoformat(), "value": value,
        "unit": "headline manufacturing PMI", "source_name": "Authorized PMI CSV supplied by user",
        "source_url": None,
    }


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS macro_indicator_observations (
            indicator TEXT NOT NULL, reference_period TEXT NOT NULL, publication_date TEXT NOT NULL,
            value REAL NOT NULL, unit TEXT NOT NULL, source_name TEXT NOT NULL, source_url TEXT,
            fetched_at TEXT NOT NULL, PRIMARY KEY(indicator,reference_period,source_name)
        );
        CREATE INDEX IF NOT EXISTS idx_macro_indicator_latest
            ON macro_indicator_observations(indicator,publication_date);
        CREATE TABLE IF NOT EXISTS macro_factor_decisions (
            decision_date TEXT PRIMARY KEY, computed_at TEXT NOT NULL, growth_state TEXT NOT NULL,
            rates_state TEXT NOT NULL, yield_direction TEXT NOT NULL, recommendation TEXT NOT NULL,
            reason TEXT NOT NULL, inputs_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS macro_refresh_log (
            refresh_id INTEGER PRIMARY KEY AUTOINCREMENT, refreshed_at TEXT NOT NULL,
            indicator TEXT NOT NULL, status TEXT NOT NULL, detail TEXT
        );
    """)


def store_observation(db: sqlite3.Connection, item: dict, fetched_at: str) -> None:
    db.execute("""
        INSERT INTO macro_indicator_observations
            (indicator,reference_period,publication_date,value,unit,source_name,source_url,fetched_at)
        VALUES (:indicator,:reference_period,:publication_date,:value,:unit,:source_name,:source_url,:fetched_at)
        ON CONFLICT(indicator,reference_period,source_name) DO UPDATE SET
            publication_date=excluded.publication_date,value=excluded.value,unit=excluded.unit,
            source_url=excluded.source_url,fetched_at=excluded.fetched_at
    """, {**item, "fetched_at": fetched_at})


def get_latest(db: sqlite3.Connection, indicator: str) -> dict | None:
    row = db.execute("""
        SELECT indicator,reference_period,publication_date,value,unit,source_name,source_url
        FROM macro_indicator_observations WHERE indicator=?
        ORDER BY publication_date DESC,reference_period DESC LIMIT 1
    """, (indicator,)).fetchone()
    keys = ("indicator", "reference_period", "publication_date", "value", "unit", "source_name", "source_url")
    return dict(zip(keys, row)) if row else None


def classify_growth(gdp: float | None, pmi: float | None) -> str:
    if gdp is None or pmi is None:
        return "Insufficient data"
    a = "Up" if gdp >= 6 else "Down" if gdp <= 5 else "Neutral"
    b = "Up" if pmi > 55 else "Down" if pmi < 50 else "Neutral"
    return a if a == b and a in {"Up", "Down"} else "Mixed / Neutral"


def classify_rates(inflation: float | None, trend: str) -> str:
    if inflation is None or trend not in {"Rising", "Falling"}:
        return "Insufficient data"
    a = "Down" if inflation < 5 else "Up" if inflation > 6 else "Neutral"
    b = "Up" if trend == "Rising" else "Down"
    return a if a == b else "Mixed / Neutral"


def build_snapshot(db: sqlite3.Connection, timestamp: datetime) -> dict:
    today = timestamp.astimezone(INDIA_TZ).date()
    indicators = {name: get_latest(db, name) for name in ("gdp", "pmi", "inflation", "bond_yield")}
    freshness = {}
    for name, item in indicators.items():
        age = (today - date.fromisoformat(item["publication_date"])).days if item else None
        freshness[name] = {"age_days": age, "max_age_days": MAX_AGE_DAYS[name],
                           "fresh": age is not None and 0 <= age <= MAX_AGE_DAYS[name]}
    recent_yields = db.execute("""
        SELECT publication_date,value FROM macro_indicator_observations
        WHERE indicator='bond_yield' ORDER BY publication_date DESC LIMIT 2
    """).fetchall()
    two_fresh_yields = len(recent_yields) == 2 and all(
        0 <= (today - date.fromisoformat(row[0])).days <= MAX_AGE_DAYS["bond_yield"]
        for row in recent_yields
    )
    yield_direction = "Insufficient data"
    if two_fresh_yields:
        current, previous = float(recent_yields[0][1]), float(recent_yields[1][1])
        yield_direction = "Rising" if current > previous else "Falling" if current < previous else "Unchanged"
    freshness["bond_yield"]["fresh"] &= two_fresh_yields

    value = lambda name: indicators[name]["value"] if freshness[name]["fresh"] else None
    growth = classify_growth(value("gdp"), value("pmi"))
    rates = classify_rates(value("inflation"), yield_direction)
    recommendation = {("Up", "Down"): "Momentum", ("Up", "Up"): "Value",
                      ("Down", "Down"): "Quality"}.get((growth, rates), "No recommendation")
    if not all(entry["fresh"] for entry in freshness.values()):
        recommendation = "No recommendation"

    reasons = []
    missing = [name for name, fresh in freshness.items() if not fresh["fresh"]]
    if "pmi" in missing:
        reasons.append("No fresh authorized PMI CSV; PMI data is not scraped or republished.")
    if "gdp" in missing:
        reasons.append("GDP is missing, stale, or has no verified publication date.")
    if "inflation" in missing:
        reasons.append("RBI inflation expectation is missing or stale.")
    if "bond_yield" in missing:
        reasons.append("Two fresh FBIL yield observations are required.")
    if not missing and growth == "Mixed / Neutral":
        reasons.append("GDP and PMI do not agree on a supported growth direction.")
    if not missing and rates == "Mixed / Neutral":
        reasons.append("Inflation and the 10-year yield direction do not agree.")
    if not missing and (growth, rates) == ("Down", "Up"):
        reasons.append("The framework does not define Growth Down + Rates Up.")
    if recommendation != "No recommendation":
        reasons.append(f"{growth} growth and {rates} rates map to {recommendation}.")
    if not reasons:
        reasons.append("The available signals do not produce a defined factor.")

    snapshot = {
        "updated_at": timestamp.astimezone(INDIA_TZ).isoformat(timespec="seconds"),
        "recommendation": recommendation, "growth_state": growth, "rates_state": rates,
        "yield_direction": yield_direction, "reason": " ".join(reasons),
        "freshness": freshness, "indicators": indicators,
    }
    db.execute("""
        INSERT INTO macro_factor_decisions
            (decision_date,computed_at,growth_state,rates_state,yield_direction,recommendation,reason,inputs_json)
        VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(decision_date) DO UPDATE SET
            computed_at=excluded.computed_at,growth_state=excluded.growth_state,
            rates_state=excluded.rates_state,yield_direction=excluded.yield_direction,
            recommendation=excluded.recommendation,reason=excluded.reason,inputs_json=excluded.inputs_json
    """, (today.isoformat(), snapshot["updated_at"], growth, rates, yield_direction, recommendation,
          snapshot["reason"], json.dumps({"freshness": freshness, "indicators": indicators}, separators=(",", ":"))))
    return snapshot


def refresh_and_export() -> dict:
    if not DATABASE.exists():
        raise SystemExit(f"Existing database not found: {DATABASE}")
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fetchers = [("gdp", fetch_gdp), ("inflation", fetch_inflation),
                ("bond_yield", fetch_fbil_yields), ("pmi", fetch_authorized_pmi_csv)]
    with sqlite3.connect(DATABASE) as db:
        ensure_schema(db)
        for name, fetcher in fetchers:
            try:
                result = fetcher()
                items = result if isinstance(result, list) else ([result] if result else [])
                for item in items:
                    store_observation(db, item, fetched_at)
                status = "saved" if items else "not supplied"
                detail = f"Saved {len(items)} observation(s)" if items else "No authorized PMI CSV supplied"
            except Exception as error:
                status, detail = "error", f"{type(error).__name__}: {error}"
            db.execute("INSERT INTO macro_refresh_log(refreshed_at,indicator,status,detail) VALUES (?,?,?,?)",
                       (fetched_at, name, status, detail))
            print(f"{name}: {status} — {detail}")
        snapshot = build_snapshot(db, datetime.now(timezone.utc))
        db.commit()
    PUBLIC_JSON.parent.mkdir(parents=True, exist_ok=True)
    PUBLIC_JSON.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"Wrote {PUBLIC_JSON.relative_to(ROOT)} ({snapshot['recommendation']}).")
    return snapshot


if __name__ == "__main__":
    refresh_and_export()
