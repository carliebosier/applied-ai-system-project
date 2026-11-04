"""Deterministic checks that stand between the LLM's parsed request and the Owner's pets."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from pawpal_system import Owner, Task

MAX_TASKS = 10
MAX_DESCRIPTION_CHARS = 80
MIN_DURATION_MINUTES = 1
MAX_DURATION_MINUTES = 240  # Matches the cap on app.py's duration input.
MAX_BUDGET_MINUTES = 1440  # Matches the cap on app.py's time-budget input.
MAX_DAYS_AHEAD = 7

# Only these values have well-defined behavior in Task.is_due and next_occurrence.
ALLOWED_FREQUENCIES = ("daily", "weekly", "as needed")
DEFAULT_FREQUENCY = "as needed"

NO_TASKS_MESSAGE = "I didn't find any pet care tasks"
ALREADY_SCHEDULED_MESSAGE = "Everything in that request is already on the schedule."

_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# strptime alone would accept "2026-1-5", so the exact shape is checked first.
_ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class ValidationResult:
    """The outcome of checking one submit_tasks call, with tasks only if every rule passed."""

    status: str  # "ok", "needs_clarification" or "llm_bad_output"
    new_tasks: list[tuple[str, str, Task]] = field(default_factory=list)  # (pet_id, ref, Task)
    plan_date: date | None = None
    budget: int | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)


@dataclass
class _Candidate:
    """One parsed task's checked fields; None marks a field that failed its rule."""

    pet_id: str | None
    description: str
    duration_minutes: int | None
    frequency: str | None
    day: date | None
    at: time | None


def validate_request(args: dict, request: str, owner: Owner, now: datetime) -> ValidationResult:
    """Check a submit_tasks call's arguments against the rules, collecting every error."""
    shape_error = _shape_error(args)
    if shape_error is not None:
        return ValidationResult(status="llm_bad_output", errors=[shape_error])

    errors: list[str] = []
    warnings: list[str] = []
    assumptions: list[str] = []
    raw_tasks = args["tasks"]
    today = now.date()

    # G2. A batch that's empty or oversized gets one error rather than a pile of
    # per-task errors for tasks the user would have to resend anyway.
    candidates: list[_Candidate] = []
    if not raw_tasks:
        errors.append(NO_TASKS_MESSAGE)
    elif len(raw_tasks) > MAX_TASKS:
        errors.append(
            f"That's {len(raw_tasks)} tasks; please send at most {MAX_TASKS} at a time."
        )
    else:
        for raw in raw_tasks:
            candidates.append(_check_task(raw, owner, today, errors, assumptions))
            _check_quote(raw, request, warnings)

    budget = _check_budget(args, errors)
    plan_date = _check_single_date(candidates, errors)

    # All or nothing: a partly valid request never reaches the Owner.
    if errors:
        return ValidationResult(
            status="needs_clarification",
            # Two tasks can trip the same check with the same wording (e.g. two
            # unknown "Rex" tasks); the user only needs to be asked once.
            errors=list(dict.fromkeys(errors)),
            warnings=warnings,
            assumptions=assumptions,
        )

    new_tasks = _build_tasks(candidates, owner, plan_date, warnings)

    # Every task was a duplicate, so an "ok" would send an empty plan to approval.
    if not new_tasks:
        return ValidationResult(
            status="needs_clarification",
            errors=[ALREADY_SCHEDULED_MESSAGE],
            warnings=warnings,
            assumptions=assumptions,
        )

    return ValidationResult(
        status="ok",
        new_tasks=new_tasks,
        plan_date=plan_date,
        budget=budget,
        errors=errors,
        warnings=warnings,
        assumptions=assumptions,
    )


def _shape_error(args: object) -> str | None:
    """G1: describe the first structural problem in the arguments, or None if well formed."""
    if not isinstance(args, dict):
        return "The parser's output wasn't a set of named arguments."
    tasks = args.get("tasks")
    if not isinstance(tasks, list):
        return "The parser's output had no list of tasks."
    for position, raw in enumerate(tasks, start=1):
        if not isinstance(raw, dict):
            return f"Task {position} in the parser's output wasn't an object."
        for key in ("pet_name", "description", "quote"):
            if not isinstance(raw.get(key), str):
                return f"Task {position} in the parser's output is missing '{key}'."
    return None


