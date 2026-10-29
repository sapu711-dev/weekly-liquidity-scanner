
"""WLS-213 signal-only worker. Never places orders."""
import os
import time
import json
import logging
import requests
import pandas as pd

from pathlib import Path
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo
from wls213_strategy import scan_candles

TZ = ZoneInfo("Asia/Kolkata")
STATE = Path(os.getenv("STATE_FILE", "/tmp/wls213_status.json"))
LOG = logging.getLogger("wls213")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s"
)


def write_state(**kwargs):
    old = {}
    try:
        old = json.loads(STATE.read_text())
    except Exception:
        pass

    old.update(kwargs)
    old["updated_at"] = datetime.now(TZ).isoformat()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(
        json.dumps(old, ensure_ascii=False, default=str)
    )


def send_telegram(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat:
        return False

    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": message},
        timeout=15
    )
    response.raise_for_status()
    return True


def market_open(now):
    return (
        now.weekday() < 5
        and dtime(9, 15) <= now.time() <= dtime(15, 30)
    )


def response_ok(response):
    """Accept common successful Kotak API response formats."""
    if not isinstance(response, dict):
        return False

    if response.get("error"):
        return False

    candidates = [
        response.get("status"),
        response.get("stat"),
        response.get("data", {}).get("status")
        if isinstance(response.get("data"), dict)
        else None,
    ]

    for value in candidates:
        if str(value).strip().lower() in (
            "success", "successful", "ok"
        ):
            return True

    code = response.get("stCode", response.get("statusCode"))
    if str(code).strip() == "200":
        return True

    return False


def authenticate():
    from neo_api_client import NeoAPI
    import pyotp

    consumer = os.getenv("KOTAK_CONSUMER_KEY", "").strip()
    mobile = os.getenv("KOTAK_MOBILE_NUMBER", "").strip()
    ucc = os.getenv("KOTAK_UCC", "").strip()
    mpin = os.getenv("KOTAK_MPIN", "").strip()
    totp_secret = os.getenv("KOTAK_TOTP_SECRET", "").strip()

    if not all([consumer, mobile, ucc, mpin, totp_secret]):
        raise RuntimeError(
            "Missing private server environment variables: "
            "KOTAK_CONSUMER_KEY, KOTAK_MOBILE_NUMBER, KOTAK_UCC, "
            "KOTAK_MPIN, KOTAK_TOTP_SECRET."
        )

    if len(mobile) == 10 and mobile.isdigit():
        mobile = "+91" + mobile

    client = NeoAPI(consumer_key=consumer, environment="prod")

    login = client.totp_login(
        mobile_number=mobile,
        ucc=ucc,
        totp=pyotp.TOTP(totp_secret).now()
    )

    if not response_ok(login):
        raise RuntimeError(
            "Kotak TOTP login was not confirmed. Check server secrets "
            "and account access."
        )

    validated = client.totp_validate(mpin=mpin)

    if not response_ok(validated):
        raise RuntimeError(
            "Kotak MPIN validation was not confirmed."
        )

    return client


def load_symbols(client):
    """Map NSE stock-futures names to NSE cash EQ tokens."""
    from io import StringIO

    url = client.scrip_master(exchange_segment="nse_fo")
    if not isinstance(url, str) or not url.startswith("http"):
        raise RuntimeError("Kotak F&O scrip-master URL unavailable.")

    fo_response = requests.get(url, timeout=45)
    fo_response.raise_for_status()
    fo = pd.read_csv(StringIO(fo_response.text), skipinitialspace=True)
    fo.columns = fo.columns.astype(str).str.strip()

    needed = {"pExchSeg", "pInstType", "pSymbolName"}
    if not needed.issubset(fo.columns):
        raise RuntimeError("Unexpected F&O scrip-master columns.")

    for col in needed:
        fo[col] = fo[col].astype(str).str.strip()

    names = sorted(
        fo.loc[
            (fo["pExchSeg"].str.lower() == "nse_fo")
            & (fo["pInstType"].str.upper() == "FUTSTK"),
            "pSymbolName"
        ].str.upper().unique()
    )

    cash_url = url.replace("nse_fo.csv", "nse_cm.csv")
    if cash_url == url:
        raise RuntimeError("Could not derive NSE cash scrip-master URL.")

    cash_response = requests.get(cash_url, timeout=45)
    cash_response.raise_for_status()
    cm = pd.read_csv(
        StringIO(cash_response.text), skipinitialspace=True
    )
    cm.columns = cm.columns.astype(str).str.strip()

    required = ["pExchSeg", "pTrdSymbol", "pSymbol", "pSymbolName"]
    for col in required:
        if col not in cm.columns:
            raise RuntimeError("Unexpected cash scrip-master columns.")
        cm[col] = cm[col].astype(str).str.strip()

    eq = cm[
        (cm["pExchSeg"].str.lower() == "nse_cm")
        & cm["pTrdSymbol"].str.endswith("-EQ")
        & cm["pSymbol"].str.fullmatch(r"\d+", na=False)
    ]

    mapped = eq[
        eq["pSymbolName"].str.upper().isin(names)
    ][["pSymbolName", "pSymbol", "pTrdSymbol"]].drop_duplicates(
        "pSymbolName"
    )

    rows = [
        {
            "symbol": r.pSymbolName.upper(),
            "token": str(r.pSymbol),
            "trade_symbol": r.pTrdSymbol
        }
        for r in mapped.itertuples(index=False)
    ]

    if not rows:
        raise RuntimeError("No NSE cash EQ tokens mapped.")

    csv_lines = ["symbol,token,trade_symbol\n"]
    for item in rows:
        csv_lines.append(
            f"{item['symbol']},{item['token']},"
            f"{item['trade_symbol']}\n"
        )

    Path("cash_stock_map.csv").write_text(
        "".join(csv_lines), encoding="utf-8"
    )

    return rows


