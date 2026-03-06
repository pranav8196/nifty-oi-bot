import os
import requests
import time
from datetime import datetime, time as dtime, timezone, timedelta
from math import inf
import sqlite3

# -------------------------------------------------------------------
# TIMEZONE (IST)
# -------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))

# -------------------------------------------------------------------
# Optional: Gemini LLM analysis
# -------------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai = None
if GEMINI_API_KEY:
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
    except ImportError:
        print("GEMINI_API_KEY set but google-generativeai package not installed.")
        genai = None

print("=== Starting NIFTY OI Monitor (Baseline vs 09:17 Snapshot) ===")

# ===========================
# CONFIGURATION
# ===========================

SYMBOL = os.getenv("SYMBOL", "NIFTY")

# % change vs BASELINE required to trigger alert
OI_CHANGE_THRESHOLD_PERCENT = float(os.getenv("OI_CHANGE_THRESHOLD_PERCENT", "400.0"))
# CE/PE OI ratio required to trigger alert (alongside % change)
OI_RATIO_THRESHOLD = float(os.getenv("OI_RATIO_THRESHOLD", "2.0"))
# ATM +/- N strikes to monitor
STRIKE_RANGE = int(os.getenv("STRIKE_RANGE", "6"))

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
# NSE changes lot size periodically — update via env var, not code
LOT_SIZE = int(os.getenv("LOT_SIZE", "65"))

DB_FILE = os.getenv("DB_FILE", "oi_history.db")

# ---------- Telegram ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ---------- NSE API ----------
NSE_BASE_URL = "https://www.nseindia.com/api/option-chain-v3"

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
    "Origin": "https://www.nseindia.com",
}

session = requests.Session()
session.headers.update(HEADERS)


# ===========================
# EXPIRY DATE DETECTION
# ===========================

# --- HARDCODED FALLBACK LIST (commented out) ---
# If dynamic expiry detection fails consistently, uncomment this list and
# replace `get_current_expiry()` call in main_loop with:
#   expiry_str = get_current_weekly_expiry_from_list(now_ist)
#
# NIFTY expires every Tuesday (Monday if Tuesday is a market holiday).
# Update this list when it runs out or NSE changes the schedule.
#
# WEEKLY_EXPIRIES = [
#     "10-Mar-2026", "17-Mar-2026", "24-Mar-2026", "30-Mar-2026",
#     "07-Apr-2026", "13-Apr-2026", "21-Apr-2026", "28-Apr-2026",
#     "05-May-2026", "12-May-2026", "19-May-2026", "26-May-2026",
#     "02-Jun-2026", "09-Jun-2026", "16-Jun-2026", "23-Jun-2026",
#     "30-Jun-2026", "07-Jul-2026", "14-Jul-2026", "21-Jul-2026",
#     "28-Jul-2026", "04-Aug-2026", "11-Aug-2026", "18-Aug-2026",
#     "25-Aug-2026", "01-Sep-2026", "08-Sep-2026", "15-Sep-2026",
#     "22-Sep-2026", "29-Sep-2026", "06-Oct-2026", "13-Oct-2026",
#     "19-Oct-2026", "27-Oct-2026", "03-Nov-2026", "09-Nov-2026",
#     "17-Nov-2026", "23-Nov-2026", "01-Dec-2026", "08-Dec-2026",
#     "15-Dec-2026", "22-Dec-2026", "29-Dec-2026",
# ]
#
# def get_current_weekly_expiry_from_list(now_ist: datetime) -> str | None:
#     today = now_ist.date()
#     for exp_str in WEEKLY_EXPIRIES:
#         try:
#             if datetime.strptime(exp_str, "%d-%b-%Y").date() >= today:
#                 return exp_str
#         except Exception:
#             continue
#     return WEEKLY_EXPIRIES[-1]

# Module-level cache: expiry is determined once per trading day per process
_cached_expiry: str | None = None
_cached_expiry_date: str | None = None


