# Changelog

## 0.2.0 — 2026-11-12

- `check` now verifies members used on objects returned by imported calls
  ([#1](https://github.com/code-with-rashid/as-installed/issues/1)). With AI
  SDK 7, `const result = streamText(...); result.toUIMessageStreamResponse()`
  used to pass; it is now reported as deprecated, with the replacement from
  the package's own deprecation note.
  - JS/TS: `const x = [await] fn(...)`, `fn(...).member` and `new Class(...)`
    for imported functions and classes (named, namespace and default imports).
    The declared return type is resolved in the package's `.d.ts` files,
    following `extends`, intersections, `Pick`/`Omit`, `Partial`, declaration
    merging, `Promise<T>` when awaited, and types imported from other packages.
  - Python: `x = Class(...)` and functions whose return annotation is a class.
    Methods, properties, class attributes, annotations, dataclass and pydantic
    fields, `__slots__` and `self.x = ...` assignments count as members.
  - Results are `ok`, `deprecated`, `missing-name` (only when the whole type
    resolved), or `unverified` with a `find` hint when it didn't.
- Deprecation notes that span several JSDoc lines are now shown in full.
- Re-exports such as `export { X } from "./client.js"` now resolve to
  `client.d.ts` (or `.d.mts`) rather than the JavaScript file next to it.
- `check` on large bundled `.d.ts` files (AI SDK: 12k lines) went from about
  30 s to 2 s.

## 0.1.1 — 2026-11-03

- `find` (Python): a `@deprecated` decorator or deprecation warning on the next
  definition no longer marks the previous one deprecated (found in the Haiku
  E3 eval: `Store.set` was flagged because `Store.put` below it is deprecated).

## 0.1.0 — 2026-11-03

First release.

- `as-installed` skill: a procedure for writing and fixing code against the
  library versions a project has installed, with hard rules against coding
  from memory, changing dependency versions to fit the code, or editing
  installed packages.
- `scripts/asinstalled.py` (stdlib Python):
  - `versions`: declared range, lockfile pin and installed version for npm,
    pnpm, yarn, bun, pip/requirements, uv, Poetry, PDM, Go modules and Cargo,
    with `not-installed`, `lock-mismatch` and `range-mismatch` flags.
  - `where`: install folder, types entry, bundled docs, changelog and
    migration guides of a package.
  - `find`: definitions of a symbol in the installed types and source, with
    file:line and deprecation notes.
  - `check`: every third-party import, imported name and module/namespace
    member in JS/TS and Python files, resolved against the installed packages;
    missing and deprecated ones reported, exit 1 when something is missing.
- Reference: where installed APIs live per ecosystem.
- Unit tests and end-to-end evals with reproducible scenarios.
