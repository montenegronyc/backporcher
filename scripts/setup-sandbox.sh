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

# 3. Path traversal: agent needs g+x on parent dirs to reach repos/logs
#    Use group-only (NOT o+x) to prevent other users from traversing
echo "[3/9] Setting directory traversal permissions..."
chmod g+x "/home/$ADMIN_USER"
chmod o-x "/home/$ADMIN_USER"
chmod g+x "$VOLTRON_DIR"
chmod o-rwx "$VOLTRON_DIR"

# 4. Repos directory: setgid so both users can read/write
echo "[4/9] Setting repos directory permissions..."
mkdir -p "$VOLTRON_DIR/repos"
chgrp -R "$SHARED_GROUP" "$VOLTRON_DIR/repos"
find "$VOLTRON_DIR/repos" -type d -exec chmod g+rwxs {} +
find "$VOLTRON_DIR/repos" -type f -exec chmod g+rw {} +

# 5. Logs directory: same treatment, but restrict from others
echo "[5/9] Setting logs directory permissions..."
mkdir -p "$VOLTRON_DIR/logs"
chgrp -R "$SHARED_GROUP" "$VOLTRON_DIR/logs"
chmod 2770 "$VOLTRON_DIR/logs"
find "$VOLTRON_DIR/logs" -type f -exec chmod 640 {} +

# 6. Data directory: restrict database access
echo "[6/9] Setting data directory permissions..."
mkdir -p "$VOLTRON_DIR/data"
chgrp "$SHARED_GROUP" "$VOLTRON_DIR/data"
chmod 750 "$VOLTRON_DIR/data"
find "$VOLTRON_DIR/data" -type f -exec chmod 640 {} +

# 7. Set core.sharedRepository=group on all repos
echo "[7/9] Configuring git shared repository..."
for repo_dir in "$VOLTRON_DIR/repos"/*/; do
    if [ -d "$repo_dir/.git" ]; then
        git -C "$repo_dir" config core.sharedRepository group
        echo "  Set sharedRepository=group on $repo_dir"
    fi
done

# 8. Claude credentials (COPY, not symlink — no access to admin's home)
echo "[8/9] Copying Claude credentials to agent user..."
mkdir -p "$AGENT_HOME/.claude"
if [ -f "/home/$ADMIN_USER/.claude/.credentials.json" ]; then
    install -m 600 -o "$AGENT_USER" -g "$SHARED_GROUP" \
        "/home/$ADMIN_USER/.claude/.credentials.json" "$AGENT_HOME/.claude/"
else
    echo "  WARNING: /home/$ADMIN_USER/.claude/.credentials.json not found"
fi
for f in settings.json settings.local.json; do
    if [ -f "/home/$ADMIN_USER/.claude/$f" ]; then
        install -m 600 -o "$AGENT_USER" -g "$SHARED_GROUP" \
            "/home/$ADMIN_USER/.claude/$f" "$AGENT_HOME/.claude/"
    fi
done
mkdir -p "$AGENT_HOME/.claude"/{projects,session-env,todos,debug}
chown -R "$AGENT_USER:$SHARED_GROUP" "$AGENT_HOME/.claude"
chmod 700 "$AGENT_HOME/.claude"

# 9. Git config (identity + safe.directory scoped to repos only)
echo "[9/9] Setting up git config for agent user..."
cat > "$AGENT_HOME/.gitconfig" << GITEOF
[user]
    name = Voltron Agent
    email = voltron@dispatch.local
[safe]
    directory = $VOLTRON_DIR/repos
GITEOF
chown "$AGENT_USER:$SHARED_GROUP" "$AGENT_HOME/.gitconfig"
chmod 600 "$AGENT_HOME/.gitconfig"

# 10. Sudoers rule: administrator can run prlimit as voltron-agent (nothing else)
echo "[10/10] Configuring sudoers..."
cat > /etc/sudoers.d/voltron-agent << 'SUDOEOF'
administrator ALL=(voltron-agent) NOPASSWD: /usr/bin/prlimit
SUDOEOF
chmod 440 /etc/sudoers.d/voltron-agent
visudo -c -q

# Verify
echo ""
echo "=== Verification ==="
id "$AGENT_USER"
groups "$ADMIN_USER" | grep -q "$SHARED_GROUP" && echo "OK: $ADMIN_USER in $SHARED_GROUP group" || echo "WARN: $ADMIN_USER not in $SHARED_GROUP group (re-login required)"
sudo -u "$AGENT_USER" -- /usr/bin/prlimit --nproc=100 -- whoami && echo "OK: prlimit sandbox works"
if [ -f "$AGENT_HOME/.claude/.credentials.json" ]; then
    sudo -u "$AGENT_USER" test -r "$AGENT_HOME/.claude/.credentials.json" && echo "OK: agent can read Claude credentials"
fi
stat -c 'repos dir: %A %G' "$VOLTRON_DIR/repos/"
stat -c 'logs dir:  %A %G' "$VOLTRON_DIR/logs/"
stat -c 'data dir:  %A %G' "$VOLTRON_DIR/data/"
echo ""
echo "Setup complete. Notes:"
echo "  - Log out and back in for group membership to take effect"
echo "  - If Claude credentials are rotated, re-run this script to sync them"
echo "  - Set VOLTRON_AGENT_USER=voltron-agent in voltron.service or env"
