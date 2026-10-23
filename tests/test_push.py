from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from daylog import push
from daylog.vault import Vault

ON = date(2026, 10, 24)


def _sub(n: int) -> dict[str, Any]:
    return {
        "endpoint": f"https://web.push.apple.com/{n}",
        "keys": {"p256dh": f"p{n}", "auth": f"a{n}"},
    }


def test_lock_screen_is_the_briefs_opening_in_plain_text() -> None:
    brief = (
        "## Today\n\n- **Austin**, 62°F and *clear*.\n"
        "- Boat foil at 2 if the wind holds.\n\nMore below."
    )
    assert push.lock_screen(brief) == (
        "Today · Austin, 62°F and clear. · Boat foil at 2 if the wind holds. · More below."
    )
    long = push.lock_screen("word " * 100)
    assert len(long) <= push.LOCK_SCREEN_CHARS and long.endswith("…")


def test_a_device_subscribes_once_and_bad_ones_are_refused(vault: Vault) -> None:
    assert push.add(vault, _sub(1), ON) == 1
    assert push.add(vault, _sub(1), ON) == 1  # the same phone again: replaced, not doubled
    assert push.add(vault, _sub(2), ON) == 2
    with pytest.raises(ValueError):
        push.add(vault, {"endpoint": "http://evil", "keys": {}}, ON)


def test_send_reaches_devices_and_forgets_gone_ones(
    vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pywebpush import WebPushException

    monkeypatch.setenv("VAPID_PRIVATE_KEY", "k")
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "p")
    push.add(vault, _sub(1), ON)
    push.add(vault, _sub(2), ON)
    sent: list[str] = []

    class Gone:
        status_code = 410

    def fake_webpush(sub: dict[str, Any], data: str, **kw: Any) -> None:
        if sub["endpoint"].endswith("/2"):
            raise WebPushException("gone", response=Gone())
        sent.append(data)

    monkeypatch.setattr("pywebpush.webpush", fake_webpush)
    assert push.send(vault, "☀️ Brief", "Austin, clear") == 1
    assert "Brief" in sent[0] and "Austin, clear" in sent[0]
    assert [d["endpoint"] for d in push.devices(vault)] == ["https://web.push.apple.com/1"]


def test_send_does_nothing_until_configured(vault: Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    push.add(vault, _sub(1), ON)
    assert push.send(vault, "t", "b") == 0
