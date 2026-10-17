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
import re
from datetime import date, timedelta
from html import escape
from typing import Any

from daylog import brief, disciplines, export, health, review, timeline, trail
from daylog.places import PlaceIndex
from daylog.vault import JournalEntry, Vault

RECENT_DAYS = 7
MAX_STAYS = 15
OPEN = ("candidate", "planned")
MAX_MILESTONES = 10
# Logged, but not what a stay was about.
NOT_DOING = ("travel", "errand", "meal")

# Matches johnalexborkowski.com/travel.html, so the two maps read as a pair.
_CSS = """
:root { --bg:#fbfaf7; --surface:#fff; --text:#1b1d21; --muted:#5d6470; --faint:#8a909a;
  --line:#e6e3dc; --accent:#1f4f8a; --land:#e9e6df; --planned:#9dbbe0;
  --dot:#1f4f8a; --here:#c2410c; --bar:#1f4f8a;
  --root-1:#2a78d6; --root-2:#eb6834; --root-3:#1baf7a; --root-4:#eda100;
  --serif:"Newsreader",Georgia,serif; --sans:"Inter",system-ui,-apple-system,sans-serif;
  --mono:"JetBrains Mono",ui-monospace,Menlo,monospace; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#111317; --surface:#171a1f; --text:#e7e9ec; --muted:#a3a9b3; --faint:#737a85;
    --line:#262a31; --accent:#7fb0ec; --land:#22262d; --planned:#2f5583;
    --dot:#a9cdf7; --here:#fb923c; --bar:#7fb0ec;
    --root-1:#3987e5; --root-2:#d95926; --root-3:#199e70; --root-4:#c98500; }
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
.country.planned { fill:var(--planned); }
.route { fill:none; stroke:var(--dot); stroke-width:1.4; stroke-dasharray:4 3; opacity:.8; }
.pin { fill:var(--dot); stroke:var(--surface); stroke-width:1.2; cursor:pointer; }
.pin.here { fill:var(--here); }
.tip { position:absolute; pointer-events:none; opacity:0; background:var(--text);
  color:var(--bg); font-size:13px; line-height:1.35; padding:7px 10px; border-radius:7px;
  transform:translate(-50%,calc(-100% - 12px)); white-space:nowrap; transition:opacity .12s; }
.tip small { display:block; opacity:.7; font:11px/1.4 var(--mono); }
.map-tools { position:absolute; right:10px; bottom:10px; display:flex; gap:6px; }
.map-tools button { width:32px; height:32px; border-radius:8px; border:1px solid var(--line);
  background:var(--surface); color:var(--text); font:500 16px/1 var(--sans); cursor:pointer; }
#disc { display:block; width:100%; height:auto; cursor:grab; touch-action:none; }
.dlink { fill:none; stroke-width:1.5; opacity:.45; }
.dlink.hub { stroke:var(--line); opacity:1; }
.dalso { fill:none; stroke-width:1; stroke-dasharray:4 4; opacity:.4; }
.dnode { cursor:pointer; }
.dlabel { font:13px var(--sans); fill:var(--text); paint-order:stroke;
  stroke:var(--surface); stroke-width:3px; stroke-linejoin:round; pointer-events:none; }
.dlabel.locked { fill:var(--faint); }
.dlabel.root { font:500 22px var(--serif); }
.glyph { display:inline-block; width:12px; height:12px; border-radius:50%; margin-right:6px;
  vertical-align:-1px; border:2px solid var(--muted); }
.glyph.full { background:var(--muted); border-color:var(--muted); }
.glyph.ready { border-style:dashed; }
.glyph.locked { border:2px dashed var(--faint); }
.glyph.line { width:18px; height:0; border-radius:0; border:0; border-top:2px dashed var(--muted);
  vertical-align:3px; }
details.list { margin-top:12px; font-size:14px; }
details.list summary { cursor:pointer; color:var(--muted); font:13px var(--mono); }
details.list li { padding:6px 0; }
.cal-nav { display:flex; align-items:center; gap:12px; margin-bottom:10px;
  font:13px var(--mono); color:var(--muted); }
.cal-nav a { color:var(--accent); text-decoration:none; font-size:18px; padding:0 6px; }
.cal { --hour:30px; display:grid; grid-template-columns:24px repeat(7,minmax(0,1fr)); gap:3px; }
.cal-gutter { position:relative; margin-top:36px; height:calc(var(--span) * var(--hour)); }
.cal-gutter span { position:absolute; right:2px; transform:translateY(-50%);
  font:10px var(--mono); color:var(--faint); }
.cal-day { display:flex; flex-direction:column; padding:0; border:0; background:none;
  color:inherit; font:inherit; text-align:left; cursor:pointer; min-width:0; }
.cal-day.empty { cursor:default; }
.cal-head { height:36px; font:11px/1.2 var(--mono); color:var(--faint); text-align:center; }
.cal-head b { display:block; font:500 16px/1.3 var(--serif); color:var(--text); }
.cal-day.today .cal-head b { color:var(--here); }
.cal-body { position:relative; display:block; height:calc(var(--span) * var(--hour));
  background:var(--surface); border:1px solid var(--line); border-radius:8px; overflow:hidden; }
.cal-day:not(.empty):hover .cal-body { border-color:var(--accent); }
.blk { position:absolute; box-sizing:border-box; padding:2px 3px; border-radius:5px;
  border:1px solid transparent; font:10px/1.2 var(--sans); overflow:hidden;
  background:hsl(var(--h) 60% 86%); color:hsl(var(--h) 45% 24%); }
.blk.guess { background:hsl(var(--h) 60% 93%); border:1px dashed hsl(var(--h) 40% 60%); }
@media (prefers-color-scheme: dark) {
  .blk { background:hsl(var(--h) 35% 30%); color:hsl(var(--h) 70% 88%); }
  .blk.guess { background:hsl(var(--h) 30% 20%); border-color:hsl(var(--h) 35% 45%); }
}
.cal-key { margin-top:8px; font-size:12px; }
dialog { max-width:min(560px,calc(100vw - 32px)); max-height:80vh; border:1px solid var(--line);
  border-radius:14px; background:var(--surface); color:var(--text); padding:20px; }
dialog::backdrop { background:rgb(0 0 0 / .45); }
dialog h3 { font:500 22px/1.2 var(--serif); margin:0 0 10px; }
dialog p { margin:0 0 10px; }
dialog .note-time { font:12px var(--mono); color:var(--faint); margin:14px 0 4px; }
dialog .times li { padding:4px 0; border-top:0; font:13px var(--mono); color:var(--muted); }
dialog .times { margin-bottom:12px; }
dialog form { margin:12px 0 0; text-align:right; }
dialog button { border:1px solid var(--line); background:var(--bg); color:var(--text);
  border-radius:8px; padding:6px 14px; font:14px var(--sans); cursor:pointer; }
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

# Opens a day's summary (a <template> beside the calendar) in the dialog.
_CAL_JS = """
const dayDialog = document.getElementById("dayDialog");
const dayBody = document.getElementById("dayBody");
document.querySelectorAll(".cal-day[data-day]").forEach(b => b.addEventListener("click", () => {
  dayBody.replaceChildren(document.getElementById("day-" + b.dataset.day).content.cloneNode(true));
  dayDialog.showModal();
}));
dayDialog.addEventListener("click", e => { if (e.target === dayDialog) dayDialog.close(); });
"""

# Draws GD (see disciplines.graph_data) as a radial tree: you at the centre,
# Water/Air/Earth/Body around you, each sport under its first parent, and a
# dashed curve to any other parent. Size is all-time sessions; fill and
# stroke are the state. Tap a node for its numbers, a root to zoom into it.
_DISC_JS = """
(() => {
const GD = JSON.parse(document.getElementById("discdata").textContent);
const S = 1000, R = 320, R0 = 90;
const svg = d3.select("#disc").attr("viewBox", `${-S / 2} ${-S / 2} ${S} ${S}`);
const g = svg.append("g");
const tip = document.getElementById("disctip"), card = document.getElementById("discCard");
const byId = new Map(GD.nodes.map(n => [n.id, n]));
const rows = [{ id: "_", parent: null },
  ...GD.nodes.map(n => ({ ...n, parent: n.parent || "_" }))];
let tree;
try { tree = d3.stratify().id(d => d.id).parentId(d => d.parent)(rows); }
catch (e) { card.remove(); return; }
// Every sport on the outer ring; groupings on inner rings by depth, the
// roots on the first one.
d3.cluster().size([2 * Math.PI, R])
  .separation((a, b) => (a.parent === b.parent ? 1 : 1.6))(tree);
const ring = (R - R0) / Math.max(1, tree.height - 1);
tree.each(d => { d.y = d.depth === 0 ? 0 : d.height === 0 ? R : R0 + (d.depth - 1) * ring; });
const at = new Map(); tree.each(d => at.set(d.id, d));
const xy = d => [Math.sin(d.x) * d.y, -Math.cos(d.x) * d.y];
const colour = id => `var(--root-${GD.slots[id] || 1})`;
const radius = n => n.parent === null ? 11
  : n.total ? Math.min(14, 4 + Math.sqrt(n.total) * 1.6) : 4.5;
const filled = n => n.state === "active" || n.state === null;
const dashed = n => n.state === "ready" || n.state === "locked";

g.append("g").selectAll("path").data(tree.links()).join("path")
  .attr("class", l => "dlink" + (l.source.id === "_" ? " hub" : ""))
  .attr("stroke", l => colour(l.target.data.root))
  .attr("d", d3.linkRadial().angle(d => d.x).radius(d => d.y));
const extra = GD.nodes.flatMap(n => n.also.filter(p => at.has(p)).map(p => [n, p]));
g.append("g").selectAll("path").data(extra).join("path").attr("class", "dalso")
  .attr("stroke", ([, p]) => colour(byId.get(p).root))
  .attr("d", ([n, p]) => {
    const [x1, y1] = xy(at.get(n.id)), [x2, y2] = xy(at.get(p));
    return `M${x1},${y1}Q${(x1 + x2) * .3},${(y1 + y2) * .3} ${x2},${y2}`;
  });

const nodes = tree.descendants().filter(d => d.id !== "_");
g.append("circle").attr("r", 5).attr("fill", "var(--text)");
g.append("g").selectAll("circle").data(nodes).join("circle").attr("class", "dnode")
  .attr("cx", d => xy(d)[0]).attr("cy", d => xy(d)[1]).attr("r", d => radius(d.data))
  .attr("fill", d => filled(d.data) ? colour(d.data.root) : "var(--surface)")
  .attr("stroke", d => filled(d.data) ? "var(--surface)"
    : d.data.state === "locked" ? "var(--faint)" : colour(d.data.root))
  .attr("stroke-width", d => filled(d.data) ? 2 : 2.2)
  .attr("stroke-dasharray", d => dashed(d.data) ? "3 2" : null)
  .on("pointerenter", show).on("pointerleave", hide)
  .on("click", (e, d) => { show(e, d); if (d.depth === 1) zoomTo(d); });

g.append("g").selectAll("text").data(nodes).join("text")
  .attr("class", d => "dlabel" + (d.depth === 1 ? " root" : "") +
    (d.data.state === "locked" ? " locked" : ""))
  .attr("dy", "0.32em")
  .attr("transform", d => {
    if (d.height > 0) {
      // Groupings sit inside the ring: label them level, just above the dot.
      const [x, y] = xy(d);
      return `translate(${x},${y - radius(d.data) - (d.depth === 1 ? 14 : 9)})`;
    }
    const flip = d.x >= Math.PI;
    return `rotate(${d.x * 180 / Math.PI - 90}) translate(${d.y},0)` + (flip ? " rotate(180)" : "");
  })
  .attr("x", d => d.height > 0 ? 0 : (d.x < Math.PI ? 1 : -1) * (radius(d.data) + 5))
  .attr("text-anchor", d => d.height > 0 ? "middle" : d.x < Math.PI ? "start" : "end")
  .text(d => d.data.name);

const STATES = { active: "Pursuing", "not started": "Pursuing · nothing logged yet",
  ready: "Want · prerequisites met", locked: "Want · locked" };
function show(evt, d) {
  const n = d.data, r = card.getBoundingClientRect();
  tip.replaceChildren();
  const line = (text, small) => {
    const el = document.createElement(small ? "small" : "div");
    el.textContent = text;
    tip.append(el);
  };
  line(n.name);
  const path = d.ancestors().slice(1, -1).reverse().map(a => a.data.name);
  if (path.length) line(path.join(" › ") + (n.also.length ?
    " · also " + n.also.map(p => byId.get(p).name).join(", ") : ""), true);
  if (n.state) line(STATES[n.state], true);
  const hours = n.recent_hours ? ` · ${n.recent_hours}h` : "";
  line(`${n.recent} in ${GD.days} days${hours} · ${n.total} all-time` +
    (n.last ? ` · last ${n.last}` : ""), true);
  n.goals.forEach(t => line("Goal: " + t, true));
  if (n.needs.length) line("Needs: " + n.needs.join(", "), true);
  tip.style.left = Math.min(Math.max(evt.clientX - r.left, 90), r.width - 90) + "px";
  tip.style.top = (evt.clientY - r.top) + "px";
  tip.style.opacity = 1;
}
function hide() { tip.style.opacity = 0; }

const zoom = d3.zoom().scaleExtent([1, 8]).on("zoom", e => g.attr("transform", e.transform));
svg.call(zoom);
function fit(points) {
  const [x0, x1] = d3.extent(points, p => p[0]), [y0, y1] = d3.extent(points, p => p[1]);
  const pad = 130, k = Math.min(8, S / Math.max(x1 - x0 + 2 * pad, y1 - y0 + 2 * pad));
  svg.transition().duration(400).call(zoom.transform,
    d3.zoomIdentity.scale(k).translate(-(x0 + x1) / 2, -(y0 + y1) / 2));
}
function zoomTo(d) { fit(d.descendants().map(xy)); }
document.getElementById("discIn").onclick = () => svg.transition().call(zoom.scaleBy, 1.6);
document.getElementById("discOut").onclick = () => svg.transition().call(zoom.scaleBy, 1 / 1.6);
document.getElementById("discFit").onclick = () =>
  svg.transition().call(zoom.transform, d3.zoomIdentity);
})();
"""

_FONTS = (
    "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600"
    "&family=JetBrains+Mono:wght@400;500&family=Newsreader:opsz,wght@6..72,500&display=swap"
)
_D3 = "https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js"
_TOPOJSON = "https://cdn.jsdelivr.net/npm/topojson-client@3.1.0/dist/topojson-client.min.js"
_WORLD = "https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/countries-110m.json"

# Draws DATA (see future_map) the way travel.html draws the past: countries
# shaded, pins, and a dashed route from here through the legs ahead in order.
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
const ahead = new Set(DATA.stops.filter(s => s.country).map(s => nat(s.country)));
const here = DATA.here && DATA.here.lat != null ? [{ ...DATA.here, here: true }] : [];
let pins = [];

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
function centre(points) {
  const lons = points.map(p => p.lon);
  let best = Infinity, at = 0;
  for (let c = -180; c < 180; c += 10) {
    const span = d3.extent(lons, l => ((l - c + 540) % 360) - 180);
    if (lons.length && span[1] - span[0] < best) { best = span[1] - span[0]; at = c; }
  }
  projection.rotate([-at, 0]);
}

function showTip(evt, p) {
  const r = card.getBoundingClientRect();
  tip.innerHTML = "";
  tip.append(p.name);
  const small = document.createElement("small");
  small.textContent = p.here ? "Now" : p.when;
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
  const byName = new Map(countries.map(f => [f.properties.name, f]));
  // A stop known only by its country goes at the country's middle.
  pins = DATA.stops.map(s => {
    if (s.lat != null) return s;
    const f = s.country && byName.get(nat(s.country));
    if (!f) return null;
    const [lon, lat] = d3.geoCentroid(f);
    return { ...s, lon, lat };
  }).filter(Boolean);
  centre([...here, ...pins]);
  g.append("g").selectAll("path").data(countries).join("path")
    .attr("class", d => "country" + (ahead.has(d.properties.name) ? " planned" : ""))
    .attr("d", path);
  g.append("g").selectAll("path").data(d3.pairs([...here, ...pins])).join("path")
    .attr("class", "route")
    .attr("d", ([a, b]) =>
      path({ type: "LineString", coordinates: [[a.lon, a.lat], [b.lon, b.lat]] }));
  g.append("g").selectAll("circle").data([...pins, ...here]).join("circle")
    .attr("class", p => "pin" + (p.here ? " here" : ""))
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


def _ahead(entry: Any, today: date) -> tuple[date, str] | None:
    """(start, label) for an open itinerary leg still ahead: a window that
    hasn't begun, or a deadline that hasn't passed. Undated entries are the
    wishlist, and a leg already under way is where you are, not where you're
    headed; neither counts."""
    if not isinstance(entry, dict) or entry.get("status") not in OPEN:
        return None
    start, label = _when(entry)
    if start is None:
        return None
    if entry.get("deadline"):
        return (start, label) if start >= today else None
    return (start, label) if start > today else None


def upcoming(itinerary: Any, today: date, limit: int = 8) -> list[tuple[str, str]]:
    """(place, when) for the legs still ahead, earliest first."""
    rows = []
    for e in itinerary or []:
        ahead = _ahead(e, today)
        if ahead is None:
            continue
        rows.append((ahead[0], str(e.get("place", e.get("id", "?"))), ahead[1]))
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


def _named_place(index: PlaceIndex, text: str) -> str | None:
    """A place id for an itinerary entry without one, where the name can't mean
    two places: the one place whose name (or alias) is the whole text
    ("Austin"), else the one country named as a word in it ("Flight to
    Japan"). Anything looser needs a place_id."""
    wanted = text.strip().lower()
    exact = {
        node["id"]
        for node in index.nodes
        if node.get("id")
        and any(str(name).strip().lower() == wanted for name in index.names_of(node))
    }
    if len(exact) == 1:
        return str(exact.pop())
    found = {
        node["id"]
        for node in index.nodes
        if node.get("kind") == "country"
        for name in index.names_of(node)
        if name and re.search(rf"\b{re.escape(str(name))}\b", text, re.IGNORECASE)
    }
    return found.pop() if len(found) == 1 else None


def _pin(index: PlaceIndex, place_id: Any) -> dict[str, Any]:
    """Coordinates (own or nearest ancestor's) and country name for a place id.
    A country without coordinates is drawn at its middle by the map."""
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
    """Where the map starts and where it goes: the current stay, then each leg
    still ahead (see _ahead), in date order. A leg is placed by its place_id;
    without one, only by an unambiguous name (see _named_place), never by a
    best guess (a guess is how Kuta moved islands)."""
    current = brief.current_location(location_data or [])
    here = None
    if current:
        node = index.current_place(current)
        here = {"name": str(current.get("place", "")), **_pin(index, node and node["id"])}

    stops: list[tuple[date, dict[str, Any]]] = []
    for e in itinerary or []:
        ahead = _ahead(e, today)
        if ahead is None:
            continue
        name = str(e.get("place", e.get("id", "?")))
        place_id = e.get("place_id") or _named_place(index, name)
        stops.append((ahead[0], {"name": name, "when": ahead[1], **_pin(index, place_id)}))
    stops.sort(key=lambda r: r[0])
    return {"here": here, "stops": [s for _, s in stops]}


def _hue(kind: str) -> int:
    """A stable colour per activity type."""
    return sum((i + 1) * ord(c) for i, c in enumerate(kind)) * 47 % 360


def _clock_text(hours: float) -> str:
    h, m = divmod(round(hours * 60), 60)
    return f"{h:02d}:{m:02d}"


def _summary_html(summary: str) -> str:
    """The day's summary as paragraphs, each note's "### 20:00" heading kept as a label."""
    parts = []
    for block in re.split(r"\n\s*\n", summary.strip()):
        text = " ".join(block.split())
        if not text:
            continue
        if text.startswith("#"):
            parts.append(f'<p class="note-time">{escape(text.lstrip("# "))}</p>')
        else:
            parts.append(f"<p>{escape(text)}</p>")
    return "".join(parts)


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _calendar_section(vault: Vault, today: date, week_of: date) -> str:
    """The week (Monday first) as columns on a clock: each activity a block at
    its rough time, guessed ones dashed. Clicking a day opens its summary."""
    monday = week_start(week_of)
    days = [monday + timedelta(days=i) for i in range(7)]
    entries = vault.read_journal_range(monday, days[-1])
    blocks = {d: timeline.day_blocks(e.frontmatter) for d, e in entries.items()}
    placed = [b for day_blocks in blocks.values() for b in day_blocks]
    lo = min([7.0, *(b.start for b in placed)])
    hi = max([22.0, *(b.end for b in placed)])
    lo, hi = float(int(lo)), float(min(24, -int(-hi)))

    gutter = "".join(
        f'<span style="top:calc({h - lo} * var(--hour))">{h:02d}</span>'
        for h in range(int(lo), int(hi), 3)
    )
    columns, notes = [], []
    for d in days:
        head = f'<span class="cal-head">{d.strftime("%a")}<b>{d.day}</b></span>'
        cells = []
        for b in blocks.get(d, []):
            title = f"{'~' if not b.guessed else '≈'}{_clock_text(b.start)} {b.kind}"
            cells.append(
                f'<span class="blk{" guess" if b.guessed else ""}" title="{escape(title)}" '
                f'style="--h:{_hue(b.kind)};top:calc({b.start - lo} * var(--hour));'
                f"height:calc({b.end - b.start} * var(--hour) - 2px);"
                f'left:{100 * b.lane / b.lanes:.2f}%;width:{100 / b.lanes:.2f}%">'
                f"{escape(b.kind.replace('_', ' '))}</span>"
            )
        today_cls = " today" if d == today else ""
        if d in entries:
            columns.append(
                f'<button class="cal-day{today_cls}" data-day="{d.isoformat()}" '
                f'aria-label="{escape(d.strftime("%A"))} {escape(_short(d))}">{head}'
                f'<span class="cal-body">{"".join(cells)}</span></button>'
            )
            times = "".join(
                f"<li>{'≈' if b.guessed else '~'}{_clock_text(b.start)}–{_clock_text(b.end)} "
                f"{escape(b.kind.replace('_', ' '))}"
                f"{' · ' + escape(b.detail) if b.detail else ''}</li>"
                for b in sorted(blocks[d], key=lambda b: b.start)
            )
            notes.append(
                f'<template id="day-{d.isoformat()}"><h3>{escape(d.strftime("%A"))}, '
                f"{escape(_short(d))}</h3>"
                + (f'<ul class="times">{times}</ul>' if times else "")
                + _summary_html(entries[d].summary)
                + "</template>"
            )
        else:
            columns.append(
                f'<div class="cal-day empty{today_cls}">{head}<span class="cal-body"></span></div>'
            )

    prev = monday - timedelta(days=7)
    nxt = monday + timedelta(days=7)
    later = (
        f'<a href="/?week={nxt.isoformat()}" aria-label="Next week">›</a>' if nxt <= today else ""
    )
    span = f"{_short(monday)} – {_short(days[-1])}"
    return (
        f'<h2>Week</h2><div class="cal-nav"><a href="/?week={prev.isoformat()}" '
        f'aria-label="Previous week">‹</a><span>{escape(span)}</span>{later}</div>'
        f'<div class="cal" style="--span:{hi - lo}"><div class="cal-gutter">{gutter}</div>'
        + "".join(columns)
        + "</div>"
        '<p class="muted cal-key">Rough times: ~ estimated from what you said, '
        "≈ guessed from the order. Tap a day for its summary.</p>"
        + "".join(notes)
        + '<dialog id="dayDialog"><div id="dayBody"></div>'
        '<form method="dialog"><button>Close</button></form></dialog>'
        f"<script>{_CAL_JS}</script>"
    )


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


def _map_section(data: dict[str, Any], title: bool = True) -> str:
    if not data["stops"]:
        return ""
    # `</` can't appear inside a script element, so the JSON can't close it early.
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return (
        ("<h2>Where I'm headed</h2>" if title else "")
        + '<div class="legend"><span><i style="background:var(--here)"></i>Now</span>'
        '<span><i style="background:var(--dot)"></i>Planned</span></div>'
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


# Root id -> categorical slot (see --root-N in the CSS); other roots take
# the slots left, in file order.
_ROOT_SLOTS = {"water": 1, "body": 2, "earth": 3, "air": 4}

_STATE_TEXT = {
    "active": "",
    "not started": "not started",
    "ready": "ready",
    "locked": "locked",
    None: "",
}


def _discipline_list(nodes: list[dict[str, Any]], days: int) -> str:
    """The graph as an indented list: the same facts without the drawing."""
    children: dict[str | None, list[dict[str, Any]]] = {}
    for n in nodes:
        children.setdefault(n["parent"], []).append(n)
    items: list[str] = []

    def walk(parent: str | None, depth: int) -> None:
        for n in children.get(parent, []):
            bits = [f"{n['recent']} in {days}d", f"{n['total']} all-time"]
            state = _STATE_TEXT.get(n["state"], "")
            if n["state"] == "locked" and n["needs"]:
                state = "locked: needs " + ", ".join(n["needs"])
            extra = " · ".join(x for x in (state, *("goal: " + t for t in n["goals"])) if x)
            items.append(
                f'<li style="padding-left:{depth * 16}px"><div class="row">'
                f"<span>{escape(n['name'])}</span><span>{escape(' · '.join(bits))}</span></div>"
                f"{_sub(extra)}</li>"
            )
            walk(n["id"], depth + 1)

    walk(None, 0)
    return f"<ul>{''.join(items)}</ul>"


def discipline_slots(graph: disciplines.Graph) -> dict[str, int]:
    """Root id -> colour slot 1-4: the fixed ones, then the slots left in file order."""
    slots = dict(_ROOT_SLOTS)
    spare = [n for n in range(1, 5) if n not in slots.values()]
    for root in graph.roots():
        if root not in slots and spare:
            slots[root] = spare.pop(0)
    return slots


def _disciplines_section(vault: Vault, today: date, load_d3: bool) -> str:
    graph, recent, all_time = disciplines.snapshot(vault, today)
    if not graph:
        return ""
    nodes = disciplines.graph_data(graph, vault.read_goals(), recent, all_time)
    slots = discipline_slots(graph)
    days = disciplines.RECENT_DAYS
    payload = json.dumps({"nodes": nodes, "slots": slots, "days": days}, ensure_ascii=False)
    payload = payload.replace("</", "<\\/")
    legend = "".join(
        f'<span><i style="background:var(--root-{slots[r]})"></i>{escape(graph.name(r))}</span>'
        for r in graph.roots()
        if r in slots
    )
    states = (
        '<span><i class="glyph full"></i>Pursuing</span>'
        '<span><i class="glyph"></i>Not started</span>'
        '<span><i class="glyph ready"></i>Want, ready</span>'
        '<span><i class="glyph locked"></i>Locked</span>'
        '<span><i class="glyph line"></i>Also counts under</span>'
    )
    scripts = f'<script src="{_D3}"></script>' if load_d3 else ""
    return (
        "<h2>Disciplines</h2>"
        f'<div class="legend">{legend}</div><div class="legend">{states}</div>'
        '<div class="map-card" id="discCard">'
        '<svg id="disc" role="img" aria-label="The discipline graph: sports under Water, Air, '
        'Earth and Body, sized by sessions"></svg>'
        '<div class="tip" id="disctip"></div><div class="map-tools">'
        '<button id="discIn" aria-label="Zoom in">+</button>'
        '<button id="discOut" aria-label="Zoom out">−</button>'
        '<button id="discFit" aria-label="Fit">⟲</button></div></div>'
        '<p class="muted" style="margin-top:8px">Size is all-time sessions. Tap a sport for '
        "its numbers, a root to zoom in.</p>"
        '<details class="list"><summary>As a list</summary>'
        f"{_discipline_list(nodes, days)}</details>"
        f'<script type="application/json" id="discdata">{payload}</script>'
        f"{scripts}<script>{_DISC_JS}</script>"
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


# The guest view is one screen: no scrolling on a phone, two columns on a
# desktop with the map filling the right.
_GUEST_CSS = """
.one { box-sizing:border-box; height:100vh; height:100dvh; max-width:1180px; margin:0 auto;
  display:grid; grid-template-rows:auto minmax(0,1fr) auto; gap:14px;
  padding:calc(14px + env(safe-area-inset-top)) 16px calc(10px + env(safe-area-inset-bottom)); }
.info { display:flex; flex-direction:column; gap:12px; min-width:0; }
.info .eyebrow { margin:0; }
.info h1 { font-size:clamp(30px,8vw,46px); margin:0; }
.next { display:flex; flex-direction:column; gap:2px; padding:12px 14px;
  border:1px solid var(--line); border-radius:14px; background:var(--surface); }
.next .k { font:500 11px/1 var(--mono); text-transform:uppercase; letter-spacing:.08em;
  color:var(--faint); }
.next b { font:500 24px/1.2 var(--serif); }
.next .when { color:var(--muted); font-size:14px; }
.then { display:flex; flex-wrap:wrap; gap:4px 14px; margin-top:6px; font-size:13px;
  color:var(--muted); }
.then li { padding:0; border:0; }
.then li span { font-family:var(--mono); color:var(--faint); }
.one .stats { margin:0; gap:22px; }
.one .stat b { font-size:24px; }
.lately { display:flex; flex-wrap:wrap; gap:6px; }
.lately span { font-size:13px; padding:4px 10px; border-radius:999px; background:var(--surface);
  border:1px solid var(--line); color:var(--muted); }
.lately span b { color:var(--text); font-weight:500; }
.mapbox { display:flex; flex-direction:column; min-height:0; }
.mapbox .legend { margin-bottom:6px; }
.mapbox .map-card { flex:1; min-height:140px; }
.one #map { height:100%; }
.one footer { margin:0; padding:8px 0 0; }
.one .empty { color:var(--faint); font-size:14px; }
@media (min-width: 900px) {
  .one { grid-template-columns:minmax(300px,380px) minmax(0,1fr);
    grid-template-rows:minmax(0,1fr) auto; column-gap:28px; padding-top:28px; }
  .info { justify-content:center; }
  .one footer { grid-column:1 / -1; }
}
@media (max-height: 600px) and (max-width: 899px) {
  .one { height:auto; }
  .mapbox .map-card { height:260px; }
}
"""


def _countdown(start: date, today: date) -> str:
    days = (start - today).days
    return "today" if days <= 0 else "tomorrow" if days == 1 else f"in {days} days"


def render_guest(vault: Vault, today: date, login: bool = False) -> str:
    """The guest view: where I am, where I'm headed and when, a few numbers,
    what I've been doing, the map. Nothing from the journal's words."""
    location = vault.read_location() or []
    itinerary = vault.read_itinerary()
    current = brief.current_location(location)
    place = str(current.get("place", "")) if current else ""
    since = _as_date(current.get("from")) if current else None
    here = f"Day {(today - since).days + 1} · since {_short(since)}" if since else ""

    headed = future_map(itinerary, PlaceIndex(vault.read_places()), location, today)
    legs = sorted(
        (ahead[0], str(e.get("place", e.get("id", "?"))), ahead[1])
        for e in itinerary or []
        if (ahead := _ahead(e, today)) is not None
    )
    if legs:
        start, name, label = legs[0]
        then = "".join(
            f"<li>{escape(n)} <span>{escape(lbl)}</span></li>" for _, n, lbl in legs[1:3]
        )
        next_block = (
            f'<div class="next"><span class="k">Next</span><b>{escape(name)}</b>'
            f'<span class="when">{escape(_countdown(start, today))} · {escape(label)}</span>'
            + (f'<ul class="then">{then}</ul>' if then else "")
            + "</div>"
        )
    else:
        next_block = '<p class="empty">Nothing dated ahead.</p>'

    countries = {s["country"] for s in headed["stops"] if s["country"]}
    places_been = len({str(s.get("place")) for s in location if s.get("place")})
    stats = (
        '<div class="stats">'
        + _stat(len(headed["stops"]), "Legs ahead")
        + _stat(len(countries), "Countries ahead")
        + _stat(places_been, "Places so far")
        + "</div>"
    )

    # The last week's sports only: what I've been doing, not my job search.
    graph = disciplines.Graph(vault.read_disciplines())
    week = vault.read_journal_range(today - timedelta(days=RECENT_DAYS - 1), today)
    days_by_sport: dict[str, set[date]] = {}
    for d, e in week.items():
        for a in e.frontmatter.get("activities") or []:
            node = graph.resolve(a.get("type")) if isinstance(a, dict) else None
            if node:
                days_by_sport.setdefault(node, set()).add(d)
    top = sorted(days_by_sport.items(), key=lambda kv: -len(kv[1]))[:4]
    lately = (
        '<div class="lately">'
        + "".join(f"<span><b>{escape(graph.name(n))}</b> {_days(len(ds))}</span>" for n, ds in top)
        + "</div>"
        if top
        else ""
    )

    info = (
        f'<section class="info"><div class="eyebrow">'
        f"{escape(today.strftime('%A'))}, {escape(_short(today))}</div>"
        f"<h1>{escape(place or 'Location unknown')}</h1>"
        f'<p class="lede">{escape(here)}</p>{next_block}{stats}{lately}</section>'
    )
    mapbox = (
        f'<section class="mapbox">{_map_section(headed, title=False)}</section>'
        if headed["stops"]
        else ""
    )
    door = (
        '<form class="key" method="post" action="/login">'
        '<input type="password" name="password" placeholder="password" aria-label="Password" '
        'autocomplete="current-password" enterkeyhint="go" required></form>'
        if login
        else ""
    )
    footer = f"<footer><span>Read-only · {escape(today.isoformat())}</span>{door}</footer>"
    sw = (
        '<script>if ("serviceWorker" in navigator) '
        'navigator.serviceWorker.register("/sw.js");</script>'
    )
    return (
        _head("Alex · status")
        + f"<style>{_GUEST_CSS}</style>"
        + f'<main class="one">{info}{mapbox}{footer}</main>{sw}</body></html>'
    )


def render(
    vault: Vault,
    today: date,
    *,
    private: bool = False,
    login: bool = False,
    week_of: date | None = None,
) -> str:
    """The page. Guests get the one-screen `render_guest`; `private` is the
    long page with the week calendar and the rest, showing the week of
    `week_of` (this week by default, never a future one). `login` shows the
    password line on the guest view (only when a password is configured)."""
    if not private:
        return render_guest(vault, today, login)
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
    header = (
        f'<header><div class="eyebrow">{"Private · " if private else ""}'
        f"{escape(today.strftime('%A'))}, {escape(_short(today))}</div>"
        f"<h1>{escape(place or 'Location unknown')}</h1>"
        f'<p class="lede">{escape(here)}</p><div class="stats">'
        + _stat(len(headed["stops"]), "Legs ahead")
        + _stat(len(countries), "Countries ahead")
        + "</div></header>"
    )

    body = header + _map_section(headed)
    body += _section("Next", legs, "Nothing dated on the itinerary.")
    if private:
        body += _calendar_section(vault, today, min(week_of or today, today))
    body += _section("Goals", goals, "No active goals.")
    if private:
        body += _disciplines_section(vault, today, load_d3=not headed["stops"])
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
