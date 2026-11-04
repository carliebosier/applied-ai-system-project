"""Tests for agent.run_agent (parse, validate, plan and the revision loop) and agent.setup_logging.

No test touches the network: runs use MockClient or a small scripted FakeClient.
"""

import copy
import json
import logging
from datetime import date, datetime
from pathlib import Path

import pytest

import agent
from agent import (
    REVISE_SYSTEM_PROMPT,
    AgentResult,
    better,
    build_revise_text,
    gate_stop,
    movable,
    run_agent,
    setup_logging,
    validate_moves,
)
from fixture_owner import build_fixture_owner
from guardrails import validate_request
from llm_client import FIXTURES_PATH, LLMConfigError, LLMResponseError, MockClient, ToolCall
from pawpal_system import Task

NOW = datetime(2026, 11, 2, 7, 0)

with open(FIXTURES_PATH, encoding="utf-8") as _handle:
    _FIXTURES = list(json.load(_handle).values())
REQUESTS = [fixture["request"] for fixture in _FIXTURES]
F1, F2, F3, F4, F5, F6 = REQUESTS

# Fixture 2 now makes one accepted revision before stopping (increment 6).
STEPS = [
    "request",
    "parse_output",
    "validation",
    "plan",
    "revision_proposal",
    "revision_validation",
    "revised_plan",
    "stop",
]


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


def _run(request, client=None, owner=None, **kwargs):
    owner = owner if owner is not None else build_fixture_owner()
    return run_agent(request, owner, NOW, client if client is not None else MockClient(), **kwargs)


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
    # The walk/meds overlap involves new tasks, so the loop now runs once and fixes it
    # (these four assertions were None / [] / None / 0 before increment 6).
    assert result.stop_reason == "clean"
    assert result.revise_calls == 1
    assert result.revision_moves == [
        {
            "iteration": 1,
            "ref": "new-2",
            "from": "08:15",
            "to": "08:30",
            "reason": _FIXTURES[1]["revise"][0]["args"]["reason"],
        }
    ]
    revised = result.revised_plan
    assert revised["conflicts"] == []
    assert revised["deferred"] == []
    assert [(ref, task.time) for _, ref, task in result.revised_new_tasks] == [
        ("new-1", datetime(2026, 11, 2, 8, 0)),
        ("new-2", datetime(2026, 11, 2, 8, 30)),
    ]
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
    # Deferred only, no overlap: the gate stops before any revise call (increment 6).
    assert result.stop_reason == "no_movable_conflicts"
    assert result.revise_calls == 0
    assert result.revised_plan is None


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
    assert result.revised_plan is not None  # a revision was accepted on the draft, not the owner
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
    # Fixture 2 now ends clean after one revise call (was None / 0 before increment 6).
    assert result.trace[-1]["stop_reason"] == "clean"
    assert result.trace[-1]["revise_calls"] == 1


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
    # A clarification never reaches the loop, so it has no stop_reason.
    assert result.stop_reason is None
    assert result.trace[-1]["stop_reason"] is None


def test_run_ids_are_short_and_distinct():
    first, second = _run(F2), _run(F2)
    assert len(first.run_id) == 8
    assert first.run_id != second.run_id


# ---------------------------------------------------------------------------
# Revision loop: helpers and scenarios
# ---------------------------------------------------------------------------

REQ = "Scripted request for the revision tests."
KEEP = ToolCall("keep_plan", {"reason": "I can't remove the overlap."})


def _at(hhmm):
    hour, minute = map(int, hhmm.split(":"))
    return datetime(2026, 11, 2, hour, minute)


def _existing(task_id, description, hhmm, minutes):
    """A pre-existing task; its id is never of the new-N form."""
    return Task(
        task_id=task_id,
        description=description,
        time=_at(hhmm),
        frequency="as needed",
        duration_minutes=minutes,
    )


def _owner_with(*tasks):
    owner = build_fixture_owner()
    for task in tasks:
        owner.pets[0].add_task(task)
    return owner


def _new(description, hhmm, minutes):
    return _task(description=description, time=hhmm, duration_minutes=minutes, quote=description)


