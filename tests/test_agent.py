"""Tests for agent.run_agent (parse + validate + plan, no revision) and agent.setup_logging.

No test touches the network: runs use MockClient or a small scripted FakeClient.
"""

import json
import logging
from datetime import date, datetime
from pathlib import Path

import pytest

import agent
from agent import AgentResult, run_agent, setup_logging
from fixture_owner import build_fixture_owner
from guardrails import validate_request
from llm_client import FIXTURES_PATH, LLMConfigError, LLMResponseError, MockClient, ToolCall

NOW = datetime(2026, 11, 2, 7, 0)

with open(FIXTURES_PATH, encoding="utf-8") as _handle:
    _FIXTURES = list(json.load(_handle).values())
REQUESTS = [fixture["request"] for fixture in _FIXTURES]
F1, F2, F3, F4, F5, F6 = REQUESTS

STEPS = ["request", "parse_output", "validation", "plan", "stop"]


class FakeClient:
    """Returns scripted ToolCalls (or raises scripted exceptions) and records every call."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def call(self, step, system_prompt, user_text, tools):
        self.calls.append(
            {"step": step, "system_prompt": system_prompt, "user_text": user_text, "tools": tools}
        )
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.fixture(autouse=True)
def _reset_agent_logger():
    """Remove any handlers setup_logging added, so tests don't leak log files into each other."""
    yield
    for handler in _setup_handlers():
        logging.getLogger(agent.LOGGER_NAME).removeHandler(handler)
        handler.close()


def _setup_handlers():
    """Handlers added by setup_logging (pytest attaches its own capture handlers too)."""
    log = logging.getLogger(agent.LOGGER_NAME)
    return [handler for handler in log.handlers if getattr(handler, agent._HANDLER_TAG, False)]


def _snapshot(owner):
    """Everything about an owner that a run could change."""
    return (
        owner.available_time_minutes,
        [(pet.pet_id, pet.name, [vars(task).copy() for task in pet.tasks]) for pet in owner.pets],
    )


def _run(request, client=None, owner=None):
    owner = owner if owner is not None else build_fixture_owner()
    return run_agent(request, owner, NOW, client if client is not None else MockClient())


def _ids(tasks):
    return [task.task_id for task in tasks]


def _submit(tasks, **extra):
    return ToolCall("submit_tasks", {"tasks": tasks, **extra})


def _task(**fields):
    task = {
        "pet_name": "Biscuit",
        "description": "walk",
        "quote": "walk",
        "date": "today",
        "time": "08:00",
        "duration_minutes": 30,
    }
    task.update(fields)
    return task


# ---------------------------------------------------------------------------
# Fixtures through MockClient
# ---------------------------------------------------------------------------


def test_fixture_2_plans_both_new_tasks_with_one_conflict():
    result = _run(F2)

    assert isinstance(result, AgentResult)
    assert result.status == "ok"
    assert [(pet_id, ref) for pet_id, ref, _ in result.new_tasks] == [
        ("p-biscuit", "new-1"),
        ("p-biscuit", "new-2"),
    ]
    walk, meds = (task for _, _, task in result.new_tasks)
    assert (walk.description, walk.time) == ("walk", datetime(2026, 11, 2, 8, 0))
    assert (meds.description, meds.time) == ("meds", datetime(2026, 11, 2, 8, 15))

    plan = result.original_plan
    assert _ids(plan["scheduled"]) == ["new-1", "new-2"]
    assert plan["deferred"] == []
    assert [(a.task_id, b.task_id) for a, b in plan["conflicts"]] == [("new-1", "new-2")]

    assert result.plan_date == date(2026, 11, 2)
    assert result.budget is None
    assert result.errors == []
    assert result.revised_plan is None
    assert result.revision_moves == []
    assert result.stop_reason is None
    assert result.revise_calls == 0
    assert result.supported_requests == []


def test_fixture_2_plans_at_end_of_request_day():
    result = _run(F2)
    plan_entry = next(entry for entry in result.trace if entry["step"] == "plan")
    assert plan_entry["plan_time"] == "2026-11-02T23:59:00"


def test_task_later_today_is_planned_even_though_not_due_at_now():
    """A 20:00 task isn't due at 07:00, so it's only scheduled because the plan runs at 23:59."""
    request = "Biscuit needs a 30 minute walk at 20:00."
    client = FakeClient(_submit([_task(time="20:00", quote="a 30 minute walk at 20:00")]))

    result = _run(request, client)

    assert result.status == "ok"
    (_, _, task), = result.new_tasks
    assert not task.is_due(NOW)
    assert _ids(result.original_plan["scheduled"]) == ["new-1"]


