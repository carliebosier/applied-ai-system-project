# PawPal+ Project Reflection

## 1. System Design
**Three Core Actions**
1. Add and manage a pet profile — The owner can register a pet (name, species, breed, age, weight, dietary/medical notes) so the system knows who it's caring for and can tailor task recommendations accordingly.
2. Schedule and track recurring care tasks — The owner can create tasks (feedings, walks, medications, grooming, enrichment) with a frequency, duration, and priority, so the system knows what needs to happen and how often.
3. Generate and view today's prioritized plan — The owner can ask the system to produce a daily schedule that fits available time, respects task priority/urgency (e.g., medication before optional enrichment), and explains why it ordered things that way.

**Classes**
- Pet
    - Attributes: pet_id, name, species, breed, age, weight, dietary_notes, medical_notes
    - Methods: add_task(task), get_tasks(), update_profile(**kwargs)
- Task
    - Attributes: task_id, name, category (walk/feed/med/groom/enrichment), duration_minutes, priority (e.g. enum: LOW/MEDIUM/HIGH/CRITICAL), frequency (daily, every N hours, specific days), preferred_time_window, is_completed
    - Methods: is_due(current_time), mark_complete(), get_priority_score()
- Owner
    - Attributes: owner_id, name, pets (list of Pet), preferences (e.g. preferred wake time, available time blocks, "no walks after 9pm")
    - Methods: add_pet(pet), set_preferences(**kwargs), get_all_tasks_today()
- Scheduler
    - Attributes: available_time_minutes, tasks (all due tasks across pets)
    - Methods: generate_plan(tasks, available_time), _sort_by_priority(), _explain_decision(task)


**a. Initial design**

- Briefly describe your initial UML design.
    My initial design (shown in the Classes section above) had the same four class names — Pet, Task, Owner, Scheduler — but different fields than what I ended up with. Task originally had a `category`, a `priority` enum, and a `preferred_time_window`; Owner had a `preferences` object instead of a plain time budget; and Scheduler was meant to call `_explain_decision()` per task rather than building a list of explanation strings. None of that survived into the final version — I dropped priority and category in favor of judging urgency by how overdue a task was, and simplified Scheduler's output into the dictionary shape it has now.


- What classes did you include, and what responsibilities did you assign to each?
    - Pet — held a pet's profile (species, breed, age, weight, dietary/medical notes) plus its tasks; could add/get tasks and update its own profile.
    - Task — represented a care item with a category, priority, frequency, and a preferred time window; could report whether it was due, mark itself complete, and score its own priority.
    - Owner — held pets and a preferences object (wake time, sleep time, available minutes); could add pets, update preferences, and pull today's tasks.
    - Scheduler — took a time budget and a task list, sorted by priority, and explained each individual scheduling decision.

**b. Design changes**

- Did your design change during implementation? Yes
- If yes, describe at least one change and why you made it.
    - My first draft added two extra classes beyond the original four: `ScheduledTask` (to wrap a `Task` with a specific start/end time and its own completion flag) and `Recurrence` (to break frequency into `interval_hours` and `specific_days`). I added these after realizing that tracking completion directly on a recurring `Task` was technically wrong. A "daily walk" shouldn't itself be permanently marked "completed," since that status needs to reset each day. After review, I removed both extra classes and instead added a single `is_completed`/timestamp field directly on `Task`, keeping completion tracking self-contained.
    - My second draft also introduced `Preferences` (a class for wake/sleep time and available minutes) and `DailyPlan` (a class to hold scheduled tasks, deferred tasks, and explanations) as separate objects owned by `Owner` and produced by `Scheduler`. After confirming the assignment scope only called for four classes total, I put both into the existing structure instead of keeping them separate. `available_time_minutes` moved directly onto `Owner` as an attribute, and `Scheduler.generate_plan()` now returns a simple dictionary (with `scheduled`, `deferred`, and `explanations` keys) instead of a dedicated `DailyPlan` object. This kept the design at exactly four classes as required, at the cost of `Scheduler`'s return value being a plain dict rather than a typed object. 
---

## 2. Scheduling Logic and Tradeoffs

**a. Constraints and priorities**

