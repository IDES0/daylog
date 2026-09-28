"""Confirmed edits to the vault's data, proposed by the chat assistant.

Pure logic: each function mutates the live structures it's given (from
vault.read_*) and returns a one-line summary, or raises EditError with a
reason the user can read. The caller (chat_flow) reads, applies and
writes; the assistant only ever proposes, and the user's tap on a
confirm card is what makes an edit happen.

Renames keep the old name as an alias — voice notes will keep saying the
old name for a while, and it should still resolve.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from daylog import goals, trail
from daylog.places import KINDS, PlaceIndex, file_for


class EditError(ValueError):
    pass


def _all_nodes(files: dict[str, Any]) -> list[Any]:
    return [n for nodes in files.values() for n in nodes]


def _locate(files: dict[str, Any], place_id: str) -> tuple[str, Any]:
    for stem, nodes in files.items():
        for node in nodes:
            if node.get("id") == place_id:
                return stem, node
    raise EditError(f"no place with id {place_id!r}")


def _aliases(node: Any) -> Any:
    aliases = node.get("aliases")
    if aliases is None:
        aliases = []
        node["aliases"] = aliases
    return aliases


# -- places ------------------------------------------------------------------


def edit_place(files: dict[str, Any], edit: dict[str, Any]) -> tuple[set[str], str]:
    """rename / aliases / kind / parent / lat-lon / description on one place.

    Returns (changed file stems, summary). A new parent that puts the
    place in another region moves it to that region's file.
    """
    stem, node = _locate(files, str(edit.get("place_id")))
    index = PlaceIndex(_all_nodes(files))
    changes: list[str] = []
    changed = {stem}

    new_name = edit.get("name")
    if new_name and new_name != node.get("name"):
        old = str(node.get("name"))
        node["name"] = new_name
        aliases = _aliases(node)
        if old not in aliases:
            aliases.append(old)
        if new_name in aliases:
            aliases.remove(new_name)
        changes.append(f"renamed {old} → {new_name}")
    for alias in edit.get("add_aliases") or []:
        aliases = _aliases(node)
        if alias not in aliases and alias != node.get("name"):
            aliases.append(alias)
            changes.append(f"alias +{alias}")
    for alias in edit.get("remove_aliases") or []:
        if alias in (node.get("aliases") or []):
            node["aliases"].remove(alias)
            changes.append(f"alias -{alias}")
    kind = edit.get("kind")
    if kind and kind != node.get("kind"):
        if kind not in KINDS:
            raise EditError(f"unknown kind {kind!r}")
        changes.append(f"kind {node.get('kind')} → {kind}")
        node["kind"] = kind
    parent = edit.get("parent_id")
    if parent and parent != node.get("parent"):
        if parent not in index.by_id:
            raise EditError(f"no place with id {parent!r} to move under")
        if parent == node["id"] or node["id"] in {a["id"] for a in index.ancestors(parent)}:
            raise EditError("that would put a place inside itself")
        node["parent"] = parent
        changes.append(f"moved under {index.path_name(parent)}")
        target = file_for(PlaceIndex(_all_nodes(files)), parent)
        if node.get("kind") not in ("region", "park") and target != stem:
            files[stem].remove(node)
            files.setdefault(target, []).append(node)
            changed.add(target)
    if edit.get("lat") is not None and edit.get("lon") is not None:
        node["lat"], node["lon"] = float(edit["lat"]), float(edit["lon"])
        node.setdefault("confidence", "approximate")
        changes.append("coordinates set")
    if edit.get("description"):
        node["description"] = edit["description"]
        changes.append("description updated")
    if not changes:
        raise EditError("nothing to change")
    return changed, f"{node.get('name')}: " + "; ".join(changes)


def delete_place(
    files: dict[str, Any], place_id: str, linked_days: list[date]
) -> tuple[set[str], str]:
    """Remove a place nothing depends on. Linked or parent places need a merge instead."""
    stem, node = _locate(files, place_id)
    index = PlaceIndex(_all_nodes(files))
    if index.children.get(place_id):
        raise EditError(f"{node.get('name')} has places inside it — move or merge those first")
    if linked_days:
        raise EditError(
            f"{node.get('name')} is linked from {len(linked_days)} journal day(s) — merge it into "
            "the right place instead of deleting"
        )
    files[stem].remove(node)
    return {stem}, f"deleted {node.get('name')}"


def merge_places(files: dict[str, Any], source_id: str, target_id: str) -> tuple[set[str], str]:
    """Fold `source` into `target`: names become aliases, facts/notes carry over,
    children re-parent, source is removed. Journal and ranking links are the
    caller's job (see relink_journal / relink_rankings)."""
    if source_id == target_id:
        raise EditError("can't merge a place into itself")
    s_stem, source = _locate(files, source_id)
    t_stem, target = _locate(files, target_id)
    aliases = _aliases(target)
    for name in [source.get("name"), *(source.get("aliases") or [])]:
        if name and name != target.get("name") and name not in aliases:
            aliases.append(name)
    for key in ("description", "lat", "lon", "facts"):
        if source.get(key) and not target.get(key):
            target[key] = source[key]
    notes = source.get("my_notes") or []
    if notes:
        target.setdefault("my_notes", [])
        target["my_notes"].extend(notes)
    changed = {s_stem, t_stem}
    for stem, nodes in files.items():
        for node in nodes:
            if node.get("parent") == source_id:
                node["parent"] = target_id
                changed.add(stem)
    files[s_stem].remove(source)
    return changed, f"merged {source.get('name')} into {target.get('name')}"


