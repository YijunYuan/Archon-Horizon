---
name: project-git
description: Commit verified changes in the owning project's Git checkout and inspect session progress.
---

## Recording a proof

Each member owns its Git repository, current branch, staging area, remotes, and hooks. Start in the member directory. From the workspace root, select the member with `"$HORIZON_GIT" -C <project-directory>`. Record changes in each project separately when a task spans projects.

Inspect `git status --short` and `git diff --cached` before editing. Leave pre-existing changes staged or unstaged as you found them. For a verified change, stage any new files you authored and name the intended files on the commit itself. If a file contains a contributor's uncommitted changes, separate your own hunks before committing.

```bash
"$HORIZON_GIT" status --short
"$HORIZON_GIT" add -- Chapter/NewLemma.lean
"$HORIZON_GIT" commit -m "feat(Topology): prove the compactness lemma" \
  --trailer 'Summary=Discharged the compactness step used by the next chapter.' \
  -- Chapter/NewLemma.lean
"$HORIZON_GIT" show --stat HEAD
```

Use the mathlib PR title and description conventions for every agent commit (https://mathlib.org/contribute/commit.html). Describe the mathematical change. Put run history in the session report.

The session wrapper adds run, session, role, and task trailers. The live dashboard reads each commit and its parent from this checkout. Independently verified changes can appear as separate checkpoints during a long run. A failed or interrupted attempt can be saved with `horizon attempt save` in the session artifacts.

Use ordinary `git log`, `git show`, and `git diff` in the project to inspect its history. Human commits are visible in project history. The workspace stores coordination records under `.archon-horizon/`. Existing ledgers from earlier Horizon versions supply their historical session views.