- What constraints does your scheduler consider (for example: time, priority, preferences)?
    - There are three main things my scheduler cares about. First is the time budget. `_fits()` checks if a task's `duration_minutes` fits into whatever `available_time_minutes` is left, and once that runs out, everything else gets pushed to the deferred list instead of scheduled. Second is urgency. I don't have a separate priority field on `Task`, so instead `_sort_by_priority()` just ranks tasks by how overdue they are. Anything never completed goes first, then whichever has been waiting the longest. Third is conflicts. `find_conflicts()` checks if any two due tasks overlap in time, like a task running past its duration into when another one is supposed to start, and it flags that as a warning without actually changing the schedule.

- How did you decide which constraints mattered most?
    - I started with Time and Urgency, since without them the app can't really do its one job, which is telling you what to do today and in what order. Conflict detection came later, once I realized that even if two tasks both individually fit the time budget, they could still be scheduled at the exact same time, which obviously doesn't work in real life. I kept conflict detection as just a warning instead of having it auto-fix the schedule, because deciding which task "wins" a conflict felt like something the owner should decide, not something I wanted the scheduler quietly deciding for them.


**b. Tradeoffs**

- Describe one tradeoff your scheduler makes.
    - `find_conflicts()` only counts two tasks as conflicting if one's duration actually bleeds into the next one's start time, so it uses `>` instead of `>=`. That means back-to-back tasks, where one ends right as the next starts, aren't flagged as a conflict, which is what I want. But it also means a task with `duration_minutes=0` can technically never conflict with anything, even something at the exact same timestamp, since a zero-length task "ends" the same instant it starts.
- Why is that tradeoff reasonable for this scenario?
    - Allowing back-to-back tasks matters way more than catching a zero-duration edge case. If a walk ends at 8:00 and breakfast starts at 8:00, that's just a normal morning, not a conflict, and I didn't want the scheduler flagging that as a problem. Every real task in this app takes some actual time anyway (walks, feedings, meds all have a duration), so the zero-duration case basically never comes up in practice. I decided it wasn't worth adding special-case logic to catch something that isn't really a realistic scenario for this app.

---

## 3. AI Collaboration

**a. How you used AI**

- How did you use AI tools during this project (for example: design brainstorming, debugging, refactoring)?
    - I used AI throughout the project as both a code generator and development assistant. I used it to generate initial code, add and modify methods, help implement scheduler behavior, write and update tests, troubleshoot bugs, connect the Streamlit UI to the existing classes, and improve the README and documentation. I also used it to reason through design decisions and compare the implementation against the UML.
    - One of the most useful things was giving AI specific pieces of my project and asking it to make targeted changes rather than asking it to build the entire system without direction. For example, I used it to work through sorting, recurrence, filtering, and conflict detection, and then checked the generated code against what the project actually required. I also compared my original UML with the finished pawpal_system.py and used AI to identify methods and relationships that needed to be reflected in the final diagram.

- What kinds of prompts or questions were most helpful?
    - Specific, task-focused prompts were much more useful than vague ones. Asking AI to implement a particular method, debug a specific failing test, compare the UML to the code, or make a targeted UI change gave me something concrete to review. I found that AI was very effective at generating and modifying code quickly, but I still needed to understand what it generated and verify that it matched the project's requirements.

**b. Judgment and verification**

- Describe one moment where you did not accept an AI suggestion as-is.
   - One example was the possibility of adding extra classes such as ScheduledTask, Recurrence, Preferences, or DailyPlan. AI could make those designs work, but I chose not to keep adding classes because the assignment was intended to have four core classes and I wanted to keep the architecture manageable. Instead, I kept the necessary behavior inside Task, Owner, and Scheduler where it fit the existing design.
    - I also did not automatically trust generated explanations or documentation. For example, when a draft described _sort_by_priority() as ordering tasks by how overdue they were, I checked the actual implementation and found that its sort key was (is_completed, time, duration_minutes, task_id). I changed the documentation to describe what the code actually did.

- How did you evaluate or verify what the AI suggested?
    - I verified generated code by reading it, comparing it against the assignment requirements and my UML, running the test suite, and manually testing the Streamlit application. This caught issues that AI-generated code did not automatically prevent, including a recurrence bug and a mistake in one of my own tests. I also found UI issues during manual testing that were not obvious from the passing tests.
    - AI generated a significant amount of the project's code, but it did not replace my responsibility for deciding what belonged in the system. I had to review the generated code, test it, reject changes that did not fit the architecture, and make sure the final implementation matched the design I was responsible for.

