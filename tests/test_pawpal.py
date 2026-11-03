"""Tests for the PawPal+ core domain classes."""

from datetime import datetime

from pawpal_system import Owner, Pet, Scheduler, Task


def test_mark_complete_marks_the_task_completed():
    """A new task starts incomplete and mark_complete() flips it to done."""
    task = Task(
        task_id="t1",
        description="Morning walk",
        time=datetime(2026, 9, 22, 8, 0),
        frequency="daily",
        duration_minutes=30,
    )

    assert task.is_completed is False

    task.mark_complete()

    assert task.is_completed is True


def test_add_task_attaches_the_task_to_the_pet():
    """A new pet has no tasks, and add_task() puts one on its list."""
    pet = Pet(pet_id="p1", name="Mochi", species="dog")

    assert len(pet.get_tasks()) == 0

    task = Task(
        task_id="t2",
        description="Evening medication",
        time=datetime(2026, 9, 22, 19, 0),
        frequency="daily",
        duration_minutes=5,
    )
    pet.add_task(task)

    assert len(pet.get_tasks()) == 1


def test_find_conflicts_flags_overlapping_tasks():
    """A 30-minute task at 8:00 collides with a task starting at 8:10."""
    walk = Task(
        task_id="t1",
        description="Morning walk",
        time=datetime(2026, 9, 22, 8, 0),
        frequency="daily",
        duration_minutes=30,
    )
    pill = Task(
        task_id="t2",
        description="Morning medication",
        time=datetime(2026, 9, 22, 8, 10),
        frequency="daily",
        duration_minutes=5,
    )

    conflicts = Scheduler().find_conflicts([walk, pill])

    assert len(conflicts) == 1
    assert conflicts[0][0].task_id == "t1"
    assert conflicts[0][1].task_id == "t2"


def test_find_conflicts_returns_empty_when_tasks_do_not_overlap():
    """Back-to-back tasks are fine: one ending exactly when the next starts is no conflict."""
    walk = Task(
        task_id="t1",
        description="Morning walk",
        time=datetime(2026, 9, 22, 8, 0),
        frequency="daily",
        duration_minutes=30,
    )
    breakfast = Task(
        task_id="t2",
        description="Breakfast",
        time=datetime(2026, 9, 22, 8, 30),
        frequency="daily",
        duration_minutes=10,
    )

    assert Scheduler().find_conflicts([walk, breakfast]) == []


def test_find_conflicts_ignores_completed_tasks():
    """A finished task never conflicts, since completed tasks linger on a pet's list."""
    done_walk = Task(
        task_id="t1",
        description="Morning walk",
        time=datetime(2026, 9, 22, 8, 0),
        frequency="daily",
        is_completed=True,
        duration_minutes=30,
    )
    pill = Task(
        task_id="t2",
        description="Morning medication",
        time=datetime(2026, 9, 22, 8, 10),
        frequency="daily",
        duration_minutes=5,
    )

    assert Scheduler().find_conflicts([done_walk, pill]) == []


def test_find_conflicts_catches_non_adjacent_overlap():
    """A long task conflicts with every task it runs past, not just the one right after it."""
    walk = Task(
        task_id="t1",
        description="Long morning walk",
        time=datetime(2026, 9, 22, 8, 0),
        frequency="daily",
        duration_minutes=60,
    )
    pill = Task(
        task_id="t2",
        description="Morning medication",
        time=datetime(2026, 9, 22, 8, 10),
        frequency="daily",
        duration_minutes=5,
    )
    feed = Task(
        task_id="t3",
        description="Breakfast",
        time=datetime(2026, 9, 22, 8, 30),
        frequency="daily",
        duration_minutes=10,
    )

    conflicts = Scheduler().find_conflicts([walk, pill, feed])

    # The walk runs until 9:00, so it collides with both later tasks; the pill
    # ends at 8:15 and so clears the 8:30 breakfast.
    assert [(first.task_id, second.task_id) for first, second in conflicts] == [
        ("t1", "t2"),
        ("t1", "t3"),
    ]


def test_generate_plan_reports_conflicts():
    """generate_plan() surfaces overlaps under "conflicts" and warns about them."""
    pet = Pet(pet_id="p1", name="Mochi", species="dog")
    pet.add_task(
        Task(
            task_id="t1",
            description="Morning walk",
            time=datetime(2026, 9, 22, 8, 0),
            frequency="daily",
            duration_minutes=30,
        )
    )
    pet.add_task(
        Task(
            task_id="t2",
            description="Morning medication",
            time=datetime(2026, 9, 22, 8, 10),
            frequency="daily",
            duration_minutes=5,
        )
    )
    owner = Owner(owner_id="o1", name="Alex", available_time_minutes=45)
    owner.add_pet(pet)

    plan = Scheduler().generate_plan(owner, datetime(2026, 9, 22, 9, 0))

    assert len(plan["conflicts"]) == 1
    assert any("overlap" in explanation for explanation in plan["explanations"])
    # Reporting only: both tasks still fit the budget and stay scheduled.
    assert len(plan["scheduled"]) == 2
    assert plan["deferred"] == []


