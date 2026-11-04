"""The agent: parse a plain-English request with an LLM, validate it, plan it on a draft copy,
and let the LLM propose bounded same-day revisions when the request's own tasks conflict.

The real Owner is never modified here. commit() comes in a later increment.
"""

from __future__ import annotations

import copy
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

import guardrails
from llm_client import (
    PARSE_TOOL_NAMES,
    PARSE_TOOLS,
    REVISE_TOOL_NAMES,
    REVISE_TOOLS,
    LLMResponseError,
    UnknownMockRequest,
)
from pawpal_system import Owner, Scheduler, Task

STATUS_OK = "ok"
STATUS_NEEDS_CLARIFICATION = "needs_clarification"
STATUS_LLM_BAD_OUTPUT = "llm_bad_output"
STATUS_LLM_ERROR = "llm_error"
STATUS_MOCK_UNKNOWN_REQUEST = "mock_unknown_request"

# Plans run at the end of the request's day, so tasks later today still count as due (plan §2).
PLAN_HOUR = 23
PLAN_MINUTE = 59

MAX_ERROR_CHARS = 300

# Revision loop (plan §4 and §5).
MAX_MOVES = 3
STOP_CLEAN = "clean"
STOP_NO_MOVABLE_CONFLICTS = "no_movable_conflicts"
STOP_MODEL_KEPT_PLAN = "model_kept_plan"
STOP_INVALID_REVISION = "invalid_revision"
STOP_NO_IMPROVEMENT = "no_improvement"
STOP_MAX_ITERATIONS = "max_iterations"
STOP_LLM_ERROR = "llm_error"

REVISE_SYSTEM_PROMPT = (
    "Some of the user's new tasks overlap other tasks. Propose new same-day start times "
    "(HH:MM) for the referenced new tasks only, so that the listed overlaps are removed "
    "without creating new ones. Tasks without a ref are fixed. If you can't do that, call "
    "keep_plan with the reason."
)

PARSE_SYSTEM_PROMPT_TEMPLATE = (
    "You turn a pet owner's request into pet care tasks by calling exactly one tool.\n"
    "Today is {today}.\n"
    "The owner's pets are: {pets}.\n"
    "The owner's current daily time budget is {budget} minutes.\n"
    "Rules:\n"
    "- Omit any field the user did not state.\n"
    "- Never invent a time, duration or pet.\n"
    "- Copy the frequency as the user said it; don't convert it.\n"
    "- For each task, quote the exact words from the request it came from.\n"
    "- If the request isn't about pet care or can't be parsed at all, call ask_clarification."
)

LOGGER_NAME = "pawpal.agent"
logger = logging.getLogger(LOGGER_NAME)
# Without setup_logging(), records are dropped quietly instead of hitting Python's last resort.
logger.addHandler(logging.NullHandler())
logger.propagate = False

# Marks handlers added by setup_logging(), so a second call replaces them instead of stacking.
_HANDLER_TAG = "_pawpal_agent_handler"


@dataclass
class AgentResult:
    """Everything one agent run produced, for the UI, the CLI and the eval."""

    run_id: str
    status: str
    request: str
    new_tasks: list[tuple[str, str, Task]] = field(default_factory=list)  # only when status "ok"
    plan_date: date | None = None
    budget: int | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    original_plan: dict | None = None
    revised_plan: dict | None = None  # the last accepted revision's plan, if any was accepted
    # New tasks at their revised times, in ref order, as fresh copies; empty without a revision.
    revised_new_tasks: list[tuple[str, str, Task]] = field(default_factory=list)
    revision_moves: list = field(default_factory=list)
    stop_reason: str | None = None
    revise_calls: int = 0
    supported_requests: list[str] = field(default_factory=list)  # only for mock_unknown_request
    trace: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(console: bool = False, log_path: str = "logs/agent.log") -> logging.Logger:
    """Send agent log lines to a UTF-8 file (and optionally the console); safe to call twice."""
    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_TAG, False):
            logger.removeHandler(handler)
            handler.close()

    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(message)s")

    handlers: list[logging.Handler] = [logging.FileHandler(path, encoding="utf-8")]
    if console:
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.setFormatter(formatter)
        setattr(handler, _HANDLER_TAG, True)
        logger.addHandler(handler)

    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def _log(run_id: str, step: str, payload: dict, trace: list[dict]) -> None:
    """Write one log line and append the same entry to the trace, so the two never diverge."""
    line = json.dumps(payload, sort_keys=True, default=str)
    logger.info("run=%s step=%s %s", run_id, step, line)
    # Round-tripped through the same JSON, so the trace holds exactly what was logged.
    trace.append({"step": step, **json.loads(line)})


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


