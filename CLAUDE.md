# NIFTY OI Monitor — Project Reference

## Overview
Monitors NIFTY options Open Interest (OI) on NSE. Captures a baseline snapshot at 9:17 AM IST each trading day, then every 60 seconds compares live OI against that baseline and fires Telegram alerts when thresholds are breached.

**Alert condition (BOTH must be true simultaneously):**
- `|OI change vs baseline|` >= `OI_CHANGE_THRESHOLD_PERCENT` (default 400%)
- Current CE/PE OI ratio >= `OI_RATIO_THRESHOLD` (default 2.0x)

---

## Expiry Date Logic

NIFTY has weekly expiries every **Tuesday** (Monday if Tuesday is a market holiday).

**Dynamic detection (primary):** The script calls the NSE option chain API without an expiry parameter to fetch `records.expiryDates` — a live list of upcoming expiry dates. It then picks the first date >= today. This happens once per trading day and the result is cached for the session.

**Hardcoded fallback (commented out):** `WEEKLY_EXPIRIES` list in `nifty_oi_monitor.py` (lines ~68–95). To use it: uncomment the list and the function, then change the `get_current_expiry()` call in `main_loop()` to `get_current_weekly_expiry_from_list()`.

**Why the logic is correct:**
- On expiry day (Tuesday): `exp_date >= today` picks that day's expiry → monitors it until market close ✓
- Day after expiry (Wednesday): that date no longer appears in NSE's list → automatically picks next Tuesday ✓
- Holiday substitution (Monday instead of Tuesday): NSE API reflects the actual expiry date → handled automatically ✓

---

## File Structure

| File | Role |
|------|------|
| `nifty_oi_monitor.py` | Main monitoring script — all logic lives here |
| `streamlit_app.py` | Streamlit dashboard — fetches NSE live, shows OI table |
| `render.yaml` | Render deployment config (legacy; primary deployment is GitHub Actions) |
| `start.sh` | Startup script for Render |
| `requirements.txt` | Python deps: `requests`, `google-generativeai` |
| `oi_history.db` | SQLite DB — ephemeral; persisted between GitHub Actions sessions via cache |
| `.github/workflows/market-monitor-am.yml` | AM session: 9:05 AM – ~1:05 PM IST |
| `.github/workflows/market-monitor-pm.yml` | PM session: 12:50 PM – ~3:35 PM IST |

---

## Environment Variables

### Required
| Variable | Description |
|----------|-------------|
| `TELEGRAM_TOKEN` | Telegram bot token (from BotFather) |
| `TELEGRAM_CHAT_ID` | Telegram chat/group ID to send alerts to |

### Optional
| Variable | Default | Description |
|----------|---------|-------------|
| `GEMINI_API_KEY` | (none) | Google Gemini API key for LLM analysis (free at aistudio.google.com) |
| `SYMBOL` | `NIFTY` | NSE symbol |
| `POLL_INTERVAL_SECONDS` | `60` | Seconds between NSE fetches |
| `LOT_SIZE` | `65` | NIFTY lot size — NSE revises this periodically |
| `STRIKE_RANGE` | `6` | ATM ± N strikes to monitor |
| `OI_CHANGE_THRESHOLD_PERCENT` | `400.0` | Alert threshold: OI % change vs baseline |
| `OI_RATIO_THRESHOLD` | `2.0` | Alert threshold: CE/PE OI ratio |
| `DB_FILE` | `oi_history.db` | SQLite DB path |

---

## Deployment: GitHub Actions (Primary — Zero Cost)

Public repo → unlimited free GitHub Actions minutes.

Two overlapping workflows cover full market hours (GitHub Actions max job runtime = 6h):

| Workflow | Cron (UTC) | IST Window | Duration |
|----------|------------|------------|----------|
| AM (`market-monitor-am.yml`) | `35 3 * * 1-5` | 9:05 AM – ~1:05 PM | 4h |
| PM (`market-monitor-pm.yml`) | `20 7 * * 1-5` | 12:50 PM – ~3:35 PM | ~2h45m |

**Baseline DB persistence:** AM session saves `oi_history.db` to GitHub Actions cache (key = today's IST date). PM session restores it, so the 9:17 AM baseline captured by AM is available to PM.

**GitHub Secrets to add** (repo Settings → Secrets and variables → Actions):
- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT_ID`
- `GEMINI_API_KEY` (optional)

---

## Strategy Logic

1. Script starts; waits for market open (9:15 AM IST, Mon–Fri)
2. Polls NSE every 60 seconds
3. On first cycle: fetches available expiry dates from NSE API, picks active expiry
4. At 9:17 AM IST: captures baseline OI snapshot for all strikes → stored in SQLite
5. Each subsequent poll: compares current OI vs baseline for ATM ± 6 strikes
6. If both thresholds breached for any strike → Telegram alert + optional Gemini analysis
7. Baseline is locked for the day (re-captured only if DB is fresh/empty, e.g. new session)

---

## Alert Flow

```
NSE data fetched → thresholds checked → notify_alert()
                                              ├── send_telegram()       # main alert
                                              └── send_llm_analysis()   # Gemini follow-up (optional)
```

---

## Key Maintenance Items

### LOT_SIZE
- Currently `65` (as of early 2026)
- NSE revises lot sizes periodically — update via `LOT_SIZE` env var (GitHub Secret or workflow env)
- No code change needed

### Hardcoded Expiry Fallback
- Commented-out `WEEKLY_EXPIRIES` list in `nifty_oi_monitor.py`
- Only needed if NSE API stops returning `expiryDates` or returns wrong dates
- Currently listed through Dec 2026 — add more if fallback is ever activated

---

## Common Issues

| Issue | Cause | Fix |
|-------|-------|-----|
| Baseline captured late/wrong time | Script restarted mid-day (Render ephemeral FS) | GitHub Actions + cache solves this |
| 403 from NSE | Rate limiting / missing cookies | Retry logic (3 attempts, 5s apart) in `fetch_option_chain` |
| Expiry shows None | NSE API down at startup | Script sleeps and retries next cycle; or activate hardcoded fallback |
| Telegram not sending | Missing secrets | Check `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` in GitHub Secrets |
| `INF` in alert text | `base_oi == 0` for a strike | `fmt_pct()` helper handles this safely — no crash |

---

## Frontend (Streamlit)

`streamlit_app.py` — deploy on Streamlit Community Cloud (free):
1. Connect GitHub repo at streamlit.io/cloud
2. Set `streamlit_app.py` as main file
3. Add `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` in Streamlit secrets (if needed)

Note: The Streamlit app fetches NSE data independently and reads `oi_history.db` for the baseline. It won't share the DB with GitHub Actions automatically — best used locally or alongside the monitor on the same machine.
