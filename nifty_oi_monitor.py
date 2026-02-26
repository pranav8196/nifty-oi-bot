import os
import requests
import time
from datetime import datetime, time as dtime, timezone, timedelta
from math import inf
import sqlite3
import smtplib
from email.mime.text import MIMEText  # correct import

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

print("=== Starting NIFTY OI Monitor (Baseline vs 09:17 Snapshot) ===")

# ===========================
# CONFIGURATION
# ===========================

SYMBOL = os.getenv("SYMBOL", "NIFTY")

# Now interpreted as % change vs BASELINE (not vs previous minute)
OI_CHANGE_THRESHOLD_PERCENT = float(os.getenv("OI_CHANGE_THRESHOLD_PERCENT", "400.0"))  # e.g. 400%
OI_RATIO_THRESHOLD = float(os.getenv("OI_RATIO_THRESHOLD", "2.0"))                      # e.g. 2x CE/PE imbalance
STRIKE_RANGE = int(os.getenv("STRIKE_RANGE", "6"))                                      # ATM +/- 6 strikes

# 1.5 minutes default
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "90"))

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


# ---------- Telegram Alert Config ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")        # Bot token from BotFather
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")    # Your user/group/chat ID

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
        chosen = WEEKLY_EXPIRIES[-1]

    print(f"[{now_ist}] Using weekly expiry from list: {chosen}")
    return chosen


# ===========================
# MARKET HOURS (IST) CHECK
# ===========================

def is_market_hours_ist(now_ist: datetime | None = None) -> bool:
    """
    NSE regular trading hours:
    Monday–Friday, 09:15–15:30 IST
    """
    if now_ist is None:
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

    # Old table no longer needed
    c.execute("DROP TABLE IF EXISTS oi_data")

    # Baseline snapshot once per trading day + expiry
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
    """
    Return the baseline_time string for given trading_date+expiry, or None.
    """
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        """
        SELECT baseline_time
        FROM baseline_oi
        WHERE trading_date = ? AND expiry = ?
        LIMIT 1
        """,
        (trading_date, expiry),
    )
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def store_baseline_snapshot(trading_date: str, expiry: str, baseline_time: datetime, strikes_dict: dict):
    """
    Store baseline OI for ALL strikes and both CE/PE for given trading_date+expiry.
    Overwrites any existing baseline rows for that date+expiry.
    """
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # Clear old baseline for this trading_date+expiry (just to be safe)
    c.execute(
        "DELETE FROM baseline_oi WHERE trading_date = ? AND expiry = ?",
        (trading_date, expiry),
    )

    baseline_time_str = baseline_time.strftime("%Y-%m-%d %H:%M:%S")

    print(f"[{baseline_time}] 🚀 CAPTURING BASELINE for {trading_date} at {baseline_time_str} IST (expiry={expiry})...")

    inserted_rows = 0
    for strike, sides in strikes_dict.items():
        for option_type in ("CE", "PE"):
            oi_value = sides.get(option_type)
            if oi_value is None:
                continue

            c.execute(
                """
                INSERT INTO baseline_oi (trading_date, expiry, strike, option_type, base_oi, baseline_time)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (trading_date, expiry, strike, option_type, int(oi_value), baseline_time_str),
            )
            inserted_rows += 1

    conn.commit()
    conn.close()
    print(
        f"[{baseline_time}] ✅ BASELINE STORED for {trading_date} at {baseline_time_str} IST "
        f"(unique strikes={len(strikes_dict)}, rows={inserted_rows})"
    )
    print(f"[{baseline_time}] ▶ All comparisons today will use this baseline.\n")


def load_baseline_snapshot(trading_date: str, expiry: str) -> dict:
    """
    Load baseline OI into dict: baseline[strike][option_type] = base_oi
    """
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        """
        SELECT strike, option_type, base_oi
        FROM baseline_oi
        WHERE trading_date = ? AND expiry = ?
        """,
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
    send_telegram(alert_text)



def send_telegram(message: str):
    """
    Send alert message to Telegram using a bot.
    Requires TELEGRAM_TOKEN and TELEGRAM_CHAT_ID env vars.
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[{datetime.now(IST)}] Telegram not configured "
              f"(missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID).")
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown"  # or remove if you don't want formatting
        }

        resp = requests.post(url, json=payload, timeout=5)
        print(f"[{datetime.now(IST)}] Telegram send status: {resp.status_code}")
        # Optional: uncomment to debug response body
        # print(resp.text[:300])

    except Exception as e:
        print(f"[{datetime.now(IST)}] Error sending Telegram message: {e}")