def build_parse_prompt(owner: Owner, now: datetime) -> str:
    """Fill the parse system prompt with today's date, the pet names and the current budget."""
    pets = ", ".join(pet.name for pet in owner.pets) or "(no pets yet)"
    return PARSE_SYSTEM_PROMPT_TEMPLATE.format(
        today=now.date().isoformat(), pets=pets, budget=owner.available_time_minutes
    )


def run_agent(
    request: str, owner: Owner, now: datetime, client, max_revisions: int = 2
) -> AgentResult:
    """Parse, validate, plan and revise one request on draft copies; never touch the owner.

    max_revisions caps the revise calls (plan §5); 0 means the LLM is never asked to revise.
    """
    run_id = uuid.uuid4().hex[:8]
    trace: list[dict] = []

    def finish(status: str, **fields) -> AgentResult:
        result = AgentResult(run_id=run_id, status=status, request=request, trace=trace, **fields)
        _log(
            run_id,
            "stop",
            {
                "status": status,
                "stop_reason": result.stop_reason,
                "revise_calls": result.revise_calls,
                "errors": result.errors,
            },
            trace,
        )
        return result

    _log(run_id, "request", {"request": request, "now": now.isoformat()}, trace)

    # Parse. user_text is the raw request and nothing else (the MockClient contract).
    system_prompt = build_parse_prompt(owner, now)
    try:
        call = client.call("parse", system_prompt, request, PARSE_TOOLS)
    except UnknownMockRequest as exc:
        return finish(
            STATUS_MOCK_UNKNOWN_REQUEST,
            errors=["Mock mode has no recorded response for this request."],
            supported_requests=list(exc.supported),
        )
    except LLMResponseError as exc:
        return finish(STATUS_LLM_BAD_OUTPUT, errors=[_error_message(exc)])
    except Exception as exc:  # noqa: BLE001 - network, quota, auth and config errors all stop here
        return finish(STATUS_LLM_ERROR, errors=[_error_message(exc)])

    name = getattr(call, "name", None)
    args = getattr(call, "args", None)
    _log(run_id, "parse_output", {"tool": name, "args": args}, trace)

    if name not in PARSE_TOOL_NAMES:
        return finish(STATUS_LLM_BAD_OUTPUT, errors=[f"The model called an unknown tool {name!r}."])

    if name == "ask_clarification":
        question = args.get("question") if isinstance(args, dict) else None
        if isinstance(question, str) and question.strip():
            return finish(STATUS_NEEDS_CLARIFICATION, errors=[question.strip()])
        return finish(
            STATUS_LLM_BAD_OUTPUT, errors=["The model asked for clarification without a question."]
        )

    # Validate (submit_tasks).
    validation = guardrails.validate_request(args, request, owner, now)
    _log(
        run_id,
        "validation",
        {
            "status": validation.status,
            "errors": validation.errors,
            "warnings": validation.warnings,
            "assumptions": validation.assumptions,
            "new_tasks": [
                {"pet_id": pet_id, "ref": ref, **_task_summary(task)}
                for pet_id, ref, task in validation.new_tasks
            ],
        },
        trace,
    )
    if validation.status != STATUS_OK:
        return finish(
            validation.status,
            errors=list(validation.errors),
            warnings=list(validation.warnings),
            assumptions=list(validation.assumptions),
        )

    # Draft: a deep copy, so the real owner is never touched. Each draft task is its own copy,
    # so the validation's tasks (returned to the caller) stay unshared with the plan.
    draft = copy.deepcopy(owner)
    draft_pets = {pet.pet_id: pet for pet in draft.pets}
    for pet_id, _ref, task in validation.new_tasks:
        draft_pets[pet_id].add_task(copy.deepcopy(task))
    if validation.budget is not None:
        draft.available_time_minutes = validation.budget
    # New tasks are identified by task_id only, never by object identity (plan §2 step 4).
    new_ids = {ref for _pet_id, ref, _task in validation.new_tasks}

    # Plan at the end of the request's day.
    plan_time = datetime.combine(validation.plan_date, time(PLAN_HOUR, PLAN_MINUTE))
    plan = Scheduler().generate_plan(draft, plan_time)
    _log(run_id, "plan", _plan_summary(plan, plan_time, draft.available_time_minutes), trace)

    # Revise. `draft` and `plan` are never mutated, so original_plan stays the pre-revision plan.
    state = _RevisionState(plan=plan, draft=draft)
    stop_reason, errors = _revision_loop(
        state,
        client,
        plan_time,
        validation.plan_date,
        new_ids,
        max_revisions,
        lambda step, payload: _log(run_id, step, payload, trace),
    )

    revised_new_tasks: list[tuple[str, str, Task]] = []
    if state.accepted:
        revised_by_id = {task.task_id: task for task in state.draft.get_all_tasks()}
        revised_new_tasks = [
            (pet_id, ref, copy.deepcopy(revised_by_id[ref]))
            for pet_id, ref, _task in validation.new_tasks
        ]

    return finish(
        STATUS_LLM_ERROR if stop_reason == STOP_LLM_ERROR else STATUS_OK,
        new_tasks=list(validation.new_tasks),
        plan_date=validation.plan_date,
        budget=validation.budget,
        errors=errors,
        warnings=list(validation.warnings),
        assumptions=list(validation.assumptions),
        original_plan=plan,
        revised_plan=state.plan if state.accepted else None,
        revised_new_tasks=revised_new_tasks,
        revision_moves=state.moves,
        stop_reason=stop_reason,
        revise_calls=state.revise_calls,
    )


