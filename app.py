import streamlit as st
import pandas as pd

st.set_page_config(
    page_title="Weekly Liquidity Scanner",
    page_icon="📊",
    layout="wide"
)

st.title("📊 Weekly Liquidity Scanner")
st.caption("Weekly Liquidity Sweep | 15-minute signals")

uploaded_file = st.file_uploader(
    "স্ক্যানারের CSV ফাইল আপলোড করুন",
    type=["csv"]
)

if uploaded_file is None:
    st.info(
        "প্রথমে Colab থেকে ডাউনলোড করা "
        "weekly_scanner_signals.csv ফাইলটি আপলোড করুন।"
    )
    st.stop()

try:
    df = pd.read_csv(uploaded_file)
except Exception as e:
    st.error(f"ফাইল পড়া যায়নি: {e}")
    st.stop()

df.columns = df.columns.str.strip()

required = ["time", "symbol", "signal"]
missing = [c for c in required if c not in df.columns]

if missing:
    st.error(f"CSV-তে এই কলামগুলি নেই: {missing}")
    st.stop()

df["time"] = pd.to_datetime(df["time"], errors="coerce")
df["signal"] = df["signal"].astype(str).str.upper().str.strip()
df["symbol"] = df["symbol"].astype(str).str.strip()

if "PWL" not in df.columns:
    df["PWL"] = None
if "PWH" not in df.columns:
    df["PWH"] = None
if "swing level" not in df.columns:
    df["swing level"] = None
if "close" not in df.columns:
    df["close"] = None

df = df.dropna(subset=["time"]).copy()
df = df.sort_values("time", ascending=False)

buy_count = (df["signal"] == "BUY").sum()
sell_count = (df["signal"] == "SELL").sum()

c1, c2, c3 = st.columns(3)
c1.metric("Total Signals", len(df))
c2.metric("BUY", int(buy_count))
c3.metric("SELL", int(sell_count))

st.divider()

signal_choice = st.selectbox(
    "Signal Filter",
    ["ALL", "BUY", "SELL"]
)

search_symbol = st.text_input(
    "স্টকের নাম খুঁজুন",
    placeholder="যেমন SAIL"
).strip().upper()

filtered = df.copy()

if signal_choice != "ALL":
    filtered = filtered[filtered["signal"] == signal_choice]

if search_symbol:
    filtered = filtered[
        filtered["symbol"].str.upper().str.contains(
            search_symbol, na=False
        )
    ]

display_columns = [
    "time", "symbol", "signal",
    "PWL", "PWH", "swing level", "close"
]

st.subheader("স্ক্যানারের সিগন্যাল")
st.caption(f"দেখানো হচ্ছে {len(filtered)}টি সিগন্যাল")

st.dataframe(
    filtered[display_columns],
    use_container_width=True,
    hide_index=True
)

st.download_button(
    "Filtered Results CSV ডাউনলোড",
    data=filtered.to_csv(index=False).encode("utf-8"),
    file_name="filtered_scanner_signals.csv",
    mime="text/csv",
    use_container_width=True
)

st.warning(
    "এই স্ক্রিনে আপলোড করা CSV-র ফলাফল দেখা যায়। "
    "নতুন লাইভ সিগন্যাল পেতে মার্কেট ডেটার সংযোগ আলাদাভাবে করতে হবে।"
)