def relink_journal(frontmatter: dict[str, Any], source_id: str, target_id: str) -> bool:
    hit = False
    for field_name in trail.PLACE_LINKED_FIELDS:
        for item in frontmatter.get(field_name) or []:
            if isinstance(item, dict) and item.get("place") == source_id:
                item["place"] = target_id
                hit = True
    return hit


def relink_rankings(rankings: Any, source_id: str, target_id: str) -> bool:
    """Target keeps its own position if ranked; otherwise it takes the source's."""
    hit = False
    for tiers in (rankings or {}).values():
        target_ranked = any(target_id in (lst or []) for lst in tiers.values())
        for lst in tiers.values():
            if lst and source_id in lst:
                i = lst.index(source_id)
                if target_ranked:
                    del lst[i]
                else:
                    lst[i] = target_id
                    target_ranked = True
                hit = True
    return hit


# -- journal -------------------------------------------------------------------

JOURNAL_FIELDS = ("activities", "meals", "felt", "skipped", "open_questions")


def edit_journal(frontmatter: dict[str, Any], edit: dict[str, Any], known_ids: set[str]) -> str:
    """Change, remove or add one item in a day's activities/meals/felt/... list."""
    field_name = edit.get("field")
    if field_name not in JOURNAL_FIELDS:
        raise EditError(f"can only edit {', '.join(JOURNAL_FIELDS)}")
    items = frontmatter.get(field_name)
    if items is None:
        items = []
        frontmatter[field_name] = items
    action = edit.get("action", "update")
    values = dict(edit.get("values") or {})
    if values.get("place") and values["place"] not in known_ids:
        raise EditError(f"no place with id {values['place']!r}")
    if action == "add":
        if field_name in ("skipped", "open_questions"):
            items.append(str(values.get("text", "")))
        else:
            items.append(values)
        return f"added to {field_name}"
    index = edit.get("index")
    if not isinstance(index, int) or not (0 <= index < len(items)):
        raise EditError(f"{field_name} has no item {index}")
    if action == "remove":
        removed = items.pop(index)
        return f"removed from {field_name}: {removed}"
    item = items[index]
    if not isinstance(item, dict):
        items[index] = str(values.get("text", item))
        return f"{field_name}[{index}] updated"
    for key, value in values.items():
        if value is None:
            item.pop(key, None)
        else:
            item[key] = value
    if values.get("place"):
        item.pop("place_mention", None)
        item.pop("place_kind", None)
    return f"{field_name}[{index}] updated: " + ", ".join(values)


# -- location history ------------------------------------------------------------


