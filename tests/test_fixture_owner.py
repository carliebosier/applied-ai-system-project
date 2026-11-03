"""Tests for the shared fixture owner used by the agent demo and eval."""

from datetime import datetime

from fixture_owner import build_fixture_owner
from pawpal_system import Task


def test_fixture_owner_has_biscuit_and_whiskers_with_fixed_ids():
    """The baseline has exactly two pets, with the names, species, and ids fixtures rely on."""
    owner = build_fixture_owner()

    assert len(owner.pets) == 2
    assert [(pet.pet_id, pet.name, pet.species) for pet in owner.pets] == [
        ("p-biscuit", "Biscuit", "dog"),
        ("p-whiskers", "Whiskers", "cat"),
    ]


# Protects against the pet ids drifting to uuids or the pets being renamed, which
# would break every recorded fixture and make traces differ from run to run.


def test_fixture_owner_starts_with_no_tasks_and_a_120_minute_budget():
    """The baseline is named Demo, has a 120-minute budget, and holds no tasks."""
    owner = build_fixture_owner()

    assert owner.name == "Demo"
    assert owner.available_time_minutes == 120
    assert owner.get_all_tasks() == []


# Protects against seed tasks creeping into the baseline: any pre-existing task could
# add an unmovable conflict or a deferral and make the fixtures' expected outcomes wrong.


def test_fixture_owner_calls_return_independent_objects():
    """Adding a task to one fixture owner leaves a second one untouched."""
    first = build_fixture_owner()
    second = build_fixture_owner()

    first.pets[0].add_task(
        Task(
            task_id="t1",
            description="Morning walk",
            time=datetime(2026, 11, 2, 8, 0),
            frequency="daily",
            duration_minutes=30,
        )
    )

    assert len(first.get_all_tasks()) == 1
    assert second.get_all_tasks() == []


# Protects against the builder being "optimized" into a shared module-level owner,
# which would leak tasks from one eval run or test into the next.
