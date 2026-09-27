"""What each Claude call costs, a running monthly total, and the budget caps.

Every module that calls the API passes the response's `usage` to
`record()`. Costs accumulate in memory and are flushed to the vault's
usage.yaml (one commit) by the jobs that already write — reconcile,
research, the brief — so there's no commit per call. A restart loses at
most the unflushed extraction costs of the day, which are cents.

Two caps, both in USD, both from the environment:
- MONTHLY_BUDGET_USD (default 40): optional work (research, planning,
  chat web searches) stops for the month when reached. Journaling never
  stops — capturing the day is the one thing that must always work.
- RESEARCH_RUN_BUDGET_USD (default 1.50): a single research run wraps up
  and reports what it has once it's spent this much.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date
from typing import Any

from daylog.vault import Vault

logger = logging.getLogger(__name__)

USAGE_FILE = "usage.yaml"

# USD per million tokens: (input, output). Cache writes bill at 1.25x input,
# cache reads at 0.1x input.
PRICES: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
}
WEB_SEARCH_USD = 0.01  # $10 per 1,000 searches

# The main model for everything the bot does. Sonnet 5 is the default the
# existing extraction and brief were tuned on; research and chat use it too.
MAIN_MODEL = "claude-sonnet-5"


def create(client: Any, **kwargs: Any) -> Any:
    """messages.create, but streamed when the client supports it.

    Long requests (web search, high effort) can sit for a minute or more
    with no bytes on the wire, and an idle-connection timeout somewhere
    between here and the API then drops them ("Server disconnected without
    sending a response"). Streaming keeps data flowing; the final Message
    is the same object create() would have returned. Test fakes that only
    implement create() still work.
    """
    messages = client.messages
    if hasattr(messages, "stream"):
        with messages.stream(**kwargs) as stream:
            return stream.get_final_message()
    return messages.create(**kwargs)


def monthly_budget() -> float:
    return float(os.environ.get("MONTHLY_BUDGET_USD", "40"))


def research_run_budget() -> float:
    return float(os.environ.get("RESEARCH_RUN_BUDGET_USD", "1.50"))


def cost_of(model: str, usage: Any) -> float:
    """USD for one response's `usage` block, including server tool calls."""
    price_in, price_out = PRICES.get(model, PRICES[MAIN_MODEL])
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    tokens_usd = (
        input_tokens * price_in
        + cache_write * price_in * 1.25
        + cache_read * price_in * 0.1
        + output_tokens * price_out
    ) / 1_000_000
    server = getattr(usage, "server_tool_use", None)
    searches = (getattr(server, "web_search_requests", 0) or 0) if server else 0
    return tokens_usd + searches * WEB_SEARCH_USD


@dataclass
class _Pending:
    month: str
    kind: str
    usd: float


_pending: list[_Pending] = []


def _month(on: date | None = None) -> str:
    return (on or date.today()).strftime("%Y-%m")


def record(kind: str, model: str, usage: Any) -> float:
    """Account for one API response. Returns its cost."""
    usd = cost_of(model, usage)
    _pending.append(_Pending(month=_month(), kind=kind, usd=usd))
    logger.info("llm cost: %s %s $%.4f", kind, model, usd)
    return usd


def pending_total(month: str | None = None) -> float:
    m = month or _month()
    return sum(p.usd for p in _pending if p.month == m)


def month_spend(vault: Vault, month: str | None = None) -> float:
    """Flushed plus not-yet-flushed spend for the month."""
    m = month or _month()
    saved = vault.read_yaml(USAGE_FILE, {}).get(m) or {}
    return float(saved.get("total", 0.0)) + pending_total(m)


def can_spend(vault: Vault, estimate: float = 0.0) -> bool:
    return month_spend(vault) + estimate <= monthly_budget()


def flush(vault: Vault) -> None:
    """Fold pending costs into usage.yaml with one commit. Never raises."""
    if not _pending:
        return
    batch = list(_pending)
    try:
        usage = vault.read_yaml(USAGE_FILE, {})
        for item in batch:
            month = usage.get(item.month)
            if month is None:
                month = {}
                usage[item.month] = month
            month[item.kind] = round(float(month.get(item.kind, 0.0)) + item.usd, 4)
            month["total"] = round(float(month.get("total", 0.0)) + item.usd, 4)
        vault.write_yaml(USAGE_FILE, usage, "usage: flush API costs")
    except Exception:
        logger.exception("usage flush failed — costs kept in memory for the next flush")
        return
    del _pending[: len(batch)]


def format_usage(vault: Vault) -> str:
    m = _month()
    saved = dict(vault.read_yaml(USAGE_FILE, {}).get(m) or {})
    for p in _pending:
        if p.month == m:
            saved[p.kind] = float(saved.get(p.kind, 0.0)) + p.usd
            saved["total"] = float(saved.get("total", 0.0)) + p.usd
    total = float(saved.pop("total", 0.0))
    lines = [f"API spend {m}: ${total:.2f} of ${monthly_budget():.0f} budget"]
    for kind, usd in sorted(saved.items(), key=lambda kv: -float(kv[1])):
        lines.append(f"- {kind}: ${float(usd):.2f}")
    return "\n".join(lines)