def _moves(*pairs, reason="Spread the tasks out."):
    return ToolCall(
        "move_tasks", {"moves": [{"ref": ref, "time": at} for ref, at in pairs], "reason": reason}
    )


def _f2_parse():
    return ToolCall("submit_tasks", copy.deepcopy(_FIXTURES[1]["parse"]))


def _pairs(plan):
    return [(first.task_id, second.task_id) for first, second in plan["conflicts"]]


def _times(tasks):
    return {ref: task.time.strftime("%H:%M") for _, ref, task in tasks}


def _revise_calls(client):
    return [call for call in client.calls if call["step"] == "revise"]


# new-1 a 08:00/30, new-2 b 08:15/5, new-3 c 09:00/30, new-4 d 09:10/5: two movable pairs.
ABCD = [_new("a", "08:00", 30), _new("b", "08:15", 5), _new("c", "09:00", 30), _new("d", "09:10", 5)]
# Plus new-5 e 10:00/30 and new-6 f 10:15/5: three movable pairs.
ABCDEF = ABCD + [_new("e", "10:00", 30), _new("f", "10:15", 5)]


# better(), gate_stop() and movable() as pure functions


def _counts(conflicts, deferred):
    return {"scheduled": [], "conflicts": [object()] * conflicts, "deferred": [object()] * deferred}


@pytest.mark.parametrize(
    "new, old, expected",
    [
        ((0, 1), (1, 1), True),  # fewer conflicts, equal deferred
        ((0, 2), (1, 1), False),  # fewer conflicts but more deferred
        ((1, 0), (1, 1), False),  # deferred-only drop is never an improvement (R4 loophole)
        ((2, 0), (1, 0), False),  # more conflicts
    ],
)
def test_better_is_conflict_first(new, old, expected):
    assert better(_counts(*new), _counts(*old)) is expected


def test_gate_stop():
    assert gate_stop(_counts(0, 0)) == "clean"
    assert gate_stop(_counts(0, 1)) == "no_movable_conflicts"
    assert gate_stop(_counts(1, 0)) == "no_movable_conflicts"


def test_movable_matches_new_tasks_by_task_id_only():
    new_walk = _existing("new-1", "walk", "08:00", 30)
    twin = _existing("task-abc", "walk", "08:00", 30)  # same fields as new-1 except task_id
    other = _existing("task-def", "meds", "08:15", 5)
    plan = {"scheduled": [], "deferred": [], "conflicts": [(twin, other), (new_walk, other)]}

    assert movable(plan, {"new-1"}) == [(new_walk, other)]
    # A deep copy (equal fields, a different object) is still recognized by its task_id.
    copied = copy.deepcopy(new_walk)
    assert movable({**plan, "conflicts": [(copied, other)]}, {"new-1"}) == [(copied, other)]
    assert movable(plan, set()) == []


# validate_moves (R1-R3)

NEW_IDS = {"new-1", "new-2"}


def _moves_draft():
    return _owner_with(
        _existing("new-1", "walk", "08:00", 30),
        _existing("new-2", "meds", "08:15", 5),
        _existing("task-abc", "grooming", "12:00", 30),
    )


def _args(*pairs, reason="Spread out."):
    return _moves(*pairs, reason=reason).args


def test_validate_moves_accepts_a_same_day_move_on_the_tasks_date():
    moves, error = validate_moves(_args(("new-2", "08:30")), _moves_draft(), NEW_IDS)
    assert error is None
    assert moves == [("new-2", _at("08:30"))]


@pytest.mark.parametrize(
    "args, rule",
    [
        (_args(("task-abc", "13:00")), "R1"),  # a pre-existing task's id
        (_args(("new-2", "08:30"), ("new-2", "08:40")), "R1"),  # repeated ref
        (_args(*[("new-1", f"1{n}:00") for n in range(4)]), "R1"),  # 4 moves
        (_args(), "R1"),  # empty moves
        (_args(("new-2", "25:00")), "R2"),  # not an HH:MM time
        (_args(("new-1", "23:45")), "R2"),  # 30 minutes from 23:45 crosses midnight
        (_args(("new-1", "23:30")), "R2"),  # ends exactly at 24:00, which is the next day
        (_args(("new-1", "23:31")), "R2"),
        (_args(("new-1", "08:00")), "R3"),  # same time as now
        ({"moves": [{"ref": "new-2", "time": "08:30"}]}, "shape"),  # missing reason
        (_args(("new-2", "08:30"), reason="   "), "shape"),  # blank reason
    ],
)
def test_validate_moves_rejects(args, rule):
    moves, error = validate_moves(args, _moves_draft(), NEW_IDS)
    assert moves is None
    assert error.startswith(rule), error