# ===========================
# NSE FUNCTIONS (v3 with expiry param)
# ===========================

def fetch_option_chain(now_ist: datetime, expiry_str: str) -> dict | None:
    """
    Fetch option chain only for the given weekly expiry.
    """
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


def compute_change_vs_baseline(base_oi: int | None, curr_oi: int | None):
    """
    Returns (pct_change_abs, diff, direction)

    - pct_change_abs: |ΔOI| / base * 100 (float) or None
    - diff: curr - base (can be +, -, or 0)
    - direction: "UP", "DOWN", or "FLAT" (or None if base invalid)
    """
    if base_oi is None or curr_oi is None:
        return None, None, None
    if base_oi == 0:
        # Avoid division by 0; treat as infinite move
        diff = curr_oi - base_oi
        direction = "UP" if diff > 0 else ("DOWN" if diff < 0 else "FLAT")
        return inf, diff, direction

    diff = curr_oi - base_oi
    direction = "UP" if diff > 0 else ("DOWN" if diff < 0 else "FLAT")
    pct_change_abs = (abs(diff) / base_oi) * 100.0
    return pct_change_abs, diff, direction


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
    LOT_SIZE = 65  # <-- NEW LOT SIZE CONSTANT

    def oi_to_lakhs(oi):
        lots = oi * LOT_SIZE
        lakhs = lots / 100000
        return lots, f"{lakhs:.2f} lakh"

    if step is None:
        print(f"[{now_ist}] Cannot determine strike step; aborting this cycle.")
        return

    monitored_strikes = [
        atm_strike + i * step
        for i in range(-STRIKE_RANGE, STRIKE_RANGE + 1)
    ]

    print(f"[{now_ist}] Monitored strikes this cycle: {monitored_strikes}")
    print(
        f"[{now_ist}] Thresholds: OI_CHANGE_THRESHOLD_PERCENT={OI_CHANGE_THRESHOLD_PERCENT}, "
        f"OI_RATIO_THRESHOLD={OI_RATIO_THRESHOLD}"
    )
    print(f"[{now_ist}] Comparing vs BASELINE snapshot for {trading_date}, expiry {expiry_str}.")

    for strike in monitored_strikes:
        curr = current_strikes.get(strike)
        base = baseline_strikes.get(strike)

        if curr is None or base is None:
            print(f"[{now_ist}] Strike {strike}: missing current or baseline OI, skipping.")
            continue

        ce_curr = curr.get("CE", 0)
        pe_curr = curr.get("PE", 0)
        ce_base = base.get("CE", 0)
        pe_base = base.get("PE", 0)

        if ce_curr == 0 and pe_curr == 0:
            print(f"[{now_ist}] Strike {strike}: both CE and PE OI are 0 currently, skipping.")
            continue

        # --- Change vs baseline (absolute %) ---
        ce_change_pct, ce_diff, ce_dir = compute_change_vs_baseline(ce_base, ce_curr)
        pe_change_pct, pe_diff, pe_dir = compute_change_vs_baseline(pe_base, pe_curr)

        ce_trigger = ce_change_pct is not None and ce_change_pct >= OI_CHANGE_THRESHOLD_PERCENT
        pe_trigger = pe_change_pct is not None and pe_change_pct >= OI_CHANGE_THRESHOLD_PERCENT

        # --- CE/PE Ratio based on CURRENT OI ---
        valid_oi = [x for x in [ce_curr, pe_curr] if x > 0]
        if len(valid_oi) < 2:
            ratio_ok = False
            ratio = None
        else:
            larger = max(ce_curr, pe_curr)
            smaller = min(ce_curr, pe_curr)
            ratio = larger / smaller
            ratio_ok = ratio >= OI_RATIO_THRESHOLD

        # ----- DEBUG LOG PER STRIKE -----
        print(
            f"[{now_ist}] Strike {strike}: "
            f"CE base={ce_base}, curr={ce_curr}, Δ={ce_diff}, |Δ%|={ce_change_pct}, dir={ce_dir}, trigger={ce_trigger}; "
            f"PE base={pe_base}, curr={pe_curr}, Δ={pe_diff}, |Δ%|={pe_change_pct}, dir={pe_dir}, trigger={pe_trigger}; "
            f"ratio={ratio}, ratio_ok={ratio_ok}"
        )

        # --- Final condition ---
        oi_jump_triggered = ce_trigger or pe_trigger
        
        if oi_jump_triggered and ratio_ok:
            print(f"[{now_ist}] 🚨 ALERT CONDITIONS MET for strike {strike}! Building alert...")

            
            #Determine which actually triggered the alert
            if ce_trigger:
                trigger_side = "CE"
            elif pe_trigger:
                trigger_side = "PE"
            else:
                trigger_side = "Unknown"

        
            
            
            
            # Always show BOTH CE and PE details
            ce_lots, ce_lakhs = oi_to_lakhs(ce_curr)
            pe_lots, pe_lakhs = oi_to_lakhs(pe_curr)
            ce_base_lots, ce_base_lakhs = oi_to_lakhs(ce_base)
            pe_base_lots, pe_base_lakhs = oi_to_lakhs(pe_base)

            diff_lots = (ce_curr - pe_curr) * LOT_SIZE
            diff_lakhs = diff_lots / 100000

            timestamp = now_ist.strftime("%Y-%m-%d %H:%M:%S")

            alert_lines = [
                "=" * 90,
                f"TIME              : {timestamp} (IST)",
                f"TRADING DATE      : {trading_date}",
                f"EXPIRY            : {expiry_str}",
                f"SYMBOL            : {SYMBOL}",
                f"SPOT              : {spot_price}",
                f"ATM STRIKE        : {atm_strike}",
                f"STRIKE            : {strike}",
                f"LOT SIZE          : {LOT_SIZE}",
                "",
                f"📌 ALERT TRIGGERED BY: {trigger_side} SIDE ",
                "",
                # --- CE DETAILS ---
                f"BASELINE CE OI    : {ce_base:,} → {ce_base_lots:,} ({ce_base_lakhs})",
                f"CURRENT  CE OI    : {ce_curr:,} → {ce_lots:,} ({ce_lakhs})",
                f"CE ΔOI            : {ce_diff:,} ({ce_dir})",
                f"CE |ΔOI%| vs baseline: {ce_change_pct:.2f}%",
                "",
                # --- PE DETAILS ---
                f"BASELINE PE OI    : {pe_base:,} → {pe_base_lots:,} ({pe_base_lakhs})",
                f"CURRENT  PE OI    : {pe_curr:,} → {pe_lots:,} ({pe_lakhs})",
                f"PE ΔOI            : {pe_diff:,} ({pe_dir})",
                f"PE |ΔOI%| vs baseline: {pe_change_pct:.2f}%",
                "",
                # --- DIFF & RATIO ---
                f"CE-PE ABS DIFF    : {abs(ce_curr - pe_curr):,} → {abs(diff_lots):,} ({abs(diff_lakhs):.2f} lakh)",
                f"CE vs PE RATIO    : {'CE' if ce_curr >= pe_curr else 'PE'} ~ {ratio:.2f}x the other side",
                "",
                f"THRESHOLDS        : |ΔOI%| ≥ {OI_CHANGE_THRESHOLD_PERCENT} AND CE/PE ratio ≥ {OI_RATIO_THRESHOLD}",
                "=" * 90,
                "",
            ]

            alert_text = "\n".join(alert_lines)
            notify_alert(alert_text, "OI ALERT")






