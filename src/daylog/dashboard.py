"""The status site: where you are, where you're headed, goals, the last week.

One page built from the vault on each request, in two views. The guest view
is what anyone at the address sees: the status summary plus a map of where
the itinerary goes next, styled like the travel map on the personal site
(which shows where I've been). The private view, behind the password line
(see web.py), adds the journal and the rest. Both are read-only: nothing is
stored, and Telegram stays the only place to change anything.

The only script is the map (d3 from a CDN) and the service-worker hook; the
lists render without it.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from html import escape
from typing import Any

from daylog import brief, export, health, review, trail
from daylog.places import PlaceIndex
from daylog.vault import JournalEntry, Vault

RECENT_DAYS = 7
MAX_STAYS = 15
OPEN = ("candidate", "planned")
MAX_MILESTONES = 10
MAX_SUMMARY_CHARS = 400
# Logged, but not what a stay was about.
NOT_DOING = ("travel", "errand", "meal")

# Matches johnalexborkowski.com/travel.html, so the two maps read as a pair.
_CSS = """
:root { --bg:#fbfaf7; --surface:#fff; --text:#1b1d21; --muted:#5d6470; --faint:#8a909a;
  --line:#e6e3dc; --accent:#1f4f8a; --land:#e9e6df; --planned:#9dbbe0; --idea:#dbe5f1;
  --dot:#1f4f8a; --here:#c2410c; --bar:#1f4f8a;
  --serif:"Newsreader",Georgia,serif; --sans:"Inter",system-ui,-apple-system,sans-serif;
  --mono:"JetBrains Mono",ui-monospace,Menlo,monospace; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#111317; --surface:#171a1f; --text:#e7e9ec; --muted:#a3a9b3; --faint:#737a85;
    --line:#262a31; --accent:#7fb0ec; --land:#22262d; --planned:#2f5583; --idea:#263a52;
    --dot:#a9cdf7; --here:#fb923c; --bar:#7fb0ec; }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:16px/1.6 var(--sans);
  -webkit-font-smoothing:antialiased;
  padding:env(safe-area-inset-top) env(safe-area-inset-right) 0 env(safe-area-inset-left); }
.wrap { max-width:720px; margin:0 auto; padding:0 16px; }
header { padding:48px 0 8px; }
.eyebrow { font:500 13px/1 var(--mono); color:var(--accent); letter-spacing:.02em;
  margin-bottom:16px; }
h1 { font:500 clamp(34px,7vw,48px)/1.05 var(--serif); letter-spacing:-.02em; margin:0 0 10px; }
.lede { color:var(--muted); margin:0; }
.stats { display:flex; gap:28px; margin-top:24px; flex-wrap:wrap; }
.stat b { display:block; font:500 28px/1.1 var(--serif); }
.stat span { font:12px/1 var(--mono); color:var(--faint); text-transform:uppercase;
  letter-spacing:.06em; }
h2 { font:500 13px/1 var(--mono); text-transform:uppercase; letter-spacing:.08em;
  color:var(--faint); margin:40px 0 12px; }
p { margin:0; } .muted { color:var(--muted); font-size:14px; }
ul { list-style:none; margin:0; padding:0; }
li { padding:10px 0; border-top:1px solid var(--line); }
.row { display:flex; justify-content:space-between; gap:12px; }
.row span:last-child { font:13px/1.8 var(--mono); color:var(--faint); white-space:nowrap; }
.sub { color:var(--muted); font-size:14px; margin-top:2px; }
.track { height:6px; background:var(--line); border-radius:3px; margin-top:6px; }
.fill { height:6px; background:var(--bar); border-radius:3px; }
.legend { display:flex; gap:16px; font-size:13px; color:var(--muted); margin-bottom:8px;
  flex-wrap:wrap; }
.legend i { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px;
  vertical-align:-1px; }
.map-card { position:relative; border:1px solid var(--line); border-radius:14px;
  background:var(--surface); overflow:hidden; }
#map { display:block; width:100%; height:auto; cursor:grab; touch-action:none; }
.country { fill:var(--land); stroke:var(--surface); stroke-width:.5; }
.country.planned { fill:var(--planned); } .country.idea { fill:var(--idea); }
.route { fill:none; stroke:var(--dot); stroke-width:1.4; stroke-dasharray:4 3; opacity:.8; }
.pin { fill:var(--dot); stroke:var(--surface); stroke-width:1.2; cursor:pointer; }
.pin.idea { fill:var(--surface); stroke:var(--dot); }
.pin.here { fill:var(--here); }
.tip { position:absolute; pointer-events:none; opacity:0; background:var(--text);
  color:var(--bg); font-size:13px; line-height:1.35; padding:7px 10px; border-radius:7px;
  transform:translate(-50%,calc(-100% - 12px)); white-space:nowrap; transition:opacity .12s; }
