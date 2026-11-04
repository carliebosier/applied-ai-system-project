"""Tests for eval_agent.py, the mock-mode fixture eval (plan §9 increment 11).

No test touches the network. Every test passes --log-path under tmp_path, so no logs/
directory is created in the project.
"""

import json
import logging
import re
from pathlib import Path

import pytest

import agent
import eval_agent
import llm_client
from llm_client import FIXTURES_PATH

NOW = "2026-11-02T07:00"
TASK_ID = re.compile(r"task-[0-9a-f]{8}")
ROW = re.compile(r"^(\d)\s{2,}.+?\s{2,}(\S+)\s{2,}(\S+)\s{2,}(\S+)\s{2,}(PASS|FAIL)$")

with open(FIXTURES_PATH, encoding="utf-8") as _handle:
    _FIXTURES = json.load(_handle)


@pytest.fixture(autouse=True)
def _reset_agent_logger():
    """Remove any handlers setup_logging added, so tests don't leak log files into each other."""
    yield
    log = logging.getLogger(agent.LOGGER_NAME)
    for handler in list(log.handlers):
        if getattr(handler, agent._HANDLER_TAG, False):
            log.removeHandler(handler)
            handler.close()


def _run(capsys, tmp_path, *argv):
    """Run the eval with a log path under tmp_path; return (exit code, stdout)."""
    code = eval_agent.main([*argv, "--log-path", str(tmp_path / "eval.log")])
    return code, capsys.readouterr().out


def _rows(out):
    """{fixture number: (status, stop reason, calls, result)} from the table."""
    rows = {}
    for line in out.splitlines():
        match = ROW.match(line)
        if match:
            rows[int(match.group(1))] = match.groups()[1:]
    return rows


def _use_fixtures(monkeypatch, tmp_path, edit=None, keep=None):
    """Point the eval at an edited copy of the fixtures in tmp_path.

    eval_agent passes llm_client.FIXTURES_PATH to MockClient explicitly, so patching the
    module attribute is enough; MockClient's import-time default is never used.
    """
    fixtures = json.loads(json.dumps(_FIXTURES))
    entries = list(fixtures.items())
    if edit:
        edit([value for _key, value in entries])
    if keep is not None:
        entries = entries[:keep]
    path = tmp_path / "mock_responses.json"
    path.write_text(json.dumps(dict(entries)), encoding="utf-8")
    monkeypatch.setattr(llm_client, "FIXTURES_PATH", path)
    return path


# ---------------------------------------------------------------------------
# The real fixtures
# ---------------------------------------------------------------------------


def test_real_fixtures_all_pass(capsys, tmp_path):
    code, out = _run(capsys, tmp_path, "--now", NOW)

    assert code == 0
    assert "6/6 passed" in out
    rows = _rows(out)
    assert sorted(rows) == [1, 2, 3, 4, 5, 6]
    assert [rows[n][3] for n in range(1, 7)] == ["PASS"] * 6
    assert [rows[n][2] for n in range(1, 7)] == ["0", "1", "0", "0", "0", "0"]
    assert out.count("PASS") == 6
    assert "FAIL" not in out


def test_default_now_is_the_baseline(capsys, tmp_path):
    code, out = _run(capsys, tmp_path)
    assert code == 0
    assert "6/6 passed" in out


def test_table_shows_status_stop_reason_and_abridged_requests(capsys, tmp_path):
    _code, out = _run(capsys, tmp_path, "--now", NOW)
    rows = _rows(out)

    assert rows[1][:2] == ("needs_clarification", "-")
    assert rows[2][:2] == ("ok", "clean")
    assert rows[3][:2] == ("ok", "no_movable_conflicts")
    header = out.splitlines()[0].split()
    assert header == ["#", "Request", "Status", "Stop", "reason", "Calls", "Result"]
    for fixture in _FIXTURES.values():
        request = fixture["request"]
        assert (request if len(request) <= 50 else request[:47] + "...") in out


def test_two_identical_runs_print_the_same_output(capsys, tmp_path):
    first = _run(capsys, tmp_path, "--now", NOW)
    second = _run(capsys, tmp_path, "--now", NOW)
    assert first == second


# ---------------------------------------------------------------------------
# Failure detection
# ---------------------------------------------------------------------------


def _set(index, key, value):
    def edit(fixtures):
        fixtures[index]["expect"][key] = value

    return edit