# ---------------------------------------------------------------------------
# Revision loop (plan §4 R1-R4 and §5)
# ---------------------------------------------------------------------------


def movable(plan: dict, new_ids: set[str]) -> list[tuple[Task, Task]]:
    """The conflict pairs that involve at least one new task, matched by task_id only."""
    return [
        (first, second)
        for first, second in plan["conflicts"]
        if first.task_id in new_ids or second.task_id in new_ids
    ]


def gate_stop(plan: dict) -> str:
    """The stop reason for a plan with nothing the LLM may fix: clean, or problems left to report."""
    if not plan["conflicts"] and not plan["deferred"]:
        return STOP_CLEAN
    return STOP_NO_MOVABLE_CONFLICTS


def better(new_plan: dict, old_plan: dict) -> bool:
    """R4: strictly fewer conflicts and no more deferred tasks; fewer deferred alone never counts."""
    return len(new_plan["conflicts"]) < len(old_plan["conflicts"]) and len(
        new_plan["deferred"]
    ) <= len(old_plan["deferred"])


def validate_moves(
    args: object, draft: Owner, new_ids: set[str]
) -> tuple[list[tuple[str, datetime]] | None, str | None]:
    """Check a move_tasks call against R1-R3; return (moves, None) or (None, error)."""
    if not isinstance(args, dict):
        return None, "shape: the arguments aren't an object"
    reason = args.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return None, "shape: missing reason"
    raw_moves = args.get("moves")
    if not isinstance(raw_moves, list):
        return None, "shape: moves isn't a list"

    # R1: 1 to MAX_MOVES moves, each naming a distinct new-task ref.
    if not 1 <= len(raw_moves) <= MAX_MOVES:
        return None, f"R1: expected 1 to {MAX_MOVES} moves, got {len(raw_moves)}"
    seen: set[str] = set()
    for raw in raw_moves:
        if not isinstance(raw, dict):
            return None, "R1: a move isn't an object"
        ref, at = raw.get("ref"), raw.get("time")
        if not isinstance(ref, str) or not isinstance(at, str):
            return None, "R1: a move needs a string ref and time"
        if ref not in new_ids:
            return None, f"R1: {ref!r} is not a new task from this request"
        if ref in seen:
            return None, f"R1: {ref!r} is moved more than once"
        seen.add(ref)

    tasks_by_id = {task.task_id: task for task in draft.get_all_tasks()}
    moves: list[tuple[str, datetime]] = []
    for raw in raw_moves:
        ref = raw["ref"]
        task = tasks_by_id.get(ref)
        if task is None:
            return None, f"R1: {ref!r} isn't in the draft"

        # R2: a valid HH:MM on the task's current date, ending before midnight.
        at = guardrails.parse_time_of_day(raw["time"])
        if at is None:
            return None, f"R2: {raw['time']!r} isn't an HH:MM time"
        start = datetime.combine(task.time.date(), at)
        end = start + timedelta(minutes=task.duration_minutes)
        if end.date() != start.date():
            return None, f"R2: {ref} would run past midnight"

        # R3: the move must actually change the time.
        if start == task.time:
            return None, f"R3: {ref} is already at {_hhmm(start)}"
        moves.append((ref, start))
    return moves, None


