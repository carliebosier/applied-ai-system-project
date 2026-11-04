import json
import os
from datetime import datetime, timedelta
from uuid import uuid4

import streamlit as st
import agent
import llm_client
from agent_demo import STOP_MEANINGS
from pawpal_system import Owner, Pet, Scheduler, Task

st.set_page_config(page_title="PawPal+", page_icon="🐾", layout="centered")

st.title("🐾 PawPal+")

st.markdown(
    """
**PawPal+** is a pet care planning assistant. It helps a pet owner plan care tasks
for their pet(s) based on constraints like available time and how overdue each task is.

This app is the interactive demo for the classes in `pawpal_system.py`.
"""
)

with st.expander("How the pieces fit together", expanded=False):
    st.markdown(
        """
- **Task** — one unit of care (description, duration, frequency).
- **Pet** — a profile plus the tasks attached to it (`add_task`, `get_tasks`).
- **Owner** — the person, their pets, and a daily time budget (`add_pet`, `get_all_tasks`).
- **Scheduler** — fits the most overdue due tasks into the budget and explains each call.

The UI below builds real objects and hands them to those methods. The `Owner` lives in
`st.session_state`, so it survives Streamlit's rerun-on-every-interaction.
"""
    )

st.divider()

# The whole object graph hangs off this one Owner, so it is the only thing that
# needs to live in session_state. Mutating it in place persists automatically.
if "owner" not in st.session_state:
    st.session_state.owner = Owner(
        owner_id=f"owner-{uuid4().hex[:8]}",
        name="Jordan",
        available_time_minutes=120,
    )

owner: Owner = st.session_state.owner

# One Scheduler for the whole page: the task list sorts with it long before the
# "Generate schedule" button below ever runs.
scheduler = Scheduler()

st.subheader("Owner")
owner.name = st.text_input("Owner name", value=owner.name, key="owner_name_input")
# An approved AI plan can set a new budget. The keyed widget ignores value= after its first
# render and can't be changed once drawn, so the approve handler parks the budget here and
# reruns; it is handed to the widget before the widget is created.
if "pending_budget" in st.session_state:
    st.session_state.owner_time_input = st.session_state.pop("pending_budget")
owner.available_time_minutes = int(
    st.number_input(
        "Time available today (minutes)",
        min_value=0,
        max_value=1440,
        value=owner.available_time_minutes,
        step=15,
        key="owner_time_input",
    )
)

st.divider()

st.subheader("Pets")
st.caption("Add a pet, then attach care tasks to it below.")

pet_col1, pet_col2 = st.columns(2)
with pet_col1:
    new_pet_name = st.text_input("Pet name", value="Mochi", key="new_pet_name")
with pet_col2:
    new_pet_species = st.selectbox("Species", ["dog", "cat", "other"], key="new_pet_species")

if st.button("Add pet"):
    if not new_pet_name.strip():
        st.warning("Give the pet a name first.")
    else:
        # Build the object here, hand the finished Pet to the Owner — the same
        # shape as Task construction below.
        pet = Pet(
            pet_id=f"pet-{uuid4().hex[:8]}",
            name=new_pet_name.strip(),
            species=new_pet_species,
        )
        owner.add_pet(pet)
        st.success(f"Added {pet.name} ({pet.species}).")

if not owner.pets:
    st.info("No pets yet. Add one above to start scheduling.")
    st.stop()

st.write(f"**{owner.name}** is caring for: " + ", ".join(f"{p.name} ({p.species})" for p in owner.pets))

st.divider()

st.subheader("Tasks")
st.caption("Tasks attach to a specific pet and feed straight into the scheduler.")

# Select by pet_id, not by the Pet itself: Streamlit deep-copies widget values,
# so a selectbox over Pet objects hands back a copy and add_task would mutate
# the copy instead of the Pet living in session_state.
pets_by_id = {pet.pet_id: pet for pet in owner.pets}
selected_pet_id = st.selectbox(
    "Which pet is this task for?",
    options=list(pets_by_id),
    format_func=lambda pet_id: f"{pets_by_id[pet_id].name} ({pets_by_id[pet_id].species})",
    key="task_pet_choice",
)
selected_pet: Pet = pets_by_id[selected_pet_id]

