"""
NIFTY OI Monitor - Streamlit Dashboard
Shows live OI data fetched directly from NSE with baseline comparison.
"""
import os
import sqlite3
import math
from datetime import datetime, time as dtime, timezone, timedelta

import requests
import streamlit as st

# ─── Config ────────────────────────────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))
SYMBOL = os.getenv("SYMBOL", "NIFTY")
STRIKE_RANGE = int(os.getenv("STRIKE_RANGE", "6"))
LOT_SIZE = int(os.getenv("LOT_SIZE", "65"))
DB_FILE = os.getenv("DB_FILE", "oi_history.db")

OI_CHANGE_THRESHOLD_PERCENT = float(os.getenv("OI_CHANGE_THRESHOLD_PERCENT", "400.0"))
OI_RATIO_THRESHOLD = float(os.getenv("OI_RATIO_THRESHOLD", "2.0"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/option-chain",
    "X-Requested-With": "XMLHttpRequest",
    "Connection": "keep-alive",
}

WEEKLY_EXPIRIES = [
    "10-Mar-2026", "17-Mar-2026", "24-Mar-2026", "30-Mar-2026",
    "07-Apr-2026", "13-Apr-2026", "21-Apr-2026", "28-Apr-2026",
    "05-May-2026", "12-May-2026", "19-May-2026", "26-May-2026",
    "02-Jun-2026", "09-Jun-2026", "16-Jun-2026", "23-Jun-2026",
    "30-Jun-2026", "07-Jul-2026", "14-Jul-2026", "21-Jul-2026",
    "28-Jul-2026", "04-Aug-2026", "11-Aug-2026", "18-Aug-2026",
    "25-Aug-2026", "01-Sep-2026", "08-Sep-2026", "15-Sep-2026",
    "22-Sep-2026", "29-Sep-2026", "06-Oct-2026", "13-Oct-2026",
    "19-Oct-2026", "27-Oct-2026", "03-Nov-2026", "09-Nov-2026",
    "17-Nov-2026", "23-Nov-2026", "01-Dec-2026", "08-Dec-2026",
    "15-Dec-2026", "22-Dec-2026", "29-Dec-2026",
]

# ─── Helpers ───────────────────────────────────────────────────────────────────

def get_expiry(now_ist: datetime) -> str:
    today = now_ist.date()
    for exp_str in WEEKLY_EXPIRIES:
        try:
            if datetime.strptime(exp_str, "%d-%b-%Y").date() >= today:
                return exp_str
        except Exception:
            continue
    return WEEKLY_EXPIRIES[-1]


@st.cache_data(ttl=60, show_spinner=False)
def fetch_nse_data(expiry_str: str) -> dict | None:
    try:
        s = requests.Session()
        s.headers.update(HEADERS)
        s.get("https://www.nseindia.com", timeout=5)
        url = f"https://www.nseindia.com/api/option-chain-v3?type=Indices&symbol={SYMBOL}&expiry={expiry_str}"
        resp = s.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        st.warning(f"NSE fetch error: {e}")
        return None


def build_strike_map(data: dict) -> dict:
    records = data.get("records", {})
    strikes = {}
    for item in records.get("data", []):
        strike = item.get("strikePrice")
        if strike is None:
            continue
        ce = item.get("CE")
        pe = item.get("PE")
        ce_oi = ce.get("openInterest") if ce else None
        pe_oi = pe.get("openInterest") if pe else None
        if ce_oi is not None or pe_oi is not None:
            strikes.setdefault(strike, {})
            if ce_oi is not None:
                strikes[strike]["CE"] = ce_oi
            if pe_oi is not None:
                strikes[strike]["PE"] = pe_oi
    return strikes


def get_spot(data: dict):
    return data.get("records", {}).get("underlyingValue")


def get_step(data: dict):
    prices = sorted(set(
        item["strikePrice"]
        for item in data.get("records", {}).get("data", [])
        if "strikePrice" in item
    ))
    diffs = [b - a for a, b in zip(prices[:-1], prices[1:])]
    return min(diffs) if diffs else None


def load_baseline(trading_date: str, expiry: str) -> tuple[dict, str | None]:
    if not os.path.exists(DB_FILE):
        return {}, None
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            "SELECT strike, option_type, base_oi, baseline_time FROM baseline_oi "
            "WHERE trading_date = ? AND expiry = ?",
            (trading_date, expiry),
        )
        rows = c.fetchall()
        conn.close()
    except Exception:
        return {}, None

    baseline = {}
    btime = None
    for strike, opt, oi, bt in rows:
        baseline.setdefault(strike, {})[opt] = oi
        btime = bt
    return baseline, btime


def fmt_pct(v) -> str:
    if v is None:
        return "N/A"
    if v == math.inf:
        return "INF"
    return f"{v:.2f}%"


def oi_lakhs(oi: int) -> str:
    return f"{(oi * LOT_SIZE / 100_000):.2f}L"


# ─── Page Setup ────────────────────────────────────────────────────────────────

st.set_page_config(page_title="NIFTY OI Monitor", layout="wide", page_icon="📊")
st.title("📊 NIFTY OI Monitor")

now_ist = datetime.now(IST)
trading_date = now_ist.date().isoformat()
expiry_str = get_expiry(now_ist)

col1, col2, col3 = st.columns(3)
col1.metric("IST Time", now_ist.strftime("%H:%M:%S"))
col2.metric("Trading Date", trading_date)
col3.metric("Expiry", expiry_str)

st.divider()

