"""Tests for agent.commit() and agent.discard(): the human approval step (plan §2 step 8).

No test touches the network: runs use MockClient or a small scripted FakeClient.
"""

import copy
import json
import logging
import re
import uuid
from datetime import datetime

import pytest

import agent
from agent import CommitError, commit, discard, run_agent, setup_logging
from fixture_owner import build_fixture_owner
from llm_client import FIXTURES_PATH, LLMResponseError, MockClient, ToolCall
from pawpal_system import Owner, Pet, Scheduler

NOW = datetime(2026, 11, 2, 7, 0)
END_OF_DAY = datetime(2026, 11, 2, 23, 59)
TASK_ID = re.compile(r"^task-[0-9a-f]{8}$")

with open(FIXTURES_PATH, encoding="utf-8") as _handle:
    _FIXTURES = list(json.load(_handle).values())
F1, F2, F3, F4, F5, F6 = (fixture["request"] for fixture in _FIXTURES)


class FakeClient:
    """Returns scripted ToolCalls, or raises scripted exceptions, in order."""

    def __init__(self, *responses):
        self._responses = list(responses)

    def call(self, step, system_prompt, user_text, tools):
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.fixture(autouse=True)
def _reset_agent_logger():
    """Remove any handlers setup_logging added, so tests don't leak log files into each other."""
    yield
    log = logging.getLogger(agent.LOGGER_NAME)
    for handler in list(log.handlers):
        if getattr(handler, agent._HANDLER_TAG, False):
            log.removeHandler(handler)
            handler.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(request, client=None, owner=None):
    owner = owner if owner is not None else build_fixture_owner()
    return run_agent(request, owner, NOW, client if client is not None else MockClient())


def _owner_snapshot(owner):
    """Everything about an owner that commit() could change."""
    return (
        owner.owner_id,
        owner.name,
        owner.available_time_minutes,
        [
            (pet.pet_id, pet.name, pet.species, [vars(task).copy() for task in pet.tasks])
            for pet in owner.pets
        ],
    )


def _result_snapshot(result):
    """Everything about a result that commit() or discard() could change."""
    return (
        result.decision,
        copy.deepcopy(result.trace),
        [(pet_id, ref, vars(task).copy()) for pet_id, ref, task in result.new_tasks],
        [(pet_id, ref, vars(task).copy()) for pet_id, ref, task in result.revised_new_tasks],
    )


def _pet(owner, pet_id):
    return next(pet for pet in owner.pets if pet.pet_id == pet_id)


def _hhmm_by_description(tasks):
    return {task.description: task.time.strftime("%H:%M") for task in tasks}


def _one_task(description, hhmm, minutes, pet_name="Biscuit"):
    return ToolCall(
        "submit_tasks",
        {
            "tasks": [
                {
                    "pet_name": pet_name,
                    "description": description,
                    "quote": description,
                    "date": "today",
                    "time": hhmm,
                    "duration_minutes": minutes,
                }
            ]
        },
    )


def _f2_parse():
    return ToolCall("submit_tasks", copy.deepcopy(_FIXTURES[1]["parse"]))


def _f2_llm_error():
    """Fixture 2 whose revise call fails: status llm_error with the original tasks kept."""
    result = _run(F2, FakeClient(_f2_parse(), RuntimeError("quota exceeded")))
    assert result.status == "llm_error"
    assert len(result.new_tasks) == 2
    return result


def _fake_uuid4(*hex_prefixes):
    """A uuid4 stand-in returning UUIDs whose hex starts with each prefix in turn."""
    values = iter(uuid.UUID(prefix + "0" * (32 - len(prefix))) for prefix in hex_prefixes)
    return lambda: next(values)


# ---------------------------------------------------------------------------
# Fixture 2 and 3
# ---------------------------------------------------------------------------


