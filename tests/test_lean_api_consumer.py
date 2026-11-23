"""Check a public Lean API from an ordinary file outside the package targets."""
import subprocess

from archon_horizon.core.workspace import Project, Workspace
from archon_horizon.lean.delivery import check_project


HEADER = "/- Copyright (c) test. -/\nmodule\n"
DOC = "/-! # Graded objects -/\n"

GRADE_CLASS = """
class Graded (α : Type) where
  degree : α → Nat

instance natGrading : Graded Nat := ⟨id⟩
"""
GRADE_DEF = """
@[expose] def Graded (α : Type) := α → Nat
def Graded.degree {α : Type} (g : Graded α) (x : α) : Nat := g x
def natGrading : Graded Nat := id
"""
API_HEADER = HEADER + DOC + "public section\n"
OPERATIONS = """
namespace Degrees
scoped notation "deg(" x ")" => Graded.degree x
end Degrees

def weighted (weight degree : Nat) : Nat := weight * degree
"""
CONSUMER = """
open scoped Degrees

example {α : Type} [Graded α] (x : α) (weight : Nat) :
    weighted weight deg(x) = weighted weight (Graded.degree x) := rfl

example (n : Nat) : Nat := deg(n)
"""


def test_delivery_compiles_import_only_consumer_against_current_api(tmp_path, lean_toolchain):
    project = tmp_path / "member"
    consumer = project / "test" / "Consumer.lean"
    consumer.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    ws = Workspace("test", tmp_path, projects={"member": Project("member", project)})
    (project / "lakefile.toml").write_text(
        'name = "grading"\ndefaultTargets = ["Main"]\n'
        'moreLeanArgs = ["-Dexperimental.module=true"]\n'
        '[[lean_lib]]\nname = "Main"\n[[lean_lib]]\nname = "Grading"\n'
    )
    (project / "Main.lean").write_text(HEADER + DOC)
    consumer.write_text(HEADER + "import Grading\n" + DOC + CONSUMER)
    provider = project / "Grading.lean"
    provider.write_text(API_HEADER + GRADE_CLASS + OPERATIONS)
    valid = check_project(ws, "member")
    assert valid["ok"], valid["issues"]
    assert "test/Consumer.lean" in valid["timings"]

    # Each replacement still builds on its own. Reuse the .olean from the last
    # run to verify the non-default import is rebuilt before checking its caller.
    for api, diagnostic in (
        (GRADE_CLASS.replace("instance natGrading", "def natGrading") + OPERATIONS, "failed to synthesize"),
        (GRADE_DEF + OPERATIONS, "binder annotation"),
        (GRADE_CLASS + OPERATIONS.replace('"deg("', '"degree("'), "deg"),
    ):
        provider.write_text(API_HEADER + api)
        report = check_project(ws, "member")
        assert not report["ok"]
        assert len(report["issues"]) == 1, report["issues"]
        issue = report["issues"][0]
        assert issue["file"] == "test/Consumer.lean", issue
        assert diagnostic in issue["reason"].lower(), issue