def test_validate_moves_counts_moves_before_checking_refs():
    four = _args(("new-1", "10:00"), ("new-2", "11:00"), ("new-3", "12:00"), ("new-4", "13:00"))
    ids = {"new-1", "new-2", "new-3", "new-4"}
    moves, error = validate_moves(four, _moves_draft(), ids)
    assert moves is None
    assert error == "R1: expected 1 to 3 moves, got 4"


def test_validate_moves_allows_ending_at_23_59():
    moves, error = validate_moves(_args(("new-1", "23:29")), _moves_draft(), NEW_IDS)
    assert error is None
    assert moves == [("new-1", _at("23:29"))]


# Full runs through the loop


def test_fixture_2_revision_keeps_owner_and_returned_tasks_untouched():
    owner = build_fixture_owner()
    before = _snapshot(owner)

    result = _run(F2, owner=owner)

    assert result.stop_reason == "clean"
    assert _snapshot(owner) == before
    assert _pairs(result.original_plan) == [("new-1", "new-2")]
    assert _times(result.new_tasks) == {"new-1": "08:00", "new-2": "08:15"}
    returned = {ref: task for _, ref, task in result.new_tasks}
    for pet_id, ref, revised in result.revised_new_tasks:
        assert pet_id == "p-biscuit"
        assert revised is not returned[ref]
        assert all(revised is not planned for planned in result.revised_plan["scheduled"])


def test_revise_prompt_for_fixture_2_has_the_exact_layout():
    client = FakeClient(_f2_parse(), _moves(("new-2", "08:30")))
    _run(F2, client)

    (call,) = _revise_calls(client)
    assert call["system_prompt"] == REVISE_SYSTEM_PROMPT
    assert {tool["name"] for tool in call["tools"]} == {"move_tasks", "keep_plan"}
    assert call["user_text"] == (
        "Date: 2026-11-02\n"
        "Tasks:\n"
        "- new-1 | walk | 08:00-08:30 (30 min) | movable\n"
        "- new-2 | meds | 08:15-08:20 (5 min) | movable\n"
        "Overlaps to remove:\n"
        "- new-1 (walk, 08:00-08:30) overlaps new-2 (meds, 08:15-08:20)"
    )
    assert "budget" not in call["user_text"].lower()


def test_revise_prompt_hides_budget_and_deferred_tasks_outside_movable_pairs():
    # Budget 40: a (30) and b (5) fit, brushing (20) is deferred and overlaps nothing.
    tasks = [_new("a", "08:00", 30), _new("b", "08:15", 5), _new("brushing", "17:00", 20)]
    client = FakeClient(_submit(tasks, available_minutes=40), KEEP)

    result = _run(REQ, client)

    assert [task.description for task in result.original_plan["deferred"]] == ["brushing"]
    (call,) = _revise_calls(client)
    assert "brushing" not in call["user_text"]
    assert "budget" not in call["user_text"].lower()
    assert "40" not in call["user_text"]
    assert result.stop_reason == "model_kept_plan"


def test_revise_prompt_lists_only_the_movable_pair():
    owner = _owner_with(
        _existing("task-abc", "grooming", "12:00", 30),
        _existing("task-def", "ear drops", "12:10", 5),
    )
    client = FakeClient(_submit([_new("a", "08:00", 30), _new("b", "08:15", 5)]), KEEP)

    result = _run(REQ, client, owner)

    assert _pairs(result.original_plan) == [("new-1", "new-2"), ("task-abc", "task-def")]
    (call,) = _revise_calls(client)
    overlaps = call["user_text"].split("Overlaps to remove:\n", 1)[1]
    assert overlaps.splitlines() == ["- new-1 (a, 08:00-08:30) overlaps new-2 (b, 08:15-08:20)"]
    assert "grooming" not in overlaps
    assert "ear drops" not in overlaps
    # Fixed tasks still appear as blocks, by description and without a ref.
    assert "- grooming | 12:00-12:30 (30 min) | fixed" in call["user_text"]