def candles_for(client, row):
    today = datetime.now(TZ).date()
    start = (today - timedelta(days=35)).isoformat()
    end = today.isoformat()

    response = client.historical_data(
        neosymbol=f"nse_cm|{row['token']}",
        interval="15min",
        from_date=start,
        to_date=end
    )

    if not response_ok(response):
        raise RuntimeError(
            f"Historical API response not confirmed for {row['symbol']}."
        )

    data = response.get("data", [])
    if isinstance(data, dict):
        candles = data.get("candles", [])
    elif isinstance(data, list):
        candles = data
    else:
        candles = []

    if not isinstance(candles, list):
        raise RuntimeError(
            f"Unexpected candle format for {row['symbol']}."
        )

    return candles


def scan_loop():
    client = authenticate()
    symbols = load_symbols(client)

    if len(symbols) < 150:
        raise RuntimeError(
            f"Only {len(symbols)} stock tokens mapped. "
            "Stopping to avoid claiming a complete scan."
        )

    LOG.info("Mapped %d NSE cash symbols.", len(symbols))
    sent = set()

    while True:
        now = datetime.now(TZ)

        if market_open(now):
            write_state(
                running=True,
                connected=True,
                status="Scanning live market",
                last_scan=None,
                signals=[],
                buy_count=0,
                sell_count=0,
                error_count=0,
                universe_count=len(symbols)
            )

            found = []
            errors = 0
            today = datetime.now(TZ).date()

            for i, row in enumerate(symbols, 1):
                try:
                    candles = candles_for(client, row)
                    result = scan_candles(candles, row["symbol"])

                    if not result.empty:
                        for record in result.to_dict("records"):
                            raw_time = record.get("datetime_ist")
                            parsed_time = pd.to_datetime(
                                raw_time, errors="coerce"
                            )

                            if pd.isna(parsed_time):
                                continue

                            if parsed_time.tzinfo is None:
                                parsed_time = parsed_time.tz_localize(TZ)
                            else:
                                parsed_time = parsed_time.tz_convert(TZ)

                            # Do not display signals from earlier dates.
                            if parsed_time.date() != today:
                                continue

                            signal = str(record.get("signal", "")).upper()
                            if signal not in ("BUY", "SELL"):
                                continue

                            def safe_float(value):
                                try:
                                    number = float(value)
                                    return number if pd.notna(number) else None
                                except (TypeError, ValueError):
                                    return None

                            item = {
                                "time": parsed_time.isoformat(),
                                "symbol": str(record["symbol"]),
                                "signal": signal,
                                "close": safe_float(record.get("close")),
                                "PWL": safe_float(record.get("pwl")),
                                "PWH": safe_float(record.get("pwh")),
                                "swing_level": safe_float(
                                    record.get("swing_level")
                                )
                            }

                            found.append(item)

                            key = (
                                f"{item['symbol']}|{item['signal']}|"
                                f"{item['time']}"
                            )

                            if key not in sent:
                                try:
                                    send_telegram(
                                        f"WLS-213 {item['signal']} "
                                        f"{item['symbol']}\n"
                                        f"Time: {item['time']}\n"
                                        f"Close: {item['close']}\n"
                                        f"PWL: {item['PWL']} | "
                                        f"PWH: {item['PWH']}\n"
                                        f"Swing: {item['swing_level']}"
                                    )
                                except Exception:
                                    LOG.exception("Telegram alert failed")

                                sent.add(key)

                except Exception as exc:
                    errors += 1
                    LOG.warning(
                        "Scan failed for %s: %s",
                        row["symbol"], exc
                    )

                if i % 20 == 0:
                    time.sleep(1.2)

            found.sort(key=lambda x: x["time"], reverse=True)
            buys = sum(x["signal"] == "BUY" for x in found)
            sells = sum(x["signal"] == "SELL" for x in found)

            status = (
                f"Scan completed with {errors} stock data errors"
                if errors
                else "Scan completed"
            )

            write_state(
                running=True,
                connected=True,
                status=status,
                last_scan=datetime.now(TZ).isoformat(),
                signals=found[:250],
                buy_count=buys,
                sell_count=sells,
                error_count=errors,
                universe_count=len(symbols)
            )

            LOG.info(
                "Scan finished: BUY=%d SELL=%d errors=%d",
                buys, sells, errors
            )

        else:
            write_state(
                running=False,
                connected=True,
                status="Waiting for NSE market hours",
                universe_count=len(symbols)
            )

        time.sleep(max(120, int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))))


if __name__ == "__main__":
    try:
        scan_loop()
    except Exception as exc:
        LOG.exception("Worker stopped")
        write_state(
            running=False,
            connected=False,
            status=f"ERROR: {exc}"
        )
        raise
