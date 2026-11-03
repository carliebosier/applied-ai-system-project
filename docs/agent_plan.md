# PawPal+ Agentic Request Planner — Plan (rev 3)

> Rev 3 of the design, kept in sync with the code through increment 2b. The original rev 3 text is in commit `0c6a295`; differences since then are small and noted inline.

## Context
PawPal+ (`pawpal_system.py`: Task, Pet, Owner, Scheduler; a Streamlit UI in `app.py`; a CLI demo in `main.py`) currently takes tasks only through form fields. The class project adds a plain-English entry point. Gemini parses the request into structured tasks. Deterministic guardrails validate each task. The existing `Scheduler.generate_plan()` builds the plan. Gemini can then propose a bounded, same-day revision, only when one of the request's own tasks is in a conflict. The user approves the result before anything changes. The Scheduler remains the only component that decides what gets scheduled.

Decisions already made: **Google Gemini** (`google-genai` SDK). The allowed frequencies are **`daily`, `weekly` and `as needed`**, matching app.py's dropdown. **`as needed` is a one-off task: it's due once its time arrives, and after it's completed it's never due again and doesn't recur** (the behavior after increment 0's `is_due` fix). **Each request covers a single day.** **Revisions may change times only, never dates.** **The revise step runs only when a conflict involves a task from this request.**