task_col1, task_col2, task_col3 = st.columns(3)
with task_col1:
    task_description = st.text_input("Task title", value="Morning walk", key="new_task_title")
with task_col2:
    task_duration = st.number_input(
        "Duration (minutes)", min_value=1, max_value=240, value=20, key="new_task_duration"
    )
with task_col3:
    # Task has no priority field — Scheduler derives urgency from completion state
    # and how long a task has waited. `frequency` is what Task.is_due reads.
    task_frequency = st.selectbox(
        "Frequency", ["daily", "weekly", "as needed"], key="new_task_frequency"
    )

# When this task is due, rather than whenever the button happened to be clicked.
# Scheduler and Task.is_due both read `time`, so this is what makes a task land
# at 10 AM instead of "now", and what lets two tasks actually conflict.
time_col1, time_col2 = st.columns(2)
with time_col1:
    task_date = st.date_input("Date", value=datetime.now().date(), key="new_task_date")
with time_col2:
    task_time = st.time_input(
        "Start time", value=datetime.now().time().replace(second=0, microsecond=0), key="new_task_time"
    )

if st.button("Add task"):
    if not task_description.strip():
        st.warning("Give the task a description first.")
    else:
        task = Task(
            task_id=f"task-{uuid4().hex[:8]}",
            description=task_description.strip(),
            time=datetime.combine(task_date, task_time),
            frequency=task_frequency,
            duration_minutes=int(task_duration),
        )
        selected_pet.add_task(task)
        st.success(
            f"Added '{task.description}' to {selected_pet.name} "
            f"for {task.time.strftime('%b %d at %I:%M %p')}."
        )

# The button click already triggered this rerun, and this block runs after the
# mutation above, so the new task shows up without an explicit st.rerun().
st.markdown("#### Task list")

filter_col1, filter_col2 = st.columns(2)
with filter_col1:
    # "" stands in for "every pet" so the options stay plain pet_id strings.
    pet_filter = st.selectbox(
        "Filter by pet",
        options=[""] + list(pets_by_id),
        format_func=lambda pet_id: "All pets" if not pet_id else pets_by_id[pet_id].name,
        key="task_pet_filter",
    )
with filter_col2:
    status_filter = st.radio(
        "Filter by status",
        options=["All", "Pending", "Completed"],
        horizontal=True,
        key="task_status_filter",
    )

visible_tasks = owner.get_tasks_for_pet(pet_filter) if pet_filter else owner.get_all_tasks()

if status_filter != "All":
    # get_tasks_by_status spans every pet, so intersect by id to keep the pet
    # filter above applied rather than overriding it.
    wanted_ids = {task.task_id for task in owner.get_tasks_by_status(status_filter == "Completed")}
    visible_tasks = [task for task in visible_tasks if task.task_id in wanted_ids]

# Chronological rather than insertion order — the scheduler's own ordering rule.
visible_tasks = scheduler.sort_by_time(visible_tasks)

pet_name_by_task_id = {task.task_id: pet.name for pet in owner.pets for task in pet.get_tasks()}

if not visible_tasks:
    st.caption("No tasks match these filters.")
else:
    st.dataframe(
        [
            {
                "Pet": pet_name_by_task_id[task.task_id],
                "Task": task.description,
                "Minutes": task.duration_minutes,
                "Frequency": task.frequency,
                "Status": "done" if task.is_completed else "pending",
                "Scheduled for": task.time.strftime("%b %d, %I:%M %p"),
            }
            for task in visible_tasks
        ],
        use_container_width=True,
        hide_index=True,
    )

    st.caption("Completing a recurring task queues up its next occurrence.")
    # Record the click and act after the loop: complete_task appends the next
    # occurrence to the same list this loop is walking.
    completion_request: tuple[Pet, str] | None = None
    for task in visible_tasks:
        if task.is_completed:
            continue
        owning_pet = next(pet for pet in owner.pets if task in pet.get_tasks())
        if st.button(
            f"Mark '{task.description}' done ({owning_pet.name})",
            key=f"complete_{task.task_id}",
        ):
            completion_request = (owning_pet, task.task_id)

    if completion_request is not None:
        pet_to_update, task_id = completion_request
        upcoming = pet_to_update.complete_task(task_id)
        if upcoming is None:
            st.session_state.completion_note = "Marked done — this task does not recur."
        else:
            st.session_state.completion_note = (
                f"Marked done — next '{upcoming.description}' is set for "
                f"{upcoming.time.strftime('%b %d, %I:%M %p')}."
            )
        # Rerun so the table redraws with the completion and its next occurrence.
        st.rerun()

