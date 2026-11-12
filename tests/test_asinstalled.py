import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SCRIPT = os.path.join(REPO, "skills", "as-installed", "scripts", "asinstalled.py")
sys.path.insert(0, os.path.dirname(SCRIPT))

import asinstalled as ai  # noqa: E402

HAS_NODE = shutil.which("node") is not None


def write(root, files):
    for rel, text in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path) or root, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)


def run(argv):
    buf = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buf):
        try:
            ai.main(argv + ["--json"])
        except SystemExit as e:
            code = e.code or 0
    return json.loads(buf.getvalue()), code


# A fictional npm library whose v3 renamed createPool -> openPool and deprecated query().
TIDEWATER = {
    "node_modules/tidewater/package.json": json.dumps({
        "name": "tidewater", "version": "3.1.0", "main": "./dist/index.js",
        "exports": {".": {"types": "./dist/index.d.ts", "require": "./dist/index.js"},
                    "./testing": {"types": "./dist/testing.d.ts", "require": "./dist/testing.js"}}}),
    "node_modules/tidewater/dist/index.js": "exports.openPool = () => ({ run(){}, query(){} });\nexports.VERSION = '3.1.0';\n",
    "node_modules/tidewater/dist/index.d.ts": (
        "export * from './pool';\nexport declare const VERSION: string;\n"
        "/** @deprecated use openPool */\nexport declare function legacyConnect(): void;\n"),
    "node_modules/tidewater/dist/pool.d.ts": (
        "export interface Pool {\n  run(sql: string): Promise<unknown>;\n"
        "  /** @deprecated Use run() instead. */\n  query(sql: string): Promise<unknown>;\n}\n"
        "export declare function openPool(url: string): Pool;\n"),
    "node_modules/tidewater/dist/testing.js": "exports.mockPool = () => ({});\n",
    "node_modules/tidewater/dist/testing.d.ts": "export { mockPool } from './testing-impl';\n",
    "node_modules/tidewater/dist/testing-impl.d.ts": "export declare function mockPool(): unknown;\n",
    "node_modules/tidewater/CHANGELOG.md": "# 3.0.0\n- createPool renamed to openPool\n",
    "node_modules/untyped/package.json": json.dumps({"name": "untyped", "version": "1.0.0", "main": "index.js"}),
    "node_modules/untyped/index.js": "module.exports = { alpha: 1, beta: 2 };\n",
}