def _day(value: Any) -> date | None:
    if value in (None, ""):
        return None
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def edit_location(location: list[Any], edit: dict[str, Any]) -> str:
    """add / update / remove a stay. Keeps the list sorted by start date."""
    action = edit.get("action", "update")
    if action == "add":
        start = _day(edit.get("from"))
        if start is None or not edit.get("place"):
            raise EditError("a new stay needs place and from")
        entry: dict[str, Any] = {"place": edit["place"], "from": start, "to": _day(edit.get("to"))}
        for key in ("place_id", "mode", "lat", "lon", "notes"):
            if edit.get(key) is not None:
                entry[key] = edit[key]
        location.append(entry)
        location.sort(key=lambda e: _day(e.get("from")) or date.min)
        return f"added stay {edit['place']} from {start}"
    index = edit.get("index")
    if not isinstance(index, int) or not (0 <= index < len(location)):
        raise EditError(f"no stay number {index}")
    if action == "remove":
        gone = location.pop(index)
        return f"removed stay {gone.get('place')}"
    entry = location[index]
    for key in ("place", "place_id", "mode", "notes", "lat", "lon"):
        if key in edit:
            entry[key] = edit[key]
    for key in ("from", "to"):
        if key in edit:
            entry[key] = _day(edit[key])
    location.sort(key=lambda e: _day(e.get("from")) or date.min)
    return f"updated stay {entry.get('place')}"


# -- goals -------------------------------------------------------------------------


def edit_goal(goals_data: list[Any], edit: dict[str, Any], on: date) -> str:
    """Create a goal, or change one's title/target/progress/date/status."""
    goal_id = edit.get("goal_id")
    goal = goals.find_goal(goals_data, goal_id) if goal_id else None
    if goal is None:
        if not edit.get("title"):
            raise EditError("a new goal needs a title")
        from daylog.places import slugify

        new_id = goal_id or slugify(str(edit["title"]))
        if goals.find_goal(goals_data, new_id):
            raise EditError(f"goal id {new_id!r} already exists")
        goal = {
            "id": new_id,
            "title": edit["title"],
            "type": edit.get("type", "soft"),
            "status": "active",
        }
        goals_data.append(goal)
        created = True
    else:
        created = False
    for key in ("title", "metric", "notes", "status"):
        if edit.get(key):
            goal[key] = edit[key]
    if edit.get("target") is not None:
        goal["target"] = edit["target"]
    if edit.get("progress_delta"):
        goal["progress"] = (goal.get("progress") or 0) + edit["progress_delta"]
    if edit.get("date"):
        new_date = _day(edit["date"])
        if goal.get("type") == "hard":
            goal["deadline"] = new_date
        else:
            window = goal.get("target_window")
            if window:
                window[-1] = new_date
            else:
                goal["target_window"] = [on, new_date]
    return f"{'created' if created else 'updated'} goal {goal['title']}"


# -- focus & research preferences (profile.yaml) ------------------------------------

FOCUS_LISTS = ("focus", "research_more", "research_less")


def edit_focus(profile: Any, edit: dict[str, Any]) -> str:
    """add / remove / replace items in profile.yaml's `focus` list (what research and
    the brief should centre on right now) or the research_more / research_less lists.

    `focus` is the explicit steer; anything not listed is inferred from goals and
    dates by the research routine."""
    target = edit.get("list", "focus")
    if target not in FOCUS_LISTS:
        raise EditError(f"can only edit {', '.join(FOCUS_LISTS)}")
    if not isinstance(profile, dict):
        raise EditError("profile.yaml is not a mapping")
    items = profile.get(target)
    if items is None:
        items = []
        profile[target] = items
    action = edit.get("action", "add")
    if action == "replace":
        new_items = [str(i) for i in edit.get("items") or []]
        items.clear()
        items.extend(new_items)
        return f"{target} set to {len(new_items)} item(s)"
    text = str(edit.get("item", "")).strip()
    if not text:
        raise EditError("nothing to add or remove")
    if action == "add":
        if text in items:
            raise EditError(f"already in {target}")
        items.append(text)
        return f"{target} +{text}"
    if action == "remove":
        match = next((i for i in items if text.lower() in str(i).lower()), None)
        if match is None:
            raise EditError(f"nothing in {target} matches {text!r}")
        items.remove(match)
        return f"{target} -{match}"
    raise EditError(f"unknown action {action!r}")
