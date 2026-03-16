#!/usr/bin/env bash
# Start the backporcher dashboard server.
# Reads password from CREDENTIALS_DIRECTORY (systemd LoadCredential) or fallback path.
set -euo pipefail

CRED_DIR="${CREDENTIALS_DIRECTORY:-/etc/openclaw/credentials}"
PASS_FILE="$CRED_DIR/backporcher-dashboard-password"

if [[ -f "$PASS_FILE" ]]; then
    export BACKPORCHER_DASHBOARD_PASSWORD
    BACKPORCHER_DASHBOARD_PASSWORD="$(cat "$PASS_FILE")"
else
    echo "ERROR: Password file not found: $PASS_FILE" >&2
    exit 1
fi

cd /home/administrator/backporcher
exec /home/administrator/backporcher/.venv/bin/python3 -c "
import asyncio, logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
from src.config import load_config
from src.db import Database
from src.dashboard import start_dashboard

config = load_config()

async def main():
    db = Database(config.db_path)
    await db.connect()
    await start_dashboard(db, config)

asyncio.run(main())
"