def test_sort_by_time_orders_tasks_chronologically():
    """Four tasks added out of order come back earliest-first."""
    nail_trim = Task(
        task_id="t3",
        description="Nail trim",
        time=datetime(2026, 9, 22, 16, 0),
        frequency="weekly",
        duration_minutes=20,
    )
    walk = Task(
        task_id="t1",
        description="Morning walk",
        time=datetime(2026, 9, 22, 8, 0),
        frequency="daily",
        duration_minutes=30,
    )
    medication = Task(
        task_id="t4",
        description="Evening medication",
        time=datetime(2026, 9, 22, 19, 0),
        frequency="daily",
        duration_minutes=5,
    )
    breakfast = Task(
        task_id="t2",
        description="Breakfast",
        time=datetime(2026, 9, 22, 8, 30),
        frequency="daily",
        duration_minutes=10,
    )

    ordered = Scheduler().sort_by_time([nail_trim, walk, medication, breakfast])

    assert [task.task_id for task in ordered] == ["t1", "t2", "t3", "t4"]
    assert [task.time for task in ordered] == [
        datetime(2026, 9, 22, 8, 0),
        datetime(2026, 9, 22, 8, 30),
        datetime(2026, 9, 22, 16, 0),
        datetime(2026, 9, 22, 19, 0),
    ]


# Protects against a regression where sort_by_time() returns tasks in insertion order
# instead of chronological order, which would make main.py's schedule listing meaningless.


def test_sort_by_time_does_not_mutate_the_input_list():
    """Sorting hands back a new list and leaves the caller's own list alone."""
    walk = Task(
        task_id="t2",
        description="Morning walk",
        time=datetime(2026, 9, 22, 8, 0),
        frequency="daily",
        duration_minutes=30,
    )
    breakfast = Task(
        task_id="t1",
        description="Breakfast",
        time=datetime(2026, 9, 22, 7, 0),
        frequency="daily",
        duration_minutes=10,
    )
    tasks = [walk, breakfast]

    ordered = Scheduler().sort_by_time(tasks)

    assert [task.task_id for task in ordered] == ["t1", "t2"]
    assert [task.task_id for task in tasks] == ["t2", "t1"]


# Protects against sort_by_time() being "optimized" into an in-place list.sort(), which
# would silently reorder a pet's own task list every time the UI displayed a schedule.


def test_complete_task_creates_next_occurrence_exactly_one_day_later():
    """Completing a daily task queues a fresh one 24 hours after the original's time."""
    pet = Pet(pet_id="p1", name="Biscuit", species="dog")
    pet.add_task(
        Task(
            task_id="t1",
            description="Morning walk",
            time=datetime(2026, 9, 22, 8, 0),
            frequency="daily",
            duration_minutes=30,
        )
    )

    upcoming = pet.complete_task("t1")

    # Dated off the original's `time`, not off whenever the task happened to be
    # marked done, so a late completion doesn't drift the whole schedule.
    assert upcoming.time == datetime(2026, 9, 23, 8, 0)
    assert upcoming.is_completed is False
    assert upcoming.duration_minutes == 30
    assert len(pet.get_tasks()) == 2


# Protects against next_occurrence() being rewritten to count forward from
# datetime.now(), which would make recurrence non-deterministic and quietly shift a
# task's time every time an owner completed it a few minutes late.


def test_complete_task_returns_none_for_a_non_recurring_task():
    """A frequency with no recurrence rule completes the task but queues nothing."""
    pet = Pet(pet_id="p1", name="Biscuit", species="dog")
    pet.add_task(
        Task(
            task_id="t1",
            description="Vet visit",
            time=datetime(2026, 9, 22, 8, 0),
            frequency="once",
            duration_minutes=60,
        )
    )

    assert pet.complete_task("t1") is None
    assert pet.get_tasks()[0].is_completed is True
    assert len(pet.get_tasks()) == 1


# Protects against a one-off task being handed a bogus recurrence and reappearing on
# the schedule forever, and pins down that a None return still means the task got done.


def test_find_conflicts_flags_tasks_at_the_exact_same_time():
    """Two tasks starting at the same moment, both taking real time, collide."""
    walk = Task(
        task_id="t1",
        description="Evening walk",
        time=datetime(2026, 9, 22, 18, 0),
        frequency="daily",
        duration_minutes=15,
    )
    playtime = Task(
        task_id="t2",
        description="Evening playtime",
        time=datetime(2026, 9, 22, 18, 0),
        frequency="daily",
        duration_minutes=20,
    )

    conflicts = Scheduler().find_conflicts([walk, playtime])

    assert len(conflicts) == 1
    assert {conflicts[0][0].task_id, conflicts[0][1].task_id} == {"t1", "t2"}


# Protects against the most obvious real-world double-booking an owner can create:
# two tasks entered at the identical timestamp, which main.py's demo data relies on.


