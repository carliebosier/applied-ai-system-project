"""Tests for agent_demo.py, the CLI demo (plan §9 increment 8).

No test touches the network: runs use --mock or a scripted fake client.
Every test passes --log-path under tmp_path, so no logs/ directory is created in the project.
"""

import copy
import json
import logging
import re

import pytest

import agent
import agent_demo
import llm_client
from llm_client import FIXTURES_PATH, ToolCall

NOW = "2026-11-02T07:00"
TASK_ID = re.compile(r"task-[0-9a-f]{8}")

with open(FIXTURES_PATH, encoding="utf-8") as _handle:
    _FIXTURES = list(json.load(_handle).values())
F1, F2, F3, F4, F5, F6 = (fixture["request"] for fixture in _FIXTURES)


@pytest.fixture(autouse=True)
def _reset_agent_logger():
    """Remove any handlers setup_logging added, so tests don't leak log files into each other."""
    yield
    log = logging.getLogger(agent.LOGGER_NAME)
    for handler in list(log.handlers):
        if getattr(handler, agent._HANDLER_TAG, False):
            log.removeHandler(handler)
            handler.close()


def _main(tmp_path, *argv):
    """Run the demo with a log path under tmp_path; return its exit code."""
    return agent_demo.main([*argv, "--log-path", str(tmp_path / "agent.log")])


def _run(capsys, tmp_path, *argv):
    """Run the demo and return (exit code, stdout)."""
    code = _main(tmp_path, *argv)
    return code, capsys.readouterr().out


def _section(out, start, end):
    """The text between the first `start` marker and the next `end` marker."""
    head = out.index(start)
    tail = out.find(end, head + len(start))
    return out[head:] if tail == -1 else out[head:tail]


def _scripted_input(monkeypatch, *answers):
    """Patch input() to return (or raise) each answer in turn; return the prompts it saw."""
    prompts = []
    queue = list(answers)

    def fake_input(prompt=""):
        prompts.append(prompt)
        answer = queue.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr("builtins.input", fake_input)
    return prompts


def _no_input(monkeypatch):
    def fail(prompt=""):
        raise AssertionError(f"input() should not be called (prompt: {prompt!r})")

    monkeypatch.setattr("builtins.input", fail)


class _FakeClient:
    """Fixture 2's recorded parse, then a failing revise call."""

    is_mock = False

    def __init__(self):
        self.steps = []

    def call(self, step, system_prompt, user_text, tools):
        self.steps.append(step)
        if step == "parse":
            return ToolCall("submit_tasks", copy.deepcopy(_FIXTURES[1]["parse"]))
        raise RuntimeError("quota exceeded")


# ---------------------------------------------------------------------------
# All six fixtures
# ---------------------------------------------------------------------------


def test_all_six_fixtures_run_with_their_expected_statuses(capsys, tmp_path, monkeypatch):
    _no_input(monkeypatch)
    code, out = _run(capsys, tmp_path, "--mock", "--now", NOW, "--yes")

    assert code == 0
    assert out.startswith("[mock mode: recorded responses, no API calls]\n")
    assert "6 requests run" in out
    for number, fixture in enumerate(_FIXTURES, start=1):
        assert fixture["request"] in out
        report = _section(out, f"=== Request {number} of 6 ===", "=== ")
        assert f"Status: {fixture['expect']['status']}" in report

    summary = _section(out, "=== Summary ===", "requests run").splitlines()
    assert summary[1].split() == ["#", "Status", "Stop", "reason", "Revise", "calls", "Decision"]
    rows = [line.split() for line in summary[3:] if len(line.split()) == 5]
    assert len(rows) == 6
    assert [row[1] for row in rows] == [fixture["expect"]["status"] for fixture in _FIXTURES]
    assert [row[3] for row in rows] == [str(f["expect"]["revise_calls"]) for f in _FIXTURES]


