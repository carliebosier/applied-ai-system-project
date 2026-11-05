# PawPal+ Agentic Request Planner

**Base project:** PawPal+ (Module 2). PawPal+ was a Streamlit app and Python library (`Task`, `Pet`, `Owner`, `Scheduler`) that helps a pet owner plan daily care. It tracked tasks with durations and recurrence, sorted and filtered them, detected overlapping tasks, and fit due tasks into the owner's time budget with a line-by-line explanation. Tasks went in only through form fields.

**This project** adds a plain-English entry point on top of it: *"Today Biscuit needs a 30 minute walk at 8:00 and 5 minutes of meds at 8:15."* An LLM parses the request, deterministic guardrails validate it, the existing `Scheduler` builds the plan, the LLM may propose a bounded same-day fix when the request's own tasks overlap, and **nothing changes until the user approves**.

## Why it matters

LLMs are good at reading messy text and bad at being trusted with someone's schedule. This system uses the LLM only for the part it is good at (parsing, and proposing a time move) and keeps every decision that matters in code: validation, scheduling, conflict checks, and the final write to the owner.

## Architecture overview

Source: [`diagrams/architecture.mmd`](diagrams/architecture.mmd) (Mermaid). Colors: orange = LLM call, blue = deterministic code, pink = human checkpoint, green = data or state.

1. **Parse.** The LLM must call a tool (`submit_tasks` or `ask_clarification`). It cannot return free text.
2. **Guardrails (G1-G12).** Every field is validated in code. Any error stops the whole batch (all or nothing) and the user is asked a question.
3. **Draft and plan.** The tasks are added to a *copy* of the owner and the existing `Scheduler.generate_plan` runs at 23:59 on the request's date.
4. **Gate.** The LLM is only asked to revise if a conflict involves one of the new tasks. Over-budget plans and conflicts among existing tasks never reach it.
5. **Revision loop (max 2 rounds).** The LLM proposes same-day time moves. Rules R1-R4 check them, the Scheduler re-plans, and a move is kept only if it strictly reduces conflicts without deferring more tasks.
6. **Human approval.** The user picks revised, original, or discard. Only `commit()` writes to the real owner.

Every step is logged to `logs/agent.log`.

## Setup

```bash
git clone https://github.com/carliebosier/applied-ai-system-project.git 
cd applied-ai-system-project
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

**Mock mode (no API key needed, deterministic):** leave `GEMINI_API_KEY` blank, or set `PAWPAL_LLM_MODE=mock` in `.env`. Mock mode replays recorded responses from `fixtures/mock_responses.json` and understands the six demo requests below.

**Live mode:** put your key in `GEMINI_API_KEY` and a current model id from Google AI Studio in `GEMINI_MODEL` (no default is built in), and leave `PAWPAL_LLM_MODE` blank. Verify it with:

```bash
python llm_client.py --smoke
```

`.env` is gitignored. Never commit it.

### Run it

```bash
python agent_demo.py --mock --now 2026-11-02T07:00 --yes   # all six demo requests
python agent_demo.py --mock "Rex needs a 20 minute walk at 10:00 today."   # one request, asks before committing
python eval_agent.py                                        # 6 fixtures, PASS/FAIL table
python -m pytest -q                                         # full test suite
PAWPAL_LLM_MODE=mock streamlit run app.py                   # UI: see "Ask PawPal (AI)"
```

In the UI, add pets named **Biscuit** and **Whiskers** first (the mock fixtures use those names), then open "Demo requests" under "Ask PawPal (AI)".

## Sample interactions

All outputs below are real, from `python agent_demo.py --mock --now 2026-11-02T07:00 --yes`. Mock mode replays recorded model responses; see "Live model check" for a real Gemini call.

### 1. A conflict the agent fixes (agent workflow)

Input: `Today Biscuit needs a 30 minute walk at 8:00 and 5 minutes of meds at 8:15.`

```text
Status: ok
Assumptions:
  - No frequency given for 'walk', so it's set to 'as needed' (one time).
  - No frequency given for 'meds', so it's set to 'as needed' (one time).
Original plan:
  Task        Pet      Start  End    Min  Result
  ----------  -------  -----  -----  ---  ---------
  new-1 walk  Biscuit  08:00  08:30   30  scheduled
  new-2 meds  Biscuit  08:15  08:20    5  scheduled
  Conflict: walk overlaps meds
  Budget: 35 of 120 min used
Revision moves:
  new-2 meds: 08:15 -> 08:30 (Moving meds to 08:30 starts it right after the walk ends, removing the overlap.)