def test_no_improvement_discards_the_candidate():
    owner = _owner_with(_existing("task-abc", "grooming", "09:00", 30))
    # Moving b to 09:10 clears a/b but creates b/grooming: 1 conflict -> 1 conflict.
    client = FakeClient(
        _submit([_new("a", "08:00", 30), _new("b", "08:15", 5)]), _moves(("new-2", "09:10"))
    )

    result = _run(REQ, client, owner)

    assert result.status == "ok"
    assert result.stop_reason == "no_improvement"
    assert result.revise_calls == 1
    assert result.revised_plan is None
    assert result.revised_new_tasks == []
    assert result.revision_moves == []
    assert _pairs(result.original_plan) == [("new-1", "new-2")]
    revised_entry = next(entry for entry in result.trace if entry["step"] == "revised_plan")
    assert revised_entry["accepted"] is False
    assert (revised_entry["conflicts_before"], revised_entry["conflicts_after"]) == (1, 1)
    assert revised_entry["conflicts"] == [["task-abc", "new-2"]]
    assert result.trace[-1]["stop_reason"] == "no_improvement"


@pytest.mark.parametrize(
    "proposal",
    [
        _moves(("task-abc", "13:00")),  # bad ref: a pre-existing task
        _moves(("new-1", "23:45")),  # crosses midnight
        ToolCall("drop_tasks", {"refs": ["new-2"]}),  # not a revise tool
    ],
    ids=["bad-ref", "midnight", "unknown-tool"],
)
def test_invalid_revision_keeps_the_original_plan(proposal):
    owner = _owner_with(_existing("task-abc", "grooming", "12:00", 30))
    client = FakeClient(_f2_parse(), proposal)

    result = _run(F2, client, owner)

    assert result.status == "ok"
    assert result.stop_reason == "invalid_revision"
    assert result.revise_calls == 1
    assert result.revised_plan is None
    assert result.revision_moves == []
    assert _pairs(result.original_plan) == [("new-1", "new-2")]
    assert _times(result.new_tasks) == {"new-1": "08:00", "new-2": "08:15"}
    steps = [entry["step"] for entry in result.trace]
    assert "revision_proposal" in steps
    assert "revised_plan" not in steps


def test_model_kept_plan():
    result = _run(F2, FakeClient(_f2_parse(), KEEP))
    assert (result.status, result.stop_reason, result.revise_calls) == ("ok", "model_kept_plan", 1)
    assert result.revised_plan is None


def test_two_accepted_revisions_end_clean_not_max_iterations():
    owner = build_fixture_owner()
    before = _snapshot(owner)
    client = FakeClient(
        _submit(ABCD), _moves(("new-2", "08:30")), _moves(("new-4", "09:30"))
    )

    result = _run(REQ, client, owner)

    assert result.status == "ok"
    assert result.stop_reason == "clean"
    assert result.revise_calls == 2
    assert [(move["iteration"], move["ref"]) for move in result.revision_moves] == [
        (1, "new-2"),
        (2, "new-4"),
    ]
    assert result.revised_plan["conflicts"] == []
    assert _pairs(result.original_plan) == [("new-1", "new-2"), ("new-3", "new-4")]
    assert _times(result.revised_new_tasks) == {
        "new-1": "08:00", "new-2": "08:30", "new-3": "09:00", "new-4": "09:30",
    }
    assert _snapshot(owner) == before
    # The second prompt shows the first accepted move and only the remaining overlap.
    second = _revise_calls(client)[1]["user_text"]
    assert "- new-2 | b | 08:30-08:35 (5 min) | movable" in second
    assert second.endswith("Overlaps to remove:\n- new-3 (c, 09:00-09:30) overlaps new-4 (d, 09:10-09:15)")


