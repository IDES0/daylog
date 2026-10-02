"""A read-only status page: where you are, what's next, goals, the last week.

One self-contained HTML page built from the vault on each request — no
scripts, no forms, nothing stored. Served by calendar_server at a secret
path, next to the calendar feed. Telegram stays the only place to change
anything.
"""

from __future__ import annotations

from datetime import date, timedelta
from html import escape
from typing import Any

from daylog import brief, export, review, trail
from daylog.vault import JournalEntry, Vault

RECENT_DAYS = 7
MAX_STAYS = 15
OPEN = ("candidate", "planned")

_CSS = """
:root { --bg:#fbfaf7; --fg:#1c1b19; --muted:#6b6862; --line:#e4e1da; --bar:#2f6f62; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#161614; --fg:#ecebe7; --muted:#9a968e; --line:#2e2d2a; --bar:#6fb8a8; }
}
body { margin:0; background:var(--bg); color:var(--fg);
  font:16px/1.5 system-ui,-apple-system,sans-serif; }
main { max-width:640px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:22px; margin:0 0 2px; }
h2 { font-size:13px; text-transform:uppercase; letter-spacing:.06em;
  color:var(--muted); margin:32px 0 8px; }
p { margin:0; } .muted { color:var(--muted); font-size:14px; }
ul { list-style:none; margin:0; padding:0; }
li { padding:10px 0; border-top:1px solid var(--line); }
.row { display:flex; justify-content:space-between; gap:12px; }
.row span:last-child { color:var(--muted); white-space:nowrap; font-variant-numeric:tabular-nums; }
.track { height:6px; background:var(--line); border-radius:3px; margin-top:6px; }
.sub { color:var(--muted); font-size:14px; margin-top:2px; }
.fill { height:6px; background:var(--bar); border-radius:3px; }
"""


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _short(d: date) -> str:
    return f"{d.strftime('%b')} {d.day}"


def _when(entry: Any) -> tuple[date | None, str]:
    """(sort date, label) for an itinerary entry or goal."""
    deadline = _as_date(entry.get("deadline")) if entry.get("deadline") else None
    if deadline:
        return deadline, f"by {_short(deadline)}"
    window = entry.get("target_window")
    if isinstance(window, list) and len(window) == 2:
        start, end = _as_date(window[0]), _as_date(window[1])
        if start and end:
            label = _short(start) if start == end else f"{_short(start)} – {_short(end)}"
            return start, label
    return None, ""


def upcoming(itinerary: Any, today: date, limit: int = 8) -> list[tuple[str, str]]:
    """(place, when) for open, dated legs that haven't ended, earliest first."""
    rows = []
    for e in itinerary or []:
        if not isinstance(e, dict) or e.get("status") not in OPEN:
            continue
        start, label = _when(e)
        window = e.get("target_window")
        end = _as_date(window[1]) if isinstance(window, list) and len(window) == 2 else start
        if start is None or (end or start) < today:
            continue
        rows.append((start, str(e.get("place", e.get("id", "?"))), label))
    rows.sort(key=lambda r: r[0])
    return [(place, label) for _, place, label in rows[:limit]]


def goal_rows(goals: Any) -> list[dict[str, Any]]:
    rows = []
    for g in goals or []:
        if not isinstance(g, dict) or g.get("status") != "active":
            continue
        progress, target = _number(g.get("progress")), _number(g.get("target"))
        value, percent = "", None
        if progress is not None and target:
            value, percent = f"{progress:g} / {target:g}", min(100, round(100 * progress / target))
        elif progress is not None:
            value = f"{progress:g}"
        rows.append(
            {
                "title": str(g.get("title", g.get("id", "?"))),
                "value": value,
                "metric": str(g.get("metric", "")).replace("_", " ") if value else "",
                "percent": percent,
                "when": _when(g)[1],
            }
        )
    return rows


