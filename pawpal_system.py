"""Core domain classes for PawPal+ (Owner, Pet, Task, Scheduler)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

# Only these frequencies have a recurrence rule simple enough to automate.
_RECURRENCE_DELTAS: dict[str, timedelta] = {
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
}


@dataclass
class Task:
    """A single unit of pet care to be scheduled, such as a walk or a dose of medication."""

    task_id: str
    description: str
    time: datetime
    frequency: str
    is_completed: bool = False
    duration_minutes: int = 0
    completed_at: datetime | None = None

    def is_due(self, current_time: datetime) -> bool:
        """Report whether this task needs doing as of the given time."""
        if not self.is_completed:
            # A pending task is only due once its scheduled time has arrived.
            # Without this, a next occurrence generated for tomorrow would be
            # treated as due today, since it starts out incomplete.
            return current_time >= self.time

        # Fall back to `time` when a task was marked complete before
        # `completed_at` existed (or was set directly without it).
        last_done = self.completed_at if self.completed_at is not None else self.time
        elapsed = current_time - last_done
        frequency = self.frequency.strip().lower()

        if frequency == "daily":
            return elapsed >= timedelta(hours=24)
        if frequency == "weekly":
            return elapsed >= timedelta(days=7)

        # Any other frequency (e.g. "as needed") has no recurrence rule, so a
        # completed task is done for good, matching next_occurrence() returning None.
        return False

    def mark_complete(self) -> None:
        """Record that this task was finished, leaving its scheduled `time` untouched."""
        self.is_completed = True
        self.completed_at = datetime.now()

    def next_occurrence(self) -> Task | None:
        """Build the next fresh occurrence of this task, or None if it doesn't recur."""
        delta = _RECURRENCE_DELTAS.get(self.frequency.strip().lower())
        if delta is None:
            return None

        return Task(
            task_id=self._next_task_id(),
            description=self.description,
            time=self.time + delta,
            frequency=self.frequency,
            is_completed=False,
            duration_minutes=self.duration_minutes,
            completed_at=None,
        )

    def _next_task_id(self) -> str:
        """Derive the next occurrence's id by bumping a trailing '-r<N>' counter."""
        stem, separator, suffix = self.task_id.rpartition("-r")
        if separator and suffix.isdigit():
            return f"{stem}-r{int(suffix) + 1}"
        return f"{self.task_id}-r2"


@dataclass
class Pet:
    """A pet's profile and the care tasks attached to it."""

    pet_id: str
    name: str
    species: str
    tasks: list[Task] = field(default_factory=list)

    def add_task(self, task: Task) -> None:
        """Attach a care task to this pet."""
        self.tasks.append(task)

    def get_tasks(self) -> list[Task]:
        """Return every task attached to this pet."""
        return self.tasks

    def complete_task(self, task_id: str) -> Task | None:
        """Mark one of this pet's tasks done, attaching and returning its next occurrence."""
        for task in self.tasks:
            if task.task_id == task_id:
                task.mark_complete()
                upcoming = task.next_occurrence()
                if upcoming is not None:
                    self.add_task(upcoming)
                return upcoming
        return None


class Owner:
    """The person responsible for one or more pets and their daily time budget."""

    def __init__(
        self,
        owner_id: str,
        name: str,
        pets: list[Pet] | None = None,
        available_time_minutes: int = 0,
    ) -> None:
        """Create an owner with their pets and daily care time budget."""
        self.owner_id: str = owner_id
        self.name: str = name
        self.pets: list[Pet] = pets if pets is not None else []
        self.available_time_minutes: int = available_time_minutes

    def add_pet(self, pet: Pet) -> None:
        """Put a pet under this owner's care."""
        self.pets.append(pet)

    def get_all_tasks(self) -> list[Task]:
        """Collect every task across all of this owner's pets."""
        all_tasks: list[Task] = []
        for pet in self.pets:
            all_tasks.extend(pet.get_tasks())
        return all_tasks

    def get_tasks_for_pet(self, pet_id: str) -> list[Task]:
        """Return a copy of one pet's task list, or an empty list if no such pet."""
        for pet in self.pets:
            if pet.pet_id == pet_id:
                return list(pet.get_tasks())
        return []

    def get_tasks_by_status(self, is_completed: bool) -> list[Task]:
        """Return a new list of this owner's tasks matching the given completion status."""
        return [task for task in self.get_all_tasks() if task.is_completed == is_completed]


class Scheduler:
    """Turns an owner's due tasks and time budget into a daily plan."""

    def generate_plan(self, owner: Owner, current_time: datetime) -> dict:
        """Fit the most urgent due tasks into the owner's time budget, explaining each call."""
        due_tasks = [task for task in owner.get_all_tasks() if task.is_due(current_time)]
        ordered_tasks = self._sort_by_priority(due_tasks)

        remaining_minutes = owner.available_time_minutes
        scheduled: list[Task] = []
        deferred: list[Task] = []
        explanations: list[str] = []

        for task in ordered_tasks:
            if self._fits(task, remaining_minutes):
                scheduled.append(task)
                remaining_minutes -= task.duration_minutes
                explanations.append(
                    f"Scheduled '{task.description}' ({task.duration_minutes} min): "
                    f"it fit the time left, leaving {remaining_minutes} min."
                )
            else:
                deferred.append(task)
                explanations.append(
                    f"Deferred '{task.description}' ({task.duration_minutes} min): "
                    f"only {remaining_minutes} min left in the budget."
                )

        conflicts = self.find_conflicts(due_tasks)
        for first, second in conflicts:
            explanations.append(
                f"Warning: '{first.description}' and '{second.description}' overlap "
                f"— both run at the same time."
            )

        return {
            "scheduled": scheduled,
            "deferred": deferred,
            "explanations": explanations,
            "conflicts": conflicts,
        }

    def sort_by_time(self, tasks: list[Task]) -> list[Task]:
        """Return a new list ordered by scheduled time, with `task_id` breaking ties."""
        return sorted(tasks, key=lambda task: (task.time, task.task_id))

    def find_conflicts(self, tasks: list[Task]) -> list[tuple[Task, Task]]:
        """Pair up every still-pending task whose duration runs into a later task's start."""
        ordered = self.sort_by_time([task for task in tasks if not task.is_completed])
        conflicts: list[tuple[Task, Task]] = []

        for index, first in enumerate(ordered):
            for second in ordered[index + 1 :]:
                # Sorted by start time, so once one task starts after `first` ends,
                # every later task does too and none of them can overlap either.
                if not self._overlaps(first, second):
                    break
                conflicts.append((first, second))

        return conflicts

    def _overlaps(self, first: Task, second: Task) -> bool:
        """Report whether `first` is still running when `second` is due to start."""
        return first.time + timedelta(minutes=first.duration_minutes) > second.time

    def _sort_by_priority(self, tasks: list[Task]) -> list[Task]:
        """Order tasks most urgent first, judging urgency by how overdue each one is."""
        # Never-completed tasks outrank completed-but-recurring ones, then the
        # oldest `time` wins, since that is the task that has waited longest.
        return sorted(
            tasks,
            key=lambda task: (task.is_completed, task.time, task.duration_minutes, task.task_id),
        )

    def _fits(self, task: Task, remaining_minutes: int) -> bool:
        """Report whether a task still fits in the time left in the plan."""
        return task.duration_minutes <= remaining_minutes
