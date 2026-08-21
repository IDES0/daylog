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

# Personal single-repo deploy key over SSH; skipping host-key verification
# is the standard tradeoff here rather than fighting known_hosts formatting.
export GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i ~/.ssh/id_ed25519"

git config --global user.name "${GIT_AUTHOR_NAME:-daylog-bot}"
git config --global user.email "${GIT_AUTHOR_EMAIL:-daylog-bot@users.noreply.github.com}"
git config --global --add safe.directory "$VAULT_PATH"

if [ ! -d "$VAULT_PATH/.git" ]; then
    echo "Cloning $VAULT_REPO_URL into $VAULT_PATH"
    rm -rf "$VAULT_PATH"
    git clone "$VAULT_REPO_URL" "$VAULT_PATH"
else
    echo "Vault already present at $VAULT_PATH — flushing any commits stranded by a previous restart"
    git -C "$VAULT_PATH" push || echo "push failed, will retry on next journal write"
fi

exec uv run python -m daylog.bot
