"""Run every recorded fixture through the agent in mock mode and check it against its `expect`.

Usage: python eval_agent.py [--now 2026-11-02T07:00] [--log-path logs/eval.log]

Each fixture runs against a fresh build_fixture_owner() and a fresh MockClient, so the
result never depends on the environment or the network. Nothing is ever committed.
Exit code 0 means all six fixtures passed (plan §9 increment 11).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime

import agent
import llm_client
from fixture_owner import build_fixture_owner

EXPECTED_FIXTURES = 6
DEFAULT_NOW = "2026-11-02T07:00"
DEFAULT_LOG_PATH = "logs/eval.log"
REQUEST_WIDTH = 50


def _parse_now(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO datetime: {value!r}") from exc


def _abridge(text: str, width: int = REQUEST_WIDTH) -> str:
    return text if len(text) <= width else text[: width - 3] + "..."


def _final_plan(result: agent.AgentResult) -> dict | None:
    return result.revised_plan if result.revised_plan is not None else result.original_plan


def check_result(result: agent.AgentResult, expect: dict) -> list[str]:
    """Every failing check, as short "name: expected X, got Y" strings."""
    failures: list[str] = []

    def compare(name: str, expected, actual) -> None:
        if expected != actual:
            failures.append(f"{name}: expected {expected}, got {actual}")

    compare("status", expect.get("status"), result.status)
    compare("stop_reason", expect.get("stop_reason"), result.stop_reason)
    compare("revise_calls", expect.get("revise_calls"), result.revise_calls)

    # Plan checks only make sense when a plan was built.
    if result.status == agent.STATUS_OK:
        plan = _final_plan(result)
        if plan is None:
            failures.append("plan: status is ok but there is no plan")
        else:
            compare("conflicts", expect.get("conflicts"), len(plan["conflicts"]))
            compare(
                "deferred",
                sorted(expect.get("deferred", [])),
                sorted(task.description for task in plan["deferred"]),
            )

    if "questions" in expect:
        compare("questions", expect["questions"], len(result.errors))
    return failures


def run_fixture(fixture: dict, now: datetime) -> tuple[agent.AgentResult | None, list[str]]:
    """Run one fixture; an exception becomes a failure instead of crashing the eval."""
    try:
        owner = build_fixture_owner()
        # The path is passed explicitly (not MockClient's import-time default), so the
        # fixtures replayed are always the ones this eval loaded.
        client = llm_client.MockClient(llm_client.FIXTURES_PATH)
        result = agent.run_agent(fixture["request"], owner, now, client)
        return result, check_result(result, fixture["expect"])
    except Exception as exc:  # noqa: BLE001 - one broken fixture must not stop the others
        return None, [f"exception: {type(exc).__name__}: {exc}"]


def _print_table(rows: list[tuple[str, str, str, str, str, str]]) -> None:
    header = ("#", "Request", "Status", "Stop reason", "Calls", "Result")
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]

    def line(cells) -> str:
        return "  ".join(cell.ljust(width) for cell, width in zip(cells, widths)).rstrip()

    print(line(header))
    print(line("-" * width for width in widths))
    for row in rows:
        print(line(row))


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(description="Evaluate the PawPal+ agent on the mock fixtures.")
    parser.add_argument(
        "--now",
        type=_parse_now,
        default=_parse_now(DEFAULT_NOW),
        metavar="ISO_DATETIME",
        help=f"the agent's current time (default {DEFAULT_NOW}, the fixtures' baseline)",
    )
    parser.add_argument(
        "--log-path", default=DEFAULT_LOG_PATH, help=f"agent log file (default {DEFAULT_LOG_PATH})"
    )
    args = parser.parse_args(argv)

    agent.setup_logging(log_path=args.log_path)

    try:
        with open(llm_client.FIXTURES_PATH, encoding="utf-8") as handle:
            fixtures = list(json.load(handle).values())
    except (OSError, ValueError) as exc:
        print(f"FAIL: couldn't load fixtures: {type(exc).__name__}: {exc}")
        return 1
    if len(fixtures) != EXPECTED_FIXTURES:
        print(f"FAIL: expected {EXPECTED_FIXTURES} fixtures, found {len(fixtures)}")
        return 1

    rows = []
    failures_by_number: list[tuple[int, list[str]]] = []
    for number, fixture in enumerate(fixtures, start=1):
        result, failures = run_fixture(fixture, args.now)
        if result is None:
            status, stop_reason, calls = "error", "-", "-"
        else:
            status = result.status
            stop_reason = result.stop_reason or "-"
            calls = str(result.revise_calls)
        rows.append(
            (
                str(number),
                _abridge(str(fixture.get("request", ""))),
                status,
                stop_reason,
                calls,
                "FAIL" if failures else "PASS",
            )
        )
        if failures:
            failures_by_number.append((number, failures))

    _print_table(rows)
    if failures_by_number:
        print()
        for number, failures in failures_by_number:
            for failure in failures:
                print(f"  #{number}: {failure}")

    passed = len(fixtures) - len(failures_by_number)
    print()
    print(f"{passed}/{len(fixtures)} passed")
    return 0 if passed == len(fixtures) else 1


if __name__ == "__main__":
    raise SystemExit(main())
