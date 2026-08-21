"""Transcript text -> structured journal facts, via a single Claude tool call."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import anthropic
from anthropic.types import MessageParam, ToolChoiceToolParam, ToolParam

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"
PROMPT_PATH = Path(__file__).parent / "prompts" / "extract.md"

RECORD_JOURNAL_ENTRY_TOOL: ToolParam = {
    "name": "record_journal_entry",
    "description": "Record structured facts extracted from a voice journal transcript.",
    "input_schema": {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "Where the user was, e.g. 'Canggu, Bali'.",
            },
            "activities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "description": "Short activity label, e.g. 'surf'.",
                        },
                        "hours": {"type": "number", "description": "Approximate hours spent."},
                        "detail": {"type": "string", "description": "One-line specifics."},
                    },
                    "required": ["type"],
                },
            },
            "goal_progress": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "goal_id": {"type": "string"},
                        "delta": {"type": "number"},
                        "detail": {"type": "string"},
                    },
                    "required": ["delta"],
                },
            },
            "skipped": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Things the user said they meant to do but skipped.",
            },
            "mood": {"type": "string"},
            "open_questions": {
                "type": "array",
                "items": {"type": "string"},
            },
            "summary": {
                "type": "string",
                "description": "Two or three plain sentences summarising the day.",
            },
        },
        "required": ["activities", "summary"],
    },
}


class ExtractError(RuntimeError):
    pass


def extract(transcript: str, *, client: anthropic.Anthropic | None = None) -> dict[str, Any]:
    """Extract structured journal facts from a raw transcript.

    Returns a dict matching the journal frontmatter schema, plus a
    `summary` key the caller should pull out before writing to the vault.
    """
    client = client or anthropic.Anthropic()
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    tool_choice: ToolChoiceToolParam = {"type": "tool", "name": "record_journal_entry"}
    messages: list[MessageParam] = [{"role": "user", "content": transcript}]

    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=system_prompt,
        tools=[RECORD_JOURNAL_ENTRY_TOOL],
        tool_choice=tool_choice,
        messages=messages,
    )

    logger.info(
        "extract usage: input=%d output=%d",
        response.usage.input_tokens,
        response.usage.output_tokens,
    )

    for block in response.content:
        if block.type == "tool_use":
            return dict(block.input)

    raise ExtractError(f"no tool_use block in response (stop_reason={response.stop_reason})")