def test_fixture_3_defers_brushing_under_30_minute_budget():
    result = _run(F3)

    assert result.status == "ok"
    assert result.budget == 30
    plan = result.original_plan
    assert [task.description for task in plan["scheduled"]] == ["walk"]
    assert [task.description for task in plan["deferred"]] == ["brushing"]
    assert plan["conflicts"] == []
    plan_entry = next(entry for entry in result.trace if entry["step"] == "plan")
    assert plan_entry["budget"] == 30
    assert plan_entry["budget_used"] == 25


@pytest.mark.parametrize("request_text", [F1, F4, F5, F6])
def test_invalid_fixtures_need_clarification_and_change_nothing(request_text):
    owner = build_fixture_owner()
    before = _snapshot(owner)

    result = _run(request_text, owner=owner)

    assert result.status == "needs_clarification"
    assert result.new_tasks == []
    assert result.original_plan is None
    assert result.plan_date is None
    assert result.errors
    assert _snapshot(owner) == before


def test_fixture_1_asks_exactly_two_questions():
    result = _run(F1)
    assert result.status == "needs_clarification"
    assert len(result.errors) == 2


def test_ok_run_leaves_real_owner_untouched():
    owner = build_fixture_owner()
    before = _snapshot(owner)

    result = _run(F2, owner=owner)

    assert result.status == "ok"
    assert owner.get_all_tasks() == []
    assert owner.available_time_minutes == 120
    assert _snapshot(owner) == before


def test_ok_run_with_budget_leaves_real_budget_untouched():
    owner = build_fixture_owner()
    result = _run(F3, owner=owner)
    assert result.budget == 30
    assert owner.available_time_minutes == 120
    assert owner.get_all_tasks() == []


def test_draft_tasks_are_not_the_validation_tasks():
    result = _run(F2)
    returned = {ref: task for _, ref, task in result.new_tasks}
    for planned in result.original_plan["scheduled"]:
        assert planned is not returned[planned.task_id]
        assert planned == returned[planned.task_id]  # same fields, different objects


def test_ok_run_passes_through_assumptions_and_warnings():
    fixture = _FIXTURES[1]
    expected = validate_request(fixture["parse"], F2, build_fixture_owner(), NOW)

    result = _run(F2)

    assert result.assumptions == expected.assumptions
    assert len(result.assumptions) == 2  # no frequency on either task
    assert result.warnings == expected.warnings


def test_quote_mismatch_warning_reaches_result():
    request = "Biscuit needs a 30 minute walk at 8:00."
    client = FakeClient(_submit([_task(quote="something the user never said")]))
    result = _run(request, client)
    assert result.status == "ok"
    assert len(result.warnings) == 1
    assert result.warnings[0].startswith("⚠")


def test_unknown_mock_request_lists_supported_requests():
    owner = build_fixture_owner()
    result = _run("Feed the goldfish at noon.", owner=owner)

    assert result.status == "mock_unknown_request"
    assert sorted(result.supported_requests) == sorted(REQUESTS)
    assert len(result.supported_requests) == 6
    assert result.new_tasks == []
    assert result.original_plan is None
    assert owner.get_all_tasks() == []


# ---------------------------------------------------------------------------
# Scripted clients: clarification, bad output and errors
# ---------------------------------------------------------------------------


def test_ask_clarification_returns_the_question():
    client = FakeClient(ToolCall("ask_clarification", {"question": "Which pet do you mean?"}))
    result = _run("do the thing", client)
    assert result.status == "needs_clarification"
    assert result.errors == ["Which pet do you mean?"]
    assert result.original_plan is None


@pytest.mark.parametrize("args", [{"question": ""}, {"question": "   "}, {}, {"question": 3}])
def test_ask_clarification_without_a_question_is_bad_output(args):
    result = _run("do the thing", FakeClient(ToolCall("ask_clarification", args)))
    assert result.status == "llm_bad_output"
    assert result.errors


def test_unknown_tool_name_is_bad_output():
    result = _run(F2, FakeClient(ToolCall("move_tasks", {"moves": [], "reason": "x"})))
    assert result.status == "llm_bad_output"
    assert result.new_tasks == []
    assert result.original_plan is None


def test_llm_response_error_is_bad_output():
    owner = build_fixture_owner()
    result = _run(F2, FakeClient(LLMResponseError("text instead of a tool call")), owner)
    assert result.status == "llm_bad_output"
    assert "text instead of a tool call" in result.errors[0]
    assert owner.get_all_tasks() == []


