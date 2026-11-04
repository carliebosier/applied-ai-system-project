"""Tests for the tool schemas, the recorded MockClient, and client selection (no network)."""

import json
from datetime import datetime

import pytest

import llm_client
from fixture_owner import build_fixture_owner
from guardrails import validate_request
from llm_client import (
    FIXTURES_PATH,
    PARSE_TOOL_NAMES,
    PARSE_TOOLS,
    REVISE_TOOL_NAMES,
    REVISE_TOOLS,
    MockClient,
    MockReviseError,
    ToolCall,
    UnknownMockRequest,
    get_client,
    normalize_request,
)

NOW = datetime(2026, 11, 2, 7, 0)

REQUESTS = [
    "Biscuit needs meds at 8am, a 30 minute walk this morning, and I only have 45 minutes today.",
    "Today Biscuit needs a 30 minute walk at 8:00 and 5 minutes of meds at 8:15.",
    "I only have 30 minutes today. Biscuit needs a 25 minute walk at 9:00 and Whiskers needs a 20 minute brushing at 17:00.",
    "Rex needs a 20 minute walk at 10:00 today.",
    "Biscuit needs a 20 minute walk today at 9:00 and Whiskers needs a 15 minute bath tomorrow at 10:00.",
    "Biscuit needs a 15 minute walk at 9:00, every 6 hours.",
]


