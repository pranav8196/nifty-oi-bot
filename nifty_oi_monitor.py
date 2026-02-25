import os
import requests
import time
from datetime import datetime, time as dtime, timezone, timedelta
from math import inf
import sqlite3
import smtplib
from email.mime.text import MIMEText   # correct import

# -------------------------------------------------------------------
# TIMEZONE (IST)
# -------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))

# -------------------------------------------------------------------
# Optional: WhatsApp via Twilio
# -------------------------------------------------------------------
try:
    from twilio.rest import Client as TwilioClient
    TWILIO_AVAILABLE = True
except ImportError:
    TWILIO_AVAILABLE = False

print("=== Starting NIFTY OI Monitor ===")

# ===========================
# CONFIGURATION
# ===========================

SYMBOL = os.getenv("SYMBOL", "NIFTY")

OI_CHANGE_THRESHOLD_PERCENT = float(os.getenv("OI_CHANGE_THRESHOLD_PERCENT", "400.0"))  # 400%
OI_RATIO_THRESHOLD = float(os.getenv("OI_RATIO_THRESHOLD", "2.0"))                       # 2x CE/PE imbalance
STRIKE_RANGE = int(os.getenv("STRIKE_RANGE", "6"))                                       # ATM +/- 6 strikes
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))                    # 1 minute

DB_FILE = os.getenv("DB_FILE", "oi_history.db")

# ---------- Email Alert Config ----------
EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "True") == "True"
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))

EMAIL_FROM = os.getenv("EMAIL_FROM", "your_email@gmail.com")
EMAIL_TO = os.getenv("EMAIL_TO", "your_target_email@gmail.com")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "YOUR_GMAIL_APP_PASSWORD")

# ---------- WhatsApp Alert Config (Twilio, optional / not free) ----------
WHATSAPP_ENABLED = os.getenv("WHATSAPP_ENABLED", "False") == "True"
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "your_twilio_sid")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "your_twilio_auth_token")
TWILIO_FROM = os.getenv("TWILIO_FROM", "whatsapp:+14155238886")       # Twilio sandbox number
TWILIO_TO = os.getenv("TWILIO_TO", "whatsapp:+91XXXXXXXXXX")          # your WhatsApp number

# Base v3 URL (we will add &expiry=...)
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

# Global Twilio client (if enabled)
twilio_client = None
if WHATSAPP_ENABLED and TWILIO_AVAILABLE:
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ===========================
# HARDCODED WEEKLY EXPIRY DATES (NSE FORMAT)
# ===========================

# These are the dates you gave, converted to 'dd-MMM-YYYY'
WEEKLY_EXPIRIES = [
    "02-Mar-2026",
    "10-Mar-2026",
    "17-Mar-2026",
    "24-Mar-2026",
    "30-Mar-2026",
    "07-Apr-2026",
    "13-Apr-2026",
    "21-Apr-2026",
    "28-Apr-2026",
    "05-May-2026",
    "12-May-2026",
    "19-May-2026",
    "26-May-2026",
    "02-Jun-2026",
    "09-Jun-2026",
    "16-Jun-2026",
    "23-Jun-2026",
    "30-Jun-2026",
    "07-Jul-2026",
    "14-Jul-2026",
    "21-Jul-2026",
    "28-Jul-2026",
    "04-Aug-2026",
    "11-Aug-2026",
    "18-Aug-2026",
    "25-Aug-2026",
    "01-Sep-2026",
    "08-Sep-2026",
    "15-Sep-2026",
    "22-Sep-2026",
    "29-Sep-2026",
    "06-Oct-2026",
    "13-Oct-2026",
    "19-Oct-2026",
    "27-Oct-2026",
    "03-Nov-2026",
    "09-Nov-2026",
    "17-Nov-2026",
    "23-Nov-2026",
    "01-Dec-2026",
    "08-Dec-2026",
    "15-Dec-2026",
    "22-Dec-2026",
    "29-Dec-2026",
]


def get_current_weekly_expiry_from_list(now_ist: datetime) -> str:
    """
    Use the hardcoded WEEKLY_EXPIRIES list to choose the
    next expiry on or after today's date (IST).

    If today is before the first date -> pick the first.
    If today is after the last date  -> pick the last.
    """
    today = now_ist.date()

    chosen = None
    for exp_str in WEEKLY_EXPIRIES:
        try:
            exp_date = datetime.strptime(exp_str, "%d-%b-%Y").date()
        except Exception:
            continue

        if exp_date >= today:
            chosen = exp_str
            break

    if chosen is None:
        # After last known expiry, fallback to last
        chosen = WEEKLY_EXPIRIES[-1]

    print(f"[{now_ist}] Using weekly expiry from list: {chosen}")
    return chosen


# ===========================
# MARKET HOURS (IST) CHECK
# ===========================