def build_revise_text(draft: Owner, plan: dict, new_ids: set[str], plan_date: date) -> str:
    """The revise call's user_text: the day's tasks and only the movable overlaps.

    No budget and no deferred information, so the model has nothing to optimize but overlaps.
    `plan`'s tasks are `draft`'s own objects, so the plan alone describes the draft.
    """
    pairs = movable(plan, new_ids)
    listed: dict[str, Task] = {}
    for task in plan["scheduled"]:
        listed.setdefault(task.task_id, task)
    for first, second in pairs:
        listed.setdefault(first.task_id, first)
        listed.setdefault(second.task_id, second)

    lines = [f"Date: {plan_date.isoformat()}", "Tasks:"]
    for task in sorted(listed.values(), key=lambda task: (task.time, task.task_id)):
        span = f"{_span(task)} ({task.duration_minutes} min)"
        if task.task_id in new_ids:
            lines.append(f"- {task.task_id} | {task.description} | {span} | movable")
        else:
            lines.append(f"- {task.description} | {span} | fixed")

    lines.append("Overlaps to remove:")
    for first, second in pairs:
        lines.append(f"- {_overlap_label(first, new_ids)} overlaps {_overlap_label(second, new_ids)}")
    return "\n".join(lines)


@dataclass
class _RevisionState:
    """The last good state of the loop: the accepted plan and the draft that produced it."""

    plan: dict
    draft: Owner
    accepted: bool = False
    revise_calls: int = 0
    moves: list[dict] = field(default_factory=list)


