
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import time
import csv
import re
import difflib
from io import StringIO

# ==========================================
# SETTINGS
# ==========================================

INDEX_NAME = "NIFTY100 + BSE500"

INDEX_URL = (
    "https://www.niftyindices.com/IndexConstituent/"
    "ind_nifty100list.csv"
)
BSE500_URL = (
    "https://www.bseindices.com/AsiaIndexAPI/api/"
    "Codewise_IndicesDownload/w?code=17"
)
NSE_SYMBOLS_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"

OUTPUT_CSV = "NIFTY100_BSE500_Momentum.csv"
OUTPUT_EXCEL = "NIFTY100_BSE500_Momentum.xlsx"

# ==========================================
# GET LATEST NIFTY 100 + BSE 500 STOCK LIST
# ==========================================

def normalize_company_name(name):
    name = re.sub(r"[^A-Z0-9]", "", str(name).upper())
    return re.sub(r"(LIMITED|LTD)$", "", name)


def match_nse_symbol(company_name, normalized_names, exact_map):
    """Map a BSE company name to its NSE symbol for Yahoo Finance."""
    normalized = normalize_company_name(company_name)
    if normalized in exact_map:
        return exact_map[normalized]

    # BSE's downloadable list truncates some company names. Accept only a
    # high-confidence, clearly better match to avoid assigning the wrong stock.
    matches = difflib.get_close_matches(
        normalized, normalized_names, n=2, cutoff=0.90
    )
    if not matches:
        return None

    best = matches[0]
    best_score = difflib.SequenceMatcher(None, normalized, best).ratio()
    if len(matches) > 1:
        second_score = difflib.SequenceMatcher(None, normalized, matches[1]).ratio()
        if best_score - second_score < 0.03:
            return None

    return exact_map.get(best)

def get_stocks():

    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    response = requests.get(
        INDEX_URL,
        headers=headers,
        timeout=30
    )

    response.raise_for_status()

    # Some versions of the constituents file contain unquoted commas in
    # company names. That shifts the remaining columns and makes pandas'
    # default C parser fail with "Expected N fields ... saw M".
    rows = csv.reader(StringIO(response.text))
    try:
        header = next(rows)
    except StopIteration as exc:
        raise ValueError("NIFTY100 constituents response was empty") from exc

    header = [column.strip().lstrip("\ufeff") for column in header]
    if "Symbol" not in header:
        preview = response.text[:200].replace("\n", " ")
        raise ValueError(
            "NIFTY100 constituents response does not contain a 'Symbol' "
            f"column. Response starts with: {preview!r}"
        )

    # Preserve the fixed columns at the end of each row and fold any extra
    # comma-separated pieces back into the first (company name) column.
    expected_fields = len(header)
    parsed_rows = []
    for row in rows:
        if len(row) > expected_fields:
            extra = len(row) - expected_fields
            row = [",".join(row[:extra + 1]), *row[extra + 1:]]
        if len(row) == expected_fields:
            parsed_rows.append(row)

    constituents = pd.DataFrame(parsed_rows, columns=header)

    nifty100_stocks = (
        constituents["Symbol"]
        .dropna()
        .astype(str)
        .str.strip()
        .unique()
        .tolist()
    )

    # BSE provides numeric security codes, so map company names to NSE
    # trading symbols (Yahoo Finance's .NS format) using the NSE master list.
    bse_response = requests.get(
        BSE500_URL,
        headers={**headers, "Referer": "https://www.bseindices.com/"},
        timeout=30,
    )
    bse_response.raise_for_status()
    bse_rows = csv.reader(StringIO(bse_response.text))
    bse_header = next(bse_rows, [])
    if bse_header[:2] != ["Constituents", "Symbol"]:
        raise ValueError("BSE 500 download has an unexpected CSV format")

    bse_records = []
    for row in bse_rows:
        if len(row) > len(bse_header):
            extra = len(row) - len(bse_header)
            row = [",".join(row[:extra + 1]), *row[extra + 1:]]
        if len(row) == len(bse_header):
            bse_records.append(row)
    bse_constituents = pd.DataFrame(bse_records, columns=bse_header)
    if not {"Constituents", "Symbol"}.issubset(bse_constituents.columns):
        raise ValueError("BSE 500 download has an unexpected CSV format")

    nse_response = requests.get(
        NSE_SYMBOLS_URL,
        headers=headers,
        timeout=30,
    )
    nse_response.raise_for_status()
    nse_master = pd.read_csv(StringIO(nse_response.text))
    required_nse_columns = {"SYMBOL", "NAME OF COMPANY"}
    if not required_nse_columns.issubset(nse_master.columns):
        raise ValueError("NSE symbol master has an unexpected CSV format")

    nse_master["_normalized_name"] = nse_master["NAME OF COMPANY"].map(
        normalize_company_name
    )
    exact_map = dict(zip(nse_master["_normalized_name"], nse_master["SYMBOL"]))
    normalized_names = list(exact_map)
    company_names = bse_constituents["Constituents"].dropna().astype(str)

    bse500_stocks = []
    unmapped = []
    for company_name in company_names:
        symbol = match_nse_symbol(company_name, normalized_names, exact_map)
        if symbol:
            bse500_stocks.append(str(symbol).strip())
        else:
            unmapped.append(company_name)

    # The BSE 500 contains most NIFTY 100 members. Keep the union and scan
    # overlaps only once.
    stocks = list(dict.fromkeys(nifty100_stocks + bse500_stocks))
    if unmapped:
        print(
            f"Could not map {len(unmapped)} BSE 500 constituents to an NSE "
            "symbol; they will be skipped."
        )

    # Explicit symbol handling for Yahoo Finance
    ticker_map = {
        "TMPV": "TMPV.NS",
        "TMCV": "TMCV.NS",
        "M&M": "M&M.NS",
        "BAJAJ-AUTO": "BAJAJ-AUTO.NS"
    }

    return stocks, ticker_map


