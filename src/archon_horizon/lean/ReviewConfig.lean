import Lake
import Lake.Build.Module
import Lake.Load.Workspace

/-! # Resolve a member file's compiler settings using Lake's native DSL loader. -/

open Lake DSL Lean

package archonSignatureReview

/-- Obtain the same module setup and compiler arguments as `lake lean`. -/
script prepare (args) do
  let [memberPath, modulePath, output] := args
    | throw <| IO.userError "expected a member directory, source and output path"
  let env ← getLakeEnv
  let some ws ← (loadWorkspace {lakeEnv := env, wsDir := memberPath}).toBaseIO
    | throw <| IO.userError "cannot load the member workspace"
  let cmd ← ws.runBuild (prepareLeanCommand modulePath)
  -- Lake appends the source and a temporary --setup pair after the configured arguments.
  let setupPath :: "--setup" :: source :: flags := cmd.args.toList.reverse
    | throw <| IO.userError "Lake omitted the module setup"
  unless source == (← resolvePath modulePath).toString do
    throw <| IO.userError "unexpected Lean source in the Lake command"
  let setup ← try ModuleSetup.load setupPath finally IO.FS.removeFile setupPath
  IO.FS.writeFile output (toJson (setup, flags.reverse)).compress
  return 0
