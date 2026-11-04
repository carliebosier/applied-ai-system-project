"""Tests for the tool schemas, MockClient, GeminiClient (fake SDK), client selection and the
smoke-test entry point. No test touches the network."""

import json
import subprocess
import sys
from datetime import datetime
from types import MappingProxyType, SimpleNamespace

import pytest
from google.genai import types

import llm_client
from fixture_owner import build_fixture_owner
from guardrails import validate_request
from llm_client import (
    FIXTURES_PATH,
    PARSE_TOOL_NAMES,
    PARSE_TOOLS,
    REVISE_TOOL_NAMES,
    REVISE_TOOLS,
    GeminiClient,
    LLMConfigError,
    LLMResponseError,
    MockClient,
    MockReviseError,
    ToolCall,
    UnknownMockRequest,
    get_client,
    main,
    normalize_request,
)

PROJECT_ROOT = FIXTURES_PATH.parent.parent

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


class RecordingGeminiClient:
    """Stands in for GeminiClient in get_client tests and records how it was built."""

    instances = []

    def __init__(self, api_key, model, sdk_client=None):
        self.api_key = api_key
        self.model = model
        RecordingGeminiClient.instances.append(self)


def test_get_client_with_a_key_and_no_mode_returns_gemini_client(monkeypatch, no_dotenv):
    RecordingGeminiClient.instances = []
    monkeypatch.setattr(llm_client, "GeminiClient", RecordingGeminiClient)
    monkeypatch.delenv("PAWPAL_LLM_MODE", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "a-real-looking-key")
    monkeypatch.setenv("GEMINI_MODEL", "some-model-id")

    client = get_client()

    assert isinstance(client, RecordingGeminiClient)
    assert RecordingGeminiClient.instances == [client]
    assert (client.api_key, client.model) == ("a-real-looking-key", "some-model-id")


def test_get_client_with_a_blank_model_raises_config_error(monkeypatch, no_dotenv):
    monkeypatch.delenv("PAWPAL_LLM_MODE", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "a-real-looking-key")
    monkeypatch.setenv("GEMINI_MODEL", "   ")

    # The model check runs before any SDK client is built.
    with pytest.raises(LLMConfigError):
        get_client()


def test_blank_model_raises_config_error_even_with_an_sdk_client():
    with pytest.raises(LLMConfigError, match="GEMINI_MODEL"):
        GeminiClient("a-real-looking-key", "  ", sdk_client=FakeSDK(_response()))


def test_importing_llm_client_does_not_import_the_gemini_sdk():
    result = subprocess.run(
        [sys.executable, "-c", "import sys, llm_client; assert 'google.genai' not in sys.modules"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


# --- GeminiClient with a fake SDK ---------------------------------------------

MODEL = "test-model"
SYSTEM_PROMPT = "You are a parser."


class FakeModels:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeSDK:
    """Exposes models.generate_content like google.genai.Client, without any network."""

    def __init__(self, response=None, error=None):
        self.models = FakeModels(response, error)


def _fn_part(name, args):
    return SimpleNamespace(function_call=SimpleNamespace(name=name, args=args), text=None)


def _text_part(text):
    return SimpleNamespace(function_call=None, text=text)


def _response(*parts):
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=list(parts)))])


def _gemini(response=None, error=None):
    sdk = FakeSDK(response, error)
    return GeminiClient("a-real-looking-key", MODEL, sdk_client=sdk), sdk


def _gemini_parse(client, request=REQUESTS[1]):
    return client.call("parse", SYSTEM_PROMPT, request, PARSE_TOOLS)


def test_gemini_client_is_not_mock():
    client, _ = _gemini(_response())

    assert client.is_mock is False
    assert client.model == MODEL


def test_a_single_valid_call_returns_a_tool_call_with_plain_copied_args():
    original = _fixture(2)["parse"]
    client, _ = _gemini(_response(_fn_part("submit_tasks", original)))

    call = _gemini_parse(client)

    assert call == ToolCall("submit_tasks", _fixture(2)["parse"])
    assert call.args is not original
    assert type(call.args) is dict
    assert type(call.args["tasks"]) is list and type(call.args["tasks"][0]) is dict
    call.args["tasks"][0]["time"] = "23:00"
    assert original["tasks"][0]["time"] == "08:00"


def test_the_request_is_forced_function_calling_at_temperature_zero():
    client, sdk = _gemini(_response(_fn_part("submit_tasks", {"tasks": []})))

    _gemini_parse(client, "Biscuit needs a walk.")

    [kwargs] = sdk.models.calls
    config = kwargs["config"]
    assert kwargs["model"] == MODEL
    assert kwargs["contents"] == "Biscuit needs a walk."
    assert isinstance(config, types.GenerateContentConfig)
    assert config.temperature == 0
    assert config.system_instruction == SYSTEM_PROMPT
    calling = config.tool_config.function_calling_config
    assert calling.mode == types.FunctionCallingConfigMode.ANY
    assert set(calling.allowed_function_names) == PARSE_TOOL_NAMES
    assert config.automatic_function_calling.disable is True
    [tool] = config.tools
    assert {fd.name for fd in tool.function_declarations} == PARSE_TOOL_NAMES


def test_revise_step_allows_only_the_revise_tools():
    client, sdk = _gemini(_response(_fn_part("keep_plan", {"reason": "nothing to move"})))

    call = client.call("revise", SYSTEM_PROMPT, "plan", REVISE_TOOLS)

    assert call == ToolCall("keep_plan", {"reason": "nothing to move"})
    calling = sdk.models.calls[0]["config"].tool_config.function_calling_config
    assert set(calling.allowed_function_names) == REVISE_TOOL_NAMES