# ===========================
# BASELINE LOGIC
# ===========================

def ensure_baseline_for_today(
    now_ist: datetime,
    expiry_str: str,
    strikes_dict: dict,
) -> tuple[bool, str]:
    """
    Ensure we have a baseline snapshot for (today, expiry_str).

    - Baseline date key: today's calendar date (IST).
    - If baseline doesn't exist and time >= 09:17:
        - Capture baseline immediately (all strikes).
        - If time > 09:22, log that baseline is "late", but still accept.
    - If time < 09:17:
        - Return False (baseline not ready yet).
    """
    trading_date = now_ist.date().isoformat()

    if baseline_exists(trading_date, expiry_str):
        baseline_time_str = get_baseline_time(trading_date, expiry_str)
        if baseline_time_str:
            print(
                f"[{now_ist}] 📌 Baseline already exists for {trading_date}, expiry {expiry_str} "
                f"(captured at {baseline_time_str} IST)"
            )
        else:
            print(
                f"[{now_ist}] 📌 Baseline already exists for {trading_date}, expiry {expiry_str} "
                f"(capture time not found)"
            )
        return True, trading_date

    t = now_ist.time()
    baseline_start = dtime(9, 17)
    baseline_soft_end = dtime(9, 22)

    if t < baseline_start:
        print(f"[{now_ist}] Baseline NOT captured yet. Waiting until after 09:17 IST...")
        return False, trading_date

    # We are at or after 09:17, so capture baseline now
    if t > baseline_soft_end:
        print(
            f"[{now_ist}] ⚠ Capturing baseline LATE (after 09:22). "
            f"Still using this as today's reference snapshot."
        )

    print(f"[{now_ist}] Capturing baseline snapshot for {trading_date}, expiry {expiry_str}...")
    store_baseline_snapshot(trading_date, expiry_str, now_ist, strikes_dict)
    return True, trading_date


