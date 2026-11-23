"""The one automated-session prompt: a task directive plus "load the skill".

Archon Horizon pushes no role prose, policy, or workspace state through the
prompt. The `horizon` skill (installed at ``.claude/skills/horizon/SKILL.md``,
user-editable) is the contract — orientation, conventions, one-shot discipline,
report shape — and everything else (roadmap, inbox, memory, DAG) the agent
*pulls* on demand through the ``horizon`` CLI. The orchestrator reconstructs
what changed by reading state back from disk, never by parsing the agent's
text, so this composer stays a pure data-to-string transform any engine can
consume.
"""

from __future__ import annotations

from .base import HorizonContext


_PUBLIC_LEAN_API = (
    "Choose def, structure or class by mathematical use. Refactor let/letI setup chains in statements "
    "and preserve their meaning. For new or changed inference or notation APIs, compile and commit a "
    "consumer using only the public import, without supplying the instance under test. "
)


def _task_block(context: HorizonContext) -> str:
    task = context.task
    workspace = context.workspace
    names = task.projects or ((task.project,) if task.project else ())
    if names:
        projects = "\n".join(f"- {n} at {workspace.project_path(n)}" for n in names)
    else:
        projects = "- (workspace-wide — no single project)"
    refs = []
    if task.roadmap_refs:
        refs.append("roadmap=" + ",".join(task.roadmap_refs))
    if task.inbox_refs:
        refs.append("inbox=" + ",".join(task.inbox_refs))
    ref_line = f"\nReferences: {'; '.join(refs)}" if refs else ""
    title = task.title or task.id
    body = (task.explanation or task.objective or "").strip()
    body_block = f"\n\n{body}" if body else ""
    if context.run.metadata.get("supervised"):
        files = ", ".join(task.write_set.files)
        bounds = f"Edit only these files: {files}." if files else "Keep edits within the assigned project scope."
        body_block += (f"\n\n{bounds} Other runs may be editing this checkout. "
                       "Commit only your own files with `git commit --only -- <paths>` via $HORIZON_GIT. "
                       "Record any work outside your assignment as a follow-up.")
    return (
        f"Task: {task.id}\n"
        f"Title: {title}\n"
        f"Project(s) — work across all of them:\n{projects}{ref_line}{body_block}"
    )


def _stale_skill_guard(context: HorizonContext) -> str:
    """Supply current minimum guidance when an old editable skill is installed.

    Horizon deliberately does not overwrite workspace skill edits at run start.
    This small package-owned fallback prevents an old copy from silently losing
    critical LSP and collection-hygiene behavior while leaving ordinary helper
    dispatch to the agent's judgement.
    """
    try:
        from archon_horizon.skills.registry import stale_skills

        names = stale_skills(context.workspace.root)
    except Exception:
        return ""
    if not names:
        return ""
    # Keep the fallback prompt bounded when a workspace has many newly bundled
    # skills; `horizon skills list` gives the complete inventory.
    shown = names[:3]
    if len(names) > len(shown):
        shown.append(f"+{len(names) - len(shown)} more")
    return (
        "\n\nThe installed workspace skills are stale ("
        + ", ".join(shown)
        + "). Reconcile it without overwriting deliberate edits. Until then, use "
        "`lean-check` before and after Lean edits, consider a bounded helper for "
        "multi-file work, and use `janitor` (`ground` fallback) for health warnings."
    )


def horizon_task_prompt(context: HorizonContext) -> str:
    """Prompt for one automated Horizon session: directive + skill pointer."""
    if context.task.metadata.get("supervisor_source") == "quality":
        return (
            "You own the final independent quality review and repairs. Begin from the paper, original goals, "
            "blueprint, source and public consumers. Prior solver transcripts are unnecessary. Read current "
            "protections and human replies. Compare inherited issues against the quality baseline before editing. "
            "Follow https://mathlib.org/contribute/doc.html, https://mathlib.org/contribute/naming.html, "
            "https://mathlib.org/contribute/style.html and https://mathlib.org/contribute/commit.html. "
            "Inspect topic directories, module headers/import visibility, namespaces, sections and variable scope. "
            f"{_PUBLIC_LEAN_API}"
            "Check inferInstance or the intended notation as applicable. Trace repeated setup to "
            "its defining API and reject extra hypotheses or wrappers that merely conceal that setup. "
            "Simplify redundant proofs (including simpa using where appropriate), keep mathematical docs and remove "
            "session history from Lean. Preserve theorem strength and verify all downstream consumers. "
            "Never change maxHeartbeats or synthInstance.maxHeartbeats or the delivery time limit. "
            "Respect frozen files and existing user edits. Commit verified repairs with $HORIZON_GIT. "
            "Record your conclusions and evidence, and set your own task status explicitly.\n\n" + _task_block(context)
        )
    skill_path = (
        context.workspace.root / ".claude" / "skills" / "horizon" / "SKILL.md"
    ).resolve()
    return (
        f"You are in an **Archon Horizon** workspace at `{context.workspace.root}`.\n\n"
        "First load the **`horizon`** skill at its absolute path\n"
        f"`{skill_path}` (also `$ARCHON_HORIZON_SKILL`). Your shell may start in a member "
        "project. Follow the skill for tools, commits and reporting."
        f"{_stale_skill_guard(context)}\n\n"
        "Before editing, check both inbox lanes:\n"
        "`\"$HORIZON_BIN\" inbox list --mine --status open --kind protection --json`\n"
        "then `\"$HORIZON_BIN\" inbox list --mine --unread --kind conversation --json`.\n"
        "Honor standing protections and answer unread conversations first.\n\n"
        "# Task\n"
        f"{_task_block(context)}\n\n"
        "Follow mathlib-conventions. Preserve maxHeartbeats and synthInstance.maxHeartbeats. "
        f"{_PUBLIC_LEAN_API}"
        "Reuse topic modules and run Lake in the owning project. "
        "Commit with `$HORIZON_GIT` and a mathlib title.\n\n"
        "In this headless one-shot, advance the objective, commit verified units and final edits, "
        "and keep operational comments to changes. For new certificate/context wrappers, `\\leanok`/completion claims, "
        "or repeated frontiers, run `honesty-reviewer`. If unavailable, record why. "
        "Finish with the skill's brief report."
    )
