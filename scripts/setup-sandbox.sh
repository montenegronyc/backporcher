#!/usr/bin/env bash
# setup-sandbox.sh — One-time setup for backporcher-agent sandbox.
# Creates a restricted user, shared group, and minimal credentials
# so agents can work in worktrees but can't access admin secrets.
#
# Usage:
#   sudo bash scripts/setup-sandbox.sh                    # auto-detects SUDO_USER
#   sudo bash scripts/setup-sandbox.sh --admin-user lee   # explicit admin user
#
# Idempotent — safe to re-run (e.g. after credential rotation).
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Must run as root (sudo bash scripts/setup-sandbox.sh)"
    exit 1
fi

# Parse arguments
ADMIN_USER=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --admin-user) ADMIN_USER="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# Auto-detect from SUDO_USER if not explicitly set
if [[ -z "$ADMIN_USER" ]]; then
    ADMIN_USER="${SUDO_USER:-}"
    if [[ -z "$ADMIN_USER" || "$ADMIN_USER" == "root" ]]; then
        echo "ERROR: Cannot detect admin user. Run with: sudo bash scripts/setup-sandbox.sh --admin-user <username>"
        exit 1
    fi
fi

AGENT_USER="backporcher-agent"
SHARED_GROUP="backporcher"
BACKPORCHER_DIR="/home/$ADMIN_USER/backporcher"
AGENT_HOME="/home/$AGENT_USER"

echo "Admin user: $ADMIN_USER"
echo "Backporcher dir: $BACKPORCHER_DIR"
echo ""

echo "=== Backporcher Agent Sandbox Setup ==="

# 1. Create shared group
echo "[1/9] Creating shared group '$SHARED_GROUP'..."
groupadd --system "$SHARED_GROUP" 2>/dev/null || true
usermod -aG "$SHARED_GROUP" "$ADMIN_USER"

# 2. Create agent user (system, no login, primary group backporcher)
echo "[2/9] Creating agent user '$AGENT_USER'..."
useradd --system --gid "$SHARED_GROUP" --home-dir "$AGENT_HOME" \
  --create-home --shell /usr/sbin/nologin "$AGENT_USER" 2>/dev/null || true

# 3. Path traversal: agent needs to traverse admin home to reach backporcher dir
#    o+x on home (traverse only, no read), backporcher dir group-owned by backporcher
echo "[3/9] Setting directory traversal permissions..."
chmod o+x "/home/$ADMIN_USER"
chgrp "$SHARED_GROUP" "$BACKPORCHER_DIR"
chmod g+x "$BACKPORCHER_DIR"
chmod o-rwx "$BACKPORCHER_DIR"

# 4. Repos directory: setgid so both users can read/write
echo "[4/9] Setting repos directory permissions..."
mkdir -p "$BACKPORCHER_DIR/repos"
chgrp -R "$SHARED_GROUP" "$BACKPORCHER_DIR/repos"
find "$BACKPORCHER_DIR/repos" -type d -exec chmod g+rwxs {} +
find "$BACKPORCHER_DIR/repos" -type f -exec chmod g+rw {} +

# 5. Logs directory: same treatment, but restrict from others
echo "[5/9] Setting logs directory permissions..."
mkdir -p "$BACKPORCHER_DIR/logs"
chgrp -R "$SHARED_GROUP" "$BACKPORCHER_DIR/logs"
chmod 2770 "$BACKPORCHER_DIR/logs"
find "$BACKPORCHER_DIR/logs" -type f -exec chmod 640 {} +

# 6. Data directory: restrict database access
echo "[6/9] Setting data directory permissions..."
mkdir -p "$BACKPORCHER_DIR/data"
chgrp "$SHARED_GROUP" "$BACKPORCHER_DIR/data"
chmod 750 "$BACKPORCHER_DIR/data"
find "$BACKPORCHER_DIR/data" -type f -exec chmod 640 {} +

# 7. Set core.sharedRepository=group on all repos
echo "[7/9] Configuring git shared repository..."
for repo_dir in "$BACKPORCHER_DIR/repos"/*/; do
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
    name = Backporcher Agent
    email = backporcher@dispatch.local
[safe]
    directory = $BACKPORCHER_DIR/repos
GITEOF
chown "$AGENT_USER:$SHARED_GROUP" "$AGENT_HOME/.gitconfig"
chmod 600 "$AGENT_HOME/.gitconfig"

# 10. Sudoers rule: administrator can run prlimit as backporcher-agent (nothing else)
echo "[10/10] Configuring sudoers..."
cat > /etc/sudoers.d/backporcher-agent << 'SUDOEOF'
administrator ALL=(backporcher-agent) NOPASSWD: /usr/bin/prlimit
SUDOEOF
chmod 440 /etc/sudoers.d/backporcher-agent
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
stat -c 'repos dir: %A %G' "$BACKPORCHER_DIR/repos/"
stat -c 'logs dir:  %A %G' "$BACKPORCHER_DIR/logs/"
stat -c 'data dir:  %A %G' "$BACKPORCHER_DIR/data/"
echo ""
echo "Setup complete. Notes:"
echo "  - Log out and back in for group membership to take effect"
echo "  - If Claude credentials are rotated, re-run this script to sync them"
echo "  - Set BACKPORCHER_AGENT_USER=backporcher-agent in backporcher.service or env"