def test_fixture_2_approve_revised_commits_the_clean_plan():
    owner = build_fixture_owner()
    result = _run(F2, owner=owner)

    ref_to_id = commit(result, owner, use_revision=True)

    biscuit = _pet(owner, "p-biscuit")
    assert _hhmm_by_description(biscuit.tasks) == {"walk": "08:00", "meds": "08:30"}
    assert len(biscuit.tasks) == 2
    assert _pet(owner, "p-whiskers").tasks == []
    assert owner.available_time_minutes == 120
    assert set(ref_to_id) == {"new-1", "new-2"}
    for task in owner.get_all_tasks():
        assert TASK_ID.match(task.task_id)
        assert not task.task_id.startswith("new-")
        assert task.is_completed is False
        assert task.completed_at is None
    assert [task.task_id for task in biscuit.tasks] == [ref_to_id["new-1"], ref_to_id["new-2"]]
    assert result.decision == "approve_revised"

    plan = Scheduler().generate_plan(owner, END_OF_DAY)
    assert len(plan["scheduled"]) == 2
    assert plan["conflicts"] == []
    assert plan["deferred"] == []


def test_fixture_2_approve_original_keeps_the_conflict():
    owner = build_fixture_owner()
    result = _run(F2, owner=owner)

    commit(result, owner, use_revision=False)

    assert _hhmm_by_description(_pet(owner, "p-biscuit").tasks) == {"walk": "08:00", "meds": "08:15"}
    assert result.decision == "approve_original"
    plan = Scheduler().generate_plan(owner, END_OF_DAY)
    assert len(plan["conflicts"]) == 1


def test_fixture_3_sets_budget_and_lands_tasks_on_the_right_pets():
    owner = build_fixture_owner()
    result = _run(F3, owner=owner)
    assert result.revised_new_tasks == []  # no revision: only the original can be approved

    commit(result, owner, use_revision=False)

    assert owner.available_time_minutes == 30
    assert [task.description for task in _pet(owner, "p-biscuit").tasks] == ["walk"]
    assert [task.description for task in _pet(owner, "p-whiskers").tasks] == ["brushing"]
    walk = _pet(owner, "p-biscuit").tasks[0]
    assert (walk.time, walk.duration_minutes) == (datetime(2026, 11, 2, 9, 0), 25)


def test_committed_tasks_copy_every_field_except_the_id():
    owner = build_fixture_owner()
    result = _run(F2, owner=owner)

    ref_to_id = commit(result, owner, use_revision=True)

    committed = {task.task_id: task for task in owner.get_all_tasks()}
    for _pet_id, ref, source in result.revised_new_tasks:
        task = committed[ref_to_id[ref]]
        assert (task.description, task.time, task.frequency, task.duration_minutes) == (
            source.description,
            source.time,
            source.frequency,
            source.duration_minutes,
        )


# ---------------------------------------------------------------------------
# New objects, unique ids
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("use_revision", [True, False])
def test_committed_tasks_are_new_objects(use_revision):
    owner = build_fixture_owner()
    result = _run(F2, owner=owner)
    before = _result_snapshot(result)

    commit(result, owner, use_revision=use_revision)

    result_tasks = [task for _, _, task in result.new_tasks + result.revised_new_tasks]
    plan_tasks = result.original_plan["scheduled"] + result.revised_plan["scheduled"]
    for task in owner.get_all_tasks():
        assert all(task is not other for other in result_tasks + plan_tasks)

    for task in owner.get_all_tasks():
        task.time = datetime(2026, 11, 2, 12, 0)
        task.description = "changed"
        task.mark_complete()
    _decision, _trace, new_tasks, revised_tasks = _result_snapshot(result)
    assert (new_tasks, revised_tasks) == before[2:]
    assert [task.task_id for task in result.original_plan["scheduled"]] == ["new-1", "new-2"]


def test_two_results_committed_to_one_owner_have_unique_ids():
    owner = build_fixture_owner()
    first = _run("walk", FakeClient(_one_task("walk", "08:00", 30)), owner)
    second = _run("meds", FakeClient(_one_task("meds", "12:00", 5)), owner)
    assert [ref for _, ref, _ in first.new_tasks] == [ref for _, ref, _ in second.new_tasks] == ["new-1"]

    first_ids = commit(first, owner, use_revision=False)
    second_ids = commit(second, owner, use_revision=False)

    ids = [task.task_id for task in owner.get_all_tasks()]
    assert len(ids) == 2
    assert len(set(ids)) == 2
    assert first_ids["new-1"] != second_ids["new-1"]
    assert all(TASK_ID.match(task_id) for task_id in ids)


