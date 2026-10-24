"""Confirm cards for the web app, held in server memory.

Anything that would change the vault after an inference comes back as a
card, never applied directly. A card carries the callable that applies its
answer (built on the service functions), lives for 24 hours, and is answered
once: it
leaves the store before its answer runs, so a double tap can't apply twice.

Single user, so one store per server; thread-safe because the HTTP server
answers each request on its own thread.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

EXPIRY_S = 24 * 3600

KINDS = (
    "goal_slip",
    "itinerary",
    "correction",
    "chat_proposal",
    "discipline_fix",
    "discipline_add",
    "ripple",
    "plan",
    "place",
    "want",
    "research",
)


@dataclass
class Outcome:
    """What answering a card did: a line to show and any follow-up cards."""

    message: str
    cards: list[Card] = field(default_factory=list)
    ok: bool = True


Apply = Callable[[str], Outcome]


def action(action_id: str, label: str, style: str | None = None) -> dict[str, str]:
    out = {"id": action_id, "label": label}
    if style:
        out["style"] = style
    return out


CONFIRM_CANCEL = [action("confirm", "Confirm", "primary"), action("cancel", "Cancel")]


@dataclass
class Card:
    id: str
    kind: str
    title: str
    detail: str
    actions: list[dict[str, str]]
    apply: Apply
    created: float
    # Cards made from something in the vault (a proposal file) carry its key,
    # so the same thing isn't carded twice.
    key: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "detail": self.detail,
            "actions": self.actions,
        }


class UnknownAction(ValueError):
    pass


class CardStore:
    def __init__(self, expiry_s: float = EXPIRY_S) -> None:
        self._cards: dict[str, Card] = {}
        self._lock = threading.Lock()
        self._expiry = expiry_s

    def add(
        self,
        kind: str,
        title: str,
        apply: Apply,
        *,
        detail: str = "",
        actions: list[dict[str, str]] | None = None,
        key: str | None = None,
        now: float | None = None,
    ) -> Card:
        assert kind in KINDS, kind
        card = Card(
            id="c_" + uuid.uuid4().hex[:16],
            kind=kind,
            title=title,
            detail=detail,
            actions=list(actions or CONFIRM_CANCEL),
            apply=apply,
            created=time.time() if now is None else now,
            key=key,
        )
        with self._lock:
            if key is not None:
                existing = next((c for c in self._cards.values() if c.key == key), None)
                if existing is not None:
                    return existing
            self._cards[card.id] = card
        return card

    def _purge(self, now: float) -> None:
        for card_id in [c.id for c in self._cards.values() if now - c.created > self._expiry]:
            del self._cards[card_id]

    def open(self, now: float | None = None) -> list[Card]:
        """Open cards, oldest first."""
        with self._lock:
            self._purge(time.time() if now is None else now)
            return list(self._cards.values())

    def card_keys(self) -> set[str]:
        with self._lock:
            return {c.key for c in self._cards.values() if c.key}

    def drop_key(self, key: str) -> None:
        with self._lock:
            for card_id in [c.id for c in self._cards.values() if c.key == key]:
                del self._cards[card_id]

    def answer(self, card_id: str, action_id: str, now: float | None = None) -> Outcome | None:
        """Apply the answer; None if the card is unknown or expired. Raises
        UnknownAction (and keeps the card) for an action it doesn't offer."""
        with self._lock:
            self._purge(time.time() if now is None else now)
            card = self._cards.get(card_id)
            if card is None:
                return None
            if action_id not in {a["id"] for a in card.actions}:
                raise UnknownAction(action_id)
            del self._cards[card_id]
        return card.apply(action_id)
