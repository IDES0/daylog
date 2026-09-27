"""Shared Telegram plumbing: auth, the vault handle, timezone, message chunking.

Every handler module imports these instead of redefining them, so the one
auth check (TELEGRAM_ALLOWED_USER_ID) lives in exactly one place.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Update

from daylog.vault import Vault

logger = logging.getLogger(__name__)


def allowed_user_id() -> int:
    return int(os.environ["TELEGRAM_ALLOWED_USER_ID"])


def vault() -> Vault:
    """The vault, caught up with the remote (throttled — see Vault.sync)."""
    v = Vault(Path(os.environ.get("VAULT_PATH", "../daylog-vault")))
    v.sync()
    return v


def tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("TZ", "UTC"))


def is_authorized(update: Update) -> bool:
    user = update.effective_user
    if user is None or user.id != allowed_user_id():
        logger.warning("rejected update from unauthorized user id=%s", user.id if user else None)
        return False
    return True


def chunks(text: str, limit: int = 4000) -> list[str]:
    """Split on line boundaries under Telegram's 4096-char message limit."""
    out: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit and current:
            out.append(current)
            current = ""
        current += line
    if current:
        out.append(current)
    return out or [""]