def test_id_collision_within_one_commit_is_retried(monkeypatch):
    owner = build_fixture_owner()
    result = _run(F2, owner=owner)  # run_agent's own uuid4 call happens before the patch
    monkeypatch.setattr(agent.uuid, "uuid4", _fake_uuid4("aaaaaaaa", "aaaaaaaa", "bbbbbbbb"))

    ref_to_id = commit(result, owner, use_revision=True)

    assert ref_to_id == {"new-1": "task-aaaaaaaa", "new-2": "task-bbbbbbbb"}


def test_id_collision_with_an_existing_owner_task_is_retried(monkeypatch):
    owner = build_fixture_owner()
    first = _run("walk", FakeClient(_one_task("walk", "08:00", 30)), owner)
    second = _run("meds", FakeClient(_one_task("meds", "12:00", 5)), owner)
    monkeypatch.setattr(agent.uuid, "uuid4", _fake_uuid4("aaaaaaaa", "aaaaaaaa", "cccccccc"))

    assert commit(first, owner, use_revision=False) == {"new-1": "task-aaaaaaaa"}
    assert commit(second, owner, use_revision=False) == {"new-1": "task-cccccccc"}
    assert sorted(task.task_id for task in owner.get_all_tasks()) == [
        "task-aaaaaaaa",
        "task-cccccccc",
    ]


def test_new_task_id_skips_taken_ids(monkeypatch):
    monkeypatch.setattr(agent.uuid, "uuid4", _fake_uuid4("11111111", "22222222", "33333333"))
    assert agent._new_task_id({"task-11111111", "task-22222222"}) == "task-33333333"


# ---------------------------------------------------------------------------
# Discard and double decisions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("request_text", [F2, F3, F1])
def test_discard_leaves_owner_unchanged_and_records_the_decision(request_text):
    owner = build_fixture_owner()
    owner_before = _owner_snapshot(owner)
    result = _run(request_text, owner=owner)
    trace_length = len(result.trace)

    assert discard(result) is None

    assert _owner_snapshot(owner) == owner_before
    assert result.decision == "discard"
    assert len(result.trace) == trace_length + 1
    assert result.trace[-1] == {"step": "approval", "decision": "discard"}
    assert result.trace[-2]["step"] == "stop"


def test_commit_after_discard_raises_and_changes_nothing():
    owner = build_fixture_owner()
    result = _run(F2, owner=owner)
    discard(result)
    owner_before, result_before = _owner_snapshot(owner), _result_snapshot(result)

    with pytest.raises(CommitError):
        commit(result, owner, use_revision=True)

    assert _owner_snapshot(owner) == owner_before
    assert _result_snapshot(result) == result_before


def test_discard_twice_or_after_commit_raises():
    result = _run(F2)
    discard(result)
    with pytest.raises(CommitError):
        discard(result)

    owner = build_fixture_owner()
    committed = _run(F2, owner=owner)
    commit(committed, owner, use_revision=True)
    with pytest.raises(CommitError):
        discard(committed)
    assert committed.decision == "approve_revised"


@pytest.mark.parametrize("second_choice", [True, False])
def test_double_approval_raises_and_adds_no_tasks(second_choice):
    owner = build_fixture_owner()
    result = _run(F2, owner=owner)
    commit(result, owner, use_revision=True)
    owner_before, result_before = _owner_snapshot(owner), _result_snapshot(result)

    with pytest.raises(CommitError):
        commit(result, owner, use_revision=second_choice)

    assert len(owner.get_all_tasks()) == 2
    assert _owner_snapshot(owner) == owner_before
    assert _result_snapshot(result) == result_before


def test_commit_error_is_a_value_error():
    assert issubclass(CommitError, ValueError)


# ---------------------------------------------------------------------------
# Refusals leave the owner and the result untouched
# ---------------------------------------------------------------------------


def _needs_clarification():
    return _run(F1), [False, True]


def _llm_bad_output():
    return _run(F2, FakeClient(LLMResponseError("text instead of a tool call"))), [False, True]