def fetch_expiry_dates_from_nse(now_ist: datetime) -> list[str]:
    """
    Call NSE option chain API (no expiry param) to get the list of all
    available expiry dates for SYMBOL. Returns list like ["10-Mar-2026", ...].
    """
    url = f"{NSE_BASE_URL}?type=Indices&symbol={SYMBOL}"
    try:
        session.get("https://www.nseindia.com", timeout=5)
        resp = session.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        expiry_dates = data.get("records", {}).get("expiryDates", [])
        print(f"[{now_ist}] NSE returned {len(expiry_dates)} expiry dates: {expiry_dates[:6]}")
        return expiry_dates
    except Exception as e:
        print(f"[{now_ist}] Could not fetch expiry dates from NSE: {e}")
        return []


def pick_next_expiry(expiry_dates: list[str], now_ist: datetime) -> str | None:
    """
    From the NSE expiry dates list, return the first expiry on or after today (IST).
    This is the active expiry: on expiry day itself it returns that day's expiry;
    the morning after expiry it naturally advances to the next one.
    """
    today = now_ist.date()
    parsed = []
    for s in expiry_dates:
        try:
            parsed.append((datetime.strptime(s, "%d-%b-%Y").date(), s))
        except Exception:
            continue
    parsed.sort()
    for exp_date, exp_str in parsed:
        if exp_date >= today:
            return exp_str
    return None


def get_current_expiry(now_ist: datetime) -> str | None:
    """
    Returns today's active expiry date string (e.g. "10-Mar-2026").
    Fetches from NSE API once per trading day; result is cached for the session.
    Returns None if NSE is unreachable — caller should skip the cycle and retry.
    """
    global _cached_expiry, _cached_expiry_date

    today = now_ist.date().isoformat()

    if _cached_expiry and _cached_expiry_date == today:
        return _cached_expiry

    print(f"[{now_ist}] Determining active expiry from NSE API...")
    expiry_dates = fetch_expiry_dates_from_nse(now_ist)

    if not expiry_dates:
        # If this keeps failing, uncomment the hardcoded list above and
        # switch the main_loop call to get_current_weekly_expiry_from_list().
        print(f"[{now_ist}] Could not determine expiry from NSE. Will retry next cycle.")
        return None

    chosen = pick_next_expiry(expiry_dates, now_ist)
    if chosen:
        _cached_expiry = chosen
        _cached_expiry_date = today
        print(f"[{now_ist}] Active expiry set to: {chosen} (cached for today)")

    return chosen


# ===========================
# MARKET HOURS CHECK (IST)
# ===========================

def is_market_hours_ist(now_ist: datetime | None = None) -> bool:
    """NSE trading hours: Monday-Friday, 09:15-15:30 IST."""
    if now_ist is None:
        now_ist = datetime.now(IST)
    if now_ist.weekday() >= 5:  # Saturday or Sunday
        return False
    return dtime(9, 15) <= now_ist.time() <= dtime(15, 30)


# ===========================
# DB FUNCTIONS (SQLite)
# ===========================

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DROP TABLE IF EXISTS oi_data")
    c.execute("""
    CREATE TABLE IF NOT EXISTS baseline_oi (
        trading_date TEXT,
        expiry TEXT,
        strike INTEGER,
        option_type TEXT,
        base_oi INTEGER,
        baseline_time TEXT,
        PRIMARY KEY (trading_date, expiry, strike, option_type)
    )
    """)
    conn.commit()
    conn.close()


def baseline_exists(trading_date: str, expiry: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT 1 FROM baseline_oi WHERE trading_date = ? AND expiry = ? LIMIT 1",
        (trading_date, expiry),
    )
    row = c.fetchone()
    conn.close()
    return row is not None


