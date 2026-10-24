"""The process-wide handles every part of the app shares: the vault and the timezone."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from daylog.vault import Vault


def vault() -> Vault:
    """The vault, caught up with the remote (throttled — see Vault.sync)."""
    v = Vault(Path(os.environ.get("VAULT_PATH", "../daylog-vault")))
    v.sync()
    return v


def tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("TZ", "UTC"))
