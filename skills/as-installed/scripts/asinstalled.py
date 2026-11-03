#!/usr/bin/env python3
"""Ground library code in the versions a project actually has installed.

  versions   resolved versions of the project's dependencies: declared range,
             lockfile pin, and what is really installed, with mismatches.
  where PKG  where the installed copy of PKG lives: version, type definitions,
             entry points, bundled docs, changelog and migration guides.
  find PKG SYMBOL
             search PKG's installed types and source for SYMBOL: definitions
             with file:line, and whether they are marked deprecated.
  check FILE...
             check every third-party import (and namespace member) in the
             given Python / JavaScript / TypeScript files against the
             installed packages: missing modules, missing names, deprecated.

Ecosystems: npm/pnpm/yarn/bun, Python (pip, uv, poetry, venvs), and
versions/where for Go modules and Cargo. Standard library only. Python 3.8+.
`check` and `where` for Python import the package with the project's own
interpreter, which runs the package's import-time code.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist",
             "build", "target", ".next", ".tox", ".nox", ".mypy_cache", ".pytest_cache"}
DOC_RE = re.compile(r"^(readme|changelog|changes|history|migrat\w*|upgrad\w*|news|release[-_]?notes)(\.\w+)?$", re.I)
JS_EXT = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts")
NODE_BUILTINS = {
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console", "constants",
    "crypto", "dgram", "diagnostics_channel", "dns", "domain", "events", "fs", "http",
    "http2", "https", "inspector", "module", "net", "os", "path", "perf_hooks", "process",
    "punycode", "querystring", "readline", "repl", "stream", "string_decoder", "sys",
    "timers", "tls", "trace_events", "tty", "url", "util", "v8", "vm", "wasi",
    "worker_threads", "zlib", "test", "sqlite",
}


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def load_json(path):
    text = read(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        # bun.lock and some tsconfig files are JSONC: strip comments and trailing commas
        text = re.sub(r"(?m)^\s*//.*$", "", text)
        text = re.sub(r",(\s*[}\]])", r"\1", text)
        try:
            return json.loads(text)
        except ValueError:
            return None


# ------------------------------------------------------------------ versions

def parse_version(v):
    m = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", v or "")
    return tuple(int(x or 0) for x in m.groups()) if m else None


def satisfies(installed, spec):
    """Rough semver/PEP 440 range check: enough to catch a wrong major/minor."""
    iv = parse_version(installed)
    if not iv or not spec:
        return None
    spec = spec.strip()
    if spec in ("*", "latest", "") or spec.startswith(("file:", "link:", "workspace:", "git", "http", "npm:")):
        return None
    for part in re.split(r"\s*\|\|\s*", spec):
        ok = True
        for clause in re.split(r"\s*,\s*|\s+(?=[<>=!~^])", part.strip()):
            m = re.match(r"^(\^|~=|~|>=|<=|>|<|==|=|!=)?\s*v?([\d.x*]+)", clause)
            if not m:
                continue
            op, ver = m.group(1) or "", m.group(2)
            rv = parse_version(ver.replace("x", "0").replace("*", "0"))
            if rv is None:
                continue
            if op in ("", "=", "==") and ("x" in ver or "*" in ver or ver.count(".") < 2):
                n = len([p for p in ver.split(".") if p not in ("x", "*")])
                ok &= iv[:n] == rv[:n]
            elif op in ("", "=", "=="):
                ok &= iv == rv
            elif op == "^":
                lead = 0 if rv[0] else (1 if rv[1] else 2)
                ok &= iv >= rv and iv[:lead + 1] == rv[:lead + 1]
            elif op == "~":
                ok &= iv >= rv and iv[:2] == rv[:2]
            elif op == "~=":
                n = max(1, ver.count("."))
                ok &= iv >= rv and iv[:n] == rv[:n]
            elif op == ">=":
                ok &= iv >= rv
            elif op == ">":
                ok &= iv > rv
            elif op == "<=":
                ok &= iv <= rv
            elif op == "<":
                ok &= iv < rv
            elif op == "!=":
                ok &= iv != rv
        if ok:
            return True
    return False


def node_versions(root):
    pkg = load_json(os.path.join(root, "package.json"))
    if not isinstance(pkg, dict):
        return []
    declared = {}
    for field in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        for name, spec in (pkg.get(field) or {}).items():
            declared.setdefault(name, spec)
    locked, lockfile = {}, None
    plock = load_json(os.path.join(root, "package-lock.json"))
    if isinstance(plock, dict):
        lockfile = "package-lock.json"
        for key, info in (plock.get("packages") or {}).items():
            if key.startswith("node_modules/") and "/node_modules/" not in key[13:]:
                locked[key[13:]] = info.get("version")
        for name, info in (plock.get("dependencies") or {}).items():
            locked.setdefault(name, info.get("version"))
    text = read(os.path.join(root, "pnpm-lock.yaml"))
    if text:
        lockfile = "pnpm-lock.yaml"
        block = re.search(r"(?ms)^importers:\s*\n\s+\.:\s*\n(.*?)(?=^\S|^  \S)", text)
        body = block.group(1) if block else text
        for m in re.finditer(r"(?m)^\s+'?((?:@[^/\s']+/)?[^:\s']+)'?:\s*\n\s+specifier:.*\n\s+version:\s*'?([^\s'(]+)", body):
            locked.setdefault(m.group(1), m.group(2))
    text = read(os.path.join(root, "yarn.lock"))
    if text:
        lockfile = "yarn.lock"
        for m in re.finditer(r'(?m)^"?((?:@[^/@\s]+/)?[^@\s"]+)@[^\n]*:\n(?:\s+.*\n)*?\s+version:?\s+"?([^"\s]+)', text):
            locked.setdefault(m.group(1), m.group(2))
    blk = load_json(os.path.join(root, "bun.lock"))
    if isinstance(blk, dict):
        lockfile = "bun.lock"
        for name, info in (blk.get("packages") or {}).items():
            if isinstance(info, list) and info and isinstance(info[0], str) and "@" in info[0][1:]:
                locked.setdefault(name, info[0].rsplit("@", 1)[1])
    out = []
    for name, spec in sorted(declared.items()):
        meta = load_json(os.path.join(root, "node_modules", *name.split("/"), "package.json"))
        installed = meta.get("version") if isinstance(meta, dict) else None
        out.append(row("npm", name, spec, locked.get(name), installed, lockfile))
    return out


def row(eco, name, spec, locked, installed, lockfile):
    status = "ok"
    if not installed:
        status = "not-installed"
    elif locked and parse_version(locked) and parse_version(installed) != parse_version(locked):
        status = "lock-mismatch"
    elif satisfies(installed, spec) is False:
        status = "range-mismatch"
    return {"ecosystem": eco, "name": name, "declared": spec, "locked": locked,
            "installed": installed, "lockfile": lockfile, "status": status}


PYTHON_OVERRIDE = []


def find_python(root):
    """The project's interpreter: --python, a local venv, VIRTUAL_ENV, Poetry's env, then this one."""
    if PYTHON_OVERRIDE:
        return PYTHON_OVERRIDE[0]
    cands = []
    for venv in (".venv", "venv", "env", ".env"):
        cands += [os.path.join(root, venv, "Scripts", "python.exe"), os.path.join(root, venv, "bin", "python")]
    if os.environ.get("VIRTUAL_ENV"):
        v = os.environ["VIRTUAL_ENV"]
        cands += [os.path.join(v, "Scripts", "python.exe"), os.path.join(v, "bin", "python")]
    for c in cands:
        if os.path.isfile(c):
            return c
    if os.path.isfile(os.path.join(root, "poetry.lock")) and shutil.which("poetry"):
        try:
            env = subprocess.run(["poetry", "env", "info", "-p"], cwd=root, capture_output=True,
                                 text=True, timeout=60).stdout.strip()
            for c in (os.path.join(env, "Scripts", "python.exe"), os.path.join(env, "bin", "python")):
                if env and os.path.isfile(c):
                    return c
        except (OSError, subprocess.TimeoutExpired):
            pass
    return sys.executable


PY_HELPER = r'''
import json, sys, importlib, importlib.util, inspect, warnings
req = json.loads(sys.stdin.read())
out = {"python": sys.version.split()[0], "executable": sys.executable}
try:
    import importlib.metadata as md
except ImportError:
    md = None
def norm(n):
    import re
    return re.sub(r"[-_.]+", "-", n).lower()
if req.get("dists") and md:
    out["dists"] = {norm(d.metadata["Name"] or ""): d.version for d in md.distributions() if d.metadata["Name"]}
    try:
        out["pkgdist"] = {k: v for k, v in md.packages_distributions().items()}
    except Exception:
        out["pkgdist"] = {}
out["stdlib"] = sorted(getattr(sys, "stdlib_module_names", ()))
def deprecated(obj):
    if getattr(obj, "__deprecated__", None):
        return str(obj.__deprecated__)
    doc = inspect.getdoc(obj) if (inspect.isfunction(obj) or inspect.isclass(obj) or inspect.ismethod(obj)) else None
    if doc:
        import re
        m = re.search(r"(?im)^\s*(?:\.\. deprecated::.*|deprecated[:.!].*|\*\*deprecated\*\*.*)$", doc)
        if m:
            return m.group(0).strip()[:160]
    return None
res = []
for item in req.get("checks", []):
    mod, names, attrs = item["module"], item.get("names", []), item.get("attrs", [])
    r = {"module": mod, "names": {}, "attrs": {}}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            m = importlib.import_module(mod)
            r["ok"] = True
            r["file"] = getattr(m, "__file__", None)
        except Exception as e:
            r["ok"] = False
            r["error"] = "%s: %s" % (type(e).__name__, str(e)[:500])
            m = None
        r["import_warnings"] = [str(w.message)[:200] for w in caught if issubclass(w.category, (DeprecationWarning, PendingDeprecationWarning, FutureWarning))]
    if m is not None:
        def resolve(base, chain):
            obj = base
            for part in chain.split("."):
                with warnings.catch_warnings(record=True) as cw:
                    warnings.simplefilter("always")
                    if hasattr(obj, part):
                        obj = getattr(obj, part)
                    elif inspect.ismodule(obj):
                        try:
                            obj = importlib.import_module(obj.__name__ + "." + part)
                        except Exception:
                            return {"exists": False, "missing": part}
                    else:
                        return {"exists": False, "missing": part}
                warn = [str(w.message)[:200] for w in cw if issubclass(w.category, (DeprecationWarning, PendingDeprecationWarning, FutureWarning))]
                if warn:
                    return {"exists": True, "deprecated": warn[0]}
            d = deprecated(obj)
            info = {"exists": True}
            if d:
                info["deprecated"] = d
            try:
                info["where"] = "%s:%s" % (inspect.getsourcefile(obj), inspect.getsourcelines(obj)[1])
            except Exception:
                pass
            return info
        for n in names:
            r["names"][n] = resolve(m, n)
        for a in attrs:
            r["attrs"][a] = resolve(m, a)
    res.append(r)
out["checks"] = res
if req.get("spec"):
    s = importlib.util.find_spec(req["spec"]) if req["spec"] else None
    out["spec"] = None if s is None else {"origin": s.origin, "locations": list(s.submodule_search_locations or [])}
print(json.dumps(out))
'''


def run_py_helper(root, request, timeout=120):
    py = find_python(root)
    try:
        p = subprocess.run([py, "-c", PY_HELPER], input=json.dumps(request), cwd=root,
                           capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": "could not run %s: %s" % (py, e), "interpreter": py}
    try:
        data = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": "interpreter %s failed: %s" % (py, (p.stderr or p.stdout)[-400:]), "interpreter": py}
    data["interpreter"] = py
    return data


def norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


TOML_ARRAY = r"(?:[^\]\"']|\"[^\"]*\"|'[^']*')*"
REQ_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*?)\s*(?:;.*)?$")


def python_declared(root):
    declared = {}
    text = read(os.path.join(root, "pyproject.toml")) or ""
    m = re.search(r"(?ms)^\[project\].*?^dependencies\s*=\s*\[(%s)\]" % TOML_ARRAY, text)
    blocks = [m.group(1)] if m else []
    for g in re.finditer(r"(?ms)^\[(?:project\.optional-dependencies|dependency-groups)\](.*?)(?=^\[|\Z)", text):
        blocks += re.findall(r"=\s*\[(%s)\]" % TOML_ARRAY, g.group(1), re.S)
    for b in blocks:
        for s in re.findall(r"[\"']([^\"']+)[\"']", b):
            rm = REQ_RE.match(s)
            if rm:
                declared.setdefault(norm(rm.group(1)), rm.group(3).strip())
    for g in re.finditer(r"(?ms)^\[tool\.poetry\.(?:group\.\w+\.)?(?:dev-)?dependencies\](.*?)(?=^\[|\Z)", text):
        for line in g.group(1).splitlines():
            lm = re.match(r"\s*([A-Za-z0-9][\w.-]*)\s*=\s*(?:\"([^\"]*)\"|\{[^}]*version\s*=\s*\"([^\"]*)\")", line)
            if lm and lm.group(1).lower() != "python":
                declared.setdefault(norm(lm.group(1)), (lm.group(2) or lm.group(3) or "").replace("^", "^"))
    for name in sorted(os.listdir(root)):
        if re.match(r"requirements.*\.(txt|in)$", name):
            for line in (read(os.path.join(root, name)) or "").splitlines():
                line = line.split("#")[0].strip()
                if not line or line.startswith(("-", "git+", "http")):
                    continue
                rm = REQ_RE.match(line)
                if rm:
                    declared.setdefault(norm(rm.group(1)), rm.group(3).strip())
    return declared


def python_locked(root):
    locked, lockfile = {}, None
    for name in ("uv.lock", "poetry.lock", "pdm.lock"):
        text = read(os.path.join(root, name))
        if text:
            lockfile = name
            for m in re.finditer(r'(?ms)^\[\[package\]\]\s*\nname\s*=\s*"([^"]+)"\s*\nversion\s*=\s*"([^"]+)"', text):
                locked.setdefault(norm(m.group(1)), m.group(2))
    return locked, lockfile


def python_versions(root):
    declared = python_declared(root)
    if not declared:
        return [], None
    locked, lockfile = python_locked(root)
    info = run_py_helper(root, {"dists": True})
    dists = info.get("dists") or {}
    out = []
    for name, spec in sorted(declared.items()):
        if not lockfile and spec.startswith("=="):
            locked.setdefault(name, spec[2:].strip())
        out.append(row("pypi", name, spec, locked.get(name), dists.get(name), lockfile))
    return out, info.get("interpreter") if "error" not in info else info["error"]


def go_versions(root):
    text = read(os.path.join(root, "go.mod"))
    if not text:
        return []
    out = []
    reqs = re.findall(r"(?m)^\s*(?:require\s+)?([\w.\-/]+\.[\w.\-/]+)\s+(v[\w.\-+]+)", text)
    cache = None
    if shutil.which("go"):
        try:
            cache = subprocess.run(["go", "env", "GOMODCACHE"], capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            cache = None
    for mod, ver in reqs:
        path = os.path.join(cache, go_escape(mod) + "@" + ver) if cache else None
        installed = ver if path and os.path.isdir(path) else None
        out.append(row("go", mod, ver, ver, installed, "go.mod"))
    return out


def go_escape(mod):
    return re.sub(r"[A-Z]", lambda m: "!" + m.group(0).lower(), mod)


def cargo_versions(root):
    text = read(os.path.join(root, "Cargo.toml"))
    lock = read(os.path.join(root, "Cargo.lock")) or ""
    if not text:
        return []
    declared = {}
    for g in re.finditer(r"(?ms)^\[(?:dev-|build-)?dependencies\](.*?)(?=^\[|\Z)", text):
        for line in g.group(1).splitlines():
            lm = re.match(r"\s*([\w-]+)\s*=\s*(?:\"([^\"]*)\"|\{[^}]*version\s*=\s*\"([^\"]*)\")", line)
            if lm:
                declared[lm.group(1)] = lm.group(2) or lm.group(3) or ""
    locked = {m.group(1): m.group(2) for m in re.finditer(r'(?m)^name = "([^"]+)"\nversion = "([^"]+)"', lock)}
    reg = os.path.join(os.environ.get("CARGO_HOME") or os.path.join(os.path.expanduser("~"), ".cargo"), "registry", "src")
    out = []
    for name, spec in sorted(declared.items()):
        ver = locked.get(name)
        installed = None
        if ver and os.path.isdir(reg):
            for idx in os.listdir(reg):
                if os.path.isdir(os.path.join(reg, idx, "%s-%s" % (name, ver))):
                    installed = ver
        out.append(row("cargo", name, "^" + spec if spec and spec[0].isdigit() else spec, ver, installed, "Cargo.lock" if lock else None))
    return out


def cmd_versions(args):
    root = os.path.abspath(args.root)
    rows = node_versions(root)
    py_rows, interp = python_versions(root)
    rows += py_rows + go_versions(root) + cargo_versions(root)
    if args.only:
        rows = [r for r in rows if r["name"] in args.only or norm(r["name"]) in map(norm, args.only)]
    result = {"root": root, "python": interp, "packages": rows,
              "problems": [r for r in rows if r["status"] != "ok"]}
    emit(result, args, render_versions)


def render_versions(res):
    lines = []
    if res["python"]:
        lines.append("python interpreter: %s" % res["python"])
    if not res["packages"]:
        lines.append("no dependency manifests found (package.json, pyproject.toml, requirements*.txt, go.mod, Cargo.toml)")
    for r in res["packages"]:
        flag = "" if r["status"] == "ok" else "  <-- " + r["status"]
        lines.append("%-6s %-32s declared %-12s locked %-12s installed %-12s%s" % (
            r["ecosystem"], r["name"], r["declared"] or "-", r["locked"] or "-", r["installed"] or "-", flag))
    if res["problems"]:
        lines.append("")
        lines.append("%d problem(s). not-installed: install before trusting any API; lock-mismatch: the install is stale, "
                     "reinstall from the lockfile; range-mismatch: installed version is outside the declared range." % len(res["problems"]))
    return "\n".join(lines)


# ------------------------------------------------------------------ node: where / exports

def node_pkg_dir(root, name):
    d = root
    while True:
        cand = os.path.join(d, "node_modules", *name.split("/"))
        if os.path.isfile(os.path.join(cand, "package.json")):
            return cand
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def split_specifier(spec):
    parts = spec.split("/")
    n = 2 if spec.startswith("@") else 1
    return "/".join(parts[:n]), "/".join(parts[n:])


def pick_types(entry):
    """From an exports-map entry, the types file (prefers 'types', then import/require/default)."""
    if isinstance(entry, str):
        return entry if entry.endswith((".d.ts", ".d.mts", ".d.cts")) else None
    if isinstance(entry, dict):
        if isinstance(entry.get("types"), str):
            return entry["types"]
        for key in ("import", "node", "require", "default"):
            if key in entry:
                t = pick_types(entry[key])
                if t:
                    return t
    return None


def node_types_file(pkg_dir, subpath, root):
    meta = load_json(os.path.join(pkg_dir, "package.json")) or {}
    exports = meta.get("exports")
    rel = None
    if isinstance(exports, dict):
        key = "./" + subpath if subpath else "."
        entry = exports.get(key)
        if entry is None and not any(k.startswith(".") for k in exports):
            entry = exports if not subpath else None
        if entry is None:
            for k, v in exports.items():
                if "*" in k and key.startswith(k.split("*")[0]):
                    star = key[len(k.split("*")[0]):len(key) - len(k.split("*")[1])] if k.split("*")[1] else key[len(k.split("*")[0]):]
                    t = pick_types(v)
                    rel = t.replace("*", star) if t else None
                    break
        else:
            rel = pick_types(entry)
    if not rel and not subpath:
        rel = meta.get("types") or meta.get("typings")
    cands = []
    if rel:
        cands.append(os.path.join(pkg_dir, rel))
    base = os.path.join(pkg_dir, subpath) if subpath else pkg_dir
    cands += [base + ".d.ts", os.path.join(base, "index.d.ts")]
    if not rel and not subpath and meta.get("main"):
        cands.append(os.path.join(pkg_dir, re.sub(r"\.[cm]?js$", "", meta["main"]) + ".d.ts"))
    for c in cands:
        if os.path.isfile(c):
            return os.path.normpath(c)
    name = os.path.basename(pkg_dir) if "@" not in pkg_dir else None
    meta_name = meta.get("name") or name
    if meta_name and not meta_name.startswith("@types/"):
        tname = "@types/" + meta_name.replace("@", "").replace("/", "__")
        tdir = node_pkg_dir(root, tname)
        if tdir:
            return node_types_file(tdir, subpath, root)
    return None


DECL_RE = re.compile(
    r"(?:/\*\*((?:(?!\*/).)*)\*/\s*)?export\s+(?:declare\s+)?(?:default\s+)?(?:abstract\s+)?(?:async\s+)?"
    r"(?:function\*?|const|let|var|class|interface|type|enum|namespace|module)\s+([A-Za-z_$][\w$]*)", re.S)
EXPORT_LIST_RE = re.compile(r"export\s+(?:type\s+)?\{([^}]*)\}\s*(?:from\s*['\"]([^'\"]+)['\"])?", re.S)
EXPORT_STAR_RE = re.compile(r"export\s+\*\s+(?:as\s+([\w$]+)\s+)?from\s*['\"]([^'\"]+)['\"]")
LOCAL_DECL_RE = r"(?:/\*\*((?:(?!\*/).)*)\*/\s*)?(?:export\s+)?(?:declare\s+)?(?:function|const|let|var|class|interface|type|enum|namespace)\s+%s\b"


def resolve_rel(base_file, spec):
    d = os.path.dirname(base_file)
    stem = os.path.normpath(os.path.join(d, spec))
    stem2 = re.sub(r"\.(m|c)?js$", "", stem)
    for c in (stem, stem2 + ".d.ts", stem2 + ".d.mts", stem2 + ".d.cts", stem + ".d.ts",
              os.path.join(stem, "index.d.ts"), stem2 + ".ts"):
        if os.path.isfile(c):
            return c
    return None


def dts_exports(path, root, seen=None):
    """name -> {"file", "line", "deprecated"} for everything a .d.ts file exports."""
    seen = seen if seen is not None else set()
    if not path or path in seen:
        return {}
    seen.add(path)
    text = read(path) or ""
    out = {}

    def add(name, pos, doc):
        dep = None
        if doc and "@deprecated" in doc:
            m = re.search(r"@deprecated\s*([^\n*]*)", doc)
            dep = (m.group(1).strip() if m else "") or "deprecated"
        line = text.count("\n", 0, pos) + 1
        prev = out.get(name)
        if prev and not prev.get("deprecated"):
            return  # overloads: one undeprecated signature is enough to be usable
        out[name] = {"file": path, "line": line, "deprecated": dep}

    for m in DECL_RE.finditer(text):
        add(m.group(2), m.start(2), m.group(1))
    for m in EXPORT_LIST_RE.finditer(text):
        src = m.group(2)
        for item in m.group(1).split(","):
            item = re.sub(r"^\s*type\s+", "", item.strip())
            if not item:
                continue
            parts = re.split(r"\s+as\s+", item)
            orig, alias = parts[0].strip(), parts[-1].strip()
            if src:
                target = resolve_rel(path, src) if src.startswith(".") else None
                sub = dts_exports(target, root, seen) if target else {}
                if orig in sub:
                    out[alias] = sub[orig]
                else:
                    out.setdefault(alias, {"file": path, "line": text.count("\n", 0, m.start()) + 1, "deprecated": None})
            else:
                lm = re.search(LOCAL_DECL_RE % re.escape(orig), text, re.S)
                if lm:
                    add(alias, lm.start(), lm.group(1))
                else:
                    out.setdefault(alias, {"file": path, "line": text.count("\n", 0, m.start()) + 1, "deprecated": None})
    for m in EXPORT_STAR_RE.finditer(text):
        target = resolve_rel(path, m.group(2)) if m.group(2).startswith(".") else None
        if m.group(1):
            out[m.group(1)] = {"file": path, "line": text.count("\n", 0, m.start()) + 1, "deprecated": None}
        elif target:
            for k, v in dts_exports(target, root, seen).items():
                out.setdefault(k, v)
    if re.search(r"export\s*=\s*([\w$]+)", text):
        out.setdefault("default", {"file": path, "line": 1, "deprecated": None})
    return out


NODE_KEYS = r'''
const spec = process.argv[1];
const {createRequire} = require("module");
const req = createRequire(process.cwd() + "/noop.js");
(async () => {
  let keys = null, how = null, err = null;
  try { const m = await import(req.resolve(spec).startsWith("node:") ? spec : require("url").pathToFileURL(req.resolve(spec)).href); keys = Object.keys(m); if (m.default && typeof m.default === "object") keys = keys.concat(Object.keys(m.default)); how = "import"; }
  catch (e) { err = String(e && e.message || e); }
  if (!keys) { try { const m = req(spec); keys = Object.keys(m); if (m && typeof m === "function") keys.push("default"); how = "require"; } catch (e) { err = err || String(e && e.message || e); } }
  console.log(JSON.stringify({keys, how, err}));
})();
'''


def node_runtime_keys(root, spec):
    node = shutil.which("node")
    if not node:
        return None
    try:
        p = subprocess.run([node, "-e", NODE_KEYS, spec], cwd=root, capture_output=True, text=True,
                           timeout=60, encoding="utf-8", errors="replace")
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return None


def list_docs(pkg_dir, limit=25):
    docs = []
    for name in sorted(os.listdir(pkg_dir)):
        full = os.path.join(pkg_dir, name)
        if os.path.isfile(full) and DOC_RE.match(name):
            docs.append(full)
        elif os.path.isdir(full) and name.lower() in ("docs", "doc", "documentation", "guides", "migrations"):
            for dp, _, files in os.walk(full):
                for f in sorted(files):
                    if f.lower().endswith((".md", ".mdx", ".txt", ".rst")):
                        docs.append(os.path.join(dp, f))
                if len(docs) >= limit:
                    break
    return docs[:limit]


# ------------------------------------------------------------------ where

def cmd_where(args):
    root = os.path.abspath(args.root)
    res = {"package": args.package}
    eco = args.ecosystem or guess_ecosystem(root, args.package)
    res["ecosystem"] = eco
    if eco == "npm":
        name, sub = split_specifier(args.package)
        d = node_pkg_dir(root, name)
        if not d:
            res["error"] = "%s is not installed in node_modules (run the project's install first)" % name
        else:
            meta = load_json(os.path.join(d, "package.json")) or {}
            res.update({"version": meta.get("version"), "dir": d,
                        "types": node_types_file(d, sub, root),
                        "entry": {k: meta.get(k) for k in ("main", "module", "exports") if meta.get(k)},
                        "source_dirs": [os.path.join(d, s) for s in ("src", "lib", "dist", "esm", "cjs") if os.path.isdir(os.path.join(d, s))],
                        "docs": list_docs(d)})
            if isinstance(res["entry"].get("exports"), dict):
                res["entry"]["exports"] = sorted(res["entry"]["exports"])[:40]
    elif eco == "pypi":
        info = run_py_helper(root, {"dists": True, "spec": None})
        mod, dist = py_names(args.package, info)
        spec = run_py_helper(root, {"spec": mod}).get("spec") if mod else None
        res["interpreter"] = info.get("interpreter")
        res["distribution"] = dist
        res["module"] = mod
        res["version"] = (info.get("dists") or {}).get(norm(dist or args.package))
        if not spec:
            res["error"] = "module %s is not importable with %s" % (mod or args.package, info.get("interpreter"))
        else:
            loc = spec["locations"][0] if spec["locations"] else spec["origin"]
            res["dir"] = loc
            res["source_dirs"] = [loc]
            if loc and os.path.isdir(loc):
                res["docs"] = list_docs(loc)
                stubs = [os.path.join(dp, f) for dp, _, fs in os.walk(loc) for f in fs if f.endswith(".pyi")][:10]
                res["types"] = stubs or None
                res["py_typed"] = os.path.isfile(os.path.join(loc, "py.typed"))
            site = os.path.dirname(loc) if loc else None
            if site and os.path.isdir(site):
                for n in os.listdir(site):
                    if n.endswith(".dist-info") and norm(n.split("-")[0]) == norm(dist or args.package):
                        res["metadata"] = os.path.join(site, n, "METADATA")
    elif eco in ("go", "cargo"):
        rows = go_versions(root) if eco == "go" else cargo_versions(root)
        hit = [r for r in rows if r["name"] == args.package]
        res.update(hit[0] if hit else {"error": "%s is not in %s" % (args.package, "go.mod" if eco == "go" else "Cargo.toml")})
    else:
        res["error"] = "can't tell which ecosystem %s belongs to; pass --ecosystem npm|pypi|go|cargo" % args.package
    emit(res, args, lambda r: json.dumps(r, indent=2))


def guess_ecosystem(root, pkg):
    name, _ = split_specifier(pkg)
    if node_pkg_dir(root, name):
        return "npm"
    if os.path.isfile(os.path.join(root, "go.mod")) and "/" in pkg and "." in pkg.split("/")[0]:
        return "go"
    if os.path.isfile(os.path.join(root, "Cargo.toml")) and not os.path.isfile(os.path.join(root, "pyproject.toml")):
        return "cargo"
    if any(os.path.isfile(os.path.join(root, f)) for f in ("pyproject.toml", "setup.py", "setup.cfg")) or \
            any(re.match(r"requirements.*\.txt$", f) for f in os.listdir(root)) or \
            os.path.isdir(os.path.join(root, ".venv")):
        return "pypi"
    if os.path.isfile(os.path.join(root, "package.json")):
        return "npm"
    return None


def py_names(pkg, info):
    """(import name, distribution name) for something the user called pkg."""
    pkgdist = info.get("pkgdist") or {}
    if pkg.split(".")[0] in pkgdist:
        return pkg, pkgdist[pkg.split(".")[0]][0]
    for mod, dists in pkgdist.items():
        if any(norm(d) == norm(pkg) for d in dists) and not mod.startswith("_"):
            return mod, dists[0]
    return pkg.replace("-", "_"), pkg


# ------------------------------------------------------------------ find

def cmd_find(args):
    root = os.path.abspath(args.root)
    eco = args.ecosystem or guess_ecosystem(root, args.package)
    dirs = []
    if eco == "npm":
        name, _ = split_specifier(args.package)
        d = node_pkg_dir(root, name)
        if d:
            dirs.append(d)
            meta = load_json(os.path.join(d, "package.json")) or {}
            t = node_pkg_dir(root, "@types/" + (meta.get("name") or name).replace("@", "").replace("/", "__"))
            if t:
                dirs.append(t)
        exts = (".d.ts", ".d.mts", ".d.cts", ".ts", ".mts", ".js", ".mjs", ".cjs")
    else:
        info = run_py_helper(root, {"dists": True})
        mod, _ = py_names(args.package, info)
        spec = run_py_helper(root, {"spec": mod.split(".")[0]}).get("spec") if mod else None
        if spec:
            dirs.append(spec["locations"][0] if spec["locations"] else spec["origin"])
        exts = (".pyi", ".py")
    if not dirs:
        emit({"package": args.package, "error": "%s is not installed for this project" % args.package}, args,
             lambda r: r["error"])
        return
    sym = re.escape(args.symbol)
    pat = re.compile(
        r"(?:\b(?:def|class|function|const|let|var|interface|type|enum|namespace)\s+%s\b"
        r"|^\s*(?:readonly\s+|static\s+|async\s+|public\s+|get\s+)*%s\s*[?]?\s*[(:<=]"
        r"|\bexport\s*\{[^}]*\b%s\b|^\s*%s\s*=)" % (sym, sym, sym, sym), re.M)
    hits = []
    for d in dirs:
        files = [d] if os.path.isfile(d) else (os.path.join(dp, f) for dp, dn, fs in os.walk(d)
                                                 for f in fs if f.endswith(exts) and "node_modules" not in dp[len(d):])
        for f in files:
            text = read(f) or ""
            if args.symbol not in text:
                continue
            for m in pat.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                before = text[max(0, m.start() - 1500):m.start()]
                doc = before[before.rfind("/**"):] if "/**" in before and "*/" in before[before.rfind("/**"):] and \
                    before[before.rfind("*/") + 2:].strip() == "" else ""
                after = text[m.end():m.end() + 1200]
                # only this definition's own body: stop at the next decorator or definition
                nxt = re.search(r"\n[ \t]*(?:@|def |async def |class )", after)
                after = after[:nxt.start()] if nxt else after
                pydep = re.search(r"(?i)(?:\.\. deprecated::|warnings\.warn\([^)]*Deprecat|@deprecated|DeprecationWarning)", after[:800]) \
                    if f.endswith((".py", ".pyi")) else None
                above = text[max(0, m.start() - 300):m.start()]
                deco = re.search(r"@(?:typing_extensions\.|warnings\.)?deprecated\(", above.split("\n\n")[-1])
                dep = None
                if "@deprecated" in doc:
                    dm = re.search(r"@deprecated\s*([^\n*]*)", doc)
                    dep = (dm.group(1).strip() if dm else "") or "deprecated"
                elif deco or pydep:
                    dep = "deprecated (see source)"
                sig = text[m.start():text.find("\n", m.end()) if text.find("\n", m.end()) > 0 else len(text)].strip()
                hits.append({"file": f, "line": line, "text": sig[:200], "deprecated": dep})
                if len(hits) >= args.limit:
                    break
            if len(hits) >= args.limit:
                break
    res = {"package": args.package, "symbol": args.symbol, "searched": dirs, "hits": hits}
    if not hits:
        res["note"] = "no definition of %s in the installed %s; it probably doesn't exist in this version" % (args.symbol, args.package)

    def render(r):
        out = ["searched: " + ", ".join(r["searched"])]
        for h in r["hits"]:
            out.append("%s:%d%s\n    %s" % (h["file"], h["line"], "  [DEPRECATED: %s]" % h["deprecated"] if h["deprecated"] else "", h["text"]))
        if r.get("note"):
            out.append(r["note"])
        return "\n".join(out)
    emit(res, args, render)


# ------------------------------------------------------------------ check

JS_IMPORT_RE = re.compile(
    r"""(?:^|[;\n])\s*(?:import|export)\s+(type\s+)?([\w$*\s{},]*?)\s*from\s*['"]([^'"]+)['"]"""
    r"""|(?:^|[;\n])\s*import\s*['"]([^'"]+)['"]"""
    r"""|(?:const|let|var)\s+(\{[^}]*\}|[\w$]+)\s*=\s*(?:await\s+)?(?:require|import)\(\s*['"]([^'"]+)['"]\s*\)""",
    re.M)


def js_imports(text):
    """[(specifier, names, namespace_alias, line)] from a JS/TS source file."""
    text_nc = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    text_nc = re.sub(r"(?m)//.*$", "", text_nc)
    out = []
    for m in JS_IMPORT_RE.finditer(text_nc):
        line = text_nc.count("\n", 0, m.start()) + 1 + (1 if text_nc[m.start():m.start() + 1] == "\n" else 0)
        names, ns = [], None
        if m.group(3):
            spec, clause = m.group(3), m.group(2) or ""
            br = re.search(r"\{([^}]*)\}", clause)
            if br:
                for item in br.group(1).split(","):
                    item = re.sub(r"^\s*type\s+", "", item.strip())
                    if item:
                        names.append(re.split(r"\s+as\s+", item)[0].strip())
            nsm = re.search(r"\*\s*as\s+([\w$]+)", clause)
            if nsm:
                ns = nsm.group(1)
            rest = re.sub(r"\{[^}]*\}|\*\s*as\s+[\w$]+|\*", "", clause).strip(" ,")
            if rest and re.match(r"^[\w$]+$", rest):
                names.append("default")
                ns = ns or None
        elif m.group(4):
            spec = m.group(4)
        else:
            spec, target = m.group(6), m.group(5)
            if target.startswith("{"):
                for item in target.strip("{} ").split(","):
                    item = item.strip()
                    if item:
                        names.append(re.split(r"\s*:\s*", item)[0].strip())
            else:
                ns = target
        out.append((spec, names, ns, line))
    return out


def is_bare(spec, root):
    if spec.startswith((".", "/", "#", "~/", "@/", "node:", "bun:", "data:", "http:", "https:", "virtual:")):
        return False
    if spec.split("/")[0] in NODE_BUILTINS:
        return False
    return True


def check_js(path, root, results):
    text = read(path) or ""
    cache = {}
    for spec, names, ns, line in js_imports(text):
        if not is_bare(spec, root):
            continue
        name, sub = split_specifier(spec)
        d = node_pkg_dir(root, name)
        if not d:
            if tsconfig_alias(root, spec):
                continue
            results.append(finding(path, line, spec, None, "missing-package",
                                   "%s is imported but not installed (not in node_modules)" % name))
            continue
        version = (load_json(os.path.join(d, "package.json")) or {}).get("version")
        if spec not in cache:
            tfile = node_types_file(d, sub, root)
            static = dts_exports(tfile, root) if tfile else {}
            rt = node_runtime_keys(root, spec)
            cache[spec] = (tfile, static, rt)
        tfile, static, rt = cache[spec]
        runtime_keys = set((rt or {}).get("keys") or [])
        if not static and rt and rt.get("keys") is None:
            results.append(finding(path, line, spec, version, "missing-module",
                                   "%s can't be loaded: %s" % (spec, rt.get("err"))))
            continue
        members = []
        if ns:
            members = sorted(set(re.findall(r"(?<![\w$.])%s\.([A-Za-z_$][\w$]*)" % re.escape(ns), text)))
        for n in names + members:
            if n == "default" and (static.get("default") or "default" in runtime_keys or rt and rt.get("how") == "require"):
                continue
            hit = static.get(n)
            if hit and hit.get("deprecated"):
                results.append(finding(path, line, spec, version, "deprecated",
                                       "%s is deprecated in %s@%s: %s" % (n, name, version, hit["deprecated"]),
                                       where="%s:%d" % (hit["file"], hit["line"])))
            elif hit or n in runtime_keys:
                results.append(finding(path, line, spec, version, "ok", n))
            elif not static and not runtime_keys:
                results.append(finding(path, line, spec, version, "unverified",
                                       "no types and no runtime exports found for %s; read the package source" % spec))
            else:
                results.append(finding(path, line, spec, version, "missing-name",
                                       "%s is not exported by %s@%s%s" % (n, name, version, " (%s)" % spec if sub else ""),
                                       where=tfile))


def tsconfig_alias(root, spec):
    cfg = load_json(os.path.join(root, "tsconfig.json")) or {}
    paths = ((cfg.get("compilerOptions") or {}).get("paths") or {})
    return any(spec == k or (k.endswith("*") and spec.startswith(k[:-1])) for k in paths)


def py_imports(path):
    import ast
    try:
        tree = ast.parse(read(path) or "", filename=path)
    except SyntaxError as e:
        return None, str(e)
    imports = []   # (module, [names], line)
    bound = {}     # local name -> module path or module.attr
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imports.append((a.name, [], node.lineno))
                if a.asname:
                    bound[a.asname] = a.name
                else:
                    bound[a.name.split(".")[0]] = a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = [a.name for a in node.names if a.name != "*"]
            imports.append((node.module, names, node.lineno))
            for a in node.names:
                if a.name != "*":
                    bound[a.asname or a.name] = node.module + ":" + a.name
    attrs = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            chain, cur = [], node
            while isinstance(cur, ast.Attribute):
                chain.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name) and cur.id in bound:
                chain.reverse()
                attrs.setdefault(cur.id, set()).add((".".join(chain), node.lineno))
    return (imports, bound, attrs), None


def local_module(root, top, path):
    for base in (root, os.path.join(root, "src"), os.path.dirname(path)):
        if os.path.isfile(os.path.join(base, top + ".py")) or os.path.isdir(os.path.join(base, top)):
            return True
    return False


def check_py(paths, root, results):
    parsed = {}
    for p in paths:
        data, err = py_imports(p)
        if err:
            results.append(finding(p, 0, None, None, "unparsed", err))
        else:
            parsed[p] = data
    if not parsed:
        return
    probe = run_py_helper(root, {"dists": True})
    if "error" in probe:
        results.append(finding(paths[0], 0, None, None, "unverified", probe["error"]))
        return
    stdlib = set(probe.get("stdlib") or [])
    pkgdist = probe.get("pkgdist") or {}
    dists = probe.get("dists") or {}
    checks, owners = {}, {}
    for p, (imports, bound, attrs) in parsed.items():
        for mod, names, line in imports:
            top = mod.split(".")[0]
            if top in stdlib or top == "__future__" or local_module(root, top, p):
                continue
            c = checks.setdefault(mod, {"module": mod, "names": set(), "attrs": set()})
            c["names"].update(names)
            owners.setdefault(mod, []).append((p, line, names))
        for local, uses in attrs.items():
            # local is bound to a module ("pkg.sub") or to an object ("pkg.sub:Name")
            mod, _, obj = bound[local].partition(":")
            top = mod.split(".")[0]
            if top in stdlib or top == "__future__" or local_module(root, top, p):
                continue
            c = checks.setdefault(mod, {"module": mod, "names": set(), "attrs": set()})
            for chain, line in uses:
                full = obj + "." + chain if obj else chain
                c["attrs"].add(full)
                owners.setdefault(mod, []).append((p, line, ["." + full]))
    if not checks:
        return
    req = {"checks": [{"module": k, "names": sorted(v["names"]), "attrs": sorted(v["attrs"])} for k, v in checks.items()]}
    res = run_py_helper(root, req)
    if "error" in res:
        results.append(finding(paths[0], 0, None, None, "unverified", res["error"]))
        return
    for r in res["checks"]:
        mod = r["module"]
        dist = (pkgdist.get(mod.split(".")[0]) or [mod.split(".")[0]])[0]
        version = dists.get(norm(dist))
        label = "%s %s" % (dist, version or "?")
        for p, line, names in owners.get(mod, []):
            if not r.get("ok"):
                status = "missing-package" if not version else "missing-module"
                results.append(finding(p, line, mod, version, status, "can't import %s (%s): %s" % (mod, label, r.get("error"))))
                break
            for w in r.get("import_warnings") or []:
                results.append(finding(p, line, mod, version, "deprecated", "importing %s warns: %s" % (mod, w)))
            for n in names:
                key = n[1:] if n.startswith(".") else n
                info = (r["attrs"] if n.startswith(".") else r["names"]).get(key)
                if info is None:
                    continue
                shown = "%s.%s" % (mod, key)
                if not info.get("exists"):
                    results.append(finding(p, line, mod, version, "missing-name",
                                           "%s does not exist in %s (missing: %s)" % (shown, label, info.get("missing"))))
                elif info.get("deprecated"):
                    results.append(finding(p, line, mod, version, "deprecated",
                                           "%s is deprecated in %s: %s" % (shown, label, info["deprecated"]), where=info.get("where")))
                else:
                    results.append(finding(p, line, mod, version, "ok", shown))


def finding(path, line, spec, version, status, detail, where=None):
    f = {"file": path, "line": line, "import": spec, "version": version, "status": status, "detail": detail}
    if where:
        f["where"] = where
    return f


def cmd_check(args):
    root = os.path.abspath(args.root)
    files = []
    for f in args.files:
        if os.path.isdir(f):
            for dp, dn, fs in os.walk(f):
                dn[:] = [d for d in dn if d not in SKIP_DIRS]
                files += [os.path.join(dp, x) for x in fs if x.endswith(JS_EXT + (".py",)) and not x.endswith(".d.ts")]
        else:
            files.append(f)
    files = [os.path.abspath(f) for f in files]
    results = []
    for f in files:
        if f.endswith(JS_EXT):
            check_js(f, root, results)
    check_py([f for f in files if f.endswith(".py")], root, results)
    for r in results:
        r["file"] = os.path.relpath(r["file"], root).replace(os.sep, "/") if r["file"] else r["file"]
    seen, unique = set(), []
    for r in results:
        key = (r["file"], r["line"], r["status"], r["detail"])
        if key not in seen:
            seen.add(key)
            unique.append(r)
    results = unique
    problems = [r for r in results if r["status"] != "ok"]
    res = {"root": root, "files": len(files), "checked": len(results), "problems": problems,
           "ok": [r for r in results if r["status"] == "ok"]}
    emit(res, args, render_check)
    if any(p["status"] in ("missing-package", "missing-module", "missing-name") for p in problems):
        sys.exit(1)


def render_check(res):
    lines = ["checked %d import(s)/name(s) in %d file(s): %d ok, %d problem(s)" % (
        res["checked"], res["files"], len(res["ok"]), len(res["problems"]))]
    for p in res["problems"]:
        lines.append("%s:%s  %-15s %s%s" % (p["file"], p["line"], p["status"], p["detail"],
                                            "\n    see %s" % p["where"] if p.get("where") else ""))
    return "\n".join(lines)


def emit(res, args, render):
    if getattr(args, "json", False):
        print(json.dumps(res, indent=2))
    else:
        print(render(res))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("versions", help="declared vs locked vs installed versions")
    p.add_argument("only", nargs="*", help="limit to these packages")
    p = sp.add_parser("where", help="where the installed copy of a package lives, with its docs")
    p.add_argument("package")
    p.add_argument("--ecosystem", choices=["npm", "pypi", "go", "cargo"])
    p = sp.add_parser("find", help="find a symbol's definition in an installed package")
    p.add_argument("package")
    p.add_argument("symbol")
    p.add_argument("--ecosystem", choices=["npm", "pypi"])
    p.add_argument("--limit", type=int, default=20)
    p = sp.add_parser("check", help="check third-party imports in files against installed packages")
    p.add_argument("files", nargs="+")
    for name, sub in sp.choices.items():
        sub.add_argument("--root", default=".", help="project root (default: current directory)")
        sub.add_argument("--json", action="store_true", help="print JSON")
        sub.add_argument("--python", help="the project's Python interpreter (default: .venv, VIRTUAL_ENV, Poetry env, else this one)")
    args = ap.parse_args(argv)
    PYTHON_OVERRIDE[:] = [args.python] if args.python else []
    {"versions": cmd_versions, "where": cmd_where, "find": cmd_find, "check": cmd_check}[args.cmd](args)


if __name__ == "__main__":
    main()
