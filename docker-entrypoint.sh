#!/bin/sh
# Railway entrypoint: prepares SSH + the vault clone, then runs the bot.
#
# The container filesystem is ephemeral unless VAULT_PATH is on a mounted
# volume. If it's not, every restart re-clones the vault fresh — fine for
# the common case, but a commit that landed locally and then failed to push
# (see vault.py's offline handling) would be lost on the next restart
# instead of retried. Mount a volume at VAULT_PATH in Railway to avoid that.
set -eu

: "${VAULT_REPO_URL:?VAULT_REPO_URL is required (e.g. git@github.com:you/your-vault.git)}"
: "${VAULT_PATH:?VAULT_PATH is required (e.g. /data/vault)}"

mkdir -p ~/.ssh
chmod 700 ~/.ssh

if [ -n "${VAULT_DEPLOY_KEY:-}" ]; then
    printf '%s\n' "$VAULT_DEPLOY_KEY" >~/.ssh/id_ed25519
    chmod 600 ~/.ssh/id_ed25519
else
    echo "WARNING: VAULT_DEPLOY_KEY is not set — git push to the vault will fail." >&2
fi

# Railway (like many PaaS platforms) blocks outbound traffic on port 22.
# GitHub's SSH service is also reachable over port 443 via ssh.github.com,
# specifically for cases like this — route github.com through it instead.
cat >~/.ssh/config <<'EOF'
Host github.com
    HostName ssh.github.com
    Port 443
    User git
EOF
chmod 600 ~/.ssh/config

# Personal single-repo deploy key over SSH; skipping host-key verification
# is the standard tradeoff here rather than fighting known_hosts formatting.
export GIT_SSH_COMMAND="ssh -F ~/.ssh/config -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i ~/.ssh/id_ed25519"

git config --global user.name "${GIT_AUTHOR_NAME:-daylog-bot}"
git config --global user.email "${GIT_AUTHOR_EMAIL:-daylog-bot@users.noreply.github.com}"
git config --global --add safe.directory "$VAULT_PATH"

# A missing .git dir isn't the only broken state worth checking for: two
# container instances briefly overlapping on the same volume (e.g. during a
# redeploy) can leave the repo with a detached HEAD or stale ref locks.
# Treat anything that isn't "on a real branch with a resolvable HEAD" as
# broken and re-clone rather than trying to repair it in place.
vault_is_healthy() {
    git -C "$VAULT_PATH" rev-parse --verify -q HEAD >/dev/null 2>&1 \
        && git -C "$VAULT_PATH" symbolic-ref -q HEAD >/dev/null 2>&1
}

if [ -d "$VAULT_PATH/.git" ] && vault_is_healthy; then
    echo "Vault already present at $VAULT_PATH — flushing any commits stranded by a previous restart"
    if ! git -C "$VAULT_PATH" push; then
        # A rejected push here usually isn't network flakiness — it means the
        # remote moved ahead of what this clone last knew (e.g. a commit
        # pushed from a different clone of the same repo), so a bare retry
        # would fail identically forever. Reconcile once via rebase.
        echo "push rejected — fetching and rebasing onto the remote before retrying"
        if git -C "$VAULT_PATH" fetch origin && git -C "$VAULT_PATH" rebase origin/main; then
            git -C "$VAULT_PATH" push || echo "push still failed after reconciling, will retry on next journal write"
        else
            echo "reconciliation failed — aborting rebase, commits stay local, will retry on next journal write"
            git -C "$VAULT_PATH" rebase --abort 2>/dev/null || true
        fi
    fi
else
    if [ -d "$VAULT_PATH" ]; then
        echo "Vault at $VAULT_PATH is missing or in a broken git state — re-cloning fresh"
    else
        echo "Cloning $VAULT_REPO_URL into $VAULT_PATH"
    fi
    rm -rf "$VAULT_PATH"
    git clone "$VAULT_REPO_URL" "$VAULT_PATH"
fi

exec uv run python -m daylog.bot
