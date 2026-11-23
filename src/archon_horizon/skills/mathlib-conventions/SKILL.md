---
name: mathlib-conventions
description: >-
  Mathlib house style for Lean you write — naming, module/declaration
  docstrings, file tree layout, preferred abstractions, and links to the
  leanprover-community guides (Palimpsest-style documentation map).
---

Write Lean that looks like it belongs in mathlib: discoverable, reusable,
documented, and composing with the existing API. This is the style side; use
[[leansearch]] / [[mathlib-orientation]] to *find* lemmas and [[lean-check]] to
verify, and keep the blueprint in step via [[blueprint-conventions]].

## Authoritative upstream guides

Before enforcing a nontrivial convention, prefer the current community pages
over memory (same map as Palimpsest's mathlib-documentation skill):

| Topic | Upstream |
|---|---|
| Naming | https://mathlib.org/contribute/naming.html |
| Style | https://mathlib.org/contribute/style.html |
| Docstrings / module docs | https://mathlib.org/contribute/doc.html |
| Agent commit titles and bodies | https://mathlib.org/contribute/commit.html |
| PR review checklist | https://mathlib.org/contribute/pr-review.html |
| How to contribute | https://mathlib.org/contribute/how-to-contribute.html |

When local summary and upstream disagree, **follow upstream** and say which
source you used. Nearby mathlib source under `.lake/packages/mathlib/` is
evidence for idiomatic spelling and file placement.

## Naming

- **Theorems / lemmas (proofs of propositions) → `snake_case`**, named by *what the statement
  says*, composing the pieces in order: `add_comm`, `mul_le_mul_left`,
  `isOpen_iUnion`, `continuous_id`. A reader should reconstruct the statement
  from the name.
- **Named propositions, types, structures, classes, inductives → `UpperCamelCase`**: `LinearMap`,
  `IsLindelof`, `RiemannianMetric`.
- **Definitions / terms / functions → `lowerCamelCase`**: `leviCivita`,
  `koszulRHS`, `parallelTransport`.
- **Structural predicates take the `Is` prefix**: `IsOpen`, `IsCompact`,
  `IsSymmetric`.
- **Name order relations by the operator you wrote**, not its mirror: use `le`
  / `lt` (`le_of_lt`, `add_lt_add`), not `ge` / `gt`.
- **Meaningful names only.** No `helper1`, `foo_aux`, `my_lemma`, `lemma2`.
  Ad-hoc names are invisible to search and un-reusable.
- Avoid engineering noise in public names (`Map`, `Func`, `Wrapper`, `Tower`,
  `Stage0`…) unless the mathematics truly is that object. Long compound names
  that encode the whole file path are a smell — shorten to the mathematical claim.

## Documentation (required, not optional)

Faithful docs beat missing docs; **overclaiming docs are worse than none**.

- **Every new file** gets a module docstring (`/-! … -/`) stating what the module
  covers, main definitions/results, and literature anchors when relevant.
- **Definitions and major theorems** get `/-- … -/` docstrings that match the
  *actual* statement (update docstring in the same edit as the type).
- Cross-reference related declarations; cite specific results (theorem number /
  page), not only a book title — pair with blueprint `\source`/`\dcref` and the
  project `.bib` when the result is literature-backed.
- Comments explain *mathematics*, not session history.

Start with the project’s accurate copyright/authorship and license information, then `module`, sorted import groups and the titled module docstring. Use public imports for exported dependencies. Never invent an author or relicense existing files. Module docstring example:

```lean
/-!
# Module title

One short mathematical paragraph.

## Main definitions
* `foo` — …

## Main results
* `bar_eq_baz` — …

Reference: Author, *Title*, Thm. X.Y / §Z.
-/
```

## File tree and modules

- Prefer a **mathlib-like tree**: `ProjectLib/Topic/Subtopic/Foo.lean`, not a
  flat dump of dozens of files in one folder.
- One primary mathematical topic per file; split when files grow huge or mix
  unrelated APIs.
- Group namespaces by mathematical API. Use sections for shared hypotheses and the narrowest sensible `variable` scope. Inspect exported signatures for accidentally strengthened assumptions.
- Use `exact` when sufficient and `simpa ... using h` when a single simplification closes the proof. Keep a longer proof when it is clearer or faster.
- After moves/renames, update consumers and delete empty shells. Writable
  hygiene helpers (`janitor`) may restructure trees; lead agents own API renames.

## Search before you prove; prefer before you build

- Search first ([[leansearch]]); a negative result after a precise query means
  it's genuinely absent — then prove it at reusable generality.
- Choose the representation from the mathematical use and nearby mathlib
  declarations. A computed value or a predicate applied to an explicit object
  fits a `def` (e.g. `IsOpen s`). Bundle data and laws in a structure when
  callers select an object, pass it to maps or compare two choices.
- Use a `class` when callers should infer an ambient structure or property
  from the available context. For example, a continuity lemma uses an
  ambient `[TopologicalSpace X]`, while a theorem comparing two topologies
  on `X` takes them explicitly. Register a canonical
  construction for an existing class as an `instance` when it composes with
  existing inference paths. Keep competing choices on the same carrier explicit
  or local to the intended scope. Check for diamonds.
- Reuse mathlib operations and notation with their established meaning.
  Introduce scoped notation when familiar mathematical syntax recurs and its
  domain determines its meaning. Check the expected type and scope in an
  importing module. See [[api-composition]] for consumer checks.

## API thinness (avoid layered blockers)

- Do **not** introduce an `abbrev` / index alias / package type solely to name
  `A ⊕ B` (or similar) when that forces a family of `@[reducible]` equivalence
  lemmas and breaks definitional equality — write the type at use sites or one
  clear structure with a real API.
- Prefer **few bundling layers**. Each extra instance/package/wrapper multiplies
  synthesis search and often ends in `set_option synthInstance.maxHeartbeats`
  or `maxHeartbeats` (see `"$HORIZON_BIN" benchmark` and [[lean-quality]]).
- When debt is layered, prefer [[restart-module]] over stacking more bridges.
- Core defs deserve Mathlib-shaped design; when consumers look wrong, audit the
  definition first ([[definition-quality]]).

## Missing infrastructure → build it properly

When a needed fact truly isn't in mathlib:

- State it in mathlib-ready generality with a proper name and a real proof
  attempt, as project-local infrastructure.
- Factor hard steps into named reusable lemmas.
- Give significant results a blueprint node (`\lean{…}` + `\uses{…}`).

## House style (light touch)

- Group related material into `section`s / `namespace`s; keep lines readable
  (~100 cols); align `calc` steps.
- Prefer `simp`/`grind`-normal statements so lemmas fire as `@[simp]` candidates.
- Mark `@[simp]` only when rewrites are confluent.

## Faithfulness (non-negotiable)

- **Never weaken a statement to make it pass**, and never change a declaration's
  statement to dodge its proof.
- **No `axiom` / `admit` / `native_decide` on a goal you haven't honestly
  closed**, and no `sorry` reported as done — see [[lean-check]].

For source-backed public APIs also consult [[formalization-review]]. Pair API
design questions with [[api-composition]].
