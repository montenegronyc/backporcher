#!/usr/bin/env bash
# setup-sandbox.sh — One-time setup for voltron-agent sandbox.
# Creates a restricted user, shared group, and minimal credentials
# so agents can work in worktrees but can't access admin secrets.
#
# Run with: sudo bash scripts/setup-sandbox.sh
# Idempotent — safe to re-run (e.g. after credential rotation).
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Must run as root (sudo bash scripts/setup-sandbox.sh)"
    exit 1
fi

ADMIN_USER="administrator"
AGENT_USER="voltron-agent"
SHARED_GROUP="voltron"
VOLTRON_DIR="/home/$ADMIN_USER/voltron"
AGENT_HOME="/home/$AGENT_USER"

echo "=== Voltron Agent Sandbox Setup ==="

# 1. Create shared group
echo "[1/9] Creating shared group '$SHARED_GROUP'..."
groupadd --system "$SHARED_GROUP" 2>/dev/null || true
usermod -aG "$SHARED_GROUP" "$ADMIN_USER"

# 2. Create agent user (system, no login, primary group voltron)
echo "[2/9] Creating agent user '$AGENT_USER'..."
useradd --system --gid "$SHARED_GROUP" --home-dir "$AGENT_HOME" \
  --create-home --shell /usr/sbin/nologin "$AGENT_USER" 2>/dev/null || true

# 3. Path traversal: agent needs o+x on parent dirs to reach repos/logs
echo "[3/10] Setting directory traversal permissions..."
chmod o+x "/home/$ADMIN_USER"
chmod o+x "$VOLTRON_DIR"

# 4. Repos directory: setgid so both users can read/write
echo "[4/10] Setting repos directory permissions..."
mkdir -p "$VOLTRON_DIR/repos"
chgrp -R "$SHARED_GROUP" "$VOLTRON_DIR/repos"
find "$VOLTRON_DIR/repos" -type d -exec chmod g+rwxs {} +
find "$VOLTRON_DIR/repos" -type f -exec chmod g+rw {} +

# 4. Logs directory: same treatment
echo "[5/10] Setting logs directory permissions..."
mkdir -p "$VOLTRON_DIR/logs"
chgrp -R "$SHARED_GROUP" "$VOLTRON_DIR/logs"
chmod g+rwxs "$VOLTRON_DIR/logs"

# 5. Set core.sharedRepository=group on all repos
echo "[6/10] Configuring git shared repository..."
for repo_dir in "$VOLTRON_DIR/repos"/*/; do
    if [ -d "$repo_dir/.git" ]; then
        git -C "$repo_dir" config core.sharedRepository group
        echo "  Set sharedRepository=group on $repo_dir"
    fi
done

# 6. Claude credentials (COPY, not symlink — no access to admin's home)
echo "[7/10] Copying Claude credentials to agent user..."
mkdir -p "$AGENT_HOME/.claude"
if [ -f "/home/$ADMIN_USER/.claude/.credentials.json" ]; then
    cp "/home/$ADMIN_USER/.claude/.credentials.json" "$AGENT_HOME/.claude/"
else
    echo "  WARNING: /home/$ADMIN_USER/.claude/.credentials.json not found"
fi
for f in settings.json settings.local.json; do
    if [ -f "/home/$ADMIN_USER/.claude/$f" ]; then
        cp "/home/$ADMIN_USER/.claude/$f" "$AGENT_HOME/.claude/"
    fi
done
mkdir -p "$AGENT_HOME/.claude"/{projects,session-env,todos,debug}
chown -R "$AGENT_USER:$SHARED_GROUP" "$AGENT_HOME/.claude"
chmod 700 "$AGENT_HOME/.claude"
if [ -f "$AGENT_HOME/.claude/.credentials.json" ]; then
    chmod 600 "$AGENT_HOME/.claude/.credentials.json"
fi

# 7. Git config (identity + safe.directory for cross-user worktrees)
echo "[8/10] Setting up git config for agent user..."
cat > "$AGENT_HOME/.gitconfig" << 'GITEOF'
[user]
    name = Voltron Agent
    email = voltron@dispatch.local
[safe]
    directory = *
GITEOF
chown "$AGENT_USER:$SHARED_GROUP" "$AGENT_HOME/.gitconfig"

# 8. Sudoers rule: administrator can run commands as voltron-agent
echo "[9/10] Configuring sudoers..."
cat > /etc/sudoers.d/voltron-agent << 'SUDOEOF'
administrator ALL=(voltron-agent) NOPASSWD: ALL
SUDOEOF
chmod 440 /etc/sudoers.d/voltron-agent
visudo -c -q

# 9. Verify
echo ""
echo "=== Verification ==="
id "$AGENT_USER"
groups "$ADMIN_USER" | grep -q "$SHARED_GROUP" && echo "OK: $ADMIN_USER in $SHARED_GROUP group" || echo "WARN: $ADMIN_USER not in $SHARED_GROUP group (re-login required)"
sudo -u "$AGENT_USER" whoami && echo "OK: sudo -u $AGENT_USER works"
if [ -f "$AGENT_HOME/.claude/.credentials.json" ]; then
    sudo -u "$AGENT_USER" test -r "$AGENT_HOME/.claude/.credentials.json" && echo "OK: agent can read Claude credentials"
fi
stat -c 'repos dir: %A %G' "$VOLTRON_DIR/repos/"
echo ""
echo "Setup complete. Notes:"
echo "  - Log out and back in for group membership to take effect"
echo "  - If Claude credentials are rotated, re-run this script to sync them"
echo "  - Set VOLTRON_AGENT_USER=voltron-agent in voltron.service or env"
