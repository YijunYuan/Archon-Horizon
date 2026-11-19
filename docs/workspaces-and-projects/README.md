# Workspaces & Projects Guide

Archon Horizon treats the **workspace** as the primary unit of management and collaboration. A single workspace encapsulates configuration, multi-project version control tracking, permissions, and metadata. The workspace model lives in [`core/workspace.py`](../../src/archon_horizon/core/workspace.py), and its configuration schema in [`config/schema.py`](../../src/archon_horizon/config/schema.py).

---

## Table of Contents

- [1. Workspace Directory Layout](#1-workspace-directory-layout)
- [2. Initializing a Workspace](#2-initializing-a-workspace)
- [3. Managing Projects](#3-managing-projects)
  - [Common Project Commands](#common-project-commands)
- [4. Permissions & Freeze Enforcement](#4-permissions--freeze-enforcement)

---

## 1. Workspace Directory Layout

When you initialize an Archon Horizon workspace, the framework scaffolds a central configuration file and a hidden state directory:

```
my-horizon-workspace/
├── config.yaml                   # Global workspace defaults, models, and harness settings
├── project_a/                    # Embedded Lean 4 project codebase
├── project_b/                    # Another member project
└── .archon-horizon/              # Managed state and orchestration data
    ├── version                   # Stamped Horizon tool version
    ├── vcs/                      # Legacy workspace Git history (when present)
    ├── roadmap/                  # Milestones and task breakdowns
    ├── inboxes/                  # Local filesystem inbox items
    ├── reports/                  # Structured agent execution reports
    ├── tmp/                      # Disposable per-session scratch (excluded from the ledger)
    └── search/                   # Cached offline declaration search indices
```

`config.yaml` is parsed and validated by [`config/loader.py`](../../src/archon_horizon/config/loader.py) against the schema in [`config/schema.py`](../../src/archon_horizon/config/schema.py).

---

## 2. Initializing a Workspace

To create a new workspace or scaffold Horizon within an existing directory (see [`commands/init.py`](../../src/archon_horizon/commands/init.py)):

```bash
horizon init --root ./my-workspace
```

During initialization, Horizon will prompt you to set default engine parameters in `config.yaml`, add initial projects, and optionally spawn an agent to review your setup and assist with project structure.

> [!NOTE]
> You can run `horizon init` non-interactively or invoke `horizon init --update` inside an existing workspace to refresh managed files (such as subagents, skills, and MCP configurations) after upgrading the CLI.

Agent subprocesses receive `TMPDIR`, `TMP`, and `TEMP` pointing to an isolated
directory below `.archon-horizon/tmp/<run>/<session>`, exposed as
`ARCHON_HORIZON_TMP`. It is disposable and normally removed after the session;
keep evidence in the run artifacts instead. Use `horizon tmp clean` to preview
stale leftovers and add `--apply` only after checking that live run directories
are protected.

---

## 3. Managing Projects

Each member is an independent Lean package and Git checkout. Register one with
`horizon project add Algebra projects/Algebra`. An existing clone, submodule
checkout, or linked worktree keeps its Git directory or gitfile, current branch,
remotes, hooks, and staging area. Horizon initializes an empty Git repository
inside a member that has none. Registration and run boundaries leave local edits
and the index in place. Initializing the workspace leaves its root outside Git
unless you already keep a repository there.

### Commits and the dashboard

Agents commit in each member directory with `"$HORIZON_GIT" commit -m "…" -- <files>`.
From the workspace root they can use `"$HORIZON_GIT" -C projects/Algebra …`.
The live dashboard's **Logs → session → Commits** shows the project name, message,
SHA and file diffs as work proceeds. Project history includes human commits.
Workspace tasks, reports and session-to-commit references live in `.archon-horizon/`.

For a 0.1.5 workspace, run `horizon init --update` after installing the new code.
When a registered project has a valid `.git.disabled/` from that release and
no `.git`, Horizon restores it. The `.git-disabled/` spelling is also recognized. An existing workspace-root repository and the
old ledger keep their history. Archived ledger commits remain readable in Logs.
Restoring a project checkout leaves edits made since it was disabled in its working tree.

### Lake packages

Each member owns its `lakefile.toml` or `lakefile.lean`, `lean-toolchain`,
`lake-manifest.json`, dependencies under `.lake/packages/`, and compiled files under
`.lake/build/`. Run `horizon check --project Algebra` from the workspace root.
For `horizon check --lean projects/Algebra/Foo.lean`, the file selects the member.
Checks on the same project share a lock. Different projects can build independently. Workspace search reads each member’s local
package checkout. When two members have their own mathlib, use a filter such
as `horizon search --lib mathlib@Algebra --name NAME` to select a copy.

### Common Project Commands

| Command | Description |
| :--- | :--- |
| `horizon project add <name>` | Register a new or existing Lean project into the workspace. |
| `horizon project list` | Display all configured projects along with their status and paths. |
| `horizon sync` | Import inbox provider shadows (e.g. GitHub issues) into the local inbox. |

---

## 4. Permissions & Freeze Enforcement

Before dispatching autonomous formalization tasks to the Horizon agent, the workspace enforces **deterministic freeze checks** and write-set locks (see [`core/freeze.py`](../../src/archon_horizon/core/freeze.py) and [`core/permissions.py`](../../src/archon_horizon/core/permissions.py)):
- **Write Sets**: Tasks explicitly define which projects or files the agent is authorized to modify.
- **Pre-Dispatch Freeze**: Horizon verifies clean Git boundaries and ensures that unapproved modifications do not leak across project seams during execution rounds.

Standing protections that soft-freeze specific declarations are covered in the [Inboxes & Communication guide](../inboxes-and-communication/README.md).
