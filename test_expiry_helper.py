from datetime import datetime
import requests

def choose_current_expiry_from_data(data, now_ist: datetime):
    records = data.get("records", {})
    print(f"[{now_ist}] records keys: {list(records.keys())}")

    expiry_list = records.get("expiryDates") or []
    print(f"[{now_ist}] expiryDates from NSE: {expiry_list}")

    if not expiry_list:
        print(f"[{now_ist}] ❌ No expiryDates found in records.")
        return None

    today = now_ist.date()
    parsed = []
    for s in expiry_list:
        try:
            d = datetime.strptime(s, "%d-%b-%Y").date()
            parsed.append((d, s))
        except Exception as e:
            print(f"[{now_ist}] Error parsing expiry date {s}: {e}")

    if not parsed:
        print(f"[{now_ist}] ❌ Could not parse expiryDates.")
        return None

    future = [(d, s) for (d, s) in parsed if d >= today]

    if not future:
        d, s = max(parsed, key=lambda x: x[0])
        print(f"[{now_ist}] ⚠ No future expiry, fallback to last: {s}")
        return s

    future.sort(key=lambda x: x[0])
    chosen_date, chosen_str = future[0]
    print(f"[{now_ist}] ✅ Chosen weekly expiry: {chosen_str}")
    return chosen_str


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nseindia.com/option-chain",
}

session = requests.Session()
session.headers.update(HEADERS)

NSE_URL = "https://www.nseindia.com/api/option-chain-v3?type=Indices&symbol=NIFTY"


def fetch_raw_nse_data():
    warm = session.get("https://www.nseindia.com", timeout=5)
    print("Warmup status:", warm.status_code)

    resp = session.get(NSE_URL, timeout=10)
    print("Main status:", resp.status_code)
    resp.raise_for_status()
    return resp.json()


if __name__ == "__main__":
    now_ist = datetime.now()
    data = fetch_raw_nse_data()
    result = choose_current_expiry_from_data(data, now_ist)
    print("\nFINAL RESULT:", result)