# ==========================================
# CALCULATE MOMENTUM INDICATORS
# ==========================================

def analyse_stock(symbol, ticker_map):

    ticker = ticker_map.get(
        symbol,
        symbol + ".NS"
    )

    try:

        df = yf.download(
            ticker,
            period="1y",
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=False
        )

        if df.empty or len(df) < 200:
            print("Insufficient data:", symbol)
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        close = df["Close"]
        high = df["High"]
        low = df["Low"]
        volume = df["Volume"]

        # Moving averages
        sma20 = close.rolling(20).mean()
        sma50 = close.rolling(50).mean()
        sma200 = close.rolling(200).mean()

        # RSI 14
        delta = close.diff()

        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)

        avg_gain = gain.ewm(
            alpha=1/14,
            min_periods=14,
            adjust=False
        ).mean()

        avg_loss = loss.ewm(
            alpha=1/14,
            min_periods=14,
            adjust=False
        ).mean()

        rs = avg_gain / avg_loss

        rsi = 100 - (100 / (1 + rs))

        # MACD
        ema12 = close.ewm(
            span=12,
            adjust=False
        ).mean()

        ema26 = close.ewm(
            span=26,
            adjust=False
        ).mean()

        macd = ema12 - ema26

        signal = macd.ewm(
            span=9,
            adjust=False
        ).mean()

        # Volume confirmation
        avg_volume = volume.rolling(20).mean()

        volume_ratio = volume / avg_volume

        # Returns
        return_1m = (
            close.iloc[-1] / close.iloc[-21] - 1
        ) * 100

        return_3m = (
            close.iloc[-1] / close.iloc[-63] - 1
        ) * 100

        return_6m = (
            close.iloc[-1] / close.iloc[-126] - 1
        ) * 100

        latest = float(close.iloc[-1])
        latest_sma20 = float(sma20.iloc[-1])
        latest_sma50 = float(sma50.iloc[-1])
        latest_sma200 = float(sma200.iloc[-1])
        latest_rsi = float(rsi.iloc[-1])
        latest_macd = float(macd.iloc[-1])
        latest_signal = float(signal.iloc[-1])
        latest_volume_ratio = float(volume_ratio.iloc[-1])

        # Trend conditions
        bullish_trend = (
            latest > latest_sma50 >
            latest_sma200
        )

        positive_rsi = latest_rsi > 50
        positive_macd = latest_macd > latest_signal
        volume_confirmation = latest_volume_ratio > 1

        # Classification
        if (
            bullish_trend
            and positive_rsi
            and positive_macd
            and volume_confirmation
        ):
            status = "Strong Bullish"

        elif (
            latest > latest_sma50
            and positive_rsi
            and positive_macd
        ):
            status = "Moderate Bullish"

        else:
            status = "Weak / Mixed"

        return {
            "Stock": symbol,
            "Yahoo_Ticker": ticker,
            "Date": df.index[-1].date(),
            "Close": round(latest, 2),
            "SMA_20": round(latest_sma20, 2),
            "SMA_50": round(latest_sma50, 2),
            "SMA_200": round(latest_sma200, 2),
            "RSI_14": round(latest_rsi, 2),
            "MACD": round(latest_macd, 2),
            "MACD_Signal": round(latest_signal, 2),
            "Volume_Ratio": round(latest_volume_ratio, 2),
            "Return_1M_%": round(float(return_1m), 2),
            "Return_3M_%": round(float(return_3m), 2),
            "Return_6M_%": round(float(return_6m), 2),
            "Bullish_Trend": bullish_trend,
            "Volume_Confirmation": volume_confirmation,
            "Momentum_Status": status
        }

    except Exception as e:
        print("Error:", symbol, e)
        return None


# ==========================================
# RUN SCANNER
# ==========================================

print("\nDownloading", INDEX_NAME, "constituents...")

stocks, ticker_map = get_stocks()

