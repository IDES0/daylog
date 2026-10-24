"""Notifications on the user's own phone, through the installed web app.

Everything the app does on its own (the morning brief, decisions waiting, the
weekly review and plan, new places to confirm, a corrected goal total) arrives
on the lock screen as one short notification per event; a tap
opens the app where it is. Web Push (iOS 16.4+ for a Home Screen app): the app
subscribes once, the subscription is kept in the vault (push.yaml), and the
server signs each push with its VAPID key (VAPID_PRIVATE_KEY /
VAPID_PUBLIC_KEY, see README). Pushes only ever go to the user's own devices —
the app never contacts anyone but the user.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date
from typing import Any

from daylog.vault import Vault

logger = logging.getLogger(__name__)

FILE = "push.yaml"
# What fits on a lock screen: about four lines.
LOCK_SCREEN_CHARS = 180


def configured() -> bool:
    return bool(os.environ.get("VAPID_PRIVATE_KEY") and os.environ.get("VAPID_PUBLIC_KEY"))


def public_key() -> str | None:
    return os.environ.get("VAPID_PUBLIC_KEY") or None


def devices(v: Vault) -> list[dict[str, Any]]:
    data = v.read_yaml(FILE, {}) or {}
    return [dict(s) for s in data.get("subscriptions") or [] if isinstance(s, dict)]


def add(v: Vault, subscription: dict[str, Any], on: date) -> int:
    """Keep a device's subscription (replacing the same endpoint); returns the count."""
    endpoint = str(subscription.get("endpoint") or "")
    keys = subscription.get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("not a push subscription")
    subs = [s for s in devices(v) if s.get("endpoint") != endpoint]
    subs.append(
        {
            "endpoint": endpoint,
            "keys": {"p256dh": str(keys["p256dh"]), "auth": str(keys["auth"])},
            "added": on.isoformat(),
        }
    )
    v.write_yaml(FILE, {"subscriptions": subs}, "push: a device subscribed")
    return len(subs)


def send(v: Vault, title: str, body: str, url: str = "/#/home") -> int:
    """Push to every subscribed device; forget the ones the push service says are gone.
    Returns how many got it. Never raises: a notification is never worth a failure."""
    if not configured():
        return 0
    from pywebpush import WebPushException, webpush  # type: ignore[import-untyped]

    payload = json.dumps({"title": title, "body": body, "url": url})
    claims = {"sub": os.environ.get("VAPID_SUBJECT", "mailto:daylog@example.com")}
    sent, gone = 0, []
    for sub in devices(v):
        try:
            webpush(
                sub,
                payload,
                vapid_private_key=os.environ["VAPID_PRIVATE_KEY"],
                vapid_claims=dict(claims),
                ttl=6 * 3600,
                timeout=15,
            )
            sent += 1
        except WebPushException as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (404, 410):
                gone.append(sub["endpoint"])
            else:
                logger.warning("push failed (%s)", status)
        except Exception:
            logger.exception("push failed")
    if gone:
        keep = [s for s in devices(v) if s.get("endpoint") not in gone]
        v.write_yaml(FILE, {"subscriptions": keep}, "push: forget devices that unsubscribed")
    return sent


def lock_screen(text: str, limit: int = LOCK_SCREEN_CHARS) -> str:
    """The brief's opening as plain text, cut to what a lock screen shows."""
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"^\s*(#+|[-*•]|\d+[.)])\s*", "", raw)
        line = re.sub(r"[*_`]+", "", line).strip()
        if line:
            lines.append(line)
        if sum(len(x) + 1 for x in lines) >= limit:
            break
    out = " · ".join(lines)
    return out if len(out) <= limit else out[: limit - 1].rstrip(" ·,;") + "…"