def stay_rows(
    location_data: Any, entries: dict[date, JournalEntry], today: date
) -> list[dict[str, str]]:
    """Each stay, newest first: place, dates, and what the journal says was done there."""
    rows = []
    for stay in reversed(trail.stays(location_data)):
        end = stay.end or today
        # A move day belongs to the place arrived at, so it isn't counted twice.
        days = {
            d: e
            for d, e in entries.items()
            if stay.start <= d and (d < stay.end if stay.end else d <= today)
        }
        stats = review.week_stats(days)
        did = [f"{kind.replace('_', ' ')} {h:g} h" for kind, h in stats["activity_hours"].items()]
        if not did:
            did = [kind.replace("_", " ") for kind in stats["activity_counts"]]
        nights = (end - stay.start).days
        span = _short(stay.start) if nights == 0 else f"{_short(stay.start)} – {_short(end)}"
        rows.append(
            {
                "place": stay.place,
                "when": span + ("" if stay.end else " · now"),
                "did": " · ".join(did[:4]) or (stay.notes or ""),
            }
        )
    return rows[:MAX_STAYS]


def _item(left: str, right: str, extra: str = "") -> str:
    return (
        f'<li><div class="row"><span>{escape(left)}</span>'
        f"<span>{escape(right)}</span></div>{extra}</li>"
    )


def _section(title: str, items: list[str], empty: str) -> str:
    body = f"<ul>{''.join(items)}</ul>" if items else f'<p class="muted">{escape(empty)}</p>'
    return f"<h2>{escape(title)}</h2>{body}"


def render(vault: Vault, today: date) -> str:
    current = brief.current_location(vault.read_location() or [])
    place = str(current.get("place", "")) if current else ""
    since = _as_date(current.get("from")) if current else None
    here = f"since {_short(since)} · day {(today - since).days + 1}" if since else ""

    legs = [_item(p, when) for p, when in upcoming(vault.read_itinerary(), today)]

    goals = []
    for g in goal_rows(vault.read_goals()):
        bar = (
            f'<div class="track"><div class="fill" style="width:{g["percent"]}%"></div></div>'
            if g["percent"] is not None
            else ""
        )
        right = " ".join(x for x in (g["value"], g["metric"]) if x) or g["when"]
        goals.append(_item(g["title"], right, bar))

    dates = vault.list_journal_dates()
    everything = vault.read_journal_range(min(dates), today) if dates else {}
    been = [
        _item(
            r["place"], r["when"], f'<div class="sub">{escape(r["did"])}</div>' if r["did"] else ""
        )
        for r in stay_rows(vault.read_location() or [], everything, today)
    ]

    week_start = today - timedelta(days=RECENT_DAYS - 1)
    entries = {d: e for d, e in everything.items() if d >= week_start}
    stats = review.week_stats(entries)
    week = [_item("Days logged", f"{stats['days_logged']} of {RECENT_DAYS}")]
    week += [
        _item(kind.replace("_", " "), f"{h:g} h") for kind, h in stats["activity_hours"].items()
    ]
    for label, key in (("Energy", "avg_energy"), ("Mood", "avg_mood"), ("Focus", "avg_focus")):
        if stats[key] is not None:
            week.append(_item(f"{label} (1–5)", f"{stats[key]:g}"))
    if stats["avg_kcal_per_day"] is not None:
        food = f"~{stats['avg_kcal_per_day']:,} kcal"
        if stats["avg_protein_g_per_day"] is not None:
            food += f", {stats['avg_protein_g_per_day']} g protein"
        week.append(_item("Food per logged day (rough)", food))
    meals = sum(r["meals"] for r in export.day_rows(entries))
    week.append(_item("Meals mentioned", str(meals)))

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>daylog</title><style>{_CSS}</style></head><body><main>"
        f"<h1>{escape(place or 'Location unknown')}</h1>"
        f'<p class="muted">{escape(here)}</p>'
        + _section("Next", legs, "Nothing dated on the itinerary.")
        + _section("Goals", goals, "No active goals.")
        + _section(f"Last {RECENT_DAYS} days", week, "Nothing logged.")
        + _section("Where I've been", been, "No locations logged.")
        + f'<p class="muted" style="margin-top:32px">Read-only · built {today.isoformat()} '
        "from the vault · change things in Telegram</p></main></body></html>"
    )
