"""The agent: parse a plain-English request with an LLM, validate it, and plan it on a draft copy.

The real Owner is never modified here. The revision loop and commit() come in later increments.
"""

from __future__ import annotations

import copy
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

import guardrails
from llm_client import LLMResponseError, PARSE_TOOL_NAMES, PARSE_TOOLS, UnknownMockRequest
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
    revised_plan: dict | None = None  # filled by the revision loop (later increment)
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
    """Parse, validate and plan one request on a draft copy of the owner; never touch the owner.

    max_revisions is accepted for the revision loop added in a later increment and unused here.
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
    new_ids = {ref for _pet_id, ref, _task in validation.new_tasks}  # noqa: F841 - used by inc 6

    # Plan at the end of the request's day.
    plan_time = datetime.combine(validation.plan_date, time(PLAN_HOUR, PLAN_MINUTE))
    plan = Scheduler().generate_plan(draft, plan_time)
    _log(run_id, "plan", _plan_summary(plan, plan_time, draft.available_time_minutes), trace)

    return finish(
        STATUS_OK,
        new_tasks=list(validation.new_tasks),
        plan_date=validation.plan_date,
        budget=validation.budget,
        warnings=list(validation.warnings),
        assumptions=list(validation.assumptions),
        original_plan=plan,
    )


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