def test_two_identical_runs_print_the_same_bytes(capsys, tmp_path):
    first = _run(capsys, tmp_path, "--mock", "--now", NOW, "--yes")
    second = _run(capsys, tmp_path, "--mock", "--now", NOW, "--yes")
    assert first == second
    assert first[1].encode("utf-8") == second[1].encode("utf-8")


# ---------------------------------------------------------------------------
# Single fixtures with --yes
# ---------------------------------------------------------------------------


def test_fixture_2_commits_the_revised_plan(capsys, tmp_path, monkeypatch):
    _no_input(monkeypatch)
    code, out = _run(capsys, tmp_path, "--mock", "--now", NOW, "--yes", F2)

    assert code == 0
    original = _section(out, "Original plan:", "Revision moves:")
    assert "Conflict: walk overlaps meds" in original
    assert "new-2 meds: 08:15 -> 08:30 (" in out
    revised = _section(out, "Revised plan:", "Stop reason:")
    assert "08:30" in revised
    assert "Conflict" not in revised
    assert "Stop reason: clean" in out
    assert "LLM revise calls: 1" in out
    committed = _section(out, "Committed to the owner:", "\n\n")
    assert "Biscuit: meds at 08:30 (5 min)" in committed
    assert "Biscuit: walk at 08:00 (30 min)" in committed
    assert "6 requests run" not in out  # one request: no summary


def test_fixture_3_defers_brushing_and_commits_the_original(capsys, tmp_path, monkeypatch):
    _no_input(monkeypatch)
    code, out = _run(capsys, tmp_path, "--mock", "--now", NOW, "--yes", F3)

    assert code == 0
    assert re.search(r"new-2 brushing\s+Whiskers\s+17:00\s+17:20\s+20\s+deferred", out)
    assert "Budget: 25 of 30 min used" in out
    assert "Stop reason: no_movable_conflicts" in out
    assert "LLM revise calls: 0" in out
    assert "Revised plan" not in out
    assert "Approved the original plan." in out
    committed = _section(out, "Committed to the owner:", "\n\n")
    assert "Whiskers: brushing at 17:00 (20 min)" in committed
    assert "Budget: 120 -> 30 min" in committed


@pytest.mark.parametrize("request_text", [F1, F4, F5, F6], ids=["f1", "f4", "f5", "f6"])
def test_clarification_fixtures_ask_and_never_approve(capsys, tmp_path, monkeypatch, request_text):
    _no_input(monkeypatch)
    code, out = _run(capsys, tmp_path, "--mock", "--now", NOW, request_text)  # no --yes

    assert code == 0
    assert "Status: needs_clarification" in out
    assert "Question: " in out
    assert "Options:" not in out
    assert "Committed" not in out
    assert "Discarded" not in out


def test_unknown_request_lists_the_supported_requests(capsys, tmp_path, monkeypatch):
    _no_input(monkeypatch)
    code, out = _run(capsys, tmp_path, "--mock", "--now", NOW, "Feed the goldfish at noon.")

    assert code == 0
    assert "Status: mock_unknown_request" in out
    for fixture in _FIXTURES:
        assert f"- {fixture['request']}" in out
    assert "Committed" not in out


# ---------------------------------------------------------------------------
# Interactive approval
# ---------------------------------------------------------------------------


def test_interactive_original_commits_the_original_times(capsys, tmp_path, monkeypatch):
    prompts = _scripted_input(monkeypatch, "o")
    code, out = _run(capsys, tmp_path, "--mock", "--now", NOW, F2)

    assert code == 0
    assert len(prompts) == 1
    assert "r = approve the revised plan" in out
    committed = _section(out, "Committed to the owner:", "\n\n")
    assert "Biscuit: meds at 08:15 (5 min)" in committed


def test_interactive_discard(capsys, tmp_path, monkeypatch):
    _scripted_input(monkeypatch, "d")
    _code, out = _run(capsys, tmp_path, "--mock", "--now", NOW, F2)

    assert "Discarded: nothing was changed." in out
    assert "Committed" not in out


