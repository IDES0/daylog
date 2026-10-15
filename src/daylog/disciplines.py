"""The discipline graph: sports as a DAG that rolls up into Water, Air, Earth, Body.

disciplines.yaml maps an id to a node:

    wing_foil:
      name: Wingfoil
      parents: [foil, air]     # what it is a kind of; its sessions count toward each
      requires: [goal: x]      # what has to come first; nothing rolls up along these
      status: active           # active | want; left out on pure groupings
      aliases: [wingfoiling]   # other labels the journal has used

A node can have several parents, so totals are computed per node by walking
every ancestor once — never by adding up the roots, which would double count
a kitefoil session that is both Water and Air.

A vague label ("foiled") on a node with `resolve:` is narrowed to one of its
children: the one logged most at that spot, then in that area, then by place
(`by_place`, matched against the place and its ancestors), then `default`.
The activity keeps `guessed: <label as said>` so the user can fix it in one
tap and history knows it was inferred.

Pure logic: the caller reads disciplines.yaml, the journal and goals via the
vault and passes plain data in.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from daylog.places import PlaceIndex
from daylog.vault import Vault

STATUSES = ("active", "want")

# How many recent sessions of a vague discipline decide its usual child at a spot.
HISTORY_SESSIONS = 10

# The window "recent" means in the brief, the planner, chat and /disciplines.
RECENT_DAYS = 30


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def slug(label: str) -> str:
    """'Sand boarding' -> 'sand_boarding', the id a new discipline gets."""
    return _norm(label).replace(" ", "_")


@dataclass
class Totals:
    sessions: int = 0
    hours: float = 0.0
    last: date | None = None

    def add(self, day: date, hours: Any) -> None:
        self.sessions += 1
        if isinstance(hours, int | float) and not isinstance(hours, bool):
            self.hours += float(hours)
        if self.last is None or day > self.last:
            self.last = day


@dataclass
class Guess:
    """A vague label narrowed to a child, for the reply and the fix-it buttons."""

    index: int
    said: str
    chosen: str
    reason: str
    options: list[str] = field(default_factory=list)


class Graph:
    def __init__(self, data: Mapping[str, Any] | None) -> None:
        self.nodes: dict[str, Any] = {
            str(k): (v if isinstance(v, Mapping) else {}) for k, v in (data or {}).items()
        }
        self._labels: dict[str, str] = {}
        for node_id, node in self.nodes.items():
            for label in [node_id, node.get("name") or "", *(node.get("aliases") or [])]:
                if label:
                    self._labels.setdefault(_norm(label), node_id)

    def __contains__(self, node_id: object) -> bool:
        return node_id in self.nodes

    def __bool__(self) -> bool:
        return bool(self.nodes)

    def name(self, node_id: str) -> str:
        return str(self.nodes.get(node_id, {}).get("name") or node_id)

    def parents(self, node_id: str) -> list[str]:
        return [str(p) for p in self.nodes.get(node_id, {}).get("parents") or [] if p in self]

    def children(self, node_id: str) -> list[str]:
        return [n for n in self.nodes if node_id in self.parents(n)]

    def roots(self) -> list[str]:
        return [n for n in self.nodes if not self.parents(n)]

    def status(self, node_id: str) -> str | None:
        value = self.nodes.get(node_id, {}).get("status")
        return str(value) if value in STATUSES else None

    def resolve(self, label: Any) -> str | None:
        """A journal activity label -> discipline id, via id, name or alias."""
        if not label:
            return None
        key = str(label).strip()
        if key in self.nodes:
            return key
        return self._labels.get(_norm(key))

    def lineage(self, node_id: str) -> set[str]:
        """The node and every ancestor, each once however many paths reach it."""
        seen: set[str] = set()
        stack = [node_id]
        while stack:
            current = stack.pop()
            if current in seen or current not in self:
                continue
            seen.add(current)
            stack.extend(self.parents(current))
        return seen

    def descendants(self, node_id: str) -> set[str]:
        out: set[str] = set()
        stack = self.children(node_id)
        while stack:
            current = stack.pop()
            if current not in out:
                out.add(current)
                stack.extend(self.children(current))
        return out

    def requires(self, node_id: str) -> list[tuple[str, str]]:
        """("goal", id) or ("discipline", id) for each prerequisite."""
        out = []
        for item in self.nodes.get(node_id, {}).get("requires") or []:
            if isinstance(item, Mapping) and item.get("goal"):
                out.append(("goal", str(item["goal"])))
            elif isinstance(item, str):
                out.append(("discipline", item))
        return out


# -- totals ----------------------------------------------------------------


def totals(graph: Graph, days: Mapping[date, Mapping[str, Any]]) -> dict[str, Totals]:
    """Sessions and hours per discipline from journal frontmatter, rolled up the parents."""
    out: dict[str, Totals] = {}
    for day, fm in sorted(days.items()):
        for activity in fm.get("activities") or []:
            if not isinstance(activity, Mapping):
                continue
            node_id = graph.resolve(activity.get("type"))
            if node_id is None:
                continue
            for member in graph.lineage(node_id):
                out.setdefault(member, Totals()).add(day, activity.get("hours"))
    return out


def numbers(graph: Graph, by_node: Mapping[str, Totals]) -> dict[str, dict[str, float]]:
    """{name: {sessions, hours}} for every discipline with a session, roots first."""
    order = graph.roots() + [n for n in graph.nodes if n not in graph.roots()]
    return {
        graph.name(n): {"sessions": by_node[n].sessions, "hours": round(by_node[n].hours, 1)}
        for n in order
        if by_node.get(n)
    }


# -- prerequisites and state ------------------------------------------------


def _goal_done(goals_data: Iterable[Any], goal_id: str) -> bool:
    return any(g.get("id") == goal_id and g.get("status") == "done" for g in goals_data or [])


def unmet(
    graph: Graph, node_id: str, goals_data: Iterable[Any], all_time: Mapping[str, Totals]
) -> list[str]:
    """Readable names of the prerequisites not met yet."""
    goals_list = list(goals_data or [])
    missing = []
    for kind, ref in graph.requires(node_id):
        if kind == "goal":
            if not _goal_done(goals_list, ref):
                goal = next((g for g in goals_list if g.get("id") == ref), None)
                missing.append(str(goal.get("title") if goal else ref))
        elif not all_time.get(ref):
            missing.append(graph.name(ref))
    return missing


def state(
    graph: Graph, node_id: str, goals_data: Iterable[Any], all_time: Mapping[str, Totals]
) -> str | None:
    """active | not started | ready | locked — None for a grouping with no status."""
    status = graph.status(node_id)
    if status == "active":
        return "active" if all_time.get(node_id) else "not started"
    if status == "want":
        return "locked" if unmet(graph, node_id, goals_data, all_time) else "ready"
    return None


def goals_for(goals_data: Iterable[Any], node_id: str) -> list[Any]:
    return [
        g
        for g in goals_data or []
        if g.get("discipline") == node_id and g.get("status", "active") == "active"
    ]


# -- vague labels -------------------------------------------------------------


def _resolver(graph: Graph, node_id: str) -> Mapping[str, Any] | None:
    rule = graph.nodes.get(node_id, {}).get("resolve")
    return rule if isinstance(rule, Mapping) else None


def _pick(
    graph: Graph,
    node_id: str,
    place_id: str | None,
    places: PlaceIndex,
    history: list[tuple[str, str | None]],
) -> tuple[str, str] | None:
    rule = _resolver(graph, node_id)
    if rule is None:
        return None
    options = graph.descendants(node_id)
    past = [(kind, where) for kind, where in history if kind in options]

    def usual(matching: set[str], label: str) -> tuple[str, str] | None:
        recent = [kind for kind, where in past if where in matching][-HISTORY_SESSIONS:]
        if not recent:
            return None
        return Counter(recent).most_common(1)[0][0], label

    if place_id:
        node = places.get(place_id)
        here = usual({place_id}, f"your usual at {node.get('name') if node else place_id}")
        if here:
            return here
        area = places.region_of(place_id) or (
            places.get(node.get("parent")) if node and node.get("parent") else None
        )
        if area is not None:
            nearby = {area["id"]} | {n["id"] for n in places.descendants(area["id"])}
            found = usual(nearby, f"your usual around {area.get('name')}")
            if found:
                return found

    by_place = rule.get("by_place") or {}
    if place_id and isinstance(by_place, Mapping):
        chain = [place_id] + [a["id"] for a in places.ancestors(place_id)]
        for candidate in chain:
            child = by_place.get(candidate)
            if child in graph:
                where = places.get(candidate)
                return str(child), f"the default in {where.get('name') if where else candidate}"

    default = rule.get("default")
    if default in graph:
        return str(default), "the default"
    return None


def resolve_vague(
    graph: Graph,
    activities: list[Any],
    places: PlaceIndex,
    current_place_id: str | None,
    history: list[tuple[str, str | None]],
    prefer: Iterable[str] = (),
) -> list[Guess]:
    """Narrow vague activity labels in place; return what was guessed.

    `history` is (discipline id, place id) per past session, oldest first.
    `prefer` is what the user already said or confirmed for the same day —
    a reconcile keeps those instead of guessing again.
    """
    preferred = [p for p in prefer if p in graph]
    guesses = []
    for index, activity in enumerate(activities):
        if not isinstance(activity, dict):
            continue
        node_id = graph.resolve(activity.get("type"))
        if node_id is None or _resolver(graph, node_id) is None:
            continue
        options = graph.descendants(node_id)
        kept = next((p for p in preferred if p in options), None)
        if kept is not None:
            activity["type"] = kept
            activity.pop("guessed", None)
            continue
        picked = _pick(graph, node_id, activity.get("place") or current_place_id, places, history)
        if picked is None:
            continue
        chosen, reason = picked
        said = str(activity.get("type"))
        activity["type"] = chosen
        activity["guessed"] = said
        siblings = sorted(c for c in graph.children(node_id) if graph.status(c))
        guesses.append(Guess(index, said, chosen, reason, siblings))
    return guesses


def history(graph: Graph, days: Mapping[date, Mapping[str, Any]]) -> list[tuple[str, str | None]]:
    """(discipline id, place id) for every logged session, oldest first."""
    out = []
    for _, fm in sorted(days.items()):
        for activity in fm.get("activities") or []:
            if isinstance(activity, Mapping):
                node_id = graph.resolve(activity.get("type"))
                if node_id:
                    out.append((node_id, activity.get("place")))
    return out


def stated(graph: Graph, fm: Mapping[str, Any] | None) -> list[str]:
    """Discipline ids the user said or confirmed on a day (not ones the bot guessed)."""
    out = []
    for activity in (fm or {}).get("activities") or []:
        if isinstance(activity, Mapping) and not activity.get("guessed"):
            node_id = graph.resolve(activity.get("type"))
            if node_id:
                out.append(node_id)
    return out


def set_type(fm: dict[str, Any], index: int, said: str, chosen: str) -> bool:
    """Apply a one-tap fix to a guessed activity; False if it no longer matches."""
    activities = fm.get("activities") or []
    if not 0 <= index < len(activities):
        return False
    activity = activities[index]
    if not isinstance(activity, dict) or activity.get("guessed") != said:
        return False
    activity["type"] = chosen
    activity.pop("guessed", None)
    return True


# -- adding a discipline ------------------------------------------------------


def new_node(graph: Graph, name: str, parents: list[str]) -> dict[str, Any]:
    """A node for a sport logged for the first time; parents not in the graph are dropped."""
    return {
        "name": name,
        "parents": [p for p in parents if p in graph],
        "status": "active",
    }


# -- text for prompts, the brief and /disciplines ---------------------------------


def outline(graph: Graph) -> str:
    """Every discipline, indented under its first parent, for the extraction prompt."""
    lines: list[str] = []

    def walk(node_id: str, depth: int) -> None:
        node = graph.nodes[node_id]
        extra = graph.parents(node_id)[1:]
        bits = []
        if extra:
            bits.append("also under " + ", ".join(extra))
        if node.get("aliases"):
            bits.append("aka " + ", ".join(str(a) for a in node["aliases"]))
        suffix = f" ({'; '.join(bits)})" if bits else ""
        lines.append(f"{'  ' * depth}- {node_id}: {graph.name(node_id)}{suffix}")
        for child in graph.children(node_id):
            if graph.parents(child)[0] == node_id:
                walk(child, depth + 1)

    for root in graph.roots():
        walk(root, 0)
    return "\n".join(lines)


def _count(t: Totals | None) -> str:
    if not t:
        return "0"
    hours = f" · {round(t.hours, 1):g}h" if t.hours else ""
    return f"{t.sessions}{hours}"


def tree(
    graph: Graph,
    goals_data: Iterable[Any],
    recent: Mapping[str, Totals],
    all_time: Mapping[str, Totals],
    days: int,
) -> str:
    """The /disciplines view: sessions in the last `days`, state, goals, locks."""
    goals_list = list(goals_data or [])
    lines = [f"Sessions · hours in the last {days} days [all-time sessions]."]

    def walk(node_id: str, depth: int) -> None:
        pad = "   " * depth
        label = graph.name(node_id)
        current = state(graph, node_id, goals_list, all_time)
        tag = ""
        if current == "not started":
            tag = " · not started"
        elif current == "ready":
            tag = " · ready"
        elif current == "locked":
            tag = " · 🔒 needs " + ", ".join(unmet(graph, node_id, goals_list, all_time))
        total = all_time.get(node_id)
        line = f"{pad}{label}"
        if total:
            line += f": {_count(recent.get(node_id))} [{total.sessions}]"
        lines.append(line + tag)
        for goal in goals_for(goals_list, node_id):
            lines.append(f"{pad}   🎯 {goal.get('title')}")
        for child in graph.children(node_id):
            if graph.parents(child)[:1] == [node_id]:
                walk(child, depth + 1)
            else:
                home = graph.name(graph.parents(child)[0])
                lines.append(f"{pad}   ↳ {graph.name(child)} (under {home})")

    for root in graph.roots():
        lines.append("")
        walk(root, 0)
    return "\n".join(lines)


def describe(
    graph: Graph,
    goals_data: Iterable[Any],
    recent: Mapping[str, Totals],
    all_time: Mapping[str, Totals],
    days: int,
) -> str:
    """A compact summary for the brief and the planner."""
    if not graph:
        return "(no disciplines set)"
    goals_list = list(goals_data or [])
    groups: dict[str, list[str]] = {"active": [], "not started": [], "ready": [], "locked": []}
    for node_id in graph.nodes:
        current = state(graph, node_id, goals_list, all_time)
        if current is None:
            continue
        name = graph.name(node_id)
        if current == "active":
            name += f" ({_count(recent.get(node_id))} in {days}d)"
        elif current == "locked":
            name += " — needs " + ", ".join(unmet(graph, node_id, goals_list, all_time))
        goal_titles = [str(g.get("title")) for g in goals_for(goals_list, node_id)]
        if goal_titles:
            name += " — goal: " + "; ".join(goal_titles)
        groups[current].append(name)
    roots = ", ".join(f"{graph.name(r)} {_count(recent.get(r))}" for r in graph.roots())
    labels = {
        "active": "Pursuing now",
        "not started": "Committed to but nothing logged yet",
        "ready": "Wanted, prerequisites met (a good moment to set a goal)",
        "locked": "Wanted, locked behind a prerequisite",
    }
    blocks = [
        f"Last {days} days by root (sessions · hours; a session can count toward "
        f"two roots): {roots}"
    ]
    for key, label in labels.items():
        if groups[key]:
            blocks.append(f"{label}:\n" + "\n".join(f"- {n}" for n in groups[key]))
    return "\n".join(blocks)


# -- reading it all from the vault ---------------------------------------------


def all_days(v: Vault, until: date) -> dict[date, dict[str, Any]]:
    """Every journal day's frontmatter up to `until`."""
    dates = v.list_journal_dates()
    if not dates:
        return {}
    return {d: e.frontmatter for d, e in v.read_journal_range(min(dates), until).items()}


def snapshot(
    v: Vault, today: date, days: int = RECENT_DAYS
) -> tuple[Graph, dict[str, Totals], dict[str, Totals]]:
    """The graph plus totals for the last `days` and for all time."""
    graph = Graph(v.read_disciplines())
    everything = all_days(v, today)
    recent = {d: fm for d, fm in everything.items() if d > today - timedelta(days=days)}
    return graph, totals(graph, recent), totals(graph, everything)


def summary(v: Vault, today: date) -> str:
    """The compact view the brief, the planner and chat are given."""
    graph, recent, all_time = snapshot(v, today)
    return describe(graph, v.read_goals(), recent, all_time, RECENT_DAYS)
