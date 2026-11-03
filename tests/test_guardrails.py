"""Tests for the guardrails that check the LLM's parsed request before it reaches the Owner."""

import copy
from datetime import date, datetime

from fixture_owner import build_fixture_owner
from guardrails import NO_TASKS_MESSAGE, validate_request
from pawpal_system import Owner, Pet, Task

NOW = datetime(2026, 11, 2, 7, 0)
REQUEST = "Biscuit needs a 30 minute walk at 8:00 today, daily."

# Passed as an override to remove a field, since None is itself a value the model can send.
_OMIT = object()


def _walk(**overrides):
    """Build one fully valid parsed task for Biscuit, with any fields replaced or removed."""
    task = {
        "pet_name": "Biscuit",
        "description": "Morning walk",
        "quote": "30 minute walk at 8:00",
        "date": "today",
        "time": "08:00",
        "duration_minutes": 30,
        "frequency": "daily",
    }
    for key, value in overrides.items():
        if value is _OMIT:
            task.pop(key)
        else:
            task[key] = value
    return task


def _validate(tasks, request=REQUEST, owner=None, **extra_args):
    """Run validate_request on a submit_tasks call built from these tasks."""
    owner = owner if owner is not None else build_fixture_owner()
    return validate_request({"tasks": tasks, **extra_args}, request, owner, NOW)


def test_valid_request_builds_a_task_with_ref_id_and_combined_time():
    """A fully specified task comes back as Task new-1 on Biscuit at 08:00 today."""
    result = _validate([_walk()])

    assert result.status == "ok"
    assert result.errors == []
    assert result.plan_date == date(2026, 11, 2)
    assert result.budget is None
    pet_id, ref, task = result.new_tasks[0]
    assert (pet_id, ref) == ("p-biscuit", "new-1")
    assert task.task_id == "new-1"
    assert task.description == "Morning walk"
    assert task.time == datetime(2026, 11, 2, 8, 0)
    assert task.frequency == "daily"
    assert task.duration_minutes == 30
    assert task.is_completed is False


# G1: shape and types


def test_tasks_that_are_not_a_list_are_bad_llm_output():
    """A tasks value that isn't a list stops validation with llm_bad_output."""
    result = validate_request({"tasks": "walk Biscuit"}, REQUEST, build_fixture_owner(), NOW)

    assert result.status == "llm_bad_output"
    assert result.new_tasks == []
    assert len(result.errors) == 1


def test_missing_quote_is_bad_llm_output_not_a_clarification():
    """A task without its required quote string is the model's fault, not the user's."""
    result = _validate([_walk(quote=_OMIT)])

    assert result.status == "llm_bad_output"
    assert "quote" in result.errors[0]


def test_missing_tasks_key_is_bad_llm_output():
    """Arguments with no tasks key at all are malformed."""
    result = validate_request({}, REQUEST, build_fixture_owner(), NOW)

    assert result.status == "llm_bad_output"


# G2: task count


def test_empty_task_list_asks_for_tasks():
    """No tasks at all is a clarification, not a silent empty plan."""
    result = _validate([])

    assert result.status == "needs_clarification"
    assert result.errors == [NO_TASKS_MESSAGE]


def test_eleven_tasks_are_rejected():
    """More than ten tasks in one request is refused outright."""
    tasks = [_walk(time=f"{hour:02d}:00") for hour in range(8, 19)]

    result = _validate(tasks)

    assert len(tasks) == 11
    assert result.status == "needs_clarification"
    assert len(result.errors) == 1
    assert result.new_tasks == []


# Protects against an oversized batch, whether from a long request or a runaway model,
# flooding the Owner with tasks the user never meant to add.


# G3: pet name


def test_unknown_pet_asks_which_pet():
    """A pet name the owner doesn't have lists the pets they do have."""
    result = _validate([_walk(pet_name="Rex")], request="Rex needs a 30 minute walk at 8:00")

    assert result.status == "needs_clarification"
    assert result.errors == ["Which pet? You have: Biscuit, Whiskers"]


def test_pet_name_match_is_trimmed_and_case_insensitive():
    """' biscuit ' still resolves to Biscuit's pet_id."""
    result = _validate([_walk(pet_name="  biscuit ")])

    assert result.status == "ok"
    assert result.new_tasks[0][0] == "p-biscuit"


def test_two_pets_sharing_a_name_is_ambiguous():
    """When two pets are both named Biscuit, the request can't pick one."""
    owner = Owner(owner_id="o1", name="Alex", available_time_minutes=120)
    owner.add_pet(Pet(pet_id="p1", name="Biscuit", species="dog"))
    owner.add_pet(Pet(pet_id="p2", name="Biscuit", species="cat"))

    result = _validate([_walk()], owner=owner)

    assert result.status == "needs_clarification"
    assert result.errors == ["Which pet? You have: Biscuit, Biscuit"]


# G4: description


def test_blank_description_is_rejected():
    """A description that is only whitespace counts as empty."""
    result = _validate([_walk(description="   ")])

    assert result.status == "needs_clarification"
    assert len(result.errors) == 1