def test_generate_plan_excludes_a_next_occurrence_dated_for_tomorrow():
    """Today's plan ignores the fresh occurrence that completing a daily task queued."""
    pet = Pet(pet_id="p1", name="Biscuit", species="dog")
    pet.add_task(
        Task(
            task_id="t1",
            description="Morning walk",
            time=datetime(2026, 9, 22, 8, 0),
            frequency="daily",
            duration_minutes=30,
        )
    )
    owner = Owner(owner_id="o1", name="Alex", available_time_minutes=120)
    owner.add_pet(pet)

    upcoming = pet.complete_task("t1")
    plan = Scheduler().generate_plan(owner, datetime(2026, 9, 22, 9, 0))

    assert upcoming.time == datetime(2026, 9, 23, 8, 0)
    assert upcoming.is_due(datetime(2026, 9, 22, 9, 0)) is False
    # The budget is roomy enough to hold it, so its absence is about the due
    # check rather than about running out of time.
    assert upcoming.task_id not in [task.task_id for task in plan["scheduled"]]
    assert upcoming.task_id not in [task.task_id for task in plan["deferred"]]


# Protects against a regression of the is_due() bug where any incomplete task counted
# as due regardless of its time, so completing a task immediately re-added tomorrow's
# copy to today's plan and made the schedule grow every time an owner finished a task.


def test_find_conflicts_does_not_flag_zero_duration_tasks_at_the_same_time():
    """Two zero-duration tasks sharing a timestamp are not treated as a conflict."""
    scoop = Task(
        task_id="t1",
        description="Litter box scoop",
        time=datetime(2026, 9, 22, 18, 0),
        frequency="daily",
    )
    refill = Task(
        task_id="t2",
        description="Refill water bowl",
        time=datetime(2026, 9, 22, 18, 0),
        frequency="daily",
    )

    assert Scheduler().find_conflicts([scoop, refill]) == []


# Documents the known limitation recorded in reflection.md: _overlaps() uses `>` so that
# back-to-back tasks aren't flagged, which as a side effect means a zero-duration task
# can never conflict with anything. Pinned here so the tradeoff is a deliberate choice
# rather than something a future change breaks or "fixes" by accident.


def test_sorting_and_conflict_detection_handle_an_empty_task_list():
    """A pet with no tasks produces empty results instead of an error."""
    pet = Pet(pet_id="p1", name="Biscuit", species="dog")
    owner = Owner(owner_id="o1", name="Alex", available_time_minutes=45)
    owner.add_pet(pet)
    scheduler = Scheduler()

    assert owner.get_all_tasks() == []
    assert scheduler.sort_by_time([]) == []
    assert scheduler.find_conflicts([]) == []

    plan = scheduler.generate_plan(owner, datetime(2026, 9, 22, 9, 0))

    assert plan["scheduled"] == []
    assert plan["deferred"] == []
    assert plan["conflicts"] == []
    assert plan["explanations"] == []


# Protects against a new owner's first visit crashing the Streamlit UI: app.py indexes
# straight into the plan's four keys, so they all have to exist even with no tasks.


def test_completed_as_needed_task_is_not_due_again():
    """A finished one-off task stays done, even a week after it was completed."""
    task = Task(
        task_id="t1",
        description="Vet visit",
        time=datetime(2026, 9, 22, 8, 0),
        frequency="as needed",
        is_completed=True,
        duration_minutes=60,
        completed_at=datetime(2026, 9, 22, 9, 0),
    )

    assert task.is_due(datetime(2026, 9, 29, 9, 0)) is False


# Protects against a regression of the is_due() fall-through where any completed task
# without a daily or weekly rule counted as always due, so a finished one-off task
# reappeared in every plan forever.


def test_completed_daily_task_is_due_again_at_24_hours():
    """A finished daily task isn't due at 23h59m after completion, and is due at 24h."""
    task = Task(
        task_id="t1",
        description="Morning walk",
        time=datetime(2026, 9, 22, 8, 0),
        frequency="daily",
        is_completed=True,
        duration_minutes=30,
        completed_at=datetime(2026, 9, 22, 8, 30),
    )

    assert task.is_due(datetime(2026, 9, 23, 8, 29)) is False
    assert task.is_due(datetime(2026, 9, 23, 8, 30)) is True


# Protects the daily rule from being caught up in the non-recurring fix: a completed
# daily task must still come back exactly 24 hours after it was done.


def test_pending_as_needed_task_is_due_once_its_time_arrives():
    """A not-yet-done one-off task isn't due a minute early, and is due at its time."""
    task = Task(
        task_id="t1",
        description="Vet visit",
        time=datetime(2026, 9, 22, 8, 0),
        frequency="as needed",
        duration_minutes=60,
    )

    assert task.is_due(datetime(2026, 9, 22, 7, 59)) is False
    assert task.is_due(datetime(2026, 9, 22, 8, 0)) is True


# Protects against the non-recurring fix leaking into the pending branch: a one-off
# task that hasn't been done yet must still show up once its time arrives.
