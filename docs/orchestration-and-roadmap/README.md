# Orchestration & Roadmap Guide

Archon Horizon manages complex mathematical formalization through structured orchestration, coordinating autonomous proof work via explicit tasks, multi-round collaboration loops, and persistent roadmaps. The loop itself is driven by [`orchestration/orchestrator.py`](../../src/archon_horizon/orchestration/orchestrator.py) and [`orchestration/scheduler.py`](../../src/archon_horizon/orchestration/scheduler.py).

---

## Table of Contents

- [1. Running Autoformalization Tasks](#1-running-autoformalization-tasks)
  - [Dry Runs and Resumption](#dry-runs-and-resumption)
- [2. Collaboration Rounds](#2-collaboration-rounds)
- [3. Roadmaps vs. Tasks](#3-roadmaps-vs-tasks)
  - [The Roadmap as a Project Board](#the-roadmap-as-a-project-board)
  - [Common Roadmap Commands](#common-roadmap-commands)
  - [Linking Tasks to Roadmap & Inbox](#linking-tasks-to-roadmap--inbox)
- [4. Reports, Events & Memory](#4-reports-events--memory)

---

## 1. Running Autoformalization Tasks

The core execution command in Archon Horizon is `horizon run` (see [`commands/run.py`](../../src/archon_horizon/commands/run.py)). It dispatches the underlying agent engine against specified targets within the workspace:

# Each target resolves in order **task id → roadmap item id → project/file**:

```bash
# Run a specific human-created task
horizon run my_formalization_task

# Launch a roadmap milestone (a task is inferred from it on demand)
horizon run thm_main_result

# Ad-hoc task scoped to a project (or a file)
horizon run project_a

# Ad-hoc task across multiple projects simultaneously
horizon run project_a project_b

# Run every queued task in the workspace
horizon run '*'
```

### Adaptive supervision

Start `horizon run` for the whole workspace, or `horizon run --supervisor project_a` for one member. Give it a project goal with `horizon task add`. A first worker can prepare a missing blueprint and file-scoped assignments. The supervisor reads the current task queue, roadmap prerequisites, and the blueprint dependency graph. It prioritizes ready proofs that unlock downstream work and refreshes its plan when a worker finishes. A linked, sorry-free result awaiting blueprint confirmation gets a correspondence check.

Use repeatable `--file` and `--blueprint-node` options on `horizon roadmap add` or `set` to record a milestone’s write scope. The assigned run records its source, file and node scope, and the CPU and memory sample used for admission.

Each assignment has its own numbered run, agent process, transcript and project commits. Disjoint files can be worked on together. A task without precise files claims its project. `horizon ps` lists the workers, and the Dashboard overview shows assignments, CPU utilization, available memory and the last sample time.

The supervisor samples the host, its CPU affinity and visible Linux cgroup v2 limits. It starts with a conservative budget, then measures each worker's whole process tree. Sustained low CPU demand while agents wait for model replies allows additional independent proofs to start. Recent CPU demand, memory peaks and falling headroom limit subsequent launches. Write conflicts, dependencies and delegation limits continue to govern which proofs can run.

Foreground Lean/Lake jobs have a separate workspace-wide queue. Workers get `lake` and `lean` on PATH and absolute `HORIZON_LAKE` and `HORIZON_LEAN` paths for shells which replace PATH. A shell or external tool that calls its own compiler by absolute path can bypass the software queue. The optional kernel limits apply to all descendants in the worker group. External resource pressure can keep a build queued until capacity returns or the caller cancels. Queue time is recorded separately from timed Lean delivery checks. See [resource configuration](../configuration/README.md#kernel-limits-for-supervised-jobs-on-linux).

The Dashboard shows active runs, running and waiting compiles, and the resource control mode. Per-run `resources.json` records CPU time and observed peak RSS. Sampled RSS sums live processes and can count shared pages more than once. To compare scheduling policies, run `python scripts/benchmark_supervisor.py` with the same active Lean toolchain, CPU affinity, node count and model delay. It uses a disposable blueprint, a synthetic model wait and real Lean verification, and reports wall time through final delivery.

`--rounds` bounds attempts per task during this invocation. Individual runs can be resumed by ID. A service or authentication limit stops further dispatch.

### Final quality review

The supervisor waits for the proof tasks, roadmap prerequisites, blueprint nodes and active workers in its member scope. It then opens a dedicated review task with a fresh engine conversation. The reviewer reads the paper, original objectives, blueprint, public Lean declarations and representative consumers, repairs issues and commits to the owning member. If verification sends work back, the same review task continues, resuming its own conversation when the engine supports it.

The review covers topic-oriented modules, public imports, namespaces, sections and variable scope, canonical instances and downstream inference, proof simplification, and mathematical documentation. Follow the mathlib [documentation](https://mathlib.org/contribute/doc.html), [naming](https://mathlib.org/contribute/naming.html), [style](https://mathlib.org/contribute/style.html) and [commit title and body](https://mathlib.org/contribute/commit.html) conventions. Store session history in the run log. Agent commits are checked using session-scoped Git hooks that delegate to the member’s existing hooks. Preserve both `maxHeartbeats` and `synthInstance.maxHeartbeats` in source, Lake options and compiler invocations.

Design public declarations around a representative caller. Use a predicate for a property of a particular object, explicit data for choices the caller compares, and a class for structure or laws the caller should recover from the context. When an API adds or changes inference or notation, commit an example or existing caller that imports the public module and exercises the promised syntax with its ordinary hypotheses. The review checks that Lean supplies the intended instance and that the notation denotes the intended operation.

Repeated local setup in an exported theorem statement requires an API refactor. Trace the repeated construction to its defining module, preserve the theorem's hypotheses and conclusion, and check its callers. Horizon examines candidate files with the member's own Lean toolchain and Lake configuration. It reports the declaration and line for a nested chain of three or more `let`/`letI` bindings in a theorem or lemma signature, or a chain containing at least two `letI` bindings. Independent subterms and proof-body bindings are outside the count. Reviewers also inspect repeated setup across shorter statements and across callers. An Inbox reply can guide how to refactor an inherited statement. An unresolved finding blocks delivery.

Horizon captures member source baselines before proof work. It opens Inbox conversations for inherited findings. Reviewers can ask about inherited design issues using `horizon quality ask -p NAME --file PATH --reason TEXT`. Reply in the Dashboard Inbox or with `horizon inbox comment I-0001 --body "..."`, then rerun the supervisor. A reply supplies repair instructions. To explicitly retain the listed pre-existing style or heartbeat findings for a file, enter `keep PATH` on its own line in that thread. The report lists retained findings separately. Proof holes and compilation times above the member limit still block delivery. Existing protected heartbeat settings can be changed by the project owner.

Use `horizon quality check -p NAME --json` to see per-file timings and findings. Final acceptance checks the committed member revision, builds the project and serially elaborates every authored Lean file with its imports prepared. The timed invocation uses `lake lean FILE -- -o TEMP.olean` and includes Lake startup. Installed dependencies under `.lake` are excluded. The Dashboard distinguishes pending proofs, quality review, waiting for a user reply and delivery. Individual focused/manual runs remain useful for development; start `horizon run --supervisor NAME` to deliver the whole member.

### Dry Runs and Resumption
- **Dry Run**: Preview the execution plan, target write sets, and harness configuration without launching models:
  ```bash
  horizon run my_task --dry-run
  ```
- **Resume**: Continue an interrupted or paused execution run using its run ID:
  ```bash
  horizon run --resume <run_id>     # or --resume latest
  ```

### Interactive runs

Add `--backend interactive` to hand the terminal straight to the Horizon engine — headless streaming is replaced by a live TTY, so you can type follow-up prompts and steer the agent yourself:
```bash
horizon run horizon --backend interactive
```

For a conversational, human-facing session — explain status, summarize recent runs, and make guided edits to projects/tasks/inbox/roadmap on request — use [`horizon discuss`](../../src/archon_horizon/commands/discuss.py).

Run lifecycle and resumption state are tracked in [`core/sessions.py`](../../src/archon_horizon/core/sessions.py); concurrency safety across projects is handled by [`core/locks.py`](../../src/archon_horizon/core/locks.py).

---

## 2. Collaboration Rounds

When a run is initiated, Horizon executes a structured loop of proof sessions;
the lead agent may add a fresh-context helper when the scope benefits from one
(implemented in [`orchestration/orchestrator.py`](../../src/archon_horizon/orchestration/orchestrator.py)):

```
[Target Task / Focus]
         │
         ▼
┌────────────────────────────────────────────────────────┐
│ Collaboration Round (Repeated up to configured rounds) │
│                                                        │
│  1. Horizon reads task, graph, roadmap, and inbox      │
│  2. Horizon writes Lean and validates the target       │
│  3. Horizon may choose a scoped review/helper          │
│  4. Horizon reconciles findings and records progress   │
└────────────────────────────────────────────────────────┘
         │
         ▼
[Final Run Report & Artifacts Emitted]
```

---

## 3. Roadmaps vs. Tasks

Horizon keeps a sharp distinction between the human's tasks and the agent-maintained roadmap:

- **Tasks (`horizon task`)**: the human's lever for launching sessions — objectives with project scope, write-set, and target files. Open to agents and humans alike: both may `add`, `set` (including status and roadmap/inbox refs), `comment`, and `remove`. The machine (scheduler/orchestrator) only ever writes `queued`/`running`; every terminal status (`done`/`blocked`/`failed`) is the agent's own word, and an agent declares `done` only when the work is *fully* complete. Modeled in [`core/tasks.py`](../../src/archon_horizon/core/tasks.py), commands in [`commands/task.py`](../../src/archon_horizon/commands/task.py).
- **Roadmap (`horizon roadmap`)**: the project's **mathematical status** — the main theorems and infrastructure formalized and still to build. Horizon maintains it against the real Lean/blueprint state, and Ground may audit it from fresh context when the lead agent chooses that perspective. It is a map that guides the work, **not** a work queue. During supervised execution, ready milestones can be materialized as tasks. A human launches a milestone with `horizon run <roadmap-id>`, which **infers** a task from the item's scope on demand. Modeled in [`core/roadmap.py`](../../src/archon_horizon/core/roadmap.py), commands in [`commands/roadmap.py`](../../src/archon_horizon/commands/roadmap.py).

### The Roadmap as a Project Board

Beyond its status and hierarchy, each roadmap item carries lightweight board metadata (backed by [`core/roadmap.py`](../../src/archon_horizon/core/roadmap.py) — `item_owner`/`item_milestone`/`item_pinned_commits`, folded in by `_apply_board_meta` in [`commands/roadmap.py`](../../src/archon_horizon/commands/roadmap.py)):

- **`owner`** — the team or agent responsible for the item, a categorization aid rather than an assignment lock. Who is *currently* running an item is **derived live** from running tasks' `roadmap_refs`, never stored on the item itself.
- **`milestone`** — a free string label for grouping and filtering. It is **not** a due date and carries no schedule; it simply buckets related items together.
- **`pinned_commits`** — commit SHAs pinned to the item as concrete deliverables (newest first).

Set these when adding or editing an item:

```bash
# Add an item already scoped to an owner and milestone
horizon roadmap add --id A.4 --title "..." --owner algebra-team --milestone "phase-1"

# Edit board fields on an existing item; pin/unpin deliverable commits
horizon roadmap set A.4 --owner algebra-team --milestone "phase-1" \
    --pin-commit <sha> --unpin-commit <sha>

# Filter the outline by milestone or owner
horizon roadmap list --milestone "phase-1"
horizon roadmap list --owner algebra-team
```

The same board also surfaces in the web dashboard as a milestone-grouped [`/board` view](../dashboard-and-search/README.md), where items with a live running task pulse as "live".

### Common Roadmap Commands

| Command | Description |
| :--- | :--- |
| `horizon roadmap list` | List roadmap items as an indented outline; filter with `--milestone <label>` / `--owner <t>`. |
| `horizon roadmap show <id>` | Inspect a specific roadmap item (fields, hierarchy, board metadata, subtree progress). |
| `horizon roadmap add --id <id> --title <t>` | Add an item; `--project` defaults from the session's `ARCHON_HORIZON_PROJECTS` when omitted. Optional `--parent`, `--depends-on`, `--owner`, `--milestone`. |
| `horizon roadmap set <id> …` | Update any field: status/title/summary/kind/priority, nest with `--parent`/`--depth`, board metadata, `--project`, `--depends-on`, `--inbox-ref`/`--task-ref`, `--pin-commit`/`--unpin-commit`. Empty string clears optional fields. |
| `horizon roadmap rename <old> <new>` | Rename an item id and rewrite parent/depends-on links that pointed at it. |
| `horizon roadmap remove <id> [--cascade]` | Delete an item; children un-nest by default, or delete with `--cascade`. |
| `horizon roadmap comment <id> --body …` | Add a concise progress comment on a milestone. |

Agents are expected to **build and reshape** this outline as strategy, not only flip status on pre-seeded rows: an empty roadmap on a multi-session formalization task is unfinished orientation. The same applies to the **blueprint**: a missing or stub chapter is unfinished orientation — write a complete mathematical route and keep `.tex` aligned with Lean as the route changes (see [`blueprint-conventions`](../../src/archon_horizon/skills/blueprint-conventions/SKILL.md)).

### Linking Tasks to Roadmap & Inbox

A task records which roadmap items and inbox items it advances via its `roadmap_refs`/`inbox_refs`. Set or repair these through the safe CLI (see [`commands/task.py`](../../src/archon_horizon/commands/task.py) — `set_task`):

```bash
horizon task set <id> --roadmap-ref <rid> --inbox-ref <iid>   # both repeatable
```

Both flags are repeatable and replace the existing refs. These links are what let the board derive its "live" status and what drives roadmap status-sync when a linked task reaches a terminal state. Like `roadmap add`, `horizon task add` defaults `--project` from the session's `ARCHON_HORIZON_PROJECTS` when omitted (`provenance_project` in [`commands/shared.py`](../../src/archon_horizon/commands/shared.py)).

---

## 4. Reports, Events & Memory

Every execution round produces persistent structured traces stored under `.archon-horizon/` (emitted via [`core/events.py`](../../src/archon_horizon/core/events.py) and persisted through the [`store/`](../../src/archon_horizon/store) layer):
- **Reports**: YAML/JSON execution reports summarizing build outputs, proof status, and tool invocations.
- **Run Logs**: Complete step-by-step audit trails of harness interactions.
- **Memory**: Persistent workspace memory items used across runs to retain lemma precedents, architectural conventions, and proven strategies.