# ===========================
# MAIN LOOP
# ===========================

def main_loop():
    print(
        f"Starting {SYMBOL} OI monitor (baseline vs 09:17 snapshot) for ATM +/- {STRIKE_RANGE} strikes..."
    )
    print(
        f"Active thresholds: OI_CHANGE_THRESHOLD_PERCENT={OI_CHANGE_THRESHOLD_PERCENT}, "
        f"OI_RATIO_THRESHOLD={OI_RATIO_THRESHOLD}, POLL_INTERVAL_SECONDS={POLL_INTERVAL_SECONDS}"
    )
    init_db()

    while True:
        now_ist = datetime.now(IST)

        if not is_market_hours_ist(now_ist):
            print(f"[{now_ist}] Outside market hours (IST), sleeping for {POLL_INTERVAL_SECONDS}s...")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        print(f"[{now_ist}] Inside market hours (IST). Starting new cycle...")

        # Determine which weekly expiry to use (based on your list)
        expiry_str = get_current_weekly_expiry_from_list(now_ist)

        # Fetch option chain for that expiry
        data = fetch_option_chain(now_ist, expiry_str)
        if data is None:
            print(f"[{now_ist}] No data from NSE (fetch_option_chain returned None). Sleeping...")
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
        print(f"[{now_ist}] Spot: {spot_price}, ATM: {atm_strike}, Step: {step}")

        # Ensure baseline exists for (today, expiry_str)
        baseline_ready, trading_date = ensure_baseline_for_today(now_ist, expiry_str, current_strikes)
        if not baseline_ready:
            print(f"[{now_ist}] Baseline not ready yet. Skipping alerts this cycle.")
            print(f"[{now_ist}] Cycle complete. Sleeping for {POLL_INTERVAL_SECONDS}s...\n")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        # Load baseline for today + expiry
        baseline_strikes = load_baseline_snapshot(trading_date, expiry_str)
        if not baseline_strikes:
            print(
                f"[{now_ist}] ⚠ Baseline expected but empty for {trading_date}, expiry {expiry_str}."
            )
            print(f"[{now_ist}] Skipping alerts this cycle.")
            print(f"[{now_ist}] Cycle complete. Sleeping for {POLL_INTERVAL_SECONDS}s...\n")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        baseline_time_str = get_baseline_time(trading_date, expiry_str)
        if baseline_time_str:
            print(
                f"[{now_ist}] 📌 Using baseline captured at {baseline_time_str} IST "
                f"for {trading_date}, expiry {expiry_str}"
            )
        else:
            print(
                f"[{now_ist}] ⚠ Using baseline for {trading_date}, expiry {expiry_str}, "
                f"but capture time not found in DB."
            )

        # Run alert logic vs baseline
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

        print(f"[{now_ist}] Cycle complete. Sleeping for {POLL_INTERVAL_SECONDS}s...\n")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main_loop()