def test_description_over_80_characters_is_rejected():
    """81 characters is one too many; 80 is fine."""
    too_long = _validate([_walk(description="x" * 81)])
    just_right = _validate([_walk(description="x" * 80)])

    assert too_long.status == "needs_clarification"
    assert just_right.status == "ok"


# G5: duration


def test_zero_duration_is_rejected():
    """A 0-minute task would always fit and never conflict, so it must be asked about."""
    result = _validate([_walk(duration_minutes=0)])

    assert result.status == "needs_clarification"
    assert result.errors[0].startswith("How many minutes does 'Morning walk' take?")


# Protects against the duration_minutes=0 default leaking in from the LLM: the Scheduler
# would book it for free and find_conflicts could never flag it.


def test_missing_duration_asks_how_many_minutes():
    """An omitted duration is never defaulted."""
    result = _validate([_walk(duration_minutes=_OMIT)])

    assert result.errors == ["How many minutes does 'Morning walk' take?"]


def test_whole_number_float_duration_is_accepted_as_an_int():
    """30.0, as some SDKs deliver JSON numbers, becomes the int 30."""
    result = _validate([_walk(duration_minutes=30.0)])

    task = result.new_tasks[0][2]
    assert result.status == "ok"
    assert task.duration_minutes == 30
    assert type(task.duration_minutes) is int


def test_true_is_rejected_as_a_duration():
    """True is an int subclass in Python, but it isn't one minute."""
    result = _validate([_walk(duration_minutes=True)])

    assert result.status == "needs_clarification"


# Protects against the isinstance(True, int) trap: without an explicit bool check, a
# model emitting `true` would schedule a 1-minute task.


def test_fractional_string_and_too_long_durations_are_rejected():
    """30.5, "30" and 241 each fail the duration rule."""
    for bad in (30.5, "30", 241):
        result = _validate([_walk(duration_minutes=bad)])
        assert result.status == "needs_clarification", bad


# G6: frequency


def test_every_6_hours_frequency_is_rejected():
    """A recurrence the scheduler can't represent asks instead of being coerced."""
    result = _validate([_walk(frequency="every 6 hours")])

    assert result.status == "needs_clarification"
    assert "every 6 hours" in result.errors[0]


def test_frequency_is_normalized_before_storing():
    """' Weekly ' is stored on the Task as 'weekly'."""
    result = _validate([_walk(frequency=" Weekly ")])

    assert result.new_tasks[0][2].frequency == "weekly"


def test_empty_string_frequency_is_rejected():
    """An empty frequency is an error, not the same as an omitted one."""
    result = _validate([_walk(frequency="")])

    assert result.status == "needs_clarification"
    assert result.assumptions == []


# G7: date


def test_unreadable_date_is_rejected():
    """'next blursday' isn't today, tomorrow or YYYY-MM-DD."""
    result = _validate([_walk(date="next blursday")])

    assert result.status == "needs_clarification"
    assert "next blursday" in result.errors[0]


def test_unpadded_iso_date_is_rejected():
    """'2026-1-5' fails the strict YYYY-MM-DD shape, even though strptime would accept it."""
    result = _validate([_walk(date="2026-1-5")])

    assert result.status == "needs_clarification"


# Protects against relying on strptime alone, which happily parses unpadded dates and
# would let loosely formatted model output through.


def test_past_date_and_date_more_than_7_days_out_are_rejected():
    """Yesterday and 8 days out both fail; exactly 7 days out passes."""
    yesterday = _validate([_walk(date="2026-11-01")])
    eight_days = _validate([_walk(date="2026-11-10")])
    seven_days = _validate([_walk(date="2026-11-09")])

    assert yesterday.status == "needs_clarification"
    assert "past" in yesterday.errors[0]
    assert eight_days.status == "needs_clarification"
    assert seven_days.status == "ok"


def test_tomorrow_is_relative_to_now_and_case_insensitive():
    """'Tomorrow' resolves against the injected now, not the real clock."""
    result = _validate([_walk(date="Tomorrow")])

    assert result.plan_date == date(2026, 11, 3)
    assert result.new_tasks[0][2].time == datetime(2026, 11, 3, 8, 0)


def test_omitted_date_and_frequency_produce_two_assumptions():
    """Missing date and frequency are defaulted by code and both reported."""
    result = _validate([_walk(date=_OMIT, frequency=_OMIT)])

    task = result.new_tasks[0][2]
    assert result.status == "ok"
    assert len(result.assumptions) == 2
    assert task.frequency == "as needed"
    assert task.time == datetime(2026, 11, 2, 8, 0)


# G8: time


def test_time_25_00_is_rejected():
    """'25:00' is not a 24-hour time."""
    result = _validate([_walk(time="25:00")])

    assert result.errors == ["What time for 'Morning walk'?"]


def test_missing_time_asks_what_time():
    """'this morning' arrives as no time at all, and is asked about rather than guessed."""
    result = _validate([_walk(time=_OMIT)])

    assert result.errors == ["What time for 'Morning walk'?"]