# ─── Fetch Data ────────────────────────────────────────────────────────────────
with st.spinner("Fetching live data from NSE..."):
    data = fetch_nse_data(expiry_str)

if data is None:
    st.error("Could not fetch option chain data from NSE. Try refreshing.")
    st.stop()

spot = get_spot(data)
step = get_step(data)
current_strikes = build_strike_map(data)
all_strikes = sorted(current_strikes.keys())

if not spot or not step or not all_strikes:
    st.error("Incomplete data from NSE.")
    st.stop()

atm = min(all_strikes, key=lambda x: abs(x - spot))
baseline, btime = load_baseline(trading_date, expiry_str)

col1, col2, col3 = st.columns(3)
col1.metric("NIFTY Spot", f"{spot:,.2f}")
col2.metric("ATM Strike", f"{atm:,}")
col3.metric("Baseline Captured At", btime or "Not yet captured")

st.divider()

# ─── OI Table ──────────────────────────────────────────────────────────────────
monitored = [atm + i * step for i in range(-STRIKE_RANGE, STRIKE_RANGE + 1)]

rows = []
for strike in monitored:
    curr = current_strikes.get(strike, {})
    base = baseline.get(strike, {})

    ce_curr = curr.get("CE", 0)
    pe_curr = curr.get("PE", 0)
    ce_base = base.get("CE", 0)
    pe_base = base.get("PE", 0)

    def pct_change(b, c):
        if b == 0:
            return math.inf if c > 0 else 0.0
        return abs(c - b) / b * 100.0

    ce_pct = pct_change(ce_base, ce_curr)
    pe_pct = pct_change(pe_base, pe_curr)

    ratio = None
    if ce_curr > 0 and pe_curr > 0:
        larger, smaller = max(ce_curr, pe_curr), min(ce_curr, pe_curr)
        ratio = larger / smaller

    ce_alert = ce_pct >= OI_CHANGE_THRESHOLD_PERCENT
    pe_alert = pe_pct >= OI_CHANGE_THRESHOLD_PERCENT
    ratio_alert = ratio is not None and ratio >= OI_RATIO_THRESHOLD

    rows.append({
        "Strike": strike,
        "ATM": "<<< ATM" if strike == atm else "",
        "CE OI": f"{ce_curr:,}",
        "CE Base OI": f"{ce_base:,}",
        "CE Change": fmt_pct(ce_pct),
        "PE OI": f"{pe_curr:,}",
        "PE Base OI": f"{pe_base:,}",
        "PE Change": fmt_pct(pe_pct),
        "CE/PE Ratio": f"{ratio:.2f}x" if ratio else "N/A",
        "_ce_alert": ce_alert,
        "_pe_alert": pe_alert,
        "_ratio_alert": ratio_alert,
    })

st.subheader(f"OI Table: ATM ± {STRIKE_RANGE} Strikes")

if not baseline:
    st.info("Baseline not yet captured (captured after 9:17 AM IST). Changes shown as current OI only.")

# Display with color coding
for row in rows:
    is_alert = (row["_ce_alert"] or row["_pe_alert"]) and row["_ratio_alert"]
    is_atm = row["Strike"] == atm

    bg = ""
    if is_alert:
        bg = "background-color: #ffcccc;"
    elif is_atm:
        bg = "background-color: #fffacc;"

    with st.container():
        cols = st.columns([1, 0.6, 1, 1, 1, 1, 1, 1, 1])
        label = f"**{row['Strike']}** {row['ATM']}"
        cols[0].markdown(label)
        cols[1].write("")
        cols[2].write(row["CE OI"])
        cols[3].write(row["CE Base OI"])

        ce_color = "red" if row["_ce_alert"] else "green"
        cols[4].markdown(f":{ce_color}[{row['CE Change']}]")

        cols[5].write(row["PE OI"])
        cols[6].write(row["PE Base OI"])

        pe_color = "red" if row["_pe_alert"] else "green"
        cols[7].markdown(f":{pe_color}[{row['PE Change']}]")

        ratio_color = "red" if row["_ratio_alert"] else "normal"
        cols[8].markdown(f":{ratio_color}[{row['CE/PE Ratio']}]" if row["_ratio_alert"] else row["CE/PE Ratio"])

# Column headers
st.caption("Columns: Strike | | CE OI | CE Base | CE Δ% | PE OI | PE Base | PE Δ% | Ratio")

st.divider()

# ─── Legend & Thresholds ───────────────────────────────────────────────────────
st.subheader("Thresholds")
col1, col2 = st.columns(2)
col1.metric("OI Change Alert Threshold", f"{OI_CHANGE_THRESHOLD_PERCENT:.0f}%")
col2.metric("CE/PE Ratio Alert Threshold", f"{OI_RATIO_THRESHOLD:.1f}x")

st.caption("Alert fires when: |OI change vs baseline| ≥ threshold AND CE/PE ratio ≥ threshold (both must be true)")

# ─── Auto Refresh ──────────────────────────────────────────────────────────────
st.divider()
col1, col2 = st.columns([3, 1])
col1.caption(f"Last refreshed: {now_ist.strftime('%Y-%m-%d %H:%M:%S IST')} | Data cached for 60s")
if col2.button("Refresh Now"):
    st.cache_data.clear()
    st.rerun()

# Auto-refresh every 60s during market hours
market_open = dtime(9, 15)
market_close = dtime(15, 30)
if now_ist.weekday() < 5 and market_open <= now_ist.time() <= market_close:
    import time
    time.sleep(60)
    st.rerun()
