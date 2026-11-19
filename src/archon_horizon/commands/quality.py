"""Member-level delivery checks and human decisions about inherited issues."""
import typer

from archon_horizon.lean.delivery import ask as ask_question
from archon_horizon.lean.delivery import check_project, directory, questions, read
from archon_horizon.log import log
from .shared import emit_json, load_workspace

app = typer.Typer(help="Verify final Lean quality and discuss inherited issues.")


@app.command()
def check(ctx: typer.Context, project: str = typer.Option(..., "-p", "--project"),
          as_json: bool = typer.Option(False, "--json")) -> None:
    """Build and force a serial compile of each authored Lean file."""
    _, ws = load_workspace(ctx.obj["root"])
    report = check_project(ws, project)
    if as_json:
        emit_json(report)
    else:
        for name, elapsed in report["timings"].items():
            log.info(f"{name}: {elapsed:g}s")
        for issue in report["retained"]:
            log.info(f"{issue['file']}: retained by the project owner ({issue['reason']})")
        for issue in report["issues"]:
            log.warn(f"{issue['file']}: {issue['reason']} " + issue.get("inbox", ""))
        if report["ok"]:
            log.success(f"Quality checks passed for {project}")
    if not report["ok"]:
        raise typer.Exit(1)


@app.command()
def ask(ctx: typer.Context, project: str = typer.Option(..., "-p", "--project"),
        file: str = typer.Option(..., "--file"), reason: str = typer.Option(..., "--reason")) -> None:
    """Ask the human in the Inbox before repairing an issue inherited from the baseline."""
    _, ws = load_workspace(ctx.obj["root"])
    log.info(ask_question(ws, project, file, reason))


@app.command()
def status(ctx: typer.Context, project: str = typer.Option(..., "-p", "--project")) -> None:
    """Show the latest checks and replies to quality questions."""
    _, ws = load_workspace(ctx.obj["root"])
    emit_json({"check": read(directory(ws, project) / "check.json"), "questions": questions(ws, project)})