def _mock_unknown_request():
    return _run("Feed the goldfish at noon."), [False, True]


def _llm_error_without_tasks():
    return _run(F2, FakeClient(RuntimeError("boom"))), [False, True]


def _no_accepted_revision():
    return _run(F3), [True]


def _llm_error_revision():
    return _f2_llm_error(), [True]


@pytest.mark.parametrize(
    "make, expected_status",
    [
        (_needs_clarification, "needs_clarification"),
        (_llm_bad_output, "llm_bad_output"),
        (_mock_unknown_request, "mock_unknown_request"),
        (_llm_error_without_tasks, "llm_error"),
        (_no_accepted_revision, "ok"),
        (_llm_error_revision, "llm_error"),
    ],
    ids=[
        "needs_clarification",
        "llm_bad_output",
        "mock_unknown_request",
        "llm_error-empty",
        "revision-not-accepted",
        "revision-on-llm_error",
    ],
)
def test_refusals_change_nothing(make, expected_status):
    result, choices = make()
    assert result.status == expected_status
    for use_revision in choices:
        owner = build_fixture_owner()
        owner_before, result_before = _owner_snapshot(owner), _result_snapshot(result)

        with pytest.raises(CommitError):
            commit(result, owner, use_revision=use_revision)

        assert _owner_snapshot(owner) == owner_before
        assert _result_snapshot(result) == result_before
        assert result.decision is None


def test_llm_error_with_tasks_can_commit_the_original():
    owner = build_fixture_owner()
    result = _f2_llm_error()
    assert result.stop_reason == "llm_error"

    ref_to_id = commit(result, owner, use_revision=False)

    assert set(ref_to_id) == {"new-1", "new-2"}
    assert _hhmm_by_description(_pet(owner, "p-biscuit").tasks) == {"walk": "08:00", "meds": "08:15"}
    assert result.decision == "approve_original"
    assert result.trace[-1]["status"] == "llm_error"
    assert result.trace[-1]["stop_reason"] == "llm_error"


def test_stale_owner_missing_a_pet_raises_and_changes_nothing():
    result = _run(F3)  # walk on p-biscuit, brushing on p-whiskers, budget 30
    stale = Owner(owner_id="o-demo", name="Demo", available_time_minutes=120)
    stale.add_pet(Pet(pet_id="p-biscuit", name="Biscuit", species="dog"))
    owner_before, result_before = _owner_snapshot(stale), _result_snapshot(result)

    with pytest.raises(CommitError, match="p-whiskers"):
        commit(result, stale, use_revision=False)

    assert stale.available_time_minutes == 120
    assert stale.get_all_tasks() == []
    assert _owner_snapshot(stale) == owner_before
    assert _result_snapshot(result) == result_before


# ---------------------------------------------------------------------------
# Trace and log
# ---------------------------------------------------------------------------


def test_approval_trace_entry_matches_the_returned_map():
    owner = build_fixture_owner()
    result = _run(F3, owner=owner)

    ref_to_id = commit(result, owner, use_revision=False)

    assert [entry["step"] for entry in result.trace[-2:]] == ["stop", "approval"]
    assert result.trace[-1] == {
        "step": "approval",
        "decision": "approve_original",
        "status": "ok",
        "stop_reason": "no_movable_conflicts",
        "budget": 30,
        "ref_to_task_id": ref_to_id,
    }


def test_approval_is_written_to_the_log(tmp_path):
    log_path = tmp_path / "logs" / "agent.log"
    setup_logging(log_path=str(log_path))
    owner = build_fixture_owner()
    result = _run(F2, owner=owner)

    ref_to_id = commit(result, owner, use_revision=True)

    approvals = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        run_part, step_part, payload = line.split(" ", 2)
        if step_part == "step=approval":
            approvals.append((run_part, json.loads(payload)))
    assert len(approvals) == 1
    run_part, payload = approvals[0]
    assert run_part == f"run={result.run_id}"
    assert payload["decision"] == "approve_revised"
    assert payload["ref_to_task_id"] == ref_to_id
    assert payload["budget"] is None
    assert {"step": "approval", **payload} == result.trace[-1]
