
import yfinance as yf
import pandas as pd
import numpy as np
import time

# =====================================
# SELECT YOUR NIFTY UNIVERSE
# =====================================

NIFTY_50 = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY",
    "BHARTIARTL", "TCS", "ITC", "SBIN", "LICI",
    "LT", "HINDUNILVR", "AXISBANK", "KOTAKBANK",
    "BAJFINANCE", "MARUTI", "SUNPHARMA", "M&M",
    "HCLTECH", "TATAMOTORS", "TATASTEEL",
    "NTPC", "POWERGRID", "ULTRACEMCO", "ONGC",
    "COALINDIA", "WIPRO", "TECHM", "TITAN",
    "ASIANPAINT", "NESTLEIND", "JSWSTEEL",
    "ADANIENT", "ADANIPORTS", "GRASIM", "HINDALCO",
    "BAJAJFINSV", "BAJAJ-AUTO", "DRREDDY",
    "CIPLA", "EICHERMOT", "APOLLOHOSP",
    "TATACONSUM", "DIVISLAB", "HEROMOTOCO",
    "BRITANNIA", "INDUSINDBK", "SBILIFE",
    "HDFCLIFE", "SHRIRAMFIN", "BEL"
]

# Choose: NIFTY_50 or your own stock list
stocks = NIFTY_50

# =====================================
# INDICATOR CALCULATION
# =====================================

def analyse_stock(symbol):

    ticker = symbol + ".NS"

    try:
        df = yf.download(
            ticker,
            period="1y",
            interval="1d",
            auto_adjust=True,
            progress=False
        )

        if df.empty or len(df) < 200:
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
            alpha=1/14, min_periods=14, adjust=False
        ).mean()

        avg_loss = loss.ewm(
            alpha=1/14, min_periods=14, adjust=False
        ).mean()

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        # MACD
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()

        macd = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()

        # Volume
        avg_volume = volume.rolling(20).mean()
        volume_ratio = volume / avg_volume

        # Price returns
        return_1m = (close.iloc[-1] / close.iloc[-21] - 1) * 100
        return_3m = (close.iloc[-1] / close.iloc[-63] - 1) * 100
        return_6m = (close.iloc[-1] / close.iloc[-126] - 1) * 100

        latest = close.iloc[-1]

        # Momentum conditions
        bullish_trend = (
            latest > sma50.iloc[-1] >
            sma200.iloc[-1]
        )

        positive_rsi = rsi.iloc[-1] > 50
        positive_macd = macd.iloc[-1] > signal.iloc[-1]
        volume_confirmation = volume_ratio.iloc[-1] > 1

        if bullish_trend and positive_rsi and positive_macd:
            status = "Strong Bullish"
        elif latest > sma50.iloc[-1] and positive_rsi:
            status = "Moderate Bullish"
        else:
            status = "Weak / Mixed"

        return {
            "Stock": symbol,
            "Date": df.index[-1].date(),
            "Close": round(float(latest), 2),
            "SMA_20": round(float(sma20.iloc[-1]), 2),
            "SMA_50": round(float(sma50.iloc[-1]), 2),
            "SMA_200": round(float(sma200.iloc[-1]), 2),
            "RSI_14": round(float(rsi.iloc[-1]), 2),
            "MACD": round(float(macd.iloc[-1]), 2),
            "MACD_Signal": round(float(signal.iloc[-1]), 2),
            "Volume_Ratio": round(float(volume_ratio.iloc[-1]), 2),
            "Return_1M_%": round(float(return_1m), 2),
            "Return_3M_%": round(float(return_3m), 2),
            "Return_6M_%": round(float(return_6m), 2),
            "Bullish_Trend": bullish_trend,
            "Volume_Confirmation": volume_confirmation,
            "Momentum_Status": status
        }

    except Exception as e:
        print(f"Error analysing {symbol}: {e}")
        return None


# =====================================
# SCAN ALL STOCKS
# =====================================

results = []

for stock in stocks:
    print("Scanning:", stock)

    result = analyse_stock(stock)

    if result:
        results.append(result)

    time.sleep(0.2)

# =====================================
# RANK AND EXPORT
# =====================================

if not results:
    raise ValueError("No stock data retrieved.")

final_df = pd.DataFrame(results)

# Relative momentum score (simple ranking measure)
final_df["Momentum_Score"] = (
    final_df["Return_1M_%"] * 0.20 +
    final_df["Return_3M_%"] * 0.40 +
    final_df["Return_6M_%"] * 0.40
).round(2)

final_df = final_df.sort_values(
    "Momentum_Score", ascending=False
)

final_df.insert(
    0, "Rank",
    range(1, len(final_df) + 1)
)

# Export
final_df.to_csv("Nifty_Momentum_Scanner.csv", index=False)
final_df.to_excel("Nifty_Momentum_Scanner.xlsx", index=False)

print("\n===== TOP 10 MOMENTUM STOCKS =====")
print(final_df.head(10).to_string(index=False))

print("\nFiles created:")
print("Nifty_Momentum_Scanner.csv")
print("Nifty_Momentum_Scanner.xlsx")
