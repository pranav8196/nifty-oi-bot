# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# NIFTY OI Monitor — Project Reference

## Running Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Run the monitor (requires TELEGRAM_TOKEN and TELEGRAM_CHAT_ID env vars)
python nifty_oi_monitor.py

# Debug expiry detection against live NSE API
python test_expiry_helper.py

```

`test_expiry_helper.py` is a standalone debug script — it calls NSE live and prints which expiry would be chosen. No env vars needed.

---

## Overview
Monitors NIFTY options Open Interest (OI) on NSE. Captures a baseline snapshot at 9:18 AM IST each trading day, then every 60 seconds compares live OI against that baseline and fires Telegram alerts when thresholds are breached.

**Alert condition (BOTH must be true simultaneously):**
- `|OI change vs baseline|` >= `OI_CHANGE_THRESHOLD_PERCENT` (default 400%)
- Current CE/PE OI ratio >= `OI_RATIO_THRESHOLD` (default 2.0x)

---

## Expiry Date Logic

NIFTY has weekly expiries every **Tuesday** (Monday if Tuesday is a market holiday).

**Dynamic detection (primary):** The script calls the NSE option chain API without an expiry parameter to fetch `records.expiryDates` — a live list of upcoming expiry dates. It then picks the first date >= today. This happens once per trading day and the result is cached for the session.

**Hardcoded fallback:** `WEEKLY_EXPIRIES` list and `get_current_weekly_expiry_from_list()` exist in `nifty_oi_monitor.py` (lines ~111–135). The dynamic path already calls this as a fallback when NSE returns no dates. To force it as primary: change the `get_current_expiry()` call in `main_loop()` to `get_current_weekly_expiry_from_list(now_ist)`.

**Why the logic is correct:**
- On expiry day (Tuesday): `exp_date >= today` picks that day's expiry → monitors it until market close ✓
- Day after expiry (Wednesday): that date no longer appears in NSE's list → automatically picks next Tuesday ✓
- Holiday substitution (Monday instead of Tuesday): NSE API reflects the actual expiry date → handled automatically ✓

---

## File Structure

| File | Role |
|------|------|
| `nifty_oi_monitor.py` | Main monitoring script — all logic lives here |
| `render.yaml` | Render deployment config (legacy; primary deployment is GitHub Actions) |
| `start.sh` | Startup script for Render |
| `requirements.txt` | Python deps: `requests`, `google-genai` |
| `oi_history.db` | SQLite DB — two tables: `baseline_oi` (9:18 AM snapshot) and `alert_log` (fired alerts) |
| `.github/workflows/market-monitor-am.yml` | Single full-day session: 9:13 AM – ~3:10 PM IST |

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

Single workflow covers full market hours (within GitHub Actions 6h job limit):

| Workflow | Trigger (UTC) | IST Window | Duration |
|----------|--------------|------------|----------|
| `market-monitor-am.yml` | `43 3 * * 1-5` via cron-job.org | 9:13 AM – ~3:10 PM | 5h57m |

No DB cache handoff needed — single session captures baseline and runs to close.

**GitHub Secrets to add** (repo Settings → Secrets and variables → Actions):
- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT_ID`
- `GEMINI_API_KEY` (optional)

---

## Workflow Trigger: cron-job.org (Backup Scheduler)

GitHub's built-in `schedule:` cron trigger is unreliable — it can be delayed up to 1 hour or silently skipped. To guarantee the workflow fires on time, one cron job on [cron-job.org](https://cron-job.org) (free account: pranav8196) calls the GitHub API via `workflow_dispatch` each market day.

| Job | Schedule (UTC) | IST Time | Triggers |
|-----|---------------|----------|----------|
| NIFTY Full Day Session | `43 3 * * 1-5` | 9:13 AM | `market-monitor-am.yml` |

**How the job is configured:**
- **URL:** `https://api.github.com/repos/pranav8196/nifty-oi-bot/actions/workflows/market-monitor-am.yml/dispatches`
- **Method:** POST
- **Headers:**
  - `Authorization: Bearer <GitHub PAT>`
  - `Accept: application/vnd.github+json`
  - `Content-Type: application/json`
