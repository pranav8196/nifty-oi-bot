#!/bin/bash
# Start a tiny HTTP server for Render health checks
python -m http.server $PORT &

# Run your NIFTY OI monitor
python nifty_oi_monitor.py