if "completion_note" in st.session_state:
    st.success(st.session_state.pop("completion_note"))

st.divider()

# ---------------------------------------------------------------------------
# Ask PawPal (AI): plan a plain-English request on a draft, then approve it.
# The agent never touches `owner`; only agent.commit(), after approval, does.
# ---------------------------------------------------------------------------

MOCK_INFO = (
    "Mock mode: recorded responses, no API calls. It only understands the six demo "
    "requests below, and they need pets named Biscuit and Whiskers."
)
EXISTING_OVERLAP = "overlap between tasks you already had; I didn't change these"


@st.cache_resource
def _ai_logging_ready() -> bool:
    """Set up the agent's log file once per process, not on every rerun."""
    agent.setup_logging()
    return True


def _ai_is_mock() -> bool:
    """The same test get_client() uses, without building a client or touching the network."""
    llm_client.load_dotenv(override=False)
    mode = os.environ.get("PAWPAL_LLM_MODE", "").strip().lower()
    return mode == "mock" or not os.environ.get("GEMINI_API_KEY", "").strip()


def _ai_demo_requests() -> list[str]:
    """The recorded requests' original text, in file order."""
    with open(llm_client.FIXTURES_PATH, encoding="utf-8") as handle:
        return [fixture["request"] for fixture in json.load(handle).values()]


def _ai_use_example(text: str) -> None:
    # A callback runs before the text area is drawn, so it may set the widget's state.
    st.session_state["ai_request"] = text