def test_a_text_only_response_raises_and_reports_the_text():
    client, _ = _gemini(_response(_text_part("Sure! " + "x" * 500)))

    with pytest.raises(LLMResponseError, match="got 0") as excinfo:
        _gemini_parse(client)

    assert "Sure! " in str(excinfo.value)
    assert "x" * 201 not in str(excinfo.value)


def test_two_function_calls_raise():
    client, _ = _gemini(
        _response(
            _fn_part("submit_tasks", {"tasks": []}),
            _fn_part("ask_clarification", {"question": "Which pet?"}),
        )
    )

    with pytest.raises(LLMResponseError, match="got 2"):
        _gemini_parse(client)


def test_an_unknown_tool_name_raises():
    client, _ = _gemini(_response(_fn_part("move_tasks", {"moves": [], "reason": "x"})))

    with pytest.raises(LLMResponseError, match="move_tasks"):
        _gemini_parse(client)


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(candidates=[]),
        SimpleNamespace(candidates=None),
        SimpleNamespace(),
        SimpleNamespace(candidates=[SimpleNamespace(content=None)]),
        SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=None))]),
    ],
)
def test_missing_candidates_content_or_parts_raise_response_error(response):
    client, _ = _gemini(response)

    with pytest.raises(LLMResponseError):
        _gemini_parse(client)


def test_mapping_args_become_plain_dicts():
    task = MappingProxyType({"pet_name": "Biscuit", "description": "walk", "quote": "walk"})
    args = MappingProxyType({"tasks": (task,), "available_minutes": 45.0})
    client, _ = _gemini(_response(_fn_part("submit_tasks", args)))

    call = _gemini_parse(client)

    assert type(call.args) is dict
    assert type(call.args["tasks"]) is list
    assert type(call.args["tasks"][0]) is dict
    # Whole floats are left for the guardrails, not coerced here.
    assert call.args["available_minutes"] == 45.0 and type(call.args["available_minutes"]) is float
    json.dumps(call.args)


def test_sdk_exceptions_propagate_unchanged():
    error = ConnectionError("quota exceeded")
    client, _ = _gemini(error=error)

    with pytest.raises(ConnectionError) as excinfo:
        _gemini_parse(client)

    assert excinfo.value is error


def test_gemini_unknown_step_raises_value_error():
    client, sdk = _gemini(_response(_fn_part("submit_tasks", {"tasks": []})))

    with pytest.raises(ValueError):
        client.call("plan", SYSTEM_PROMPT, REQUESTS[0], PARSE_TOOLS)

    assert sdk.models.calls == []


# --- main / smoke test --------------------------------------------------------


@pytest.fixture
def smoke_env(monkeypatch, no_dotenv):
    monkeypatch.delenv("PAWPAL_LLM_MODE", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "secret-key-123")
    monkeypatch.setenv("GEMINI_MODEL", "some-model-id")


def _fake_gemini_class(result=None, error=None):
    class FakeGemini:
        def __init__(self, api_key, model, sdk_client=None):
            self.model = model
            self.calls = []

        def call(self, step, system_prompt, user_text, tools):
            self.calls.append((step, system_prompt, user_text, tools))
            if error is not None:
                raise error
            return result

    return FakeGemini


def test_main_without_smoke_prints_usage_and_returns_0(capsys):
    assert main([]) == 0
    assert "--smoke" in capsys.readouterr().out


def test_smoke_skips_in_mock_mode(monkeypatch, smoke_env, capsys):
    monkeypatch.setenv("PAWPAL_LLM_MODE", " MOCK ")

    assert main(["--smoke"]) == 0
    assert capsys.readouterr().out.startswith("SKIP:")


def test_smoke_skips_with_a_blank_key(monkeypatch, smoke_env, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", "  ")

    assert main(["--smoke"]) == 0
    assert capsys.readouterr().out.startswith("SKIP:")


def test_smoke_passes_with_a_valid_tool_call(monkeypatch, smoke_env, capsys):
    result = ToolCall("submit_tasks", {"tasks": [{"pet_name": "Biscuit"}]})
    monkeypatch.setattr(llm_client, "GeminiClient", _fake_gemini_class(result=result))

    assert main(["--smoke"]) == 0

    out = capsys.readouterr().out
    assert "model: some-model-id" in out
    assert "submit_tasks" in out
    assert out.rstrip().endswith("SMOKE OK")
    assert "secret-key-123" not in out


def test_smoke_fails_when_the_client_raises(monkeypatch, smoke_env, capsys):
    error = RuntimeError("bad model secret-key-123")
    monkeypatch.setattr(llm_client, "GeminiClient", _fake_gemini_class(error=error))

    assert main(["--smoke"]) == 1

    out = capsys.readouterr().out
    assert "SMOKE FAILED: RuntimeError:" in out
    assert "secret-key-123" not in out


def test_smoke_fails_on_a_non_parse_tool(monkeypatch, smoke_env, capsys):
    result = ToolCall("keep_plan", {"reason": "x"})
    monkeypatch.setattr(llm_client, "GeminiClient", _fake_gemini_class(result=result))

    assert main(["--smoke"]) == 1
    assert "SMOKE FAILED" in capsys.readouterr().out


def test_smoke_fails_with_a_blank_model(monkeypatch, smoke_env, capsys):
    monkeypatch.setenv("GEMINI_MODEL", "")

    assert main(["--smoke"]) == 1
    assert "SMOKE FAILED: LLMConfigError:" in capsys.readouterr().out
