# Where installed APIs live

Read this when `where`, `find` or `check` can't find something you expect, or
for ecosystems the script only covers partly.

## npm, pnpm, yarn, bun

- Install folder: `node_modules/<name>` (scoped: `node_modules/@scope/name`),
  found by walking up from the project root. pnpm symlinks these into
  `node_modules/.pnpm/`; reading through the symlink is fine. Yarn Plug'n'Play
  (`.pnp.cjs`, no `node_modules`) isn't supported: run `yarn unplug <pkg>` or
  read the zip under `.yarn/cache`, or use `nodeLinker: node-modules`.
- Which file defines the API: `package.json` `exports` decides what can be
  imported and from which subpath (`"./server"` means `import ... from
  "pkg/server"`; anything not listed can't be imported). The `types` condition
  inside each entry, or the top-level `types`/`typings` field, names the `.d.ts`
  file. Without types, `check` falls back to loading the module with Node and
  listing its exports.
- Types in a separate package: `@types/<name>` (`@types/scope__name` for
  `@scope/name`).
- Deprecation is a JSDoc `@deprecated` tag on the declaration. Some packages
  also print a runtime warning; the tag is the reliable signal.
- Bundled docs: many packages ship `README.md`, `CHANGELOG.md`, and some a
  `docs/` folder or the original `src/`. `where` lists them.
- Lockfile pins: `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `bun.lock`.
  Install exactly the locked versions with `npm ci`, `pnpm install
  --frozen-lockfile`, `yarn install --immutable`, `bun install --frozen-lockfile`.

## Python

- Which interpreter: the project's venv (`.venv`, `venv`), `VIRTUAL_ENV`,
  Poetry's env (`poetry env info -p`), or pass `--python`. uv projects use
  `.venv` by default; `uv sync` creates it from `uv.lock`.
- Distribution name vs import name differ often (`PyYAML` → `yaml`,
  `beautifulsoup4` → `bs4`). `where` maps them through the installed metadata.
- Types: inline with a `py.typed` marker, `.pyi` stubs in the package, or a
  separate `types-<name>` / `<name>-stubs` distribution.
- Deprecation shows up as `warnings.warn(..., DeprecationWarning)`, the
  `@warnings.deprecated` / `@typing_extensions.deprecated` decorator (PEP 702,
  sets `__deprecated__`), or `.. deprecated::` in the docstring. `check` catches
  all three for module-level names and attributes.
- Changelogs usually aren't installed. Look at the `METADATA` file `where`
  prints (it often holds the README), the project URLs in it, or the docs for
  the exact installed version (most docs sites have a version switcher;
  GitHub tags: `https://github.com/<org>/<repo>/blob/v<version>/CHANGELOG.md`).
- Lock pins: `uv.lock`, `poetry.lock`, `pdm.lock`, or `==` pins in
  `requirements*.txt`. Install with `uv sync`, `poetry install`,
  `pdm install`, `pip install -r requirements.txt`.

## Go

- `versions` reads `go.mod` and checks the module cache (`go env GOMODCACHE`,
  with capitals escaped as `!x`). Source: `$GOMODCACHE/<module>@<version>/`.
- Verification is the compiler: `go build ./...` and `go vet ./...`.
  Deprecation is a `// Deprecated:` paragraph in the doc comment; `staticcheck`
  (SA1019) reports uses of deprecated identifiers.
- Download what `go.mod` pins: `go mod download`.

## Rust (Cargo)

- `versions` reads `Cargo.toml` and `Cargo.lock`. Source:
  `~/.cargo/registry/src/<index>/<crate>-<version>/` (or `$CARGO_HOME`).
- Verification is the compiler: `cargo check`. Deprecation is the
  `#[deprecated]` attribute; the compiler warns on use.
- Fetch locked versions: `cargo fetch --locked`.

## Other ecosystems

The same procedure works by hand: find the locked version, open the installed
source in the package cache (Maven `~/.m2/repository`, NuGet
`~/.nuget/packages`, RubyGems `gem contents <gem>`, Composer `vendor/`), read
the declarations, then let the compiler or test suite verify.
