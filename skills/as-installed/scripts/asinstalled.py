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
             installed packages: missing modules, missing names, deprecated;
             plus members used on what imported functions/classes return.

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
def returned_members(m, callee, members):
    """What calling m.<callee>(...) returns (the class itself, or the function's return
    annotation if it is a class), and whether each member exists on it."""
    import re, typing
    obj = m
    try:
        for part in callee.split("."):
            obj = getattr(obj, part)
    except Exception:
        return {"type": None, "why": "can't resolve %s" % callee}
    target = obj if inspect.isclass(obj) else None
    if target is None and callable(obj):
        try:
            hint = typing.get_type_hints(obj).get("return")
        except Exception:
            hint = getattr(obj, "__annotations__", {}).get("return")
        if typing.get_origin(hint) in (getattr(typing, "Awaitable", None), getattr(typing, "Coroutine", None)):
            hint = None
        if inspect.isclass(hint):
            target = hint
    if inspect.iscoroutinefunction(obj):
        target = None
    if target is None:
        return {"type": None, "why": "%s has no class return annotation" % callee}
    out = {"type": "%s.%s" % (target.__module__, target.__qualname__), "members": {}}
    mro = [c for c in inspect.getmro(target) if c is not object]
    dyn = any("__getattr__" in vars(c) or "__getattribute__" in vars(c) for c in mro)
    srcs, unreadable = [], False
    for c in mro:
        try:
            srcs.append(inspect.getsource(c))
        except Exception:
            unreadable = True
    for name in members:
        try:
            attr = inspect.getattr_static(target, name)
        except AttributeError:
            attr = None
            found = False
        else:
            found = True
        if found:
            fn = attr.fget if isinstance(attr, property) else getattr(attr, "__func__", attr)
            fn = getattr(fn, "func", fn)  # functools.cached_property
            info = {"exists": True}
            d = deprecated(fn) if (inspect.isfunction(fn) or inspect.isclass(fn)) else None
            if d:
                info["deprecated"] = d
            try:
                info["where"] = "%s:%s" % (inspect.getsourcefile(fn), inspect.getsourcelines(fn)[1])
            except Exception:
                pass
            out["members"][name] = info
            continue
        declared = any(name in getattr(c, "__annotations__", {}) for c in mro) or \
            name in (getattr(target, "__dataclass_fields__", None) or {}) or \
            name in (getattr(target, "model_fields", None) or {}) or \
            name in (getattr(target, "__fields__", None) or {}) or \
            any(name in (getattr(c, "__slots__", ()) or ()) for c in mro) or \
            any(re.search(r"\bself\.%s\s*(?::[^=\n]+)?=(?!=)" % re.escape(name), s) for s in srcs)
        if declared:
            out["members"][name] = {"exists": True}
        elif dyn or unreadable:
            out["members"][name] = {"exists": None}
        else:
            out["members"][name] = {"exists": False}
    return out
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
        r["returns"] = {}
        for callee, members in (item.get("returns") or {}).items():
            r["returns"][callee] = returned_members(m, callee, members)
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
LOCAL_DECL_RE = r"(?m)^[ \t]*(?:export\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?(?:function\*?|const|let|var|class|interface|type|enum|namespace)\s+%s(?![\w$])"


def resolve_rel(base_file, spec):
    d = os.path.dirname(base_file)
    stem = os.path.normpath(os.path.join(d, spec))
    stem2 = re.sub(r"\.(m|c)?js$", "", stem)
    # declaration files first: `./client.js` usually exists next to the `client.d.ts` we want
    ext = {".mjs": ".d.mts", ".cjs": ".d.cts"}.get(os.path.splitext(stem)[1], ".d.ts")
    for c in (stem2 + ext, stem2 + ".d.ts", stem2 + ".d.mts", stem2 + ".d.cts", stem + ".d.ts",
              os.path.join(stem, "index.d.ts"), stem if stem.endswith((".d.ts", ".d.mts", ".d.cts")) else None,
              stem2 + ".ts", stem):
        if c and os.path.isfile(c):
            return c
    return None