def _ai_redact(text: str) -> str:
    """Keep the API key off the page, even if an SDK error message quotes it."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    return text.replace(key, "***") if key else text


def _ai_approvable(result: agent.AgentResult) -> bool:
    """The same rule agent.commit() enforces."""
    return result.status == agent.STATUS_OK or (
        result.status == agent.STATUS_LLM_ERROR and bool(result.new_tasks)
    )


def _ai_bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _ai_show_plan(plan: dict, pet_by_task_id: dict[str, str], new_ids: set[str], budget: int) -> None:
    """One plan: a table of every due task, the minutes used, then each conflict."""
    entries = [(task, "scheduled") for task in plan["scheduled"]] + [
        (task, "deferred") for task in plan["deferred"]
    ]
    entries.sort(key=lambda entry: (entry[0].time, entry[0].task_id))
    if entries:
        st.table(
            [
                {
                    "Task": task.description,  # no refs in the UI
                    "Pet": pet_by_task_id.get(task.task_id, "?"),
                    "Start": task.time.strftime("%H:%M"),
                    "End": (task.time + timedelta(minutes=task.duration_minutes)).strftime("%H:%M"),
                    "Min": task.duration_minutes,
                    "Result": outcome,
                }
                for task, outcome in entries
            ]
        )
    used = sum(task.duration_minutes for task in plan["scheduled"])
    st.markdown(f"Budget: {used} of {budget} min used")
    for first, second in plan["conflicts"]:
        if first.task_id in new_ids or second.task_id in new_ids:
            st.warning(f"⚠️ '{first.description}' and '{second.description}' overlap.")
        else:
            st.warning(f"⚠️ '{first.description}' and '{second.description}': {EXISTING_OVERLAP}.")


def _ai_close(note: str | None = None) -> None:
    """Drop the pending result, leave a note for the next run, and rerun."""
    if note is not None:
        st.session_state.ai_note = note
    st.session_state.pop("pending_agent_result", None)
    st.rerun()


def _ai_approve(result: agent.AgentResult, use_revision: bool) -> None:
    try:
        ref_to_task_id = agent.commit(result, owner, use_revision=use_revision)
    except agent.CommitError as exc:
        # A stale result, such as a pet removed since the run: nothing was changed.
        st.session_state.ai_error = f"Couldn't apply that plan: {exc} Nothing was changed."
        _ai_close()
        return
    committed_ids = set(ref_to_task_id.values())
    added = [
        (pet.name, sum(1 for task in pet.get_tasks() if task.task_id in committed_ids))
        for pet in owner.pets
    ]
    note = "Added " + " and ".join(f"{count} task(s) to {name}" for name, count in added if count) + "."
    if result.budget is not None:
        note += f" Time available today is now {result.budget} minutes."
        st.session_state.pending_budget = result.budget
    _ai_close(note)


def _ai_show_result(result: agent.AgentResult) -> None:
    """Everything about the pending result, redrawn on every rerun, then the approval buttons."""
    if result.status == agent.STATUS_NEEDS_CLARIFICATION:
        st.warning("I need a bit more information:\n" + _ai_bullets(result.errors))
    elif result.status in (agent.STATUS_LLM_BAD_OUTPUT, agent.STATUS_LLM_ERROR):
        st.error("Something went wrong with the AI step:\n" + _ai_bullets([_ai_redact(e) for e in result.errors]))
    elif result.status == agent.STATUS_MOCK_UNKNOWN_REQUEST:
        st.warning(
            "Mock mode has no recorded response for that request. It only knows these:\n"
            + _ai_bullets(result.supported_requests)
        )

    if result.assumptions:
        st.info("I assumed:\n" + _ai_bullets(result.assumptions))
    for warning in result.warnings:
        st.warning(warning)

    if result.original_plan is not None:
        pet_name_by_id = {pet.pet_id: pet.name for pet in owner.pets}
        pet_by_task_id = {task.task_id: pet.name for pet in owner.pets for task in pet.get_tasks()}
        # In the draft, new tasks carry their refs (new-1, ...) as task_ids.
        for pet_id, ref, _task in result.new_tasks:
            pet_by_task_id[ref] = pet_name_by_id.get(pet_id, "?")
        new_ids = {ref for _pet_id, ref, _task in result.new_tasks}
        budget = result.budget if result.budget is not None else owner.available_time_minutes

        if result.revised_plan is not None:
            left, right = st.columns(2)
            with left:
                st.markdown("#### Original plan")
                _ai_show_plan(result.original_plan, pet_by_task_id, new_ids, budget)
            with right:
                st.markdown("#### Revised plan")
                _ai_show_plan(result.revised_plan, pet_by_task_id, new_ids, budget)
                descriptions = {ref: task.description for _pet_id, ref, task in result.new_tasks}
                st.markdown(
                    "Moves:\n"
                    + _ai_bullets(
                        [
                            f"{descriptions.get(move['ref'], '?')}: {move['from']} -> {move['to']} "
                            f"({move['reason']})"
                            for move in result.revision_moves
                        ]
                    )
                )
        else:
            st.markdown("#### Plan")
            _ai_show_plan(result.original_plan, pet_by_task_id, new_ids, budget)

        shown = result.revised_plan if result.revised_plan is not None else result.original_plan
        if shown["deferred"]:
            st.warning(
                "Didn't fit in the time budget:\n"
                + _ai_bullets([f"{task.description} ({task.duration_minutes} min)" for task in shown["deferred"]])
            )

    if result.stop_reason is not None:
        meaning = STOP_MEANINGS.get(result.stop_reason, "")
        st.caption(
            f"Stop reason: {result.stop_reason}"
            + (f" ({meaning})" if meaning else "")
            + f". LLM revise calls: {result.revise_calls}"
        )

    with st.expander("Agent trace"):
        st.json(json.loads(_ai_redact(json.dumps(result.trace))))

    if _ai_approvable(result):
        can_revise = result.status == agent.STATUS_OK and bool(result.revised_new_tasks)
        buttons = st.columns(3 if can_revise else 2)
        if can_revise and buttons[0].button("Approve revised plan", key="ai_approve_revised"):
            _ai_approve(result, use_revision=True)
        if buttons[-2].button("Approve original plan", key="ai_approve_original"):
            _ai_approve(result, use_revision=False)
        if buttons[-1].button("Discard", key="ai_discard"):
            agent.discard(result)
            _ai_close("Discarded: nothing was changed.")
    elif st.button("Dismiss", key="ai_dismiss"):
        agent.discard(result)
        _ai_close()


_ai_logging_ready()

st.subheader("Ask PawPal (AI)")
st.caption("Describe a single day of care in plain English; nothing changes until you approve.")

if "ai_note" in st.session_state:
    st.success(st.session_state.pop("ai_note"))
if "ai_error" in st.session_state:
    st.error(st.session_state.pop("ai_error"))

if _ai_is_mock():
    st.info(MOCK_INFO)
    with st.expander("Demo requests"):
        for index, example in enumerate(_ai_demo_requests()):
            st.markdown(f"{index + 1}. {example}")
            st.button("Use this request", key=f"ai_example_{index}", on_click=_ai_use_example, args=(example,))

ai_request = st.text_area("What do your pets need?", key="ai_request")
if st.button("Plan with AI", key="ai_plan_button"):
    if not ai_request.strip():
        st.warning("Type a request first.")
    else:
        try:
            # Fresh on every click: a MockClient keeps per-request state.
            ai_client = llm_client.get_client()
        except llm_client.LLMConfigError as exc:
            st.error(_ai_redact(str(exc)))
        else:
            with st.spinner("Planning..."):
                # Replaces any earlier pending result; that one was never committed.
                st.session_state.pending_agent_result = agent.run_agent(
                    ai_request, owner, datetime.now(), ai_client
                )

if "pending_agent_result" in st.session_state:
    _ai_show_result(st.session_state.pending_agent_result)

st.divider()

st.subheader("Build Schedule")
st.caption(
    f"Fits the most overdue tasks into {owner.available_time_minutes} minutes, most urgent first."
)

if st.button("Generate schedule", key="generate_schedule"):
    if not owner.get_all_tasks():
        st.info("Add at least one task before generating a schedule.")
    else:
        plan = scheduler.generate_plan(owner, datetime.now())

        # Conflicts render on the page directly — a real scheduling collision is
        # the one thing the owner has to see without clicking anything open.
        for first, second in plan["conflicts"]:
            st.warning(
                f"⚠️ '{first.description}' and '{second.description}' overlap "
                f"— both are scheduled at the same time."
            )

        scheduled = plan["scheduled"]
        deferred = plan["deferred"]

        booked = sum(task.duration_minutes for task in scheduled)
        st.markdown(
            f"**{len(scheduled)} scheduled**, {len(deferred)} deferred — "
            f"{booked} of {owner.available_time_minutes} minutes booked."
        )

        st.markdown("#### Today's plan")
        if scheduled:
            st.success(f"{len(scheduled)} task(s) fit today's {booked}-minute plan.")
            st.table(
                [
                    {
                        "#": position,
                        "Task": task.description,
                        "Minutes": task.duration_minutes,
                        "Frequency": task.frequency,
                        "Scheduled for": task.time.strftime("%b %d, %I:%M %p"),
                    }
                    for position, task in enumerate(scheduler.sort_by_time(scheduled), start=1)
                ]
            )
        else:
            st.warning("Nothing fit in the time available.")

        if deferred:
            st.markdown("#### Deferred")
            st.table(
                [
                    {
                        "Task": task.description,
                        "Minutes": task.duration_minutes,
                        "Frequency": task.frequency,
                        "Scheduled for": task.time.strftime("%b %d, %I:%M %p"),
                    }
                    for task in scheduler.sort_by_time(deferred)
                ]
            )

        with st.expander("Why the scheduler made these calls", expanded=True):
            for explanation in plan["explanations"]:
                st.markdown(f"- {explanation}")