### Gaps in the current code that this plan accounts for
- `Task.is_due` returns `current_time >= self.time` for pending tasks ([pawpal_system.py:33](../pawpal_system.py#L33)). If the user asks at 7am for "meds at 8am" and the plan runs at `now`, the meds task silently drops out. **The agent therefore plans with `plan_time = 23:59 on the request's date`.** Code picks this value, not the LLM. app.py's existing "Generate schedule" button is left unchanged.
- `frequency` is a free string, and for a *completed* task any value except daily or weekly falls through to `return True` (always due) ([pawpal_system.py:46-48](../pawpal_system.py#L46-L48)). A completed "as needed" task would therefore reappear in every plan. **Increment 0 fixes this in `pawpal_system.py`** with one change to `is_due`: that branch returns `False`. This matches `next_occurrence()`, which already returns `None` for those frequencies. It's the only change to `pawpal_system.py`, and the file stays at exactly four classes. G6 still restricts *input* to the three supported values.
- `duration_minutes=0` always passes `_fits` and can never overlap in `_overlaps`.
- `find_conflicts` runs on *all due tasks*, including deferred ones ([pawpal_system.py:175](../pawpal_system.py#L175)). Conflict counts can therefore include deferred tasks, and the revision metric uses them as reported.
- The time-budget widget in app.py is keyed (`key="owner_time_input"`, [app.py:52-61](../app.py#L52-L61)), and its value is written back to `owner.available_time_minutes` on every rerun. After the widget's first render, Streamlit ignores `value=`. So a budget set directly on the owner by `commit()` would be overwritten by the widget's stale value on the next rerun. See increment 10.
- **main.py's sample data isn't a usable baseline for the agent.** t2 and t5 are pre-existing tasks with the same start time, so they conflict and the agent can't move either one. The 45-minute budget against 70 due minutes guarantees a deferred task. A `clean` result is unreachable there. The agent demo and eval therefore use their own fixture owner (§6), and main.py stays exactly as it is.

---

## 1. File and module layout
```
pawpal_system.py         ONE SMALL EDIT (increment 0): Task.is_due's non-daily/weekly completed branch returns False; still 4 classes
main.py                  UNCHANGED (keeps its own sample data for the original CLI demo)
guardrails.py            NEW: pure validation functions, no LLM and no I/O
llm_client.py            NEW: tool schemas, GeminiClient, MockClient, get_client(); `python llm_client.py --smoke`
agent.py                 NEW: run_agent(), commit(), setup_logging(), AgentResult
fixture_owner.py         NEW: build_fixture_owner(): the shared baseline for agent_demo, eval and tests
agent_demo.py            NEW: CLI demo for the agent (argparse)
eval_agent.py            NEW: runs every fixture and prints a PASS/FAIL summary
fixtures/mock_responses.json   NEW: recorded Gemini tool calls + expected outcomes
app.py                   EDIT: new "Ask PawPal (AI)" section before "Build Schedule"
tests/test_guardrails.py NEW
tests/test_agent.py      NEW (uses MockClient and a scripted fake client, no network)
diagrams/architecture.mmd NEW (written after the code): data flow including the human approval step
model_card.md            NEW
docs/sample_trace.log    NEW, committed: a mock run's log with a fixed --now
requirements.txt         + google-genai, python-dotenv
.gitignore               + .env, logs/
.env.example             NEW: GEMINI_API_KEY=, GEMINI_MODEL= (blank; copy the current id from Google AI Studio), PAWPAL_LLM_MODE=
```
The dependency direction runs one way: `agent → guardrails, llm_client, pawpal_system`. Guardrails import only `pawpal_system` and `datetime`. `fixture_owner` imports only `pawpal_system`.

`build_fixture_owner() -> Owner`: owner "Demo", `available_time_minutes=120`, pets **Biscuit** (dog, `pet_id="p-biscuit"`) and **Whiskers** (cat, `pet_id="p-whiskers"`), **no tasks**. The ids are fixed so traces are reproducible.

## 2. Data flow, step by step
`run_agent(request: str, owner: Owner, now: datetime, client, max_revisions=2) -> AgentResult`
1. **Log the request** with a short `run_id`.
2. **Parse.** `client.call("parse", system_prompt, user_text, PARSE_TOOLS)` returns `ToolCall(name, args)`. The prompt includes today's date, the pet names and the current budget. Function-calling mode is `ANY`, so Gemini must call a tool. **Log the parsed output.**
3. **Validate.** `guardrails.validate_request(args, request, owner, now)` returns `ValidationResult(new_tasks: list[(pet_id, ref, Task)], plan_date, budget: int|None, errors, warnings, assumptions)`. **Log the result.** If anything fails, the whole batch stops (all-or-nothing): status `needs_clarification`, nothing is scheduled and nothing is committed. Warnings (G9) don't block.
4. **Draft.** `draft = copy.deepcopy(owner)`, then add the new tasks to the draft pets by `pet_id` and apply the budget. The real `owner` is not touched. **Each draft task's `task_id` is its ref** (`new-1`, `new-2`, … in parse order), so traces, logs and fixtures are deterministic. The agent keeps `new_ids: set[str]` and identifies new tasks **by `task_id` only**, never by object equality or `in` on Task objects. Deep copies and re-runs create new objects, and dataclass `__eq__` compares fields, so object checks would be wrong.
5. **Plan.** `plan0 = Scheduler().generate_plan(draft, plan_time)`, with `plan_time = plan_date 23:59`. **Log a summary** (task ids, conflicts, deferred).
6. **Revision loop** (§5). It runs only if a conflict pair includes a new task. Each proposal is validated, then applied to a *second* deep copy, and the Scheduler runs again. **Log each proposal, its validation and the new plan.**
7. **Return an `AgentResult`** with: `status`, `new_tasks`, `plan_date`, `budget`, `assumptions`, `warnings`, `original_plan`, `revised_plan | None`, `revision_moves`, `stop_reason`, `revise_calls`, and a `trace` (the same steps that were logged, for the UI).
8. **Human approval.** The UI or CLI shows the original and revised plans side by side, the deferred tasks, any unmovable conflicts, the assumptions and any **⚠ flagged warnings**. The user picks *Approve revised*, *Approve original* or *Discard*. Only the original exists when there was no revision. `commit(result, owner, use_revision: bool)` adds the chosen versions of the new tasks to the real pets and sets the budget. It does nothing else. **`commit()` mints a fresh `task-{uuid4().hex[:8]}` id for each committed task**, matching app.py's form ([app.py:141](../app.py#L141)). app.py keys its buttons (`complete_{task_id}`) and `pet_name_by_task_id` on `task_id`, so reusing `new-1` across two approved requests would create duplicate widget keys and collide in that dict. Committed tasks are new `Task` objects, not the draft's. **Log the decision**, including a `ref → committed task_id` map.

## 3. Tool schemas (Gemini function declarations)
These are plain dict schemas passed as `types.Tool(function_declarations=[...])`, with `tool_config` mode `ANY` and `temperature=0`. There are no Python callables, so the SDK's automatic function calling never runs: our code executes everything.

**Parse step: Gemini must call exactly one tool**
```
submit_tasks(
  available_minutes?: integer,          # only if user stated their time budget
  tasks: array (max 10) of {
    pet_name: string,                   # required
    description: string,                # required, short
    quote: string,                      # required: exact words from the request this task came from
    date?: string,                      # "today" | "tomorrow" | "YYYY-MM-DD"
    time?: string,                      # "HH:MM" 24-hour
    duration_minutes?: integer,
    frequency?: string                  # description: "How often, in the user's words. Supported: daily, weekly, as needed."
  })
ask_clarification(question: string)    # request isn't pet care / can't be parsed at all
```
The system prompt says: *"Omit any field the user did not state. Never invent a time, duration or pet."* Optional fields give the model a legal way to leave a value out instead of inventing one. `date` stays per task so that a request mixing days is *detected* (G12) instead of being collapsed silently by the model. For the same reason, **`frequency` has no enum.** An enum forces the model to pick one of the three values, so "every 6 hours" would be coerced to `daily`, a guess the guardrails could never see. As a free string, the model passes the user's wording through, and G6 enforces the allowed set. The prompt adds: *"Copy the frequency as the user said it; don't convert it."*

**Revise step: Gemini must call exactly one tool. Same day only, so there is no date field.**
```
move_tasks(moves: array (max 3) of {ref: string, time: string /* HH:MM */}, reason: string)
keep_plan(reason: string)
```
This step is only reached when at least one conflict pair includes a new task. Gemini sees the day's tasks with times and durations. New tasks are labelled with stable refs (`new-1`, `new-2`, …) because UUIDs aren't reproducible. Existing tasks appear without refs as fixed blocks. Gemini also sees **only the conflict pairs that involve a new task**. Deferred tasks and the budget are left out of the revise prompt on purpose, so the model has nothing to "optimize" there.

The revise system prompt: *"Some of the user's new tasks overlap other tasks. Propose new same-day start times (HH:MM) for the referenced new tasks only, so that the listed overlaps are removed without creating new ones. Tasks without a ref are fixed. If you can't do that, call keep_plan with the reason."*

The model has no tool that adds or drops tasks, changes durations or dates, or changes the budget.

## 4. Guardrail rules (guardrails.py)
**Parse rules**: an **error** stops the whole batch with `needs_clarification`, and each error becomes a question shown to the user. Every error is collected, per task in the order G3–G8, then G10 and G12, and identical messages are shown once. A **warning** is flagged at approval and doesn't block.

| # | Rule | On failure |
|---|------|-----------|
| G1 | Tool name is in the expected set and args have the right shape and types | error, status `llm_bad_output`, no changes |
| G2 | At least 1 task and at most 10 | error: "I didn't find any pet care tasks" / too many |
| G3 | `pet_name` matches exactly one owner pet (trimmed, case-insensitive) | error: "I couldn't find a pet named 'Rex'. Which pet? You have: Biscuit, Whiskers" (or "More than one pet is named '…'." when two pets share the name) |
| G4 | `description` is not empty and is ≤ 80 characters | error |
| G5 | `duration_minutes` is present, an int, and in 1–240 (the UI's cap). **Closes the 0-duration gap.** | error: "How many minutes does 'meds' take?" |
| G6 | `frequency.strip().lower()` is in {daily, weekly, as needed}. This is the **only** place the allowed set is enforced, because the schema has no enum. It keeps unsupported recurrences out at input time, and increment 0 fixes the fall-through inside `Task.is_due` itself | error. If omitted, defaults to `as needed` and is recorded as an **assumption** |
| G7 | `date` parses strictly ("today", "tomorrow" or `%Y-%m-%d`), is not in the past and is ≤ 7 days out | error. If omitted, defaults to today and is recorded as an **assumption** |
| G8 | `time` is present and matches `^([01]\d|2[0-3]):[0-5]\d$` | error: "What time for the walk?" (this is how "this morning" is handled) |
| G9 | `quote` is a substring of the request (case- and whitespace-normalized), a cheap hallucination check | **warning**: the task is kept and shown at approval with "⚠ couldn't match this to your request: '…'" |
| G10 | `available_minutes` is an int in 0–1440 | error |
| G11 | Not a duplicate (same pet, description and datetime) of an existing task or of another task in the batch | duplicate dropped, warning logged and shown. **If every task is dropped**, error: "Everything in that request is already on the schedule." (increment 2b) |
| G12 | **All tasks resolve to the same date** (after the G7 defaults) | error: "Your request covers more than one day (Nov 2, Nov 3). Please send one day at a time." |

**Revision rules** (any failure ends the loop and keeps the last good plan)

| # | Rule | On failure |
|---|------|-----------|
| R1 | Every `ref` is a new-task ref from this run, with no repeats, and there are ≤ 3 moves | stop, `stop_reason="invalid_revision"` |
| R2 | `time` passes G8. The new datetime = **the task's existing date** + the new time. The task must end by 23:59 the same day (`time + duration` doesn't cross midnight) | stop, `invalid_revision` |
| R3 | Each move actually changes the task's time | stop, `invalid_revision` |
| R4 | The re-run plan has **strictly fewer conflicts and no more deferred tasks** | revision discarded, `stop_reason="no_improvement"` |

Defaults for date and frequency are applied by code, never by the LLM, and they are always shown to the user. Time and duration are never defaulted, because they are the values that drive fitting and conflicts.

### Why revisions are same-day only (gaming the metric)
`generate_plan` only considers tasks that are **due at `plan_time`** (end of the request's day). If a revision could change dates, moving any deferred or conflicting task to tomorrow would remove it from the due set entirely. The deferred count and the conflict count would both drop, so the metric would report a strict improvement, yet nothing would have fit and no overlap would have been resolved. The work would just leave the frame being measured. An "improve the plan" loop would learn that the cheapest fix for everything is postponing it, and the user would see a "clean" plan that hides work. With same-day moves, every task stays inside the measured window, so a lower count reflects a real change.

R4 closes a smaller version of the same loophole. Because `_sort_by_priority` ranks by `time`, a same-day move to a later time *demotes* a task, which can change which tasks fit and lower the deferred count by reshuffling priority, not by finding time. So a drop in deferred tasks alone never counts as an improvement. Only a strict drop in conflicts does, and deferred tasks may not increase. The §5 gate goes further: the LLM is never asked about a plan whose only problem is deferred tasks. **Over-budget requests end in `no_movable_conflicts` with zero revise calls**, and the deferred tasks are reported to the user, who decides whether to free up time or reschedule.

## 5. Loop termination
```
def movable(plan):
    return [(a, b) for a, b in plan.conflicts if a.task_id in new_ids or b.task_id in new_ids]

def gate_stop(plan):                     # used before each revise call AND after the loop
    if not plan.conflicts and not plan.deferred: return "clean"
    return "no_movable_conflicts"

plan = plan0
for i in range(MAX_REVISIONS):          # MAX_REVISIONS = 2, a hard cap
    if not movable(plan):
        return stop(gate_stop(plan))                                 # no LLM call
    call = client.call("revise", ...)    # only movable pairs in the prompt
    if call.name == "keep_plan":         return stop("model_kept_plan")
    if moves fail R1–R3:                 return stop("invalid_revision")
    new_plan = generate_plan(apply(moves))
    if not better(new_plan, plan):       return stop("no_improvement")   # R4
    plan = new_plan

# The loop ran out of iterations. Re-apply the gate before calling it a cap hit.
if not movable(plan): return stop(gate_stop(plan))    # e.g. 2nd accepted revision fixed everything
return stop("max_iterations")                          # movable conflicts still remain
```
`max_iterations` therefore means only that movable conflicts remain and the cap stopped the loop. A run whose last allowed revision succeeded ends `clean` or `no_movable_conflicts`, just like any other run.
**Call counts:**
- A clarification or bad-output request makes 1 call (parse only).
- A request with no conflict involving its own tasks makes 1 call. This covers clean plans, over-budget plans and conflicts only among pre-existing tasks.
- A request whose own tasks conflict makes 2 or 3 calls (parse plus 1–2 revise). The hard maximum is `1 + MAX_REVISIONS = 3`.

`AgentResult.revise_calls` records the actual count, and the eval checks it. Any client exception (network, quota, timeout, a non-tool response) stops the run and returns the last valid state with `status="llm_error"`. Every exit path logs its `stop_reason`, and the result always lists the remaining deferred tasks and any unmovable conflicts.

## 6. Mock mode and reproducibility
- `llm_client.get_client()`: if `PAWPAL_LLM_MODE=mock`, or `GEMINI_API_KEY` is unset, return `MockClient` and log a warning (the UI also shows a banner). Otherwise return `GeminiClient`. `.env` is loaded with `python-dotenv`. `.env` is gitignored and `.env.example` is committed.
- `fixtures/mock_responses.json` is keyed by the normalized request (lowercased, whitespace collapsed):
  `{ "<request>": {"parse": {...}, "revise": [...], "expect": {"status", "stop_reason", "revise_calls", "conflicts", "deferred": [...]}} }`.
  `revise[i]` answers revision iteration `i`, and is empty when no revise call is expected. `expect` is used only by `eval_agent.py`.
- **Baseline:** every fixture runs against `build_fixture_owner()` (Biscuit and Whiskers, no tasks, a 120-minute budget) with `now = 2026-11-02T07:00`, so `plan_time = 2026-11-02 23:59`. Fixtures use `"today"` and `HH:MM`, never absolute dates, so they don't go stale. Output is byte-identical from run to run.
- If mock mode gets an unknown request, it returns `status="mock_unknown_request"` with the list of supported demo requests.
- `agent_demo.py --record` wraps `GeminiClient` in a recorder that writes the parse and revise calls to the fixture file, so the fixtures are real Gemini output. `expect` blocks are written by hand.

**Demo fixtures (6), each rechecked against the baseline:**

| # | Request (abridged) | Trace against the baseline | `expect` |
|---|---|---|---|
| 1 | "Biscuit needs meds at 8am, a 30 minute walk this morning, and I only have 45 minutes today." | meds has no duration (G5); the walk has no time (G8) | `needs_clarification`, `stop_reason=null`, `revise_calls=0`, 2 questions |
| 2 | "Today Biscuit needs a 30 minute walk at 8:00 and 5 minutes of meds at 8:15." | walk 08:00–08:30 overlaps meds at 08:15 → 1 conflict, and both tasks are new. Total 35 of 120 min, 0 deferred. Recorded revise: move meds (`new-2`) to `08:30`. `_overlaps` uses `>`, so 08:30 ≥ 08:30 means no overlap → 0 conflicts, 0 deferred (R4 ✓). Next iteration: no movable conflicts and nothing deferred → `clean` | `ok`, `clean`, `revise_calls=1`, conflicts 0, deferred [] |
| 3 | "I only have 30 minutes today. Biscuit needs a 25 minute walk at 9:00 and Whiskers needs a 20 minute brushing at 17:00." | budget 30. Priority runs by time: the walk fits (5 min left) and brushing is deferred. No overlap → no movable conflicts → no revise call | `ok`, `no_movable_conflicts`, `revise_calls=0`, conflicts 0, deferred ["brushing"] |
| 4 | "Rex needs a 20 minute walk at 10:00 today." | there is no pet named Rex (G3) | `needs_clarification`, `revise_calls=0` |
| 5 | "Biscuit needs a 20 minute walk today at 9:00 and Whiskers needs a 15 minute bath tomorrow at 10:00." | both dates are individually valid (G7 ✓), but they differ (G12) | `needs_clarification`, `revise_calls=0` |
| 6 | "Biscuit needs a 15 minute walk at 9:00, every 6 hours." | the recorded parse has `frequency: "every 6 hours"`, which isn't in the allowed set (G6). The date is omitted, so it defaults to today (an assumption) | `needs_clarification`, `revise_calls=0` |

Fixture 6 also guards the schema decision. If a live run ever records `frequency: "daily"` for that request, the model has started converting instead of copying, and `eval_agent.py --live` fails on it.

Unit tests cover the cases the fixtures can't reach, using a hand-built owner: a conflict only between pre-existing tasks (→ `no_movable_conflicts`, 0 revise calls), and a mix of one pre-existing conflict plus one involving a new task (→ the revise call sees only the movable pair).

## 7. Logging
`agent.setup_logging()` configures the `pawpal.agent` logger to write `logs/agent.log` (gitignored) and to the console in the CLI. Each line has the form `run=<id> step=<request|parse_output|validation|plan|revision_proposal|revision_validation|revised_plan|stop|approval> <json>`. Tasks are serialized as dicts. The `stop` line includes `stop_reason` and `revise_calls`. The same entries go into `AgentResult.trace` for the UI's "Agent trace" expander. A copy of one mock run is committed as `docs/sample_trace.log`.

## 8. Failure modes and mitigations
- **LLM invents a time or duration** → fields are optional, and the prompt forbids inventing. G9 flags a quote that doesn't match the request. The approval screen shows every value, the assumptions and the warnings.
- **LLM paraphrases the quote** (a false G9 hit) → this is why G9 is a warning and not an error. The user sees the flag and decides.
- **Vague times ("this morning", "after work")** → G8 asks for clarification. This is intentional and is fixture 1.
- **Future-today tasks not due at `now`** → `plan_time` is set to the end of the request's day.
- **Request dated tomorrow** → the plan is built for tomorrow 23:59. Under the existing `is_due` semantics, today's still-pending tasks are overdue and also appear. This is documented, not changed.
- **Multi-day requests** → G12 asks the user to split them.
- **Prompt injection ("ignore rules, schedule everything")** → the model can only emit the four tools. Guardrails bound every field, and the Scheduler alone decides placement.
- **Revision gaming** (date changes, or priority reshuffles that lower the deferred count) → same-day moves only, R4's conflict-first metric, and the §5 gate that never sends deferred-only plans to the LLM.
- **Revision oscillation** → the hard cap plus the strict-improvement check.
- **Conflicts among the user's pre-existing tasks** → not sent to the LLM. The run ends with `no_movable_conflicts` and the conflicts are reported, not hidden.
- **Unreachable demo outcomes because of noisy sample data** → the dedicated `build_fixture_owner()` baseline. main.py's data isn't used by the agent.
- **API errors, quota, missing key** → `llm_error` with nothing changed, or an automatic fallback to mock with a visible warning.
- **Streamlit reruns lose the pending result** → store `AgentResult` in `st.session_state.pending_agent_result` and clear it after commit or discard, which also prevents double-approving.
- **Committed budget overwritten by the keyed widget** → handled in increment 10.
- **Gemini returns text instead of a tool call** → treated as G1 `llm_bad_output`.
- **Clock-dependent tests** → `now` is always injected and never read inside agent or guardrails.
- **Model name drift or SDK response shape changes** → `GEMINI_MODEL` env var, plus the smoke test in increment 4.

## 9. Build increments (each one testable on its own)
**[CUT]** = can be dropped if time runs short, and what replaces it. **[REQUIRED]** = a rubric deliverable that can't be cut.

**Commit after each increment** once its test passes: one commit per increment, with a message like `inc 6: revision loop + R1–R4 + movable gate`. That way the history shows the build order, and any increment can be reverted on its own.

0. **Fix `Task.is_due` for non-recurring completed tasks** (its own commit, before anything else: `inc 0: completed non-recurring tasks are not due again`). In [pawpal_system.py:46-48](../pawpal_system.py#L46-L48), change the final `return True` to `return False`, and update the comment to say a completed task with no recurrence rule is done for good, consistent with `next_occurrence()` returning `None`. There are no other changes, and the file still has four classes. *Tests added to `tests/test_pawpal.py`:*
   - a completed `"as needed"` task is **not** due, even a week after `completed_at`;
   - a completed `"daily"` task **is** due once 24 hours have passed since `completed_at`, and isn't due before then;
   - a **pending** `"as needed"` task is unaffected: it's not due before its `time` and is due at or after it.

   *Pass criteria:* the full existing suite still passes (no current test relies on the fall-through; the only non-recurring test, `test_complete_task_returns_none_for_a_non_recurring_task`, checks `complete_task`, not `is_due`), `python main.py` still runs, and `grep -c "^class " pawpal_system.py` returns 4.
1. **Housekeeping**: requirements, `.gitignore` (`.env`, `logs/`), `.env.example`, and `fixture_owner.py`. *Test:* `git check-ignore .env logs/x` prints both; `pip install -r requirements.txt`; `build_fixture_owner()` has 2 pets, 0 tasks and a 120-minute budget.
2. **`guardrails.validate_request`** (G1–G12, pure). *Test:* `tests/test_guardrails.py`, at least one case per rule, including duration 0, frequency "every 6 hours", unknown pet, "25:00", "next blursday", a mixed-date batch, and a quote mismatch (asserting a *warning*, not an error).
3. **`llm_client`**: schemas, `ToolCall`, `MockClient`, `get_client()`, plus hand-written parse fixtures. *Test:* a known request returns its fixture, an unknown request is flagged, and no key gives mock.
4. **Live Gemini smoke test**: `python llm_client.py --smoke` makes **one** real parse call on a fixed request. It prints the model id actually used, asserts the response has exactly one `function_call` whose `name` is in the parse tool set and whose `args` converts to a plain dict, and exits non-zero otherwise. Without a key it skips and says so. *Test:* run it once with a key.
5. **`agent.run_agent`, parse + plan only** (no revision), plus `setup_logging`. *Test:* in mock mode against `build_fixture_owner()`, the new tasks appear in `original_plan`, `plan_time` is the end of the request's day, the **real owner is unchanged** (deep-copy check), and log lines are written.
6. **Revision loop + `validate_moves` (R1–R4) + the §5 gate**. *Test:* a scripted fake client covering:
   - an improving same-day move is accepted;
   - a move that only lowers the deferred count is discarded (`no_improvement`);
   - a move crossing midnight, or a bad ref, gives `invalid_revision`;
   - **two accepted revisions that end clean**: new tasks A 08:00/30m, B 08:15/5m, C 09:00/30m and D 09:10/5m give 2 movable conflicts. Revise 1 moves B to 08:30 (2→1), and revise 2 moves D to 09:30 (1→0). Expect `stop_reason="clean"` (not `max_iterations`) and `revise_calls=2`;
   - **cap hit with movable conflicts left**: three such overlapping pairs, with each revise fixing one. Expect `max_iterations`, `revise_calls=2` and 1 movable conflict remaining;
   - new tasks are detected by `task_id` (`new-N`): a pre-existing task with identical fields isn't treated as new;
   - a clean plan, an over-budget plan and a plan with only pre-existing conflicts each make **0** revise calls, with the right `stop_reason`;
   - the revise prompt contains only movable pairs.
7. **`commit()`**. *Test:* approving the revised or original version adds the right task times to the right pets and sets the budget; discard leaves the owner unchanged. Committed ids match `^task-[0-9a-f]{8}$` and none start with `new-`. Committing two separate agent results leaves all of the owner's `task_id`s unique.
8. **`agent_demo.py`** (uses `build_fixture_owner()`; main.py untouched): `--mock`, `--now`, `--yes`, a positional request, or no request to run every demo fixture. *Test:* `python agent_demo.py --mock --now 2026-11-02T07:00 --yes` gives the same output twice; `python main.py` output is unchanged.
9. **Record live fixtures** (`agent_demo.py --record`). *Test:* record all 6 live, re-run in mock, and output matches. **[CUT]**: keep the hand-written fixtures from increment 3, and label them "hand-authored, shape verified by the smoke test" in the README.
10. **app.py section**: a text area, a "Plan with AI" button, the clarification questions, a two-column original/revised view, the deferred list, unmovable conflicts, the ⚠ warnings and assumptions, and the approval buttons. The AI section plans against the live session `owner`, not the fixture owner.
    **Budget widget handling:** the approve handler can't write `st.session_state.owner_time_input` directly, because that widget was already created higher up in this run. Streamlit raises an exception when a widget's state is modified after it renders, and `value=` is ignored once the key exists. So the handler calls `commit()`, stores `st.session_state.pending_budget = result.budget`, and calls `st.rerun()`. At the top of the script, **before** the `number_input` is created: `if "pending_budget" in st.session_state: st.session_state.owner_time_input = st.session_state.pop("pending_budget")`. The widget then renders with the committed value, and the existing `owner.available_time_minutes = int(...)` line writes back the same number.
    *Test:* `streamlit run app.py` with `PAWPAL_LLM_MODE=mock`. Add Biscuit and Whiskers, run fixture 3 (which states a 30-minute budget), approve it, then click any other button: the budget field still shows 30, and "Generate schedule" uses 30.
    Sub-item **[CUT]**: the "Agent trace" expander (the log file and sample trace still cover it).
11. **`eval_agent.py`**: for each fixture, run the agent in mock mode against `build_fixture_owner()` with `now = 2026-11-02T07:00`, compare it to `expect` (status, stop_reason, revise_calls, conflict count, deferred descriptions), print a PASS/FAIL table plus "N/6 passed", and exit non-zero on any failure. `--live` runs the same checks against real Gemini. *Test:* all 6 pass in mock. **[CUT]** the `--live` flag only. Keep the mock eval.
12. **`docs/sample_trace.log`**: produced by `agent_demo.py --mock --now 2026-11-02T07:00 --yes` on fixtures 2 and 3, then committed. *Test:* it contains every step name from §7, including one `revision_proposal` and one `stop_reason=no_movable_conflicts`. **[CUT]**: the README can quote a few log lines instead.
13. **`diagrams/architecture.mmd`** **[REQUIRED]**, written after the code so it matches: request → Gemini parse → guardrails (to clarification on error) → draft copy → Scheduler → gate: "a conflict involves a new task?" (no → stop) → revision loop (Gemini → R1–R4 → Scheduler, max 2) → **human approval** (approve revised / original / discard) → commit to Owner. Logging is shown as a side channel. *Test:* it renders in the Mermaid live editor or GitHub preview.
14. **`model_card.md`** **[REQUIRED]**, with these sections:
    - **System overview**: intended use, model and version (taken from the smoke test), mock vs live.
    - **What the LLM may and may not decide**: it parses and proposes same-day moves, while the Scheduler decides; the guardrails, the revise gate and the same-day rule.
    - **How I used AI while building this**: AI-assisted planning in revisions 1–3, then the build in increments.
    - **A suggestion I accepted**: e.g. the per-task `quote` field with the G9 hallucination check (later softened to a warning), or planning at the end of the day to work around `is_due`.
    - **A suggestion I rejected or revised**: rev 1 let `move_tasks` change dates, and the over-budget fixture "fixed" deferral by moving a task to tomorrow. I caught that this games the metric: tasks moved off the planned day leave the due set, so the conflict and deferred counts drop while nothing gets done. The fix was same-day-only moves, a conflict-first R4, and the gate so deferred-only plans never reach the LLM.
    - **Limitations and biases**:
      - English-only parsing.
      - Priority is by time only, which follows from the Scheduler's design.
      - Clarifications favor users who give exact times.
      - G9 can false-flag paraphrases.
      - The parse step depends on Gemini's interpretation.
      - Naive local datetimes.
      - The fixtures cover a small two-pet baseline.
    - **Eval results**: the `eval_agent.py` summary table (mock, and live if increment 11's `--live` was kept), plus the pytest count.
15. **README** **[REQUIRED]** (plus an `ai_interactions.md` entry): setup, `.env`, mock vs live, how to run the demo and eval, the fixture-owner baseline, and links to the diagram, the model card and the sample trace. It also includes a **guardrail examples table**:

    | Input | Behavior | Result |
    |---|---|---|
    | "Biscuit needs meds at 8am" | G5: no duration | asks "How many minutes does 'meds' take?" — nothing scheduled |
    | "a 30 minute walk this morning" | G8: no exact time | asks "What time for the walk?" |
    | "Rex needs a 20 minute walk at 10:00" | G3: unknown pet | asks "I couldn't find a pet named 'Rex'. Which pet? You have: Biscuit, Whiskers" |
    | "…walk every 6 hours" | G6: unsupported frequency | asks to choose daily, weekly or as needed |
    | "walk today at 9, bath tomorrow at 10" | G12: two dates | asks to send one day at a time |
    | "…at 25:00" | G8: invalid time | asks for a valid time |
    | parsed task quote not found in the request | G9: warning | task kept, ⚠ flagged at approval |
    | walk 08:00/30m + meds 08:15 | conflict on new tasks → revise | meds moved to 08:30 → `clean` (after approval) |
    | 30-min budget, 45 min of tasks, no overlap | gate: nothing movable | 0 revise calls, `no_movable_conflicts`, deferred task reported |
    | revision tries to move a pre-existing task | R1: bad ref | `invalid_revision`, last good plan kept |

The **minimum deliverable** is 0–8, 10, 13, 14 and 15. 9, 11 and 12 strengthen the evidence and are the first things to cut.

## Verification (end to end)
- `pytest` passes, including the existing `tests/test_pawpal.py` unchanged, with no network.
- `python llm_client.py --smoke` passes with a key and skips cleanly without one.
- `python eval_agent.py --now 2026-11-02T07:00` → `6/6 passed` in mock mode, including the `revise_calls` checks (0, 1, 0, 0, 0, 0).
- `PAWPAL_LLM_MODE=mock python agent_demo.py --now 2026-11-02T07:00 --yes` shows the clarification, the same-day conflict fix ending `clean`, the over-budget run ending `no_movable_conflicts` with brushing deferred, the unknown pet, the mixed-day rejection and the "every 6 hours" frequency rejection. `logs/agent.log` has every step.
- `python main.py` output is unchanged from before.
- In `streamlit run app.py` in mock mode: submit, review, approve; the tasks appear in the Task list; the budget persists across reruns; "Generate schedule" agrees.
- `grep -c "^class " pawpal_system.py` returns 4.