---

## 4. Testing and Verification

**a. What you tested**

- What behaviors did you test?
    - The suite is 15 tests, and I aimed them at the four behaviors the whole app depends on: chronological sorting through `sort_by_time()`, filtering through `get_tasks_for_pet()` and `get_tasks_by_status()`, recurrence through `next_occurrence()` and `complete_task()`, and conflict detection through `find_conflicts()`. For conflicts I specifically tested non-adjacent overlaps. A long task colliding with one two slots later, not just the very next one because that's the case a naive implementation quietly misses.

- Why were these tests important?
    - Because they caught two real problems. The bigger one was a recurrence bug: completing a task could make its next occurrence show up as due immediately instead of the following day, since a pending task was being treated as due no matter what its scheduled time was. `is_due()` now checks that the scheduled time has actually arrived before calling a pending task due. I don't think I would have found that by clicking around the app, because it only shows up right after you complete something. The second was a bug in one of my own tests, where I mixed up two task IDs and was asserting against the wrong object. 

**b. Confidence**

- How confident are you that your scheduler works correctly?
    - 4/5. All 15 tests pass, total coverage is 95%, and `pawpal_system.py` sits at 89%. The behaviors I actually care about are covered, and the two bugs I found are fixed. I also went back and checked the AI-generated changes instead of assuming that passing tests meant everything was automatically correct. But roughly 11% of `pawpal_system.py` isn't covered yet, and having written a broken test myself, I'm not willing to treat a green suite as proof the logic is airtight. It proves the paths I thought to check behave the way I expected, which is not quite the same claim.

- What edge cases would you test next if you had more time?
    - A zero-duration task against the conflict boundary, since `_overlaps()` uses `>` and a zero-length task can never overlap anything. Frequencies that aren't "daily" or "weekly," which currently fall through to being treated as always due. `complete_task()` called with a `task_id` that doesn't exist on that pet. An owner with `available_time_minutes` set to 0, where everything should defer. And completing the same recurring task several times in a row, to make sure `_next_task_id()` keeps incrementing cleanly instead of generating IDs that collide.

---

## 5. Reflection

**a. What went well**

- What part of this project are you most satisfied with?
    - Honestly, working in separate phases is what saved this project. Doing architecture and UML first, then implementation, then testing, then the Streamlit integration, then final polish meant that when something broke I usually knew which layer it belonged to instead of hunting across the whole thing. It also kept me from treating my first design as final. When I compared my original UML against the finished code, there were eleven things that needed correcting, and I wrote a separate `uml_final.mmd` rather than pretending the first draft had been right all along.
    - The phase split also exposed a gap I don't think I'd have caught otherwise. `pawpal_system.py` had sorting, filtering, recurrence, and conflict detection all working and tested, while `app.py` was still on a version that surfaced none of it. The logic was fine, the app just wasn't showing any of it. Treating UI integration as its own phase is what made that visible instead of letting me assume "the tests pass, so the app works."

**b. What you would improve**

- If you had another iteration, what would you improve or redesign?
    - I'd revisit `generate_plan()` returning a plain dictionary. It works, and I chose it deliberately to stay within four classes, but the return shape basically only exists in my head and in the code that reads it, nothing enforces that `"conflicts"` is a list of pairs, and `app.py` has to just know that. I'd also want to close some of that uncovered 11%, and I'd look harder at the frequency handling, since anything that isn't "daily" or "weekly" currently falls through to always-due, which is a simplification rather than real behavior.

**c. Key takeaway**

- What is one important thing you learned about designing systems or working with AI on this project?
    - That being the lead architect is mostly about deciding what the system should be and then actually checking whether what got built matches that decision, across the code, the tests, the UI, the UML, and the documentation. Those five things drift apart on their own if nobody keeps them honest, and every place they drifted in this project, it was because I'd assumed one of them was still true instead of verifying it. AI made the actual implementation and checking dramatically faster. It generated a lot of the code, helped me debug it, and helped me catch places where my different project pieces didn't match. What it couldn't do was tell me that PawPal+ should have four classes instead of eight, or that a scheduling conflict deserves a warning rather than a silent fix. Those were mine, and keeping them mine is the part I'd want to carry into the next project.
