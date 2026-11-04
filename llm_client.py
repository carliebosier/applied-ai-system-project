"""Tool schemas and LLM clients for the agent: a recorded MockClient and a live GeminiClient.

Run `python llm_client.py --smoke` to make one real Gemini parse call (skipped without a key).
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import sys
from collections.abc import Mapping, Sequence
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
# Gemini client
# ---------------------------------------------------------------------------


class LLMConfigError(Exception):
    """Raised when required LLM configuration is missing or blank."""


class LLMResponseError(Exception):
    """Raised when a model response isn't exactly one valid tool call."""


def _function_declarations(tools: list) -> list:
    """Convert the plain-dict tool schemas into the SDK's FunctionDeclaration objects."""
    from google.genai import types

    return [types.FunctionDeclaration.model_validate(tool) for tool in tools]


def _to_plain(value, path: str = "args"):
    """Recursively copy SDK/proto values into plain dict, list, str, int, float, bool or None."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _to_plain(item, f"{path}.{key}") for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_to_plain(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise LLMResponseError(f"Unsupported value type {type(value).__name__} at {path}.")


class GeminiClient:
    """Calls Gemini with forced function calling and returns exactly one ToolCall.

    SDK exceptions (network, quota, auth) are not caught; they propagate to the caller.
    """

    is_mock = False

    def __init__(self, api_key: str, model: str, sdk_client=None):
        model = (model or "").strip()
        if not model:
            raise LLMConfigError("GEMINI_MODEL is not set; copy the current model id from Google AI Studio.")
        self.model = model
        if sdk_client is None:
            if not (api_key or "").strip():
                raise LLMConfigError("GEMINI_API_KEY is not set.")
            from google import genai

            sdk_client = genai.Client(api_key=api_key.strip())
        self._sdk = sdk_client

    def call(self, step: str, system_prompt: str, user_text: str, tools: list) -> ToolCall:
        """Make one forced function-calling request and return its single tool call."""
        if step not in ("parse", "revise"):
            raise ValueError(f"Unknown step {step!r}; expected 'parse' or 'revise'.")
        from google.genai import types

        allowed = [tool["name"] for tool in tools]
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0,
            tools=[types.Tool(function_declarations=_function_declarations(tools))],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.ANY,
                    allowed_function_names=allowed,
                )
            ),
            # The SDK must never execute anything; our code runs every tool.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        response = self._sdk.models.generate_content(
            model=self.model, contents=user_text, config=config
        )
        return self._extract(response, set(allowed))

    @staticmethod
    def _extract(response, allowed: set[str]) -> ToolCall:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            raise LLMResponseError("Gemini returned no candidates.")
        content = getattr(candidates[0], "content", None)
        parts = getattr(content, "parts", None) or []
        if not parts:
            raise LLMResponseError("Gemini returned a candidate with no content parts.")

        calls = [part.function_call for part in parts if getattr(part, "function_call", None)]
        text = "".join(getattr(part, "text", None) or "" for part in parts)
        if len(calls) != 1:
            names = [getattr(call, "name", None) for call in calls]
            raise LLMResponseError(
                f"Expected exactly one function call, got {len(calls)} {names}; "
                f"text: {text[:200]!r}"
            )

        call = calls[0]
        name = getattr(call, "name", None)
        if name not in allowed:
            raise LLMResponseError(f"Gemini called {name!r}; allowed tools: {sorted(allowed)}.")

        raw_args = getattr(call, "args", None)
        args = _to_plain(raw_args if raw_args is not None else {})
        if not isinstance(args, dict):
            raise LLMResponseError(f"Function call args are a {type(raw_args).__name__}, not a mapping.")
        try:
            json.dumps(args, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise LLMResponseError(f"Function call args aren't plain JSON: {exc}") from exc
        return ToolCall(name, args)


# ---------------------------------------------------------------------------
# Client selection
# ---------------------------------------------------------------------------


def get_client():
    """Return the client to use: MockClient in mock mode or without an API key, else GeminiClient.

    .env is loaded first without overriding set variables. With a key and no mock mode, the
    model comes from GEMINI_MODEL; a blank value raises LLMConfigError (never defaulted).
    """
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
    return GeminiClient(api_key, os.environ.get("GEMINI_MODEL", ""))


# ---------------------------------------------------------------------------
# Live smoke test: `python llm_client.py --smoke`
# ---------------------------------------------------------------------------

SMOKE_SYSTEM_PROMPT = (
    "You turn a pet owner's request into pet care tasks. Today is 2026-11-02. "
    "The pets are Biscuit and Whiskers. Omit any field the user did not state. "
    "Never invent a time, duration or pet. "
    "Copy the frequency as the user said it; don't convert it."
)


def _smoke() -> int:
    load_dotenv(override=False)
    if os.environ.get("PAWPAL_LLM_MODE", "").strip().lower() == "mock":
        print("SKIP: PAWPAL_LLM_MODE=mock, so no live call is made.")
        return 0
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("SKIP: GEMINI_API_KEY is not set.")
        return 0

    try:
        with open(FIXTURES_PATH, encoding="utf-8") as handle:
            request = next(iter(json.load(handle).values()))["request"]
        client = GeminiClient(api_key, os.environ.get("GEMINI_MODEL", ""))
        print(f"model: {client.model}")
        print(f"request: {request}")
        call = client.call("parse", SMOKE_SYSTEM_PROMPT, request, PARSE_TOOLS)
        print(f"tool: {call.name}")
        print(json.dumps(call.args, indent=2))
        # Explicit checks rather than `assert`, so they still run under `python -O`.
        if not isinstance(call, ToolCall):
            raise AssertionError(f"expected a ToolCall, got {type(call).__name__}")
        if call.name not in PARSE_TOOL_NAMES:
            raise AssertionError(f"tool {call.name!r} is not a parse tool")
        if type(call.args) is not dict:
            raise AssertionError(f"args are a {type(call.args).__name__}, not a plain dict")
        json.dumps(call.args, allow_nan=False)
    except Exception as exc:  # noqa: BLE001 - the smoke test reports every failure
        message = str(exc).replace(api_key, "***")
        print(f"SMOKE FAILED: {type(exc).__name__}: {message}")
        return 1
    print("SMOKE OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(description="PawPal+ LLM client utilities.")
    parser.add_argument(
        "--smoke", action="store_true", help="make one live Gemini parse call and check its shape"
    )
    args = parser.parse_args(argv)
    if not args.smoke:
        print("Usage: python llm_client.py --smoke  (one live Gemini parse call; skips without a key)")
        return 0
    return _smoke()


if __name__ == "__main__":
    sys.exit(main())