def get_baseline_time(trading_date: str, expiry: str) -> str | None:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT baseline_time FROM baseline_oi WHERE trading_date = ? AND expiry = ? LIMIT 1",
        (trading_date, expiry),
    )
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def store_baseline_snapshot(trading_date: str, expiry: str, baseline_time: datetime, strikes_dict: dict):
    """Store baseline OI for all strikes for (trading_date, expiry)."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "DELETE FROM baseline_oi WHERE trading_date = ? AND expiry = ?",
        (trading_date, expiry),
    )
    baseline_time_str = baseline_time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{baseline_time}] CAPTURING BASELINE for {trading_date} expiry={expiry} at {baseline_time_str} IST...")

    inserted_rows = 0
    for strike, sides in strikes_dict.items():
        for option_type in ("CE", "PE"):
            oi_value = sides.get(option_type)
            if oi_value is None:
                continue
            c.execute(
                "INSERT INTO baseline_oi (trading_date, expiry, strike, option_type, base_oi, baseline_time) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (trading_date, expiry, strike, option_type, int(oi_value), baseline_time_str),
            )
            inserted_rows += 1

    conn.commit()
    conn.close()
    print(
        f"[{baseline_time}] BASELINE STORED: {len(strikes_dict)} unique strikes, {inserted_rows} rows. "
        f"All comparisons today will use this baseline.\n"
    )


def load_baseline_snapshot(trading_date: str, expiry: str) -> dict:
    """Load baseline into dict: {strike: {'CE': oi, 'PE': oi}}"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT strike, option_type, base_oi FROM baseline_oi WHERE trading_date = ? AND expiry = ?",
        (trading_date, expiry),
    )
    rows = c.fetchall()
    conn.close()

    baseline: dict[int, dict[str, int]] = {}
    for strike, option_type, base_oi in rows:
        baseline.setdefault(strike, {})[option_type] = base_oi
    return baseline


# ===========================
# ALERT SENDING
# ===========================