def _check_task(
    raw: dict, owner: Owner, today: date, errors: list[str], assumptions: list[str]
) -> _Candidate:
    """Apply G3 through G8 to one task in that order, recording errors and assumptions."""
    description = raw["description"].strip()
    return _Candidate(
        pet_id=_check_pet(raw["pet_name"], owner, errors),
        description=_check_description(description, errors),
        duration_minutes=_check_duration(raw.get("duration_minutes"), description, errors),
        frequency=_check_frequency(raw.get("frequency"), description, errors, assumptions),
        day=_check_date(raw.get("date"), description, today, errors, assumptions),
        at=_check_time(raw.get("time"), description, errors),
    )


def _check_pet(pet_name: str, owner: Owner, errors: list[str]) -> str | None:
    """G3: resolve a pet name to exactly one of the owner's pets."""
    wanted = pet_name.strip().lower()
    matches = [pet for pet in owner.pets if pet.name.strip().lower() == wanted]
    if len(matches) == 1:
        return matches[0].pet_id

    # Two pets sharing a name is as ambiguous as no match, so both end in the same question.
    shown_name = pet_name.strip()
    if matches:
        problem = f"More than one pet is named '{shown_name}'."
    else:
        problem = f"I couldn't find a pet named '{shown_name}'."
    names = ", ".join(pet.name for pet in owner.pets)
    question = f"Which pet? You have: {names}" if names else "Which pet? You have no pets yet."
    errors.append(f"{problem} {question}")
    return None


def _check_description(description: str, errors: list[str]) -> str:
    """G4: require a short, non-empty description."""
    if not description:
        errors.append("Each task needs a short description of what to do.")
    elif len(description) > MAX_DESCRIPTION_CHARS:
        errors.append(
            f"Please shorten '{description[:40]}…' to {MAX_DESCRIPTION_CHARS} characters or fewer."
        )
    return description


def _check_duration(value: object, description: str, errors: list[str]) -> int | None:
    """G5: require a whole number of minutes in range, since 0 always fits and never conflicts."""
    minutes = _whole_number(value)
    if minutes is None:
        errors.append(f"How many minutes does '{description}' take?")
        return None
    if not MIN_DURATION_MINUTES <= minutes <= MAX_DURATION_MINUTES:
        errors.append(
            f"How many minutes does '{description}' take? It must be between "
            f"{MIN_DURATION_MINUTES} and {MAX_DURATION_MINUTES}."
        )
        return None
    return minutes


def _check_frequency(
    value: object, description: str, errors: list[str], assumptions: list[str]
) -> str | None:
    """G6: normalize the frequency, defaulting to a one-off task when none was stated."""
    if value is None:
        assumptions.append(
            f"No frequency given for '{description}', so it's set to '{DEFAULT_FREQUENCY}' (one time)."
        )
        return DEFAULT_FREQUENCY

    normalized = value.strip().lower() if isinstance(value, str) else None
    if normalized in ALLOWED_FREQUENCIES:
        return normalized

    errors.append(
        f"How often should '{description}' happen? I can schedule daily, weekly or "
        f"as needed, not '{value}'."
    )
    return None


def _check_date(
    value: object, description: str, today: date, errors: list[str], assumptions: list[str]
) -> date | None:
    """G7: parse today, tomorrow or a strict YYYY-MM-DD within the next week."""
    if value is None:
        assumptions.append(
            f"No date given for '{description}', so it's set for today ({_format_day(today)})."
        )
        return today

    day = _parse_date(value, today)
    if day is None:
        errors.append(
            f"I couldn't read the date '{value}' for '{description}'. "
            f"Use today, tomorrow or YYYY-MM-DD."
        )
        return None
    if day < today:
        errors.append(f"'{description}' is dated {_format_day(day)}, which is in the past.")
        return None
    if day > today + timedelta(days=MAX_DAYS_AHEAD):
        errors.append(
            f"'{description}' is dated {_format_day(day)}, more than "
            f"{MAX_DAYS_AHEAD} days away. Please plan closer to the day."
        )
        return None
    return day


