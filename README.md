# as-installed

> A portable [Agent Skill](https://agentskills.io) that makes a coding agent write code against the library versions your project actually has installed, not the API it remembers from training. It reads the real version from your lockfile and `node_modules` / `site-packages`, looks up each function in the installed types and source before using it, and afterwards checks that every import and name in the changed files exists in that version and isn't deprecated. Works in Claude Code, Codex CLI, Cursor, Gemini CLI, Antigravity CLI, GitHub Copilot, and any other Agent-Skills-compatible tool.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Part of the [Agent Skills catalog](https://github.com/code-with-rashid/agent-skills).

## The problem

A model's knowledge of a library is a snapshot of whatever version was common
in its training data. Your project runs the version in its lockfile. When a
library ships a major release, the agent keeps writing the old API:

- **Loud breaks:** `from mcp.server.fastmcp import FastMCP` on MCP Python SDK 2,
  where it became `from mcp.server.mcpserver import MCPServer`. Fails at import.
- **Quiet breaks:** `result.toUIMessageStreamResponse()` on AI SDK 7 still
  works, but it's deprecated and goes away in the next major. It type-checks,
  passes review, and breaks at the next upgrade.
- **Copying the repo:** the agent copies a call from a nearby file or an old
  doc that was written against the previous version.

Context7's October 2026 test of 47 API changes from 2026 releases found an agent
with no tools wrote old-API-only code for 24 of them
([source](https://context7.com/guides/why-ai-coding-agents-use-outdated-apis)).
Docs-lookup tools help, but they fetch "a" version of the docs over the network.
The code you have on disk is the exact version that will run.

## What this does

- **Pins down versions first.** Declared range, lockfile pin, and what's really
  installed, side by side. A stale or missing install gets fixed (from the
  lockfile) before anyone reads the wrong API.
- **Reads the installed package, not memory.** Finds the `.d.ts` / `.pyi` /
  source, bundled changelog and migration guide, and looks up every symbol it's
  about to use, with file:line and deprecation notes.
- **Proves it afterwards.** A dependency-free checker resolves every
  third-party import, imported name, and module/namespace member in the changed
  files against the installed packages, and flags missing and deprecated ones.
  The agent fixes them and re-runs until clean, then runs your typecheck/tests.
- **Fixes the code, not your dependencies.** It never bumps or pins versions
  to make its code work, and never edits installed packages.

## Quick demo

A project with `ai@7` and `mcp` 2.x installed, and code written from memory:

```text
$ python asinstalled.py versions
python interpreter: .venv\Scripts\python.exe
npm    ai          declared ^7.0.162   locked 7.0.162   installed 7.0.162
npm    zod         declared ^4.6.7     locked 4.6.7     installed 4.6.7
pypi   mcp         declared >=2        locked -         installed 2.3.0

$ python asinstalled.py check server.py route.ts
checked 9 import(s)/name(s) in 2 file(s): 6 ok, 3 problem(s)
route.ts:1  missing-name    notAThing is not exported by ai@7.0.162
    see node_modules\ai\dist\index.d.ts
route.ts:1  missing-name    experimental_bogus is not exported by ai@7.0.162
server.py:1  missing-module  can't import mcp.server.fastmcp (mcp 2.3.0): ModuleNotFoundError:
    No module named 'mcp.server.fastmcp'. This is mcp 2.x, where FastMCP was renamed to MCPServer
    (from mcp.server.mcpserver import MCPServer) ...

$ python asinstalled.py find ai toUIMessageStreamResponse
node_modules\ai\dist\index.d.ts:3003  [DEPRECATED: Use the standalone `toUIMessageStream` and]
    toUIMessageStreamResponse<UI_MESSAGE extends UIMessage>(options?: ...): Response;
```

You don't run these yourself: ask your agent for the feature, or "this crashes
with ImportError since we upgraded, fix it", and the skill drives it.

## Install

The skill is the `skills/as-installed/` folder: a `SKILL.md`, one reference
file, and one Python script (standard library only).

### Claude Code

```
/plugin marketplace add code-with-rashid/agent-skills
/plugin install as-installed@codewithrashid-skills
```

Or in one step on Claude Code v2.1.275+: `/plugin install as-installed --marketplace code-with-rashid/agent-skills`.
Then `/reload-plugins` (or start a new session).

### Codex CLI, Cursor, Gemini CLI, Antigravity CLI, GitHub Copilot, and other tools

Copy the skill folder (not the whole repo) into the skills directory your tool reads:

| Tool | Personal (all projects) | Project only |
|---|---|---|
| Codex CLI | `~/.agents/skills/` | `.agents/skills/` |
| Cursor | `~/.cursor/skills/` or `~/.agents/skills/` | `.cursor/skills/` or `.agents/skills/` |
| Gemini CLI | `~/.gemini/skills/` or `~/.agents/skills/` | `.gemini/skills/` or `.agents/skills/` |
| Antigravity CLI | `~/.gemini/skills/` | `.agents/skills/` |
| GitHub Copilot | `~/.copilot/skills/` or `~/.agents/skills/` | `.github/skills/` or `.agents/skills/` |
| Claude Code (without the plugin) | `~/.claude/skills/` | `.claude/skills/` |

```bash
git clone https://github.com/code-with-rashid/as-installed /tmp/as-installed
mkdir -p ~/.agents/skills
cp -r /tmp/as-installed/skills/as-installed ~/.agents/skills/
```

```powershell
git clone https://github.com/code-with-rashid/as-installed $env:TEMP\as-installed
New-Item -ItemType Directory -Force "$HOME\.agents\skills" | Out-Null
Copy-Item -Recurse "$env:TEMP\as-installed\skills\as-installed" "$HOME\.agents\skills\"
```

Or straight from the repo:

- GitHub CLI 2.90+, any of these tools:
  `gh skill install code-with-rashid/as-installed as-installed --agent <tool> --scope user`
  (`<tool>` is e.g. `github-copilot`, `codex`, `cursor`, `gemini-cli`, `antigravity-cli`, `claude-code`).
- Gemini CLI: `gemini skills install https://github.com/code-with-rashid/as-installed.git --path skills/as-installed`

Requirements: Python 3.8+. Node.js is used when present (to list runtime
exports of npm packages without types).

## Usage

Ask in plain words:

- "Add an MCP server with an `add` tool." (in a project on MCP SDK 2)
- "Stream the chat response with the AI SDK." (on AI SDK 7)
- "`python app.py` crashes with ImportError since we upgraded. Fix it."
- "We just bumped Prisma to v7; make the data layer compile again."
- "Check this AI-written PR only uses APIs that exist in our installed versions."

The script also works on its own, from the project root:

```bash
python skills/as-installed/scripts/asinstalled.py versions [pkg ...]
python skills/as-installed/scripts/asinstalled.py where <pkg>
python skills/as-installed/scripts/asinstalled.py find <pkg> <symbol>
python skills/as-installed/scripts/asinstalled.py check <files or dirs>   # exit 1 on missing APIs
```

Add `--json` for machine-readable output, `--root DIR` for another project, and
`--python PATH` if the project's interpreter isn't `.venv`, `VIRTUAL_ENV` or
Poetry's.

## Limitations

- `check` covers JavaScript/TypeScript and Python. It resolves imports, imported
  names, and members of imported modules, namespaces and classes. It can't
  follow methods on objects returned at runtime (`client.chat.create()`); the
  skill has the agent confirm those with `find`, and your typecheck catches the
  rest. For Go and Rust it reports versions and source locations; the compiler
  (`go build`, `cargo check`) does the verification.
- Python checks import the packages with the project's interpreter, which runs
  their import-time code. Use it on environments you trust.
- Yarn Plug'n'Play (no `node_modules`) isn't supported.
- Deprecation is detected from `@deprecated` JSDoc, `DeprecationWarning`s,
  PEP 702 `@deprecated`, and `.. deprecated::` docstrings. Packages that don't
  mark deprecations any of these ways aren't flagged.

## Tests

```bash
python -m unittest discover -s tests
```

End-to-end evals (scenario builders, prompts, expected behaviour, results) are
in [`evals/`](evals/).

## Contributing

Issues and pull requests are welcome. Please include a failing case (a minimal
package layout and the import that was misjudged) with any change to
`asinstalled.py`, and keep it standard-library only.

## License

[MIT](LICENSE) © Rashid Mahmood
