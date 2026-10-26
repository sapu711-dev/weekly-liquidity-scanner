"""WLS-213 strategy copied from the user's Step 4D notebook logic."""
import pandas as pd

def scan_candles(candles, symbol):
    """Return confirmed BUY/SELL rows using weekly sweep -> later reclaim -> later swing break.
    candles may be Kotak rows [timestamp, open, high, low, close, volume, ...] or a DataFrame.
    """
    if isinstance(candles, pd.DataFrame):
        frame = candles.copy()
    else:
        if not candles:
            return pd.DataFrame()
        if isinstance(candles[0], dict):
            frame = pd.DataFrame(candles).rename(columns={"timestamp":"datetime","time":"datetime"})
        else:
            frame = pd.DataFrame(candles)
            if len(frame.columns) < 6:
                return pd.DataFrame()
            frame = frame.iloc[:, :6]
            frame.columns = ["datetime","open","high","low","close","volume"]
    if "datetime" not in frame.columns or not {"open","high","low","close"}.issubset(frame.columns):
        raise ValueError("Candle data must include datetime/open/high/low/close")
    frame["symbol"] = str(symbol)
    frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce", utc=True)
    for col in ["open","high","low","close"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["datetime","open","high","low","close"]).sort_values("datetime").reset_index(drop=True)
    if frame.empty:
        return pd.DataFrame()
    frame["datetime_ist"] = frame["datetime"].dt.tz_convert("Asia/Kolkata")
    local_day = frame["datetime_ist"].dt.normalize()
    frame["week_start"] = local_day - pd.to_timedelta(frame["datetime_ist"].dt.weekday, unit="D")
    weekly = frame.groupby("week_start", as_index=False).agg(weekly_low=("low","min"), weekly_high=("high","max")).sort_values("week_start")
    weekly["pwl"] = weekly["weekly_low"].shift(1)
    weekly["pwh"] = weekly["weekly_high"].shift(1)
    frame = frame.merge(weekly[["week_start","pwl","pwh"]], on="week_start", how="left")
    frame["buy_sweep"] = frame["pwl"].notna() & (frame["low"] < frame["pwl"])
    frame["sell_sweep"] = frame["pwh"].notna() & (frame["high"] > frame["pwh"])

    h, l = frame["high"], frame["low"]
    pivot_high = (h > h.shift(1)) & (h > h.shift(2)) & (h >= h.shift(-1)) & (h >= h.shift(-2))
    pivot_low = (l < l.shift(1)) & (l < l.shift(2)) & (l <= l.shift(-1)) & (l <= l.shift(-2))
    frame["pivot_high_confirmed"] = h.where(pivot_high).shift(2)
    frame["pivot_low_confirmed"] = l.where(pivot_low).shift(2)
    frame["last_swing_high"] = frame["pivot_high_confirmed"].ffill()
    frame["last_swing_low"] = frame["pivot_low_confirmed"].ffill()

    buy_phase = sell_phase = 0
    buy_level = sell_level = buy_week = sell_week = None
    buy_signals, sell_signals, buy_levels, sell_levels = [], [], [], []
    for _, row in frame.iterrows():
        week, close, pwl, pwh = row["week_start"], row["close"], row["pwl"], row["pwh"]
        if buy_week is not None and week != buy_week:
            buy_phase, buy_level, buy_week = 0, None, None
        if sell_week is not None and week != sell_week:
            sell_phase, sell_level, sell_week = 0, None, None
        buy_level_bar, sell_level_bar = buy_level, sell_level
        bsignal = ssignal = False
        if buy_phase == 1 and pd.notna(pwl) and close > pwl:
            buy_phase = 2
        elif buy_phase == 2 and buy_level is not None and pd.notna(pwl) and close > pwl and close > buy_level:
            bsignal = True
            buy_phase, buy_level, buy_week = 0, None, None
        if sell_phase == 1 and pd.notna(pwh) and close < pwh:
            sell_phase = 2
        elif sell_phase == 2 and sell_level is not None and pd.notna(pwh) and close < pwh and close < sell_level:
            ssignal = True
            sell_phase, sell_level, sell_week = 0, None, None
        if bool(row["buy_sweep"]) and pd.notna(row["last_swing_high"]):
            buy_phase, buy_level, buy_week = 1, float(row["last_swing_high"]), week
        if bool(row["sell_sweep"]) and pd.notna(row["last_swing_low"]):
            sell_phase, sell_level, sell_week = 1, float(row["last_swing_low"]), week
        buy_signals.append(bsignal); sell_signals.append(ssignal)
        buy_levels.append(buy_level_bar if buy_level_bar is not None else buy_level)
        sell_levels.append(sell_level_bar if sell_level_bar is not None else sell_level)
    frame["buy_signal"], frame["sell_signal"] = buy_signals, sell_signals
    frame["locked_buy_swing_high"], frame["locked_sell_swing_low"] = buy_levels, sell_levels
    out = []
    for mask, label, levelcol in [(frame["buy_signal"],"BUY","locked_buy_swing_high"),(frame["sell_signal"],"SELL","locked_sell_swing_low")]:
        part = frame.loc[mask, ["datetime_ist","symbol","close","pwl","pwh",levelcol]].copy()
        if not part.empty:
            part["signal"] = label
            part["swing_level"] = part[levelcol]
            out.append(part[["datetime_ist","symbol","signal","close","pwl","pwh","swing_level"]])
    if not out:
        return pd.DataFrame(columns=["datetime_ist","symbol","signal","close","pwl","pwh","swing_level"])
    result = pd.concat(out, ignore_index=True)
    if ((result.signal == "BUY") & (result.close <= result.pwl)).any():
        raise RuntimeError("BUY consistency guard failed")
    if ((result.signal == "SELL") & (result.close >= result.pwh)).any():
        raise RuntimeError("SELL consistency guard failed")
    return result.sort_values("datetime_ist", ascending=False).reset_index(drop=True)