def _parse_date(value: object, today: date) -> date | None:
    """Turn a date string into a date, or None if it isn't one of the accepted forms."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.lower() == "today":
        return today
    if text.lower() == "tomorrow":
        return today + timedelta(days=1)
    if not _ISO_DATE_PATTERN.match(text):
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:  # Right shape but impossible, such as 2026-02-30.
        return None


def parse_time_of_day(value: object) -> time | None:
    """G8's rule on its own: a stripped 24-hour HH:MM string becomes a time, anything else None.

    Shared with the agent's revision check (R2), so both steps accept exactly the same times.
    """
    text = value.strip() if isinstance(value, str) else ""
    if not _TIME_PATTERN.match(text):
        return None
    return datetime.strptime(text, "%H:%M").time()


def _check_time(value: object, description: str, errors: list[str]) -> time | None:
    """G8: require an exact 24-hour HH:MM start time rather than guessing one."""
    at = parse_time_of_day(value)
    if at is None:
        errors.append(f"What time for '{description}'?")
    return at


def _check_quote(raw: dict, request: str, warnings: list[str]) -> None:
    """G9: warn when a task's quote isn't in the request, a sign the model invented it."""
    quote = _normalize_text(raw["quote"])
    if not quote or quote not in _normalize_text(request):
        warnings.append(
            f"⚠ Couldn't match '{raw['quote']}' to your request; please check "
            f"'{raw['description'].strip()}'."
        )


def _check_budget(args: dict, errors: list[str]) -> int | None:
    """G10: accept a stated time budget only as a whole number of minutes in a day."""
    value = args.get("available_minutes")
    if value is None:
        return None
    minutes = _whole_number(value)
    if minutes is None or not 0 <= minutes <= MAX_BUDGET_MINUTES:
        errors.append(
            f"How many minutes do you have today? It must be a whole number "
            f"from 0 to {MAX_BUDGET_MINUTES}."
        )
        return None
    return minutes


def _check_single_date(candidates: list[_Candidate], errors: list[str]) -> date | None:
    """G12: require one shared date, skipping the check if any date already failed G7."""
    if not candidates or any(candidate.day is None for candidate in candidates):
        return None
    days = sorted({candidate.day for candidate in candidates})
    if len(days) > 1:
        listed = ", ".join(_format_day(day) for day in days)
        errors.append(
            f"Your request covers more than one day ({listed}). Please send one day at a time."
        )
        return None
    return days[0]


def _build_tasks(
    candidates: list[_Candidate], owner: Owner, plan_date: date, warnings: list[str]
) -> list[tuple[str, str, Task]]:
    """G11: drop duplicates, then build Tasks whose ids are contiguous refs new-1, new-2, ..."""
    pet_names = {pet.pet_id: pet.name for pet in owner.pets}
    existing = {
        (pet.pet_id, task.description.strip().lower(), task.time)
        for pet in owner.pets
        for task in pet.get_tasks()
    }
    seen: set[tuple[str, str, datetime]] = set()
    new_tasks: list[tuple[str, str, Task]] = []

    for candidate in candidates:
        start = datetime.combine(plan_date, candidate.at)
        key = (candidate.pet_id, candidate.description.lower(), start)
        if key in existing or key in seen:
            where = "already on the schedule" if key in existing else "repeated in this request"
            warnings.append(
                f"Dropped '{candidate.description}' for {pet_names[candidate.pet_id]} at "
                f"{start.strftime('%H:%M')}: it's {where}."
            )
            continue
        seen.add(key)

        # Refs are numbered after duplicates are dropped, so they stay contiguous.
        ref = f"new-{len(new_tasks) + 1}"
        task = Task(
            task_id=ref,
            description=candidate.description,
            time=start,
            frequency=candidate.frequency,
            duration_minutes=candidate.duration_minutes,
        )
        new_tasks.append((candidate.pet_id, ref, task))

    return new_tasks


def _whole_number(value: object) -> int | None:
    """Return value as an int if it's a whole number, rejecting bools, strings and fractions."""
    # bool is a subclass of int, so True would otherwise pass as 1.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    # Some SDKs deliver JSON numbers as floats, so 30.0 means 30.
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _normalize_text(text: str) -> str:
    """Lowercase and collapse whitespace so quoting doesn't hinge on spacing or case."""
    return " ".join(text.lower().split())


def _format_day(day: date) -> str:
    """Format a date like 'Nov 2' without the platform-specific %-d directive."""
    return f"{day.strftime('%b')} {day.day}"
