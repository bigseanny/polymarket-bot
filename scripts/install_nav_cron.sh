#!/usr/bin/env bash
# Install daily NAV report cron job for the bot user.
# Runs at 01:00 UTC = 09:00 HKT every day.
set -euo pipefail

REPO="/home/bot/polymarket-bot"
CRON_LINE="0 1 * * * cd $REPO && $REPO/.venv/bin/python $REPO/nav_report.py >> $REPO/logs/nav_cron.log 2>&1"

# Read current crontab (if any), strip prior nav_report lines, append fresh line
( crontab -l 2>/dev/null | grep -v 'nav_report.py' || true ; echo "$CRON_LINE" ) | crontab -

echo "Installed cron:"
crontab -l | grep nav_report
echo
echo "Will run daily at 01:00 UTC (09:00 HKT)."
