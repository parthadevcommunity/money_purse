
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import sqlite3
import json
import time
from io import StringIO
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# ==========================================
# SETTINGS
# ==========================================

INDEX_NAME = "NSE listed stocks"
NSE_SYMBOLS_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"

OUTPUT_CSV = "NSE_All_Stocks_Momentum.csv"
OUTPUT_EXCEL = "NSE_All_Stocks_Momentum.xlsx"
OUTPUT_V2_CSV = "NSE_All_Stocks_Momentum_V2.csv"
DATABASE_PATH = Path(__file__).with_name("nse_stock_prices.sqlite3")
MARKET_CAP_CSV = Path(__file__).with_name("amfi_market_cap_2026-06.csv")
INITIAL_HISTORY_DAYS = 400
REFRESH_OVERLAP_DAYS = 7
INDIA_TZ = ZoneInfo("Asia/Kolkata")

# ==========================================
# GET CURRENT NSE STOCK UNIVERSE
# ==========================================

def get_stocks():
    response = requests.get(
        NSE_SYMBOLS_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    response.raise_for_status()
    nse_master = pd.read_csv(StringIO(response.text))
    if "SYMBOL" not in nse_master.columns:
        raise ValueError("NSE equity list does not contain a SYMBOL column")

    stocks = (
        nse_master["SYMBOL"]
        .dropna()
        .astype(str)
        .str.strip()
        .unique()
        .tolist()
    )

    # Explicit symbol handling for Yahoo Finance
    ticker_map = {
        "TMPV": "TMPV.NS",
        "TMCV": "TMCV.NS",
        "M&M": "M&M.NS",
        "BAJAJ-AUTO": "BAJAJ-AUTO.NS"
    }

    return stocks, ticker_map


def open_price_database():
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS daily_prices (
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            PRIMARY KEY (symbol, trade_date)
        );
        CREATE INDEX IF NOT EXISTS idx_daily_prices_symbol_date
            ON daily_prices(symbol, trade_date);
        CREATE TABLE IF NOT EXISTS sync_state (
            symbol TEXT PRIMARY KEY,
            last_checked_date TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS momentum_rank_history (
            snapshot_date TEXT NOT NULL,
            data_date TEXT NOT NULL,
            model TEXT NOT NULL,
            symbol TEXT NOT NULL,
            rank INTEGER NOT NULL,
            score REAL,
            momentum_status TEXT,
            close REAL,
            metrics_json TEXT,
            PRIMARY KEY (snapshot_date, model, symbol)
        );
        CREATE INDEX IF NOT EXISTS idx_momentum_rank_history_lookup
            ON momentum_rank_history(model, snapshot_date, rank);
        CREATE TABLE IF NOT EXISTS market_cap_categories (
            symbol TEXT PRIMARY KEY,
            company TEXT,
            category TEXT NOT NULL,
            average_market_cap REAL,
            as_of TEXT NOT NULL
        );
        """
    )
    rank_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(momentum_rank_history)")
    }
    if "metrics_json" not in rank_columns:
        connection.execute("ALTER TABLE momentum_rank_history ADD COLUMN metrics_json TEXT")
    if MARKET_CAP_CSV.is_file():
        cap_data = pd.read_csv(MARKET_CAP_CSV).fillna("")
        connection.executemany(
            """
            INSERT INTO market_cap_categories
                (symbol, company, category, average_market_cap, as_of)
            VALUES (?, ?, ?, ?, '2026-06-30')
            ON CONFLICT(symbol) DO UPDATE SET
                company=excluded.company,
                category=excluded.category,
                average_market_cap=excluded.average_market_cap,
                as_of=excluded.as_of
            """,
            [
                (
                    str(row["NSE Symbol"]).strip().upper(),
                    str(row["Company"]).strip(),
                    str(row["Category"]).strip(),
                    float(row["Average Market Cap (Rs Cr)"])
                    if str(row["Average Market Cap (Rs Cr)"]).strip()
                    else None,
                )
                for _, row in cap_data.iterrows()
                if str(row["NSE Symbol"]).strip() and str(row["Category"]).strip()
            ],
        )
    connection.commit()
    return connection


def save_momentum_rank_history(v1_df, v2_df):
    """Persist this run's V1 and V2 rankings for later forward-return analysis."""
    snapshot_date = datetime.now(INDIA_TZ).date().isoformat()
    connection = open_price_database()
    try:
        for model, frame, rank_column, score_column in (
            ("V1", v1_df, "Rank", "Momentum_Score"),
            ("V2", v2_df, "Momentum_V2_Rank", "Momentum_V2_Score_%"),
        ):
            rows = []
            for _, row in frame.iterrows():
                metrics = {
                    column: (
                        "" if pd.isna(value)
                        else pd.Timestamp(value).date().isoformat() if column == "Date"
                        else str(value)
                    )
                    for column, value in row.items()
                }
                rows.append((
                    snapshot_date,
                    pd.Timestamp(row["Date"]).date().isoformat(),
                    model,
                    str(row["Stock"]),
                    int(row[rank_column]),
                    float(row[score_column]),
                    str(row.get("Momentum_Status", "")),
                    float(row["Close"]),
                    json.dumps(metrics, allow_nan=False),
                ))
            connection.executemany(
                """
                INSERT INTO momentum_rank_history
                    (snapshot_date, data_date, model, symbol, rank, score,
                     momentum_status, close, metrics_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_date, model, symbol) DO UPDATE SET
                    data_date = excluded.data_date,
                    rank = excluded.rank,
                    score = excluded.score,
                    momentum_status = excluded.momentum_status,
                    close = excluded.close,
                    metrics_json = excluded.metrics_json
                """,
                rows,
            )
        connection.commit()
        print(
            f"Saved ranking history: {len(v1_df)} V1 and {len(v2_df)} V2 stocks "
            f"for {snapshot_date}"
        )
    finally:
        connection.close()


def refresh_price_cache(connection, symbol, ticker):
    today = datetime.now(INDIA_TZ).date()
    today_text = today.isoformat()
    last_checked = connection.execute(
        "SELECT last_checked_date FROM sync_state WHERE symbol = ?",
        (symbol,),
    ).fetchone()
    if last_checked and last_checked[0] == today_text:
        return False

    last_trade_date = connection.execute(
        "SELECT MAX(trade_date) FROM daily_prices WHERE symbol = ?",
        (symbol,),
    ).fetchone()[0]
    if last_trade_date:
        start_date = (
            date.fromisoformat(last_trade_date)
            - timedelta(days=REFRESH_OVERLAP_DAYS)
        )
    else:
        start_date = today - timedelta(days=INITIAL_HISTORY_DAYS)

    prices = yf.download(
        ticker,
        start=start_date.isoformat(),
        end=(today + timedelta(days=1)).isoformat(),
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=False,
    )

    if not prices.empty:
        if isinstance(prices.columns, pd.MultiIndex):
            prices.columns = prices.columns.get_level_values(0)
        rows = []
        for timestamp, row in prices.iterrows():
            trade_date = pd.Timestamp(timestamp).date().isoformat()
            values = [row.get(name) for name in ("Open", "High", "Low", "Close", "Volume")]
            if pd.isna(values[3]):
                continue
            rows.append((
                symbol,
                trade_date,
                *(None if pd.isna(value) else float(value) for value in values[:4]),
                None if pd.isna(values[4]) else int(values[4]),
            ))

        connection.executemany(
            """
            INSERT INTO daily_prices
                (symbol, trade_date, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, trade_date) DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                volume = excluded.volume
            WHERE daily_prices.open IS NOT excluded.open
               OR daily_prices.high IS NOT excluded.high
               OR daily_prices.low IS NOT excluded.low
               OR daily_prices.close IS NOT excluded.close
               OR daily_prices.volume IS NOT excluded.volume
            """,
            rows,
        )

    connection.execute(
        """
        INSERT INTO sync_state (symbol, last_checked_date)
        VALUES (?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            last_checked_date = excluded.last_checked_date
        """,
        (symbol, today_text),
    )
    connection.commit()
    time.sleep(0.3)
    return True


def load_cached_prices(connection, symbol):
    first_date = (
        datetime.now(INDIA_TZ).date() - timedelta(days=INITIAL_HISTORY_DAYS)
    ).isoformat()
    rows = connection.execute(
        """
        SELECT trade_date, open, high, low, close, volume
        FROM daily_prices
        WHERE symbol = ? AND trade_date >= ?
        ORDER BY trade_date
        """,
        (symbol, first_date),
    ).fetchall()
    if not rows:
        return pd.DataFrame()

    frame = pd.DataFrame(
        rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"]
    )
    frame["Date"] = pd.to_datetime(frame["Date"])
    return frame.set_index("Date")


def calculate_momentum_v2(df):
    """Apply the attached 8-rule EMA/ATR/CCI/volume selection matrix."""
    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"]
    latest_close = float(close.iloc[-1])

    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    latest_ema20 = float(ema20.iloc[-1])
    latest_ema50 = float(ema50.iloc[-1])

    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr14 = true_range.rolling(14).mean().iloc[-1]
    atr14_pct = float(atr14 / latest_close * 100) if pd.notna(atr14) else np.nan

    typical_price = (high + low + close) / 3
    typical_sma20 = typical_price.rolling(20).mean()
    mean_deviation20 = typical_price.rolling(20).apply(
        lambda values: np.mean(np.abs(values - np.mean(values))),
        raw=True,
    )
    cci20 = (typical_price - typical_sma20) / (0.015 * mean_deviation20)
    latest_cci_values = cci20.dropna().tail(20).to_numpy()
    cci_slope = (
        float(np.polyfit(np.arange(len(latest_cci_values)), latest_cci_values, 1)[0])
        if len(latest_cci_values) > 1
        else np.nan
    )

    high_52w = float(high.tail(252).max()) if len(df) >= 252 else np.nan
    pct_below_52w_high = (
        (high_52w - latest_close) / high_52w * 100
        if np.isfinite(high_52w) and high_52w > 0
        else np.nan
    )

    avg_volume20 = float(volume.tail(20).mean())
    avg_volume2 = float(volume.tail(2).mean())
    avg_volume5 = float(volume.tail(5).mean())

    conditions = {
        "V2_Close_Above_EMA50": (latest_close > latest_ema50, 10),
        "V2_Close_Above_EMA20": (latest_close > latest_ema20, 10),
        "V2_EMA20_Above_EMA50": (latest_ema20 > latest_ema50, 20),
        "V2_Within_20Pct_Of_52W_High": (
            np.isfinite(pct_below_52w_high)
            and (latest_close >= high_52w or 0 <= pct_below_52w_high <= 20),
            30,
        ),
        "V2_ATR14_Below_3Pct": (np.isfinite(atr14_pct) and atr14_pct < 3, 10),
        "V2_CCI20_Rising": (np.isfinite(cci_slope) and cci_slope > 0, 10),
        "V2_Volume_Above_20D_Avg": (float(volume.iloc[-1]) > avg_volume20, 5),
        "V2_Avg_Volume_2D_Above_5D": (avg_volume2 > avg_volume5, 5),
    }

    result = {
        "Momentum_V2_Score_%": int(
            sum(weight for passed, weight in conditions.values() if passed)
        ),
        "V2_EMA20": round(latest_ema20, 2),
        "V2_EMA50": round(latest_ema50, 2),
        "V2_52W_High": round(high_52w, 2) if np.isfinite(high_52w) else None,
        "V2_Pct_Below_52W_High": (
            round(pct_below_52w_high, 2)
            if np.isfinite(pct_below_52w_high)
            else None
        ),
        "V2_ATR14_%": round(atr14_pct, 2) if np.isfinite(atr14_pct) else None,
        "V2_CCI20_Slope": round(cci_slope, 2) if np.isfinite(cci_slope) else None,
    }
    result.update({name: bool(passed) for name, (passed, _) in conditions.items()})
    return result


# ==========================================
# CALCULATE MOMENTUM INDICATORS
# ==========================================

def analyse_stock(symbol, ticker_map, connection):

    ticker = ticker_map.get(
        symbol,
        symbol + ".NS"
    )

    try:
        try:
            refresh_price_cache(connection, symbol, ticker)
        except Exception as refresh_error:
            print("Price update failed:", symbol, refresh_error)

        df = load_cached_prices(connection, symbol)
        # Yahoo can include placeholder rows with no traded volume. They are
        # not trading sessions and would otherwise distort rolling indicators
        # and return lookbacks for illiquid stocks.
        if not df.empty:
            df = df.loc[
                pd.to_numeric(df["Volume"], errors="coerce").fillna(0) > 0
            ]

        if df.empty or len(df) < 200:
            print("Insufficient traded history:", symbol)
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

        result = {
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
        result.update(calculate_momentum_v2(df))
        return result

    except Exception as e:
        print("Error:", symbol, e)
        return None


# ==========================================
# RUN SCANNER
# ==========================================

print("\nDownloading", INDEX_NAME, "constituents...")

stocks, ticker_map = get_stocks()

print("Total NSE symbols found:", len(stocks))

results = []
price_db = open_price_database()
try:
    for i, stock in enumerate(stocks, start=1):
        print(f"Scanning {i}/{len(stocks)}: {stock}")
        result = analyse_stock(stock, ticker_map, price_db)
        if result is not None:
            results.append(result)
finally:
    price_db.close()


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

# Export a separate leaderboard sorted by the attached V2 selection matrix.
v2_df = final_df.sort_values(
    "Momentum_V2_Score_%",
    ascending=False,
    kind="stable",
).reset_index(drop=True)
v2_df.insert(0, "Momentum_V2_Rank", range(1, len(v2_df) + 1))
v2_df.to_csv(OUTPUT_V2_CSV, index=False)
save_momentum_rank_history(final_df, v2_df)

final_df.to_excel(
    OUTPUT_EXCEL,
    index=False
)

# ==========================================
# DISPLAY TOP STOCKS
# ==========================================

print("\n================================")
print("NSE ALL-STOCKS MOMENTUM RESULTS")
print("================================")

print(
    final_df[
        [
            "Rank", "Stock", "Momentum_Status", "Momentum_Score",
            "Momentum_V2_Score_%",
        ]
    ].head(20).to_string(index=False)
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
print("                NSE ALL-STOCKS MOMENTUM LEADERBOARD")
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
    "Momentum_V2_Score_%",
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
            "Momentum_V2_Score_%": "{}%".format,
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
print(f"3. {OUTPUT_V2_CSV}")

print("\nTOP 5 MOMENTUM STOCKS")
print(
    final_df[
        [
            "Rank", "Stock", "Momentum_Score", "Momentum_V2_Score_%",
            "Momentum_Status",
        ]
    ].head(5).to_string(index=False)
)

print("\nSCAN COMPLETED")