Revised plan:
  Task        Pet      Start  End    Min  Result
  ----------  -------  -----  -----  ---  ---------
  new-1 walk  Biscuit  08:00  08:30   30  scheduled
  new-2 meds  Biscuit  08:30  08:35    5  scheduled
  Budget: 35 of 120 min used
Stop reason: clean (no conflicts and nothing deferred)
LLM revise calls: 1

Approved the revised plan.
Committed to the owner:
  Biscuit: walk at 08:00 (30 min)
  Biscuit: meds at 08:30 (5 min)
```

The `new-N` labels are stable refs used inside the agent; the real tasks get fresh ids when committed.

### 2. Over budget: the LLM is deliberately not asked

Input: `I only have 30 minutes today. Biscuit needs a 25 minute walk at 9:00 and Whiskers needs a 20 minute brushing at 17:00.`

```text
Status: ok
Original plan:
  Task            Pet       Start  End    Min  Result
  --------------  --------  -----  -----  ---  ---------
  new-1 walk      Biscuit   09:00  09:25   25  scheduled
  new-2 brushing  Whiskers  17:00  17:20   20  deferred
  Budget: 25 of 30 min used
Stop reason: no_movable_conflicts (nothing the LLM may fix; deferred tasks and conflicts among existing tasks are left to you)
LLM revise calls: 0

Approved the original plan.
Committed to the owner:
  Biscuit: walk at 09:00 (25 min)
  Whiskers: brushing at 17:00 (20 min)
  Budget: 120 -> 30 min
```

Deferred tasks are reported to the user. They are never handed to the LLM, which could otherwise "fix" them by moving work out of the day.

### 3. Guardrails stop a vague request

Input: `Biscuit needs meds at 8am, a 30 minute walk this morning, and I only have 45 minutes today.`

```text
Status: needs_clarification
  Question: How many minutes does 'meds' take?
  Question: What time for 'walk'?
LLM revise calls: 0
```

Nothing is scheduled and nothing is committed. The LLM was told never to invent a time or duration, and the guardrails check that it didn't.

### Live model check

`python llm_client.py --smoke` makes one real Gemini call (model `gemini-3.6-flash`) and verifies the response is exactly one valid tool call:

```text
model: gemini-3.6-flash
request: Biscuit needs meds at 8am, a 30 minute walk this morning, and I only have 45 minutes today.
tool: submit_tasks
{
  "tasks": [
    {
      "quote": "Biscuit needs meds at 8am",
      "pet_name": "Biscuit",
      "description": "meds",
      "date": "today",
      "time": "08:00"
    },
    {
      "date": "today",
      "pet_name": "Biscuit",
      "duration_minutes": 30,
      "quote": "a 30 minute walk this morning",
      "description": "walk"
    }
  ],
  "available_minutes": 45
}
SMOKE OK
```

The model left out the meds duration and the walk time instead of inventing them, which is what the guardrails rely on.

## Reliability and guardrail results

### Evaluation script

`python eval_agent.py` runs all six fixtures through the real agent and compares each to its expected status, stop reason, LLM call count, conflicts, and deferred tasks:

```text
#  Request                                             Status               Stop reason           Calls  Result
-  --------------------------------------------------  -------------------  --------------------  -----  ------
1  Biscuit needs meds at 8am, a 30 minute walk thi...  needs_clarification  -                     0      PASS
2  Today Biscuit needs a 30 minute walk at 8:00 an...  ok                   clean                 1      PASS
3  I only have 30 minutes today. Biscuit needs a 2...  ok                   no_movable_conflicts  0      PASS
4  Rex needs a 20 minute walk at 10:00 today.          needs_clarification  -                     0      PASS
5  Biscuit needs a 20 minute walk today at 9:00 an...  needs_clarification  -                     0      PASS
6  Biscuit needs a 15 minute walk at 9:00, every 6...  needs_clarification  -                     0      PASS