def test_interactive_invalid_answer_is_asked_again(capsys, tmp_path, monkeypatch):
    prompts = _scripted_input(monkeypatch, "maybe", " R ")
    _code, out = _run(capsys, tmp_path, "--mock", "--now", NOW, F2)

    assert len(prompts) == 2
    assert "Please type r, o or d." in out
    assert "Approved the revised plan." in out
    assert "Biscuit: meds at 08:30 (5 min)" in out


@pytest.mark.parametrize("error", [EOFError(), KeyboardInterrupt()], ids=["eof", "ctrl-c"])
def test_interactive_eof_or_interrupt_discards(capsys, tmp_path, monkeypatch, error):
    _scripted_input(monkeypatch, error)
    code, out = _run(capsys, tmp_path, "--mock", "--now", NOW, F2)

    assert code == 0
    assert "Discarded: nothing was changed." in out
    assert "Committed" not in out


def test_interactive_revised_is_not_offered_without_a_revision(capsys, tmp_path, monkeypatch):
    prompts = _scripted_input(monkeypatch, "r", "o")
    _code, out = _run(capsys, tmp_path, "--mock", "--now", NOW, F3)

    options = next(line for line in out.splitlines() if line.startswith("Options:"))
    assert "revised" not in options
    assert "r =" not in options
    assert all("r/" not in prompt and "revised" not in prompt for prompt in prompts)
    assert len(prompts) == 2  # "r" was rejected as invalid
    assert "Please type o or d." in out
    assert "Approved the original plan." in out


# ---------------------------------------------------------------------------
# llm_error recovery, secrets, arguments and logging
# ---------------------------------------------------------------------------


def test_llm_error_on_revise_commits_the_original(capsys, tmp_path, monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(llm_client, "get_client", lambda: fake)
    _no_input(monkeypatch)
    code, out = _run(capsys, tmp_path, "--now", NOW, "--yes", F2)

    assert code == 0
    assert fake.steps == ["parse", "revise"]
    assert "[mock mode" not in out
    assert "Status: llm_error" in out
    assert "Problem: RuntimeError: quota exceeded" in out
    assert "Revised plan" not in out
    assert re.search(r"^Stop reason: llm_error\b", out, re.MULTILINE)
    committed = _section(out, "Committed to the owner:", "\n\n")
    assert "Biscuit: meds at 08:15 (5 min)" in committed


def test_llm_error_without_yes_offers_only_original_or_discard(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(llm_client, "get_client", lambda: _FakeClient())
    prompts = _scripted_input(monkeypatch, "d")
    _code, out = _run(capsys, tmp_path, "--now", NOW, F2)

    options = next(line for line in out.splitlines() if line.startswith("Options:"))
    assert "revised" not in options
    assert prompts == ["Your choice [o/d]: "]
    assert "Discarded: nothing was changed." in out


def test_output_never_shows_a_run_id_or_committed_task_id(capsys, tmp_path, monkeypatch):
    seen_run_ids = []
    real_run_agent = agent.run_agent

    def spy(*args, **kwargs):
        result = real_run_agent(*args, **kwargs)
        seen_run_ids.append(result.run_id)
        return result

    monkeypatch.setattr(agent, "run_agent", spy)
    _code, out = _run(capsys, tmp_path, "--mock", "--now", NOW, "--yes", F2)

    assert "Committed to the owner:" in out
    assert seen_run_ids and all(run_id not in out for run_id in seen_run_ids)
    assert "run=" not in out
    assert not TASK_ID.search(out)


def test_invalid_now_is_an_argparse_error(capsys, tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        _main(tmp_path, "--mock", "--now", "next blursday")
    assert excinfo.value.code != 0
    assert "--now" in capsys.readouterr().err


def test_log_path_receives_run_lines(capsys, tmp_path):
    log_path = tmp_path / "nested" / "agent.log"
    agent_demo.main(["--mock", "--now", NOW, "--yes", "--log-path", str(log_path), F2])

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert lines
    assert all(line.startswith("run=") for line in lines)
    assert any(" step=approval " in line for line in lines)