- **Body:** `{"ref":"main"}`

**GitHub PAT requirements:** Fine-grained token, `nifty-oi-bot` repo only, Actions = Read & Write. Expires annually — renew before expiry to avoid silent failures.

**If workflows stop auto-triggering:** Log in to cron-job.org, check job history for failures (likely expired PAT). Regenerate PAT on GitHub → Developer Settings → Fine-grained tokens, update on cron-job.org.

---

## Strategy Logic

1. Script starts → checks if today is a market holiday → exits with Telegram notification if so
2. Sends startup Telegram ping
3. Waits for market open (9:15 AM IST); polls NSE every 60 seconds
4. On first cycle: fetches available expiry dates from NSE API, picks active expiry
5. At 9:18 AM IST: captures baseline OI snapshot for **all strikes** → stored in `baseline_oi` table; sends rich Telegram message (spot, ATM, top 3 CE/PE strikes, PCR)
6. Each subsequent poll: compares current OI vs baseline for ATM ± 6 strikes (range shifts with ATM throughout the day)
7. If both thresholds breached for any strike → dedup check → Telegram alert + optional Gemini analysis; alert logged to `alert_log` table
8. At 3:08 PM IST: sends session close summary (final spot/ATM/PCR + all alerts from the day)
9. Baseline is locked for the day (re-captured only if DB is fresh/empty)

---

## Alert Flow

```
NSE data fetched → thresholds checked → notify_alert()
                                              ├── send_telegram()       # main alert
                                              └── send_llm_analysis()   # Gemini follow-up (optional, model: gemini-2.0-flash)
```

---

## Key Maintenance Items

### LOT_SIZE
- Currently `65` (as of early 2026)
- NSE revises lot sizes periodically — update via `LOT_SIZE` env var (GitHub Secret or workflow env)
- No code change needed

### Hardcoded Expiry Fallback
- `WEEKLY_EXPIRIES` list in `nifty_oi_monitor.py` (~line 111); already used automatically when NSE returns empty dates
- To force as primary: swap the call in `main_loop()` to `get_current_weekly_expiry_from_list(now_ist)`
- Currently listed through Dec 2026 — extend when needed

### Alert Deduplication
- Alerts fire once per breach per (strike, side) per session
- Suppressed while conditions stay breached; re-fires if conditions clear then breach again
- State: in-memory `_alert_active` dict — resets automatically on new trading day

### PCR in Alerts
- Each alert includes PCR = total PE OI / total CE OI across ATM ± 6 strikes only
- PCR < 1 = more calls (bullish lean); PCR > 1 = more puts (bearish lean)

### Market Holidays
- `MARKET_HOLIDAYS` dict in `nifty_oi_monitor.py` (~line 49) — keyed by `YYYY-MM-DD`
- Source: NSE website equity holiday list for the year
- Script exits early on holidays: sends one Telegram notification, no monitoring
- `is_market_hours_ist()` also returns `False` on holidays as a secondary guard
- **Update annually:** add next year's holidays before Dec 31. Current list covers 2026 only.

### Silent Failure Detection
- Workflow distinguishes normal timeout (exit 124) from Python crash (any other non-zero exit)
- On crash: `if: failure()` step sends `"⚠️ session crashed"` Telegram via `curl`

---

## Common Issues

| Issue | Cause | Fix |
|-------|-------|-----|
| Baseline captured late/wrong time | Script restarted mid-day | Single full-day session eliminates this |
| 403 from NSE | Rate limiting / missing cookies | Retry logic (3 attempts, 5s apart) in `fetch_option_chain` |
| Expiry shows None | NSE API down at startup | Script sleeps and retries next cycle; or activate hardcoded fallback |
| Telegram not sending | Missing secrets | Check `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` in GitHub Secrets |
| `INF` in alert text | `base_oi == 0` for a strike | `fmt_pct()` helper handles this safely — no crash |
| Duplicate alerts flooding Telegram | Same strike breaching every 60s | Fixed: dedup via `_alert_active` in-memory dict |
| Cron fires on market holiday | GitHub Actions runs Mon–Fri regardless | Fixed: `MARKET_HOLIDAYS` dict in code — script exits early with a Telegram notification |