@pytest.mark.parametrize(
    "index, key, value, message",
    [
        (1, "revise_calls", 2, "#2: revise_calls: expected 2, got 1"),
        (1, "status", "needs_clarification", "#2: status: expected needs_clarification, got ok"),
        (2, "deferred", ["bath"], "#3: deferred: expected ['bath'], got ['brushing']"),
    ],
    ids=["revise_calls", "status", "deferred"],
)
def test_a_wrong_expectation_fails_only_that_fixture(
    capsys, tmp_path, monkeypatch, index, key, value, message
):
    _use_fixtures(monkeypatch, tmp_path, edit=_set(index, key, value))
    code, out = _run(capsys, tmp_path, "--now", NOW)

    assert code == 1
    rows = _rows(out)
    assert [n for n in range(1, 7) if rows[n][3] == "FAIL"] == [index + 1]
    assert f"  {message}" in out.splitlines()
    assert "5/6 passed" in out


def test_five_fixtures_is_a_failure(capsys, tmp_path, monkeypatch):
    _use_fixtures(monkeypatch, tmp_path, keep=5)
    code, out = _run(capsys, tmp_path, "--now", NOW)

    assert code == 1
    assert "FAIL: expected 6 fixtures, found 5" in out
    assert "passed" not in out


def test_a_run_that_raises_fails_that_fixture_and_the_rest_still_run(
    capsys, tmp_path, monkeypatch
):
    real_run_agent = agent.run_agent
    calls = []

    def flaky(*args, **kwargs):
        calls.append(args[0])
        if len(calls) == 2:
            raise RuntimeError("boom on purpose")
        return real_run_agent(*args, **kwargs)

    monkeypatch.setattr(agent, "run_agent", flaky)
    code, out = _run(capsys, tmp_path, "--now", NOW)

    assert code == 1
    assert len(calls) == 6
    rows = _rows(out)
    assert rows[2][3] == "FAIL"
    assert [rows[n][3] for n in (1, 3, 4, 5, 6)] == ["PASS"] * 5
    assert "  #2: exception: RuntimeError: boom on purpose" in out.splitlines()
    assert "5/6 passed" in out


# ---------------------------------------------------------------------------
# Conditional checks
# ---------------------------------------------------------------------------


def test_plan_checks_are_skipped_when_status_is_not_ok(capsys, tmp_path, monkeypatch):
    def edit(fixtures):
        fixtures[3]["expect"]["conflicts"] = 5
        fixtures[3]["expect"]["deferred"] = ["x"]

    _use_fixtures(monkeypatch, tmp_path, edit=edit)
    code, out = _run(capsys, tmp_path, "--now", NOW)

    assert code == 0
    assert _rows(out)[4][3] == "PASS"
    assert "6/6 passed" in out


def test_question_count_is_checked(capsys, tmp_path, monkeypatch):
    _use_fixtures(monkeypatch, tmp_path, edit=_set(0, "questions", 3))
    code, out = _run(capsys, tmp_path, "--now", NOW)

    assert code == 1
    assert _rows(out)[1][3] == "FAIL"
    assert "  #1: questions: expected 3, got 2" in out.splitlines()


# ---------------------------------------------------------------------------
# Arguments, secrets and imports
# ---------------------------------------------------------------------------


def test_invalid_now_is_an_argparse_error(capsys, tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        eval_agent.main(["--now", "next blursday", "--log-path", str(tmp_path / "eval.log")])
    assert excinfo.value.code != 0
    assert "--now" in capsys.readouterr().err


def test_output_never_shows_a_run_id_or_task_id(capsys, tmp_path, monkeypatch):
    seen_run_ids = []
    real_run_agent = agent.run_agent

    def spy(*args, **kwargs):
        result = real_run_agent(*args, **kwargs)
        seen_run_ids.append(result.run_id)
        return result

    monkeypatch.setattr(agent, "run_agent", spy)
    _code, out = _run(capsys, tmp_path, "--now", NOW)

    assert len(seen_run_ids) == 6
    assert all(run_id not in out for run_id in seen_run_ids)
    assert "run=" not in out
    assert not TASK_ID.search(out)


def test_log_path_receives_run_lines(capsys, tmp_path):
    log_path = tmp_path / "nested" / "eval.log"
    eval_agent.main(["--now", NOW, "--log-path", str(log_path)])

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert lines
    assert all(line.startswith("run=") for line in lines)
    assert " step=approval " not in "\n".join(lines)  # the eval never commits or discards


def test_source_has_no_google_import_or_environment_read():
    source = Path(eval_agent.__file__).read_text(encoding="utf-8")
    assert "google" not in source
    assert "os.environ" not in source
