"""Tests for app.py's "Ask PawPal (AI)" section (plan §9 increment 10).

Every test runs the real app with Streamlit's AppTest in mock mode: no network, no .env,
and agent.setup_logging is a no-op so no logs/ directory is created.
"""

import json
import re
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import agent
import llm_client
from fixture_owner import build_fixture_owner
from llm_client import FIXTURES_PATH

TASK_ID = re.compile(r"task-[0-9a-f]{8}")
APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")

with open(FIXTURES_PATH, encoding="utf-8") as _handle:
    _FIXTURES = list(json.load(_handle).values())
F1, F2, F3, F4, F5, F6 = (fixture["request"] for fixture in _FIXTURES)


@pytest.fixture
def at(monkeypatch):
    """A first run of the app in mock mode, with the fixture owner as the session owner."""
    monkeypatch.setenv("PAWPAL_LLM_MODE", "mock")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(llm_client, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(agent, "setup_logging", lambda *args, **kwargs: None)
    app = AppTest.from_file(APP_PATH, default_timeout=15)
    app.session_state["owner"] = build_fixture_owner()
    app.run()
    assert not app.exception
    return app


def _owner(app):
    return app.session_state["owner"]


def _tasks(app):
    """(pet name, description, HH:MM) for every task on the session owner."""
    return sorted(
        (pet.name, task.description, task.time.strftime("%H:%M"))
        for pet in _owner(app).pets
        for task in pet.get_tasks()
    )


def _plan(app, request):
    app.text_area(key="ai_request").set_value(request)
    app.button(key="ai_plan_button").click().run()
    assert not app.exception
    return app


def _keys(app):
    return {button.key for button in app.button}


def _texts(elements):
    return [element.value for element in elements]


def _pending(app):
    return "pending_agent_result" in app.session_state


def _all_rendered_text(app):
    """Every string the page shows: text elements plus every table cell."""
    parts = []
    for kind in ("markdown", "caption", "info", "warning", "error", "success", "subheader"):
        parts += [str(value) for value in _texts(getattr(app, kind))]
    for table in app.table:
        parts.append(table.value.to_string())
    for button in app.button:
        parts.append(str(button.label))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Page basics
# ---------------------------------------------------------------------------


def test_app_runs_in_mock_mode_with_the_ai_section(at):
    assert "Ask PawPal (AI)" in _texts(at.subheader)
    assert any(text.startswith("Mock mode: recorded responses, no API calls.") for text in _texts(at.info))
    assert {f"ai_example_{index}" for index in range(6)} <= _keys(at)
    assert {"ai_plan_button", "generate_schedule"} <= _keys(at)
    assert not _pending(at)


def test_demo_request_button_fills_the_text_area(at):
    at.button(key="ai_example_0").click().run()
    assert at.text_area(key="ai_request").value == F1


def test_blank_request_warns_and_runs_nothing(at):
    _plan(at, "   ")
    assert "Type a request first." in _texts(at.warning)
    assert not _pending(at)


# ---------------------------------------------------------------------------
# Fixture 2: a conflict fixed by a revision
# ---------------------------------------------------------------------------


def test_fixture_2_shows_both_plans_and_approving_the_revision_commits_it(at):
    _plan(at, F2)

    assert {"#### Original plan", "#### Revised plan"} <= set(_texts(at.markdown))
    assert len(at.table) == 2
    original, revised = (table.value for table in at.table)
    assert list(original.columns) == ["Task", "Pet", "Start", "End", "Min", "Result"]
    assert list(original["Start"]) == ["08:00", "08:15"]
    assert list(revised["Start"]) == ["08:00", "08:30"]
    assert set(original["Pet"]) == {"Biscuit"}
    assert any("'walk' and 'meds' overlap" in text for text in _texts(at.warning))
    assert any("meds: 08:15 -> 08:30 (" in text for text in _texts(at.markdown))
    assert any(text.startswith("Stop reason: clean") for text in _texts(at.caption))
    assert {"ai_approve_revised", "ai_approve_original", "ai_discard"} <= _keys(at)
    assert _pending(at)
    assert _tasks(at) == []  # the run never touched the real owner

    at.button(key="ai_approve_revised").click().run()
    assert not at.exception
    assert _tasks(at) == [("Biscuit", "meds", "08:30"), ("Biscuit", "walk", "08:00")]
    assert not _pending(at)
    assert "Added 2 task(s) to Biscuit." in _texts(at.success)
    # Buttons are gone, so the same result can't be approved twice.
    assert not {"ai_approve_revised", "ai_approve_original", "ai_discard"} & _keys(at)


def test_fixture_2_approve_original_keeps_meds_at_08_15(at):
    _plan(at, F2)
    at.button(key="ai_approve_original").click().run()
    assert not at.exception
    assert _tasks(at) == [("Biscuit", "meds", "08:15"), ("Biscuit", "walk", "08:00")]
    assert not _pending(at)


def test_discard_leaves_the_owner_unchanged(at):
    _plan(at, F2)
    at.button(key="ai_discard").click().run()
    assert not at.exception
    assert _tasks(at) == []
    assert _owner(at).available_time_minutes == 120
    assert not _pending(at)
    assert "Discarded: nothing was changed." in _texts(at.success)
    assert not {"ai_approve_revised", "ai_approve_original", "ai_discard"} & _keys(at)


def test_no_task_ids_are_ever_rendered(at):
    _plan(at, F2)
    assert not TASK_ID.search(_all_rendered_text(at))
    at.button(key="ai_approve_revised").click().run()
    # The committed tasks now exist with task-xxxxxxxx ids; none of them reach the page.
    assert len(_owner(at).get_all_tasks()) == 2
    assert not TASK_ID.search(_all_rendered_text(at))
    assert "new-1" not in _all_rendered_text(at)


# ---------------------------------------------------------------------------
# Fixture 3: over budget, and the budget widget regression
# ---------------------------------------------------------------------------


def test_fixture_3_budget_survives_the_next_rerun(at):
    _plan(at, F3)
    assert "ai_approve_revised" not in _keys(at)
    assert "#### Plan" in _texts(at.markdown)
    assert any(text.startswith("Didn't fit in the time budget:") and "brushing (20 min)" in text
               for text in _texts(at.warning))
    assert "Budget: 25 of 30 min used" in _texts(at.markdown)
    assert _owner(at).available_time_minutes == 120  # not applied before approval

    at.button(key="ai_approve_original").click().run()
    assert not at.exception
    assert _owner(at).available_time_minutes == 30
    assert at.number_input(key="owner_time_input").value == 30

    # Any later rerun must not write the widget's stale 120 back over the committed 30.
    at.button(key="generate_schedule").click().run()
    assert not at.exception
    assert at.number_input(key="owner_time_input").value == 30
    assert _owner(at).available_time_minutes == 30
    assert "Fits the most overdue tasks into 30 minutes, most urgent first." in _texts(at.caption)


def test_stale_result_shows_an_error_and_changes_nothing(at):
    _plan(at, F3)
    owner = _owner(at)
    owner.pets[:] = [pet for pet in owner.pets if pet.name != "Whiskers"]

    at.button(key="ai_approve_original").click().run()
    assert not at.exception
    assert any("p-whiskers" in text and "Nothing was changed" in text for text in _texts(at.error))
    assert _tasks(at) == []
    assert _owner(at).available_time_minutes == 120
    assert not _pending(at)


# ---------------------------------------------------------------------------
# Results that can't be approved
# ---------------------------------------------------------------------------


def test_fixture_1_asks_questions_and_can_only_be_dismissed(at):
    _plan(at, F1)
    questions = next(text for text in _texts(at.warning) if text.startswith("I need a bit more information:"))
    assert "How many minutes does 'meds' take?" in questions
    assert "What time for 'walk'?" in questions
    assert not {"ai_approve_revised", "ai_approve_original", "ai_discard"} & _keys(at)
    assert "ai_dismiss" in _keys(at)

    at.button(key="ai_dismiss").click().run()
    assert not at.exception
    assert not _pending(at)
    assert "ai_dismiss" not in _keys(at)
    assert _tasks(at) == []


def test_unknown_request_lists_the_supported_requests(at):
    _plan(at, "Feed the goldfish at noon.")
    message = next(text for text in _texts(at.warning) if "no recorded response" in text)
    for request in (F1, F2, F3, F4, F5, F6):
        assert f"- {request}" in message
    assert "ai_dismiss" in _keys(at)