.tip small { display:block; opacity:.7; font:11px/1.4 var(--mono); }
.map-tools { position:absolute; right:10px; bottom:10px; display:flex; gap:6px; }
.map-tools button { width:32px; height:32px; border-radius:8px; border:1px solid var(--line);
  background:var(--surface); color:var(--text); font:500 16px/1 var(--sans); cursor:pointer; }
footer { margin-top:48px; padding:24px 0 calc(40px + env(safe-area-inset-bottom));
  border-top:1px solid var(--line); color:var(--faint); font-size:13px;
  display:flex; justify-content:space-between; align-items:center; gap:16px; flex-wrap:wrap; }
footer form { margin:0; }
.key input { width:160px; padding:4px 0; border:0; border-bottom:1px solid var(--line);
  background:transparent; color:var(--text); font:13px var(--mono); outline:none; }
.key input:focus { border-color:var(--accent); }
.key input::placeholder { color:var(--faint); }
footer button { border:0; background:none; color:var(--faint); font:13px var(--sans);
  padding:0; cursor:pointer; text-decoration:underline; text-underline-offset:3px; }
"""

_FONTS = (
    "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600"
    "&family=JetBrains+Mono:wght@400;500&family=Newsreader:opsz,wght@6..72,500&display=swap"
)
_D3 = "https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js"
_TOPOJSON = "https://cdn.jsdelivr.net/npm/topojson-client@3.1.0/dist/topojson-client.min.js"
_WORLD = "https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/countries-110m.json"

# Draws DATA (see future_map) the way travel.html draws the past: countries
# shaded, pins, and a dashed route from here through the dated legs in order.
_MAP_JS = """
const DATA = JSON.parse(document.getElementById("mapdata").textContent);
const NAT = { "United States": "United States of America", "USA": "United States of America" };
const nat = c => NAT[c] || c;
const W = 1000, H = 520;
const svg = d3.select("#map").attr("viewBox", `0 0 ${W} ${H}`);
const g = svg.append("g");
const projection = d3.geoNaturalEarth1().scale(190).translate([W / 2, H / 2 + 20]);
const path = d3.geoPath(projection);
const tip = document.getElementById("tip"), card = document.getElementById("mapCard");
const planned = new Set(DATA.stops.filter(s => s.planned && s.country).map(s => nat(s.country)));
const ideas = new Set(DATA.stops.filter(s => !s.planned && s.country).map(s => nat(s.country)));
const here = DATA.here && DATA.here.lat != null ? [{ ...DATA.here, here: true }] : [];
const pins = DATA.stops.filter(s => s.lat != null);
const route = [...here, ...pins.filter(s => s.planned)];

const zoom = d3.zoom().scaleExtent([1, 24]).translateExtent([[0, 0], [W, H]])
  .on("zoom", e => {
    const k = e.transform.k;
    g.attr("transform", e.transform);
    g.selectAll(".pin").attr("r", 5 / Math.sqrt(k)).attr("stroke-width", 1.4 / k);
    g.selectAll(".country").attr("stroke-width", .5 / k);
    g.selectAll(".route").attr("stroke-width", 1.4 / k)
      .attr("stroke-dasharray", `${4 / k} ${3 / k}`);
  });
svg.call(zoom);

// Centre the world where the points sit tightest, so a trip across the
// Pacific stays in one piece instead of wrapping off both edges.
const lons = [...here, ...pins].map(p => p.lon);
let centre = 0, best = Infinity;
for (let c = -180; c < 180; c += 10) {
  const span = d3.extent(lons, l => ((l - c + 540) % 360) - 180);
  if (lons.length && span[1] - span[0] < best) { best = span[1] - span[0]; centre = c; }
}
projection.rotate([-centre, 0]);

function showTip(evt, p) {
  const r = card.getBoundingClientRect();
  tip.innerHTML = "";
  tip.append(p.name);
  const small = document.createElement("small");
  small.textContent = p.here ? "Now" : (p.when || "Someday");
  tip.append(small);
  tip.style.left = (evt.clientX - r.left) + "px";
  tip.style.top = (evt.clientY - r.top) + "px";
  tip.style.opacity = 1;
}
const hideTip = () => tip.style.opacity = 0;

