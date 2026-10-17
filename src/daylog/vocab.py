"""Words the user says that speech-to-text gets wrong: NASCO came out as
"Lasco", Uluwatu as "Iluwatu", foiling as "forling", heelside as "hillside".

Whisper takes a short primer (`initial_prompt`) and leans toward the words in
it. Transcripts are kept verbatim forever, so this is the only point where a
name can be heard right; extraction gets the same list to spell names in the
facts it writes. Built fresh per note from the vault: `vocabulary` in
profile.yaml first (the user's own list), then the current region's places,
then the discipline names. Kept short — an overloaded primer makes Whisper
hallucinate its words into silence.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from daylog import brief
from daylog.disciplines import Graph
from daylog.places import PlaceIndex
from daylog.vault import Vault

logger = logging.getLogger(__name__)

# Whisper's primer is cut at ~224 tokens; stay well under it.
MAX_CHARS = 700

# Place kinds worth priming: what gets named in a note about a day out.
_PLACE_KINDS = (
    "town",
    "region",
    "park",
    "surf_spot",
    "wind_spot",
    "fly_site",
    "dive_site",
    "beach",
    "hike",
    "viewpoint",
)


def custom(profile: Any) -> list[str]:
    raw = profile.get("vocabulary") if isinstance(profile, dict) else None
    return [str(x).strip() for x in raw or [] if str(x).strip()]


def terms(
    profile: Any, places: list[Any], location_entry: Any | None, disciplines: Any
) -> list[str]:
    """The user's words, then nearby places, then sports — deduplicated, capped."""
    index = PlaceIndex(list(places or []))
    here = index.current_place(location_entry)
    nearby: list[str] = []
    if here is not None:
        nearby = [str(n.get("name")) for n in [here, *index.ancestors(here["id"])]]
        nearby += [str(n.get("name")) for n in index.region_nodes(here, _PLACE_KINDS)]
    graph = Graph(disciplines)
    # Only what I'm doing now gets said; "want" sports would crowd out places.
    sports = [graph.name(n) for n in graph.nodes if graph.status(n) == "active"]
    out: list[str] = []
    seen: set[str] = set()
    size = 0
    for raw in [*custom(profile), *nearby, *sports]:
        # "Uluwatu (the wave)" is said "Uluwatu"; the bracket is for the eye.
        word = re.sub(r"\s*\([^)]*\)", "", raw).strip()
        key = word.casefold()
        if not word or key in seen or word == "None":
            continue
        if size + len(word) + 2 > MAX_CHARS:
            break
        seen.add(key)
        out.append(word)
        size += len(word) + 2
    return out


def for_vault(v: Vault) -> list[str]:
    return terms(
        v.read_profile(),
        v.read_places(),
        brief.current_location(v.read_location()),
        v.read_disciplines(),
    )


def primer(words: list[str]) -> str | None:
    """Whisper's initial_prompt: reads like text that came before the note."""
    return f"Words in this note may include: {', '.join(words)}." if words else None


def primer_for(v: Vault) -> str | None:
    """The primer for the next note; None (no primer) if the vault can't be read,
    since a note must never fail over its glossary."""
    try:
        return primer(for_vault(v))
    except Exception:
        logger.exception("couldn't build the transcription primer")
        return None
