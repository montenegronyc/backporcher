#!/usr/bin/env bash
# Generate local service files from .example templates.
# Run once after clone, or again after pulling template changes.
#
# Usage:
#   ./scripts/configure.sh                          # interactive prompts
#   DEPLOY_USER=me DEPLOY_GROUP=me GITHUB_OWNER=myorg ./scripts/configure.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Gather values from env or prompt
if [[ -z "${DEPLOY_USER:-}" ]]; then
    read -rp "System user [$(whoami)]: " DEPLOY_USER
    DEPLOY_USER="${DEPLOY_USER:-$(whoami)}"
fi

if [[ -z "${DEPLOY_GROUP:-}" ]]; then
    read -rp "System group [$DEPLOY_USER]: " DEPLOY_GROUP
    DEPLOY_GROUP="${DEPLOY_GROUP:-$DEPLOY_USER}"
fi

if [[ -z "${GITHUB_OWNER:-}" ]]; then
    read -rp "GitHub owner (org or username): " GITHUB_OWNER
fi

if [[ -z "${GITHUB_OWNER:-}" ]]; then
    echo "ERROR: GitHub owner is required." >&2
    exit 1
fi

# Optional: allowed users defaults to github owner
if [[ -z "${ALLOWED_USERS:-}" ]]; then
    read -rp "Allowed issue authors [$GITHUB_OWNER]: " ALLOWED_USERS
    ALLOWED_USERS="${ALLOWED_USERS:-$GITHUB_OWNER}"
fi

echo ""
echo "Generating service files with:"
echo "  User:          $DEPLOY_USER"
echo "  Group:         $DEPLOY_GROUP"
echo "  GitHub owner:  $GITHUB_OWNER"
echo "  Allowed users: $ALLOWED_USERS"
echo ""

# Generate from templates
for template in backporcher.service.example backporcher-dashboard.service.example; do
    output="${template%.example}"
    sed \
        -e "s|YOUR_USER|$DEPLOY_USER|g" \
        -e "s|YOUR_GROUP|$DEPLOY_GROUP|g" \
        -e "s|your-github-org|$GITHUB_OWNER|g" \
        -e "s|your-github-username|$ALLOWED_USERS|g" \
        "$template" > "$output"
    echo "  Created $output"
done

echo ""
echo "Done. Next steps:"
echo "  1. Review the generated .service files"
echo "  2. sudo cp backporcher.service /etc/systemd/system/"
echo "  3. sudo cp backporcher-dashboard.service /etc/systemd/system/"
echo "  4. sudo systemctl daemon-reload"
echo "  5. Edit /etc/systemd/system/*.service for secrets (dashboard password, credentials path)"