@pytest.mark.parametrize("exc", [RuntimeError("boom"), LLMConfigError("GEMINI_MODEL is not set")])
def test_client_exception_is_llm_error_and_owner_unchanged(exc):
    owner = build_fixture_owner()
    before = _snapshot(owner)

    result = _run(F2, FakeClient(exc), owner)

    assert result.status == "llm_error"
    assert len(result.errors) == 1
    assert type(exc).__name__ in result.errors[0]
    assert result.new_tasks == []
    assert result.original_plan is None
    assert _snapshot(owner) == before
    assert result.trace[-1]["step"] == "stop"
    assert result.trace[-1]["status"] == "llm_error"


def test_long_exception_message_is_truncated():
    result = _run(F2, FakeClient(RuntimeError("x" * 5000)))
    assert len(result.errors[0]) <= 300


def test_submit_tasks_missing_quote_is_bad_output():
    task = _task()
    del task["quote"]
    owner = build_fixture_owner()
    result = _run("Biscuit needs a 30 minute walk at 8:00.", FakeClient(_submit([task])), owner)

    assert result.status == "llm_bad_output"
    assert result.errors
    assert result.new_tasks == []
    assert result.original_plan is None
    assert owner.get_all_tasks() == []


# ---------------------------------------------------------------------------
# Prompt contract
# ---------------------------------------------------------------------------


def test_parse_call_gets_prompt_context_and_raw_request():
    request = "  Biscuit needs a 30 minute walk at 8:00.  "
    client = FakeClient(_submit([_task(quote="a 30 minute walk at 8:00")]))

    _run(request, client)

    (call,) = client.calls
    assert call["step"] == "parse"
    assert call["user_text"] == request
    prompt = call["system_prompt"]
    assert "2026-11-02" in prompt
    assert "Biscuit" in prompt
    assert "Whiskers" in prompt
    assert "120" in prompt
    assert "Omit any field the user did not state" in prompt
    assert "Never invent a time, duration or pet" in prompt
    assert "Copy the frequency as the user said it" in prompt
    assert {tool["name"] for tool in call["tools"]} == {"submit_tasks", "ask_clarification"}


# ---------------------------------------------------------------------------
# Logging and trace
# ---------------------------------------------------------------------------


def _read_log(path: Path):
    """Parse each log line into (run_id, step, payload)."""
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        run_part, step_part, payload = line.split(" ", 2)
        assert run_part.startswith("run=")
        assert step_part.startswith("step=")
        entries.append((run_part[4:], step_part[5:], json.loads(payload)))
    return entries


def test_log_lines_match_trace(tmp_path):
    log_path = tmp_path / "logs" / "agent.log"
    setup_logging(log_path=str(log_path))

    result = _run(F2)

    entries = _read_log(log_path)
    assert [step for _, step, _ in entries] == STEPS
    assert {run_id for run_id, _, _ in entries} == {result.run_id}
    assert [entry["step"] for entry in result.trace] == STEPS
    for (_, step, payload), entry in zip(entries, result.trace):
        assert {"step": step, **payload} == entry
    assert result.trace[-1]["status"] == "ok"
    assert result.trace[-1]["stop_reason"] is None
    assert result.trace[-1]["revise_calls"] == 0


def test_setup_logging_twice_does_not_duplicate_lines(tmp_path):
    log_path = tmp_path / "agent.log"
    setup_logging(log_path=str(log_path))
    setup_logging(log_path=str(log_path))
    assert len(_setup_handlers()) == 1

    _run(F2)

    assert [step for _, step, _ in _read_log(log_path)] == STEPS


def test_setup_logging_with_console(tmp_path, capsys):
    setup_logging(console=True, log_path=str(tmp_path / "agent.log"))
    result = _run(F4)
    err = capsys.readouterr().err
    assert f"run={result.run_id} step=stop" in err


def test_running_without_setup_logging_does_not_raise():
    assert _setup_handlers() == []
    result = _run(F2)
    assert result.status == "ok"
    assert [entry["step"] for entry in result.trace] == STEPS


def test_failure_trace_ends_with_stop():
    result = _run(F1)
    assert [entry["step"] for entry in result.trace] == [
        "request",
        "parse_output",
        "validation",
        "stop",
    ]
    assert result.trace[-1]["status"] == "needs_clarification"


def test_run_ids_are_short_and_distinct():
    first, second = _run(F2), _run(F2)
    assert len(first.run_id) == 8
    assert first.run_id != second.run_id


# ---------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------


def test_agent_source_has_no_clock_env_or_sdk_access():
    source = Path(agent.__file__).read_text(encoding="utf-8")
    assert "datetime.now" not in source
    assert "os.environ" not in source
    assert "google.genai" not in source
    assert "from google" not in source
    assert "import os" not in source