def test_cap_hit_with_movable_conflicts_left_is_max_iterations():
    client = FakeClient(
        _submit(ABCDEF), _moves(("new-2", "08:30")), _moves(("new-4", "09:30"))
    )

    result = _run(REQ, client)

    assert result.status == "ok"
    assert result.stop_reason == "max_iterations"
    assert result.revise_calls == 2
    assert _pairs(result.revised_plan) == [("new-5", "new-6")]
    assert movable(result.revised_plan, {f"new-{n}" for n in range(1, 7)}) != []
    assert _times(result.revised_new_tasks) == {
        "new-1": "08:00", "new-2": "08:30", "new-3": "09:00",
        "new-4": "09:30", "new-5": "10:00", "new-6": "10:15",
    }
    assert len(result.revision_moves) == 2


def test_max_revisions_zero_never_calls_revise():
    client = FakeClient(_f2_parse())
    result = _run(F2, client, max_revisions=0)

    assert result.status == "ok"
    assert result.stop_reason == "max_iterations"
    assert result.revise_calls == 0
    assert _revise_calls(client) == []
    assert result.revised_plan is None


@pytest.mark.parametrize(
    "parse, owner_tasks, expected",
    [
        (_submit([_new("walk", "08:00", 30)]), [], "clean"),
        (
            _submit(
                [_new("walk", "09:00", 25), _new("brushing", "17:00", 20)], available_minutes=30
            ),
            [],
            "no_movable_conflicts",
        ),
        (
            _submit([_new("walk", "14:00", 30)]),
            [
                _existing("task-abc", "grooming", "12:00", 30),
                _existing("task-def", "ear drops", "12:10", 5),
            ],
            "no_movable_conflicts",
        ),
    ],
    ids=["clean", "over-budget", "pre-existing-conflict-only"],
)
def test_gate_makes_zero_revise_calls(parse, owner_tasks, expected):
    # Scripted with no revise responses: an unexpected revise call would pop an empty list.
    client = FakeClient(parse)
    owner = _owner_with(*copy.deepcopy(owner_tasks))
    before = _snapshot(owner)

    result = _run(REQ, client, owner)

    assert result.status == "ok"
    assert result.stop_reason == expected
    assert result.revise_calls == 0
    assert len(client.calls) == 1
    assert result.revised_plan is None
    assert result.trace[-1]["stop_reason"] == expected
    assert _snapshot(owner) == before


def test_llm_error_during_revise_keeps_the_last_good_state():
    owner = build_fixture_owner()
    before = _snapshot(owner)
    result = _run(F2, FakeClient(_f2_parse(), RuntimeError("quota exceeded")), owner)

    assert result.status == "llm_error"
    assert result.stop_reason == "llm_error"
    assert result.revise_calls == 1
    assert result.errors == ["RuntimeError: quota exceeded"]
    assert _times(result.new_tasks) == {"new-1": "08:00", "new-2": "08:15"}
    assert _pairs(result.original_plan) == [("new-1", "new-2")]
    assert result.plan_date == date(2026, 11, 2)
    assert len(result.assumptions) == 2
    assert result.revised_plan is None
    assert _snapshot(owner) == before
    assert result.trace[-1]["status"] == "llm_error"
    assert result.trace[-1]["revise_calls"] == 1


def test_llm_error_on_second_revise_keeps_the_first_accepted_revision():
    client = FakeClient(_submit(ABCD), _moves(("new-2", "08:30")), RuntimeError("timeout"))

    result = _run(REQ, client)

    assert result.status == "llm_error"
    assert result.stop_reason == "llm_error"
    assert result.revise_calls == 2
    assert _pairs(result.revised_plan) == [("new-3", "new-4")]
    assert [(move["iteration"], move["ref"], move["to"]) for move in result.revision_moves] == [
        (1, "new-2", "08:30")
    ]
    assert _times(result.revised_new_tasks)["new-2"] == "08:30"
    assert _pairs(result.original_plan) == [("new-1", "new-2"), ("new-3", "new-4")]


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
