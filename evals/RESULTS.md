# Eval results

Run on 2026-11-03 (v0.1.0; the `find` fix in 0.1.1 came from the Haiku E3 run) on Windows 11, Python 3.14, Node 24, npm 11.
Each eval ran in a fresh agent session that saw only the skill's name,
description and SKILL.md path (the skill folder was read-only). Grading was
done from the filesystem: git status and diff, running the code and tests,
`tsc --noEmit`, and installed package versions. Baselines ran the same prompt
on a fresh copy of the scenario with no skill.

## End to end

| Eval | Model | With skill | Baseline (no skill) |
|---|---|---|---|
| E1 tidewater: new code, repo docs show the old API | default | pass 5/5 (openPool + run, tests pass, stale docs and deprecated `query()` in users.js reported, not edited) | pass 5/5 |
| E2 tidewater: lockfile 3.1.0, node_modules 2.4.0 | default | pass 5/5 (found the stale install, `npm ci`, coded against 3.1.0, tests pass) | not run |
| E3 quillstore: ImportError after upgrade | default | pass 5/5 (`open_store`, `get`, `set`; runs with `-W error::DeprecationWarning`; quillstore still 2.0.1) | not run |
| E3 quillstore | Haiku | pass 5/5 | pass 5/5 |
| E4 real MCP Python SDK 2.3.0: new server | default | pass 4/4 (`MCPServer`, check clean, stdio `tools/call add` verified) | pass 4/4 |
| E5 real AI SDK 7.0.162: chat route for useChat | default | pass (`createUIMessageStreamResponse`, typecheck passes, check clean) | pass |
| E5 real AI SDK 7.0.162 | Haiku | **pass** (no deprecated calls, typecheck passes) | **fail**: returns `result.toUIMessageStreamResponse()`, deprecated in v7 and removed in the next major. Typecheck passes, so nothing flags it. |

What this shows: a strong model already reads `node_modules` when it meets a
library it doesn't know (the fictional ones) or hits an import error. Where the
skill pays off is the quiet case, an API the model *thinks* it knows that still
type-checks: without the skill, the smaller model shipped the deprecated call,
even though its report said it had "checked the v7 type definitions". With the
skill, `find` and `check` caught it and both models used the replacement the
deprecation note names. With the skill, agents also reported stale docs, stale
installs and deprecated calls elsewhere in the repo without editing them.

## Triggering

Router test: 12 skill descriptions (as-installed, the 5 other skills in this
catalog, and 6 common neighbours including `dependency-upgrade`, `debugging`,
`code-review` and a docs-fetching `source-driven-development`), 8 prompts that
should load as-installed and 8 that should not (see `evals.json`).

| Router model | Should trigger | Should not trigger |
|---|---|---|
| default | 8/8 | 8/8 |
| Sonnet | 8/8 | 8/8 |
| Haiku (3 runs) | 7/8 each | 8/8 each |

The negative evals N1–N3 in `evals.json` are covered by this router test
(prompts 9–12), not by end-to-end runs.

Haiku's one miss, in all three runs, was "Add an MCP server with an add tool to
this Python project". It chose no skill at all, not a wrong one. Prompts that
name an error, a library version or an SDK triggered every time.

## Unit tests

`python -m unittest discover -s tests`: 18 tests, all passing. They cover range
checks; npm, pnpm, yarn, uv, requirements, Go and Cargo version resolution;
`.d.ts` export resolution (star and list re-exports, subpath exports, `@types`
fallback, `@deprecated`); JS/TS import parsing; Python import, attribute,
lazy-module and deprecation checks; `where` and `find` for both ecosystems; and
tsconfig path aliases, and (0.1.1) a decorator on the next definition not leaking into `find` results.

Real-package smoke test: on `ai@7.0.162` and `mcp` 2.3.0, `check` flagged the
removed `mcp.server.fastmcp` import and made-up exports, and `find` marked
`toUIMessageStreamResponse` deprecated with the replacement named in its note.

## 0.2.0 — 2026-11-12: members of returned objects (issue #1)

Checked against real packages in a scratch project (Windows 11, Python 3.14, Node 24):
`ai@7.0.170`, `@ai-sdk/openai`, `openai@7.55.0`, and Python `click 8.5.0`.

- Reported case: `const result = streamText(...); return result.toUIMessageStreamResponse()`
  was clean in 0.1.1; 0.2.0 reports it `deprecated` with the package's note (use `toUIMessageStream` +
  `createUIMessageStreamResponse` with `result.stream`). `streamText(...).toDataStreamResponse()` (the
  v4 name) and an invented `r.notAThing` on `generateText` are `missing-name`.
- False-positive sweep: 46 real members across `new OpenAI()`, `streamText`, `generateText`,
  `embedMany` results: 0 wrong `missing-name`. 3 `deprecated` (all real JSDoc `@deprecated`),
  3 `unverified` (`ReadableStream` from lib.dom, and `Agent`, which didn't fully resolve).
- `click.Context(...).made_up` → `missing-name`; `find_root()` ok.
- Unit tests: 20 (2 new, covering inheritance, Pick, unions, awaited promises, default exports,
  comments/strings, reassigned variables, `with ... as`, dataclass/property/`self.x` members).
