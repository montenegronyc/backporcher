#!/usr/bin/env bash
# Start the backporcher dashboard server.
# Reads password from CREDENTIALS_DIRECTORY (systemd LoadCredential),
# BACKPORCHER_DASHBOARD_PASSWORD env var, or exits with an error.
set -euo pipefail

# If using systemd LoadCredential, the password file is in CREDENTIALS_DIRECTORY.
# Otherwise, BACKPORCHER_DASHBOARD_PASSWORD must be set in the environment.
if [[ -n "${CREDENTIALS_DIRECTORY:-}" ]]; then
    PASS_FILE="$CREDENTIALS_DIRECTORY/backporcher-dashboard-password"
    if [[ -f "$PASS_FILE" ]]; then
        export BACKPORCHER_DASHBOARD_PASSWORD
        BACKPORCHER_DASHBOARD_PASSWORD="$(cat "$PASS_FILE")"
    fi
fi

if [[ -z "${BACKPORCHER_DASHBOARD_PASSWORD:-}" ]]; then
    echo "ERROR: BACKPORCHER_DASHBOARD_PASSWORD not set. Provide it via environment or systemd LoadCredential." >&2
    exit 1
fi

# Resolve the project directory (parent of scripts/)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"
exec "$PROJECT_DIR/.venv/bin/python3" -c "
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
