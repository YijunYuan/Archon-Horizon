import Lean

/-! # Source-level review of public theorem statements

Lake supplies each file's syntax extensions, module visibility and package options.
Only declarations with a theorem constant and matching source range reach the report.
-/

open Lean

/-- Length and instance count of the longest nested path. Separate branches are independent. -/
private partial def setupDepth (stx : Syntax) : Nat × Nat := Id.run do
  if stx.isOfKind ``Parser.Term.byTactic || stx.isQuot then return (0, 0)
  let mut best := (0, 0)
  for child in stx.getArgs do
    let depth := setupDepth child
    if depth.1 > best.1 || (depth.1 == best.1 && depth.2 > best.2) then best := depth
  if stx.isOfKind ``Parser.Term.letI then return (best.1 + 1, best.2 + 1)
  if stx.isOfKind ``Parser.Term.let then return (best.1 + 1, best.2)
  return best

private structure Candidate where
  pos : Position
  name : Name
  header : String
  lets : Nat
  letIs : Nat

private def candidate? (fm : FileMap) (stx : Syntax) : Option Candidate := do
  guard (stx[0].getAtomVal == "theorem" || stx[0].getAtomVal == "lemma")
  guard (stx[1].isOfKind ``Parser.Command.declId && stx[2].isOfKind ``Parser.Command.declSig)
  let (total, insts) := setupDepth stx[2]
  guard (total ≥ 3 || insts ≥ 2)
  let pos ← stx[1].getPos?
  let startPos ← stx[0].getPos?
  let sig ← stx[2].getSubstring? false false
  let name := stx[1][0].getId.replacePrefix `_root_ .anonymous
  return ⟨fm.toPosition pos, name, {sig with startPos}.toString, total - insts, insts⟩

private partial def collect (fm : FileMap) (stx : Syntax) : Array Candidate := Id.run do
  if let some c := candidate? fm stx then return #[c]
  if stx.isQuot || stx.isOfKind ``Parser.Command.declValSimple ||
      stx.isOfKind ``Parser.Command.declValEqns || stx.isOfKind ``Parser.Command.declSig then
    return #[]
  return stx.getArgs.flatMap (collect fm)

private partial def scanCommands (fm : FileMap) (s : Language.Lean.CommandParsedSnapshot)
    (candidates : Array Candidate := #[]) : Array Candidate × Environment := Id.run do
  let candidates := candidates ++ collect fm s.stx
  if let some next := s.nextCmdSnap? then
    return scanCommands fm next.get candidates
  return (candidates, s.elabSnap.resultSnap.get.cmdState.env)

private def memberConfig (configFile : String) : IO (ModuleSetup × Options) := do
  let json ← IO.ofExcept (Json.parse (← IO.FS.readFile configFile))
  let config : ModuleSetup × Array String ← IO.ofExcept (fromJson? json)
  let (setup, flags) := config
  let mut opts : Options := {}
  let decls ← getOptionDecls
  let mut following := false
  for arg in flags do
    if arg == "-D" then
      following := true
      continue
    unless following || arg.startsWith "-D" do continue
    let setting := if following then arg else toString (arg.drop 2)
    following := false
    let key :: value := setting.splitOn "=" | throw <| IO.userError s!"invalid option {arg}"
    let name := key.toName
    let option := ({} : Options).set name ("=".intercalate value)
    -- Imported libraries can register further options. The frontend reparses these after import.
    let option ← if decls.contains name then Language.Lean.reparseOptions option else pure option
    opts := opts.mergeBy (fun _ _ new => new) option
  return (setup, opts.mergeBy (fun _ _ new => new) setup.options.toOptions)

private def scan (path configFile : String) : IO Json := do
  let (setup, opts) ← memberConfig configFile
  setup.dynlibs.forM loadDynlib
  let input := Parser.mkInputContext (← IO.FS.readFile path) path
  let snap ← Language.Lean.process (fun header => do
    let isModule := setup.isModule || header.isModule
    let config : Language.Lean.SetupImportsResult := {
      mainModuleName := setup.name, isModule
      imports := setup.imports?.getD header.imports, plugins := setup.plugins
      importArts := setup.importArts, opts
    }
    -- The package field was added after the experimental module system.
    return .ok (by first | exact { config with package? := setup.package? } | exact config))
    none {input with}
  for snapshot in (Language.toSnapshotTree snap).getAll do
    for msg in snapshot.diagnostics.msgLog.toList do
      if msg.severity == .error then throw <| IO.userError (← msg.toString)
  let some header := snap.result?.bind (·.processedSnap.get.result?)
    | throw <| IO.userError s!"failed to process the imports of {path}"
  let (candidates, env) := scanCommands input.fileMap header.firstCmdSnap.get
  let mut byPos : Std.HashMap (Nat × Nat) Candidate := {}
  for c in candidates do
    byPos := byPos.insert (c.pos.line, c.pos.column) c
  let mut rows := #[]
  for (name, ranges) in declRangeExt.toPersistentEnvExtension.getState env do
    if isPrivateName name || name.hasMacroScopes then continue
    let pos := ranges.selectionRange.pos
    if let some c := byPos[(pos.line, pos.column)]? then
      unless c.name.isSuffixOf name do continue
      if let some (.thmInfo _) := env.find? name then
        rows := rows.push <| Json.mkObj [
          ("name", toJson name.toString), ("line", toJson c.pos.line), ("header", toJson c.header),
          ("let", toJson c.lets), ("letI", toJson c.letIs)]
  return toJson rows

unsafe def main (args : List String) : IO Unit := do
  initSearchPath (← findSysroot)
  enableInitializersExecution
  let [path, configFile, out] := args
    | throw <| IO.userError "expected a member source, compiler configuration and report path"
  IO.FS.writeFile out (← scan path configFile).compress
