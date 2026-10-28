
"""WLS-213 weekly liquidity sweep strategy."""
import pandas as pd

OUTPUT_COLUMNS = [
    "datetime_ist", "symbol", "signal", "close", "pwl", "pwh",
    "swing_level", "sweep_extreme", "risk", "reward", "rr_ratio",
]


def scan_candles(candles, symbol):
    """Scan 15-minute candles for weekly sweep and swing-break signals."""
    if isinstance(candles, pd.DataFrame):
        frame = candles.copy()
    else:
        if not candles:
            return pd.DataFrame(columns=OUTPUT_COLUMNS)
        if isinstance(candles[0], dict):
            frame = pd.DataFrame(candles).rename(
                columns={"timestamp": "datetime", "time": "datetime"}
            )
        else:
            frame = pd.DataFrame(candles)
            if len(frame.columns) < 6:
                return pd.DataFrame(columns=OUTPUT_COLUMNS)
            frame = frame.iloc[:, :6]
            frame.columns = [
                "datetime", "open", "high", "low", "close", "volume"
            ]

    required = {"datetime", "open", "high", "low", "close"}
    if not required.issubset(frame.columns):
        raise ValueError("Candle data must include datetime/open/high/low/close")

    frame["symbol"] = str(symbol)
    frame["datetime"] = pd.to_datetime(
        frame["datetime"], errors="coerce", utc=True
    )

    for col in ["open", "high", "low", "close"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")

    frame = (
        frame.dropna(subset=["datetime", "open", "high", "low", "close"])
        .sort_values("datetime")
        .drop_duplicates(subset=["datetime"], keep="last")
        .reset_index(drop=True)
    )

    if frame.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    frame["datetime_ist"] = frame["datetime"].dt.tz_convert("Asia/Kolkata")
    local_day = frame["datetime_ist"].dt.normalize()
    frame["week_start"] = local_day - pd.to_timedelta(
        frame["datetime_ist"].dt.weekday, unit="D"
    )

    # Previous observed calendar week's high and low.
    weekly = (
        frame.groupby("week_start", as_index=False)
        .agg(weekly_low=("low", "min"), weekly_high=("high", "max"))
        .sort_values("week_start")
    )
    weekly["pwl"] = weekly["weekly_low"].shift(1)
    weekly["pwh"] = weekly["weekly_high"].shift(1)

    frame = frame.merge(
        weekly[["week_start", "pwl", "pwh"]],
        on="week_start",
        how="left",
    )

    frame["buy_sweep"] = (
        frame["pwl"].notna() & (frame["low"] < frame["pwl"])
    )
    frame["sell_sweep"] = (
        frame["pwh"].notna() & (frame["high"] > frame["pwh"])
    )

    high, low = frame["high"], frame["low"]

    # A swing is confirmed only after two candles on its right have closed.
    pivot_high = (
        (high > high.shift(1))
        & (high > high.shift(2))
        & (high >= high.shift(-1))
        & (high >= high.shift(-2))
    )
    pivot_low = (
        (low < low.shift(1))
        & (low < low.shift(2))
        & (low <= low.shift(-1))
        & (low <= low.shift(-2))
    )

    frame["confirmed_swing_high"] = high.where(pivot_high).shift(2)
    frame["confirmed_swing_low"] = low.where(pivot_low).shift(2)

    results = []
    current_week = None
    last_high = None
    last_low = None

    buy_active = False
    sell_active = False
    buy_extreme = None
    sell_extreme = None
    buy_extreme_i = None
    sell_extreme_i = None
    buy_level = None
    sell_level = None

    for i, row in frame.iterrows():
        week = row["week_start"]
        close = float(row["close"])
        candle_low = float(row["low"])
        candle_high = float(row["high"])
        pwl, pwh = row["pwl"], row["pwh"]

        if week != current_week:
            current_week = week
            last_high = None
            last_low = None
            buy_active = sell_active = False
            buy_extreme = sell_extreme = None
            buy_extreme_i = sell_extreme_i = None
            buy_level = sell_level = None

        ph = row["confirmed_swing_high"]
        pl = row["confirmed_swing_low"]

        if pd.notna(ph) and pd.notna(pwl) and float(ph) > float(pwl):
            last_high = (float(ph), i - 2)

        if pd.notna(pl) and pd.notna(pwh) and float(pl) < float(pwh):
            last_low = (float(pl), i - 2)

        # BUY: record the lowest low of the active PWL sweep structure.
        if bool(row["buy_sweep"]):
            if not buy_active:
                buy_active = True
                buy_extreme = candle_low
                buy_extreme_i = i
                buy_level = (
                    last_high[0]
                    if last_high is not None and last_high[1] < i
                    else None
                )
            elif candle_low < buy_extreme:
                buy_extreme = candle_low
                buy_extreme_i = i
                buy_level = None

        # SELL: record the highest high of the active PWH sweep structure.
        if bool(row["sell_sweep"]):
            if not sell_active:
                sell_active = True
                sell_extreme = candle_high
                sell_extreme_i = i
                sell_level = (
                    last_low[0]
                    if last_low is not None and last_low[1] < i
                    else None
                )
            elif candle_high > sell_extreme:
                sell_extreme = candle_high
                sell_extreme_i = i
                sell_level = None

        # A new qualifying swing formed after the latest extreme can replace
        # a discarded level. The swing must be on the correct side of PWL/PWH.
        if (
            buy_active and last_high is not None
            and last_high[1] > buy_extreme_i
        ):
            buy_level = last_high[0]

        if (
            sell_active and last_low is not None
            and last_low[1] > sell_extreme_i
        ):
            sell_level = last_low[0]

        # BUY price is the actual trigger candle close.
        if (
            buy_active and buy_level is not None
            and pd.notna(pwl) and pd.notna(pwh)
            and close > float(pwl) and close > buy_level
        ):
            risk = close - buy_extreme
            reward = float(pwh) - close

            if risk > 0 and reward >= risk:
                results.append({
                    "datetime_ist": row["datetime_ist"],
                    "symbol": str(symbol),
                    "signal": "BUY",
                    "close": close,
                    "pwl": float(pwl),
                    "pwh": float(pwh),
                    "swing_level": float(buy_level),
                    "sweep_extreme": float(buy_extreme),
                    "risk": float(risk),
                    "reward": float(reward),
                    "rr_ratio": float(reward / risk),
                })
                buy_active = False
                buy_extreme = buy_extreme_i = buy_level = None

        # SELL price is the actual trigger candle close.
        if (
            sell_active and sell_level is not None
            and pd.notna(pwl) and pd.notna(pwh)
            and close < float(pwh) and close < sell_level
        ):
            risk = sell_extreme - close
            reward = close - float(pwl)

            if risk > 0 and reward >= risk:
                results.append({
                    "datetime_ist": row["datetime_ist"],
                    "symbol": str(symbol),
                    "signal": "SELL",
                    "close": close,
                    "pwl": float(pwl),
                    "pwh": float(pwh),
                    "swing_level": float(sell_level),
                    "sweep_extreme": float(sell_extreme),
                    "risk": float(risk),
                    "reward": float(reward),
                    "rr_ratio": float(reward / risk),
                })
                sell_active = False
                sell_extreme = sell_extreme_i = sell_level = None

    if not results:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    result = pd.DataFrame(results, columns=OUTPUT_COLUMNS)

    if ((result["signal"] == "BUY") & (result["close"] <= result["pwl"])).any():
        raise RuntimeError("BUY validation failed: close must be above PWL")

    if ((result["signal"] == "SELL") & (result["close"] >= result["pwh"])).any():
        raise RuntimeError("SELL validation failed: close must be below PWH")

    if (result["rr_ratio"] < 1.0).any():
        raise RuntimeError("Risk/reward validation failed: minimum ratio is 1:1")

    return result.sort_values(
        "datetime_ist", ascending=False
    ).reset_index(drop=True)