def test_time_is_stripped_before_matching():
    """Surrounding spaces don't make a valid time invalid."""
    result = _validate([_walk(time=" 08:00 ")])

    assert result.status == "ok"


# G9: quote


def test_quote_mismatch_is_a_warning_and_the_task_is_kept():
    """A quote not found in the request flags the task but still returns ok."""
    result = _validate([_walk(quote="45 minute run at dawn")])

    assert result.status == "ok"
    assert len(result.new_tasks) == 1
    assert len(result.warnings) == 1
    assert "45 minute run at dawn" in result.warnings[0]


def test_quote_match_ignores_case_and_spacing():
    """Different capitalization and doubled spaces still count as a match."""
    result = _validate([_walk(quote="30  MINUTE walk\nat 8:00")])

    assert result.warnings == []


# G10: budget


def test_stated_budget_becomes_the_budget():
    """available_minutes of 45, or 45.0, becomes budget 45."""
    assert _validate([_walk()], available_minutes=45).budget == 45
    assert _validate([_walk()], available_minutes=45.0).budget == 45


def test_out_of_range_or_bool_budget_is_rejected():
    """1441 minutes and True both fail the budget rule."""
    for bad in (1441, -1, True, "45"):
        result = _validate([_walk()], available_minutes=bad)
        assert result.status == "needs_clarification", bad


# G11: duplicates


def test_duplicate_of_an_existing_task_is_dropped_with_a_warning():
    """A walk already on Biscuit's schedule at the same time isn't added twice."""
    owner = build_fixture_owner()
    owner.pets[0].add_task(
        Task(
            task_id="task-1",
            description="morning WALK",
            time=datetime(2026, 11, 2, 8, 0),
            frequency="daily",
            duration_minutes=30,
        )
    )

    result = _validate([_walk()], owner=owner)

    assert result.status == "ok"
    assert result.new_tasks == []
    assert len(result.warnings) == 1
    assert "already on the schedule" in result.warnings[0]


def test_refs_stay_contiguous_after_a_duplicate_in_the_batch_is_dropped():
    """With the second task a repeat of the first, the third becomes new-2, not new-3."""
    meds = _walk(description="Meds", time="09:00", duration_minutes=5)

    result = _validate([_walk(), _walk(), meds])

    assert [ref for _, ref, _ in result.new_tasks] == ["new-1", "new-2"]
    assert [task.task_id for _, _, task in result.new_tasks] == ["new-1", "new-2"]
    assert result.new_tasks[1][2].description == "Meds"
    assert "repeated in this request" in result.warnings[0]


# Protects the agent's ref scheme: refs label tasks in the revise prompt and fixtures,
# so a gap like new-1, new-3 would make recorded revisions point at the wrong task.


# G12: single date


def test_mixed_date_batch_is_rejected():
    """A walk today and a bath tomorrow must be sent as two requests."""
    bath = _walk(pet_name="Whiskers", description="Bath", date="tomorrow", time="10:00")

    result = _validate([_walk(), bath])

    assert result.status == "needs_clarification"
    assert result.errors == [
        "Your request covers more than one day (Nov 2, Nov 3). Please send one day at a time."
    ]


def test_single_date_check_is_skipped_when_a_date_is_invalid():
    """An unreadable date reports only its own error, not a second mixed-days error."""
    result = _validate([_walk(), _walk(date="someday", time="10:00")])

    assert len(result.errors) == 1
    assert "someday" in result.errors[0]


# Error collection and all-or-nothing


def test_missing_duration_and_missing_time_collect_two_errors_in_rule_order():
    """Both problems are reported at once, the duration question before the time question."""
    result = _validate([_walk(duration_minutes=_OMIT, time=_OMIT)])

    assert result.errors == [
        "How many minutes does 'Morning walk' take?",
        "What time for 'Morning walk'?",
    ]


# Protects the clarification UX: stopping at the first error would make the user answer
# one question, resubmit, and only then hear about the next.


def test_any_error_leaves_new_tasks_empty_even_for_valid_tasks():
    """One bad task in a batch means no task from the batch is returned."""
    result = _validate([_walk(), _walk(pet_name="Rex", time="10:00")], available_minutes=60)

    assert result.status == "needs_clarification"
    assert result.new_tasks == []
    assert result.plan_date is None
    assert result.budget is None


# Protects the all-or-nothing rule: a partly applied request would leave the user's
# schedule half-updated while they're still being asked a question about it.


def test_validation_does_not_mutate_the_args_or_the_owner():
    """The parsed arguments and the owner come out exactly as they went in."""
    args = {
        "tasks": [_walk(frequency=" Weekly "), _walk(date=_OMIT, time="09:00")],
        "available_minutes": 45.0,
    }
    snapshot = copy.deepcopy(args)
    owner = build_fixture_owner()

    validate_request(args, REQUEST, owner, NOW)

    assert args == snapshot
    assert owner.get_all_tasks() == []


# Protects the guardrails' purity: the agent keeps the raw parsed output for its log and
# trace, so normalizing in place would make them show values the model never sent.
