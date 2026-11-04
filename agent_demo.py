"""CLI demo for the PawPal+ agent (plan §9 increment 8).

    python agent_demo.py --mock --now 2026-11-02T07:00 --yes     # all six demo requests
    python agent_demo.py --mock "Rex needs a 20 minute walk at 10:00 today."

Every run plans against a fresh build_fixture_owner(), so runs never affect each other.
The output never includes run ids, API keys or committed task ids, so a mock run with a
fixed --now and --yes prints the same text every time.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta

import agent
import llm_client
from fixture_owner import build_fixture_owner
from llm_client import MockClient

DEFAULT_LOG_PATH = "logs/agent.log"
MOCK_BANNER = "[mock mode: recorded responses, no API calls]"

STOP_MEANINGS = {
    agent.STOP_CLEAN: "no conflicts and nothing deferred",
    agent.STOP_NO_MOVABLE_CONFLICTS: (
        "nothing the LLM may fix; deferred tasks and conflicts among existing tasks are left to you"
    ),
    agent.STOP_MODEL_KEPT_PLAN: "the LLM chose to keep the plan as it was",
    agent.STOP_INVALID_REVISION: "the LLM's proposed moves broke a rule, so the last good plan was kept",
    agent.STOP_NO_IMPROVEMENT: "the LLM's proposed moves didn't reduce conflicts, so they were discarded",
    agent.STOP_MAX_ITERATIONS: "the revision limit was reached with movable conflicts still left",
    agent.STOP_LLM_ERROR: "the LLM call failed, so the last good plan was kept",
}

DECISION_LABELS = {
    agent.DECISION_APPROVE_REVISED: "revised",
    agent.DECISION_APPROVE_ORIGINAL: "original",
    agent.DECISION_DISCARD: "discarded",
}

PLAN_COLUMNS = ["Task", "Pet", "Start", "End", "Min", "Result"]
SUMMARY_COLUMNS = ["#", "Status", "Stop reason", "Revise calls", "Decision"]


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def _iso_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO datetime: {value!r}") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the PawPal+ agent on a plain-English request.")
    parser.add_argument(
        "request", nargs="?", help="the request to plan; omit it to run all six demo requests"
    )
    parser.add_argument(
        "--mock", action="store_true", help="use recorded responses (no .env, no API key)"
    )
    parser.add_argument(
        "--now",
        type=_iso_datetime,
        default=None,
        metavar="ISO_DATETIME",
        help="the current time, e.g. 2026-11-02T07:00 (default: the real current time)",
    )
    parser.add_argument("--yes", action="store_true", help="approve without asking")
    parser.add_argument(
        "--log-path", default=DEFAULT_LOG_PATH, help=f"agent log file (default: {DEFAULT_LOG_PATH})"
    )
    return parser


def _demo_requests() -> list[str]:
    with open(llm_client.FIXTURES_PATH, encoding="utf-8") as handle:
        return [fixture["request"] for fixture in json.load(handle).values()]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _table(columns: list[str], rows: list[list[str]], right: set[str] = frozenset()) -> list[str]:
    """A fixed-width text table; columns named in `right` are right-aligned."""
    widths = [max([len(col)] + [len(row[i]) for row in rows]) for i, col in enumerate(columns)]

    def line(cells: list[str]) -> str:
        parts = [
            cell.rjust(width) if col in right else cell.ljust(width)
            for col, cell, width in zip(columns, cells, widths)
        ]
        return "  ".join(parts).rstrip()

    rule = "  ".join("-" * width for width in widths)
    return [line(columns), rule] + [line(row) for row in rows]


def _hhmm(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def _task_label(task, new_ids: set[str]) -> str:
    """New tasks carry their stable ref (new-1); anything else is shown by description only."""
    if task.task_id in new_ids:
        return f"{task.task_id} {task.description}"
    return task.description


def _plan_lines(plan: dict, pet_by_task_id: dict[str, str], new_ids: set[str], budget: int) -> list[str]:
    entries = [(task, "scheduled") for task in plan["scheduled"]] + [
        (task, "deferred") for task in plan["deferred"]
    ]
    entries.sort(key=lambda entry: (entry[0].time, entry[0].task_id))
    rows = [
        [
            _task_label(task, new_ids),
            pet_by_task_id.get(task.task_id, "?"),
            _hhmm(task.time),
            _hhmm(task.time + timedelta(minutes=task.duration_minutes)),
            str(task.duration_minutes),
            outcome,
        ]
        for task, outcome in entries
    ]
    lines = ["  " + row for row in _table(PLAN_COLUMNS, rows, right={"Min"})]
    for first, second in plan["conflicts"]:
        lines.append(f"  Conflict: {first.description} overlaps {second.description}")
    used = sum(task.duration_minutes for task in plan["scheduled"])
    lines.append(f"  Budget: {used} of {budget} min used")
    return lines


def _report(result: agent.AgentResult, owner) -> list[str]:
    lines: list[str] = []
    lines.append(f"Status: {result.status}")

    if result.status == agent.STATUS_NEEDS_CLARIFICATION:
        lines += [f"  Question: {error}" for error in result.errors]
    elif result.status in (agent.STATUS_LLM_BAD_OUTPUT, agent.STATUS_LLM_ERROR):
        lines += [f"  Problem: {error}" for error in result.errors]
    elif result.status == agent.STATUS_MOCK_UNKNOWN_REQUEST:
        lines.append("  Mock mode only knows these requests:")
        lines += [f"    - {request}" for request in result.supported_requests]

    if result.assumptions:
        lines.append("Assumptions:")
        lines += [f"  - {item}" for item in result.assumptions]
    if result.warnings:
        lines.append("Warnings:")
        lines += [f"  {item}" for item in result.warnings]

    pet_name_by_id = {pet.pet_id: pet.name for pet in owner.pets}
    pet_by_task_id = {
        task.task_id: pet.name for pet in owner.pets for task in pet.get_tasks()
    }
    for pet_id, ref, _task in result.new_tasks:
        pet_by_task_id[ref] = pet_name_by_id.get(pet_id, "?")
    new_ids = {ref for _pet_id, ref, _task in result.new_tasks}
    budget = result.budget if result.budget is not None else owner.available_time_minutes

    if result.original_plan is not None:
        lines.append("Original plan:")
        lines += _plan_lines(result.original_plan, pet_by_task_id, new_ids, budget)

    if result.revised_plan is not None:
        descriptions = {ref: task.description for _pet_id, ref, task in result.new_tasks}
        lines.append("Revision moves:")
        for move in result.revision_moves:
            lines.append(
                f"  {move['ref']} {descriptions.get(move['ref'], '?')}: "
                f"{move['from']} -> {move['to']} ({move['reason']})"
            )
        lines.append("Revised plan:")
        lines += _plan_lines(result.revised_plan, pet_by_task_id, new_ids, budget)

    if result.stop_reason is not None:
        meaning = STOP_MEANINGS.get(result.stop_reason, "")
        lines.append(f"Stop reason: {result.stop_reason}" + (f" ({meaning})" if meaning else ""))
    lines.append(f"LLM revise calls: {result.revise_calls}")
    return lines


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


def _committable(result: agent.AgentResult) -> bool:
    """The same rule agent.commit() enforces."""
    return result.status == agent.STATUS_OK or (
        result.status == agent.STATUS_LLM_ERROR and bool(result.new_tasks)
    )


def _can_use_revision(result: agent.AgentResult) -> bool:
    return result.status == agent.STATUS_OK and bool(result.revised_new_tasks)


def _ask_choice(result: agent.AgentResult) -> str:
    """Prompt until the user picks a valid option; EOF or Ctrl-C means discard."""
    options: list[tuple[str, str]] = []
    if _can_use_revision(result):
        options.append(("r", "approve the revised plan"))
    options.append(("o", "approve the original plan"))
    options.append(("d", "discard"))
    keys = [key for key, _label in options]

    print("Options: " + ", ".join(f"{key} = {label}" for key, label in options))
    prompt = f"Your choice [{'/'.join(keys)}]: "
    while True:
        try:
            answer = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            return "d"
        answer = answer.strip().lower()
        if answer in keys:
            return answer
        print(f"Please type {', '.join(keys[:-1])} or {keys[-1]}.")


def _approve(result: agent.AgentResult, owner, yes: bool) -> list[str]:
    if yes:
        choice = "r" if _can_use_revision(result) else "o"
    else:
        choice = _ask_choice(result)

    if choice == "d":
        agent.discard(result)
        return ["Discarded: nothing was changed."]

    budget_before = owner.available_time_minutes
    ref_to_task_id = agent.commit(result, owner, use_revision=(choice == "r"))
    label = "revised" if choice == "r" else "original"
    lines = [f"Approved the {label} plan.", "Committed to the owner:"]
    committed = {task.task_id: (pet.name, task) for pet in owner.pets for task in pet.get_tasks()}
    for _pet_id, ref, _task in result.new_tasks:
        pet_name, task = committed[ref_to_task_id[ref]]
        lines.append(
            f"  {pet_name}: {task.description} at {_hhmm(task.time)} ({task.duration_minutes} min)"
        )
    if owner.available_time_minutes != budget_before:
        lines.append(f"  Budget: {budget_before} -> {owner.available_time_minutes} min")
    return lines


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point; returns the process exit code."""
    args = _build_parser().parse_args(argv)
    now = args.now if args.now is not None else datetime.now()

    agent.setup_logging(log_path=args.log_path)
    try:
        client = MockClient() if args.mock else llm_client.get_client()
    except llm_client.LLMConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if getattr(client, "is_mock", False):
        print(MOCK_BANNER)

    requests = [args.request] if args.request is not None else _demo_requests()
    summary: list[list[str]] = []

    for number, request in enumerate(requests, start=1):
        owner = build_fixture_owner()
        result = agent.run_agent(request, owner, now, client)

        heading = f"Request {number} of {len(requests)}" if len(requests) > 1 else "Request"
        print()
        print(f"=== {heading} ===")
        print(request)
        print()
        for line in _report(result, owner):
            print(line)

        if _committable(result):
            print()
            for line in _approve(result, owner, args.yes):
                print(line)

        summary.append(
            [
                str(number),
                result.status,
                result.stop_reason or "-",
                str(result.revise_calls),
                DECISION_LABELS.get(result.decision, "-"),
            ]
        )

    if len(requests) > 1:
        print()
        print("=== Summary ===")
        for line in _table(SUMMARY_COLUMNS, summary, right={"#", "Revise calls"}):
            print(line)
        print(f"{len(requests)} requests run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
