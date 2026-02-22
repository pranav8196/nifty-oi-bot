#!/bin/bash
python -m http.server $PORT &

# run monitor with unbuffered logging
python -u nifty_oi_monitor.py