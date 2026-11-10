# AI Interactions Log

## Agent Workflow

### What task did I give the agent?

I used Claude in VS Code as a coding agent to build the whole agentic request planner on top of PawPal+. I didn't ask it to "build the project" in one go. I planned the system first (a written plan, `agent_plan.md`), then gave the agent one numbered increment at a time (0 to 13). Each prompt named the exact files it could change, what behavior to implement, which tests to write, and what *not* to touch. Each increment had to pass the full test suite before I committed.

Example prompt (increment 7, shortened):

> Implement increment 7 from the plan, and nothing else. Files you may change: agent.py and a new tests/test_commit.py. Don't commit, no network. Add `commit(result, owner, use_revision)`. Refuse with CommitError, and change NOTHING, when the result was already decided, has a status that can't be committed, or names a pet the owner no longer has. Check everything first, then mutate. Committed tasks must be new Task objects with fresh `task-<8 hex>` ids. Run the full suite and show me the results.

### What did the agent do?

| Increment | What it built | Suite after |
|---|---|---|
| 0 | Fixed `Task.is_due` so a finished "as needed" task isn't due forever | |
| 1 | `.env.example`, `.gitignore`, `fixture_owner.py` | |
| 2 | `guardrails.py`, rules G1-G12 | 61 |
| 3 | Tool schemas, `MockClient`, six recorded fixtures | 101 |
| 4 | `GeminiClient` and the live `--smoke` test | 125 |
| 5 | `run_agent` (parse, validate, plan) and logging | 159 |
| 6 | Revision loop, rules R1-R4, movable-conflict gate | 197 |
| 7 | `commit()` and `discard()` | 225 |
| 8 | `agent_demo.py` CLI | 245 |
| 10 | Streamlit "Ask PawPal (AI)" section | 256 |
| 11 | `eval_agent.py` | 271 |
| 12 | `docs/sample_trace.log` | 277 |
| 13 | `diagrams/architecture.mmd` | 277 |

The agent in the product itself is also a multi-step workflow: **parse** (the LLM must call a tool), **validate** (guardrails), **plan** (the existing Scheduler on a draft copy), **revise** (the LLM proposes same-day time moves, checked by rules R1-R4, at most twice), then **human approval**. Every step is logged.

### Intermediate reasoning trace (embedded)

This is the real log from running `python agent_demo.py --mock --now 2026-11-02T07:00 --yes` on the request *"Today Biscuit needs a 30 minute walk at 8:00 and 5 minutes of meds at 8:15."* The full file, including a second run, is [`docs/sample_trace.log`](docs/sample_trace.log). The run id and the `task-...` ids are random, and everything else is reproducible.

```text
run=c88def0b step=request {"now": "2026-11-02T07:00:00", "request": "Today Biscuit needs a 30 minute walk at 8:00 and 5 minutes of meds at 8:15."}
run=c88def0b step=parse_output {"args": {"tasks": [{"date": "today", "description": "walk", "duration_minutes": 30, "pet_name": "Biscuit", "quote": "a 30 minute walk at 8:00", "time": "08:00"}, {"date": "today", "description": "meds", "duration_minutes": 5, "pet_name": "Biscuit", "quote": "5 minutes of meds at 8:15", "time": "08:15"}]}, "tool": "submit_tasks"}
run=c88def0b step=validation {"assumptions": ["No frequency given for 'walk', so it's set to 'as needed' (one time).", "No frequency given for 'meds', so it's set to 'as needed' (one time)."], "errors": [], "new_tasks": [{"description": "walk", "duration_minutes": 30, "frequency": "as needed", "pet_id": "p-biscuit", "ref": "new-1", "task_id": "new-1", "time": "2026-11-02T08:00:00"}, {"description": "meds", "duration_minutes": 5, "frequency": "as needed", "pet_id": "p-biscuit", "ref": "new-2", "task_id": "new-2", "time": "2026-11-02T08:15:00"}], "status": "ok", "warnings": []}
run=c88def0b step=plan {"budget": 120, "budget_used": 35, "conflicts": [["new-1", "new-2"]], "deferred": [], "plan_time": "2026-11-02T23:59:00", "scheduled": [{"description": "walk", "task_id": "new-1"}, {"description": "meds", "task_id": "new-2"}]}
run=c88def0b step=revision_proposal {"args": {"moves": [{"ref": "new-2", "time": "08:30"}], "reason": "Moving meds to 08:30 starts it right after the walk ends, removing the overlap."}, "iteration": 1, "tool": "move_tasks"}
run=c88def0b step=revision_validation {"error": null, "iteration": 1, "valid": true}
run=c88def0b step=revised_plan {"accepted": true, "budget": 120, "budget_used": 35, "conflicts": [], "conflicts_after": 0, "conflicts_before": 1, "deferred": [], "deferred_after": 0, "deferred_before": 0, "iteration": 1, "plan_time": "2026-11-02T23:59:00", "scheduled": [{"description": "walk", "task_id": "new-1"}, {"description": "meds", "task_id": "new-2"}]}
run=c88def0b step=stop {"errors": [], "revise_calls": 1, "status": "ok", "stop_reason": "clean"}
run=c88def0b step=approval {"budget": null, "decision": "approve_revised", "ref_to_task_id": {"new-1": "task-9b3071d8", "new-2": "task-a8497b48"}, "status": "ok", "stop_reason": "clean"}
```

How to read it:
1. **parse_output:** the model's tool call. It had to call `submit_tasks`.
2. **validation:** the guardrails accepted it and recorded two assumptions (no frequency given).
3. **plan:** the Scheduler found one conflict (walk 08:00-08:30 overlaps meds at 08:15).
4. **revision_proposal and revision_validation:** the model proposed moving meds to 08:30, and the rules accepted it.
5. **revised_plan:** conflicts went from 1 to 0, so the move was kept.
6. **stop and approval:** it ended `clean`, and the committed plan was the revised one.

The second run in the file (a 30-minute budget with 45 minutes of tasks) shows the opposite case: no revision steps at all, because the gate never sends a deferred-only plan to the model.

### What did I have to verify or fix manually?

- **The live model check failed at first.** `gemini-3.8-flash` returned `503 UNAVAILABLE` twice. Passing tests didn't tell me that. Switching to `gemini-3.6-flash` gave `SMOKE OK`.
- **The first live run was skipped, not passed.** My `.env` had `PAWPAL_LLM_MODE=mock` and a blank API key, so the smoke test only printed SKIP. I fixed the file myself and ran it again.
- **The architecture diagram.** The first version rendered but was far too big to read, and the second still was. I asked for simpler ones until the third was readable.
- **A mock bug caught in review:** after a failed parse, `MockClient` kept the previous fixture active, so a later revise call could return a stale answer. The fix and a test went in as a follow-up.
- **A contradiction in my own prompt:** I wrote that a move ending exactly at midnight is invalid, and also that a 30-minute task moved to 23:30 is valid. Those conflict, and the code followed the rule, so 23:29 is the latest valid start.
- **Claims the agent hadn't actually seen.** Twice I asked "are you sure?", and the answer separated what had been read from what had only been reported (the pytest count and the demo output). I then ran those myself.
- **A reordering.** The AI moved on to the model card before the README, and I pointed out we hadn't done Step 3 yet.