6/6 passed
```

### Guardrail examples

| Input | Guardrail | Result |
|---|---|---|
| `meds at 8am` (no duration) | G5: duration required | Asks "How many minutes does 'meds' take?" Nothing scheduled. |
| `a 30 minute walk this morning` | G8: exact time required | Asks "What time for 'walk'?" |
| `Rex needs a 20 minute walk at 10:00 today.` | G3: unknown pet | "I couldn't find a pet named 'Rex'. Which pet? You have: Biscuit, Whiskers" |
| `walk today at 9:00 ... bath tomorrow at 10:00` | G12: one day per request | "Your request covers more than one day (Nov 2, Nov 3). Please send one day at a time." |
| `a 15 minute walk at 9:00, every 6 hours` | G6: only daily, weekly, as needed | "How often should 'walk' happen? I can schedule daily, weekly or as needed, not 'every 6 hours'." |
| Task quote not found in the request | G9: hallucination check | Task kept, warning shown at approval |
| Walk 08:00-08:30 and meds 08:15 | Revision loop | Meds moved to 08:30, ends `clean` (sample 1) |
| 30-minute budget, 45 minutes of tasks | Gate | 0 LLM calls, `no_movable_conflicts`, brushing reported as deferred |
| LLM tries to move a pre-existing task, or past midnight | R1 / R2 | Proposal rejected, `invalid_revision`, last good plan kept |
| Revise call fails (e.g. a 503) | Error handling | `llm_error`, last good plan kept; the original can still be approved |

Each row is covered by a test in `tests/`.

### Test summary

```text
$ python -m pytest -q
277 passed in 2.49s
```

- Guardrails G1-G12, the revision rules, the commit/discard step, the CLI, the eval script, the Streamlit section (via Streamlit's `AppTest`), and the committed trace are all tested. No test uses the network.
- **What worked:** the budget-widget regression test failed when I removed the fix and passed with it, so it does guard that bug. Forced function calling returned exactly one valid tool call on a real model.
- **What didn't, or isn't proven:** the six fixtures are hand-written, so the eval shows the agent and guardrails behave as designed on those inputs, not that Gemini would parse them identically. A live run of `gemini-3.8-flash` returned `503 UNAVAILABLE`, so live behavior depends on provider availability (the agent turns that into `llm_error` and keeps the last good plan). Fixture 6 shows the guardrail rejects "every 6 hours" but cannot show that the live model copies the phrase instead of converting it; that needs a live run.

### Execution evidence

[`docs/sample_trace.log`](docs/sample_trace.log) is the agent's own log for samples 1 and 2: one line per step (`request`, `parse_output`, `validation`, `plan`, `revision_proposal`, `revision_validation`, `revised_plan`, `stop`, `approval`). Regenerating it changes the run ids and the committed `task-...` ids, which are random; the steps and payloads are reproducible.

## Design decisions and trade-offs

- **The LLM never decides what gets scheduled.** It can only call four tools. The Scheduler decides placement, and `commit()` is the only code that writes to the owner.
- **All or nothing validation.** One bad field asks a question and schedules nothing, so the schedule is never half-updated.
- **No enum on frequency.** An enum would make the model turn "every 6 hours" into "daily", a guess the guardrails could never see. The model passes the user's words through and G6 checks them.
- **Revisions are same-day only.** If a revision could change dates, moving a task to tomorrow would remove it from the planned day, and the conflict and deferred counts would drop while nothing got done. Same-day moves, a conflict-first improvement rule (R4), and a gate that never sends deferred-only plans to the LLM close that loophole.
- **Plans run at 23:59 on the request's date,** because `Task.is_due` would otherwise silently drop "meds at 8am" when asked at 7am.
- **Mock mode.** It makes the demo and tests deterministic and keyless. The cost is that it only understands the six recorded requests.
- **One small fix to the base project:** a completed `"as needed"` task is no longer treated as always due (`Task.is_due`). `pawpal_system.py` still has exactly four classes.

## Project layout

```text
pawpal_system.py   Base project: Task, Pet, Owner, Scheduler
guardrails.py      Validation rules G1-G12 (pure, no LLM)
llm_client.py      Tool schemas, GeminiClient, MockClient, get_client, --smoke
agent.py           run_agent, revision loop, commit, discard, logging
agent_demo.py      CLI demo         eval_agent.py   fixture eval
app.py             Streamlit UI, including "Ask PawPal (AI)"
fixtures/          Recorded mock responses + expected outcomes
diagrams/          architecture.mmd (Mermaid source)
docs/              sample_trace.log
tests/             277 tests
model_card.md      Limitations, misuse, AI collaboration, eval results
```

## Reflection
This project taught me that AI is most useful when it has a small job and I stay in charge of the rest. It was great for explaining things I didn’t understand, fixing errors, and helping me build and write up PawPal+. But I learned I can’t just accept what it gives me. One diagram looked fine on the screen but was way too big to read, so I had to push back and ask for a simpler one. I also learned that passing tests don’t mean everything works. I had to double check these things myself and make sure that whatever I was asking for was clear enough for the AI to give me a good output.

## Links

- Architecture diagram: [`diagrams/architecture.mmd`](diagrams/architecture.mmd)
- Model card: [`model_card.md`](model_card.md)
- Sample trace: [`docs/sample_trace.log`](docs/sample_trace.log)