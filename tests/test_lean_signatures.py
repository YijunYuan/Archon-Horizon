"""Public signature checks exercise the member parser, including project-defined syntax."""
import pytest

from archon_horizon.lean.signatures import SignatureCheckError, signature_findings


def test_source_without_a_pile_needs_no_toolchain(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    (tmp_path / "Clean.lean").write_text("theorem one : (let n := 1; n = 1) := rfl")
    assert signature_findings(tmp_path, "Clean.lean") == []


def test_native_scan_finds_only_public_nested_signature_setup(tmp_path, lean_toolchain):
    (tmp_path / "lakefile.toml").write_text(
        'name = "Review"\nmoreLeanArgs = ["-Dexperimental.module=true", "-Dpp.universes=true"]\n'
        '[[lean_lib]]\nname = "Module"\nmoreLeanArgs = ["-D", "pp.explicit=true"]\n'
    )
    source = """module
public import Lean

open Lean Parser Command
run_cmd
  let opts ← getOptions
  unless opts.getBool `pp.universes && opts.getBool `pp.explicit do
    throwError "expected both package and target options"
syntax (name := reviewLemma) declModifiers group("lemma " declId ppIndent(declSig) declVal) : command
macro_rules
  | `($mods:declModifiers lemma $id:declId $sig:declSig $val:declVal) =>
    `($mods:declModifiers theorem $id:declId $sig:declSig $val:declVal)

theorem modulePrivate : let a := 1; let b := a; let c := b; c = 1 := rfl
public section
namespace Visible
local notation "One" => (1 : Nat)
/-- A misleading theorem letI letI in a comment. -/
@[simp] protected theorem α : let a := One; let b := a; let c := b; c = 1 := rfl
private theorem hidden : let a := 1; let b := a; let c := b; c = 1 := rfl
lemma instances : letI : Inhabited Nat := ⟨1⟩; letI : Inhabited Bool := ⟨true⟩; True := trivial
set_option pp.universes true in
theorem binder (h : (let a := 1; let b := a; let c := b; c = 1)) : True := trivial
theorem separate : (let a := 1; a = 1) ∧ (let b := 2; b = 2) ∧ (let c := 3; c = 3) :=
  ⟨rfl, rfl, rfl⟩
theorem proofOnly : True := by
  letI : Inhabited Nat := ⟨1⟩
  letI : Inhabited Bool := ⟨false⟩
  trivial
theorem termProof : (by
    letI : Inhabited Nat := ⟨1⟩
    letI : Inhabited Bool := ⟨false⟩
    exact True) := trivial
example : let a := 1; let b := a; let c := b; c = 1 := rfl
def chosen : Prop := let a := 1; let b := a; let c := b; c = 1
/- theorem comments : /- nested letI -/ letI letI -/
def sample := "theorem literals letI letI letI"
end Visible
"""
    (tmp_path / "Module.lean").write_bytes(source.replace("\n", "\r\n").encode())
    found = signature_findings(tmp_path, "Module.lean")
    assert [f.reason.split()[2] for f in found] == [
        "Visible.α", "Visible.instances", "Visible.binder",
    ]
    assert "(3 let, 0 letI)" in found[0].reason
    assert "(0 let, 2 letI)" in found[1].reason
    assert found[0].header == "theorem α : let a := One; let b := a; let c := b; c = 1"
    assert source.splitlines()[found[0].line - 1].startswith("@[simp] protected theorem α")

    legacy = "theorem legacy : let a := 1; let b := a; let c := b; c = 1 := rfl\n"
    (tmp_path / "Legacy.lean").write_text(legacy)
    assert "public theorem legacy " in signature_findings(tmp_path, "Legacy.lean")[0].reason

    (tmp_path / "Broken.lean").write_text(legacy.replace(":= rfl", ":= ("))
    with pytest.raises(SignatureCheckError, match="Broken.lean"):
        signature_findings(tmp_path, "Broken.lean")


def test_native_scan_loads_a_lean_lake_configuration(tmp_path, lean_toolchain):
    (tmp_path / "lakefile.lean").write_text("import Lake\nopen Lake DSL\npackage review\nlean_lib Demo\n")
    (tmp_path / "Demo.lean").write_text(
        "theorem fromDSL : let a := 1; let b := a; let c := b; c = 1 := rfl\n"
    )
    assert "public theorem fromDSL " in signature_findings(tmp_path, "Demo.lean")[0].reason
