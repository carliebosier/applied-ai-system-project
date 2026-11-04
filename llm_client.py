"""Tool schemas and LLM clients for the agent: a recorded MockClient now, GeminiClient later."""

from __future__ import annotations

import copy
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger("pawpal.llm_client")

# Resolved from this file, not the working directory, so tests and the UI find it anywhere.
FIXTURES_PATH = Path(__file__).resolve().parent / "fixtures" / "mock_responses.json"

# ---------------------------------------------------------------------------
# Tool schemas (Gemini function-declaration shape, plain dicts).
# ---------------------------------------------------------------------------

SUBMIT_TASKS = {
    "name": "submit_tasks",
    "description": (
        "Submit the pet care tasks found in the user's request. "
        "Omit any field the user did not state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "available_minutes": {
                "type": "integer",
                "description": "The user's time budget for the day in minutes, only if they stated it.",
            },
            "tasks": {
                "type": "array",
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {
                        "pet_name": {
                            "type": "string",
                            "description": "The pet's name as the user wrote it.",
                        },
                        "description": {
                            "type": "string",
                            "description": "A short description of the task, such as 'walk' or 'meds'.",
                        },
                        "quote": {
                            "type": "string",
                            "description": "The exact words from the request this task came from.",
                        },
                        "date": {
                            "type": "string",
                            "description": '"today", "tomorrow" or YYYY-MM-DD.',
                        },
                        "time": {
                            "type": "string",
                            "description": "Start time as HH:MM 24-hour.",
                        },
                        "duration_minutes": {
                            "type": "integer",
                            "description": "How many minutes the task takes.",
                        },
                        # No enum on purpose: an enum would coerce "every 6 hours" into a
                        # supported value, a guess the guardrails could never see (plan §3).
                        "frequency": {
                            "type": "string",
                            "description": (
                                "How often, in the user's words. Supported: daily, weekly, as needed."
                            ),
                        },
                    },
                    "required": ["pet_name", "description", "quote"],
                },
            },
        },
        "required": ["tasks"],
    },
}

ASK_CLARIFICATION = {
    "name": "ask_clarification",
    "description": "Ask the user a question when the request isn't about pet care or can't be parsed at all.",
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to show the user.",
            },
        },
        "required": ["question"],
    },
}

# Same-day revisions only, so neither revise tool has a date field (plan §4).
MOVE_TASKS = {
    "name": "move_tasks",
    "description": "Propose new same-day start times for referenced new tasks to remove the listed overlaps.",
    "parameters": {
        "type": "object",
        "properties": {
            "moves": {
                "type": "array",
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "ref": {
                            "type": "string",
                            "description": "The new task's ref, such as new-1.",
                        },
                        "time": {
                            "type": "string",
                            "description": "The new start time as HH:MM 24-hour.",
                        },
                    },
                    "required": ["ref", "time"],
                },
            },
            "reason": {
                "type": "string",
                "description": "A short explanation of the moves.",
            },
        },
        "required": ["moves", "reason"],
    },
}

KEEP_PLAN = {
    "name": "keep_plan",
    "description": "Keep the plan unchanged when the overlaps can't be removed by moving new tasks.",
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Why the plan can't be improved.",
            },
        },
        "required": ["reason"],
    },
}

PARSE_TOOLS = [SUBMIT_TASKS, ASK_CLARIFICATION]
REVISE_TOOLS = [MOVE_TASKS, KEEP_PLAN]
PARSE_TOOL_NAMES = {tool["name"] for tool in PARSE_TOOLS}
REVISE_TOOL_NAMES = {tool["name"] for tool in REVISE_TOOLS}


@dataclass(frozen=True)
class ToolCall:
    """One function call returned by a client: the tool's name and its arguments."""

    name: str
    args: dict


def normalize_request(text: str) -> str:
    """Lowercase and collapse whitespace so fixture lookup ignores case and spacing."""
    return " ".join(text.lower().split())


# ---------------------------------------------------------------------------
# Mock client
# ---------------------------------------------------------------------------


class UnknownMockRequest(Exception):
    """Raised when mock mode has no recorded response for a request."""

    def __init__(self, request: str, supported: list[str]):
        self.request = request
        self.supported = list(supported)
        super().__init__(
            f"No recorded mock response for {request!r}. Supported requests: {self.supported}"
        )


class MockReviseError(RuntimeError):
    """Raised when a revise call has no recorded response to return."""


class MockClient:
    """Replays recorded tool calls from fixtures/mock_responses.json, never inventing one.

    Contract: on a "parse" call, user_text is the raw request and nothing else.
    """

    is_mock = True

    def __init__(self, fixtures_path: Path | str = FIXTURES_PATH):
        with open(fixtures_path, encoding="utf-8") as handle:
            self._fixtures: dict = json.load(handle)
        self._current: dict | None = None
        self._revise_index = 0

    @property
    def supported_requests(self) -> list[str]:
        """The original text of every recorded request."""
        return [fixture["request"] for fixture in self._fixtures.values()]

    def call(self, step: str, system_prompt: str, user_text: str, tools: list) -> ToolCall:
        """Return the recorded tool call for this step of the current request."""
        if step == "parse":
            return self._parse(user_text)
        if step == "revise":
            return self._revise()
        raise ValueError(f"Unknown step {step!r}; expected 'parse' or 'revise'.")

    def _parse(self, user_text: str) -> ToolCall:
        # Cleared first, so a failed lookup can't leave a previous fixture active for revise.
        self._current = None
        self._revise_index = 0
        fixture = self._fixtures.get(normalize_request(user_text))
        if fixture is None:
            raise UnknownMockRequest(user_text, self.supported_requests)
        self._current = fixture
        self._revise_index = 0
        # Deep copies, so a caller mutating the args can't change the next replay.
        return ToolCall("submit_tasks", copy.deepcopy(fixture["parse"]))

    def _revise(self) -> ToolCall:
        if self._current is None:
            raise MockReviseError("Mock revise call made before any parse call.")
        recorded = self._current["revise"]
        if self._revise_index >= len(recorded):
            raise MockReviseError(
                f"Mock has {len(recorded)} recorded revise response(s) for "
                f"{self._current['request']!r}; revise call {self._revise_index + 1} has none."
            )
        entry = recorded[self._revise_index]
        self._revise_index += 1
        return ToolCall(entry["name"], copy.deepcopy(entry["args"]))


# ---------------------------------------------------------------------------
# Client selection
# ---------------------------------------------------------------------------


def get_client():
    """Return MockClient in mock mode or without an API key; live Gemini comes in increment 4."""
    # Loaded here rather than at import, and never overriding variables already set.
    load_dotenv(override=False)

    mode = os.environ.get("PAWPAL_LLM_MODE", "").strip().lower()
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if mode == "mock":
        logger.warning("PAWPAL_LLM_MODE=mock: using recorded responses, no API calls.")
        return MockClient()
    if not api_key:
        logger.warning("GEMINI_API_KEY is not set: falling back to mock mode.")
        return MockClient()
    raise NotImplementedError("GeminiClient is added in increment 4")