def _fixtures():
    """Load the raw fixture file, independently of MockClient."""
    with open(FIXTURES_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def _fixture(number):
    """Return demo fixture 1–6 by its position in REQUESTS."""
    return _fixtures()[normalize_request(REQUESTS[number - 1])]


def _parse(client, request):
    return client.call("parse", "system prompt", request, PARSE_TOOLS)


def _revise(client):
    return client.call("revise", "system prompt", "plan", REVISE_TOOLS)


def _tool(tools, name):
    return next(tool for tool in tools if tool["name"] == name)


# --- normalize_request --------------------------------------------------------


def test_normalize_request_lowercases_collapses_whitespace_and_strips():
    assert normalize_request("  Rex   needs\ta\n\nWALK.  ") == "rex needs a walk."


# --- fixture file -------------------------------------------------------------


def test_fixture_file_has_the_six_requests_keyed_by_normalized_text():
    fixtures = _fixtures()

    assert [fixture["request"] for fixture in fixtures.values()] == REQUESTS
    for key, fixture in fixtures.items():
        assert key == normalize_request(fixture["request"])
        assert set(fixture) == {"request", "parse", "revise", "expect"}


# --- MockClient parse ---------------------------------------------------------


@pytest.mark.parametrize("request_text", REQUESTS)
def test_each_canonical_request_returns_its_submit_tasks_call(request_text):
    call = _parse(MockClient(), request_text)

    assert isinstance(call, ToolCall)
    assert call.name == "submit_tasks"
    assert call.args == _fixtures()[normalize_request(request_text)]["parse"]


def test_lookup_ignores_capitalization_and_extra_spaces():
    client = MockClient()
    messy = "  rex   NEEDS a 20 minute\twalk at 10:00   TODAY.  "

    assert _parse(client, messy) == _parse(client, REQUESTS[3])


def test_unknown_request_raises_with_every_supported_request():
    with pytest.raises(UnknownMockRequest) as excinfo:
        _parse(MockClient(), "Schedule a vet visit for my goldfish.")

    assert excinfo.value.supported == REQUESTS


def test_mock_client_is_flagged_as_mock():
    assert MockClient().is_mock is True


# --- MockClient revise --------------------------------------------------------


def test_revise_returns_fixture_2s_recorded_move():
    client = MockClient()
    _parse(client, REQUESTS[1])

    call = _revise(client)

    assert call.name == "move_tasks"
    assert call.args["moves"] == [{"ref": "new-2", "time": "08:30"}]
    assert isinstance(call.args["reason"], str) and call.args["reason"]


def test_revise_past_the_recorded_list_raises():
    client = MockClient()
    _parse(client, REQUESTS[1])
    _revise(client)

    with pytest.raises(RuntimeError):
        _revise(client)


def test_revise_for_a_fixture_with_no_recorded_revisions_raises():
    client = MockClient()
    _parse(client, REQUESTS[2])

    with pytest.raises(RuntimeError):
        _revise(client)


def test_revise_before_any_parse_raises():
    with pytest.raises(RuntimeError):
        _revise(MockClient())


def test_a_new_parse_resets_the_revise_counter():
    client = MockClient()
    _parse(client, REQUESTS[1])
    first = _revise(client)

    _parse(client, REQUESTS[1])

    assert _revise(client) == first


def test_a_failed_parse_clears_the_previous_fixture():
    client = MockClient()
    _parse(client, REQUESTS[1])
    with pytest.raises(UnknownMockRequest):
        _parse(client, "Schedule a vet visit for my goldfish.")

    with pytest.raises(MockReviseError):
        _revise(client)


def test_unknown_step_raises():
    with pytest.raises(ValueError):
        MockClient().call("plan", "system prompt", REQUESTS[0], PARSE_TOOLS)


# --- copies -------------------------------------------------------------------


def test_mutating_returned_parse_args_does_not_change_the_next_call():
    client = MockClient()
    first = _parse(client, REQUESTS[1])
    first.args["tasks"][0]["time"] = "23:00"
    first.args["tasks"].clear()
    first.args["extra"] = True

    second = _parse(client, REQUESTS[1])

    assert second.args == _fixture(2)["parse"]


def test_mutating_returned_revise_args_does_not_change_the_next_call():
    client = MockClient()
    _parse(client, REQUESTS[1])
    first = _revise(client)
    first.args["moves"][0]["time"] = "12:00"

    _parse(client, REQUESTS[1])

    assert _revise(client).args["moves"] == [{"ref": "new-2", "time": "08:30"}]


# --- fixtures against the guardrails ------------------------------------------


@pytest.mark.parametrize("number", range(1, 7))
def test_each_fixture_parse_passes_guardrails_as_expected(number):
    fixture = _fixture(number)
    call = _parse(MockClient(), fixture["request"])

    result = validate_request(call.args, fixture["request"], build_fixture_owner(), NOW)

    assert result.status == fixture["expect"]["status"]
    # G9: every quote is copied verbatim from its request.
    assert result.warnings == []


@pytest.mark.parametrize("number", [2, 3])
def test_ok_fixtures_have_no_errors(number):
    fixture = _fixture(number)

    result = validate_request(fixture["parse"], fixture["request"], build_fixture_owner(), NOW)

    assert result.errors == []


def test_fixture_1_yields_exactly_two_questions():
    fixture = _fixture(1)

    result = validate_request(fixture["parse"], fixture["request"], build_fixture_owner(), NOW)

    assert len(result.errors) == 2 == fixture["expect"]["questions"]


def test_fixture_6_keeps_the_users_frequency_and_is_rejected_for_it():
    fixture = _fixture(6)

    result = validate_request(fixture["parse"], fixture["request"], build_fixture_owner(), NOW)

    assert fixture["parse"]["tasks"][0]["frequency"] == "every 6 hours"
    assert any("every 6 hours" in error for error in result.errors)


# --- schemas ------------------------------------------------------------------


def test_tool_names_match_the_exported_sets():
    assert {tool["name"] for tool in PARSE_TOOLS} == PARSE_TOOL_NAMES == {
        "submit_tasks",
        "ask_clarification",
    }
    assert {tool["name"] for tool in REVISE_TOOLS} == REVISE_TOOL_NAMES == {
        "move_tasks",
        "keep_plan",
    }


def test_every_tool_has_an_object_parameter_schema():
    for tool in PARSE_TOOLS + REVISE_TOOLS:
        assert isinstance(tool["description"], str) and tool["description"]
        assert tool["parameters"]["type"] == "object"
        assert set(tool["parameters"]["required"]) <= set(tool["parameters"]["properties"])


def test_frequency_has_no_enum():
    items = _tool(PARSE_TOOLS, "submit_tasks")["parameters"]["properties"]["tasks"]["items"]

    assert "enum" not in items["properties"]["frequency"]


def test_submit_tasks_caps_tasks_and_requires_only_name_description_quote():
    tasks = _tool(PARSE_TOOLS, "submit_tasks")["parameters"]["properties"]["tasks"]

    assert tasks["maxItems"] == 10
    assert tasks["items"]["required"] == ["pet_name", "description", "quote"]


def test_move_tasks_caps_moves_and_revise_tools_have_no_date():
    moves = _tool(REVISE_TOOLS, "move_tasks")["parameters"]["properties"]["moves"]
    assert moves["maxItems"] == 3

    def property_names(schema):
        if isinstance(schema, dict):
            for key, value in schema.items():
                if key == "properties":
                    yield from value
                yield from property_names(value)
        elif isinstance(schema, list):
            for value in schema:
                yield from property_names(value)

    assert "date" not in set(property_names(REVISE_TOOLS))


# --- get_client ---------------------------------------------------------------


@pytest.fixture
def no_dotenv(monkeypatch):
    """Keep a real .env on this machine from changing get_client's result."""
    monkeypatch.setattr(llm_client, "load_dotenv", lambda *args, **kwargs: None)


def test_get_client_returns_mock_when_mode_is_mock(monkeypatch, no_dotenv):
    monkeypatch.setenv("PAWPAL_LLM_MODE", "  Mock ")
    monkeypatch.setenv("GEMINI_API_KEY", "a-real-looking-key")

    client = get_client()

    assert isinstance(client, MockClient)
    assert client.is_mock is True


def test_get_client_returns_mock_when_key_is_unset(monkeypatch, no_dotenv):
    monkeypatch.delenv("PAWPAL_LLM_MODE", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    client = get_client()

    assert isinstance(client, MockClient)
    assert client.is_mock is True


def test_get_client_returns_mock_when_key_is_blank(monkeypatch, no_dotenv):
    monkeypatch.delenv("PAWPAL_LLM_MODE", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "   ")

    assert isinstance(get_client(), MockClient)


def test_get_client_with_a_key_and_no_mode_is_not_implemented_yet(monkeypatch, no_dotenv):
    monkeypatch.delenv("PAWPAL_LLM_MODE", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "a-real-looking-key")

    with pytest.raises(NotImplementedError):
        get_client()


def test_llm_client_does_not_import_the_gemini_sdk():
    source = FIXTURES_PATH.parent.parent.joinpath("llm_client.py").read_text(encoding="utf-8")

    assert "google.genai" not in source and "from google" not in source
