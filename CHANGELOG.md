# Changelog

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