_DTS_CACHE = {}


def dts_exports(path, root, seen=None):
    """name -> {"file", "line", "deprecated"} for everything a .d.ts file exports."""
    if seen is None and path in _DTS_CACHE:
        return _DTS_CACHE[path]
    if seen is None:
        _DTS_CACHE[path] = out = _dts_exports(path, root, set())
        return out
    return _dts_exports(path, root, seen)


def _dts_exports(path, root, seen):
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
                lm = re.search(LOCAL_DECL_RE % re.escape(orig), text)
                if lm:
                    doc = jsdoc_before(text, lm.start())
                    add(alias, lm.start(), doc[3:] if doc else None)
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
    check_js_returned(path, text, root, results)


# ------------------------------------------------------------------ check: members of returned objects (JS/TS)
#
# `const r = await streamText(...); r.toX()` or `new Client().foo` or `streamText(...).toX()`:
# resolve the declared return type of the imported function (or the class) in the package's
# .d.ts files, then check each member used on the result against that type.

def blank_comments(text):
    """Same-length copy of text with comments and string contents replaced by spaces."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
        elif text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
        elif c in "'\"`":
            j = i + 1
            while j < n and text[j] != c and not (c != "`" and text[j] == "\n"):
                j += 2 if text[j] == "\\" else 1
            for k in range(i + 1, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j + 1
            continue
        else:
            i += 1
            continue
        for k in range(i, j):
            if out[k] != "\n":
                out[k] = " "
        i = j
    return "".join(out)


def match_close(text, i, open_ch="(", close_ch=")"):
    """Index of the bracket closing the one at text[i] (text should be comment-blanked)."""
    depth = 0
    for k in range(i, len(text)):
        if text[k] == open_ch:
            depth += 1
        elif text[k] == close_ch:
            depth -= 1
            if depth == 0:
                return k
    return -1


def match_angle(text, i):
    """Index of the '>' closing the '<' at text[i] ('=>' doesn't count); len(text) if unclosed."""
    depth = 0
    for k in range(i, len(text)):
        if text[k] == "<":
            depth += 1
        elif text[k] == ">" and text[k - 1] != "=":
            depth -= 1
            if depth == 0:
                return k
    return len(text)


def strip_generics(s):
    prev = None
    while prev != s:
        prev = s
        s = re.sub(r"<[^<>]*>", "", s.replace("=>", "->"))
    return s


def jsdoc_before(text, pos):
    before = text[:pos].rstrip()
    if before.endswith("*/"):
        start = before.rfind("/**")
        if start >= 0:
            return before[start:]
    return ""


def deprecation_of(doc):
    if doc and "@deprecated" in doc:
        m = re.search(r"@deprecated\b(.*?)(?=\n\s*\*\s*@|\*/|$)", doc, re.S)
        msg = re.sub(r"\s*\n\s*\*?\s*", " ", m.group(1)).strip() if m else ""
        return msg[:240] or "deprecated"
    return None


MEMBER_RE = re.compile(
    r"(?:^|[;{,\n])\s*(?:(?:readonly|static|get|set|abstract|public|protected|private|declare|async|override)\s+)*"
    r"(\[|['\"]?[A-Za-z_$][\w$]*['\"]?)\s*(\??)\s*([(:<]|;|$)", re.M)


def body_members(text, start, end):
    """{name: [deprecation-or-None, ...]} for the top-level members of the body text[start:end];
    '[' in the result marks an index signature (any member name allowed)."""
    clean = blank_comments(text[start:end])
    depth, top = 0, []
    for ch in clean:
        top.append(ch if depth == 0 else " ")
        if ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth -= 1
    flat = "".join(top)
    out = {}
    for m in MEMBER_RE.finditer(flat):
        name = m.group(1).strip("'\"")
        if name == "[" and not re.match(r"\s*\[\s*[\w$]+\s*:", clean[m.start(1):]):
            continue  # computed key like [Symbol.iterator], not an index signature
        if name in ("constructor", "new"):
            continue
        out.setdefault(name, []).append(deprecation_of(jsdoc_before(text, start + m.start(1))))
    return out


class DtsIndex:
    """Type declarations (interface/class/type) across a package's .d.ts files, loaded lazily."""

    def __init__(self, root):
        self.root = root
        self.pkgs = {}

    def files(self, pkg_dir):
        if pkg_dir not in self.pkgs:
            fs = []
            for dp, dn, names in os.walk(pkg_dir):
                dn[:] = [d for d in dn if d != "node_modules"]
                fs += [os.path.join(dp, f) for f in names if f.endswith((".d.ts", ".d.mts", ".d.cts"))]
            self.pkgs[pkg_dir] = fs
        return self.pkgs[pkg_dir]

    def decls(self, name, pkg_dir, ctx_file=None, depth=0):
        """[(file, kind, text, header_start, body_start, body_end)] for type `name`."""
        out = []
        pat = re.compile(r"(?:^|[\s;])(?:export\s+)?(?:declare\s+)?(?:abstract\s+)?(interface|class|type)\s+%s\b" % re.escape(name))
        for f in ([ctx_file] if ctx_file else []) + [x for x in self.files(pkg_dir) if x != ctx_file]:
            text = read(f) or ""
            if name not in text:
                continue
            clean = blank_comments(text)
            for m in pat.finditer(clean):
                kind = m.group(1)
                if kind == "type":
                    k = m.end()
                    while k < len(clean) and clean[k] in " \t\r\n":
                        k += 1
                    if k < len(clean) and clean[k] == "<":
                        k = match_angle(clean, k) + 1
                        while k < len(clean) and clean[k] in " \t\r\n":
                            k += 1
                    if k >= len(clean) or clean[k] != "=":
                        continue  # `type X` in an export list, not a declaration
                    eq = k
                    end = eq
                    d = 0
                    for k in range(eq + 1, len(clean)):
                        ch = clean[k]
                        if ch in "({[<":
                            d += 1
                        elif ch in ")}]>" and clean[k - 1] != "=":
                            d -= 1
                        elif ch == ";" and d <= 0:
                            end = k
                            break
                    else:
                        end = len(clean)
                    out.append((f, kind, text, m.start(1), eq + 1, end))
                else:
                    ob = clean.find("{", m.end())
                    cb = match_close(clean, ob, "{", "}") if ob >= 0 else -1
                    if cb > 0:
                        out.append((f, kind, text, m.start(1), ob, cb))
            if out:
                break
        if not out and ctx_file and depth < 3:
            # imported from another package: import { X as name } from 'pkg'
            text = read(ctx_file) or ""
            for m in re.finditer(r"import\s+(?:type\s+)?\{([^}]*)\}\s*from\s*['\"]([^'\"]+)['\"]", text):
                for item in m.group(1).split(","):
                    parts = re.split(r"\s+as\s+", re.sub(r"^\s*type\s+", "", item.strip()))
                    if parts[-1].strip() == name:
                        spec = m.group(2)
                        if spec.startswith("."):
                            target = resolve_rel(ctx_file, spec)
                            return self.decls(parts[0].strip(), pkg_dir, target, depth + 1) if target else []
                        d2 = node_pkg_dir(os.path.dirname(ctx_file), split_specifier(spec)[0])
                        if d2:
                            t = node_types_file(d2, split_specifier(spec)[1], self.root)
                            return self.decls(parts[0].strip(), d2, t, depth + 1)
        return out

    def members(self, name, pkg_dir, ctx_file=None, seen=None):
        """({member: [deprecations]}, complete, where) for type `name`; complete=False if part of the
        type couldn't be resolved, so a member missing from the result might still exist."""
        seen = seen if seen is not None else set()
        if (name, pkg_dir) in seen:
            return {}, True, None
        seen.add((name, pkg_dir))
        decls = self.decls(name, pkg_dir, ctx_file)
        if not decls:
            return {}, False, None
        out, complete = {}, True
        where = "%s:%d" % (decls[0][0], decls[0][2].count("\n", 0, decls[0][3]) + 1)

        def merge(res):
            nonlocal complete
            m, c, _ = res
            for k, v in m.items():
                out.setdefault(k, []).extend(v)
            complete = complete and c

        for f, kind, text, hs, bs, be in decls:
            clean = blank_comments(text)
            if kind == "type":
                merge(self.type_expr_members(clean[bs:be], text, bs, pkg_dir, f, seen))
                continue
            header = strip_generics(clean[hs:bs])
            em = re.search(r"\bextends\s+(.*?)(?:\bimplements\b|$)", header, re.S)
            if em:
                for base in em.group(1).split(","):
                    base = base.strip()
                    if re.match(r"^[A-Za-z_$][\w$]*$", base):
                        merge(self.members(base, pkg_dir, f, seen))
                    elif base:
                        complete = False
            merge((body_members(text, bs + 1, be), True, None))
        if "[" in out:
            complete = False
        return out, complete, where

    def pick_omit(self, which, targs, pkg_dir, ctx_file, seen):
        """Members of Pick<T, 'a' | 'b'> / Omit<T, 'a' | 'b'> when T is a named type and keys are literals."""
        clean = blank_comments(targs)
        depth, cut = 0, -1
        for k, ch in enumerate(clean):
            if ch in "<({[":
                depth += 1
            elif ch in ")}]" or (ch == ">" and clean[k - 1] != "="):
                depth -= 1
            elif ch == "," and depth == 0:
                cut = k
                break
        if cut < 0:
            return {}, False
        base = re.match(r"^\s*([A-Za-z_$][\w$]*)\s*(?:<.*>)?\s*$", targs[:cut], re.S)
        keys_txt = targs[cut + 1:].strip()
        if not base or not re.match(r"""^(?:\s*\|?\s*(?:'[^']*'|"[^"]*"))+\s*$""", keys_txt):
            return {}, False
        keys = set(re.findall(r"['\"]([^'\"]*)['\"]", keys_txt))
        ms, c, _ = self.members(base.group(1), pkg_dir, ctx_file, set(seen))
        if which == "Pick":
            # Pick's keys must exist on T, so they exist even where T itself didn't fully resolve
            return {k: ms.get(k, [None]) for k in keys}, True
        return {k: v for k, v in ms.items() if k not in keys}, c

    def type_expr_members(self, expr, text, offset, pkg_dir, ctx_file, seen):
        out, complete = {}, True
        flat = strip_generics(expr)
        if "|" in flat or "=>" in expr or "->" in flat:
            complete = False
        i = 0
        while i < len(expr):
            ch = expr[i]
            if ch == "{":
                cb = match_close(expr, i, "{", "}")
                if cb < 0:
                    break
                for k, v in body_members(text, offset + i + 1, offset + cb).items():
                    out.setdefault(k, []).extend(v)
                i = cb + 1
                continue
            m = re.match(r"[A-Za-z_$][\w$.]*", expr[i:])
            if m and (i == 0 or not re.match(r"[\w$.]", expr[i - 1])):
                ident = m.group(0)
                j = i + len(ident)
                rest = expr[j:].lstrip()
                targs = None
                if rest.startswith("<"):  # type arguments
                    k = expr.index("<", j)
                    e = match_angle(expr, k)
                    targs = text[offset + k + 1:offset + e]  # original text: keeps string literals
                    j = e + 1
                if ident in ("Pick", "Omit") and targs:
                    sub, c = self.pick_omit(ident, targs, pkg_dir, ctx_file, seen)
                    for k2, v in sub.items():
                        out.setdefault(k2, []).extend(v)
                    complete = complete and c
                elif ident in ("Partial", "Required", "Readonly") and targs and re.match(r"^\s*[A-Za-z_$][\w$]*\s*(?:<.*>)?\s*$", targs, re.S):
                    sub, c, _ = self.members(re.match(r"\s*([\w$]+)", targs).group(1), pkg_dir, ctx_file, seen)
                    for k2, v in sub.items():
                        out.setdefault(k2, []).extend(v)
                    complete = complete and c
                elif ident in ("Omit", "Pick", "Partial", "Required", "Readonly", "ReturnType", "Awaited") or "." in ident:
                    complete = False
                elif ident not in ("typeof", "keyof", "infer", "extends"):
                    sub, c, _ = self.members(ident, pkg_dir, ctx_file, seen)
                    for k2, v in sub.items():
                        out.setdefault(k2, []).extend(v)
                    complete = complete and c
                i = j
                continue
            i += 1
        return out, complete, None


def return_types(text, fname, awaited):
    """Type names function `fname` returns (all overloads declared in text).
    None if the return type isn't a plain (generic) type reference."""
    clean = blank_comments(text)
    names = []
    for m in re.finditer(r"(?:declare\s+)?function\s+%s(?![\w$])\s*" % re.escape(fname), clean):
        k = m.end()
        if k < len(clean) and clean[k] == "<":
            k = match_angle(clean, k) + 1
        k = clean.find("(", k)
        close = match_close(clean, k) if k >= 0 else -1
        rm = re.match(r"\s*:\s*([^;{]+)", clean[close + 1:]) if close > 0 else None
        if not rm:
            return None
        rt = strip_generics(rm.group(1)).strip()
        pm = re.match(r"^(?:Promise|PromiseLike)\s*$", rt)
        full = rm.group(1).strip()
        if pm or full.startswith(("Promise<", "PromiseLike<")):
            if not awaited:
                return []  # a promise; its members are Promise's
            inner = full[full.index("<") + 1:full.rindex(">")]
            rt = strip_generics(inner).strip()
        if not re.match(r"^[A-Za-z_$][\w$]*$", rt):
            return None
        names.append(rt)
    return names or None


def default_export_name(path):
    """(declared name, file declaring it) behind a .d.ts file's default export, or (None, None)."""
    text = blank_comments(read(path) or "")
    raw = read(path) or ""
    m = re.search(r"export\s+default\s+(?:declare\s+)?(?:abstract\s+)?(?:class|function|interface)?\s*([A-Za-z_$][\w$]*)", text)
    if m and m.group(1) not in ("class", "function", "interface"):
        return m.group(1), path
    m = re.search(r"export\s*=\s*([A-Za-z_$][\w$]*)", text)
    if m:
        return m.group(1), path
    for m in EXPORT_LIST_RE.finditer(raw):
        for item in m.group(1).split(","):
            parts = re.split(r"\s+as\s+", re.sub(r"^\s*type\s+", "", item.strip()))
            if len(parts) == 2 and parts[1].strip() == "default":
                src = m.group(2)
                target = resolve_rel(path, src) if src and src.startswith(".") else path
                return parts[0].strip(), target or path
    return None, None


CALL_SITE_RE = re.compile(r"(?<![\w$.])(new\s+)?([A-Za-z_$][\w$]*)((?:\s*\.\s*[A-Za-z_$][\w$]*)*)\s*(?:<[^<>()]*>)?\s*\(")
JS_GLOBAL_MEMBERS = {"then", "catch", "finally", "constructor", "toString", "valueOf", "hasOwnProperty"}


def js_bindings(text):
    """{local name: (specifier, exported name or '*')} for imports in a JS/TS file."""
    clean = blank_comments(text)
    out = {}
    for m in re.finditer(r"""import\s+(?:type\s+)?([\w$*\s{},]*?)\s*from\s*(['"])([^'"]+)\2""", text):
        if clean[m.start()] != "i":
            continue
        clause, spec = m.group(1), m.group(3)
        br = re.search(r"\{([^}]*)\}", clause)
        if br:
            for item in br.group(1).split(","):
                item = re.sub(r"^\s*type\s+", "", item.strip())
                if item:
                    parts = re.split(r"\s+as\s+", item)
                    out[parts[-1].strip()] = (spec, parts[0].strip())
        nsm = re.search(r"\*\s*as\s+([\w$]+)", clause)
        if nsm:
            out[nsm.group(1)] = (spec, "*")
        rest = re.sub(r"\{[^}]*\}|\*\s*as\s+[\w$]+|\*", "", clause).strip(" ,")
        if rest and re.match(r"^[\w$]+$", rest):
            out[rest] = (spec, "default")
    for m in re.finditer(r"""(?:const|let|var)\s+(\{[^}]*\}|[\w$]+)\s*=\s*require\(\s*['"]([^'"]+)['"]\s*\)""", text):
        target, spec = m.group(1), m.group(2)
        if target.startswith("{"):
            for item in target.strip("{} ").split(","):
                parts = re.split(r"\s*:\s*", item.strip())
                if parts[0]:
                    out[parts[-1].strip()] = (spec, parts[0].strip())
        else:
            out[target] = (spec, "*")
    return out


def check_js_returned(path, text, root, results, index=None):
    bindings = {k: v for k, v in js_bindings(text).items() if is_bare(v[0], root)}
    if not bindings:
        return
    index = index or DtsIndex(root)
    clean = blank_comments(text)
    sites = []  # (local var or None, callee label, spec, export name, is_new, awaited, line, direct member)
    for m in CALL_SITE_RE.finditer(clean):
        local = m.group(2)
        if local not in bindings:
            continue
        spec, exported = bindings[local]
        chain = [c for c in re.split(r"\s*\.\s*", m.group(3).strip()) if c]
        if exported == "*" and len(chain) == 1:
            exported = chain[0]
        elif exported == "*" or chain:
            continue  # a method on an object: return type not resolvable here
        is_new = bool(m.group(1))
        open_paren = m.end() - 1
        close = match_close(clean, open_paren)
        if close < 0:
            continue
        before = clean[:m.start()]
        vm = re.search(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=;]+)?=\s*(await\s+)?$", before)
        dm = re.match(r"\s*\.\s*([A-Za-z_$][\w$]*)", clean[close + 1:])
        line = clean.count("\n", 0, m.start()) + 1
        awaited = bool(vm and vm.group(2))
        if vm:
            sites.append((vm.group(1), local, spec, exported, is_new, awaited, line, None))
        elif dm and not re.search(r"await\s*$", before):
            sites.append((None, local, spec, exported, is_new, False, line, (dm.group(1), clean.count("\n", 0, close) + 1)))
    for var, callee, spec, exported, is_new, awaited, line, direct in sites:
        uses = []
        if direct:
            uses.append(direct)
        else:
            for um in re.finditer(r"(?<![\w$.])%s\s*\??\.\s*([A-Za-z_$][\w$]*)" % re.escape(var), clean):
                uses.append((um.group(1), clean.count("\n", 0, um.start()) + 1))
        uses = [(u, ln) for u, ln in uses if u not in JS_GLOBAL_MEMBERS]
        if not uses:
            continue
        name, sub = split_specifier(spec)
        d = node_pkg_dir(root, name)
        if not d:
            continue
        version = (load_json(os.path.join(d, "package.json")) or {}).get("version")
        tfile = node_types_file(d, sub, root)
        hit = dts_exports(tfile, root).get(exported) if tfile else None
        label = "%s(...)" % callee if not is_new else "new %s(...)" % callee
        types = None
        if hit and exported == "default":
            exported, deffile = default_export_name(tfile)
            hit = {"file": deffile} if exported else None
        if hit:
            dtext = read(hit["file"]) or ""
            if is_new:
                types = [exported]
            else:
                types = return_types(dtext, exported, awaited)
        if types == []:
            continue
        seen_use = set()
        for member, uline in uses:
            if member in seen_use:
                continue
            seen_use.add(member)
            shown = "%s.%s" % (var or label, member)
            if not types:
                results.append(finding(path, uline, spec, version, "unverified",
                                       "%s: can't resolve what %s returns in %s@%s; confirm %s with `find %s %s`"
                                       % (shown, label, name, version, member, name, member)))
                continue
            decls, complete, where = {}, True, None
            for t in types:
                ms, c, w = index.members(t, d, hit["file"])
                for k, v in ms.items():
                    decls.setdefault(k, []).extend(v)
                complete = complete and c
                where = where or w
            tname = " | ".join(types)
            if member in decls:
                deps = decls[member]
                if all(deps):
                    results.append(finding(path, uline, spec, version, "deprecated",
                                           "%s is deprecated in %s@%s (on %s): %s" % (shown, name, version, tname, deps[0]),
                                           where=where))
                else:
                    results.append(finding(path, uline, spec, version, "ok", "%s (on %s)" % (shown, tname)))
            elif complete and decls:
                results.append(finding(path, uline, spec, version, "missing-name",
                                       "%s: %s in %s@%s has no member %s" % (shown, tname, name, version, member),
                                       where=where))
            else:
                results.append(finding(path, uline, spec, version, "unverified",
                                       "%s: %s isn't fully resolvable in %s@%s; confirm %s with `find %s %s`"
                                       % (shown, tname, name, version, member, name, member), where=where))


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

    def callee_of(call):
        """(bound local, attribute chain) if call invokes something imported, else None."""
        if isinstance(call, ast.Await):
            call = call.value
        if not isinstance(call, ast.Call):
            return None
        chain, cur = [], call.func
        while isinstance(cur, ast.Attribute):
            chain.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name) and cur.id in bound:
            return cur.id, ".".join(reversed(chain))
        return None

    returned = {}  # (local, chain) -> set((member, line))
    assigned = {}  # variable -> (local, chain); variables assigned more than once are dropped
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) and node.value else []
        for t in targets:
            if isinstance(t, ast.Name):
                c = callee_of(node.value)
                assigned[t.id] = c if t.id not in assigned else None
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for it in node.items:  # `with Client() as c` is __enter__'s result: don't guess
                if isinstance(it.optional_vars, ast.Name):
                    assigned[it.optional_vars.id] = None
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Call):
            c = callee_of(node.value)
            if c:
                returned.setdefault(c, set()).add((node.attr, node.lineno))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and assigned.get(node.value.id):
            returned.setdefault(assigned[node.value.id], set()).add((node.attr, node.lineno))
    return (imports, bound, attrs, returned), None


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
    checks, owners, ret_owners = {}, {}, {}
    for p, (imports, bound, attrs, returned) in parsed.items():
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
        for (local, chain), uses in returned.items():
            mod, _, obj = bound[local].partition(":")
            top = mod.split(".")[0]
            callee = ".".join(x for x in (obj, chain) if x)
            if not callee or top in stdlib or top == "__future__" or local_module(root, top, p):
                continue
            c = checks.setdefault(mod, {"module": mod, "names": set(), "attrs": set()})
            for member, line in uses:
                c.setdefault("returns", {}).setdefault(callee, set()).add(member)
                ret_owners.setdefault(mod, []).append((p, line, callee, member))
    if not checks:
        return
    req = {"checks": [{"module": k, "names": sorted(v["names"]), "attrs": sorted(v["attrs"]),
                       "returns": {ck: sorted(cv) for ck, cv in v.get("returns", {}).items()}}
                      for k, v in checks.items()]}
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
        if not r.get("ok"):
            continue
        for p, line, callee, member in ret_owners.get(mod, []):
            rt = (r.get("returns") or {}).get(callee) or {}
            shown = "%s(...).%s" % (callee, member)
            if not rt.get("type"):
                results.append(finding(p, line, mod, version, "unverified",
                                       "%s: %s; confirm %s with `find %s %s`" % (shown, rt.get("why", "return type unknown"),
                                                                                 member, dist, member)))
                continue
            info = (rt.get("members") or {}).get(member) or {}
            if info.get("exists") is False:
                results.append(finding(p, line, mod, version, "missing-name",
                                       "%s: %s (%s) has no attribute %s" % (shown, rt["type"], label, member)))
            elif info.get("exists") is None:
                results.append(finding(p, line, mod, version, "unverified",
                                       "%s: %s is dynamic or compiled; confirm %s with `find %s %s`" % (shown, rt["type"], member, dist, member)))
            elif info.get("deprecated"):
                results.append(finding(p, line, mod, version, "deprecated",
                                       "%s is deprecated in %s: %s" % (shown, label, info["deprecated"]), where=info.get("where")))
            else:
                results.append(finding(p, line, mod, version, "ok", "%s (on %s)" % (shown, rt["type"])))


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
