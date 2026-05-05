#!/usr/bin/env bash
# Install all scheduled jobs for the bot user:
#   1. Daily NAV report at 01:00 UTC = 09:00 HKT
#   2. Weekly trade journal digest Mondays 01:00 UTC = 09:00 HKT
#   3. Hourly auto-redeem of resolved positions (Stage 2)
#   4. Daily journal reconcile at 00:30 UTC (catches resolved trades)
#
# Run as the `bot` user (not root). All output appended to logs/.
set -euo pipefail

REPO="/home/bot/polymarket-bot"
PY="$REPO/.venv/bin/python"
LOGS="$REPO/logs"

NAV="0 1 * * * cd $REPO && $PY $REPO/nav_report.py >> $LOGS/nav_cron.log 2>&1"
DIGEST="0 1 * * 1 cd $REPO && $PY $REPO/journal.py --digest >> $LOGS/digest_cron.log 2>&1"
REDEEM="15 * * * * cd $REPO && $PY $REPO/redeemer.py >> $LOGS/redeem_cron.log 2>&1"
RECONCILE="30 0 * * * cd $REPO && $PY $REPO/journal.py --reconcile >> $LOGS/reconcile_cron.log 2>&1"

# Strip prior versions of each job, then append fresh lines.
( crontab -l 2>/dev/null \
    | grep -v 'nav_report.py' \
    | grep -v 'journal.py' \
    | grep -v 'redeemer.py' \
    || true ; \
  echo "$NAV" ; \
  echo "$DIGEST" ; \
  echo "$REDEEM" ; \
  echo "$RECONCILE" ) | crontab -

echo "Installed crons:"
crontab -l | grep -E 'nav_report|journal|redeemer'
echo
echo "Schedule:"
echo "  • Daily NAV         01:00 UTC (09:00 HKT)"
echo "  • Weekly digest     Monday 01:00 UTC (09:00 HKT)"
echo "  • Auto-redeem       Every hour at :15"
echo "  • Reconcile journal 00:30 UTC daily"