def send_telegram(message: str):
    """Send a message via Telegram bot."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[{datetime.now(IST)}] Telegram not configured (missing TOKEN or CHAT_ID).")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        resp = requests.post(url, json=payload, timeout=5)
        print(f"[{datetime.now(IST)}] Telegram send status: {resp.status_code}")
    except Exception as e:
        print(f"[{datetime.now(IST)}] Error sending Telegram message: {e}")


def send_llm_analysis(alert_text: str):
    """
    Ask Gemini for a brief trading interpretation of the alert.
    Sends the analysis as a follow-up Telegram message.
    Silently skips if GEMINI_API_KEY is not set or the API call fails.
    """
    if not genai or not GEMINI_API_KEY:
        return

    system_prompt = (
        "You are a NIFTY options trading analyst. Given an Open Interest alert, "
        "provide a brief 3-4 line interpretation: what the OI movement likely signals "
        "(bullish/bearish pressure, support/resistance), and any key risk to watch. "
        "Be concise and actionable."
    )

    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        response = model.generate_content(f"{system_prompt}\n\nAlert:\n{alert_text}")
        send_telegram(f"*Gemini Analysis:*\n{response.text.strip()}")
        print(f"[{datetime.now(IST)}] Gemini analysis sent to Telegram.")
    except Exception as e:
        print(f"[{datetime.now(IST)}] Gemini analysis failed (skipping): {e}")


def notify_alert(alert_text: str):
    print(alert_text)
    send_telegram(alert_text)
    send_llm_analysis(alert_text)


# ===========================
# NSE DATA FUNCTIONS
# ===========================

def fetch_option_chain(now_ist: datetime, expiry_str: str) -> dict | None:
    """
    Fetch option chain for the given weekly expiry.
    Retries up to 3 times (5s between retries) on transient errors.
    """
    print(f"[{now_ist}] Fetching option chain from NSE for {SYMBOL}, expiry {expiry_str}...")
    url = f"{NSE_BASE_URL}?type=Indices&symbol={SYMBOL}&expiry={expiry_str}"

    for attempt in range(3):
        try:
            warmup = session.get("https://www.nseindia.com", timeout=5)
            print(f"[{now_ist}] Warmup status: {warmup.status_code}")

            resp = session.get(url, timeout=10)
            print(f"[{now_ist}] NSE response: {resp.status_code} (attempt {attempt + 1})")
            resp.raise_for_status()

            try:
                data = resp.json()
            except Exception:
                print(f"[{now_ist}] JSON decode failed. Response (first 500 chars): {resp.text[:500]}")
                return None

            if not isinstance(data, dict) or not data:
                print(f"[{now_ist}] NSE returned empty JSON for expiry {expiry_str}.")
                return None

            records = data.get("records", {})
            if isinstance(records, dict):
                print(f"[{now_ist}] records.data length: {len(records.get('data', []))}")

            return data

        except Exception as e:
            print(f"[{now_ist}] Error fetching option chain (attempt {attempt + 1}/3): {e}")
            if attempt < 2:
                print(f"[{now_ist}] Retrying in 5s...")
                time.sleep(5)

    return None


def build_strike_map(data: dict) -> dict:
    """Returns {strike: {'CE': ce_oi, 'PE': pe_oi}}"""
    strikes: dict[int, dict[str, int]] = {}
    for item in data.get("records", {}).get("data", []):
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


def get_spot_price_and_step(data: dict):
    records = data.get("records", {})
    underlying = records.get("underlyingValue")
    strike_prices = sorted(set(
        item["strikePrice"]
        for item in records.get("data", [])
        if "strikePrice" in item
    ))
    step = None
    if len(strike_prices) >= 2:
        diffs = [j - i for i, j in zip(strike_prices[:-1], strike_prices[1:])]
        step = min(diffs) if diffs else None
    return underlying, step


def find_atm_strike(spot_price, strike_prices):
    return min(strike_prices, key=lambda x: abs(x - spot_price))


def compute_change_vs_baseline(base_oi: int | None, curr_oi: int | None):
    """
    Returns (pct_change_abs, diff, direction).
    pct_change_abs is math.inf when base_oi == 0 (avoid division by zero).
    """
    if base_oi is None or curr_oi is None:
        return None, None, None
    if base_oi == 0:
        diff = curr_oi - base_oi
        direction = "UP" if diff > 0 else ("DOWN" if diff < 0 else "FLAT")
        return inf, diff, direction
    diff = curr_oi - base_oi
    direction = "UP" if diff > 0 else ("DOWN" if diff < 0 else "FLAT")
    return (abs(diff) / base_oi) * 100.0, diff, direction


def fmt_pct(v) -> str:
    """Format percentage safely — returns 'INF' for math.inf, 'N/A' for None."""
    if v is None:
        return "N/A"
    if v == inf:
        return "INF"
    return f"{v:.2f}%"


# ===========================
# MAIN ALERT LOGIC
# ===========================

def check_alerts(
    spot_price,
    current_strikes: dict,
    baseline_strikes: dict,
    atm_strike,
    step,
    now_ist: datetime,
    trading_date: str,
    expiry_str: str,
):
    def oi_to_lakhs(oi):
        lots = oi * LOT_SIZE
        return lots, f"{lots / 100_000:.2f} lakh"

    if step is None:
        print(f"[{now_ist}] Cannot determine strike step; aborting this cycle.")
        return

    monitored_strikes = [atm_strike + i * step for i in range(-STRIKE_RANGE, STRIKE_RANGE + 1)]

    print(f"[{now_ist}] Monitored strikes: {monitored_strikes}")
    print(
        f"[{now_ist}] Thresholds: OI_CHANGE>={OI_CHANGE_THRESHOLD_PERCENT}%, "
        f"CE/PE ratio>={OI_RATIO_THRESHOLD}x"
    )
    print(f"[{now_ist}] Comparing vs BASELINE for {trading_date}, expiry {expiry_str}.")

    for strike in monitored_strikes:
        curr = current_strikes.get(strike)
        base = baseline_strikes.get(strike)

        if curr is None or base is None:
            print(f"[{now_ist}] Strike {strike}: missing current or baseline data, skipping.")
            continue

        ce_curr = curr.get("CE", 0)
        pe_curr = curr.get("PE", 0)
        ce_base = base.get("CE", 0)
        pe_base = base.get("PE", 0)

        if ce_curr == 0 and pe_curr == 0:
            continue

        ce_change_pct, ce_diff, ce_dir = compute_change_vs_baseline(ce_base, ce_curr)
        pe_change_pct, pe_diff, pe_dir = compute_change_vs_baseline(pe_base, pe_curr)

        ce_trigger = ce_change_pct is not None and ce_change_pct >= OI_CHANGE_THRESHOLD_PERCENT
        pe_trigger = pe_change_pct is not None and pe_change_pct >= OI_CHANGE_THRESHOLD_PERCENT

        ratio = None
        ratio_ok = False
        if ce_curr > 0 and pe_curr > 0:
            ratio = max(ce_curr, pe_curr) / min(ce_curr, pe_curr)
            ratio_ok = ratio >= OI_RATIO_THRESHOLD

        print(
            f"[{now_ist}] Strike {strike}: "
            f"CE {ce_base}->{ce_curr} ({fmt_pct(ce_change_pct)}, {ce_dir}) trigger={ce_trigger} | "
            f"PE {pe_base}->{pe_curr} ({fmt_pct(pe_change_pct)}, {pe_dir}) trigger={pe_trigger} | "
            f"ratio={f'{ratio:.2f}x' if ratio else 'N/A'} ok={ratio_ok}"
        )

        if (ce_trigger or pe_trigger) and ratio_ok:
            print(f"[{now_ist}] ALERT CONDITIONS MET for strike {strike}!")

            trigger_side = "CE" if ce_trigger else "PE"

            ce_lots, ce_lakhs = oi_to_lakhs(ce_curr)
            pe_lots, pe_lakhs = oi_to_lakhs(pe_curr)
            ce_base_lots, ce_base_lakhs = oi_to_lakhs(ce_base)
            pe_base_lots, pe_base_lakhs = oi_to_lakhs(pe_base)
            diff_lots = (ce_curr - pe_curr) * LOT_SIZE

            alert_lines = [
                "=" * 55,
                f"TIME         : {now_ist.strftime('%Y-%m-%d %H:%M:%S')} IST",
                f"DATE/EXPIRY  : {trading_date} | {expiry_str}",
                f"SPOT         : {spot_price}  |  ATM: {atm_strike}  |  STRIKE: {strike}",
                f"LOT SIZE     : {LOT_SIZE}",
                "",
                f"ALERT SIDE   : {trigger_side}",
                "",
                f"CE BASE OI   : {ce_base:,}  ({ce_base_lakhs})",
                f"CE CURR OI   : {ce_curr:,}  ({ce_lakhs})",
                f"CE CHANGE    : {ce_diff:,} ({ce_dir})  |  {fmt_pct(ce_change_pct)}",
                "",
                f"PE BASE OI   : {pe_base:,}  ({pe_base_lakhs})",
                f"PE CURR OI   : {pe_curr:,}  ({pe_lakhs})",
                f"PE CHANGE    : {pe_diff:,} ({pe_dir})  |  {fmt_pct(pe_change_pct)}",
                "",
                f"CE-PE DIFF   : {abs(ce_curr - pe_curr):,} contracts  ({abs(diff_lots / 100_000):.2f} lakh)",
                f"CE/PE RATIO  : {'CE' if ce_curr >= pe_curr else 'PE'} is {ratio:.2f}x the other",
                "",
                f"THRESHOLDS   : OI change >={OI_CHANGE_THRESHOLD_PERCENT}%  AND  ratio >={OI_RATIO_THRESHOLD}x",
                "=" * 55,
            ]

            notify_alert("\n".join(alert_lines))


# ===========================
# BASELINE LOGIC
# ===========================

def ensure_baseline_for_today(
    now_ist: datetime,
    expiry_str: str,
    strikes_dict: dict,
) -> tuple[bool, str]:
    """
    Ensure a baseline snapshot exists for (today, expiry_str).

    Timing rules:
    - Before 09:15 IST: script is outside market hours (never reaches here)
    - 09:15–09:17 IST: data is live but OI is still settling — wait
    - 09:17 IST onwards: capture baseline on the first successful fetch
      (even if that's 10:37 AM because the script started late — still valid)

    Returns (baseline_ready, trading_date).
    """
    trading_date = now_ist.date().isoformat()

    if baseline_exists(trading_date, expiry_str):
        btime = get_baseline_time(trading_date, expiry_str)
        print(
            f"[{now_ist}] Baseline exists for {trading_date}, expiry {expiry_str}"
            + (f" (captured at {btime} IST)" if btime else "")
        )
        return True, trading_date

    t = now_ist.time()
    if t < dtime(9, 17):
        print(f"[{now_ist}] Waiting for 09:17 IST to capture baseline (OI settling period)...")
        return False, trading_date

    late = t > dtime(9, 22)
    if late:
        print(f"[{now_ist}] Capturing baseline LATE (after 09:22 IST). Still valid as today's reference.")

    store_baseline_snapshot(trading_date, expiry_str, now_ist, strikes_dict)

    # Notify via Telegram that baseline is captured and monitoring begins
    capture_time_str = now_ist.strftime("%H:%M:%S")
    late_note = " (late capture — script started after 09:22)" if late else ""
    send_telegram(
        f"*Baseline Captured{' — LATE' if late else ''}*\n"
        f"Date     : {trading_date}\n"
        f"Expiry   : {expiry_str}\n"
        f"Captured : {capture_time_str} IST{late_note}\n"
        f"Strikes  : ATM +/- {STRIKE_RANGE}\n"
        f"Alert when: OI change >={OI_CHANGE_THRESHOLD_PERCENT:.0f}% AND ratio >={OI_RATIO_THRESHOLD}x\n"
        f"Monitoring live from now."
    )

    return True, trading_date


# ===========================
# MAIN LOOP
# ===========================

def main_loop():
    print(f"Starting {SYMBOL} OI monitor | ATM +/- {STRIKE_RANGE} strikes | Poll: {POLL_INTERVAL_SECONDS}s")
    print(f"Thresholds: OI change >={OI_CHANGE_THRESHOLD_PERCENT}% AND CE/PE ratio >={OI_RATIO_THRESHOLD}x")
    init_db()

    # Startup notification — lets you know the bot session is live
    now_ist = datetime.now(IST)
    send_telegram(
        f"*NIFTY OI Monitor Started*\n"
        f"Time     : {now_ist.strftime('%Y-%m-%d %H:%M:%S')} IST\n"
        f"Symbol   : {SYMBOL} | Lot size: {LOT_SIZE}\n"
        f"Poll     : every {POLL_INTERVAL_SECONDS}s\n"
        f"Strikes  : ATM +/- {STRIKE_RANGE}\n"
        f"Alert when: OI change >={OI_CHANGE_THRESHOLD_PERCENT:.0f}% AND ratio >={OI_RATIO_THRESHOLD}x\n"
        f"Expiry   : determined from NSE on first market cycle\n"
        f"Baseline : will be captured at 09:17 IST (or first fetch after that)"
    )

    while True:
        now_ist = datetime.now(IST)

        if not is_market_hours_ist(now_ist):
            print(f"[{now_ist}] Outside market hours, sleeping {POLL_INTERVAL_SECONDS}s...")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        print(f"\n[{now_ist}] --- New cycle ---")

        # Determine active expiry (from NSE API, cached per day)
        # To switch to hardcoded fallback: uncomment WEEKLY_EXPIRIES above and replace next line with:
        #   expiry_str = get_current_weekly_expiry_from_list(now_ist)
        expiry_str = get_current_expiry(now_ist)
        if expiry_str is None:
            print(f"[{now_ist}] Could not determine expiry. Sleeping and retrying...")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        data = fetch_option_chain(now_ist, expiry_str)
        if data is None:
            print(f"[{now_ist}] No data from NSE. Sleeping...")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        spot_price, step = get_spot_price_and_step(data)
        if spot_price is None or step is None:
            print(f"[{now_ist}] Could not determine spot price or strike step. Sleeping...")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        current_strikes = build_strike_map(data)
        all_strikes = sorted(current_strikes.keys())
        if not all_strikes:
            print(f"[{now_ist}] No strikes in option chain data. Sleeping...")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        atm_strike = find_atm_strike(spot_price, all_strikes)
        print(f"[{now_ist}] Spot: {spot_price} | ATM: {atm_strike} | Step: {step} | Expiry: {expiry_str}")

        baseline_ready, trading_date = ensure_baseline_for_today(now_ist, expiry_str, current_strikes)
        if not baseline_ready:
            print(f"[{now_ist}] Baseline not ready. Sleeping {POLL_INTERVAL_SECONDS}s...")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        baseline_strikes = load_baseline_snapshot(trading_date, expiry_str)
        if not baseline_strikes:
            print(f"[{now_ist}] Baseline empty for {trading_date}/{expiry_str}. Sleeping...")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        btime = get_baseline_time(trading_date, expiry_str)
        if btime:
            print(f"[{now_ist}] Using baseline captured at {btime} IST")

        check_alerts(
            spot_price=spot_price,
            current_strikes=current_strikes,
            baseline_strikes=baseline_strikes,
            atm_strike=atm_strike,
            step=step,
            now_ist=now_ist,
            trading_date=trading_date,
            expiry_str=expiry_str,
        )

        print(f"[{now_ist}] Cycle complete. Sleeping {POLL_INTERVAL_SECONDS}s...")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main_loop()