print("Total stocks found:", len(stocks))

results = []

for i, stock in enumerate(stocks, start=1):

    print(
        f"Scanning {i}/{len(stocks)}: {stock}"
    )

    result = analyse_stock(
        stock,
        ticker_map
    )

    if result is not None:
        results.append(result)

    time.sleep(0.3)


# ==========================================
# RANK MOMENTUM
# ==========================================

if not results:
    raise ValueError(
        "No stock data retrieved. Check internet or ticker availability."
    )

final_df = pd.DataFrame(results)

# Weighted relative momentum score
final_df["Momentum_Score"] = (
    final_df["Return_1M_%"] * 0.20
    + final_df["Return_3M_%"] * 0.40
    + final_df["Return_6M_%"] * 0.40
).round(2)

# Sort highest momentum first
final_df = final_df.sort_values(
    "Momentum_Score",
    ascending=False
).reset_index(drop=True)

final_df.insert(
    0,
    "Rank",
    range(1, len(final_df) + 1)
)

# ==========================================
# EXPORT FILES
# ==========================================

final_df.to_csv(
    OUTPUT_CSV,
    index=False
)

final_df.to_excel(
    OUTPUT_EXCEL,
    index=False
)

# ==========================================
# DISPLAY TOP STOCKS
# ==========================================

print("\n================================")
print("NIFTY100 + BSE500 MOMENTUM RESULTS")
print("================================")

print(
    final_df.head(20).to_string(index=False)
)

print("\nTotal stocks successfully scanned:", len(final_df))

print("\nFiles saved:")
print(OUTPUT_CSV)
print(OUTPUT_EXCEL)

# ==========================================
# RANK, PRETTY PRINT AND EXPORT
# ==========================================

if not results:
    raise ValueError("No stock data retrieved.")

final_df = pd.DataFrame(results)

# Weighted momentum score
final_df["Momentum_Score"] = (
    final_df["Return_1M_%"] * 0.20
    + final_df["Return_3M_%"] * 0.40
    + final_df["Return_6M_%"] * 0.40
).round(2)

# Sort highest score first
final_df = final_df.sort_values(
    by="Momentum_Score",
    ascending=False
).reset_index(drop=True)

# Add ranking
final_df.insert(
    0,
    "Rank",
    range(1, len(final_df) + 1)
)

# ==========================================
# PRETTY PRINT
# ==========================================

print("\n" + "=" * 115)
print("               NIFTY100 + BSE500 MOMENTUM LEADERBOARD")
print("=" * 115)

print(f"Total stocks scanned : {len(final_df)}")
print(f"Data date            : {final_df['Date'].max()}")
print("=" * 115)

# Display columns
display_columns = [
    "Rank",
    "Stock",
    "Close",
    "Momentum_Score",
    "Return_1M_%",
    "Return_3M_%",
    "Return_6M_%",
    "RSI_14",
    "MACD",
    "Volume_Ratio",
    "Momentum_Status"
]

# Format output
pretty_df = final_df[display_columns].copy()

print(
    pretty_df.head(20).to_string(
        index=False,
        formatters={
            "Close": "{:.2f}".format,
            "Momentum_Score": "{:+.2f}".format,
            "Return_1M_%": "{:+.2f}%".format,
            "Return_3M_%": "{:+.2f}%".format,
            "Return_6M_%": "{:+.2f}%".format,
            "RSI_14": "{:.2f}".format,
            "MACD": "{:.2f}".format,
            "Volume_Ratio": "{:.2f}x".format
        }
    )
)

print("\n" + "=" * 115)
print("                  MOMENTUM CATEGORY SUMMARY")
print("=" * 115)

print(
    final_df["Momentum_Status"]
    .value_counts()
    .to_string()
)

# ==========================================
# EXPORT FULL SORTED RESULTS
# ==========================================

final_df.to_csv(
    OUTPUT_CSV,
    index=False
)

# Excel with formatting
with pd.ExcelWriter(
    OUTPUT_EXCEL,
    engine="openpyxl"
) as writer:

    final_df.to_excel(
        writer,
        sheet_name="Momentum Ranking",
        index=False
    )

    worksheet = writer.sheets["Momentum Ranking"]

    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions

    # Auto-adjust column widths
    for column in worksheet.columns:

        max_length = max(
            len(str(cell.value or ""))
            for cell in column
        )

        worksheet.column_dimensions[
            column[0].column_letter
        ].width = min(max_length + 3, 25)

print("\n" + "=" * 115)
print("FILES SAVED SUCCESSFULLY")
print("=" * 115)

print(f"1. {OUTPUT_CSV}")
print(f"2. {OUTPUT_EXCEL}")

print("\nTOP 5 MOMENTUM STOCKS")
print(
    final_df[
        ["Rank", "Stock", "Momentum_Score", "Momentum_Status"]
    ].head(5).to_string(index=False)
)

print("\nSCAN COMPLETED")