def is_market_hours_ist() -> bool:
    """
    Return True only during NSE regular trading hours:
    Monday–Friday, 09:15–15:30 IST
    """
    now_ist = datetime.now(IST)
    weekday = now_ist.weekday()  # 0 = Monday, 6 = Sunday

    # Weekend check
    if weekday >= 5:  # 5 = Saturday, 6 = Sunday
        return False

    market_open = dtime(9, 15)
    market_close = dtime(15, 30)

    return market_open <= now_ist.time() <= market_close


# ===========================
# DB FUNCTIONS (SQLite)
# ===========================

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS oi_data (
        strike INTEGER,
        option_type TEXT,
        last_oi INTEGER,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (strike, option_type)
    )
    """)
    conn.commit()
    conn.close()


def get_previous_oi(strike, option_type):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
    SELECT last_oi FROM oi_data
    WHERE strike = ? AND option_type = ?
    """, (strike, option_type))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def set_previous_oi(strike, option_type, oi_value):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
    INSERT INTO oi_data (strike, option_type, last_oi)
    VALUES (?, ?, ?)
    ON CONFLICT(strike, option_type)
    DO UPDATE SET last_oi = excluded.last_oi,
                  last_updated = CURRENT_TIMESTAMP
    """, (strike, option_type, oi_value))
    conn.commit()
    conn.close()


# ===========================
# ALERT SENDING
# ===========================

def send_email(subject, message):
    if not EMAIL_ENABLED:
        return

    try:
        msg = MIMEText(message)
        msg["Subject"] = subject
        msg["From"] = EMAIL_FROM
        msg["To"] = EMAIL_TO

        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(EMAIL_FROM, EMAIL_PASSWORD)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())

        print(f"[{datetime.now(IST)}] Email alert sent.")
    except Exception as e:
        print(f"[{datetime.now(IST)}] Error sending email: {e}")


def send_whatsapp(message):
    if not WHATSAPP_ENABLED or not TWILIO_AVAILABLE or twilio_client is None:
        return

    try:
        twilio_client.messages.create(
            body=message,
            from_=TWILIO_FROM,
            to=TWILIO_TO
        )
        print(f"[{datetime.now(IST)}] WhatsApp alert sent.")
    except Exception as e:
        print(f"[{datetime.now(IST)}] Error sending WhatsApp: {e}")


def notify_alert(alert_text, email_subject):
    print(alert_text)          # Console
    send_email(email_subject, alert_text)
    send_whatsapp(alert_text)


# ===========================
# NSE FUNCTIONS (v3 with expiry param)
# ===========================

def fetch_option_chain(now_ist: datetime) -> dict | None:
    """
    Fetch option chain only for the weekly expiry chosen from WEEKLY_EXPIRIES.
    """
    expiry_str = get_current_weekly_expiry_from_list(now_ist)
    print(f"[{now_ist}] Fetching option chain from NSE for {SYMBOL}, expiry {expiry_str}...")

    url = f"{NSE_BASE_URL}?type=Indices&symbol={SYMBOL}&expiry={expiry_str}"

    try:
        # Warmup to set cookies (may give 403, that's okay)
        warmup = session.get("https://www.nseindia.com", timeout=5)
        print(f"[{now_ist}] Warmup status: {warmup.status_code}")

        resp = session.get(url, timeout=10)
        print(f"[{now_ist}] NSE response status: {resp.status_code}")
        resp.raise_for_status()

        try:
            data = resp.json()
        except Exception:
            print(f"[{now_ist}] JSON decode failed. Raw response (first 500 chars):")
            print(resp.text[:500])
            return None

        if not isinstance(data, dict) or len(data.keys()) == 0:
            print(f"[{now_ist}] NSE returned empty JSON for expiry {expiry_str}.")
            return None

        keys = list(data.keys())
        print(f"[{now_ist}] Top-level JSON keys: {keys}")
        records = data.get("records", {})
        if isinstance(records, dict):
            all_data = records.get("data", [])
            print(f"[{now_ist}] records.data length (for {expiry_str}): {len(all_data)}")

        return data

    except Exception as e:
        print(f"[{now_ist}] Error fetching option chain: {e}")
        return None


def build_strike_map(data: dict) -> dict:
    """
    Returns dict: strikes[strike] = {"CE": ce_oi, "PE": pe_oi}
    v3 response is already for a single expiry (we filtered via URL).
    """
    records = data.get("records", {})
    all_data = records.get("data", [])
    strikes: dict[int, dict[str, int]] = {}

    for item in all_data:
        strike = item.get("strikePrice")
        ce = item.get("CE")
        pe = item.get("PE")
        if strike is None:
            continue

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
    underlying = records.get("underlyingValue", None)
    strike_prices = [item.get("strikePrice") for item in records.get("data", []) if "strikePrice" in item]

    step = None
    if len(strike_prices) >= 2:
        strike_prices = sorted(set(strike_prices))
        diffs = [j - i for i, j in zip(strike_prices[:-1], strike_prices[1:])]
        step = min(diffs) if diffs else None

    return underlying, step


def find_atm_strike(spot_price, strike_prices):
    return min(strike_prices, key=lambda x: abs(x - spot_price))


def percent_change(prev, curr):
    if prev is None:
        return None
    if prev == 0:
        return inf
    return ((curr - prev) / prev) * 100.0


# ===========================
# MAIN ALERT LOGIC
# ===========================

def check_alerts(spot_price, strikes_dict, atm_strike, step):
    if step is None:
        print("Cannot determine strike step; aborting this cycle.")
        return

    monitored_strikes = [
        atm_strike + i * step
        for i in range(-STRIKE_RANGE, STRIKE_RANGE + 1)
    ]

    for strike in monitored_strikes:
        if strike not in strikes_dict:
            continue

        ce_oi = strikes_dict[strike].get("CE", 0)
        pe_oi = strikes_dict[strike].get("PE", 0)

        if ce_oi == 0 and pe_oi == 0:
            continue

        # --- Get previous OI from DB ---
        ce_prev = get_previous_oi(strike, "CE")
        pe_prev = get_previous_oi(strike, "PE")

        ce_change_pct = percent_change(ce_prev, ce_oi) if ce_prev is not None else None
        pe_change_pct = percent_change(pe_prev, pe_oi) if pe_prev is not None else None

        ce_trigger = ce_change_pct is not None and ce_change_pct >= OI_CHANGE_THRESHOLD_PERCENT
        pe_trigger = pe_change_pct is not None and pe_change_pct >= OI_CHANGE_THRESHOLD_PERCENT

        oi_jump_triggered = ce_trigger or pe_trigger

        # --- Call-Put ratio ---
        valid_oi = [x for x in [ce_oi, pe_oi] if x > 0]
        if len(valid_oi) < 2:
            ratio_ok = False
            ratio = None
        else:
            larger = max(ce_oi, pe_oi)
            smaller = min(ce_oi, pe_oi)
            ratio = larger / smaller
            ratio_ok = ratio >= OI_RATIO_THRESHOLD

        # --- Final condition ---
        if oi_jump_triggered and ratio_ok:
            if ce_trigger:
                side = "CE"
                prev_oi = ce_prev
                change_pct = ce_change_pct
            else:
                side = "PE"
                prev_oi = pe_prev
                change_pct = pe_change_pct

            larger_side = "CE" if ce_oi >= pe_oi else "PE"
            diff = abs(ce_oi - pe_oi)

            timestamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
            change_str = "INF" if change_pct == inf else f"{change_pct:.2f}%"
            subject = f"[OI ALERT] {SYMBOL} {strike} {side} OI {change_str} | CE:{ce_oi} PE:{pe_oi}"

            alert_lines = [
                "=" * 80,
                f"TIME        : {timestamp}",
                f"SYMBOL      : {SYMBOL}",
                f"SPOT        : {spot_price}",
                f"ATM STRIKE  : {atm_strike}",
                f"STRIKE      : {strike}",
                "",
                f"CE OI       : {ce_oi:,}",
                f"PE OI       : {pe_oi:,}",
                f"TRIGGER SIDE: {side}",
                f"PREV {side} OI: {prev_oi:,}" if prev_oi is not None else f"PREV {side} OI: N/A",
                f"{side} OI CHANGE %: {change_str}",
                "",
                f"CE-PE ABS DIFF : {diff:,}",
                f"CE vs PE RATIO : {larger_side} ~ {ratio:.2f}x the other side" if ratio is not None else "CE vs PE RATIO : N/A",
                "=" * 80,
                "",
            ]

            alert_text = "\n".join(alert_lines)
            notify_alert(alert_text, subject)

        # Update DB with current OIs for next cycle
        if ce_oi is not None:
            set_previous_oi(strike, "CE", ce_oi)
        if pe_oi is not None:
            set_previous_oi(strike, "PE", pe_oi)


# ===========================
# MAIN LOOP
# ===========================

def main_loop():
    print(f"Starting {SYMBOL} OI monitor for ATM +/- {STRIKE_RANGE} strikes...")
    init_db()

    while True:
        now_ist = datetime.now(IST)

        if not is_market_hours_ist():
            print(f"[{now_ist}] Outside market hours (IST), sleeping for {POLL_INTERVAL_SECONDS}s...")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        print(f"[{now_ist}] Inside market hours (IST). Starting new cycle...")

        data = fetch_option_chain(now_ist)
        if data is None:
            print(f"[{now_ist}] No data from NSE (fetch_option_chain returned None). Sleeping...")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        spot_price, step = get_spot_price_and_step(data)
        if spot_price is None or step is None:
            print(f"[{now_ist}] Could not determine spot price or strike step. Sleeping...")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        strikes_dict = build_strike_map(data)
        all_strikes = sorted(strikes_dict.keys())
        if not all_strikes:
            print(f"[{now_ist}] No strikes in option chain data. Sleeping...")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        atm_strike = find_atm_strike(spot_price, all_strikes)
        print(f"[{now_ist}] Spot: {spot_price}, ATM: {atm_strike}, Step: {step}")

        check_alerts(spot_price, strikes_dict, atm_strike, step)

        print(f"[{now_ist}] Cycle complete. Sleeping for {POLL_INTERVAL_SECONDS}s...\n")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main_loop()