class TempRepo(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ait")
        self.old_pp = os.environ.get("PYTHONPATH")

    def tearDown(self):
        if self.old_pp is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = self.old_pp
        shutil.rmtree(self.root, ignore_errors=True)

    def site(self, files):
        """A fake site-packages on PYTHONPATH (inherited by the helper interpreter)."""
        site = os.path.join(self.root, "_site")
        write(site, files)
        os.environ["PYTHONPATH"] = site
        return site


class RangeTest(unittest.TestCase):
    def test_satisfies(self):
        cases = [("3.1.0", "^3.0.0", True), ("2.9.0", "^3.0.0", False), ("0.3.4", "^0.3.1", True),
                 ("0.4.0", "^0.3.1", False), ("1.2.9", "~1.2.0", True), ("1.3.0", "~1.2.0", False),
                 ("2.3.0", ">=2", True), ("1.9", ">=2,<3", False), ("2.1.0", "~=2.0", True),
                 ("3.0.0", "~=2.0", False), ("5.0.0", "4.x || 5.x", True), ("1.0.0", "==1.0.0", True),
                 ("1.0.1", "==1.0.0", False), ("1.0.0", "workspace:*", None), ("1.0.0", "*", None)]
        for inst, spec, want in cases:
            self.assertEqual(ai.satisfies(inst, spec), want, (inst, spec))


class VersionsTest(TempRepo):
    def test_npm_lock_and_install_mismatch(self):
        write(self.root, dict(TIDEWATER, **{
            "package.json": json.dumps({"dependencies": {"tidewater": "^3.0.0", "untyped": "^2.0.0", "absent": "^1.0.0"}}),
            "package-lock.json": json.dumps({"packages": {"node_modules/tidewater": {"version": "3.2.0"},
                                                          "node_modules/untyped": {"version": "1.0.0"}}})}))
        res, _ = run(["versions", "--root", self.root])
        st = {r["name"]: r["status"] for r in res["packages"] if r["ecosystem"] == "npm"}
        self.assertEqual(st, {"tidewater": "lock-mismatch", "untyped": "range-mismatch", "absent": "not-installed"})

    def test_pnpm_and_yarn_locks(self):
        write(self.root, dict(TIDEWATER, **{
            "package.json": json.dumps({"dependencies": {"tidewater": "^3.0.0"}}),
            "pnpm-lock.yaml": "lockfileVersion: '9.0'\n\nimporters:\n\n  .:\n    dependencies:\n      tidewater:\n"
                              "        specifier: ^3.0.0\n        version: 3.1.0\n\npackages:\n"}))
        res, _ = run(["versions", "--root", self.root])
        row = [r for r in res["packages"] if r["name"] == "tidewater"][0]
        self.assertEqual((row["locked"], row["status"]), ("3.1.0", "ok"))
        os.remove(os.path.join(self.root, "pnpm-lock.yaml"))
        write(self.root, {"yarn.lock": '"tidewater@^3.0.0":\n  version "3.0.5"\n  resolved "x"\n'})
        res, _ = run(["versions", "--root", self.root])
        row = [r for r in res["packages"] if r["name"] == "tidewater"][0]
        self.assertEqual((row["locked"], row["status"]), ("3.0.5", "lock-mismatch"))

    def test_python_declared_locked_installed(self):
        self.site({"fakepy/__init__.py": "",
                   "fakepy-2.0.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: fakepy\nVersion: 2.0.0\n",
                   "fakepy-2.0.0.dist-info/top_level.txt": "fakepy\n"})
        write(self.root, {"pyproject.toml": '[project]\nname = "x"\ndependencies = ["fakepy>=2,<3", "Not_Installed[extra]>=1"]\n',
                          "uv.lock": '[[package]]\nname = "fakepy"\nversion = "2.1.0"\n'})
        res, _ = run(["versions", "--root", self.root])
        st = {r["name"]: (r["installed"], r["locked"], r["status"]) for r in res["packages"]}
        self.assertEqual(st["fakepy"], ("2.0.0", "2.1.0", "lock-mismatch"))
        self.assertEqual(st["not-installed"][2], "not-installed")

    def test_requirements_pin(self):
        self.site({"fakepy-1.0.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: fakepy\nVersion: 1.0.0\n"})
        write(self.root, {"requirements.txt": "fakepy==1.2.0  # pinned\n-r other.txt\n"})
        res, _ = run(["versions", "--root", self.root])
        self.assertEqual(res["packages"][0]["status"], "lock-mismatch")

    def test_go_and_cargo_manifests(self):
        write(self.root, {"go.mod": "module x\n\ngo 1.22\n\nrequire (\n\tgithub.com/BurntSushi/toml v1.3.2\n)\n",
                          "Cargo.toml": '[package]\nname="x"\n\n[dependencies]\nserde = { version = "1.0", features = ["derive"] }\n',
                          "Cargo.lock": 'name = "serde"\nversion = "1.0.200"\n'})
        res, _ = run(["versions", "--root", self.root])
        names = {r["name"]: r for r in res["packages"]}
        self.assertEqual(names["github.com/BurntSushi/toml"]["locked"], "v1.3.2")
        self.assertEqual(names["serde"]["locked"], "1.0.200")
        self.assertEqual(ai.go_escape("github.com/BurntSushi/toml"), "github.com/!burnt!sushi/toml")


class DtsTest(TempRepo):
    def test_exports_follow_star_and_lists(self):
        write(self.root, TIDEWATER)
        d = os.path.join(self.root, "node_modules", "tidewater")
        tfile = ai.node_types_file(d, "", self.root)
        self.assertTrue(tfile.endswith(os.path.join("dist", "index.d.ts")))
        ex = ai.dts_exports(tfile, self.root)
        self.assertIn("openPool", ex)
        self.assertIn("Pool", ex)
        self.assertEqual(ex["legacyConnect"]["deprecated"], "use openPool")
        self.assertIsNone(ex["openPool"]["deprecated"])
        sub = ai.dts_exports(ai.node_types_file(d, "testing", self.root), self.root)
        self.assertIn("mockPool", sub)

    def test_types_package_fallback(self):
        write(self.root, {"node_modules/legacy/package.json": json.dumps({"name": "legacy", "version": "1.0.0", "main": "i.js"}),
                          "node_modules/@types/legacy/package.json": json.dumps({"name": "@types/legacy", "types": "index.d.ts"}),
                          "node_modules/@types/legacy/index.d.ts": "export function go(): void;\n"})
        tfile = ai.node_types_file(os.path.join(self.root, "node_modules", "legacy"), "", self.root)
        self.assertIn("@types", tfile)

    def test_js_imports(self):
        src = ("import def, { a, b as c, type T } from 'pkg';\nimport * as ns from \"@scope/lib/sub\";\n"
               "// import { gone } from 'commented';\nconst { x, y: z } = require('cjs');\nconst whole = require('w');\n"
               "import './side-effect.css';\nexport { e } from 'reexp';\n")
        got = {s: (sorted(n), ns) for s, n, ns, _ in ai.js_imports(src)}
        self.assertEqual(got["pkg"], (["T", "a", "b", "default"], None))
        self.assertEqual(got["@scope/lib/sub"], ([], "ns"))
        self.assertEqual(got["cjs"], (["x", "y"], None))
        self.assertEqual(got["w"], ([], "whole"))
        self.assertEqual(got["reexp"], (["e"], None))
        self.assertNotIn("commented", got)
        self.assertFalse(ai.is_bare("./x", self.root))
        self.assertFalse(ai.is_bare("node:fs", self.root))
        self.assertFalse(ai.is_bare("fs/promises", self.root))
        self.assertTrue(ai.is_bare("@scope/lib", self.root))


class CheckJsTest(TempRepo):
    def test_missing_deprecated_ok(self):
        write(self.root, dict(TIDEWATER, **{
            "package.json": json.dumps({"dependencies": {"tidewater": "^3.0.0"}}),
            "src/db.ts": ("import { openPool, createPool, legacyConnect, type Pool } from 'tidewater';\n"
                          "import { mockPool } from 'tidewater/testing';\nimport * as tw from 'tidewater';\n"
                          "import { thing } from 'not-installed';\nimport { local } from './local';\n"
                          "tw.VERSION; tw.nope;\n")}))
        res, code = run(["check", os.path.join(self.root, "src"), "--root", self.root])
        self.assertEqual(code, 1)
        probs = {(p["status"], p["detail"].split()[0]) for p in res["problems"]}
        self.assertIn(("missing-name", "createPool"), probs)
        self.assertIn(("missing-name", "nope"), probs)
        self.assertIn(("deprecated", "legacyConnect"), probs)
        self.assertIn(("missing-package", "not-installed"), probs)
        oks = {o["detail"] for o in res["ok"]}
        self.assertTrue({"openPool", "Pool", "mockPool", "VERSION"} <= oks)

    @unittest.skipUnless(HAS_NODE, "node not installed")
    def test_untyped_package_uses_runtime_exports(self):
        write(self.root, dict(TIDEWATER, **{"a.js": "const { alpha, gamma } = require('untyped');\n"}))
        res, _ = run(["check", os.path.join(self.root, "a.js"), "--root", self.root])
        self.assertEqual([o["detail"] for o in res["ok"]], ["alpha"])
        self.assertEqual([p["detail"].split()[0] for p in res["problems"]], ["gamma"])

    def test_members_of_returned_objects(self):
        extra = {
            "node_modules/tidewater/dist/stream.d.ts": (
                "import { Pool } from './pool';\n"
                "interface Base { close(): void; }\n"
                "interface StreamResult<T> extends Base {\n  readonly text: Promise<string>;\n"
                "  /**\n   * @deprecated Use `toStream` with\n   * `respond()` instead.\n   */\n"
                "  toResponse(init?: { status: number }): Response;\n  toStream(): ReadableStream<T>;\n}\n"
                "type Lite = Pick<StreamResult<string>, 'text' | 'close'>;\n"
                "type Loose = StreamResult<string> | Record<string, unknown>;\n"
                "declare function stream<T = string>(opts: { onChunk?: (c: T) => void }): StreamResult<T>;\n"
                "declare function liteStream(): Lite;\n"
                "declare function looseStream(): Loose;\n"
                "declare function fetchIt(): Promise<StreamResult<string>>;\n"
                "declare function weird(): { a: 1 }['a'];\n"
                "export { stream, liteStream, looseStream, fetchIt, weird, type StreamResult };\n"),
            "node_modules/tidewater/dist/index.d.ts": (
                "export * from './pool';\nexport * from './stream';\nexport declare const VERSION: string;\n"
                "/** @deprecated use openPool */\nexport declare function legacyConnect(): void;\n"),
            "package.json": json.dumps({"dependencies": {"tidewater": "^3.0.0"}}),
            "src/use.ts": (
                "import { openPool, stream, liteStream, looseStream, fetchIt, weird } from 'tidewater';\n"
                "import * as tw from 'tidewater';\n"
                "const pool = openPool('db://x');\npool.run('select 1');\npool.query('x');\npool.nope();\n"
                "const s = stream({ onChunk: (c) => { } });\ns.toStream(); s.close(); s.toResponse(); s.text;\n"
                "s.bogus;\n"
                "openPool('y').gone();\n"
                "const l = liteStream(); l.text; l.toStream();\n"
                "const z = looseStream(); z.whatever;\n"
                "async function f() { const r = await fetchIt(); r.toStream(); r.zap; }\n"
                "const p = fetchIt(); p.then(() => 1);\n"
                "const w = weird(); w.a;\n"
                "const n = tw.openPool('z'); n.missingToo();\n"
                "// pool.commented();\nconst str = 'pool.inString()';\n")}
        write(self.root, dict(TIDEWATER, **extra))
        res, code = run(["check", os.path.join(self.root, "src"), "--root", self.root])
        self.assertEqual(code, 1)
        probs = {(p["status"], p["detail"].split(":")[0].split(" ")[0]) for p in res["problems"]}
        expected = {
            ("deprecated", "pool.query"), ("missing-name", "pool.nope"),
            ("deprecated", "s.toResponse"), ("missing-name", "s.bogus"),
            ("missing-name", "openPool(...).gone"), ("missing-name", "l.toStream"),
            ("unverified", "z.whatever"), ("missing-name", "r.zap"), ("unverified", "w.a"),
            ("missing-name", "n.missingToo"),
        }
        self.assertEqual(probs, expected)
        dep = [p for p in res["problems"] if p["detail"].startswith("s.toResponse")][0]
        self.assertIn("Use `toStream` with `respond()` instead.", dep["detail"])
        oks = {o["detail"].split(" ")[0] for o in res["ok"]}
        self.assertTrue({"pool.run", "s.toStream", "s.close", "s.text", "l.text", "r.toStream"} <= oks, oks)

    def test_tsconfig_alias_is_not_a_package(self):
        write(self.root, {"tsconfig.json": '{\n  // comment\n  "compilerOptions": {"paths": {"$lib/*": ["src/lib/*"]},},\n}',
                          "x.ts": "import { a } from '$lib/a';\n"})
        res, code = run(["check", os.path.join(self.root, "x.ts"), "--root", self.root])
        self.assertEqual((res["problems"], code), ([], 0))


FAKEPY = {
    "fakepy/__init__.py": (
        "import warnings\nfrom .client import Client\n__all__ = ['Client', 'connect']\n"
        "def connect(url):\n    return Client()\n"
        "def old_connect(url):\n    '''Connect.\n\n    .. deprecated:: 2.0\n       Use connect().\n    '''\n"
        "    return Client()\n"
        "def __getattr__(name):\n    if name == 'LegacyClient':\n"
        "        warnings.warn('LegacyClient is deprecated; use Client', DeprecationWarning, stacklevel=2)\n"
        "        return Client\n    raise AttributeError(name)\n"),
    "fakepy/client.py": "class Client:\n    def send(self):\n        pass\n",
    "fakepy/server/__init__.py": "",
    "fakepy/server/app.py": "class App:\n    pass\n",
    "fakepy-2.0.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: fakepy\nVersion: 2.0.0\n",
    "fakepy-2.0.0.dist-info/top_level.txt": "fakepy\n",
}


class CheckPyTest(TempRepo):
    def test_names_attrs_modules(self):
        self.site(FAKEPY)
        write(self.root, {"app.py": (
            "import os, json\nfrom fakepy import Client, connect, old_connect, LegacyClient, vanished\n"
            "from fakepy.server.app import App\nfrom fakepy.server.gone import X\nimport fakepy as fp\n"
            "import fakepy.server.app\nimport notinstalled\nfrom mylocal import helper\n"
            "fp.connect('x'); fp.missing_fn(); Client.send; Client.nosuch\nfakepy.server.app.App\n"),
            "mylocal.py": "def helper(): pass\n"})
        res, code = run(["check", os.path.join(self.root, "app.py"), "--root", self.root])
        self.assertEqual(code, 1)
        probs = {(p["status"], p["detail"].split(" ")[0]) for p in res["problems"]}
        self.assertIn(("missing-name", "fakepy.vanished"), probs)
        self.assertIn(("missing-name", "fakepy.missing_fn"), probs)
        self.assertIn(("missing-name", "fakepy.Client.nosuch"), probs)
        self.assertIn(("deprecated", "fakepy.old_connect"), probs)
        self.assertIn(("deprecated", "fakepy.LegacyClient"), probs)
        statuses = {p["import"]: p["status"] for p in res["problems"]}
        self.assertEqual(statuses["fakepy.server.gone"], "missing-module")
        self.assertEqual(statuses["notinstalled"], "missing-package")
        self.assertNotIn("mylocal", statuses)
        self.assertNotIn("os", statuses)
        oks = {o["detail"] for o in res["ok"]}
        self.assertTrue({"fakepy.Client", "fakepy.connect", "fakepy.Client.send", "fakepy.server.app.App"} <= oks, oks)

    def test_members_of_returned_objects(self):
        self.site(dict(FAKEPY, **{
            "fakepy/client.py": (
                "import dataclasses\n"
                "@dataclasses.dataclass\nclass Result:\n    status: int\n    def json(self):\n        pass\n"
                "class Base:\n    def close(self):\n        pass\n"
                "class Client(Base):\n    retries = 3\n"
                "    def __init__(self):\n        self.timeout = 5\n"
                "    def send(self) -> Result:\n        return Result(200)\n"
                "    def post(self):\n        '''Post.\n\n        Deprecated: use send().\n        '''\n"
                "    @property\n    def url(self):\n        return ''\n"
                "class Dyn:\n    def __getattr__(self, n):\n        return 1\n"
                "def make() -> Result:\n    return Result(1)\n"
                "def untyped():\n    return Result(1)\n"),
            "fakepy/__init__.py": "from .client import Client, Dyn, Result, make, untyped\n"}))
        write(self.root, {"app.py": (
            "import fakepy\nfrom fakepy import Client, Dyn, make, untyped\n"
            "c = Client()\nc.send(); c.post(); c.timeout; c.retries; c.url; c.close(); c.nosuch()\n"
            "r = make()\nr.status; r.json(); r.text\n"
            "Client().gone\n"
            "d = Dyn(); d.anything\n"
            "u = untyped(); u.whatever\n"
            "k = fakepy.Client(); k.vanished\n"
            "x = Client()\nx = 3\nx.real\n"
            "with Client() as cm:\n    cm.ctx\n")})
        res, code = run(["check", os.path.join(self.root, "app.py"), "--root", self.root])
        self.assertEqual(code, 1)
        probs = {(p["status"], p["detail"].split(":")[0].split(" ")[0]) for p in res["problems"]}
        self.assertEqual(probs, {
            ("deprecated", "Client(...).post"), ("missing-name", "Client(...).nosuch"),
            ("missing-name", "make(...).text"), ("missing-name", "Client(...).gone"),
            ("unverified", "Dyn(...).anything"), ("unverified", "untyped(...).whatever"),
            ("missing-name", "Client(...).vanished"),
        })
        oks = {o["detail"].split(" ")[0] for o in res["ok"]}
        self.assertTrue({"Client(...).send", "Client(...).timeout", "Client(...).retries", "Client(...).url",
                         "Client(...).close", "make(...).status", "make(...).json"} <= oks, oks)

    def test_syntax_error_reported(self):
        write(self.root, {"bad.py": "def (:\n"})
        res, _ = run(["check", os.path.join(self.root, "bad.py"), "--root", self.root])
        self.assertEqual(res["problems"][0]["status"], "unparsed")


class WhereFindTest(TempRepo):
    def test_where_npm(self):
        write(self.root, dict(TIDEWATER, **{"package.json": "{}"}))
        res, _ = run(["where", "tidewater", "--root", self.root])
        self.assertEqual(res["version"], "3.1.0")
        self.assertTrue(res["types"].endswith("index.d.ts"))
        self.assertTrue(any(d.endswith("CHANGELOG.md") for d in res["docs"]))
        res, _ = run(["where", "nothing-here", "--root", self.root, "--ecosystem", "npm"])
        self.assertIn("not installed", res["error"])

    def test_find_npm_member_and_deprecation(self):
        write(self.root, dict(TIDEWATER, **{"package.json": "{}"}))
        res, _ = run(["find", "tidewater", "query", "--root", self.root])
        dts = [h for h in res["hits"] if h["file"].endswith("pool.d.ts")]
        self.assertEqual(dts[0]["deprecated"], "Use run() instead.")
        res, _ = run(["find", "tidewater", "run", "--root", self.root])
        self.assertTrue(any(h["file"].endswith("pool.d.ts") and not h["deprecated"] for h in res["hits"]))
        res, _ = run(["find", "tidewater", "createPool", "--root", self.root])
        self.assertEqual(res["hits"], [])
        self.assertIn("doesn't exist", res["note"])

    def test_where_and_find_python(self):
        self.site(FAKEPY)
        write(self.root, {"requirements.txt": "fakepy\n"})
        res, _ = run(["where", "fakepy", "--root", self.root])
        self.assertEqual(res["version"], "2.0.0")
        self.assertTrue(res["dir"].endswith("fakepy"))
        res, _ = run(["find", "fakepy", "old_connect", "--root", self.root])
        self.assertTrue(res["hits"] and res["hits"][0]["deprecated"])
        res, _ = run(["find", "fakepy", "send", "--root", self.root])
        self.assertTrue(any(h["file"].endswith("client.py") for h in res["hits"]))

    def test_find_python_deprecation_does_not_leak_to_previous_def(self):
        # The decorator belongs to put(); set(), defined just above it, is not deprecated.
        self.site({"kv/__init__.py": (
            "from warnings import deprecated\n\nclass Store:\n"
            "    def set(self, k, v):\n        '''Store v.'''\n        self.d[k] = v\n\n"
            "    @deprecated('use set()')\n    def put(self, k, v):\n        self.set(k, v)\n"),
            "kv-1.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: kv\nVersion: 1.0\n"})
        write(self.root, {"requirements.txt": "kv\n"})
        res, _ = run(["find", "kv", "set", "--root", self.root])
        self.assertEqual([h["deprecated"] for h in res["hits"] if h["text"].startswith("def set")], [None])
        res, _ = run(["find", "kv", "put", "--root", self.root])
        self.assertTrue([h for h in res["hits"] if h["text"].startswith("def put") and h["deprecated"]])


if __name__ == "__main__":
    unittest.main()