function fit(points) {
  if (!points.length) return;
  const xy = points.map(p => projection([p.lon, p.lat]));
  const [x0, x1] = d3.extent(xy, d => d[0]), [y0, y1] = d3.extent(xy, d => d[1]);
  const k = Math.min(8, 0.8 / Math.max((x1 - x0 + 1) / W, (y1 - y0 + 1) / H));
  svg.call(zoom.transform,
    d3.zoomIdentity.translate(W / 2, H / 2).scale(k).translate(-(x0 + x1) / 2, -(y0 + y1) / 2));
}

d3.json("WORLD_URL").then(world => {
  const countries = topojson.feature(world, world.objects.countries).features;
  g.append("g").selectAll("path").data(countries).join("path")
    .attr("class", d => "country" + (planned.has(d.properties.name) ? " planned"
      : ideas.has(d.properties.name) ? " idea" : ""))
    .attr("d", path);
  g.append("g").selectAll("path").data(d3.pairs(route)).join("path").attr("class", "route")
    .attr("d", ([a, b]) =>
      path({ type: "LineString", coordinates: [[a.lon, a.lat], [b.lon, b.lat]] }));
  g.append("g").selectAll("circle").data([...pins.filter(p => !p.planned),
      ...pins.filter(p => p.planned), ...here]).join("circle")
    .attr("class", p => "pin" + (p.here ? " here" : p.planned ? "" : " idea"))
    .attr("cx", p => projection([p.lon, p.lat])[0])
    .attr("cy", p => projection([p.lon, p.lat])[1]).attr("r", 5)
    .on("pointerenter", showTip).on("pointerleave", hideTip)
    .on("click", (e, p) => { showTip(e, p); setTimeout(hideTip, 2000); });
  fit([...here, ...pins]);
});
const step = f => svg.transition().duration(300).call(zoom.scaleBy, f);
document.getElementById("zoomIn").onclick = () => step(1.6);
document.getElementById("zoomOut").onclick = () => step(1 / 1.6);
document.getElementById("zoomReset").onclick = () => fit([...here, ...pins]);
""".replace("WORLD_URL", _WORLD)


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


def _days(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


def activity_rows(stats: dict[str, Any]) -> list[tuple[str, str]]:
    """(activity, "3 days · 4.5 h") by days done; hours only where some were said."""
    rows = []
    for kind, n in stats["activity_days"].items():
        h = stats["activity_hours"].get(kind)
        rows.append((kind.replace("_", " "), _days(n) + (f" · {h:g} h" if h else "")))
    return rows


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
        did = [
            f"{kind.replace('_', ' ')} {_days(n)}"
            for kind, n in stats["activity_days"].items()
            if kind not in NOT_DOING
        ]
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


def _pin(index: PlaceIndex, place_id: Any) -> dict[str, Any]:
    """Coordinates (own or nearest ancestor's) and country name for a place id."""
    node = index.get(str(place_id)) if place_id else None
    if node is None:
        return {"lat": None, "lon": None, "country": None}
    located = index.with_coordinates([node])
    country = index.country_of(node["id"])
    return {
        "lat": located[0]["lat"] if located else None,
        "lon": located[0]["lon"] if located else None,
        "country": str(country["name"]) if country else None,
    }


def future_map(
    itinerary: Any, index: PlaceIndex, location_data: Any, today: date
) -> dict[str, Any]:
    """Where the map starts and where it goes: the current stay, then every open
    itinerary entry. Dated legs that haven't ended are `planned` and joined by
    the route in date order; undated ones are ideas. Only an entry's place_id
    places it on the map, never its name (a guess is how Kuta moved islands)."""
    current = brief.current_location(location_data or [])
    here = None
    if current:
        node = index.current_place(current)
        here = {"name": str(current.get("place", "")), **_pin(index, node and node["id"])}

    dated: list[tuple[date, dict[str, Any]]] = []
    undated: list[tuple[date, dict[str, Any]]] = []
    for e in itinerary or []:
        if not isinstance(e, dict) or e.get("status") not in OPEN:
            continue
        start, label = _when(e)
        window = e.get("target_window")
        end = _as_date(window[1]) if isinstance(window, list) and len(window) == 2 else start
        if start is not None and (end or start) < today:
            continue
        stop = {
            "name": str(e.get("place", e.get("id", "?"))),
            "when": label,
            "planned": start is not None,
            **_pin(index, e.get("place_id")),
        }
        (dated if start is not None else undated).append((start or today, stop))
    dated.sort(key=lambda r: r[0])
    return {"here": here, "stops": [s for _, s in dated + undated]}


def journal_rows(
    entries: dict[date, JournalEntry], limit: int = RECENT_DAYS
) -> list[tuple[str, str]]:
    """(date label, summary) for the newest logged days."""
    rows = []
    for d in sorted(entries, reverse=True)[:limit]:
        # A day told in several notes has a "### 20:00" heading per note.
        lines = entries[d].summary.splitlines()
        text = " ".join(" ".join(ln for ln in lines if not ln.lstrip().startswith("#")).split())
        if len(text) > MAX_SUMMARY_CHARS:
            text = text[: MAX_SUMMARY_CHARS - 1].rstrip() + "…"
        rows.append((f"{d.strftime('%a')} {_short(d)}", text))
    return rows


def _item(left: str, right: str, extra: str = "") -> str:
    return (
        f'<li><div class="row"><span>{escape(left)}</span>'
        f"<span>{escape(right)}</span></div>{extra}</li>"
    )


def _sub(text: str) -> str:
    return f'<div class="sub">{escape(text)}</div>' if text else ""


def _section(title: str, items: list[str], empty: str) -> str:
    body = f"<ul>{''.join(items)}</ul>" if items else f'<p class="muted">{escape(empty)}</p>'
    return f"<h2>{escape(title)}</h2>{body}"


def _stat(value: int, label: str) -> str:
    return f'<div class="stat"><b>{value}</b><span>{escape(label)}</span></div>'


def _number_text(key: str, value: float) -> str:
    return (
        f"{key.replace('_', ' ')} {value:,.0f}"
        if value >= 100
        else f"{key.replace('_', ' ')} {value:g}"
    )


def _map_section(data: dict[str, Any]) -> str:
    if not data["stops"]:
        return ""
    # `</` can't appear inside a script element, so the JSON can't close it early.
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return (
        "<h2>Where I'm headed</h2>"
        '<div class="legend"><span><i style="background:var(--here)"></i>Now</span>'
        '<span><i style="background:var(--dot)"></i>Planned</span>'
        '<span><i style="border:1.5px solid var(--dot)"></i>Ideas</span></div>'
        '<div class="map-card" id="mapCard">'
        '<svg id="map" role="img" aria-label="World map of where the itinerary goes next"></svg>'
        '<div class="tip" id="tip"></div><div class="map-tools">'
        '<button id="zoomIn" aria-label="Zoom in">+</button>'
        '<button id="zoomOut" aria-label="Zoom out">−</button>'
        '<button id="zoomReset" aria-label="Fit">⟲</button></div></div>'
        f'<script type="application/json" id="mapdata">{payload}</script>'
        f'<script src="{_D3}"></script><script src="{_TOPOJSON}"></script>'
        f"<script>{_MAP_JS}</script>"
    )


def _head(title: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<meta name="robots" content="noindex"><meta name="referrer" content="no-referrer">'
        '<meta name="theme-color" content="#fbfaf7" media="(prefers-color-scheme: light)">'
        '<meta name="theme-color" content="#111317" media="(prefers-color-scheme: dark)">'
        '<link rel="manifest" href="/manifest.webmanifest">'
        '<link rel="icon" href="/icon.svg" type="image/svg+xml">'
        '<link rel="apple-touch-icon" href="/icon-180.png">'
        '<meta name="apple-mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-status-bar-style" content="default">'
        '<meta name="apple-mobile-web-app-title" content="daylog">'
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        f'<link rel="stylesheet" href="{_FONTS}">'
        f"<title>{escape(title)}</title><style>{_CSS}</style></head><body>"
    )


def render(vault: Vault, today: date, *, private: bool = False, login: bool = False) -> str:
    """The page. `private` adds the journal and the rest; `login` shows the
    password line on the guest view (only when a password is configured)."""
    location = vault.read_location() or []
    itinerary = vault.read_itinerary()
    current = brief.current_location(location)
    place = str(current.get("place", "")) if current else ""
    since = _as_date(current.get("from")) if current else None
    here = f"Since {_short(since)} · day {(today - since).days + 1}" if since else ""

    headed = future_map(itinerary, PlaceIndex(vault.read_places()), location, today)
    legs = [_item(p, when) for p, when in upcoming(itinerary, today)]

    raw_goals = [g for g in vault.read_goals() or [] if isinstance(g, dict)]
    slips = {
        str(g.get("title", g.get("id", "?"))): len(g.get("slip_history") or []) for g in raw_goals
    }
    goals = []
    for g in goal_rows(raw_goals):
        bar = (
            f'<div class="track"><div class="fill" style="width:{g["percent"]}%"></div></div>'
            if g["percent"] is not None
            else ""
        )
        right = " ".join(x for x in (g["value"], g["metric"]) if x) or g["when"]
        moved = slips.get(g["title"], 0)
        note = (
            " · ".join(x for x in (g["when"], f"moved {moved}×") if x) if private and moved else ""
        )
        goals.append(_item(g["title"], right, bar + _sub(note)))

    dates = vault.list_journal_dates()
    everything = vault.read_journal_range(min(dates), today) if dates else {}
    been = [
        _item(r["place"], r["when"], _sub(r["did"])) for r in stay_rows(location, everything, today)
    ]

    wins = [
        _item(m["what"], _short(date.fromisoformat(m["date"])))
        for m in reversed(review.week_stats(everything)["milestones"][-MAX_MILESTONES:])
    ]

    week_start = today - timedelta(days=RECENT_DAYS - 1)
    entries = {d: e for d, e in everything.items() if d >= week_start}
    stats = review.week_stats(entries)
    week = [_item("Days logged", f"{stats['days_logged']} of {RECENT_DAYS}")]
    week += [_item(kind, value) for kind, value in activity_rows(stats)]
    for label, key in (("Energy", "avg_energy"), ("Mood", "avg_mood"), ("Focus", "avg_focus")):
        if stats[key] is not None:
            week.append(_item(f"{label} (1–5)", f"{stats[key]:g}"))
    if stats["avg_kcal_per_day"] is not None:
        food = f"~{stats['avg_kcal_per_day']:,} kcal"
        if stats["avg_protein_g_per_day"] is not None:
            food += f", {stats['avg_protein_g_per_day']} g protein"
        week.append(_item("Food per logged day (rough)", food))
    stored = vault.read_yaml(health.FILE, {})
    phone: dict[str, list[float]] = {}
    by_day: list[tuple[date, dict[str, float]]] = []
    for offset in range(RECENT_DAYS):
        day = today - timedelta(days=offset)
        numbers = health.for_day(stored, day)
        if numbers:
            by_day.append((day, numbers))
        for key, value in numbers.items():
            phone.setdefault(key, []).append(value)
    for key, values in phone.items():
        avg = sum(values) / len(values)
        shown = f"{avg:,.0f}" if avg >= 100 else f"{avg:,.1f}"
        week.append(_item(f"{key.replace('_', ' ')} (phone, daily avg)", shown))
    meals = sum(r["meals"] for r in export.day_rows(entries))
    week.append(_item("Meals mentioned", str(meals)))

    countries = {s["country"] for s in headed["stops"] if s["country"]}
    planned_n = sum(1 for s in headed["stops"] if s["planned"])
    header = (
        f'<header><div class="eyebrow">{"Private · " if private else ""}'
        f"{escape(today.strftime('%A'))}, {escape(_short(today))}</div>"
        f"<h1>{escape(place or 'Location unknown')}</h1>"
        f'<p class="lede">{escape(here)}</p><div class="stats">'
        + _stat(planned_n, "Legs planned")
        + _stat(len(headed["stops"]) - planned_n, "Ideas")
        + _stat(len(countries), "Countries ahead")
        + "</div></header>"
    )

    body = header + _map_section(headed)
    body += _section("Next", legs, "Nothing dated on the itinerary.")
    if private:
        journal = [_item(d, "", _sub(s)) for d, s in journal_rows(entries)]
        body += _section("Journal", journal, "Nothing logged.")
    body += _section("Goals", goals, "No active goals.")
    if private:
        ideas = [
            _item(str(e.get("place", e.get("id", "?"))), "", _sub(str(e.get("notes") or "")))
            for e in itinerary or []
            if isinstance(e, dict) and e.get("status") == "candidate"
        ]
        body += _section("Ideas", ideas, "No candidates on the list.")
    body += _section("Milestones", wins, "None logged yet.")
    body += _section(f"Last {RECENT_DAYS} days", week, "Nothing logged.")
    if private and by_day:
        phone_rows = [
            _item(_short(d), "", _sub(" · ".join(_number_text(k, v) for k, v in nums.items())))
            for d, nums in by_day
        ]
        body += _section("Phone, by day", phone_rows, "")
    body += _section("Where I've been", been, "No locations logged.")

    if private:
        door = '<form method="post" action="/logout"><button>Sign out</button></form>'
    elif login:
        door = (
            '<form class="key" method="post" action="/login">'
            '<input type="password" name="password" placeholder="password" aria-label="Password" '
            'autocomplete="current-password" enterkeyhint="go" required></form>'
        )
    else:
        door = ""
    footer = (
        f"<footer><span>Read-only · built {today.isoformat()} from the vault</span>{door}</footer>"
    )
    sw = (
        '<script>if ("serviceWorker" in navigator) '
        'navigator.serviceWorker.register("/sw.js");</script>'
    )
    return (
        _head("daylog" if private else "Alex · status")
        + f'<main class="wrap">{body}{footer}</main>{sw}</body></html>'
    )