def _revision_loop(
    state: _RevisionState,
    client,
    plan_time: datetime,
    plan_date: date,
    new_ids: set[str],
    max_revisions: int,
    log,
) -> tuple[str, list[str]]:
    """Run plan §5's loop, updating state only on an accepted revision; return (stop_reason, errors)."""
    for index in range(max_revisions):
        iteration = index + 1
        if not movable(state.plan, new_ids):
            return gate_stop(state.plan), []

        user_text = build_revise_text(state.draft, state.plan, new_ids, plan_date)
        state.revise_calls += 1
        try:
            call = client.call("revise", REVISE_SYSTEM_PROMPT, user_text, REVISE_TOOLS)
        except Exception as exc:  # noqa: BLE001 - any client failure keeps the last good state
            return STOP_LLM_ERROR, [_error_message(exc)]

        name = getattr(call, "name", None)
        args = getattr(call, "args", None)
        log("revision_proposal", {"iteration": iteration, "tool": name, "args": args})
        if name not in REVISE_TOOL_NAMES:
            return STOP_INVALID_REVISION, []
        if name == "keep_plan":
            return STOP_MODEL_KEPT_PLAN, []

        moves, error = validate_moves(args, state.draft, new_ids)
        log("revision_validation", {"iteration": iteration, "valid": error is None, "error": error})
        if error is not None:
            return STOP_INVALID_REVISION, []

        # Applied to a fresh copy, so a discarded candidate never touches the last good draft.
        candidate = copy.deepcopy(state.draft)
        candidate_by_id = {task.task_id: task for task in candidate.get_all_tasks()}
        for ref, new_time in moves:
            candidate_by_id[ref].time = new_time
        new_plan = Scheduler().generate_plan(candidate, plan_time)
        accepted = better(new_plan, state.plan)
        log(
            "revised_plan",
            {
                "iteration": iteration,
                "accepted": accepted,
                "conflicts_before": len(state.plan["conflicts"]),
                "conflicts_after": len(new_plan["conflicts"]),
                "deferred_before": len(state.plan["deferred"]),
                "deferred_after": len(new_plan["deferred"]),
                **_plan_summary(new_plan, plan_time, candidate.available_time_minutes),
            },
        )
        if not accepted:
            return STOP_NO_IMPROVEMENT, []

        current_by_id = {task.task_id: task for task in state.draft.get_all_tasks()}
        for ref, new_time in moves:
            state.moves.append(
                {
                    "iteration": iteration,
                    "ref": ref,
                    "from": _hhmm(current_by_id[ref].time),
                    "to": _hhmm(new_time),
                    "reason": args["reason"].strip(),
                }
            )
        state.plan = new_plan
        state.draft = candidate
        state.accepted = True

    # Out of iterations: re-apply the gate, so a final fix still ends clean (plan §5).
    if not movable(state.plan, new_ids):
        return gate_stop(state.plan), []
    return STOP_MAX_ITERATIONS, []


def _hhmm(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def _span(task: Task) -> str:
    """A task's start and end as HH:MM-HH:MM."""
    end = task.time + timedelta(minutes=task.duration_minutes)
    return f"{_hhmm(task.time)}-{_hhmm(end)}"


def _overlap_label(task: Task, new_ids: set[str]) -> str:
    """A new task by its ref, a fixed task by its description, each with its time span."""
    if task.task_id in new_ids:
        return f"{task.task_id} ({task.description}, {_span(task)})"
    return f"{task.description} ({_span(task)})"


def _error_message(exc: Exception) -> str:
    """A short, user-safe description of a client exception."""
    message = f"{type(exc).__name__}: {exc}"
    return message[:MAX_ERROR_CHARS]


def _task_summary(task: Task) -> dict:
    """A JSON-safe view of one task for the log and trace."""
    return {
        "task_id": task.task_id,
        "description": task.description,
        "time": task.time.isoformat(),
        "duration_minutes": task.duration_minutes,
        "frequency": task.frequency,
    }


def _plan_summary(plan: dict, plan_time: datetime, budget: int) -> dict:
    """A JSON-safe summary of a Scheduler plan: ids, conflict pairs and minutes used."""
    return {
        "plan_time": plan_time.isoformat(),
        "budget": budget,
        "budget_used": sum(task.duration_minutes for task in plan["scheduled"]),
        "scheduled": [
            {"task_id": task.task_id, "description": task.description}
            for task in plan["scheduled"]
        ],
        "deferred": [
            {"task_id": task.task_id, "description": task.description}
            for task in plan["deferred"]
        ],
        "conflicts": [[first.task_id, second.task_id] for first, second in plan["conflicts"]],
    }
